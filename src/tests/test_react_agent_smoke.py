"""Smoke tests for React agent model creation wiring.

Validates that react_agent delegates model instantiation to the shared factory and
that missing keys error cleanly.
"""

from __future__ import annotations

import os
import unittest


class TestReactAgentModelCreation(unittest.TestCase):
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
        from src.react_agent.utils import create_chat_model

        m = create_chat_model("gpt-5.1-mini")
        self.assertIsNotNone(m)

    def test_create_chat_model_gemini(self) -> None:
        from src.react_agent.utils import create_chat_model

        m = create_chat_model("gemini-3-flash-preview")
        self.assertIsNotNone(m)

    def test_create_chat_model_anthropic(self) -> None:
        from src.react_agent.utils import create_chat_model

        m = create_chat_model("claude-sonnet-4-5")
        self.assertIsNotNone(m)

    def test_missing_key_raises(self) -> None:
        from src.react_agent.utils import create_chat_model

        os.environ.pop("OPENAI_API_KEY", None)
        with self.assertRaises(ValueError):
            create_chat_model("gpt-5.1-mini")

    def test_graph_imports_without_error(self) -> None:
        from src.react_agent.graph import get_react_agent, react_agent

        self.assertIsNotNone(react_agent)
        self.assertTrue(callable(get_react_agent))

    def test_default_model_is_gemini_flash_latest(self) -> None:
        from src.react_agent.configuration import DEFAULT_MODEL, Configuration

        self.assertEqual(DEFAULT_MODEL, "gemini-flash-latest")
        self.assertEqual(Configuration().model, "gemini-flash-latest")


