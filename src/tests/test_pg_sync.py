"""Chroma -> Postgres backfill/dual-write: reuse embeddings, idempotent, guarded prune."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone

from src.database.pg_sync import _merge_documents, derive_document, parse_published
from src.pipeline.audio.episode_ids import episode_key
from src.tests.pg_testutil import PG_TEST_DSN, SKIP_REASON, TempDatabase


class TestDerive(unittest.TestCase):
    def test_parse_published_formats(self) -> None:
        utc = timezone.utc
        self.assertEqual(parse_published("20250102"), datetime(2025, 1, 2, tzinfo=utc))
        self.assertEqual(parse_published("2025-01-02T03:04:05Z"), datetime(2025, 1, 2, 3, 4, 5, tzinfo=utc))
        self.assertEqual(parse_published("Mon, 01 Jan 2024 00:00:00 GMT"), datetime(2024, 1, 1, tzinfo=utc))
        self.assertIsNone(parse_published("not a date"))
        self.assertIsNone(parse_published("20251399"))  # 8 digits, not a date: no exception
        self.assertIsNone(parse_published(None))

    def test_documents_for_each_source(self) -> None:
        ep = derive_document({"episode_id": "S2E001", "title": "Hi"})
        self.assertEqual(ep["doc_id"], f"podcast:{episode_key('S2E001', 'Hi')}")
        self.assertEqual((ep["source_type"], ep["metadata"]), ("podcast", {"episode_id": "S2E001"}))
        # The feed reuses episode numbers: same id, different title -> different document.
        self.assertNotEqual(derive_document({"episode_id": "S2E001", "title": "Other"})["doc_id"], ep["doc_id"])
        self.assertEqual(derive_document({"episode_id": "S2E001"})["doc_id"], "podcast:S2E001")
        unknown = derive_document({"episode_id": "Unknown ID", "title": "Intro"})
        self.assertEqual((unknown["doc_id"], unknown["metadata"]), (f"podcast:{episode_key('unknown', 'Intro')}", {}))
        self.assertIsNone(derive_document({"episode_id": "Unknown ID"}))
        self.assertIsNone(derive_document({"doc_id": "unknown", "title": "Unknown Article"}))
        series = derive_document({"type": "series_overview", "group_title": "AI tutors"})
        self.assertEqual((series["doc_id"], series["title"]), ("series:AI tutors", "AI tutors"))
        yt = derive_document({"doc_id": "abc", "url": "https://y/abc", "upload_date": "20250102"})
        self.assertEqual(yt["url"], "https://y/abc")
        self.assertIsNotNone(yt["published_at"])
        self.assertIsNone(derive_document({"type": "orphan"}))

    def test_placeholder_title_never_wins(self) -> None:
        merged = _merge_documents([
            {"doc_id": "post-1", "title": "Unknown Article"},
            {"doc_id": "post-1", "title": "Real title"},
        ])
        self.assertEqual([d["title"] for d in merged], ["Real title"])


def _collection(tmp: str):
    import chromadb

    client = chromadb.PersistentClient(path=tmp)
    col = client.create_collection("lf_test")
    vec = [0.0] * 1536
    col.add(
        ids=["s1", "s2", "a1", "o1"],
        documents=["mastery learning", "two hour learning", "an article", "overview"],
        metadatas=[
            {"type": "transcript_segment", "episode_id": "S2E001", "title": "Ep 1"},
            {"type": "transcript_segment", "episode_id": "S2E001", "title": "Ep 1"},
            {"type": "article_text", "doc_id": "post-1", "published_at": "2025-03-01T00:00:00Z"},
            {"type": "series_overview", "group_title": "AI tutors"},
        ],
        embeddings=[[1.0] + vec[1:], [0.0, 1.0] + vec[2:], vec[:1535] + [1.0], [0.5] * 1536],
    )
    return col


@unittest.skipUnless(PG_TEST_DSN, SKIP_REASON)
class TestSyncFromChroma(unittest.TestCase):
    def test_backfill_is_idempotent_and_prunes_with_a_guard(self) -> None:
        from src.database import pg_store
        from src.database.pg_sync import sync_from_chroma

        with tempfile.TemporaryDirectory() as tmp, TempDatabase() as db:
            pg_store.migrate(db.dsn)
            col = _collection(tmp)
            first = sync_from_chroma(db.dsn, collection=col, batch_size=3)
            self.assertEqual((first.chroma_rows, first.chunks_written), (4, 4))
            vector_reads: list[list[str]] = []
            real_get = col.get

            def counting_get(*args, **kwargs):
                if kwargs.get("ids") is not None:
                    vector_reads.append(list(kwargs["ids"]))
                return real_get(*args, **kwargs)

            col.get = counting_get  # type: ignore[method-assign]
            second = sync_from_chroma(db.dsn, collection=col, batch_size=3)
            self.assertEqual((second.chunks_written, second.chunks_unchanged), (0, 4))
            self.assertEqual(vector_reads, [])  # unchanged rows never fetch their vectors

            col.update(ids=["s2"], documents=["two hour learning, revised"], embeddings=[[0.0, 1.0] + [0.0] * 1534])
            third = sync_from_chroma(db.dsn, collection=col)
            self.assertEqual((third.chunks_written, vector_reads), (1, [["s2"]]))

            with pg_store.session(db.dsn) as conn:
                docs = dict(conn.execute("SELECT doc_id, source_type FROM documents").fetchall())
                nearest = conn.execute(
                    "SELECT id FROM chunks ORDER BY embedding <=> (SELECT embedding FROM chunks WHERE id = 's1') LIMIT 1"
                ).fetchone()[0]
            episode = f"podcast:{episode_key('S2E001', 'Ep 1')}"
            self.assertEqual(docs, {episode: "podcast", "post-1": None, "series:AI tutors": "podcast_series"})
            self.assertEqual(nearest, "s1")

            col.delete(ids=["a1"])
            kept = sync_from_chroma(db.dsn, collection=col)
            self.assertEqual(kept.chunks_pruned, 0)
            # 1 of 4 chunks is over the default 10% limit, so the prune is held back.
            held = sync_from_chroma(db.dsn, collection=col, prune=True)
            self.assertEqual(held.chunks_pruned, 0)
            self.assertIn("would delete 1 of 4", held.prune_skipped or "")
            pruned = sync_from_chroma(db.dsn, collection=col, prune=True, max_prune_fraction=0.5)
            self.assertEqual((pruned.chunks_pruned, pruned.documents_pruned, pruned.prune_skipped), (1, 1, None))


if __name__ == "__main__":
    unittest.main()
