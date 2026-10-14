"""Chroma -> Postgres backfill/dual-write: reuse embeddings, idempotent, prune on request."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone

from src.database.pg_sync import derive_document, parse_published
from src.tests.pg_testutil import PG_TEST_DSN, SKIP_REASON, TempDatabase


class TestDerive(unittest.TestCase):
    def test_parse_published_formats(self) -> None:
        utc = timezone.utc
        self.assertEqual(parse_published("20250102"), datetime(2025, 1, 2, tzinfo=utc))
        self.assertEqual(parse_published("2025-01-02T03:04:05Z"), datetime(2025, 1, 2, 3, 4, 5, tzinfo=utc))
        self.assertEqual(parse_published("Mon, 01 Jan 2024 00:00:00 GMT"), datetime(2024, 1, 1, tzinfo=utc))
        self.assertIsNone(parse_published("not a date"))
        self.assertIsNone(parse_published(None))

    def test_documents_for_each_source(self) -> None:
        self.assertEqual(derive_document({"episode_id": "S2E001", "title": "Hi"})["doc_id"], "S2E001")
        self.assertEqual(derive_document({"episode_id": "S2E001"})["source_type"], "podcast")
        series = derive_document({"type": "series_overview", "group_title": "AI tutors"})
        self.assertEqual((series["doc_id"], series["title"]), ("series:AI tutors", "AI tutors"))
        yt = derive_document({"doc_id": "abc", "url": "https://y/abc", "upload_date": "20250102"})
        self.assertEqual(yt["url"], "https://y/abc")
        self.assertIsNotNone(yt["published_at"])
        self.assertIsNone(derive_document({"type": "orphan"}))


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
    def test_backfill_is_idempotent_and_prunes_on_request(self) -> None:
        from src.database import pg_store
        from src.database.pg_sync import sync_from_chroma

        with tempfile.TemporaryDirectory() as tmp, TempDatabase() as db:
            pg_store.migrate(db.dsn)
            col = _collection(tmp)
            first = sync_from_chroma(db.dsn, collection=col, batch_size=3)
            self.assertEqual((first.chroma_rows, first.chunks_written), (4, 4))
            second = sync_from_chroma(db.dsn, collection=col, batch_size=3)
            self.assertEqual((second.chunks_written, second.chunks_unchanged), (0, 4))

            col.update(ids=["s2"], documents=["two hour learning, revised"], embeddings=[[0.0, 1.0] + [0.0] * 1534])
            third = sync_from_chroma(db.dsn, collection=col)
            self.assertEqual(third.chunks_written, 1)

            with pg_store.session(db.dsn) as conn:
                docs = dict(conn.execute("SELECT doc_id, source_type FROM documents").fetchall())
                nearest = conn.execute(
                    "SELECT id FROM chunks ORDER BY embedding <=> (SELECT embedding FROM chunks WHERE id = 's1') LIMIT 1"
                ).fetchone()[0]
            self.assertEqual(docs, {"S2E001": "podcast", "post-1": None, "series:AI tutors": "podcast_series"})
            self.assertEqual(nearest, "s1")

            col.delete(ids=["a1"])
            kept = sync_from_chroma(db.dsn, collection=col)
            self.assertEqual(kept.chunks_pruned, 0)
            pruned = sync_from_chroma(db.dsn, collection=col, prune=True)
            self.assertEqual(pruned.chunks_pruned, 1)


if __name__ == "__main__":
    unittest.main()
