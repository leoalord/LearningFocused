"""Smoke tests for Deep Research agent imports and default model wiring."""

from __future__ import annotations

import os
import unittest


class TestDeepResearchAgentImports(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = os.environ.copy()
        os.environ["OPENAI_API_KEY"] = "test-openai"
        os.environ["GOOGLE_API_KEY"] = "test-google"
        os.environ["ANTHROPIC_API_KEY"] = "test-anthropic"
        os.environ["FIREWORKS_API_KEY"] = "test-fireworks"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)

    def test_graph_imports_without_error(self) -> None:
        from src.deep_research_agent.graph import deep_researcher

        self.assertIsNotNone(deep_researcher)

    def test_default_model_family_is_gemini_flash_latest(self) -> None:
        from src.deep_research_agent.configuration import DEFAULT_MODEL_FAMILY, Configuration

        self.assertEqual(DEFAULT_MODEL_FAMILY, "gemini-flash-latest")
        cfg = Configuration()
        self.assertEqual(cfg.research_model, "gemini-flash-latest")
        self.assertEqual(cfg.final_report_model, "gemini-flash-latest")
