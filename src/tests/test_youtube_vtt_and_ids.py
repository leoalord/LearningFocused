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

    def test_parse_vtt_collapses_rollup_captions(self) -> None:
        """YouTube auto-subs restate the visible window; each line must appear once.

        Shape copied from `youtube_videos/captions/*.en.vtt`: a whitespace-only
        first body line, inline timing tags, and a ~10ms "flush" cue between each
        pair of wide cues.
        """
        vtt = """WEBVTT
Kind: captions
Language: en

00:00:03.980 --> 00:00:06.889 align:start position:0%
 
thank<00:00:04.980><c> you</c><00:00:05.100><c> everybody</c>

00:00:06.889 --> 00:00:06.899 align:start position:0%
thank you everybody
 

00:00:06.899 --> 00:00:08.990 align:start position:0%
thank you everybody
really<00:00:07.140><c> excited</c><00:00:07.740><c> to</c><00:00:07.859><c> be</c>

00:00:08.990 --> 00:00:09.000 align:start position:0%
really excited to be
 

00:00:09.000 --> 00:00:10.669 align:start position:0%
really excited to be
here<00:00:09.140><c> now</c>
"""
        cues = parse_vtt(vtt)
        self.assertEqual(
            [c["text"] for c in cues],
            ["thank you everybody", "really excited to be", "here now"],
        )
        # A dropped flush cue extends the cue it repeats rather than losing its span.
        self.assertAlmostEqual(cues[0]["start_time"], 3.98)
        self.assertAlmostEqual(cues[0]["end_time"], 6.899)
        self.assertAlmostEqual(cues[2]["start_time"], 9.0)

    def test_parse_vtt_collapses_three_line_rollup(self) -> None:
        """A window taller than two lines still collapses to one copy per line."""
        vtt = """WEBVTT

00:00:00.000 --> 00:00:02.000
one two

00:00:02.000 --> 00:00:04.000
one two
three four

00:00:04.000 --> 00:00:06.000
one two
three four
five six

00:00:06.000 --> 00:00:08.000
three four
five six
seven eight
"""
        cues = parse_vtt(vtt)
        self.assertEqual(
            " ".join(c["text"] for c in cues),
            "one two three four five six seven eight",
        )

    def test_parse_vtt_keeps_genuinely_repeated_phrase(self) -> None:
        """Non-adjacent repeats are real speech, not rolling restatement."""
        vtt = """WEBVTT

00:00:00.000 --> 00:00:02.000
we can do this

00:00:02.000 --> 00:00:04.000
and so today

00:00:04.000 --> 00:00:06.000
we can do this
"""
        cues = parse_vtt(vtt)
        self.assertEqual(len(cues), 3)
        self.assertEqual(cues[2]["text"], "we can do this")

    def test_parse_vtt_accepts_single_digit_hour(self) -> None:
        vtt = """WEBVTT

1:02:03.500 --> 1:02:05.500
past the hour mark
"""
        cues = parse_vtt(vtt)
        self.assertEqual(len(cues), 1)
        self.assertAlmostEqual(cues[0]["start_time"], 3723.5)

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
        self.assertEqual(
            docs[0].metadata["_chroma_id"],
            "youtube_transcript_segment_abcABCabcAB_12.40_Intro",
        )
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

    def test_sub_second_segments_get_distinct_ids(self) -> None:
        """Whole-second ids collapsed segments less than 1s apart onto one row."""
        docs = create_segment_documents(
            {
                "video_id": "abcABCabcAB",
                "title": "Demo",
                "segments": [
                    {"topic": "A", "start_time": 12.4, "end_time": 12.9, "content": "first"},
                    {"topic": "B", "start_time": 12.9, "end_time": 40.0, "content": "second"},
                ],
            }
        )
        ids = [d.metadata["_chroma_id"] for d in docs]
        self.assertEqual(len(set(ids)), 2, ids)

    def test_find_caption_file_prefers_canonical_en_track(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from src.pipeline.youtube import download as dl

        with tempfile.TemporaryDirectory() as tmp:
            cap = Path(tmp)
            (cap / "ObxGVl-PU3I.en-en-nP7-2PuUl7o.vtt").write_text("hashed")
            (cap / "ObxGVl-PU3I.en.vtt").write_text("canonical")
            with patch.object(dl, "YOUTUBE_CAPTIONS_DIR", cap):
                picked = dl.find_caption_file("ObxGVl-PU3I")
            assert picked is not None
            self.assertEqual(picked.name, "ObxGVl-PU3I.en.vtt")


if __name__ == "__main__":
    unittest.main()
