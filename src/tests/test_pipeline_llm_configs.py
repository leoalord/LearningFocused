"""Smoke tests for pipeline LLM config modules.

These validate that task-specific config helpers build runnable LLMs given env vars.
No provider APIs are called.
"""

from __future__ import annotations

import os
import unittest


class TestPipelineLLMConfigs(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = os.environ.copy()
        os.environ["OPENAI_API_KEY"] = "test-openai"
        os.environ["GOOGLE_API_KEY"] = "test-google"
        os.environ["ANTHROPIC_API_KEY"] = "test-anthropic"
        os.environ["FIREWORKS_API_KEY"] = "test-fireworks"

        # Keep tests fast: avoid real backoff waits.
        os.environ["LF_AUDIO_LLM_INITIAL_BACKOFF_SECONDS"] = "0.0"
        os.environ["LF_SUBSTACK_LLM_INITIAL_BACKOFF_SECONDS"] = "0.0"
        os.environ["LF_NEO4J_GRAPH_LLM_INITIAL_BACKOFF_SECONDS"] = "0.0"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)

    def test_audio_llm_builders(self) -> None:
        from src.pipeline.audio.llm_config import (
            get_combined_summary_llm,
            get_grouping_llm,
            get_segmentation_llm,
            get_speaker_id_llm,
        )

        self.assertTrue(hasattr(get_grouping_llm(), "invoke"))
        self.assertTrue(hasattr(get_combined_summary_llm(), "invoke"))
        self.assertTrue(hasattr(get_segmentation_llm(), "invoke"))
        self.assertTrue(hasattr(get_speaker_id_llm(), "invoke"))

    def test_substack_llm_builders(self) -> None:
        from src.pipeline.substack.llm_config import get_article_summary_llm

        self.assertTrue(hasattr(get_article_summary_llm(), "invoke"))

    def test_youtube_llm_builders(self) -> None:
        os.environ["LF_YOUTUBE_LLM_INITIAL_BACKOFF_SECONDS"] = "0.0"
        from src.pipeline.youtube.llm_config import get_segmentation_llm, get_summary_llm

        self.assertTrue(hasattr(get_segmentation_llm(), "invoke"))
        self.assertTrue(hasattr(get_summary_llm(), "invoke"))

    def test_neo4j_llm_models_builder(self) -> None:
        from src.pipeline.neo4j_llm_config import get_graph_llm_models

        models = get_graph_llm_models()
        self.assertGreaterEqual(len(models), 1)
        self.assertTrue(hasattr(models[0], "invoke") or hasattr(models[0], "ainvoke"))


