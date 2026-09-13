"""Hard caps for the public unauthenticated MCP surface (TASK-18)."""

from __future__ import annotations

import os
import re

# Public tools clamp to these. Agent/UI LangChain tools are unchanged.
DEFAULT_MAX_SEGMENTS = 5
DEFAULT_MAX_SUMMARIES = 3
MAX_SEGMENTS = 8
MAX_SUMMARIES = 5
DEFAULT_SNIPPET_LIMIT = 900
MAX_SNIPPET_LIMIT = 900
MAX_QUERY_CHARS = 500
MAX_ID_CHARS = 200
RATE_LIMIT_PER_MINUTE = 60
RATE_LIMIT_WINDOW_SECONDS = 60

_EPISODE_ID = re.compile(r"^S\d*E\d+$", re.IGNORECASE)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def max_segments_cap() -> int:
    return max(1, _env_int("MCP_MAX_SEGMENTS", MAX_SEGMENTS))


def max_summaries_cap() -> int:
    return max(1, _env_int("MCP_MAX_SUMMARIES", MAX_SUMMARIES))


def snippet_limit_cap() -> int:
    return max(64, _env_int("MCP_SNIPPET_LIMIT", MAX_SNIPPET_LIMIT))


def rate_limit_per_minute() -> int:
    return max(0, _env_int("MCP_RATE_LIMIT_PER_MINUTE", RATE_LIMIT_PER_MINUTE))


def clamp_k(value: int | None, *, cap: int, default: int) -> int:
    if value is None:
        return min(default, cap)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return min(default, cap)
    return max(1, min(parsed, cap))


def clamp_snippet_limit(value: int | None) -> int:
    cap = snippet_limit_cap()
    if value is None:
        return min(DEFAULT_SNIPPET_LIMIT, cap)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return min(DEFAULT_SNIPPET_LIMIT, cap)
    return max(64, min(parsed, cap))


def bound_query(query: str) -> str:
    text = (query or "").strip()
    if len(text) <= MAX_QUERY_CHARS:
        return text
    return text[:MAX_QUERY_CHARS]


def bound_id(value: str) -> str:
    text = (value or "").strip()
    if len(text) <= MAX_ID_CHARS:
        return text
    return text[:MAX_ID_CHARS]


def is_episode_id(value: str) -> bool:
    return bool(_EPISODE_ID.fullmatch(value.replace(" ", "")))
