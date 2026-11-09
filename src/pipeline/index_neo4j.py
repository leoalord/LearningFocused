"""Cross-pipeline Neo4j indexing orchestrator (graph extraction + write).

This module owns:
- schema constraints (allowed nodes/relationships)
- graph extraction prompt construction
- LLM graph transformer invocation
- schema filtering with drop accounting (`src/pipeline/graph_quality.py`)
- writing GraphDocuments to Neo4j, with per-edge provenance

Every extracted relationship is stamped (on create) with `source_doc_id` (the
`Document.id` it came from), `extract_model` and `ingest_run_id`, so audits can
check an edge against the exact passage that produced it. Each `IngestedDoc`
record stores raw vs kept node/relationship counts and the off-vocabulary types
the model proposed.

Pipeline-specific modules own *what to index* (how to construct Documents from artifacts):
- `src/pipeline/audio/index_neo4j.py`
- `src/pipeline/substack/index_neo4j.py`

`src/database/neo4j_manager.py` should remain a thin DB adapter (connect/query/schema)
for agent tools and lightweight programmatic use.
"""

from __future__ import annotations

import logging
import os
import hashlib
from hashlib import md5
from datetime import datetime, timezone
import time
import uuid
from typing import Any, Dict, List, Optional, cast, TypedDict

from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_experimental.graph_transformers import LLMGraphTransformer
from langchain_neo4j import Neo4jGraph
from langchain_neo4j.graphs.graph_document import (
    GraphDocument as N4jGraphDocument,
    Node as N4jNode,
    Relationship as N4jRelationship,
)

# LLM selection (primary + fallbacks) for graph extraction
from src.pipeline.neo4j_llm_config import get_graph_llm_models, get_graph_retry_config
from src.llm.factory import retryable_invoke, RetryConfig
from src.pipeline.graph_quality import ExtractionStats, filter_to_schema

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
    """Build a stable ingest key for deduping LLM+Neo4j work."""
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


def _ensure_ingest_registry_exists(*, graph: Neo4jGraph) -> None:
    """Create a single placeholder node so Neo4j stops warning about unknown label/properties."""
    cypher = """
    MERGE (d:IngestedDoc {source_key: "__init__", content_hash: "__init__"})
    SET d.status = "completed",
        d.first_ingested_at = coalesce(d.first_ingested_at, toString(datetime())),
        d.last_ingested_at = toString(datetime())
    """
    graph.query(cypher)


def _filter_already_ingested(*, graph: Neo4jGraph, docs: list[Document], items: list[_IngestItem]) -> tuple[list[Document], list[_IngestItem], int]:
    """Filter out docs that have already been ingested (same source_key + content_hash)."""
    if not docs:
        return [], [], 0

    cypher = """
    UNWIND $items AS item
    MATCH (d:IngestedDoc {source_key: item.source_key, content_hash: item.content_hash, status: "completed"})
    RETURN item.source_key AS source_key, item.content_hash AS content_hash
    """
    existing_rows = graph.query(cypher, params=cast(Dict[Any, Any], {"items": items}))
    existing = {(r.get("source_key"), r.get("content_hash")) for r in existing_rows}

    kept_docs: list[Document] = []
    kept_items: list[_IngestItem] = []
    skipped = 0
    for d, it in zip(docs, items):
        if (it.get("source_key"), it.get("content_hash")) in existing:
            skipped += 1
            continue
        kept_docs.append(d)
        kept_items.append(it)
    return kept_docs, kept_items, skipped


def _mark_ingest_status(*, graph: Neo4jGraph, items: list[_IngestItem], status: str) -> None:
    """Upsert ingest registry records for docs with a status (started/completed)."""
    if not items:
        return

    cypher = """
    UNWIND $items AS item
    MERGE (d:IngestedDoc {source_key: item.source_key, content_hash: item.content_hash})
    SET d.source_type = item.source_type,
        d.doc_id = item.doc_id,
        d.canonical_url = item.canonical_url,
        d.title = item.title,
        d.status = $status,
        d.last_ingested_at = item.ingested_at,
        d.last_ingest_run_id = item.ingest_run_id,
        d.first_ingested_at = coalesce(d.first_ingested_at, item.ingested_at)
    """
    graph.query(cypher, params=cast(Dict[Any, Any], {"items": items, "status": status}))


def _record_extraction_stats(*, graph: Neo4jGraph, rows: list[dict[str, Any]]) -> None:
    """Store per-document extraction counts on the IngestedDoc registry record."""
    if not rows:
        return
    cypher = """
    UNWIND $rows AS row
    MATCH (d:IngestedDoc {source_key: row.source_key, content_hash: row.content_hash})
    SET d.extract_model = row.extract_model,
        d.raw_nodes = row.raw_nodes,
        d.kept_nodes = row.kept_nodes,
        d.raw_rels = row.raw_rels,
        d.kept_rels = row.kept_rels,
        d.rels_dropped_for_endpoint = row.rels_dropped_for_endpoint,
        d.dropped_node_types = row.dropped_node_types,
        d.dropped_rel_types = row.dropped_rel_types
    """
    graph.query(cypher, params=cast(Dict[Any, Any], {"rows": rows}))


def _document_id(source: Document) -> str:
    """Same id `Neo4jGraph.add_graph_documents` assigns to the Document node."""
    existing = source.metadata.get("id")
    if existing:
        return str(existing)
    doc_id = md5(source.page_content.encode("utf-8")).hexdigest()
    source.metadata["id"] = doc_id
    return doc_id


def apply_schema_and_provenance(
    graph_documents: List[N4jGraphDocument],
    *,
    allowed_nodes: list[str],
    allowed_relationships: list[str],
    extract_model: str,
    ingest_run_id: str | None,
) -> tuple[List[N4jGraphDocument], list[ExtractionStats]]:
    """Filter each graph document to the schema and stamp edge provenance.

    Returns the filtered documents and one stats object per input document.
    """
    out: list[N4jGraphDocument] = []
    per_doc: list[ExtractionStats] = []
    for gd in graph_documents:
        nodes, rels, stats = filter_to_schema(
            gd.nodes,
            gd.relationships,
            allowed_nodes=allowed_nodes,
            allowed_relationships=allowed_relationships,
        )
        source_doc_id = _document_id(gd.source) if gd.source is not None else None
        for rel in rels:
            props = dict(rel.properties or {})
            if source_doc_id:
                props["source_doc_id"] = source_doc_id
            props["extract_model"] = extract_model
            if ingest_run_id:
                props["ingest_run_id"] = ingest_run_id
            rel.properties = props
        out.append(N4jGraphDocument(nodes=nodes, relationships=rels, source=gd.source))
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


def get_graph_transformer(*, allowed_nodes: list[str], allowed_relationships: list[str]) -> LLMGraphTransformer:
    """Backwards-compatible single-transformer builder (kept for external callers).

    Internally, this pipeline uses multiple models with fallback for resilience.
    """
    llm_models = get_graph_llm_models(temperature=0.0)
    return LLMGraphTransformer(
        llm=cast(Any, llm_models[0]),
        allowed_nodes=allowed_nodes,
        allowed_relationships=allowed_relationships,
    )


def convert_to_neo4j_graph_documents(docs: List[Any]) -> List[N4jGraphDocument]:
    """Convert transformer output into `langchain_neo4j` GraphDocuments."""
    neo4j_docs: list[N4jGraphDocument] = []
    for doc in docs:
        if isinstance(doc, N4jGraphDocument):
            neo4j_docs.append(doc)
            continue

        nodes = [N4jNode(id=node.id, type=node.type, properties=node.properties) for node in doc.nodes]
        relationships = [
            N4jRelationship(
                source=N4jNode(id=rel.source.id, type=rel.source.type),
                target=N4jNode(id=rel.target.id, type=rel.target.type),
                type=rel.type,
                properties=rel.properties,
            )
            for rel in doc.relationships
        ]
        neo4j_docs.append(N4jGraphDocument(nodes=nodes, relationships=relationships, source=doc.source))
    return neo4j_docs


@traceable(name="neo4j_llm_graph_transform")
def _trace_llm_transform(llm_transformer: LLMGraphTransformer, batch: List[Document]) -> List[Any]:
    """LLM graph transformation for a batch (LangSmith-traced)."""
    return llm_transformer.convert_to_graph_documents(batch)


class _TransformerRunnable:
    """Adapter so we can reuse `retryable_invoke` around transformer conversion."""

    def __init__(self, transformer: LLMGraphTransformer):
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


def process_and_store(
    documents: List[Document],
    *,
    graph: Neo4jGraph,
    llm_transformers: List[LLMGraphTransformer],
    ingest_items: list[_IngestItem],
    retry_cfg: RetryConfig,
    allowed_nodes: list[str],
    allowed_relationships: list[str],
) -> ExtractionStats:
    """Process documents through LLMGraphTransformer and store in Neo4j.

    Transformers must be built with `strict_mode=False`; schema filtering happens
    here so drops are counted. Returns totals for the run.
    """
    run_stats = ExtractionStats()
    total_docs = len(documents)
    logger.info("Processing %s documents...", total_docs)

    batch_size = 5
    total_batches = (total_docs + batch_size - 1) // batch_size
    for i in range(0, total_docs, batch_size):
        batch = documents[i : i + batch_size]
        batch_items = ingest_items[i : i + batch_size]
        batch_num = i // batch_size + 1
        logger.info("Processing batch %s/%s", batch_num, total_batches)

        try:
            # Mark "started" BEFORE the LLM call so Ctrl-C doesn't cause repeated reprocessing.
            _mark_ingest_status(graph=graph, items=batch_items, status="started")

            t0 = time.perf_counter()
            # Try transformers in order (primary + fallbacks). Retry each transformer on transient failures.
            graph_documents_raw: List[Any] | None = None
            last_err: Exception | None = None
            used_model = "unknown-model"
            for transformer in llm_transformers:
                llm = getattr(transformer, "llm", None)
                model_name = _model_name_for_llm(llm)
                try:
                    graph_documents_raw = retryable_invoke(_TransformerRunnable(transformer), batch, retry=retry_cfg)
                    used_model = model_name
                    logger.info("Batch %s graph transform model=%s", batch_num, model_name)
                    break
                except Exception as e:
                    last_err = e
                    logger.warning(
                        "Batch %s graph transform failed model=%s error=%s",
                        batch_num,
                        model_name,
                        str(e),
                    )
                    continue
            if graph_documents_raw is None:
                # Mark as failed so future runs can retry (we only dedupe on status='completed').
                _mark_ingest_status(graph=graph, items=batch_items, status="failed")
                raise RuntimeError(f"All graph extraction models failed; last_error={last_err}")
            t1 = time.perf_counter()
            logger.info("Batch %s LLM transform time: %.2fs", batch_num, t1 - t0)

            graph_documents = convert_to_neo4j_graph_documents(graph_documents_raw)
            run_id = cast(Optional[str], batch[0].metadata.get("ingest_run_id")) if batch else None
            graph_documents, doc_stats = apply_schema_and_provenance(
                graph_documents,
                allowed_nodes=allowed_nodes,
                allowed_relationships=allowed_relationships,
                extract_model=used_model,
                ingest_run_id=run_id,
            )
            batch_stats = ExtractionStats()
            for st in doc_stats:
                batch_stats.add(st)
            run_stats.add(batch_stats)
            logger.info("Batch %s schema filter: %s", batch_num, batch_stats.summary())
            t2 = time.perf_counter()
            if graph_documents:
                graph.add_graph_documents(graph_documents, baseEntityLabel=True, include_source=True)
            t3 = time.perf_counter()
            logger.info("Batch %s Neo4j write time: %.2fs", batch_num, t3 - t2)
            logger.info("Added %s graph documents to Neo4j", len(graph_documents))

            # Mark batch as completed even if it produced no graph docs; the LLM work was done.
            _mark_ingest_status(graph=graph, items=batch_items, status="completed")
            # The transformer returns one graph document per input, in order.
            if len(doc_stats) == len(batch_items):
                _record_extraction_stats(
                    graph=graph,
                    rows=[
                        {
                            "source_key": it.get("source_key"),
                            "content_hash": it.get("content_hash"),
                            "extract_model": used_model,
                            **st.to_record(),
                        }
                        for it, st in zip(batch_items, doc_stats)
                    ],
                )
            else:
                logger.warning(
                    "Batch %s returned %s graph docs for %s inputs; per-doc stats not recorded",
                    batch_num,
                    len(doc_stats),
                    len(batch_items),
                )
        except Exception as e:
            logger.error("Error processing batch starting at index %s: %s", i, e)

    logger.info("Run schema filter totals: %s", run_stats.summary(top=10))
    return run_stats


def update_knowledge_graph(
    *,
    include_audio: bool = True,
    include_articles: bool = True,
    audio_limit: int | None = None,
    article_limit: int | None = None,
    schema_profile: str | None = None,
) -> None:
    """Build/update the Neo4j knowledge graph from pipeline artifacts."""
    from src.config import SEGMENTED_DIR, SUBSTACK_METADATA_DIR, SUBSTACK_TEXT_DIR, ensure_data_dirs
    from src.database.neo4j_manager import get_graph

    ensure_data_dirs()

    # Pipeline-owned document loaders
    from src.pipeline.audio.index_neo4j import collect_audio_graph_documents
    from src.pipeline.substack.index_neo4j import collect_substack_graph_documents

    docs: list[Document] = []
    if include_audio:
        docs.extend(collect_audio_graph_documents(SEGMENTED_DIR, limit=audio_limit))
    if include_articles and SUBSTACK_TEXT_DIR.exists() and SUBSTACK_METADATA_DIR.exists():
        docs.extend(
            collect_substack_graph_documents(
                SUBSTACK_TEXT_DIR,
                SUBSTACK_METADATA_DIR,
                limit=article_limit,
            )
        )

    ingest_run_id = str(uuid.uuid4())
    graph = get_graph()
    _ensure_ingest_registry_exists(graph=graph)

    ingest_items = [_build_ingest_item(d, ingest_run_id=ingest_run_id) for d in docs]
    docs, ingest_items, skipped = _filter_already_ingested(graph=graph, docs=docs, items=ingest_items)
    if skipped:
        logger.info("Skipping %s documents already ingested (dedupe)", skipped)

    allowed_nodes, allowed_relationships, profile = resolve_allowed_schema(schema_profile)
    graph_spec = build_graph_extraction_spec(
        allowed_nodes=allowed_nodes,
        allowed_relationships=allowed_relationships,
    )
    docs = [_wrap_for_graph_extraction(d, graph_extraction_spec=graph_spec) for d in docs]
    # Preferred path: build a transformer per model so we can fallback safely.
    llm_models = get_graph_llm_models(temperature=0.0)
    # strict_mode=False: we filter in apply_schema_and_provenance so drops are counted.
    llm_transformers = [
        LLMGraphTransformer(
            llm=cast(Any, m),
            allowed_nodes=allowed_nodes,
            allowed_relationships=allowed_relationships,
            strict_mode=False,
        )
        for m in llm_models
    ]
    retry_cfg = get_graph_retry_config()

    logger.info(
        "Neo4j schema_profile=%s allowed_nodes=%d allowed_relationships=%d",
        profile,
        len(allowed_nodes),
        len(allowed_relationships),
    )

    langsmith_project = os.getenv("LANGCHAIN_PROJECT")
    for d in docs:
        d.metadata["ingest_run_id"] = ingest_run_id
        if langsmith_project:
            d.metadata["langsmith_project"] = langsmith_project
    logger.info("Neo4j ingest_run_id=%s langsmith_project=%s", ingest_run_id, langsmith_project)

    if not docs:
        logger.info("No documents to process.")
        return

    process_and_store(
        docs,
        graph=graph,
        llm_transformers=llm_transformers,
        ingest_items=ingest_items,
        retry_cfg=retry_cfg,
        allowed_nodes=allowed_nodes,
        allowed_relationships=allowed_relationships,
    )


def delete_audio_from_neo4j() -> None:
    """Delete audio-derived nodes and ingest records from Neo4j."""
    from src.database.neo4j_manager import get_graph
    graph = get_graph()

    logger.info("=== CLEANUP NEO4J (AUDIO) ===")
    
    # 1. Delete IngestedDoc records
    logger.info("Deleting IngestedDoc records for audio...")
    graph.query("MATCH (n:IngestedDoc) WHERE n.source_key STARTS WITH 'audio:' DETACH DELETE n")

    # 2. Delete Document nodes (sources)
    logger.info("Deleting audio Document nodes...")
    # Audio docs have 'episode_id' or specific types
    cypher = """
    MATCH (d:Document)
    WHERE d.type IN ['transcript_segment', 'series_overview', 'series_motivation', 'key_takeaway']
       OR d.episode_id IS NOT NULL
    DETACH DELETE d
    """
    graph.query(cypher)
    logger.info("Audio cleanup complete.")


