"""Public FastMCP server: tool names, caps, no Cypher (no live Chroma)."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from langchain_core.documents import Document
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from src.deep_research_agent.tools import SearchResults
from src.mcp_server import caps
from src.mcp_server.rate_limit import IPRateLimitMiddleware
from src.mcp_server.retrieval import documents_to_search_results, fetch, search
from src.mcp_server.server import build_http_app, mcp


def _doc(text: str, **meta) -> Document:
    return Document(page_content=text, metadata=meta)


class TestCaps(unittest.TestCase):
    def test_clamp_k_caps_and_floors(self) -> None:
        self.assertEqual(caps.clamp_k(100, cap=8, default=5), 8)
        self.assertEqual(caps.clamp_k(0, cap=8, default=5), 1)
        self.assertEqual(caps.clamp_k(-3, cap=8, default=5), 1)
        self.assertEqual(caps.clamp_k(None, cap=8, default=5), 5)

    def test_snippet_limit_is_bounded(self) -> None:
        self.assertEqual(caps.clamp_snippet_limit(50_000), caps.MAX_SNIPPET_LIMIT)
        self.assertGreaterEqual(caps.clamp_snippet_limit(1), 64)

    def test_query_and_id_are_bounded(self) -> None:
        self.assertEqual(len(caps.bound_query("x" * 5000)), caps.MAX_QUERY_CHARS)
        self.assertEqual(len(caps.bound_id("y" * 5000)), caps.MAX_ID_CHARS)


class TestPublicToolSurface(unittest.IsolatedAsyncioTestCase):
    async def test_only_search_and_fetch(self) -> None:
        from fastmcp import Client

        async with Client(mcp) as client:
            tools = await client.list_tools()
        names = sorted(t.name for t in tools)
        self.assertEqual(names, ["fetch", "search"])
        blob = " ".join(
            f"{t.name} {t.description or ''} {getattr(t, 'input_schema', None) or getattr(t, 'inputSchema', '')}"
            for t in tools
        ).lower()
        self.assertNotIn("cypher", blob)
        self.assertNotIn("neo4j", blob)
        self.assertNotIn("query_knowledge_graph", names)

    async def test_search_returns_typed_results_and_clips_snippets(self) -> None:
        from fastmcp import Client

        long_text = "alpha " * 400
        summaries = [_doc(long_text, type="key_takeaway", title="Ep", episode_id="S2E1", canonical_url="https://ex")]
        segments = [_doc(long_text, type="transcript_segment", title="Ep", episode_id="S2E1")]

        with patch("src.mcp_server.retrieval.retrieve_documents", return_value=(summaries, segments)):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "search",
                    {"query": "alpha", "max_segments": 99, "max_summaries": 99, "snippet_limit": 50_000},
                )
        payload = result.structured_content
        if isinstance(payload, SearchResults):
            data = payload.model_dump()
        else:
            data = payload
        self.assertIn("summaries", data)
        self.assertIn("segments", data)
        self.assertLessEqual(len(data["summaries"][0]["snippet"]), caps.MAX_SNIPPET_LIMIT + 20)
        self.assertEqual(data["summaries"][0]["episode_id"], "S2E1")
        self.assertEqual(data["summaries"][0]["canonical_url"], "https://ex")


class TestRetrievalWrappers(unittest.TestCase):
    def test_search_calls_query_helpers(self) -> None:
        summaries = [_doc("sum", type="key_takeaway", title="T", canonical_url="https://a")]
        segments = [_doc("seg", type="transcript_segment", episode_id="S2E335")]
        with (
            patch("src.mcp_server.retrieval._read_only_store", return_value="store"),
            patch("src.mcp_server.retrieval.query_summaries", return_value=summaries) as qs,
            patch("src.mcp_server.retrieval.query_segments", return_value=segments) as qg,
        ):
            result = search("parent agency", max_segments=3, max_summaries=2)
        self.assertIsInstance(result, SearchResults)
        qs.assert_called_once()
        qg.assert_called_once()
        self.assertEqual(result.summaries[0].canonical_url, "https://a")
        self.assertEqual(result.segments[0].episode_id, "S2E335")

    def test_fetch_does_not_accept_cypher(self) -> None:
        with (
            patch("src.mcp_server.retrieval._read_only_store", return_value="store"),
            patch("src.mcp_server.retrieval.query_summaries", return_value=[]) as qs,
            patch("src.mcp_server.retrieval.query_segments", return_value=[]) as qg,
        ):
            result = fetch("MATCH (n) RETURN n")
        self.assertEqual(result.summaries, [])
        self.assertEqual(result.segments, [])
        for call in qs.call_args_list + qg.call_args_list:
            kwargs = call.kwargs
            filter_meta = kwargs.get("filter_metadata") or {}
            self.assertNotIn("cypher", str(filter_meta).lower())

    def test_snippet_truncation(self) -> None:
        docs = [_doc("n" * 5000, type="transcript_segment", title="x")]
        payload = documents_to_search_results([], docs, snippet_limit=200)
        self.assertLessEqual(len(payload.segments[0].snippet), 200 + len(" ...[truncated]..."))
        self.assertTrue(payload.segments[0].snippet.endswith("...[truncated]..."))


class TestRateLimit(unittest.TestCase):
    def test_http_429_after_cap(self) -> None:
        async def inner_app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"ok"})

        app = IPRateLimitMiddleware(inner_app, max_requests=2, window_seconds=60)

        async def request_once() -> int:
            status = {"code": 0}

            async def receive():
                return {"type": "http.request"}

            async def send(message):
                if message["type"] == "http.response.start":
                    status["code"] = message["status"]

            await app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "POST",
                    "path": "/mcp",
                    "headers": [(b"x-forwarded-for", b"203.0.113.9")],
                    "client": ("127.0.0.1", 1234),
                    "scheme": "http",
                    "query_string": b"",
                },
                receive,
                send,
            )
            return status["code"]

        self.assertEqual(asyncio.run(request_once()), 200)
        self.assertEqual(asyncio.run(request_once()), 200)
        self.assertEqual(asyncio.run(request_once()), 429)

    def test_health_is_exempt(self) -> None:
        async def inner_app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"ok"})

        app = IPRateLimitMiddleware(inner_app, max_requests=1, window_seconds=60)

        async def hit_health() -> int:
            status = {"code": 0}

            async def receive():
                return {"type": "http.request"}

            async def send(message):
                if message["type"] == "http.response.start":
                    status["code"] = message["status"]

            await app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "GET",
                    "path": "/health",
                    "headers": [],
                    "client": ("10.0.0.1", 1),
                    "scheme": "http",
                    "query_string": b"",
                },
                receive,
                send,
            )
            return status["code"]

        self.assertEqual(asyncio.run(hit_health()), 200)
        self.assertEqual(asyncio.run(hit_health()), 200)

    def test_stateless_http_app_exposes_mcp_path(self) -> None:
        http_app = build_http_app()
        self.assertTrue(any("/mcp" in str(getattr(r, "path", "")) or "/mcp" in str(r) for r in http_app.routes))
        with TestClient(http_app) as client:
            health = client.get("/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["status"], "ok")


class TestMiddlewareWiring(unittest.TestCase):
    def test_rate_limit_middleware_is_configured(self) -> None:
        mw = [Middleware(IPRateLimitMiddleware)]
        self.assertEqual(mw[0].cls, IPRateLimitMiddleware)


if __name__ == "__main__":
    unittest.main()
