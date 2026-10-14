"""Copy the Chroma collection into Postgres (`documents` + `chunks`), reusing its embeddings.

Used two ways:
- one-time backfill: `python -m src.database.pg_sync` (from a machine that has chroma_db/
  and can reach Postgres, e.g. through the IAP tunnel in docs/postgres.md)
- daily dual-write: the daily job calls `sync_from_chroma()` after the Chroma upsert, so
  Postgres follows Chroma until the MCP server switches to Postgres.

Nothing is re-embedded: Chroma already stores the text-embedding-3-small vectors. Rows
are upserted by Chroma id. Each page of Chroma ids, texts and metadata is compared with
Postgres first, and vectors are read from Chroma only for rows that are new or changed,
so a daily run over an unchanged corpus moves little data.

Pruning deletes Postgres chunks that are no longer in Chroma (Chroma's own
`--prune-stale-chroma` runs remove rows), then documents left with no chunks. The daily
job prunes; the CLI prunes with `--prune`. Either way pruning is skipped, not forced,
when Chroma is empty or it would delete more than `max_prune_fraction` of the chunks.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from typing import TYPE_CHECKING, Any, Callable, Iterator, Sequence

from src.pipeline.audio.episode_ids import episode_key

if TYPE_CHECKING:
    import psycopg

DEFAULT_BATCH_SIZE = 500
# A daily prune that would remove more than this share of chunks is skipped: that many
# deletes means the Chroma copy is incomplete (a bad hydrate), not that content was retired.
DEFAULT_MAX_PRUNE_FRACTION = 0.1
# Fallback ids and titles the indexers write when the real value is missing. They are not
# identities, so they never become a document row or overwrite a real title.
_PLACEHOLDER_IDS = {"", "unknown", "unknown id"}
_PLACEHOLDER_TITLES = {"", "unknown", "unknown title", "unknown article", "untitled"}


@dataclass
class SyncStats:
    chroma_rows: int = 0
    documents_upserted: int = 0
    chunks_written: int = 0
    chunks_unchanged: int = 0
    chunks_without_embedding: int = 0
    chunks_pruned: int = 0
    documents_pruned: int = 0
    prune_skipped: str | None = None


_YYYYMMDD = re.compile(r"^\d{8}$")


def parse_published(value: Any) -> datetime | None:
    """ISO 8601, RFC 822 (RSS) or YYYYMMDD (yt-dlp) -> aware datetime; else None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed: datetime | None = None
    try:
        if _YYYYMMDD.match(text):
            parsed = datetime.strptime(text, "%Y%m%d")
        else:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _real(value: Any, placeholders: set[str]) -> str | None:
    text = str(value).strip() if value is not None else ""
    return None if text.lower() in placeholders else text


def derive_document(meta: dict[str, Any]) -> dict[str, Any] | None:
    """The document a chunk belongs to, from the same metadata the MCP `fetch` filters on.

    Substack and YouTube chunks carry `doc_id`. Podcast segments carry `episode_id`,
    which the feed reuses across recordings, so their document id pairs it with the
    episode title the same way the Chroma ids do (`podcast:<episode_key>`); the bare
    episode id is kept in the document's metadata. Podcast series summaries carry only
    `group_title`. Placeholder ids ("unknown") give no document.
    """
    source_type = meta.get("source_type")
    title = _real(meta.get("title"), _PLACEHOLDER_TITLES)
    extra: dict[str, Any] = {}
    doc_id = _real(meta.get("doc_id"), _PLACEHOLDER_IDS)
    if not doc_id and meta.get("episode_id") is not None:
        episode_id = _real(meta.get("episode_id"), _PLACEHOLDER_IDS)
        source_type = source_type or "podcast"
        if title:
            doc_id = f"podcast:{episode_key(episode_id or 'unknown', title)}"
        elif episode_id:
            doc_id = f"podcast:{episode_id}"
        if episode_id:
            extra["episode_id"] = episode_id
    if not doc_id and _real(meta.get("group_title"), _PLACEHOLDER_TITLES):
        doc_id = f"series:{meta['group_title']}"
        source_type = source_type or "podcast_series"
        title = title or str(meta["group_title"])
    if not doc_id:
        return None
    return {
        "doc_id": doc_id,
        "source_type": source_type,
        "title": title,
        "url": meta.get("url") or meta.get("canonical_url"),
        "published_at": parse_published(meta.get("published_at") or meta.get("upload_date")),
        "metadata": extra,
    }


def _merge_documents(metas: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per doc_id; the first non-empty value of each field wins."""
    docs: dict[str, dict[str, Any]] = {}
    for meta in metas:
        doc = derive_document(meta)
        if doc is None:
            continue
        current = docs.setdefault(doc["doc_id"], doc)
        for key, value in doc.items():
            if current.get(key) in (None, "", {}) and value not in (None, "", {}):
                current[key] = value
    return list(docs.values())


def upsert_batch(
    conn: "psycopg.Connection",
    ids: Sequence[str],
    documents: Sequence[str | None],
    metadatas: Sequence[dict[str, Any] | None],
    embeddings: Sequence[Any] | None,
    stats: SyncStats,
) -> None:
    """Upsert one batch of Chroma rows (documents first, for the chunks.doc_id FK)."""
    import numpy as np

    metas = [dict(m or {}) for m in metadatas]
    doc_rows = [{**d, "metadata": json.dumps(d["metadata"])} for d in _merge_documents(metas)]
    with conn.cursor() as cur:
        # executemany pipelines each statement: one round trip per batch, not per row.
        cur.executemany(
            """
            INSERT INTO documents (doc_id, source_type, title, url, published_at, metadata)
            VALUES (%(doc_id)s, %(source_type)s, %(title)s, %(url)s, %(published_at)s, %(metadata)s::jsonb)
            ON CONFLICT (doc_id) DO UPDATE SET
                source_type = COALESCE(EXCLUDED.source_type, documents.source_type),
                title = COALESCE(EXCLUDED.title, documents.title),
                url = COALESCE(EXCLUDED.url, documents.url),
                published_at = COALESCE(EXCLUDED.published_at, documents.published_at),
                metadata = documents.metadata || EXCLUDED.metadata
            WHERE (documents.source_type, documents.title, documents.url, documents.published_at,
                   documents.metadata)
                IS DISTINCT FROM (COALESCE(EXCLUDED.source_type, documents.source_type),
                                  COALESCE(EXCLUDED.title, documents.title),
                                  COALESCE(EXCLUDED.url, documents.url),
                                  COALESCE(EXCLUDED.published_at, documents.published_at),
                                  documents.metadata || EXCLUDED.metadata)
            """,
            doc_rows,
        )
        stats.documents_upserted += max(cur.rowcount, 0)

    rows = []
    for i, chunk_id in enumerate(ids):
        meta = metas[i]
        chunk_doc = derive_document(meta)
        vec = None
        if embeddings is not None and embeddings[i] is not None:
            vec = np.asarray(embeddings[i], dtype=np.float32)
        else:
            stats.chunks_without_embedding += 1
        rows.append((
            str(chunk_id),
            chunk_doc["doc_id"] if chunk_doc else None,
            str(meta.get("type") or "unknown"),
            documents[i] or "",
            json.dumps(meta, default=str),
            vec,
        ))
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO chunks (id, doc_id, type, content, metadata, embedding)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (id) DO UPDATE SET
                doc_id = EXCLUDED.doc_id, type = EXCLUDED.type, content = EXCLUDED.content,
                metadata = EXCLUDED.metadata, embedding = EXCLUDED.embedding
            WHERE (chunks.doc_id, chunks.type, chunks.content, chunks.metadata, chunks.embedding)
                IS DISTINCT FROM
                  (EXCLUDED.doc_id, EXCLUDED.type, EXCLUDED.content, EXCLUDED.metadata, EXCLUDED.embedding)
            """,
            rows,
        )
        written = max(cur.rowcount, 0)
    stats.chunks_written += written
    stats.chunks_unchanged += len(rows) - written


def _md5(text: str | None) -> str:
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()  # noqa: S324 - change check, not security


def changed_indexes(
    conn: "psycopg.Connection",
    ids: Sequence[str],
    documents: Sequence[str | None],
    metadatas: Sequence[dict[str, Any] | None],
) -> list[int]:
    """Positions of rows that are new in Postgres, differ from it, or have no vector there.

    Compares text (by md5), metadata and the derived doc_id, so only those rows need
    their 1536-d vectors read from Chroma.
    """
    existing = {
        row[0]: row[1:]
        for row in conn.execute(
            "SELECT id, doc_id, md5(content), metadata, embedding IS NOT NULL FROM chunks WHERE id = ANY(%s)",
            ([str(i) for i in ids],),
        ).fetchall()
    }
    changed = []
    for i, chunk_id in enumerate(ids):
        meta = dict(metadatas[i] or {})
        have = existing.get(str(chunk_id))
        doc = derive_document(meta)
        want = (
            doc["doc_id"] if doc else None,
            _md5(documents[i]),
            json.loads(json.dumps(meta, default=str)),
            True,
        )
        if have is None or tuple(have) != want:
            changed.append(i)
    return changed


def iter_chroma(collection: Any, batch_size: int) -> Iterator[tuple[list, list, list]]:
    """Page through a Chroma collection without vectors: (ids, documents, metadatas)."""
    offset = 0
    while True:
        got = collection.get(include=["documents", "metadatas"], limit=batch_size, offset=offset)
        ids = list(got.get("ids") or [])
        if not ids:
            return
        yield ids, list(got.get("documents") or []), list(got.get("metadatas") or [])
        offset += len(ids)


def chroma_embeddings(collection: Any) -> Callable[[list[str]], dict[str, Any]]:
    """Reader for the stored vectors of some ids (Chroma doesn't promise result order)."""

    def read(ids: list[str]) -> dict[str, Any]:
        if not ids:
            return {}
        got = collection.get(ids=ids, include=["embeddings"])
        vectors = got.get("embeddings")
        if vectors is None:
            return {}
        return dict(zip((str(i) for i in got.get("ids") or []), vectors))

    return read


def open_chroma_collection(chroma_dir: str | None = None, name: str | None = None) -> Any:
    """The persisted collection, opened read-only in spirit (no embedding function needed)."""
    import chromadb

    from src.config import CHROMA_DIR
    from src.database.chroma_manager import COLLECTION_NAME

    client = chromadb.PersistentClient(path=str(chroma_dir or CHROMA_DIR))
    return client.get_collection(name or COLLECTION_NAME)


def sync_from_chroma(
    dsn: str | None = None,
    *,
    collection: Any = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    prune: bool = False,
    max_prune_fraction: float = DEFAULT_MAX_PRUNE_FRACTION,
) -> SyncStats:
    """Mirror the Chroma collection into Postgres over one connection, committing per batch.

    A failed batch raises before any prune runs, so a partial read never deletes rows.
    """
    from src.database import pg_store

    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    collection = collection if collection is not None else open_chroma_collection()
    read_vectors = chroma_embeddings(collection)
    stats = SyncStats()
    seen: list[str] = []
    conn = pg_store.connect(dsn)
    try:
        for ids, documents, metadatas in iter_chroma(collection, batch_size):
            stats.chroma_rows += len(ids)
            if prune:
                seen.extend(str(i) for i in ids)
            todo = changed_indexes(conn, ids, documents, metadatas)
            stats.chunks_unchanged += len(ids) - len(todo)
            if todo:
                vectors = read_vectors([str(ids[i]) for i in todo])
                upsert_batch(
                    conn,
                    [ids[i] for i in todo],
                    [documents[i] for i in todo],
                    [metadatas[i] for i in todo],
                    [vectors.get(str(ids[i])) for i in todo],
                    stats,
                )
            conn.commit()
        if prune:
            _prune(conn, seen, max_prune_fraction, stats)
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return stats


def _prune(conn: "psycopg.Connection", keep: list[str], max_fraction: float, stats: SyncStats) -> None:
    """Delete chunks not in `keep`, then documents with no chunks, unless that looks wrong."""
    if not keep:
        stats.prune_skipped = "the Chroma collection is empty"
        return
    conn.execute("CREATE TEMP TABLE keep_ids (id text PRIMARY KEY) ON COMMIT DROP")
    with conn.cursor() as cur:
        with cur.copy("COPY keep_ids (id) FROM STDIN") as copy:
            for chunk_id in keep:
                copy.write_row((chunk_id,))
        row = cur.execute(
            "SELECT count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM keep_ids k WHERE k.id = c.id)), count(*) "
            "FROM chunks c"
        ).fetchone()
        stale, total = (int(row[0]), int(row[1])) if row else (0, 0)
        if total and stale / total > max_fraction:
            stats.prune_skipped = (
                f"would delete {stale} of {total} chunks (> {max_fraction:.0%}); "
                "check the Chroma copy, then prune with a higher --max-prune-fraction"
            )
            return
        cur.execute("DELETE FROM chunks c WHERE NOT EXISTS (SELECT 1 FROM keep_ids k WHERE k.id = c.id)")
        stats.chunks_pruned = max(cur.rowcount, 0)
        cur.execute("DELETE FROM documents d WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.doc_id = d.doc_id)")
        stats.documents_pruned = max(cur.rowcount, 0)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m src.database.pg_sync", description=__doc__.splitlines()[0])
    p.add_argument("--chroma-dir", help="Chroma directory (default: CHROMA_DIR)")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument("--prune", action="store_true",
                   help="Delete Postgres chunks that are not in Chroma, and documents left empty.")
    p.add_argument("--max-prune-fraction", type=float, default=DEFAULT_MAX_PRUNE_FRACTION,
                   help="Skip the prune if it would delete more than this share of chunks "
                        f"(default {DEFAULT_MAX_PRUNE_FRACTION}; 1 allows any).")
    args = p.parse_args(argv)
    stats = sync_from_chroma(
        collection=open_chroma_collection(args.chroma_dir),
        batch_size=args.batch_size,
        prune=args.prune,
        max_prune_fraction=args.max_prune_fraction,
    )
    print(json.dumps(asdict(stats), indent=2))
    return 1 if stats.prune_skipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
