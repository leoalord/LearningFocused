"""Unit tests for batched episode grouping (no provider APIs)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from src.pipeline.audio.generate_summaries import (
    EpisodeGroup,
    _is_grouping_timeout,
    episode_sort_key,
    filename_title_key,
    group_episodes,
    identical_title_groups,
    iter_grouping_batches,
    merge_and_dedupe_groups,
)


def _group(group_id: str, *filenames: str) -> EpisodeGroup:
    return EpisodeGroup(group_id=group_id, filenames=list(filenames), reasoning="test")


class TestIterGroupingBatches(unittest.TestCase):
    def test_small_list_is_a_single_batch(self) -> None:
        files = [f"S2E{i}.json" for i in range(1, 11)]
        batches = iter_grouping_batches(files, batch_size=60, overlap=8)
        self.assertEqual(batches, [files])

    def test_batches_stay_within_size_and_cover_every_file(self) -> None:
        files = [f"S2E{i:03d}.json" for i in range(1, 333)]
        batch_size, overlap = 60, 8
        batches = iter_grouping_batches(files, batch_size=batch_size, overlap=overlap)

        self.assertGreater(len(batches), 1)
        self.assertTrue(all(1 <= len(batch) <= batch_size for batch in batches))
        self.assertEqual(batches[0], files[:batch_size])
        self.assertEqual(batches[-1][-1], files[-1])

        seen: set[str] = set()
        for batch in batches:
            seen.update(batch)
        self.assertEqual(seen, set(files))

        for prev, nxt in zip(batches, batches[1:]):
            overlap_names = set(prev) & set(nxt)
            self.assertGreaterEqual(len(overlap_names), overlap)

    def test_rejects_overlap_not_smaller_than_batch_size(self) -> None:
        with self.assertRaises(ValueError):
            iter_grouping_batches(["a.json"], batch_size=8, overlap=8)


class TestMergeAndDedupeGroups(unittest.TestCase):
    def test_overlapping_part_series_merge_and_each_file_appears_once(self) -> None:
        files = [f"S2E{i}.json" for i in range(58, 63)]
        # Batch 1 cut between 60/61; overlap lets batch 2 see 59-60 with 61.
        groups = [
            _group("solo-58", "S2E58.json"),
            _group("parts-59-60", "S2E59.json", "S2E60.json"),
            _group("parts-59-61", "S2E59.json", "S2E60.json", "S2E61.json"),
            _group("solo-62", "S2E62.json"),
        ]
        merged = merge_and_dedupe_groups(files, groups)
        assigned = [name for group in merged for name in group.filenames]
        self.assertEqual(sorted(assigned), sorted(files))
        self.assertEqual(len(assigned), len(set(assigned)))

        series = next(g for g in merged if "S2E59.json" in g.filenames)
        self.assertEqual(set(series.filenames), {"S2E59.json", "S2E60.json", "S2E61.json"})
        self.assertEqual(len([g for g in merged if "S2E58.json" in g.filenames]), 1)
        self.assertEqual(len([g for g in merged if "S2E62.json" in g.filenames]), 1)

    def test_complete_seven_part_series_is_not_truncated(self) -> None:
        files = [f"S2E{i}.json" for i in range(1, 8)]
        groups = [_group("parts-1-7", *[f"S2E{i}.json" for i in range(1, 8)])]
        merged = merge_and_dedupe_groups(files, groups)
        series = next(g for g in merged if g.group_id == "parts-1-7")
        self.assertEqual(len(series.filenames), 7)

    def test_drops_hallucinated_filenames_and_fills_missing_as_singletons(self) -> None:
        files = ["S2E1.json", "S2E2.json", "S2E3.json"]
        groups = [_group("invented", "S2E1.json", "S2E999.json")]
        merged = merge_and_dedupe_groups(files, groups)
        by_file = {name: group for group in merged for name in group.filenames}
        self.assertEqual(set(by_file), set(files))
        self.assertEqual(by_file["S2E1.json"].filenames, ["S2E1.json"])
        self.assertNotIn("S2E999.json", [n for g in merged for n in g.filenames])
        self.assertEqual(by_file["S2E2.json"].filenames, ["S2E2.json"])
        self.assertEqual(by_file["S2E3.json"].filenames, ["S2E3.json"])

    def test_partial_overlap_does_not_fuse_two_series(self) -> None:
        files = [f"S2E{i}.json" for i in range(4, 9)]
        groups = [
            _group("topicX", "S2E4.json", "S2E5.json", "S2E6.json"),
            _group("topicY", "S2E6.json", "S2E7.json", "S2E8.json"),
        ]
        merged = merge_and_dedupe_groups(files, groups)
        by_id = {g.group_id: set(g.filenames) for g in merged}
        self.assertEqual(by_id["topicX"], {"S2E4.json", "S2E5.json", "S2E6.json"})
        self.assertEqual(by_id["topicY"], {"S2E7.json", "S2E8.json"})
        assigned = [name for group in merged for name in group.filenames]
        self.assertEqual(sorted(assigned), sorted(files))

    def test_identical_titles_lock_across_sort_distance(self) -> None:
        files = [
            "S E17 How to Use 2Hr Learning.json",
            "S E18 Other.json",
            "S E152 How to Use 2Hr Learning.json",
        ]
        locked = identical_title_groups(files)
        self.assertEqual(len(locked), 1)
        merged = merge_and_dedupe_groups(files, [_group("solo", "S E18 Other.json")], locked=locked)
        pair = next(g for g in merged if len(g.filenames) == 2)
        self.assertEqual(
            set(pair.filenames),
            {"S E17 How to Use 2Hr Learning.json", "S E152 How to Use 2Hr Learning.json"},
        )

    def test_timeout_matcher_ignores_token_counts(self) -> None:
        self.assertFalse(_is_grouping_timeout(RuntimeError("requested 15042 tokens")))
        self.assertTrue(_is_grouping_timeout(TimeoutError("timed out")))
        err = RuntimeError("gateway timeout")
        err.status_code = 504  # type: ignore[attr-defined]
        self.assertTrue(_is_grouping_timeout(err))

    def test_filename_title_key_strips_episode_token(self) -> None:
        self.assertEqual(
            filename_title_key("S E17 How to Use 2Hr Learning.json"),
            filename_title_key("S E152 How to Use 2Hr Learning.json"),
        )


class TestGroupEpisodesBatching(unittest.TestCase):
    def test_group_episodes_invokes_one_llm_batch_at_a_time(self) -> None:
        files = [f"S2E{i:03d}.json" for i in range(1, 121)]
        seen_sizes: list[int] = []

        def fake_batch(episodes_context, llm, failures=None):
            names = [item["filename"] for item in episodes_context]
            seen_sizes.append(len(names))
            self.assertLessEqual(len(names), 60)
            return [_group(name, name) for name in names]

        with patch(
            "src.pipeline.audio.generate_summaries.get_grouping_llm",
            return_value=object(),
        ), patch(
            "src.pipeline.audio.generate_summaries._group_single_batch",
            side_effect=fake_batch,
        ):
            groups = group_episodes(files, Path("/tmp/unused-metadata"), batch_size=60, overlap=8)

        self.assertGreater(len(seen_sizes), 1)
        self.assertTrue(all(size <= 60 for size in seen_sizes))
        assigned = [name for group in groups for name in group.filenames]
        self.assertEqual(sorted(assigned), sorted(files))
        self.assertEqual(len(assigned), len(files))

    def test_episode_sort_key_orders_by_season_then_number(self) -> None:
        names = [
            "Intro to Life Skills.json",
            "S2E356 Later.json",
            "S2E290 Earlier.json",
            "S1E9 First.json",
        ]
        ordered = sorted(names, key=episode_sort_key)
        self.assertEqual(
            ordered,
            [
                "S1E9 First.json",
                "S2E290 Earlier.json",
                "S2E356 Later.json",
                "Intro to Life Skills.json",
            ],
        )


if __name__ == "__main__":
    unittest.main()
