"""FastMCP 4 Streamable HTTP server: public tools `search` and `fetch`.

With `KB_BACKEND=postgres` it adds `keyword_search`, `vector_search`, `hybrid_search`
and `graph_query` (fixed read-only templates over the Postgres graph; callers never send
query text). Product transport is HTTP (`stateless_http=True`), endpoint `/mcp`.
Agent and UI LangChain tools stay on the CLIs.
"""

from __future__ import annotations

import functools
import os
from typing import Any, Callable, Literal, TypeVar

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.deep_research_agent.tools import SearchResults
from src.mcp_server.caps import (
    DEFAULT_MAX_SEGMENTS,
    DEFAULT_MAX_SUMMARIES,
    DEFAULT_SNIPPET_LIMIT,
)
from src.mcp_server.rate_limit import IPRateLimitMiddleware
from src.mcp_server.retrieval import backend, search_mode
from src.mcp_server.retrieval import fetch as fetch_corpus
from src.mcp_server.retrieval import search as search_corpus

_CHROMA_INSTRUCTIONS = (
    "Future of Education knowledge base (podcast, Substack, unique YouTube). "
    "Use search for semantic retrieval and fetch for a known episode_id or doc_id. "
    "Responses are attributed snippets, not full transcripts. No graph/Cypher tools."
)
_SEARCH_STYLE = {"vector": "semantic", "keyword": "keyword", "hybrid": "hybrid (keyword + semantic)"}


def _postgres_instructions() -> str:
    return (
        "Future of Education knowledge base (podcast, Substack, unique YouTube). "
        f"search is {_SEARCH_STYLE[search_mode()]}; " + _POSTGRES_TOOLS
    )


_POSTGRES_TOOLS = (
    "keyword_search matches exact words and phrases (quotes, OR, -word); "
    "vector_search is semantic by meaning; hybrid_search fuses both and is the best default "
    "for names and terms. fetch returns a known episode_id or doc_id. graph_query explores "
    "the entity graph (people, organizations, concepts) with fixed templates: start with "
    "schema or find_entities, then neighbors, connections or mentions. "
    "Responses are attributed snippets, not full transcripts."
)

# mask_error_details: unexpected errors (a database hiccup, say) reach public clients as a
# generic message, never with hosts, roles or SQL. ToolError messages still go through.
mcp = FastMCP(
    name="learningfocused",
    instructions=_postgres_instructions() if backend() == "postgres" else _CHROMA_INSTRUCTIONS,
    mask_error_details=True,
)

F = TypeVar("F", bound=Callable[..., Any])


def _friendly_store_errors(fn: F) -> F:
    """Turn Postgres outages and overload into a short, retryable tool error."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        import psycopg

        from src.database.pg_search import StoreBusy

        try:
            return fn(*args, **kwargs)
        except StoreBusy as exc:
            raise ToolError("The knowledge base is busy; try again in a few seconds.") from exc
        except psycopg.errors.QueryCanceled as exc:
            raise ToolError("That query took too long; try a narrower one.") from exc
        except psycopg.Error as exc:
            raise ToolError("The knowledge base is unavailable right now; try again shortly.") from exc

    return wrapper  # type: ignore[return-value]

_READ_ONLY = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}


@mcp.tool(
    name="search",
    annotations=ToolAnnotations(
        title="Search knowledge base",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
@_friendly_store_errors
def search(
    query: str,
    max_segments: int = DEFAULT_MAX_SEGMENTS,
    max_summaries: int = DEFAULT_MAX_SUMMARIES,
    snippet_limit: int = DEFAULT_SNIPPET_LIMIT,
) -> SearchResults:
    """Search podcast transcripts, Substack articles, and unique YouTube videos.

    Args:
        query: Natural-language search query.
        max_segments: Max transcript/article segments (clamped).
        max_summaries: Max summaries (clamped).
        snippet_limit: Max characters per snippet (clamped).
    """
    return search_corpus(
        query,
        max_segments=max_segments,
        max_summaries=max_summaries,
        snippet_limit=snippet_limit,
    )


@mcp.tool(
    name="fetch",
    annotations=ToolAnnotations(
        title="Fetch source by id",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
@_friendly_store_errors
def fetch(
    id: str,
    max_segments: int = DEFAULT_MAX_SEGMENTS,
    max_summaries: int = DEFAULT_MAX_SUMMARIES,
    snippet_limit: int = DEFAULT_SNIPPET_LIMIT,
) -> SearchResults:
    """Fetch attributed snippets for an episode_id (e.g. S2E335) or doc_id.

    Args:
        id: Episode id or document id from a prior search hit.
        max_segments: Max transcript/article segments (clamped).
        max_summaries: Max summaries (clamped).
        snippet_limit: Max characters per snippet (clamped).
    """
    return fetch_corpus(
        id,
        max_segments=max_segments,
        max_summaries=max_summaries,
        snippet_limit=snippet_limit,
    )


def register_postgres_tools(server: FastMCP) -> None:
    """The search styles only Postgres has: keyword, explicit vector, hybrid, graph."""
    from src.database.pg_search import MAX_GRAPH_HOPS
    from src.mcp_server.caps import bound_id, bound_query, clamp_snippet_limit

    def _mode_search(mode: str, query: str, max_segments: int, max_summaries: int,
                     snippet_limit: int) -> SearchResults:
        return search_corpus(query, max_segments=max_segments, max_summaries=max_summaries,
                             snippet_limit=snippet_limit, mode=mode)

    @server.tool(name="keyword_search", annotations=ToolAnnotations(title="Keyword search", **_READ_ONLY))
    @_friendly_store_errors
    def keyword_search(
        query: str,
        max_segments: int = DEFAULT_MAX_SEGMENTS,
        max_summaries: int = DEFAULT_MAX_SUMMARIES,
        snippet_limit: int = DEFAULT_SNIPPET_LIMIT,
    ) -> SearchResults:
        """Full-text search for exact words, names and phrases.

        Web-search syntax: "quoted phrase", OR, -excluded. Best ranked by term density.

        Args:
            query: Words or phrases to match.
            max_segments: Max transcript/article segments (clamped).
            max_summaries: Max summaries (clamped).
            snippet_limit: Max characters per snippet (clamped).
        """
        return _mode_search("keyword", query, max_segments, max_summaries, snippet_limit)

    @server.tool(name="vector_search", annotations=ToolAnnotations(title="Semantic search", **_READ_ONLY))
    @_friendly_store_errors
    def vector_search(
        query: str,
        max_segments: int = DEFAULT_MAX_SEGMENTS,
        max_summaries: int = DEFAULT_MAX_SUMMARIES,
        snippet_limit: int = DEFAULT_SNIPPET_LIMIT,
    ) -> SearchResults:
        """Semantic search by meaning (embedding similarity), even without shared words.

        Args:
            query: Natural-language question or description.
            max_segments: Max transcript/article segments (clamped).
            max_summaries: Max summaries (clamped).
            snippet_limit: Max characters per snippet (clamped).
        """
        return _mode_search("vector", query, max_segments, max_summaries, snippet_limit)

    @server.tool(name="hybrid_search", annotations=ToolAnnotations(title="Hybrid search", **_READ_ONLY))
    @_friendly_store_errors
    def hybrid_search(
        query: str,
        max_segments: int = DEFAULT_MAX_SEGMENTS,
        max_summaries: int = DEFAULT_MAX_SUMMARIES,
        snippet_limit: int = DEFAULT_SNIPPET_LIMIT,
    ) -> SearchResults:
        """Keyword and semantic results fused by reciprocal rank (good default for names + topics).

        Args:
            query: Natural-language query; names and exact terms help.
            max_segments: Max transcript/article segments (clamped).
            max_summaries: Max summaries (clamped).
            snippet_limit: Max characters per snippet (clamped).
        """
        return _mode_search("hybrid", query, max_segments, max_summaries, snippet_limit)

    @server.tool(name="graph_query", annotations=ToolAnnotations(title="Explore the entity graph", **_READ_ONLY))
    @_friendly_store_errors
    def graph_query(
        template: Literal["schema", "find_entities", "neighbors", "connections", "mentions"],
        entity: str = "",
        other_entity: str = "",
        label: str = "",
        max_hops: int = 2,
        limit: int = 10,
        snippet_limit: int = DEFAULT_SNIPPET_LIMIT,
    ) -> dict[str, Any]:
        """Explore people, organizations, concepts, tools and events extracted from the corpus.

        Templates (entity names match case-insensitively):
        - schema: node labels and relationship types with counts
        - find_entities: entities whose name contains `entity` (optional node `label`)
        - neighbors: everything one relationship away from `entity`
        - connections: paths of up to `max_hops` (1-3) between `entity` and `other_entity`
        - mentions: source passages that mention `entity`

        Args:
            template: Which query to run.
            entity: Entity name (or part of one, for find_entities).
            other_entity: Second entity, for connections.
            label: Node label filter for find_entities (see schema).
            max_hops: Path length for connections (1-3).
            limit: Max rows (1-50).
            snippet_limit: Max characters per text property (clamped).
        """
        from src.database import pg_search

        try:
            return pg_search.graph_query(
                template,
                entity=bound_query(entity),
                other_entity=bound_query(other_entity),
                label=bound_id(label),
                max_hops=max(1, min(max_hops, MAX_GRAPH_HOPS)),
                limit=limit,
                snippet_limit=clamp_snippet_limit(snippet_limit),
            )
        except ValueError as exc:
            return {"template": template, "error": str(exc), "rows": []}


if backend() == "postgres":
    register_postgres_tools(mcp)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "learningfocused-mcp"})


def http_middleware() -> list[Middleware]:
    return [Middleware(IPRateLimitMiddleware)]


def build_http_app():
    return mcp.http_app(
        path="/mcp",
        stateless_http=True,
        middleware=http_middleware(),
    )


def run() -> None:
    host = os.getenv("MCP_HOST", "127.0.0.1")
    port = int(os.getenv("PORT") or os.getenv("MCP_PORT") or "8000")
    mcp.run(
        transport="http",
        host=host,
        port=port,
        path="/mcp",
        stateless_http=True,
        middleware=http_middleware(),
    )
