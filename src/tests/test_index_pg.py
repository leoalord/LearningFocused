"""Direct Postgres indexing: same rows as the Chroma copy, embeds only what changed, guarded prune."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.documents import Document

from src.pipeline.audio.episode_ids import episode_key
from src.pipeline.index_pg import IndexStats, to_rows, update_pg_index
from src.tests.pg_testutil import PG_TEST_DSN, SKIP_REASON, TempDatabase


def _doc(chunk_id: str, text: str, **meta: object) -> Document:
    return Document(page_content=text, metadata={"_chroma_id": chunk_id, **meta})


def _corpus() -> list[Document]:
    return [
        _doc("s1", "mastery learning", type="transcript_segment", episode_id="S2E001", title="Ep 1"),
        _doc("s2", "two hour learning", type="transcript_segment", episode_id="S2E001", title="Ep 1"),
        _doc("a1", "an article", type="article_text", doc_id="post-1", published_at=None),
        _doc("o1", "overview", type="series_overview", group_title="AI tutors"),
    ]


class FakeEmbedder:
    """Deterministic 1536-d vectors; records every batch it is asked to embed."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        out = []
        for text in texts:
            vec = [0.0] * 1536
            vec[sum(map(ord, text)) % 1536] = 1.0
            out.append(vec)
        return out


class TestToRows(unittest.TestCase):
    def test_drops_row_id_and_none_values_and_dedupes(self) -> None:
        stats = IndexStats()
        docs = _corpus() + [_doc("s1", "mastery learning, again", type="transcript_segment")]
        ids, texts, metas = to_rows(docs, stats)
        self.assertEqual(ids, ["s1", "s2", "a1", "o1"])
        self.assertEqual(stats.duplicates, 1)
        self.assertEqual(texts[0], "mastery learning, again")  # the last artifact wins
        self.assertEqual(metas[2], {"type": "article_text", "doc_id": "post-1"})  # no _chroma_id, no None
        self.assertNotIn("_chroma_id", metas[0])

    def test_missing_row_id_fails_loudly(self) -> None:
        with self.assertRaises(ValueError):
            to_rows([Document(page_content="x", metadata={"type": "article_text"})], IndexStats())


class TestPipelinesIndexPostgres(unittest.TestCase):
    def setUp(self) -> None:
        p = patch.dict(os.environ, {"PG_DSN": "postgresql://unused"})
        p.start()
        self.addCleanup(p.stop)

    def test_audio_indexes_even_with_no_new_transcripts(self) -> None:
        from src.pipeline.audio import run as audio_run

        quiet_day = SimpleNamespace(mode="daily", new_transcripts_created=0, transcribe_failed=0)
        with patch("src.pipeline.audio.process_all.main", return_value=quiet_day), \
                patch("src.pipeline.index_pg.update_pg_index") as index:
            audio_run.main(["--skip-summaries", "--skip-neo4j", "--", "--mode", "daily"])
        index.assert_called_once_with(include_audio=True, include_articles=False, include_youtube=False,
                                      prune_stale=False)
        with patch("src.pipeline.index_pg.update_pg_index") as index:
            audio_run.main(["--skip-processing", "--skip-summaries", "--skip-neo4j", "--skip-chroma"])
        index.assert_not_called()  # the old flag name still skips indexing

    def test_pipelines_refuse_to_start_without_postgres(self) -> None:
        from src.pipeline.audio import run as audio_run
        from src.pipeline.substack import run as substack_run

        with patch.dict(os.environ, {"PG_DSN": ""}), \
                patch("src.pipeline.audio.process_all.main", side_effect=AssertionError("paid work ran")), \
                patch("src.pipeline.substack.process_all.main", side_effect=AssertionError("paid work ran")):
            with self.assertRaisesRegex(RuntimeError, "PG_DSN is not set"):
                audio_run.main(["--skip-neo4j", "--", "--mode", "daily"])
            with self.assertRaisesRegex(RuntimeError, "PG_DSN is not set"):
                substack_run.main(["--skip-neo4j", "--", "--mode", "daily"])
            with patch("src.pipeline.index_pg.update_pg_index") as index:
                substack_run.main(["--skip-processing", "--skip-neo4j", "--skip-index"])  # opting out still works
            index.assert_not_called()

    def test_substack_and_youtube_index_their_own_types(self) -> None:
        from src.pipeline.substack import run as substack_run
        from src.pipeline.youtube.run import index_youtube_only

        with patch("src.pipeline.index_pg.update_pg_index") as index:
            substack_run.main(["--skip-processing", "--skip-neo4j"])
            index_youtube_only(prune_stale=True)
        self.assertEqual(index.call_args_list[0].kwargs,
                         {"include_audio": False, "include_articles": True, "include_youtube": False})
        self.assertEqual(index.call_args_list[1].kwargs,
                         {"include_audio": False, "include_articles": False, "include_youtube": True,
                          "prune_stale": True})


@unittest.skipUnless(PG_TEST_DSN, SKIP_REASON)
class TestUpdatePgIndex(unittest.TestCase):
    def test_embeds_only_new_or_changed_rows(self) -> None:
        from src.database import pg_store

        with TempDatabase() as db:
            pg_store.migrate(db.dsn)
            embed = FakeEmbedder()
            first = update_pg_index(dsn=db.dsn, embed=embed, documents=_corpus())
            self.assertEqual((first.unique, first.embedded, first.chunks_written), (4, 4, 4))

            second = update_pg_index(dsn=db.dsn, embed=embed, documents=_corpus())
            self.assertEqual((second.unchanged, second.embedded, second.chunks_written), (4, 0, 0))
            self.assertEqual(len(embed.calls), 1)  # the rerun embedded nothing

            changed = _corpus()
            changed[1] = _doc("s2", "two hour learning, revised", type="transcript_segment",
                              episode_id="S2E001", title="Ep 1")
            third = update_pg_index(dsn=db.dsn, embed=embed, documents=changed)
            self.assertEqual((third.embedded, embed.calls[-1]), (1, ["two hour learning, revised"]))

            dry = update_pg_index(dsn=db.dsn, embed=embed, documents=changed + [
                _doc("s3", "new segment", type="transcript_segment", episode_id="S2E002", title="Ep 2")
            ], dry_run=True)
            self.assertEqual((dry.embedded, dry.chunks_written, len(embed.calls)), (1, 0, 2))

            with pg_store.session(db.dsn) as conn:
                docs = dict(conn.execute("SELECT doc_id, source_type FROM documents").fetchall())
                meta_row = conn.execute("SELECT metadata FROM chunks WHERE id = 'a1'").fetchone()
                nearest_row = conn.execute(
                    "SELECT id FROM chunks ORDER BY embedding <=> (SELECT embedding FROM chunks WHERE id = 's1') LIMIT 1"
                ).fetchone()
            assert meta_row is not None and nearest_row is not None
            meta, nearest = meta_row[0], nearest_row[0]
            episode = f"podcast:{episode_key('S2E001', 'Ep 1')}"
            self.assertEqual(docs, {episode: "podcast", "post-1": None, "series:AI tutors": "podcast_series"})
            self.assertEqual(meta, {"type": "article_text", "doc_id": "post-1"})
            self.assertEqual(nearest, "s1")

    def test_failed_batch_keeps_earlier_batches_and_never_prunes(self) -> None:
        from src.database import pg_store

        class FailsSecondBatch(FakeEmbedder):
            def __call__(self, texts: list[str]) -> list[list[float]]:
                if self.calls:
                    raise RuntimeError("embedding API down")
                return super().__call__(texts)

        with TempDatabase() as db:
            pg_store.migrate(db.dsn)
            docs = [_doc(f"s{i}", f"segment {i}", type="transcript_segment", episode_id="E1", title="Ep")
                    for i in range(150)]
            with self.assertRaisesRegex(RuntimeError, "embedding API down"):
                update_pg_index(dsn=db.dsn, embed=FailsSecondBatch(), documents=docs, prune_stale=True)
            with pg_store.session(db.dsn) as conn:
                row = conn.execute("SELECT count(*) FROM chunks").fetchone()
            assert row is not None
            self.assertEqual(row[0], 100)  # the first batch of 100 stayed committed

            resumed = update_pg_index(dsn=db.dsn, embed=FakeEmbedder(), documents=docs)
            self.assertEqual((resumed.unchanged, resumed.embedded), (100, 50))

    def test_prune_is_opt_in_guarded_and_refused_on_unreadable_artifacts(self) -> None:
        from src.database import pg_store

        with TempDatabase() as db:
            pg_store.migrate(db.dsn)
            embed = FakeEmbedder()
            update_pg_index(dsn=db.dsn, embed=embed, documents=_corpus())
            without_s2 = [d for d in _corpus() if d.metadata["_chroma_id"] != "s2"]

            kept = update_pg_index(dsn=db.dsn, embed=embed, documents=without_s2)
            self.assertEqual(kept.chunks_pruned, 0)  # no prune unless asked

            unreadable = update_pg_index(dsn=db.dsn, embed=embed, documents=without_s2, prune_stale=True,
                                         unreadable=["segmented_transcripts/broken.json"])
            self.assertEqual(unreadable.chunks_pruned, 0)
            self.assertIn("could not be read", unreadable.prune_skipped or "")

            # 1 of 2 transcript segments is over the default 10% limit.
            held = update_pg_index(dsn=db.dsn, embed=embed, documents=without_s2, prune_stale=True)
            self.assertEqual(held.chunks_pruned, 0)
            self.assertIn("would delete 1 of 2 transcript_segment", held.prune_skipped or "")

            pruned = update_pg_index(dsn=db.dsn, embed=embed, documents=without_s2, prune_stale=True,
                                     max_prune_fraction=0.5, include_articles=False)
            self.assertEqual((pruned.chunks_pruned, pruned.prune_skipped), (1, None))
            with pg_store.session(db.dsn) as conn:
                ids = sorted(r[0] for r in conn.execute("SELECT id FROM chunks").fetchall())
            self.assertEqual(ids, ["a1", "o1", "s1"])  # only the stale segment went


if __name__ == "__main__":
    unittest.main()
