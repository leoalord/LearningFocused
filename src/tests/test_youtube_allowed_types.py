"""allowed_types + formatter coverage for YouTube Chroma types."""

from __future__ import annotations

import unittest

from langchain_core.documents import Document

from src.database.chroma_manager import DEFAULT_SEGMENT_TYPES, DEFAULT_SUMMARY_TYPES
from src.deep_research_agent.tools import _format_youtube_hit
from src.pipeline.index_chroma import YOUTUBE_CHROMA_TYPES


class TestYoutubeAllowedTypes(unittest.TestCase):
    def test_query_segments_defaults_include_youtube(self) -> None:
        self.assertIn("youtube_transcript_segment", DEFAULT_SEGMENT_TYPES)
        self.assertIn("transcript_segment", DEFAULT_SEGMENT_TYPES)
        self.assertIn("article_text", DEFAULT_SEGMENT_TYPES)

    def test_query_summaries_defaults_include_youtube(self) -> None:
        self.assertIn("youtube_summary_overview", DEFAULT_SUMMARY_TYPES)
        self.assertIn("article_summary_overview", DEFAULT_SUMMARY_TYPES)

    def test_index_helper_types(self) -> None:
        self.assertEqual(
            set(YOUTUBE_CHROMA_TYPES),
            {"youtube_transcript_segment", "youtube_summary_overview"},
        )

    def test_formatter_uses_title_and_url_not_episode(self) -> None:
        doc = Document(
            page_content="Meet the school with no teachers.",
            metadata={
                "type": "youtube_transcript_segment",
                "title": "Meet Gifted Minds",
                "canonical_url": "https://www.youtube.com/watch?v=abcdefghijk",
                "topic": "Branding",
            },
        )
        rendered = _format_youtube_hit(doc)
        self.assertIn("YouTube: Meet Gifted Minds", rendered)
        self.assertIn("https://www.youtube.com/watch?v=abcdefghijk", rendered)
        self.assertNotIn("Episode:", rendered)


if __name__ == "__main__":
    unittest.main()
