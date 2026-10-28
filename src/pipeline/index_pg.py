"""Index pipeline artifacts straight into Postgres (`documents` + `chunks`, pgvector embeddings).

This replaces the Chroma write path: the pipeline collectors build the same Documents
and ids as before, and this module embeds and upserts them into Postgres, where the
MCP server reads them (KB_BACKEND=postgres). Nothing goes through Chroma any more.

Rows keep the ids, text, metadata and embedding model that the Chroma -> Postgres
copy wrote, so the first run over an existing corpus finds every row unchanged and
embeds nothing. Each batch is compared with Postgres first (text, metadata and
document id); only new or changed rows are embedded, and each batch is committed on
its own, so a failed run keeps the batches it finished.

Pruning is opt-in, like the Chroma prune it replaces: `prune_stale` deletes segment
rows the artifacts on disk no longer produce (after a document-id change), only when
every artifact parsed, and not when it would delete more than `max_prune_fraction`
of that type's rows.

    uv run python -m src.pipeline.index_pg --dry-run     # count what would be embedded
    uv run python -m src.pipeline.index_pg --include-youtube
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Callable, Sequence

from src.database.pg_store import EMBEDDING_MODEL
from src.database.pg_sync import DEFAULT_MAX_PRUNE_FRACTION, SyncStats, changed_indexes, upsert_batch

if TYPE_CHECKING:
    import psycopg
    from langchain_core.documents import Document

EMBED_BATCH_SIZE = 100
PREFLIGHT_STATEMENT_TIMEOUT_MS = 5000
AUDIO_SEGMENT_TYPE = "transcript_segment"
YOUTUBE_SEGMENT_TYPE = "youtube_transcript_segment"

Embedder = Callable[[list[str]], list[list[float]]]


@dataclass
class IndexStats:
    collected: int = 0
    unique: int = 0
    duplicates: int = 0
    unreadable: int = 0
    unchanged: int = 0
    embedded: int = 0
    chunks_written: int = 0
    documents_upserted: int = 0
    chunks_pruned: int = 0
    documents_pruned: int = 0
    prune_skipped: str | None = None
    dry_run: bool = False


def require_postgres(dsn: str | None = None) -> None:
    """Fail before a pipeline downloads, transcribes or summarizes anything it couldn't index.

    A set PG_DSN is not enough: the server must answer (bounded by the connect and
    statement timeouts), the schema must be migrated, and this login must be able to
    write `chunks` and `documents`.
    """
    import psycopg

    from src.database import pg_store

    opt_out = "or pass --skip-index to run without indexing."
    if not (dsn or pg_store.is_configured()):
        raise RuntimeError(
            "PG_DSN is not set: chunks are indexed into Postgres. Set PG_DSN "
            f"(see docs/postgres.md), {opt_out}"
        )
    try:
        conn = pg_store.connect(dsn, autocommit=True,
                                extra_options=f"-c statement_timeout={PREFLIGHT_STATEMENT_TIMEOUT_MS}")
    except psycopg.Error as exc:
        raise RuntimeError(f"Postgres is unreachable ({type(exc).__name__}: {exc}). Fix PG_DSN, {opt_out}") from exc
    try:
        row = conn.execute(
            "SELECT to_regclass('public.chunks') IS NOT NULL AND to_regclass('public.documents') IS NOT NULL, "
            "current_setting('transaction_read_only') = 'on'"
        ).fetchone()
        migrated, read_only = (bool(row[0]), bool(row[1])) if row else (False, False)
        if not migrated:
            raise RuntimeError(
                f"Postgres has no chunks/documents tables: run `python -m src.database.pg_store migrate`, {opt_out}"
            )
        writable = False
        if not read_only:
            # has_table_privilege with a list is true when *any* privilege is held, so check each.
            priv = conn.execute(
                "SELECT bool_and(has_table_privilege(t, p)) "
                "FROM unnest(ARRAY['public.chunks', 'public.documents']) t, "
                "unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) p"
            ).fetchone()
            writable = bool(priv and priv[0])
        if not writable:
            raise RuntimeError(
                f"Postgres login {conn.info.user!r} cannot write chunks/documents (read-only role or "
                f"transaction). Use the ingest writer's PG_DSN, {opt_out}"
            )
    except psycopg.Error as exc:
        raise RuntimeError(f"Postgres preflight failed ({type(exc).__name__}: {exc}), {opt_out}") from exc
    finally:
        conn.close()


def collect_documents(
    *,
    include_audio: bool,
    include_articles: bool,
    include_youtube: bool,
    unreadable: list[str],
) -> list["Document"]:
    """The pipeline collectors' Documents; artifacts they could not parse go to `unreadable`."""
    from src.pipeline.audio.index_chroma import collect_audio_documents
    from src.pipeline.substack.index_chroma import collect_substack_documents
    from src.pipeline.youtube.index_chroma import collect_youtube_documents

    docs: list["Document"] = []
    if include_audio:
        docs.extend(collect_audio_documents(unreadable))
    if include_articles:
        docs.extend(collect_substack_documents())
    if include_youtube:
        docs.extend(collect_youtube_documents(unreadable))
    return docs


def to_rows(docs: Sequence["Document"], stats: IndexStats) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """(ids, texts, metadatas), one row per `_chroma_id` (the last artifact wins).

    `_chroma_id` is the collectors' name for the row id; it is not stored. Keys whose
    value is None are dropped, because Chroma never stored them and the rows it copied
    into Postgres don't have them: keeping them would make every such row look changed.
    """
    by_id: dict[str, "Document"] = {}
    for doc in docs:
        chunk_id = doc.metadata.get("_chroma_id")
        if chunk_id is None:
            raise ValueError("every collected Document needs metadata['_chroma_id'] (its row id)")
        if str(chunk_id) in by_id:
            stats.duplicates += 1
        by_id[str(chunk_id)] = doc
    ids = list(by_id)
    texts = [by_id[i].page_content for i in ids]
    metas = [
        {k: v for k, v in by_id[i].metadata.items() if k != "_chroma_id" and v is not None}
        for i in ids
    ]
    return ids, texts, metas


def openai_embedder() -> Embedder:
    from langchain_openai import OpenAIEmbeddings

    return OpenAIEmbeddings(model=EMBEDDING_MODEL).embed_documents


def write_rows(
    conn: "psycopg.Connection",
    ids: Sequence[str],
    texts: Sequence[str],
    metas: Sequence[dict[str, Any]],
    *,
    embed: Embedder | None,
    stats: IndexStats,
    batch_size: int = EMBED_BATCH_SIZE,
) -> None:
    """Embed and upsert the rows that are new or changed, committing per batch.

    With `embed=None` (dry run) nothing is embedded or written; the would-be count
    lands in `stats.embedded`.
    """
    upserts = SyncStats()
    for start in range(0, len(ids), batch_size):
        b_ids = list(ids[start:start + batch_size])
        b_texts = list(texts[start:start + batch_size])
        b_metas = list(metas[start:start + batch_size])
        todo = changed_indexes(conn, b_ids, b_texts, b_metas)
        stats.unchanged += len(b_ids) - len(todo)
        stats.embedded += len(todo)
        if not todo or embed is None:
            continue
        vectors = embed([b_texts[i] for i in todo])
        if len(vectors) != len(todo):
            raise RuntimeError(f"embedder returned {len(vectors)} vectors for {len(todo)} texts")
        upsert_batch(
            conn,
            [b_ids[i] for i in todo],
            [b_texts[i] for i in todo],
            [b_metas[i] for i in todo],
            vectors,
            upserts,
        )
        conn.commit()
    stats.chunks_written += upserts.chunks_written
    stats.documents_upserted += upserts.documents_upserted


def _skip_prune(stats: IndexStats, reason: str) -> None:
    stats.prune_skipped = f"{stats.prune_skipped}; {reason}" if stats.prune_skipped else reason


def prune_type(
    conn: "psycopg.Connection",
    doc_type: str,
    keep: set[str],
    *,
    max_fraction: float,
    stats: IndexStats,
) -> None:
    """Delete `doc_type` chunks not in `keep`, then those chunks' documents if left with no chunks.

    Only documents this prune emptied are removed: other documents without chunks (an
    article not yet indexed, say) are not this prune's to delete.
    """
    if not keep:
        _skip_prune(stats, f"no {doc_type} rows were collected")
        return
    keep_list = sorted(keep)
    row = conn.execute(
        "SELECT count(*) FILTER (WHERE NOT (id = ANY(%s))), count(*) FROM chunks WHERE type = %s",
        (keep_list, doc_type),
    ).fetchone()
    stale, total = (int(row[0]), int(row[1])) if row else (0, 0)
    if total and stale / total > max_fraction:
        _skip_prune(stats, (
            f"would delete {stale} of {total} {doc_type} chunks (> {max_fraction:.0%}); "
            "check the artifacts on disk, then prune with a higher --max-prune-fraction"
        ))
        return
    with conn.cursor() as cur:
        cur.execute("DELETE FROM chunks WHERE type = %s AND NOT (id = ANY(%s)) RETURNING doc_id",
                    (doc_type, keep_list))
        deleted = cur.fetchall()
        stats.chunks_pruned += len(deleted)
        affected = sorted({r[0] for r in deleted if r[0] is not None})
        if affected:
            cur.execute(
                "DELETE FROM documents d WHERE d.doc_id = ANY(%s) "
                "AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.doc_id = d.doc_id)",
                (affected,),
            )
            stats.documents_pruned += max(cur.rowcount, 0)
    conn.commit()


def update_pg_index(
    *,
    include_audio: bool = True,
    include_articles: bool = True,
    include_youtube: bool = False,
    prune_stale: bool = False,
    max_prune_fraction: float = DEFAULT_MAX_PRUNE_FRACTION,
    dry_run: bool = False,
    dsn: str | None = None,
    embed: Embedder | None = None,
    documents: Sequence["Document"] | None = None,
    unreadable: list[str] | None = None,
) -> IndexStats:
    """Collect the selected sources' artifacts and bring Postgres up to date with them.

    `documents`/`unreadable` replace the collectors (tests). Prunes run only after
    every batch is written, so a failed run never leaves a gap.
    """
    from src.database import pg_store

    if not dry_run:
        require_postgres(dsn)

    stats = IndexStats(dry_run=dry_run)
    unreadable = unreadable if unreadable is not None else []
    if documents is None:
        documents = collect_documents(
            include_audio=include_audio,
            include_articles=include_articles,
            include_youtube=include_youtube,
            unreadable=unreadable,
        )
    stats.collected = len(documents)
    stats.unreadable = len(unreadable)
    if unreadable:
        print(f"WARNING: {len(unreadable)} artifact(s) could not be read and were skipped:")
        for item in unreadable[:10]:
            print(f"  - {item}")
    ids, texts, metas = to_rows(documents, stats)
    stats.unique = len(ids)
    if not ids:
        print("No documents found to index.")
        return stats

    conn = pg_store.connect(dsn)
    try:
        write_rows(conn, ids, texts, metas, embed=None if dry_run else (embed or openai_embedder()), stats=stats)
        if prune_stale and not dry_run:
            if unreadable:
                stats.prune_skipped = (
                    f"{len(unreadable)} artifact(s) could not be read, so the collected ids are "
                    "incomplete and pruning would delete live rows"
                )
            else:
                segment_types = [t for t, on in ((AUDIO_SEGMENT_TYPE, include_audio),
                                                 (YOUTUBE_SEGMENT_TYPE, include_youtube)) if on]
                for doc_type in segment_types:
                    keep = {i for i, m in zip(ids, metas) if m.get("type") == doc_type}
                    prune_type(conn, doc_type, keep, max_fraction=max_prune_fraction, stats=stats)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    verb = "would embed" if dry_run else "embedded"
    print(
        f"Postgres index: {stats.unique} rows collected, {stats.unchanged} unchanged, "
        f"{verb} {stats.embedded}, wrote {stats.chunks_written} chunks"
        + (f", pruned {stats.chunks_pruned}" if stats.chunks_pruned else "")
        + (f" (prune skipped: {stats.prune_skipped})" if stats.prune_skipped else "")
    )
    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m src.pipeline.index_pg", description=__doc__.splitlines()[0])
    p.add_argument("--no-audio", action="store_true", help="Do not collect podcast documents.")
    p.add_argument("--no-articles", action="store_true", help="Do not collect Substack documents.")
    p.add_argument("--include-youtube", action="store_true", help="Also collect YouTube documents.")
    p.add_argument("--dry-run", action="store_true", help="Count new/changed rows; embed and write nothing.")
    p.add_argument("--prune-stale", action="store_true",
                   help="Delete segment rows of the collected sources that on-disk artifacts no longer "
                        "produce. Only safe when this machine holds the complete artifact set.")
    p.add_argument("--max-prune-fraction", type=float, default=DEFAULT_MAX_PRUNE_FRACTION,
                   help="Skip a prune that would delete more than this share of a type's rows.")
    args = p.parse_args(argv)
    stats = update_pg_index(
        include_audio=not args.no_audio,
        include_articles=not args.no_articles,
        include_youtube=args.include_youtube,
        prune_stale=args.prune_stale,
        max_prune_fraction=args.max_prune_fraction,
        dry_run=args.dry_run,
    )
    print(json.dumps(asdict(stats), indent=2))
    return 1 if stats.prune_skipped and args.prune_stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
