"""CLI refuse-closed tests for the YouTube runner."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.pipeline.youtube.run import build_parser, main


class TestYoutubeRunRefuses(unittest.TestCase):
    def test_help_exits_zero(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit) as ctx:
            parser.parse_args(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_print_plan(self) -> None:
        code = main(["--print-plan"])
        self.assertEqual(code, 0)

    def test_refuses_channel(self) -> None:
        code = main(["--channel", "https://www.youtube.com/@future_of_education"])
        self.assertEqual(code, 2)

    def test_refuses_reset_chroma(self) -> None:
        code = main(["--reset-chroma"])
        self.assertEqual(code, 2)

    def test_refuses_uu_playlist(self) -> None:
        code = main(["--playlist", "https://www.youtube.com/playlist?list=UUKHZkY1J1NyKypLn80Pj19A"])
        self.assertEqual(code, 2)

    def test_refuses_thealphaschool(self) -> None:
        code = main(["--playlist", "https://www.youtube.com/@thealphaschool"])
        self.assertEqual(code, 2)

    def test_refuses_benign_playlist_that_is_not_wired(self) -> None:
        code = main(["--playlist", "https://www.youtube.com/playlist?list=PLabcdefghij"])
        self.assertEqual(code, 2)

    def test_refuses_limits_above_v1_cap(self) -> None:
        code = main(["--longform-limit", "500"])
        self.assertEqual(code, 2)

    def test_refuses_empty_rss_without_override(self) -> None:
        with patch("src.pipeline.youtube.run.load_rss_entries", return_value=[]), patch(
            "src.pipeline.youtube.run._episodes_playlist_ids", return_value=set()
        ):
            code = main(["--dry-run"])
        self.assertEqual(code, 2)

    def test_prune_stale_chroma_flag_exists_and_defaults_off(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        self.assertFalse(args.prune_stale_chroma)
        args = parser.parse_args(["--prune-stale-chroma"])
        self.assertTrue(args.prune_stale_chroma)

    def test_ingest_one_skips_complete_artifacts_without_ytdlp(self) -> None:
        from src.pipeline.youtube.run import Candidate, ingest_one

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcripts = root / "t"
            segmented = root / "s"
            summaries = root / "u"
            for d in (transcripts, segmented, summaries):
                d.mkdir()
            vid = "abcdefghijk"
            (transcripts / f"{vid}.json").write_text("{}", encoding="utf-8")
            (segmented / f"{vid}.json").write_text("{}", encoding="utf-8")
            (summaries / f"{vid}.json").write_text("{}", encoding="utf-8")
            cand = Candidate(video_id=vid, title="already ingested", bucket="unique_longform")
            with (
                patch("src.pipeline.youtube.run.YOUTUBE_SEGMENTED_DIR", segmented),
                patch("src.pipeline.youtube.run.YOUTUBE_SUMMARIES_DIR", summaries),
                patch("src.pipeline.youtube.run.transcript_exists", return_value=transcripts / f"{vid}.json"),
                patch("src.pipeline.youtube.run._enrich_candidate") as enrich,
                patch("src.pipeline.youtube.run.download_captions") as captions,
            ):
                out = ingest_one(cand, force=False)
            self.assertEqual(out, vid)
            enrich.assert_not_called()
            captions.assert_not_called()


if __name__ == "__main__":
    unittest.main()
