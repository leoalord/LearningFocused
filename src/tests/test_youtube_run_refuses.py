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


if __name__ == "__main__":
    unittest.main()
