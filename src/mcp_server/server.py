"""FastMCP 4 Streamable HTTP server: public tools `search` and `fetch`.

Product transport is HTTP (`stateless_http=True`), endpoint `/mcp`.
Does not expose Neo4j / Cypher. Agent and UI LangChain tools stay on the CLIs.
"""

from __future__ import annotations

import os

from fastmcp import FastMCP
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
from src.mcp_server.retrieval import fetch as fetch_corpus
from src.mcp_server.retrieval import search as search_corpus

mcp = FastMCP(
    name="learningfocused",
    instructions=(
        "Future of Education knowledge base (podcast, Substack, unique YouTube). "
        "Use search for semantic retrieval and fetch for a known episode_id or doc_id. "
        "Responses are attributed snippets, not full transcripts. No graph/Cypher tools."
    ),
)


@mcp.tool(
    name="search",
    annotations=ToolAnnotations(
        title="Search knowledge base",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
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
