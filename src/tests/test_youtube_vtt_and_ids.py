"""Caption VTT parsing + Chroma document id scheme."""

from __future__ import annotations

import unittest

from src.pipeline.youtube.index_chroma import create_segment_documents, create_summary_document
from src.pipeline.youtube.transcribe import parse_vtt


class TestYoutubeVttAndIds(unittest.TestCase):
    def test_parse_vtt_basic(self) -> None:
        vtt = """WEBVTT

00:00:00.000 --> 00:00:01.500
Hello world

00:00:01.500 --> 00:00:03.000
Welcome to Alpha
"""
        cues = parse_vtt(vtt)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0]["text"], "Hello world")
        self.assertAlmostEqual(cues[1]["start_time"], 1.5)

    def test_segment_id_includes_video_id(self) -> None:
        docs = create_segment_documents(
            {
                "video_id": "abcABCabcAB",
                "title": "Demo",
                "segments": [
                    {
                        "topic": "Intro",
                        "start_time": 12.4,
                        "end_time": 40.0,
                        "summary": "Intro",
                        "content": "Hello from a unique YouTube clip.",
                    }
                ],
            }
        )
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].metadata["_chroma_id"], "youtube_transcript_segment_abcABCabcAB_12")
        self.assertEqual(docs[0].metadata["type"], "youtube_transcript_segment")
        self.assertEqual(docs[0].metadata["video_id"], "abcABCabcAB")
        self.assertEqual(docs[0].metadata["canonical_url"], "https://www.youtube.com/watch?v=abcABCabcAB")

    def test_summary_id_includes_video_id(self) -> None:
        doc = create_summary_document(
            {
                "video_id": "abcABCabcAB",
                "title": "Demo",
                "bucket": "unique_longform",
                "source_type": "youtube_video",
                "generated_content": {
                    "overview": "An overview.",
                    "thesis": "Thesis.",
                    "themes": ["learning"],
                    "key_takeaways": ["one"],
                },
            }
        )
        self.assertEqual(doc.metadata["_chroma_id"], "youtube_summary_overview_abcABCabcAB")
        self.assertEqual(doc.metadata["type"], "youtube_summary_overview")


if __name__ == "__main__":
    unittest.main()
