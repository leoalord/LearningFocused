"""In-memory per-IP rate limit for the public MCP HTTP app."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock
from starlette.types import ASGIApp, Receive, Scope, Send

from src.mcp_server.caps import RATE_LIMIT_WINDOW_SECONDS, rate_limit_per_minute

# Paths that must stay reachable for Cloud Run / load-balancer probes.
_EXEMPT_PATHS = frozenset({"/health", "/healthz"})
_MAX_TRACKED_IPS = 10_000


class IPRateLimitMiddleware:
    """ASGI middleware: sliding window of requests per client IP.

    Disabled when MCP_RATE_LIMIT_PER_MINUTE is 0. Uses X-Forwarded-For
    (first hop) when present so Cloud Run sees the caller, not the proxy.
    """

    def __init__(
        self,
        app: ASGIApp,
        max_requests: int | None = None,
        window_seconds: int = RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        self.app = app
        self.max_requests = rate_limit_per_minute() if max_requests is None else max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def _client_ip(self, scope: Scope) -> str:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        forwarded = headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip() or "unknown"
        client = scope.get("client")
        if client and client[0]:
            return str(client[0])
        return "unknown"

    def _allow(self, ip: str) -> bool:
        if self.max_requests <= 0:
            return True
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            if ip not in self._hits and len(self._hits) >= _MAX_TRACKED_IPS:
                oldest = min(self._hits, key=lambda k: self._hits[k][0] if self._hits[k] else now)
                self._hits.pop(oldest, None)
            bucket = self._hits[ip]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.max_requests:
                return False
            bucket.append(now)
            return True

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if path in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        ip = self._client_ip(scope)
        if self._allow(ip):
            await self.app(scope, receive, send)
            return

        retry_after = str(self.window_seconds)
        body = b'{"error":"rate_limited"}'
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"retry-after", retry_after.encode("ascii")),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
