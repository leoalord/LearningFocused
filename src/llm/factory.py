"""LLM factory utilities (multi-provider + fallbacks + retries).

This module is intentionally lightweight and dependency-minimal. It centralizes:
- mapping model names -> provider + required API key env var
- instantiating provider-specific LangChain chat models
- parsing ordered model lists (primary + fallbacks)
- retry/backoff for transient failures (rate limits, timeouts, 5xx)

Design goals:
- Keep pipelines readable: they should declare *which* models to try, and call the factory.
- Avoid hard-coding any single provider inside pipeline modules.
- Prefer env var configuration so large batch runs can switch providers without code edits.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, MutableMapping, Optional, Sequence, TypeAlias

from langchain_anthropic import ChatAnthropic
from langchain_fireworks import ChatFireworks
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI

ModelRegistry: TypeAlias = Mapping[str, Mapping[str, str]]


DEFAULT_MODEL_REGISTRY: dict[str, dict[str, str]] = {
    # OpenAI
    "gpt-5.2": {"provider": "openai", "env_var": "OPENAI_API_KEY"},
    "gpt-5.1": {"provider": "openai", "env_var": "OPENAI_API_KEY"},
    "gpt-5.1-mini": {"provider": "openai", "env_var": "OPENAI_API_KEY"},
    # Google Gemini
    "gemini-3-pro-preview": {"provider": "google_genai", "env_var": "GOOGLE_API_KEY"},
    "gemini-3-flash-preview": {"provider": "google_genai", "env_var": "GOOGLE_API_KEY"},
    # Use Google-managed aliases when you want Google to control the "latest" pointer.
    # These should be treated as first-class model IDs (do NOT alias them to previews).
    "gemini-flash-latest": {"provider": "google_genai", "env_var": "GOOGLE_API_KEY"},
    # Anthropic
    # Prefer stable API aliases (always latest) + keep versioned IDs for compatibility.
    "claude-sonnet-4-5": {"provider": "anthropic", "env_var": "ANTHROPIC_API_KEY"},
    "claude-haiku-4-5": {"provider": "anthropic", "env_var": "ANTHROPIC_API_KEY"},
    "claude-opus-4-5": {"provider": "anthropic", "env_var": "ANTHROPIC_API_KEY"},
    "claude-sonnet-4-5-20250929": {"provider": "anthropic", "env_var": "ANTHROPIC_API_KEY"},
    "claude-haiku-4-5-20251001": {"provider": "anthropic", "env_var": "ANTHROPIC_API_KEY"},
    "claude-opus-4-5-20251101": {"provider": "anthropic", "env_var": "ANTHROPIC_API_KEY"},
    # Fireworks (from planning/current_state_of_models.json)
    "accounts/fireworks/models/deepseek-v3p2": {"provider": "fireworks", "env_var": "FIREWORKS_API_KEY"},
    "accounts/fireworks/models/kimi-k2-thinking": {"provider": "fireworks", "env_var": "FIREWORKS_API_KEY"},
    "accounts/fireworks/models/gpt-oss-120b": {"provider": "fireworks", "env_var": "FIREWORKS_API_KEY"},
}

# Backwards-compatible aliases for older/legacy identifiers used in the repo.
# These let existing env vars/configs continue working while the canonical IDs
# move to those in `planning/current_state_of_models.json`.
MODEL_ALIASES: dict[str, str] = {
    # OpenAI legacy
    "gpt-5": "gpt-5.2",
    "gpt-5-mini": "gpt-5.1-mini",
    # Google legacy
    "gemini-2.5-pro": "gemini-3-pro-preview",
    # Anthropic legacy -> prefer stable aliases
    "claude-sonnet-4-20250514": "claude-sonnet-4-5",
    # If someone passes the versioned ID, allow normalizing back to the alias.
    "claude-sonnet-4-5-20250929": "claude-sonnet-4-5",
    "claude-haiku-4-5-20251001": "claude-haiku-4-5",
    "claude-opus-4-5-20251101": "claude-opus-4-5",
}


@dataclass(frozen=True)
class RetryConfig:
    """Retry configuration for transient model call failures."""

    max_attempts: int = 4
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 20.0
    jitter_ratio: float = 0.2


def parse_model_list(raw: str | None, *, default: Sequence[str]) -> list[str]:
    """Parse a comma-separated model list into a de-duplicated ordered list."""
    if not raw:
        models = list(default)
    else:
        parts = [p.strip() for p in raw.split(",")]
        parts = [p for p in parts if p]
        models = parts or list(default)

    seen: set[str] = set()
    out: list[str] = []
    for m in models:
        if m in seen:
            continue
        seen.add(m)
        out.append(m)
    return out


def _infer_provider(model_name: str) -> str | None:
    name = (model_name or "").lower()
    if "gemini" in name:
        return "google_genai"
    if "claude" in name:
        return "anthropic"
    if "fireworks" in name or name.startswith("accounts/fireworks/models/"):
        return "fireworks"
    if name.startswith(("gpt", "o1")):
        return "openai"
    return None


def _resolve_registry_entry(
    model_name: str,
    *,
    registry: ModelRegistry | None = None,
) -> Mapping[str, str]:
    reg = registry or DEFAULT_MODEL_REGISTRY
    if model_name in reg:
        return reg[model_name]
    aliased = MODEL_ALIASES.get(model_name)
    if aliased and aliased in reg:
        return reg[aliased]
    return {}


def _require_api_key(env_var: str, model_name: str) -> str:
    value = os.getenv(env_var)
    if not value:
        raise ValueError(f"Missing API key for model '{model_name}'. Set {env_var}.")
    return value


def create_chat_model(
    *,
    model_name: str,
    registry: ModelRegistry | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: int | None = None,
    extra_kwargs: Optional[MutableMapping[str, Any]] = None,
) -> Any:
    """Create a provider-specific LangChain chat model instance.

    Returns:
        A LangChain chat model (provider-specific) that supports `.invoke()` / `.ainvoke()`.
    """
    # Normalize legacy names to canonical IDs when possible.
    canonical_name = MODEL_ALIASES.get(model_name, model_name)
    entry = _resolve_registry_entry(canonical_name, registry=registry)
    provider = entry.get("provider") or _infer_provider(model_name)
    if provider is None:
        raise ValueError(
            f"Unknown provider for model '{model_name}'. "
            "Supported providers: openai, anthropic, google_genai, fireworks"
        )

    env_var = entry.get("env_var")
    api_key = _require_api_key(env_var, canonical_name) if env_var else None

    kwargs: dict[str, Any] = dict(extra_kwargs or {})

    if provider == "openai":
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if timeout is not None:
            kwargs["timeout"] = timeout
        return ChatOpenAI(model=canonical_name, **kwargs)  # type: ignore[call-arg]

    if provider == "google_genai":
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_output_tokens"] = max_tokens
        if timeout is not None:
            kwargs["timeout"] = timeout
        if api_key is not None:
            kwargs["google_api_key"] = api_key
        return ChatGoogleGenerativeAI(model=canonical_name, **kwargs)

    if provider == "anthropic":
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if timeout is not None:
            kwargs["timeout"] = timeout
        return ChatAnthropic(model=canonical_name, **kwargs)  # type: ignore[call-arg]

    if provider == "fireworks":
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if timeout is not None:
            kwargs["timeout"] = timeout
        return ChatFireworks(model=canonical_name, **kwargs)

    raise ValueError(f"Unsupported provider '{provider}' for model '{model_name}'.")


def create_chat_models(
    model_names: Sequence[str],
    *,
    registry: ModelRegistry | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: int | None = None,
    extra_kwargs: Optional[MutableMapping[str, Any]] = None,
) -> list[Any]:
    """Create multiple chat models (ordered) for primary + fallbacks."""
    if not model_names:
        raise ValueError("model_names must be non-empty.")
    return [
        create_chat_model(
            model_name=m,
            registry=registry,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            extra_kwargs=extra_kwargs,
        )
        for m in model_names
    ]


def _is_retryable_exception(e: Exception) -> bool:
    """Best-effort retryability detection (provider-agnostic)."""
    msg = str(e).lower()
    # Common transient patterns
    transient_markers = [
        "rate limit",
        "too many requests",
        "429",
        "deadlineexceeded",
        "deadline exceeded",
        "504",
        "502",
        "503",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "service unavailable",
        "connection reset",
        "connection aborted",
        "read error",
    ]
    return any(m in msg for m in transient_markers)


def _compute_backoff(attempt: int, cfg: RetryConfig) -> float:
    base = cfg.initial_backoff_seconds * (2 ** max(0, attempt - 1))
    base = min(base, cfg.max_backoff_seconds)
    jitter = base * cfg.jitter_ratio * (random.random() * 2 - 1)  # +/- jitter_ratio
    return max(0.0, base + jitter)


def retryable_invoke(
    runnable: Any,
    input: Any,
    *,
    retry: RetryConfig | None = None,
) -> Any:
    """Invoke a runnable with retry/backoff on transient failures."""
    cfg = retry or RetryConfig()
    last_exc: Exception | None = None
    for attempt in range(1, cfg.max_attempts + 1):
        try:
            return runnable.invoke(input)
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt >= cfg.max_attempts or not _is_retryable_exception(e):
                raise
            time.sleep(_compute_backoff(attempt, cfg))
    if last_exc:
        raise last_exc
    raise RuntimeError("retryable_invoke failed without exception (unexpected).")


async def _retryable_ainvoke(
    runnable: Any,
    input: Any,
    *,
    retry: RetryConfig | None = None,
) -> Any:
    cfg = retry or RetryConfig()
    last_exc: Exception | None = None
    for attempt in range(1, cfg.max_attempts + 1):
        try:
            return await runnable.ainvoke(input)
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt >= cfg.max_attempts or not _is_retryable_exception(e):
                raise
            await asyncio.sleep(_compute_backoff(attempt, cfg))
    if last_exc:
        raise last_exc
    raise RuntimeError("_retryable_ainvoke failed without exception (unexpected).")


def with_fallbacks(
    primary: Any,
    fallbacks: Sequence[Any],
) -> Any:
    """Attach fallbacks using LangChain's `with_fallbacks` when available."""
    if not fallbacks:
        return primary
    # Many LangChain runnables implement .with_fallbacks([...])
    method = getattr(primary, "with_fallbacks", None)
    if callable(method):
        return method(list(fallbacks))
    # If unavailable, keep primary (pipelines can implement manual fallback at call sites).
    return primary


def build_chat_runnable(
    *,
    model_names: Sequence[str],
    registry: ModelRegistry | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: int | None = None,
    retry: RetryConfig | None = None,
    extra_kwargs: Optional[MutableMapping[str, Any]] = None,
) -> Any:
    """Create a runnable chat model with fallbacks (when supported).

    Note: some downstream components (e.g., certain experimental transformers) may
    require a concrete BaseChatModel instance; in those cases, prefer `create_chat_models`
    and implement manual fallback around `.invoke()`.
    """
    models = create_chat_models(
        model_names,
        registry=registry,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        extra_kwargs=extra_kwargs,
    )
    runnable = with_fallbacks(models[0], models[1:])

    # IMPORTANT: keep this as a LangChain Runnable so `prompt | llm | parser` works.
    # Use LangChain's built-in retry wrapper rather than returning a custom object
    # (custom wrappers may not be recognized by LCEL's `|` operator).
    if retry is None:
        return runnable

    # `with_retry` uses Tenacity under the hood and returns a Runnable.
    # We map our RetryConfig to "stop after N attempts". Backoff/jitter are left
    # to LangChain defaults for now to preserve Runnable compatibility.
    return runnable.with_retry(stop_after_attempt=retry.max_attempts)


def retryable_invoke_models(
    models: Sequence[Any],
    input: Any,
    *,
    retry: RetryConfig | None = None,
) -> Any:
    """Invoke models in order (primary then fallbacks), with retry per model.

    This is useful for components that require a concrete chat model object and do not
    accept a generic runnable with fallbacks.
    """
    if not models:
        raise ValueError("models must be non-empty.")
    last_exc: Exception | None = None
    for m in models:
        try:
            return retryable_invoke(m, input, retry=retry)
        except Exception as e:  # noqa: BLE001
            last_exc = e
            continue
    if last_exc:
        raise last_exc
    raise RuntimeError("All models failed without exception (unexpected).")


