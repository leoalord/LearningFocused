"""Schema migration for the Postgres knowledge store (pgvector + AGE + full-text)."""

from __future__ import annotations

import unittest

from src.tests.pg_testutil import PG_TEST_DSN, SKIP_REASON, TempDatabase


@unittest.skipUnless(PG_TEST_DSN, SKIP_REASON)
class TestPgStoreMigrate(unittest.TestCase):
    def test_migrate_is_idempotent_and_creates_graph(self) -> None:
        from src.database import pg_store

        with TempDatabase() as db:
            first = pg_store.migrate(db.dsn)
            second = pg_store.migrate(db.dsn)
            self.assertTrue(first["graph_created"])
            self.assertFalse(second["graph_created"])
            info = pg_store.status(db.dsn)
            self.assertEqual(set(info["extensions"]), {"vector", "age"})
            self.assertEqual(info["counts"]["chunks"], 0)

    def test_chunk_round_trip_with_vector_and_tsv(self) -> None:
        from src.database import pg_store

        with TempDatabase() as db:
            pg_store.migrate(db.dsn)
            with pg_store.session(db.dsn) as conn:
                import numpy as np

                vec = np.zeros(pg_store.EMBEDDING_DIMS, dtype=np.float32)
                vec[0] = 1.0
                conn.execute(
                    "INSERT INTO chunks (id, doc_id, type, content, embedding) VALUES (%s, %s, %s, %s, %s)",
                    ("c1", "d1", "article_text", "Mastery learning with AI tutors", vec),
                )
                row = conn.execute(
                    "SELECT id, embedding <=> %s AS dist FROM chunks "
                    "WHERE tsv @@ websearch_to_tsquery('english', 'tutors')",
                    (vec,),
                ).fetchone()
            self.assertEqual(row[0], "c1")
            self.assertAlmostEqual(float(row[1]), 0.0, places=6)

    def test_age_cypher_runs(self) -> None:
        from src.database import pg_store

        with TempDatabase() as db:
            pg_store.migrate(db.dsn)
            with pg_store.session(db.dsn) as conn:
                conn.execute(
                    "SELECT * FROM cypher('knowledge', $$ CREATE (:Person {id: 'MacKenzie Price'}) $$) AS (v agtype)"
                )
                count = conn.execute(
                    "SELECT * FROM cypher('knowledge', $$ MATCH (p:Person) RETURN count(p) $$) AS (n agtype)"
                ).fetchone()[0]
            self.assertEqual(str(count), "1")


if __name__ == "__main__":
    unittest.main()
