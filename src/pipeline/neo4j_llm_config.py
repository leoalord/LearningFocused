"""Neo4j graph extraction LLM configuration.

This is used by `src/pipeline/index_graph.py` during `LLMGraphTransformer` conversion.

Environment variables:
- LF_NEO4J_GRAPH_MODELS (comma-separated; primary + fallbacks)

Knobs:
- NEO4J_GRAPH_LLM_TIMEOUT_SECONDS (existing; default: 120)
- LF_NEO4J_GRAPH_LLM_MAX_TOKENS (optional)
- LF_NEO4J_GRAPH_LLM_MAX_ATTEMPTS (default: 4)  # retry attempts per model
"""

from __future__ import annotations

import os
from typing import Any

from src.llm.factory import RetryConfig, create_chat_models, parse_model_list


def get_graph_llm_models(*, temperature: float = 0.0) -> list[Any]:
    """Return ordered concrete chat model instances for graph extraction."""
    models = parse_model_list(
        os.getenv("LF_NEO4J_GRAPH_MODELS"),
        default=[
            "gemini-flash-latest",
            "claude-haiku-4-5",
            "gpt-5.1-mini",
            "claude-sonnet-4-5",
            "gpt-5.2",
            "accounts/fireworks/models/deepseek-v3p2",
        ],
    )

    timeout = int(os.getenv("NEO4J_GRAPH_LLM_TIMEOUT_SECONDS", "120"))
    max_tokens_raw = os.getenv("LF_NEO4J_GRAPH_LLM_MAX_TOKENS")
    max_tokens = int(max_tokens_raw) if max_tokens_raw else None

    # `create_chat_models` returns provider-specific chat model objects. We intentionally
    # avoid `.with_fallbacks(...)` here because some experimental transformers expect
    # a concrete chat model instance, not a generic runnable wrapper.
    return create_chat_models(
        models,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )


def get_graph_retry_config() -> RetryConfig:
    """Retry config for transient graph extraction failures."""
    return RetryConfig(
        max_attempts=int(os.getenv("LF_NEO4J_GRAPH_LLM_MAX_ATTEMPTS", "4")),
        initial_backoff_seconds=float(os.getenv("LF_NEO4J_GRAPH_LLM_INITIAL_BACKOFF_SECONDS", "1.0")),
        max_backoff_seconds=float(os.getenv("LF_NEO4J_GRAPH_LLM_MAX_BACKOFF_SECONDS", "20.0")),
        jitter_ratio=float(os.getenv("LF_NEO4J_GRAPH_LLM_JITTER_RATIO", "0.2")),
    )


