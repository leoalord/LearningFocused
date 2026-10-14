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
                conn.execute("INSERT INTO documents (doc_id, title) VALUES ('d1', 'Doc')")
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

    def test_search_path_survives_rollback(self) -> None:
        from src.database import pg_store

        with TempDatabase() as db:
            pg_store.migrate(db.dsn)
            with pg_store.connect(db.dsn) as conn:
                conn.execute("SELECT 1")
                conn.rollback()
                row = conn.execute(
                    "SELECT * FROM cypher('knowledge', $$ RETURN 1 $$) AS (n agtype)"
                ).fetchone()
            self.assertEqual(str(row[0]), "1")

    def test_updated_at_moves_on_update(self) -> None:
        from src.database import pg_store

        with TempDatabase() as db:
            pg_store.migrate(db.dsn)
            with pg_store.connect(db.dsn, autocommit=True) as conn:
                conn.execute("INSERT INTO documents (doc_id) VALUES ('d1')")
                before = conn.execute("SELECT updated_at FROM documents").fetchone()[0]
                conn.execute("UPDATE documents SET title = 'x'")
                after = conn.execute("SELECT updated_at FROM documents").fetchone()[0]
            self.assertGreater(after, before)

    def test_app_role_is_not_superuser_and_can_write_graph(self) -> None:
        import uuid

        import psycopg

        from src.database import pg_store
        from src.tests.pg_testutil import with_credentials

        role = f"lf_app_{uuid.uuid4().hex[:8]}"
        with TempDatabase() as db:
            try:
                pg_store.migrate(db.dsn, app_role=role, app_password="pw-test")
                pg_store.migrate(db.dsn, app_role=role, app_password="pw-test")  # idempotent
                app_dsn = with_credentials(db.dsn, role, "pw-test")
                with pg_store.session(app_dsn) as conn:
                    superuser = conn.execute(
                        "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
                    ).fetchone()[0]
                    conn.execute("INSERT INTO documents (doc_id) VALUES ('d1')")
                    # A brand-new label and edge type: AGE creates child tables for them.
                    conn.execute(
                        "SELECT * FROM cypher('knowledge', $$ CREATE (:Person {id: 'a'})-[:MENTIONS]->(:Topic {id: 't'}) $$) AS (v agtype)"
                    )
                    edges = conn.execute(
                        "SELECT * FROM cypher('knowledge', $$ MATCH ()-[r:MENTIONS]->() RETURN count(r) $$) AS (n agtype)"
                    ).fetchone()[0]
                self.assertFalse(superuser)
                self.assertEqual(str(edges), "1")
                with self.assertRaises(ValueError):
                    pg_store.migrate(db.dsn, app_role=role)
            finally:
                with psycopg.connect(db.dsn, autocommit=True) as admin:
                    admin.execute(f'REASSIGN OWNED BY "{role}" TO CURRENT_USER')
                    admin.execute(f'DROP OWNED BY "{role}"')
                with psycopg.connect(PG_TEST_DSN or "", autocommit=True) as admin:
                    admin.execute(f'DROP ROLE IF EXISTS "{role}"')


if __name__ == "__main__":
    unittest.main()
