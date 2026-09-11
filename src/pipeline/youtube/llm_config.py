"""YouTube pipeline LLM configuration.

Default model: gemini-flash-latest (TASK-5 contract).
"""

from __future__ import annotations

import os
from typing import Any

from src.llm.factory import RetryConfig, build_chat_runnable, parse_model_list

DEFAULT_MODEL = "gemini-flash-latest"


def _timeout_seconds() -> int:
    return int(os.getenv("LF_YOUTUBE_LLM_TIMEOUT_SECONDS", "60"))


def _max_tokens() -> int | None:
    raw = os.getenv("LF_YOUTUBE_LLM_MAX_TOKENS")
    return int(raw) if raw else None


def _retry_cfg() -> RetryConfig:
    return RetryConfig(
        max_attempts=int(os.getenv("LF_YOUTUBE_LLM_MAX_ATTEMPTS", "4")),
        initial_backoff_seconds=float(os.getenv("LF_YOUTUBE_LLM_INITIAL_BACKOFF_SECONDS", "1.0")),
        max_backoff_seconds=float(os.getenv("LF_YOUTUBE_LLM_MAX_BACKOFF_SECONDS", "20.0")),
        jitter_ratio=float(os.getenv("LF_YOUTUBE_LLM_JITTER_RATIO", "0.2")),
    )


def _models(env_var: str, extra_primary: str | None = None) -> list[str]:
    defaults = [
        DEFAULT_MODEL,
        "claude-haiku-4-5",
        "gpt-5.1-mini",
        "claude-sonnet-4-5",
    ]
    env_models = parse_model_list(os.getenv(env_var), default=defaults)
    if extra_primary:
        return [extra_primary] + [m for m in env_models if m != extra_primary]
    return env_models


def get_segmentation_llm(*, model: str | None = None, temperature: float = 0.0) -> Any:
    return build_chat_runnable(
        model_names=_models("LF_YOUTUBE_SEGMENTATION_MODELS", model),
        temperature=temperature,
        max_tokens=_max_tokens(),
        timeout=_timeout_seconds(),
        retry=_retry_cfg(),
    )


def get_summary_llm(*, model: str | None = None, temperature: float = 0.1) -> Any:
    return build_chat_runnable(
        model_names=_models("LF_YOUTUBE_SUMMARY_MODELS", model),
        temperature=temperature,
        max_tokens=_max_tokens(),
        timeout=_timeout_seconds(),
        retry=_retry_cfg(),
    )
