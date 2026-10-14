"""Channel-feed selection: every new upload, podcast twins skipped, bounded per run."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from src.pipeline.youtube.constants import BUCKET_APPEARANCE, BUCKET_UNIQUE_LONGFORM, VIDEOS_TAB_URL
from src.pipeline.youtube.run import build_parser, main, select_feed_candidates

RSS = [{"title": "S2E354: Are You Passing Limiting Beliefs to Your Kid? (6 Simple Fixes)", "duration_seconds": 1500}]
LISTINGS = [
    ("https://example.invalid/appearances", BUCKET_APPEARANCE, "PLapp", False),
    (VIDEOS_TAB_URL, BUCKET_UNIQUE_LONGFORM, None, True),
]
ITEMS = {
    "https://example.invalid/appearances": [
        {"video_id": "app00000001", "title": "Keynote on Jenga", "duration_seconds": 600},
    ],
    VIDEOS_TAB_URL: [
        {"video_id": "new00000001", "title": "Brand new essay", "duration_seconds": 700},
        {"video_id": "dup00000001", "title": "Are You Passing Limiting Beliefs to Your Kid? (6 Simple Fixes)", "duration_seconds": 1500},
        {"video_id": "old00000001", "title": "Already ingested", "duration_seconds": 800},
        {"video_id": "app00000001", "title": "Keynote on Jenga", "duration_seconds": 600},
        {"video_id": "new00000002", "title": "Another new upload", "duration_seconds": 900},
    ],
}


def fake_flat(url: str, *, allow_videos_tab: bool = False) -> list[dict]:
    return ITEMS[url]


class TestSelectFeedCandidates(unittest.TestCase):
    def select(self, max_new: int = 10):
        with patch("src.pipeline.youtube.run._flat_items", side_effect=fake_flat):
            return select_feed_candidates(
                rss_entries=RSS,
                episodes_ids=set(),
                max_new=max_new,
                listings=LISTINGS,
                already_ingested=lambda vid: vid == "old00000001",
            )

    def test_new_uploads_selected_and_podcast_twin_skipped(self) -> None:
        selected, skipped = self.select()
        self.assertEqual([c.video_id for c in selected], ["app00000001", "new00000001", "new00000002"])
        self.assertEqual(selected[0].bucket, BUCKET_APPEARANCE)
        self.assertEqual(selected[1].bucket, BUCKET_UNIQUE_LONGFORM)
        self.assertEqual([d.video_id for d in skipped], ["dup00000001"])

    def test_max_new_bounds_each_run(self) -> None:
        selected, _ = self.select(max_new=2)
        self.assertEqual([c.video_id for c in selected], ["app00000001", "new00000001"])

    def test_given_up_videos_are_excluded(self) -> None:
        with patch("src.pipeline.youtube.run._flat_items", side_effect=fake_flat):
            selected, _ = select_feed_candidates(
                rss_entries=RSS, episodes_ids=set(), listings=LISTINGS,
                already_ingested=lambda vid: False, exclude={"new00000001"},
            )
        self.assertNotIn("new00000001", [c.video_id for c in selected])

    def test_failure_ledger_counts_and_clears(self) -> None:
        import tempfile
        from pathlib import Path

        from src.pipeline.youtube import run as yt

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(yt, "FEED_FAILURES_PATH", Path(tmp) / "_feed_failures.json"):
                for _ in range(yt.FEED_MAX_ATTEMPTS):
                    yt.update_feed_failures(ingested=[], failed=[{"video_id": "bad", "error": "no captions"}])
                self.assertEqual(yt.given_up_video_ids(), {"bad"})
                yt.update_feed_failures(ingested=["bad"], failed=[])
                self.assertEqual(yt.given_up_video_ids(), set())

    def test_cli_flags(self) -> None:
        args = build_parser().parse_args(["--feed", "--max-new", "3"])
        self.assertTrue(args.feed)
        self.assertEqual(args.max_new, 3)
        self.assertEqual(main(["--feed", "--max-new", "-1"]), 2)
        self.assertEqual(main(["--feed", "--video-id", "abc"]), 2)


if __name__ == "__main__":
    unittest.main()
