"""Unit tests for gold-set hit scoring (no Chroma / network)."""

from __future__ import annotations

import unittest
from pathlib import Path

from evals.scoring import (
    MISS_NOT_IN_CORPUS,
    MISS_RETRIEVER_FAILED,
    classify_miss,
    concatenate_retrieved_text,
    load_gold_questions,
    parse_source_hint,
    score_hit,
    slice_name,
)

GOLD_PATH = Path(__file__).resolve().parent / "gold_questions.json"


class TestHitDefinition(unittest.TestCase):
    def test_hit_requires_every_phrase_casefold(self) -> None:
        text = concatenate_retrieved_text(
            ["Alpha ANYWHERE is run by joe morone at alphaanywhere.co"]
        )
        result = score_hit(
            must_include=["Alpha Anywhere", "Joe Morone", "alphaanywhere.co"],
            retrieved_text=text,
        )
        self.assertTrue(result.hit)
        self.assertEqual(result.missing_phrases, ())

    def test_miss_if_any_phrase_absent_no_partial_credit(self) -> None:
        result = score_hit(
            must_include=["Alpha Anywhere", "Joe Morone", "alphaanywhere.co"],
            retrieved_text="Alpha Anywhere with Joe Morone",
        )
        self.assertFalse(result.hit)
        self.assertEqual(result.missing_phrases, ("alphaanywhere.co",))

    def test_source_hint_is_not_an_argument_to_score_hit(self) -> None:
        # score_hit has no source_hint parameter; metadata match is not scoring.
        result = score_hit(must_include=["Pygmalion effect"], retrieved_text="the pygmalion effect")
        self.assertTrue(result.hit)


class TestSourceHintAndMissSplit(unittest.TestCase):
    def test_parse_podcast_and_substack_hints(self) -> None:
        podcast = parse_source_hint(
            "podcast S2E335 (published 2026-05-27) | "
            "segmented_transcripts/S2E335 Alpha Anywhere Elite At-Home Academics, Worldwide_segmented.json"
        )
        self.assertEqual(podcast.episode_token, "S2E335")
        self.assertIn("segmented_transcripts/", podcast.artifact_paths[0])

        spaced = parse_source_hint(
            "podcast S E104 (published 2024-07-04) | "
            "segmented_transcripts/S E104 The Revolutionary 2-Hour Learning System_segmented.json"
        )
        self.assertEqual(spaced.episode_token, "S E104")

        substack = parse_source_hint(
            "substack ASK ME ANYTHING #10 | "
            "substack_articles/text/ask-me-anything-10-what-is-map-testing-1480fd4af2.md | "
            "article_summaries/ask-me-anything-10-what-is-map-testing-1480fd4af2_summary.json"
        )
        self.assertEqual(substack.doc_id, "ask-me-anything-10-what-is-map-testing-1480fd4af2")

    def test_not_indexed_is_not_in_corpus(self) -> None:
        self.assertEqual(
            classify_miss(disk_present=True, indexed=False, phrases_in_corpus=False),
            MISS_NOT_IN_CORPUS,
        )
        self.assertEqual(
            classify_miss(disk_present=False, indexed=False),
            MISS_NOT_IN_CORPUS,
        )

    def test_indexed_miss_is_retriever_failed(self) -> None:
        self.assertEqual(
            classify_miss(disk_present=True, indexed=True, phrases_in_corpus=True),
            MISS_RETRIEVER_FAILED,
        )


class TestGoldSetLoads(unittest.TestCase):
    def test_eighteen_items_with_required_fields(self) -> None:
        items = load_gold_questions(GOLD_PATH)
        self.assertEqual(len(items), 18)
        ids = [q.id for q in items]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sum(1 for q in items if slice_name(q.id) == "2026_podcast"), 9)
        self.assertEqual(sum(1 for q in items if slice_name(q.id) == "substack"), 5)
        self.assertEqual(sum(1 for q in items if slice_name(q.id) == "earlier_podcast"), 4)
        for q in items:
            self.assertTrue(q.question)
            self.assertTrue(q.must_include)
            self.assertTrue(q.source_hint)


if __name__ == "__main__":
    unittest.main()
