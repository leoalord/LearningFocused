"""LLM graph extraction into the Postgres AGE graph `knowledge`.

This module owns:
- schema constraints (allowed nodes/relationships)
- graph extraction prompt construction
- LLM graph transformer invocation (hosted models, primary + fallbacks)
- schema filtering with drop accounting (`src/pipeline/graph_quality.py`)
- writing the result into AGE (`src/database/age_graph.py`), with per-edge provenance
- bookkeeping in the `ingested_docs` table: which passages were extracted, with which
  model, and how much the schema filter dropped

Every extracted relationship is stamped (on create) with `source_doc_id` (the
`Document.id` it came from), `extract_model` and `ingest_run_id`, so audits can check
an edge against the exact passage that produced it.

A passage is extracted once per version of its text: rows in `ingested_docs` are keyed
by `source_key` + `content_hash`, and only `completed` rows are skipped. A passage whose
Document vertex is already in the graph (copied from Neo4j by
`src/database/neo4j_to_age.py`) is recorded as completed without an LLM call.

Pipeline-specific modules own *what to index* (how to construct Documents from artifacts):
- `src/pipeline/audio/index_neo4j.py`
- `src/pipeline/substack/index_neo4j.py`

Needs PG_DSN and the API key of at least one model in LF_NEO4J_GRAPH_MODELS; no Neo4j.

    uv run python -m src.pipeline.index_graph --max-docs 20
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, List, Optional, Sequence, TypedDict, cast

from dotenv import load_dotenv
from langchain_core.documents import Document

from src.database.age_graph import AgeGraphWriter, GraphWriteStats, document_id
from src.llm.factory import RetryConfig, retryable_invoke
from src.pipeline.graph_quality import ExtractionStats, filter_to_schema

if TYPE_CHECKING:  # importing langchain_experimental is slow; only the extraction path needs it
    from langchain_experimental.graph_transformers import LLMGraphTransformer

# LangSmith tracing (optional at runtime; enabled via env vars).
try:
    from langsmith import traceable  # type: ignore
except Exception:  # pragma: no cover
    def traceable(*_args: Any, **_kwargs: Any):  # type: ignore
        def _decorator(fn):  # type: ignore
            return fn

        return _decorator


logger = logging.getLogger(__name__)

# Load env early so this module can be used as a CLI target.
load_dotenv()

DEFAULT_BATCH_SIZE = 5
NOTE_ALREADY_IN_GRAPH = "document already in graph; not re-extracted"


class _IngestItem(TypedDict, total=False):
    source_key: str
    content_hash: str
    source_type: str | None
    doc_id: str | None
    canonical_url: str | None
    title: str | None
    ingested_at: str
    ingest_run_id: str
    status: str


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_ingest_item(doc: Document, *, ingest_run_id: str) -> _IngestItem:
    """Build a stable ingest key for deduping LLM work."""
    source_type = cast(Optional[str], doc.metadata.get("source_type"))
    doc_id = cast(Optional[str], doc.metadata.get("doc_id"))
    episode_id = cast(Optional[str], doc.metadata.get("episode_id"))
    start_time = doc.metadata.get("start_time")
    topic = cast(Optional[str], doc.metadata.get("topic"))

    # Prefer stored content_hash for articles; otherwise hash the raw text content.
    content_hash = cast(Optional[str], doc.metadata.get("content_hash"))
    if not content_hash:
        content_hash = _sha256_text(doc.page_content)

    if doc_id:
        source_key = f"substack:{doc_id}"
    elif episode_id:
        source_key = f"audio:{episode_id}:{start_time or 'unknown'}:{topic or 'unknown'}"
    else:
        # Fallback for unknown sources
        source_key = f"unknown:{_sha256_text(str(doc.metadata))[:12]}"

    return {
        "source_key": source_key,
        "content_hash": content_hash,
        "source_type": source_type,
        "doc_id": doc_id,
        "canonical_url": cast(Optional[str], doc.metadata.get("canonical_url")),
        "title": cast(Optional[str], doc.metadata.get("title")),
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "ingest_run_id": ingest_run_id,
        "status": "pending",
    }


# ---------- ingested_docs bookkeeping ----------

_UPSERT_STATUS = """
INSERT INTO ingested_docs AS d (source_key, content_hash, source_type, doc_id, canonical_url, title,
                                status, graph_doc_id, last_ingest_run_id, note, error)
SELECT * FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[], %s::text[], %s::text[],
                     %s::text[], %s::text[], %s::text[], %s::text[], %s::text[])
ON CONFLICT (source_key, content_hash) DO UPDATE SET
    source_type = EXCLUDED.source_type,
    doc_id = EXCLUDED.doc_id,
    canonical_url = EXCLUDED.canonical_url,
    title = EXCLUDED.title,
    status = EXCLUDED.status,
    graph_doc_id = coalesce(EXCLUDED.graph_doc_id, d.graph_doc_id),
    last_ingest_run_id = EXCLUDED.last_ingest_run_id,
    last_ingested_at = now(),
    note = EXCLUDED.note,
    error = EXCLUDED.error
"""


def mark_ingest_status(
    conn: Any,
    items: Sequence[_IngestItem],
    status: str,
    *,
    graph_doc_ids: Sequence[str | None] | None = None,
    note: str | None = None,
    error: str | None = None,
) -> None:
    """Upsert `ingested_docs` rows for `items` with a status (started/completed/failed)."""
    if not items:
        return
    n = len(items)
    gids = list(graph_doc_ids) if graph_doc_ids is not None else [None] * n
    conn.execute(_UPSERT_STATUS, (
        [it.get("source_key") for it in items],
        [it.get("content_hash") for it in items],
        [it.get("source_type") for it in items],
        [it.get("doc_id") for it in items],
        [it.get("canonical_url") for it in items],
        [it.get("title") for it in items],
        [status] * n,
        gids,
        [it.get("ingest_run_id") for it in items],
        [note] * n,
        [error[:2000] if error else None] * n,
    ))


def record_extraction_stats(conn: Any, rows: Sequence[dict[str, Any]]) -> None:
    """Store per-passage extraction counts on its `ingested_docs` row."""
    for row in rows:
        conn.execute(
            """
            UPDATE ingested_docs SET extract_model = %s, raw_nodes = %s, kept_nodes = %s, raw_rels = %s,
                   kept_rels = %s, rels_dropped_for_endpoint = %s,
                   dropped_node_types = %s::jsonb, dropped_rel_types = %s::jsonb
            WHERE source_key = %s AND content_hash = %s
            """,
            (
                row["extract_model"], row["raw_nodes"], row["kept_nodes"], row["raw_rels"], row["kept_rels"],
                row["rels_dropped_for_endpoint"], json.dumps(row["dropped_node_types"]),
                json.dumps(row["dropped_rel_types"]), row["source_key"], row["content_hash"],
            ),
        )


def completed_keys(conn: Any, items: Sequence[_IngestItem]) -> set[tuple[str, str]]:
    if not items:
        return set()
    rows = conn.execute(
        "SELECT d.source_key, d.content_hash FROM ingested_docs d "
        "JOIN unnest(%s::text[], %s::text[]) AS t(k, h) ON d.source_key = t.k AND d.content_hash = t.h "
        "WHERE d.status = 'completed'",
        ([it.get("source_key") for it in items], [it.get("content_hash") for it in items]),
    ).fetchall()
    return {(str(r[0]), str(r[1])) for r in rows}


def _stats_rows(items: Sequence[_IngestItem], stats: Sequence[ExtractionStats], model: str) -> list[dict[str, Any]]:
    return [
        {
            "source_key": it.get("source_key"),
            "content_hash": it.get("content_hash"),
            "extract_model": model,
            "raw_nodes": st.raw_nodes,
            "kept_nodes": st.kept_nodes,
            "raw_rels": st.raw_rels,
            "kept_rels": st.kept_rels,
            "rels_dropped_for_endpoint": st.rels_dropped_for_endpoint,
            "dropped_node_types": dict(sorted(st.dropped_node_types.items())),
            "dropped_rel_types": dict(sorted(st.dropped_rel_types.items())),
        }
        for it, st in zip(items, stats)
    ]


# ---------- schema filter + provenance ----------

def apply_schema_and_provenance(
    graph_documents: Sequence[Any],
    *,
    allowed_nodes: list[str],
    allowed_relationships: list[str],
    extract_model: str,
    ingest_run_id: str | None,
) -> tuple[list[Any], list[ExtractionStats]]:
    """Filter each graph document to the schema and stamp edge provenance.

    Returns the filtered documents (same class as the input) and one stats object per
    input document.
    """
    out: list[Any] = []
    per_doc: list[ExtractionStats] = []
    for gd in graph_documents:
        nodes, rels, stats = filter_to_schema(
            gd.nodes,
            gd.relationships,
            allowed_nodes=allowed_nodes,
            allowed_relationships=allowed_relationships,
        )
        source_doc_id = document_id(gd.source) if gd.source is not None else None
        for rel in rels:
            props = dict(rel.properties or {})
            if source_doc_id:
                props["source_doc_id"] = source_doc_id
            props["extract_model"] = extract_model
            if ingest_run_id:
                props["ingest_run_id"] = ingest_run_id
            rel.properties = props
        out.append(type(gd)(nodes=nodes, relationships=rels, source=gd.source))
        per_doc.append(stats)
    return out, per_doc


def _parse_csv_env(name: str) -> list[str] | None:
    """Parse a comma-separated env var into a list of non-empty strings."""
    raw = os.getenv(name)
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",")]
    parts = [p for p in parts if p]
    return parts or None


_ALLOWED_NODES_CORE = ["Person", "Concept", "Organization", "Tool", "Event"]
_ALLOWED_RELATIONSHIPS_CORE = [
    "ADVOCATES_FOR",
    "CRITICIZES",
    "IMPLEMENTS",
    "ENABLES",
    "FOUNDED",
    "DISCUSSED",
]
_ALLOWED_RELATIONSHIPS_EXTENDED = _ALLOWED_RELATIONSHIPS_CORE + [
    "SUPPORTS",
    "CONTRADICTS",
    "EVIDENCE_FOR",
    "CAUSES",
    "INFLUENCES",
    "CORRELATES_WITH",
    "MEASURES",
    "RECOMMENDS",
    "COMPARED_TO",
    "APPLIES_TO",
]


def resolve_allowed_schema(schema_profile: str | None = None) -> tuple[list[str], list[str], str]:
    """Resolve allowed nodes/relationships for the graph transformer."""
    env_profile = os.getenv("NEO4J_SCHEMA_PROFILE", "extended")
    profile = (schema_profile if schema_profile is not None else env_profile).strip().lower()
    if profile not in {"core", "extended"}:
        profile = "extended"

    allowed_nodes = _parse_csv_env("NEO4J_ALLOWED_NODES") or _ALLOWED_NODES_CORE
    rel_defaults = _ALLOWED_RELATIONSHIPS_EXTENDED if profile == "extended" else _ALLOWED_RELATIONSHIPS_CORE
    allowed_relationships = _parse_csv_env("NEO4J_ALLOWED_RELATIONSHIPS") or rel_defaults
    return allowed_nodes, allowed_relationships, profile


def build_graph_extraction_spec(*, allowed_nodes: list[str], allowed_relationships: list[str]) -> str:
    """Build explicit extraction instructions to prepend to each Document."""
    return (
        "You are extracting a knowledge graph from educational content.\n\n"
        "Extract:\n"
        f"- Nodes limited to these types: {', '.join(allowed_nodes)}\n"
        "- Relationships limited to these types:\n"
        + "\n".join(f"  - {r}" for r in allowed_relationships)
        + "\n\n"
        "Rules:\n"
        "- Only create nodes/edges that are explicitly supported by the text (no guessing).\n"
        '- Prefer canonical, human-readable names for entities (e.g., "Alpha School", not abbreviations unless used in text).\n'
        "- If a relationship is ambiguous, omit it rather than inventing it.\n"
        '- If the text is mostly descriptive with no clear relationship, use "DISCUSSED" where appropriate.\n'
    )


def _wrap_for_graph_extraction(doc: Document, *, graph_extraction_spec: str) -> Document:
    """Prepend graph extraction instructions and basic provenance to the document text."""
    title = doc.metadata.get("title") or doc.metadata.get("doc_id") or doc.metadata.get("episode_id")
    canonical_url = doc.metadata.get("canonical_url")
    source_type = doc.metadata.get("source_type")
    topic = doc.metadata.get("topic")

    provenance = []
    if source_type:
        provenance.append(f"SourceType: {source_type}")
    if title:
        provenance.append(f"Title: {title}")
    if canonical_url:
        provenance.append(f"URL: {canonical_url}")
    if topic:
        provenance.append(f"Topic: {topic}")

    preamble = graph_extraction_spec + "\n"
    if provenance:
        preamble += "Provenance:\n" + "\n".join(f"- {p}" for p in provenance) + "\n\n"

    return Document(page_content=preamble + doc.page_content, metadata=doc.metadata)


def get_graph_transformer(*, allowed_nodes: list[str], allowed_relationships: list[str]) -> "LLMGraphTransformer":
    """Single-transformer builder (kept for external callers).

    Internally, this pipeline uses multiple models with fallback for resilience.
    """
    from langchain_experimental.graph_transformers import LLMGraphTransformer

    from src.pipeline.neo4j_llm_config import get_graph_llm_models

    llm_models = get_graph_llm_models(temperature=0.0)
    return LLMGraphTransformer(
        llm=cast(Any, llm_models[0]),
        allowed_nodes=allowed_nodes,
        allowed_relationships=allowed_relationships,
    )


def build_transformers(*, allowed_nodes: list[str], allowed_relationships: list[str]) -> list[Any]:
    """One transformer per configured model (primary + fallbacks)."""
    from langchain_experimental.graph_transformers import LLMGraphTransformer

    from src.pipeline.neo4j_llm_config import get_graph_llm_models

    transformers = []
    for m in get_graph_llm_models(temperature=0.0):
        # strict_mode=False: we filter in apply_schema_and_provenance so drops are counted.
        t = LLMGraphTransformer(
            llm=cast(Any, m),
            allowed_nodes=allowed_nodes,
            allowed_relationships=allowed_relationships,
            strict_mode=False,
        )
        # The transformer keeps only its chain, not the model, so remember the name for
        # `extract_model` provenance.
        t.lf_model_name = _model_name_for_llm(m)  # type: ignore[attr-defined]
        transformers.append(t)
    return transformers


@traceable(name="llm_graph_transform")
def _trace_llm_transform(llm_transformer: Any, batch: List[Document]) -> List[Any]:
    """LLM graph transformation for a batch (LangSmith-traced)."""
    return llm_transformer.convert_to_graph_documents(batch)


class _TransformerRunnable:
    """Adapter so we can reuse `retryable_invoke` around transformer conversion."""

    def __init__(self, transformer: Any):
        self._t = transformer

    def invoke(self, batch: List[Document]) -> List[Any]:
        return self._t.convert_to_graph_documents(batch)


def _model_name_for_llm(llm: Any) -> str:
    # Best-effort across providers.
    return (
        getattr(llm, "model", None)
        or getattr(llm, "model_name", None)
        or getattr(llm, "model_id", None)
        or "unknown-model"
    )


@dataclass
class GraphRunResult:
    ingest_run_id: str
    docs_seen: int = 0
    skipped_completed: int = 0
    skipped_in_graph: int = 0
    deferred: int = 0  # left for a later run by max_docs
    docs_processed: int = 0
    docs_failed: int = 0
    batches_failed: int = 0
    errors: list[str] = field(default_factory=list)
    extraction: ExtractionStats = field(default_factory=ExtractionStats)
    writes: GraphWriteStats = field(default_factory=GraphWriteStats)

    @property
    def ok(self) -> bool:
        return self.batches_failed == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "ingest_run_id": self.ingest_run_id,
            "docs_seen": self.docs_seen,
            "skipped_completed": self.skipped_completed,
            "skipped_in_graph": self.skipped_in_graph,
            "deferred": self.deferred,
            "docs_processed": self.docs_processed,
            "docs_failed": self.docs_failed,
            "batches_failed": self.batches_failed,
            "errors": self.errors[:20],
            "extraction": {
                "summary": self.extraction.summary(top=10),
                "raw_nodes": self.extraction.raw_nodes,
                "kept_nodes": self.extraction.kept_nodes,
                "raw_rels": self.extraction.raw_rels,
                "kept_rels": self.extraction.kept_rels,
                "rels_dropped_for_endpoint": self.extraction.rels_dropped_for_endpoint,
                "dropped_node_types": dict(self.extraction.dropped_node_types.most_common(20)),
                "dropped_rel_types": dict(self.extraction.dropped_rel_types.most_common(20)),
            },
            "writes": self.writes.to_dict(),
        }


def process_and_store(
    documents: List[Document],
    *,
    llm_transformers: Sequence[Any],
    ingest_items: list[_IngestItem],
    retry_cfg: RetryConfig,
    allowed_nodes: list[str],
    allowed_relationships: list[str],
    result: GraphRunResult,
    dsn: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> GraphRunResult:
    """Extract graph documents batch by batch and write each batch into AGE.

    Transformers must be built with `strict_mode=False`; schema filtering happens here
    so drops are counted. Each batch is marked `started` (committed) before the LLM
    call, then written to the graph and marked `completed` in one transaction; a batch
    that fails is marked `failed` so a later run retries it.
    """
    from src.database import pg_store

    total_docs = len(documents)
    logger.info("Processing %s documents...", total_docs)
    total_batches = (total_docs + batch_size - 1) // batch_size
    for i in range(0, total_docs, batch_size):
        batch = documents[i : i + batch_size]
        batch_items = ingest_items[i : i + batch_size]
        batch_num = i // batch_size + 1
        logger.info("Processing batch %s/%s", batch_num, total_batches)
        try:
            # Mark "started" BEFORE the LLM call and commit, so the attempt is on record
            # even if the process dies mid-call.
            with pg_store.session(dsn) as conn:
                mark_ingest_status(conn, batch_items, "started")

            t0 = time.perf_counter()
            # Try transformers in order (primary + fallbacks). Retry each on transient failures.
            graph_documents_raw: List[Any] | None = None
            last_err: Exception | None = None
            used_model = "unknown-model"
            for transformer in llm_transformers:
                model_name = getattr(transformer, "lf_model_name", None) or _model_name_for_llm(
                    getattr(transformer, "llm", None))
                try:
                    graph_documents_raw = retryable_invoke(_TransformerRunnable(transformer), batch, retry=retry_cfg)
                    used_model = model_name
                    logger.info("Batch %s graph transform model=%s", batch_num, model_name)
                    break
                except Exception as e:  # noqa: BLE001 - try the next model
                    last_err = e
                    logger.warning("Batch %s graph transform failed model=%s error=%s", batch_num, model_name, e)
            if graph_documents_raw is None:
                raise RuntimeError(f"All graph extraction models failed; last_error={last_err}")
            logger.info("Batch %s LLM transform time: %.2fs", batch_num, time.perf_counter() - t0)

            graph_documents, doc_stats = apply_schema_and_provenance(
                graph_documents_raw,
                allowed_nodes=allowed_nodes,
                allowed_relationships=allowed_relationships,
                extract_model=used_model,
                ingest_run_id=result.ingest_run_id,
            )
            batch_stats = ExtractionStats()
            for st in doc_stats:
                batch_stats.add(st)
            logger.info("Batch %s schema filter: %s", batch_num, batch_stats.summary())

            t2 = time.perf_counter()
            with pg_store.session(dsn) as conn:
                writes = AgeGraphWriter(conn).write(graph_documents)
                # The transformer returns one graph document per input, in order.
                aligned = len(doc_stats) == len(batch_items)
                gids = [document_id(d) for d in batch] if aligned else None
                mark_ingest_status(conn, batch_items, "completed", graph_doc_ids=gids)
                if aligned:
                    record_extraction_stats(conn, _stats_rows(batch_items, doc_stats, used_model))
                else:
                    logger.warning("Batch %s returned %s graph docs for %s inputs; per-doc stats not recorded",
                                   batch_num, len(doc_stats), len(batch_items))
            logger.info("Batch %s AGE write time: %.2fs %s", batch_num, time.perf_counter() - t2, writes.to_dict())
            result.extraction.add(batch_stats)
            result.writes.add(writes)
            result.docs_processed += len(batch)
        except Exception as e:  # noqa: BLE001 - one bad batch must not stop the run
            logger.error("Error processing batch starting at index %s: %s", i, e)
            result.batches_failed += 1
            result.docs_failed += len(batch)
            result.errors.append(f"batch {batch_num}: {type(e).__name__}: {e}")
            try:
                with pg_store.session(dsn) as conn:
                    mark_ingest_status(conn, batch_items, "failed", error=f"{type(e).__name__}: {e}")
            except Exception as mark_err:  # noqa: BLE001
                logger.error("Could not mark batch %s failed: %s", batch_num, mark_err)

    logger.info("Run schema filter totals: %s", result.extraction.summary(top=10))
    return result


def collect_documents(
    *,
    include_audio: bool = True,
    include_articles: bool = True,
    audio_limit: int | None = None,
    article_limit: int | None = None,
) -> list[Document]:
    """Source passages from the on-disk pipeline artifacts."""
    from src.config import SEGMENTED_DIR, SUBSTACK_METADATA_DIR, SUBSTACK_TEXT_DIR, ensure_data_dirs
    from src.pipeline.audio.index_neo4j import collect_audio_graph_documents
    from src.pipeline.substack.index_neo4j import collect_substack_graph_documents

    ensure_data_dirs()
    docs: list[Document] = []
    if include_audio:
        docs.extend(collect_audio_graph_documents(SEGMENTED_DIR, limit=audio_limit))
    if include_articles and SUBSTACK_TEXT_DIR.exists() and SUBSTACK_METADATA_DIR.exists():
        docs.extend(collect_substack_graph_documents(SUBSTACK_TEXT_DIR, SUBSTACK_METADATA_DIR, limit=article_limit))
    return docs


def update_knowledge_graph(
    *,
    include_audio: bool = True,
    include_articles: bool = True,
    audio_limit: int | None = None,
    article_limit: int | None = None,
    schema_profile: str | None = None,
    max_docs: int | None = None,
    dsn: str | None = None,
    documents: list[Document] | None = None,
    llm_transformers: Sequence[Any] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> GraphRunResult:
    """Extract a knowledge graph from new or changed passages and write it into AGE.

    `max_docs` caps how many passages are sent to the LLM this run (the rest wait for
    the next run). `documents` and `llm_transformers` replace the artifact loaders and
    the configured models (tests).
    """
    from src.database import pg_store

    dsn = dsn or pg_store.get_dsn()  # fail before any LLM work if Postgres isn't configured
    with pg_store.session(dsn) as conn:
        pg_store.ensure_graph(conn)

    docs = documents if documents is not None else collect_documents(
        include_audio=include_audio, include_articles=include_articles,
        audio_limit=audio_limit, article_limit=article_limit,
    )
    result = GraphRunResult(ingest_run_id=str(uuid.uuid4()), docs_seen=len(docs))

    allowed_nodes, allowed_relationships, profile = resolve_allowed_schema(schema_profile)
    graph_spec = build_graph_extraction_spec(allowed_nodes=allowed_nodes, allowed_relationships=allowed_relationships)

    items = [_build_ingest_item(d, ingest_run_id=result.ingest_run_id) for d in docs]
    with pg_store.session(dsn) as conn:
        done = completed_keys(conn, items)
    pending = [(d, it) for d, it in zip(docs, items) if (it["source_key"], it["content_hash"]) not in done]
    result.skipped_completed = len(docs) - len(pending)
    if result.skipped_completed:
        logger.info("Skipping %s documents already extracted (dedupe)", result.skipped_completed)

    wrapped = [(_wrap_for_graph_extraction(d, graph_extraction_spec=graph_spec), it) for d, it in pending]
    # A passage whose Document vertex already exists was extracted before (typically into
    # Neo4j, then copied by neo4j_to_age): record it instead of paying for it again.
    if wrapped:
        with pg_store.session(dsn) as conn:
            in_graph = AgeGraphWriter(conn).existing_document_ids(document_id(w) for w, _ in wrapped)
            seen = [(w, it) for w, it in wrapped if document_id(w) in in_graph]
            if seen:
                mark_ingest_status(conn, [it for _, it in seen], "completed",
                                   graph_doc_ids=[document_id(w) for w, _ in seen], note=NOTE_ALREADY_IN_GRAPH)
        result.skipped_in_graph = len(seen)
        wrapped = [(w, it) for w, it in wrapped if document_id(w) not in in_graph]

    if max_docs is not None and len(wrapped) > max_docs:
        result.deferred = len(wrapped) - max_docs
        wrapped = wrapped[:max_docs]

    langsmith_project = os.getenv("LANGCHAIN_PROJECT")
    for d, _ in wrapped:
        d.metadata["ingest_run_id"] = result.ingest_run_id
        if langsmith_project:
            d.metadata["langsmith_project"] = langsmith_project
    logger.info(
        "Graph extraction ingest_run_id=%s schema_profile=%s allowed_nodes=%d allowed_relationships=%d "
        "to_extract=%d deferred=%d langsmith_project=%s",
        result.ingest_run_id, profile, len(allowed_nodes), len(allowed_relationships),
        len(wrapped), result.deferred, langsmith_project,
    )
    if not wrapped:
        logger.info("No documents to process.")
        return result

    from src.pipeline.neo4j_llm_config import get_graph_retry_config

    transformers = list(llm_transformers) if llm_transformers is not None else build_transformers(
        allowed_nodes=allowed_nodes, allowed_relationships=allowed_relationships)
    return process_and_store(
        [d for d, _ in wrapped],
        llm_transformers=transformers,
        ingest_items=[it for _, it in wrapped],
        retry_cfg=get_graph_retry_config(),
        allowed_nodes=allowed_nodes,
        allowed_relationships=allowed_relationships,
        result=result,
        dsn=dsn,
        batch_size=batch_size,
    )


_AUDIO_DOCUMENTS = (
    "d.type IN ['transcript_segment', 'series_overview', 'series_motivation', 'key_takeaway'] "
    "OR d.episode_id IS NOT NULL"
)


def delete_audio_from_graph(dsn: str | None = None) -> dict[str, int]:
    """Delete audio-derived Document vertices (and their MENTIONS edges) and their
    `ingested_docs` rows, so the next run re-extracts audio. Entities stay."""
    from src.database import pg_store

    with pg_store.session(dsn) as conn:
        writer = AgeGraphWriter(conn)
        writer.lock()
        deleted = writer.delete_documents(_AUDIO_DOCUMENTS)
        rows = conn.execute("DELETE FROM ingested_docs WHERE source_key LIKE 'audio:%%'").rowcount
    logger.info("Deleted %s audio Document vertices and %s ingested_docs rows.", deleted, rows)
    return {"documents": deleted, "ingested_docs": max(rows, 0)}


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m src.pipeline.index_graph",
                                description="Extract a knowledge graph into the Postgres AGE graph (billed LLM work).")
    p.add_argument("--schema-profile", choices=["core", "extended"], default=None)
    p.add_argument("--no-audio", action="store_true", help="Exclude audio transcript segments.")
    p.add_argument("--no-articles", action="store_true", help="Exclude Substack articles.")
    p.add_argument("--audio-limit", type=int, default=None, help="Read at most this many audio files' segments.")
    p.add_argument("--article-limit", type=int, default=None, help="Read at most this many articles.")
    p.add_argument("--max-docs", type=int, default=None, help="Send at most this many new passages to the LLM.")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = update_knowledge_graph(
        include_audio=not args.no_audio,
        include_articles=not args.no_articles,
        audio_limit=args.audio_limit,
        article_limit=args.article_limit,
        schema_profile=args.schema_profile,
        max_docs=args.max_docs,
    )
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
