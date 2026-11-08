"""Graph extraction straight into the Postgres AGE graph (no Neo4j, no real LLM).

The Postgres tests skip without PG_TEST_DSN (see src/tests/pg_testutil.py).
"""

from __future__ import annotations

import os
import unittest
import uuid
from typing import Any
from unittest.mock import patch

from langchain_community.graphs.graph_document import GraphDocument, Node, Relationship
from langchain_core.documents import Document

from src.database.age_graph import name_lc, relationship_label
from src.tests.pg_testutil import PG_TEST_DSN, SKIP_REASON, TempDatabase, with_credentials

NEO4J_ENV = {"NEO4J_URI": "", "NEO4J_USERNAME": "", "NEO4J_PASSWORD": ""}


def _gd(text: str, nodes: list[tuple[str, str]], rels: list[tuple[str, str, str, str, str]],
        metadata: dict[str, Any] | None = None) -> GraphDocument:
    """nodes: (id, type); rels: (source id, source type, type, target id, target type)."""
    return GraphDocument(
        nodes=[Node(id=i, type=t) for i, t in nodes],
        relationships=[Relationship(source=Node(id=s, type=st), target=Node(id=o, type=ot), type=r)
                       for s, st, r, o, ot in rels],
        source=Document(page_content=text, metadata=dict(metadata or {})),
    )


def _count(conn: Any, pattern: str) -> int:
    row = conn.execute(
        f"SELECT * FROM ag_catalog.cypher('knowledge', $$ MATCH {pattern} RETURN count(*) $$) AS (n ag_catalog.agtype)"
    ).fetchone()
    return int(str(row[0]))


def _vertex(conn: Any, entity_id: str) -> tuple[str, dict[str, Any]]:
    from src.database.pg_search import parse_agtype

    rows = conn.execute(
        "SELECT * FROM ag_catalog.cypher('knowledge', $$ MATCH (n) WHERE n.id = $id RETURN label(n), properties(n) $$, "
        "%s) AS (l ag_catalog.agtype, p ag_catalog.agtype)",
        (f'{{"id": "{entity_id}"}}',),
    ).fetchall()
    assert len(rows) == 1, f"{entity_id}: {len(rows)} vertices"
    return parse_agtype(rows[0][0]), parse_agtype(rows[0][1])


class TestNaming(unittest.TestCase):
    def test_relationship_labels_follow_neo4j_normalization(self) -> None:
        self.assertEqual(relationship_label("advocates for"), "ADVOCATES_FOR")
        self.assertEqual(relationship_label("`EVIDENCE-FOR`"), "EVIDENCE_FOR")
        self.assertEqual(relationship_label(""), "RELATED_TO")

    def test_name_lc(self) -> None:
        self.assertEqual(name_lc("Alpha School"), "alpha school")
        self.assertEqual(name_lc(42), "42")
        self.assertIsNone(name_lc(None))


@unittest.skipUnless(PG_TEST_DSN, SKIP_REASON)
class TestAgeGraphWriter(unittest.TestCase):
    def test_write_matches_imported_shape_and_merges_by_id(self) -> None:
        from src.database import pg_search, pg_store
        from src.database.age_graph import AgeGraphWriter

        doc1 = _gd("Alice founded Alpha School.", [("Alice", "Person"), ("Alpha School", "Organization")],
                   [("Alice", "Person", "FOUNDED", "Alpha School", "Organization")], {"title": "Ep 1", "episode_id": "e1"})
        doc2 = _gd("Alice backs Bob; Carol agrees.", [("Alice", "Organization"), ("Bob", "Person")],
                   [("Alice", "Organization", "advocates for", "Bob", "Person"),
                    ("Carol", "Person", "SUPPORTS", "Bob", "Person")])
        doc1.relationships[0].properties = {"source_doc_id": "first", "ingest_run_id": "run-1"}

        with TempDatabase() as db:
            pg_store.migrate(db.dsn)
            with pg_store.session(db.dsn) as conn:
                stats = AgeGraphWriter(conn).write([doc1, doc2])
            self.assertEqual((stats.documents_created, stats.entities_created), (2, 4))
            self.assertEqual((stats.mentions_created, stats.edges_created), (4, 3))

            with pg_store.session(db.dsn) as conn:
                label, alice = _vertex(conn, "Alice")
                self.assertEqual(label, "Person")  # first type written; AGE keeps one label
                self.assertEqual(alice["labels"], ["__Entity__", "Person", "Organization"])
                self.assertEqual(alice["name_lc"], "alice")
                # A relationship endpoint gets a vertex but no MENTIONS edge, like Neo4j.
                carol_label, carol = _vertex(conn, "Carol")
                self.assertEqual((carol_label, carol["labels"]), ("Person", ["__Entity__", "Person"]))
                self.assertEqual(_count(conn, "(:Document)-[:MENTIONS]->({id: 'Carol'})"), 0)
                self.assertEqual(_count(conn, "(:Document)-[:MENTIONS]->()"), 4)
                self.assertEqual(_count(conn, "({id: 'Alice'})-[:ADVOCATES_FOR]->({id: 'Bob'})"), 1)

            # Readers see it exactly as they see a graph copied from Neo4j.
            mentions = pg_search.graph_query("mentions", entity="alice", dsn=db.dsn)["rows"]
            self.assertEqual(sorted(r["properties"]["text"] for r in mentions),
                             ["Alice backs Bob; Carol agrees.", "Alice founded Alpha School."])
            self.assertIn("Ep 1", {r["properties"].get("title") for r in mentions})
            neighbors = pg_search.graph_query("neighbors", entity="alpha school", dsn=db.dsn)["rows"]
            self.assertIn(("FOUNDED", "Alice"), {(r["relationship"], r["other"]["name"]) for r in neighbors})
            schema = {r["name"]: r["count"] for r in pg_search.graph_query("schema", dsn=db.dsn)["rows"]}
            self.assertEqual((schema["Document"], schema["MENTIONS"], schema["Person"]), (2, 4, 3))

            # Re-writing adds nothing, and edge properties are kept from creation.
            again = _gd("Alice founded Alpha School.", [("Alice", "Person"), ("Alpha School", "Organization")],
                        [("Alice", "Person", "FOUNDED", "Alpha School", "Organization")])
            again.relationships[0].properties = {"source_doc_id": "second", "ingest_run_id": "run-2"}
            with pg_store.session(db.dsn) as conn:
                stats = AgeGraphWriter(conn).write([again])
                self.assertEqual((stats.documents_created, stats.entities_created, stats.edges_created,
                                  stats.mentions_created, stats.edges_existing), (0, 0, 0, 0, 1))
                row = conn.execute(
                    "SELECT * FROM ag_catalog.cypher('knowledge', $$ MATCH ()-[r:FOUNDED]->() "
                    "RETURN r.source_doc_id, r.ingest_run_id $$) AS (d ag_catalog.agtype, r ag_catalog.agtype)"
                ).fetchone()
            assert row is not None
            self.assertEqual((str(row[0]), str(row[1])), ('"first"', '"run-1"'))

    def test_merges_into_a_graph_copied_from_neo4j(self) -> None:
        from src.database import pg_store
        from src.database.age_graph import AgeGraphWriter
        from src.database.neo4j_to_age import NodeRow, import_graph

        with TempDatabase() as db:
            pg_store.migrate(db.dsn)
            import_graph(iter([NodeRow("1", ["__Entity__", "Person"], {"id": "MacKenzie Price"})]), iter([]),
                         dsn=db.dsn)
            gd = _gd("MacKenzie Price founded 2 Hour Learning.",
                     [("MacKenzie Price", "Person"), ("2 Hour Learning", "Organization")],
                     [("MacKenzie Price", "Person", "FOUNDED", "2 Hour Learning", "Organization")])
            with pg_store.session(db.dsn) as conn:
                stats = AgeGraphWriter(conn).write([gd])
                self.assertEqual((stats.entities_created, stats.entities_updated), (1, 0))
                _label, props = _vertex(conn, "MacKenzie Price")
                self.assertEqual(props["neo4j_id"], "1")  # the imported vertex, not a copy
                self.assertEqual(_count(conn, "({id: 'MacKenzie Price'})-[:FOUNDED]->()"), 1)

    def test_app_role_can_write_new_labels(self) -> None:
        from src.database import pg_store
        from src.database.age_graph import AgeGraphWriter

        password = uuid.uuid4().hex
        role = f"lf_app_{uuid.uuid4().hex[:8]}"
        with TempDatabase() as db:
            try:
                pg_store.migrate(db.dsn, app_role=role, app_password=password)
                app_dsn = with_credentials(db.dsn, role, password)
                gd = _gd("Khanmigo is a tool.", [("Khanmigo", "Tool")], [])
                with pg_store.session(app_dsn) as conn:
                    AgeGraphWriter(conn).write([gd])
                    conn.execute("INSERT INTO ingested_docs (source_key, content_hash, status) VALUES ('k', 'h', 'completed')")
                with pg_store.session(app_dsn) as conn:
                    self.assertEqual(_count(conn, "(:Tool)"), 1)
            finally:
                import psycopg

                with psycopg.connect(str(PG_TEST_DSN), autocommit=True) as admin:
                    admin.execute(f'DROP DATABASE IF EXISTS "{db.name}" WITH (FORCE)')
                    admin.execute(f'DROP ROLE IF EXISTS "{role}"')


class FakeTransformer:
    """Stands in for LLMGraphTransformer: one graph document per passage, in order."""

    def __init__(self, model: str = "fake-model", fail: bool = False) -> None:
        self.llm = type("LLM", (), {"model": model})()
        self.fail = fail
        self.calls = 0

    def convert_to_graph_documents(self, docs: list[Document]) -> list[GraphDocument]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider down")
        out = []
        for d in docs:
            who = d.metadata["speaker"]
            out.append(GraphDocument(
                nodes=[Node(id=who, type="Person"), Node(id="Alpha School", type="Organization"),
                       Node(id="Austin", type="Place")],  # Place is off-vocabulary
                relationships=[
                    Relationship(source=Node(id=who, type="Person"), target=Node(id="Alpha School", type="Organization"),
                                 type="ADVOCATES_FOR"),
                    Relationship(source=Node(id=who, type="Person"), target=Node(id="Austin", type="Place"),
                                 type="ADVOCATES_FOR"),  # endpoint dropped
                    Relationship(source=Node(id=who, type="Person"), target=Node(id="Alpha School", type="Organization"),
                                 type="LIVES_NEAR"),  # off-vocabulary type
                ],
                source=d,
            ))
        return out


def _passages(n: int, prefix: str = "") -> list[Document]:
    return [
        Document(page_content=f"{prefix}Speaker {i} talks about Alpha School.",
                 metadata={"episode_id": f"ep{i}", "start_time": "00:00", "topic": "schools",
                           "title": f"Episode {i}", "speaker": f"Speaker {i}"})
        for i in range(n)
    ]


@unittest.skipUnless(PG_TEST_DSN, SKIP_REASON)
class TestUpdateKnowledgeGraph(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, NEO4J_ENV)
        env.start()
        self.addCleanup(env.stop)
        for key in NEO4J_ENV:
            os.environ.pop(key, None)

    def _run(self, dsn: str, docs: list[Document], transformer: FakeTransformer, **kwargs: Any) -> Any:
        from src.pipeline.index_graph import update_knowledge_graph

        return update_knowledge_graph(documents=docs, llm_transformers=[transformer], dsn=dsn,
                                      schema_profile="extended", batch_size=2, **kwargs)

    def test_end_to_end_with_bookkeeping_dedupe_and_cap(self) -> None:
        from src.database import pg_store

        with TempDatabase() as db:
            pg_store.migrate(db.dsn)
            fake = FakeTransformer()
            first = self._run(db.dsn, _passages(3), fake)
            self.assertTrue(first.ok)
            self.assertEqual((first.docs_seen, first.docs_processed, fake.calls), (3, 3, 2))
            self.assertEqual(first.extraction.dropped_node_types["Place"], 3)
            self.assertEqual(first.extraction.dropped_rel_types["LIVES_NEAR"], 3)
            self.assertEqual(first.extraction.rels_dropped_for_endpoint, 3)

            with pg_store.session(db.dsn) as conn:
                rows = conn.execute(
                    "SELECT source_key, status, extract_model, raw_nodes, kept_nodes, raw_rels, kept_rels, "
                    "rels_dropped_for_endpoint, dropped_node_types, dropped_rel_types, graph_doc_id, "
                    "last_ingest_run_id FROM ingested_docs ORDER BY source_key"
                ).fetchall()
                self.assertEqual(len(rows), 3)
                key, status, model, rn, kn, rr, kr, ep, dnt, drt, gid, run_id = rows[0]
                self.assertEqual((key, status, model), ("audio:ep0:00:00:schools", "completed", "fake-model"))
                self.assertEqual((rn, kn, rr, kr, ep), (3, 2, 3, 1, 1))
                self.assertEqual((dnt, drt), ({"Place": 1}, {"LIVES_NEAR": 1}))
                self.assertEqual(run_id, first.ingest_run_id)

                # Per-edge provenance points at the passage's Document vertex.
                prov = conn.execute(
                    "SELECT * FROM ag_catalog.cypher('knowledge', $$ MATCH (a {id: 'Speaker 0'})-[r:ADVOCATES_FOR]->(b) "
                    "MATCH (d:Document {id: r.source_doc_id})-[:MENTIONS]->(a) "
                    "RETURN r.extract_model, r.ingest_run_id, d.id, d.episode_id $$) "
                    "AS (m ag_catalog.agtype, r ag_catalog.agtype, d ag_catalog.agtype, e ag_catalog.agtype)"
                ).fetchall()
                self.assertEqual(len(prov), 1)
                self.assertEqual([str(v).strip('"') for v in prov[0]], ["fake-model", first.ingest_run_id, gid, "ep0"])
                self.assertEqual(_count(conn, "(:Place)"), 0)
                self.assertEqual(_count(conn, "()-[:LIVES_NEAR]->()"), 0)

            # Unchanged passages are not sent to the LLM again.
            again_fake = FakeTransformer()
            again = self._run(db.dsn, _passages(3), again_fake)
            self.assertEqual((again.skipped_completed, again.docs_processed, again_fake.calls), (3, 0, 0))

            # Changed text is a new version; max_docs defers the rest to a later run.
            capped_fake = FakeTransformer()
            capped = self._run(db.dsn, _passages(3, prefix="Updated: "), capped_fake, max_docs=1)
            self.assertEqual((capped.docs_processed, capped.deferred), (1, 2))
            with pg_store.session(db.dsn) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM ingested_docs").fetchall(), [(4,)])

    def test_failed_batches_are_recorded_and_retried(self) -> None:
        from src.database import pg_store

        with TempDatabase() as db:
            pg_store.migrate(db.dsn)
            failed = self._run(db.dsn, _passages(3), FakeTransformer(fail=True))
            self.assertFalse(failed.ok)
            self.assertEqual((failed.batches_failed, failed.docs_failed), (2, 3))
            with pg_store.session(db.dsn) as conn:
                rows = conn.execute("SELECT status, error FROM ingested_docs").fetchall()
                self.assertEqual({r[0] for r in rows}, {"failed"})
                self.assertIn("provider down", rows[0][1])
                self.assertEqual(_count(conn, "(n)"), 0)

            retry = self._run(db.dsn, _passages(3), FakeTransformer())
            self.assertTrue(retry.ok)
            self.assertEqual(retry.docs_processed, 3)

    def test_passages_already_in_graph_are_not_re_extracted(self) -> None:
        from src.database import pg_store
        from src.pipeline.index_graph import NOTE_ALREADY_IN_GRAPH, delete_audio_from_graph

        with TempDatabase() as db:
            pg_store.migrate(db.dsn)
            self._run(db.dsn, _passages(2), FakeTransformer())
            # As after a Neo4j copy: the graph has the Document vertices but no bookkeeping.
            with pg_store.session(db.dsn) as conn:
                conn.execute("DELETE FROM ingested_docs")
            fake = FakeTransformer()
            result = self._run(db.dsn, _passages(2), fake)
            self.assertEqual((result.skipped_in_graph, fake.calls), (2, 0))
            with pg_store.session(db.dsn) as conn:
                notes = {r[0] for r in conn.execute("SELECT note FROM ingested_docs WHERE status = 'completed'")}
            self.assertEqual(notes, {NOTE_ALREADY_IN_GRAPH})

            # Deleting audio passages lets them be extracted again; entities stay.
            deleted = delete_audio_from_graph(db.dsn)
            self.assertEqual(deleted, {"documents": 2, "ingested_docs": 2})
            with pg_store.session(db.dsn) as conn:
                self.assertEqual(_count(conn, "(:Document)"), 0)
                self.assertEqual(_count(conn, "(:Person)"), 2)
            redo = FakeTransformer()
            self.assertEqual(self._run(db.dsn, _passages(2), redo).docs_processed, 2)

    def test_needs_postgres_not_neo4j(self) -> None:
        from src.pipeline.index_graph import update_knowledge_graph

        with patch.dict(os.environ, {"PG_DSN": ""}):
            os.environ.pop("PG_DSN")
            with self.assertRaisesRegex(RuntimeError, "PG_DSN"):
                update_knowledge_graph(documents=_passages(1), llm_transformers=[FakeTransformer()])


if __name__ == "__main__":
    unittest.main()
