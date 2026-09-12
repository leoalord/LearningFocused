"""Unit tests for shared LLM factory utilities.

These tests are intentionally network-free: they validate configuration/instantiation
logic without calling any provider APIs.
"""

from __future__ import annotations

import os
import unittest

from src.llm.factory import RetryConfig, create_chat_model, parse_model_list, retryable_invoke


class _FlakyRunnable:
    def __init__(self, fail_times: int) -> None:
        self._remaining = fail_times
        self.calls = 0

    def invoke(self, _input: object) -> str:
        self.calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise RuntimeError("429 rate limit")
        return "ok"


class TestModelListParsing(unittest.TestCase):
    def test_parse_model_list_defaults_and_dedupes(self) -> None:
        self.assertEqual(parse_model_list(None, default=["a", "b"]), ["a", "b"])
        self.assertEqual(parse_model_list("", default=["a", "b"]), ["a", "b"])
        self.assertEqual(parse_model_list("a, b, a,  ,b", default=["x"]), ["a", "b"])


class TestRetryableInvoke(unittest.TestCase):
    def test_retryable_invoke_retries_on_transient_error(self) -> None:
        r = _FlakyRunnable(fail_times=2)
        out = retryable_invoke(r, {"x": 1}, retry=RetryConfig(max_attempts=5, initial_backoff_seconds=0.0))
        self.assertEqual(out, "ok")
        self.assertEqual(r.calls, 3)


class TestCreateChatModel(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = os.environ.copy()
        os.environ["OPENAI_API_KEY"] = "test-openai"
        os.environ["GOOGLE_API_KEY"] = "test-google"
        os.environ["ANTHROPIC_API_KEY"] = "test-anthropic"
        os.environ["FIREWORKS_API_KEY"] = "test-fireworks"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)

    def test_create_chat_model_openai(self) -> None:
        model = create_chat_model(model_name="gpt-5.1-mini", timeout=1)
        self.assertIsNotNone(model)

    def test_create_chat_model_google(self) -> None:
        model = create_chat_model(model_name="gemini-3-flash-preview", timeout=1)
        self.assertIsNotNone(model)

    def test_create_chat_model_gemini_flash_latest(self) -> None:
        model = create_chat_model(model_name="gemini-flash-latest", timeout=1)
        self.assertIsNotNone(model)
        self.assertEqual(getattr(model, "model", None), "gemini-flash-latest")

    def test_create_chat_model_anthropic(self) -> None:
        model = create_chat_model(model_name="claude-sonnet-4-5", timeout=1)
        self.assertIsNotNone(model)

    def test_missing_api_key_raises(self) -> None:
        os.environ.pop("GOOGLE_API_KEY", None)
        with self.assertRaises(ValueError):
            create_chat_model(model_name="gemini-3-flash-preview", timeout=1)


