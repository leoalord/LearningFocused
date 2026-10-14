"""Copy the Chroma collection into Postgres (`documents` + `chunks`), reusing its embeddings.

Used two ways:
- one-time backfill: `python -m src.database.pg_sync` (from a machine that has chroma_db/
  and can reach Postgres, e.g. through the IAP tunnel in docs/postgres.md)
- daily dual-write: the daily job calls `sync_from_chroma()` after the Chroma upsert, so
  Postgres follows Chroma until the MCP server switches to Postgres.

Nothing is re-embedded: Chroma already stores the text-embedding-3-small vectors. Rows
are upserted by Chroma id and only rewritten when content, metadata or vector changed.
`--prune` deletes Postgres chunks that are no longer in Chroma (off by default).
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Iterator, Sequence

if TYPE_CHECKING:
    import psycopg

DEFAULT_BATCH_SIZE = 500


@dataclass
class SyncStats:
    chroma_rows: int = 0
    documents_upserted: int = 0
    chunks_written: int = 0
    chunks_unchanged: int = 0
    chunks_without_embedding: int = 0
    chunks_pruned: int = 0


_YYYYMMDD = re.compile(r"^\d{8}$")


def parse_published(value: Any) -> datetime | None:
    """ISO 8601, RFC 822 (RSS) or YYYYMMDD (yt-dlp) -> aware datetime; else None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed: datetime | None = None
    if _YYYYMMDD.match(text):
        parsed = datetime.strptime(text, "%Y%m%d")
    else:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(text)
            except (TypeError, ValueError):
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def derive_document(meta: dict[str, Any]) -> dict[str, Any] | None:
    """The document a chunk belongs to, from the same metadata the MCP `fetch` filters on.

    Substack and YouTube chunks carry `doc_id`; podcast segments carry `episode_id`;
    podcast series summaries carry only `group_title`.
    """
    doc_id = meta.get("doc_id") or meta.get("episode_id")
    source_type = meta.get("source_type")
    title = meta.get("title")
    if not doc_id and meta.get("group_title"):
        doc_id = f"series:{meta['group_title']}"
        source_type = source_type or "podcast_series"
        title = title or meta["group_title"]
    if not doc_id:
        return None
    if not source_type and meta.get("episode_id"):
        source_type = "podcast"
    return {
        "doc_id": str(doc_id),
        "source_type": source_type,
        "title": title,
        "url": meta.get("url") or meta.get("canonical_url"),
        "published_at": parse_published(meta.get("published_at") or meta.get("upload_date")),
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
            if current.get(key) in (None, "") and value not in (None, ""):
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
    for doc in _merge_documents(metas):
        conn.execute(
            """
            INSERT INTO documents (doc_id, source_type, title, url, published_at)
            VALUES (%(doc_id)s, %(source_type)s, %(title)s, %(url)s, %(published_at)s)
            ON CONFLICT (doc_id) DO UPDATE SET
                source_type = COALESCE(EXCLUDED.source_type, documents.source_type),
                title = COALESCE(EXCLUDED.title, documents.title),
                url = COALESCE(EXCLUDED.url, documents.url),
                published_at = COALESCE(EXCLUDED.published_at, documents.published_at)
            WHERE (documents.source_type, documents.title, documents.url, documents.published_at)
                IS DISTINCT FROM (COALESCE(EXCLUDED.source_type, documents.source_type),
                                  COALESCE(EXCLUDED.title, documents.title),
                                  COALESCE(EXCLUDED.url, documents.url),
                                  COALESCE(EXCLUDED.published_at, documents.published_at))
            """,
            doc,
        )
        stats.documents_upserted += 1

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
        # executemany pipelines the batch: one round trip, not one per row.
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


def iter_chroma(collection: Any, batch_size: int) -> Iterator[tuple[list, list, list, Any]]:
    """Page through a Chroma collection: (ids, documents, metadatas, embeddings)."""
    offset = 0
    while True:
        got = collection.get(
            include=["documents", "metadatas", "embeddings"], limit=batch_size, offset=offset
        )
        ids = list(got.get("ids") or [])
        if not ids:
            return
        yield ids, list(got.get("documents") or []), list(got.get("metadatas") or []), got.get("embeddings")
        offset += len(ids)


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
) -> SyncStats:
    """Mirror the Chroma collection into Postgres. Each batch commits on its own."""
    from src.database import pg_store

    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    collection = collection if collection is not None else open_chroma_collection()
    stats = SyncStats()
    seen: list[str] = []
    for ids, documents, metadatas, embeddings in iter_chroma(collection, batch_size):
        with pg_store.session(dsn) as conn:
            upsert_batch(conn, ids, documents, metadatas, embeddings, stats)
        stats.chroma_rows += len(ids)
        if prune:
            seen.extend(str(i) for i in ids)
    if prune:
        if not seen:
            raise RuntimeError("Refusing to prune: the Chroma collection is empty.")
        with pg_store.session(dsn) as conn:
            conn.execute("CREATE TEMP TABLE keep_ids (id text PRIMARY KEY) ON COMMIT DROP")
            with conn.cursor() as cur:
                with cur.copy("COPY keep_ids (id) FROM STDIN") as copy:
                    for chunk_id in seen:
                        copy.write_row((chunk_id,))
                cur.execute("DELETE FROM chunks c WHERE NOT EXISTS (SELECT 1 FROM keep_ids k WHERE k.id = c.id)")
                stats.chunks_pruned = cur.rowcount
    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m src.database.pg_sync", description=__doc__.splitlines()[0])
    p.add_argument("--chroma-dir", help="Chroma directory (default: CHROMA_DIR)")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument("--prune", action="store_true", help="Delete Postgres chunks that are not in Chroma.")
    args = p.parse_args(argv)
    stats = sync_from_chroma(
        collection=open_chroma_collection(args.chroma_dir),
        batch_size=args.batch_size,
        prune=args.prune,
    )
    print(json.dumps(asdict(stats), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
