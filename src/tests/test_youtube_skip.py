"""Skip-rule tests for unique YouTube ingest (no network)."""

from __future__ import annotations

import unittest

from src.pipeline.youtube.constants import PLAYLIST_GIFTED_MINDS_EPISODES, SKIP_PROOF_VIDEO_ID
from src.pipeline.youtube.skip import (
    SKIP_EPISODES_PLAYLIST,
    SKIP_RSS_INTRO_TWIN,
    SKIP_RSS_TITLE_MATCH,
    SKIP_SAME_RECORDING,
    skip_decision,
)


def _rss(title: str, duration: str | None = None) -> dict:
    return {"title": title, "itunes_duration": duration}


class TestYoutubeSkipRules(unittest.TestCase):
    def test_alpha_anywhere_title_match(self) -> None:
        rss = [_rss("S2E335: Alpha Anywhere: Elite At-Home Academics, Worldwide", "00:42:10")]
        decision = skip_decision(
            video_id=SKIP_PROOF_VIDEO_ID,
            title="Alpha Anywhere: Elite At-Home Academics, Worldwide",
            duration_seconds=2530,
            rss_entries=rss,
            episodes_playlist_ids=set(),
        )
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.skip_reason, SKIP_RSS_TITLE_MATCH)
        self.assertIn("Alpha Anywhere", decision.matched_rss_title or "")

    def test_gifted_minds_intro_twin(self) -> None:
        rss = [_rss("Intro to Academics", "00:01:17")]
        decision = skip_decision(
            video_id="abcABCabcAB",
            title="Gifted Minds: Academics",
            duration_seconds=77,
            rss_entries=rss,
            episodes_playlist_ids=set(),
        )
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.skip_reason, SKIP_RSS_INTRO_TWIN)

    def test_episodes_playlist_by_id(self) -> None:
        decision = skip_decision(
            video_id="vFAIv_boPqw",
            title="Consult, Don't Manage: … Ep #1",
            duration_seconds=1800,
            rss_entries=[],
            episodes_playlist_ids={"vFAIv_boPqw"},
        )
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.skip_reason, SKIP_EPISODES_PLAYLIST)

    def test_episodes_playlist_by_playlist_id(self) -> None:
        decision = skip_decision(
            video_id="xxxxxxxxxxx",
            title="Unrelated",
            duration_seconds=60,
            rss_entries=[],
            episodes_playlist_ids=set(),
            playlist_id=PLAYLIST_GIFTED_MINDS_EPISODES,
        )
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.skip_reason, SKIP_EPISODES_PLAYLIST)

    def test_same_recording_rewritten_title(self) -> None:
        rss = [_rss("S2E355: Steal the Stock Market Simulation We Use to Build Young Investors", "00:36:42")]
        decision = skip_decision(
            video_id="49GX6Zw5les",
            title="How We Teach 7-Year-Olds the Stock Market",
            duration_seconds=2203,
            rss_entries=rss,
            episodes_playlist_ids=set(),
        )
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.skip_reason, SKIP_SAME_RECORDING)

    def test_duration_collision_without_title_cue_is_not_skipped(self) -> None:
        # Inventory: Here's How I'm Fixing School vs an unrelated RSS item at the same length.
        rss = [_rss("S2E100: NFL Homeschool Sideline Secrets Nobody Asked For", "00:21:19")]
        decision = skip_decision(
            video_id="fixSchool01",
            title="Here's How I'm Fixing School",
            duration_seconds=1279,
            rss_entries=rss,
            episodes_playlist_ids=set(),
        )
        self.assertIsNone(decision)

    def test_offchannel_appearance_duration_collision_not_skipped(self) -> None:
        rss = [_rss("S2E234: Alpha Dads Talk Alternative Education (Part 1)", "00:23:59")]
        decision = skip_decision(
            video_id="jO6QFe8D0Fo",
            title="MacKenzie Price Keynote-The Game of Jenga in Education",
            duration_seconds=1442,
            rss_entries=rss,
            episodes_playlist_ids=set(),
            allow_same_recording=False,
        )
        self.assertIsNone(decision)

    def test_unique_branding_is_not_skipped(self) -> None:
        rss = [_rss("S2E335: Alpha Anywhere: Elite At-Home Academics, Worldwide", "00:42:10")]
        decision = skip_decision(
            video_id="meetGifted1",
            title="Meet Gifted Minds",
            duration_seconds=120,
            rss_entries=rss,
            episodes_playlist_ids=set(),
        )
        self.assertIsNone(decision)


if __name__ == "__main__":
    unittest.main()
