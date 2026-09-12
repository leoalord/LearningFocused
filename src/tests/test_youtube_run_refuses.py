"""CLI refuse-closed tests for the YouTube runner."""

from __future__ import annotations

import unittest

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
        from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
