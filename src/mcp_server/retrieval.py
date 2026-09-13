"""Thin search/fetch wrappers around query_segments / query_summaries.

Used in-process by evals and by the public FastMCP tools. Not a Cypher surface.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from langchain_core.documents import Document

from src.database.chroma_manager import get_vector_store, query_segments, query_summaries
from src.deep_research_agent.tools import SearchResults, _doc_to_source_chunk
from src.mcp_server.caps import (
    DEFAULT_MAX_SEGMENTS,
    DEFAULT_MAX_SUMMARIES,
    DEFAULT_SNIPPET_LIMIT,
    bound_id,
    bound_query,
    clamp_k,
    clamp_snippet_limit,
    is_episode_id,
    max_segments_cap,
    max_summaries_cap,
)

_STORE = None


def _read_only_store():
    global _STORE
    if _STORE is None:
        _STORE = get_vector_store(create_if_missing=False)
    return _STORE


def retrieve_documents(
    query: str,
    *,
    max_segments: int = DEFAULT_MAX_SEGMENTS,
    max_summaries: int = DEFAULT_MAX_SUMMARIES,
    filter_metadata: Optional[dict] = None,
) -> Tuple[List[Document], List[Document]]:
    """Bounded retrieval used by MCP tools and evals (full page_content)."""
    q = bound_query(query)
    k_seg = clamp_k(max_segments, cap=max_segments_cap(), default=DEFAULT_MAX_SEGMENTS)
    k_sum = clamp_k(max_summaries, cap=max_summaries_cap(), default=DEFAULT_MAX_SUMMARIES)
    store = _read_only_store()
    summaries = (
        query_summaries(q, k=k_sum, filter_metadata=filter_metadata, vector_store=store)
        if k_sum > 0 and q
        else []
    )
    segments = (
        query_segments(q, k=k_seg, filter_metadata=filter_metadata, vector_store=store)
        if k_seg > 0 and q
        else []
    )
    return summaries, segments


def documents_to_search_results(
    summaries: List[Document],
    segments: List[Document],
    *,
    snippet_limit: int | None = None,
) -> SearchResults:
    limit = clamp_snippet_limit(snippet_limit)
    return SearchResults(
        summaries=[_doc_to_source_chunk(d, snippet_limit=limit) for d in summaries],
        segments=[_doc_to_source_chunk(d, snippet_limit=limit) for d in segments],
    )


def search(
    query: str,
    max_segments: int = DEFAULT_MAX_SEGMENTS,
    max_summaries: int = DEFAULT_MAX_SUMMARIES,
    snippet_limit: int = DEFAULT_SNIPPET_LIMIT,
) -> SearchResults:
    """Semantic search over the education corpus. Returns attributed snippets."""
    summaries, segments = retrieve_documents(
        query,
        max_segments=max_segments,
        max_summaries=max_summaries,
    )
    return documents_to_search_results(summaries, segments, snippet_limit=snippet_limit)


def _filters_for_id(source_id: str) -> list[dict]:
    """Try episode_id then doc_id (and vice versa) without accepting Cypher."""
    filters: list[dict] = []
    if is_episode_id(source_id):
        filters.append({"episode_id": source_id})
        filters.append({"episode_id": source_id.replace(" ", "")})
    filters.append({"doc_id": source_id})
    # De-dupe while preserving order.
    seen: set[tuple[tuple[str, str], ...]] = set()
    unique: list[dict] = []
    for item in filters:
        key = tuple(sorted(item.items()))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def fetch(
    id: str,
    max_segments: int = DEFAULT_MAX_SEGMENTS,
    max_summaries: int = DEFAULT_MAX_SUMMARIES,
    snippet_limit: int = DEFAULT_SNIPPET_LIMIT,
) -> SearchResults:
    """Fetch attributed snippets for a source id (episode_id or doc_id)."""
    source_id = bound_id(id)
    if not source_id:
        return SearchResults()
    for metadata_filter in _filters_for_id(source_id):
        summaries, segments = retrieve_documents(
            source_id,
            max_segments=max_segments,
            max_summaries=max_summaries,
            filter_metadata=metadata_filter,
        )
        if summaries or segments:
            return documents_to_search_results(
                summaries, segments, snippet_limit=snippet_limit
            )
    return SearchResults()
