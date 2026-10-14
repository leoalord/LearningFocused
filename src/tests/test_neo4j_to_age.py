"""Neo4j -> AGE import: label mapping, batching, idempotent re-runs (no Neo4j needed)."""

from __future__ import annotations

import unittest

from src.database.neo4j_to_age import (
    EdgeRow,
    NodeRow,
    age_name,
    name_collisions,
    node_properties,
    primary_label,
)
from src.tests.pg_testutil import PG_TEST_DSN, SKIP_REASON, TempDatabase


class TestMapping(unittest.TestCase):
    def test_labels(self) -> None:
        self.assertEqual(primary_label(["__Entity__", "Person"]), "Person")
        self.assertEqual(primary_label(["__Entity__"]), "Entity")
        self.assertEqual(age_name("HAS-TOPIC", "R"), "HAS_TOPIC")
        self.assertEqual(age_name("__x", "R"), "x")

    def test_name_collisions_are_caught_before_import(self) -> None:
        self.assertEqual(name_collisions(["Person", "__Entity__", "IngestedDoc"], ["KNOWS"]), [])
        problems = name_collisions(["Has-Topic", "Has_Topic", "Person"], ["PERSON_OF", "Person"])
        self.assertEqual(len(problems), 2)
        self.assertIn("'Has_Topic'", problems[0])
        self.assertIn("'Person'", problems[1])

    def test_properties_keep_labels_and_drop_embeddings(self) -> None:
        import json

        props = json.loads(node_properties(
            NodeRow("7", ["Document"], {"text": "t", "embedding": [0.1], "score": float("nan")}),
            ("embedding",),
        ))
        self.assertEqual(props, {"text": "t", "score": None, "labels": ["Document"], "neo4j_id": "7"})


def _graph():
    nodes = [NodeRow(str(i), ["__Entity__", "Person" if i % 2 else "Topic"], {"id": f"n{i}"}) for i in range(7)]
    nodes.append(NodeRow("100", ["Document"], {"id": "doc", "text": "transcript"}))
    edges = [EdgeRow(str(i), "MENTIONS", "100", str(i), {}) for i in range(7)]
    edges.append(EdgeRow("50", "KNOWS", "1", "3", {"since": 2020}))
    edges.append(EdgeRow("51", "KNOWS", "1", "999", {}))  # endpoint not in the graph
    return nodes, edges


@unittest.skipUnless(PG_TEST_DSN, SKIP_REASON)
class TestImport(unittest.TestCase):
    def test_import_is_complete_and_rerunnable(self) -> None:
        from src.database import pg_store
        from src.database.neo4j_to_age import import_graph

        with TempDatabase() as db:
            pg_store.migrate(db.dsn)
            nodes, edges = _graph()
            first = import_graph(iter(nodes), iter(edges), dsn=db.dsn, batch_size=3)
            self.assertEqual((first.nodes_written, first.edges_written), (8, 8))
            self.assertEqual(first.edges_missing_endpoint, 1)
            again = import_graph(iter(nodes), iter(edges), dsn=db.dsn, batch_size=4)
            self.assertEqual((again.nodes_written, again.edges_written), (0, 0))

            with pg_store.session(db.dsn) as conn:
                mentions = conn.execute(
                    "SELECT * FROM cypher('knowledge', $$ MATCH (:Document)-[r:MENTIONS]->(t) RETURN count(r) $$) AS (n agtype)"
                ).fetchone()[0]
                knows = conn.execute(
                    "SELECT * FROM cypher('knowledge', $$ MATCH (a:Person {id: 'n1'})-[r:KNOWS]->(b) RETURN b.id, r.since $$) AS (b agtype, s agtype)"
                ).fetchone()
            self.assertEqual(str(mentions), "7")
            self.assertEqual((str(knows[0]), str(knows[1])), ('"n3"', "2020"))

    def test_app_role_can_extend_imported_graph(self) -> None:
        import uuid

        import psycopg

        from src.database import pg_store
        from src.database.neo4j_to_age import import_graph
        from src.tests.pg_testutil import with_credentials

        role = f"lf_app_{uuid.uuid4().hex[:8]}"
        with TempDatabase() as db:
            try:
                pg_store.migrate(db.dsn, app_role=role, app_password="pw")
                nodes, edges = _graph()
                import_graph(iter(nodes), iter(edges), dsn=db.dsn)
                with pg_store.session(db.dsn) as conn:
                    pg_store.grant_graph_ownership(conn, role)
                with pg_store.session(with_credentials(db.dsn, role, "pw")) as conn:
                    conn.execute(
                        "SELECT * FROM cypher('knowledge', $$ MATCH (p:Person {id: 'n1'}) CREATE (p)-[:WROTE]->(:Book {id: 'b'}) $$) AS (v agtype)"
                    )
            finally:
                with psycopg.connect(db.dsn, autocommit=True) as admin:
                    admin.execute(f'REASSIGN OWNED BY "{role}" TO CURRENT_USER')
                    admin.execute(f'DROP OWNED BY "{role}"')
                with psycopg.connect(PG_TEST_DSN or "", autocommit=True) as admin:
                    admin.execute(f'DROP ROLE IF EXISTS "{role}"')


if __name__ == "__main__":
    unittest.main()
