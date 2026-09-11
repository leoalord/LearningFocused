import json
from typing import Any, Dict, List, Optional

from langchain_core.documents import Document
from langchain.tools import tool
from pydantic import BaseModel, Field

from src.database.chroma_manager import query_segments, query_summaries
from src.database.neo4j_manager import run_cypher_query, get_graph_schema


class SourceChunk(BaseModel):
    """A single retrieved chunk suitable for citation/UI rendering."""

    kind: str
    title: Optional[str] = None
    canonical_url: Optional[str] = None
    doc_id: Optional[str] = None
    episode_id: Optional[str] = None
    topic: Optional[str] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    group_title: Optional[str] = None
    snippet: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SearchResults(BaseModel):
    """Structured search payload for the knowledge base."""

    summaries: List[SourceChunk] = Field(default_factory=list)
    segments: List[SourceChunk] = Field(default_factory=list)


def _safe_float(value: Any) -> Optional[float]:
    """Best-effort float coercion for messy metadata values."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _snippet(text: str, *, limit: int) -> str:
    """Return a UI-friendly snippet (never raises)."""
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit] + " ...[truncated]..."


def _doc_to_source_chunk(doc: Document, *, snippet_limit: int = 900) -> SourceChunk:
    meta = doc.metadata or {}
    kind = str(meta.get("type") or "unknown")
    meta_clean = dict(meta)
    # Avoid duplicating large/low-value fields in the payload.
    meta_clean.pop("chroma_content_hash", None)
    return SourceChunk(
        kind=kind,
        title=meta.get("title") or meta.get("group_title"),
        canonical_url=meta.get("canonical_url"),
        doc_id=meta.get("doc_id"),
        episode_id=meta.get("episode_id"),
        topic=meta.get("topic"),
        start_time=_safe_float(meta.get("start_time")),
        end_time=_safe_float(meta.get("end_time")),
        group_title=meta.get("group_title"),
        snippet=_snippet(doc.page_content, limit=snippet_limit),
        metadata=meta_clean,
    )

@tool
def search_knowledge_base(query: str, max_segments: int = 5, max_summaries: int = 3) -> str:
    """
    Search the educational podcast transcripts and summaries for specific information.
    Use this to find quotes, definitions, or discussions on specific topics.
    
    Args:
        query: The search query string.
        max_segments: Maximum number of transcript segments to return (default: 5).
        max_summaries: Maximum number of episode summaries to return (default: 3).
        
    Returns:
        A formatted string of relevant segments and summaries.
    """
    # Query both collections (serial execution is fast enough for local Chroma)
    segments = query_segments(query, k=max_segments)
    summaries = query_summaries(query, k=max_summaries)
    
    results = []
    
    if summaries:
        results.append("=== High-level Summaries (episodes + articles) ===")
        for doc in summaries:
            doc_type = doc.metadata.get("type", "unknown")
            if str(doc_type).startswith("article_"):
                title = doc.metadata.get("title", "Unknown Article")
                results.append(
                    f"Article: {title}\nType: {doc_type}\nURL: {doc.metadata.get('canonical_url')}\nContent: {doc.page_content}\n"
                )
            else:
                results.append(
                    f"Source: {doc.metadata.get('group_title', 'Unknown')}\nType: {doc_type}\nContent: {doc.page_content}\n"
                )
    
    if segments:
        results.append("\n=== Detailed Segments (transcripts + articles) ===")
        for doc in segments:
            doc_type = doc.metadata.get("type", "unknown")
            if doc_type == "article_text":
                results.append(
                    f"Article: {doc.metadata.get('title', 'Unknown')}\nContent: {doc.page_content}\n"
                )
            else:
                results.append(
                    f"Episode: {doc.metadata.get('title', 'Unknown')}\nTopic: {doc.metadata.get('topic', 'General')}\nContent: {doc.page_content}\n"
                )
            
    if not results:
        return "No relevant information found in the knowledge base."
        
    return "\n".join(results)

@tool
def search_knowledge_base_structured(query: str, max_segments: int = 5, max_summaries: int = 3) -> str:
    """Search the knowledge base and return results as structured JSON for UIs.

    This is the preferred tool when you need reliable citations (URLs, episode IDs,
    timestamps) or want to render sources in a frontend.
    """
    segments = query_segments(query, k=max_segments)
    summaries = query_summaries(query, k=max_summaries)

    payload = SearchResults(
        summaries=[_doc_to_source_chunk(d) for d in summaries],
        segments=[_doc_to_source_chunk(d) for d in segments],
    )
    return json.dumps(payload.model_dump(), ensure_ascii=False)

@tool
def query_knowledge_graph(query: str) -> str:
    """
    Run a Cypher query against the Neo4j knowledge graph.
    Use this to find structural relationships, e.g., "Who criticized Alpha parenting?" or "What concepts are related to Agency?".
    
    IMPORTANT:
    1. Check the schema with inspect_graph_schema ONLY if you do not already have it or are unsure of the structure.
    2. ALWAYS use a LIMIT clause (e.g., LIMIT 20) to prevent overwhelming response sizes.
    """
    try:
        results = run_cypher_query(query)
        if not results:
            return "No results found."
        return str(results)
    except Exception as e:
        return f"Query failed: {e}"

@tool
def inspect_graph_schema() -> str:
    """
    Get the schema of the Neo4j knowledge graph.
    Use this before writing Cypher queries to understand the available Node labels and Relationship types.
    """
    return get_graph_schema()

# Export list of tools
tools = [
    search_knowledge_base,
    search_knowledge_base_structured,
    query_knowledge_graph,
    inspect_graph_schema,
]
