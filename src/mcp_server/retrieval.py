"""Thin search/fetch wrappers over the knowledge store the server is configured for.

`KB_BACKEND=chroma` (default) uses the hydrated Chroma snapshot (query_segments /
query_summaries). `KB_BACKEND=postgres` reads Postgres live through `PG_DSN`
(src/database/pg_search.py), with `KB_SEARCH_MODE` = vector (default, same ranking as
Chroma), keyword or hybrid for the `search` tool.

Used in-process by evals and by the public FastMCP tools. Callers never send query text
to a database: graph access is a fixed set of templates.
"""

from __future__ import annotations

import os
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

BACKENDS = ("chroma", "postgres")
_STORE = None


def backend() -> str:
    value = (os.getenv("KB_BACKEND") or "chroma").strip().lower()
    if value not in BACKENDS:
        raise ValueError(f"KB_BACKEND must be one of {BACKENDS}, got {value!r}")
    return value


def search_mode() -> str:
    from src.database.pg_search import SEARCH_MODES

    value = (os.getenv("KB_SEARCH_MODE") or "vector").strip().lower()
    if value not in SEARCH_MODES:
        raise ValueError(f"KB_SEARCH_MODE must be one of {SEARCH_MODES}, got {value!r}")
    return value


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
    mode: Optional[str] = None,
    kb_backend: Optional[str] = None,
) -> Tuple[List[Document], List[Document]]:
    """Bounded retrieval used by MCP tools and evals (full page_content).

    `mode` (vector / keyword / hybrid) applies to the postgres backend; Chroma is vector only.
    `filter_metadata` applies to the chroma backend; postgres fetches by id in `fetch`.
    `kb_backend` overrides KB_BACKEND for this call (evals compare backends with it).
    """
    q = bound_query(query)
    k_seg = clamp_k(max_segments, cap=max_segments_cap(), default=DEFAULT_MAX_SEGMENTS)
    k_sum = clamp_k(max_summaries, cap=max_summaries_cap(), default=DEFAULT_MAX_SUMMARIES)
    if kb_backend is not None and kb_backend not in BACKENDS:
        raise ValueError(f"kb_backend must be one of {BACKENDS}, got {kb_backend!r}")
    if (kb_backend or backend()) == "postgres":
        if filter_metadata:
            raise ValueError("filter_metadata is a chroma-backend option; use fetch for ids")
        from src.database import pg_search

        return pg_search.retrieve(q, mode=mode or search_mode(), max_segments=k_seg, max_summaries=k_sum)
    if mode not in (None, "vector"):
        raise ValueError(f"search mode {mode!r} needs KB_BACKEND=postgres")
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
    mode: Optional[str] = None,
) -> SearchResults:
    """Search the education corpus (semantic by default). Returns attributed snippets."""
    summaries, segments = retrieve_documents(
        query,
        max_segments=max_segments,
        max_summaries=max_summaries,
        mode=mode,
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
    if backend() == "postgres":
        from src.database import pg_search

        summaries, segments = pg_search.fetch_documents(
            source_id,
            max_segments=clamp_k(max_segments, cap=max_segments_cap(), default=DEFAULT_MAX_SEGMENTS),
            max_summaries=clamp_k(max_summaries, cap=max_summaries_cap(), default=DEFAULT_MAX_SUMMARIES),
        )
        return documents_to_search_results(summaries, segments, snippet_limit=snippet_limit)
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
