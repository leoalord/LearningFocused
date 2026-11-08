"""Graph audit against the served AGE graph (needs PG_TEST_DSN)."""

from __future__ import annotations

import unittest

from evals.graph_audit.age import entity_types
from src.tests.pg_testutil import PG_TEST_DSN, SKIP_REASON, TempDatabase


class TestEntityTypes(unittest.TestCase):
    def test_uses_labels_property_minus_base(self) -> None:
        v = {"label": "Person", "properties": {"labels": ["__Entity__", "Person", "Organization"]}}
        self.assertEqual(entity_types(v), ["Person", "Organization"])

    def test_falls_back_to_age_label(self) -> None:
        self.assertEqual(entity_types({"label": "Tool", "properties": {}}), ["Tool"])


def _seed(dsn: str) -> None:
    from src.database import pg_store

    pg_store.migrate(dsn)
    preamble = (
        "You are extracting a knowledge graph from educational content.\n\nExtract:\n- Nodes\n\n"
        "Rules:\n- Only explicit.\n\nProvenance:\n- Title: Ep 7\n\n"
    )
    with pg_store.session(dsn) as conn:
        # Shape as neo4j_to_age writes it: one AGE label, all labels in `labels`.
        conn.execute(f"""SELECT * FROM ag_catalog.cypher('knowledge', $$
            CREATE (p:Person {{id: 'MacKenzie Price', name_lc: 'mackenzie price',
                               labels: ['__Entity__', 'Person']}})
                     -[:FOUNDED {{source_doc_id: 'doc1', extract_model: 'gemini-flash-latest',
                                  neo4j_type: 'FOUNDED'}}]->
                   (o:Organization {{id: 'Alpha School', name_lc: 'alpha school',
                                     labels: ['__Entity__', 'Organization', 'Concept']}}),
                   (d:Document {{id: 'doc1', text: '{preamble}MacKenzie Price founded Alpha School.',
                                 title: 'Ep 7', labels: ['Document']}}),
                   (d)-[:MENTIONS]->(o), (d)-[:MENTIONS]->(p),
                   (d2:Document {{id: 'doc2', text: 'Alpha School teaches Two Hour Learning.', title: 'Ep 8',
                                  labels: ['Document']}}),
                   (o)-[:DISCUSSED {{neo4j_type: 'DISCUSSED'}}]->
                   (c:Concept {{id: 'Two Hour Learning', name_lc: 'two hour learning',
                                labels: ['__Entity__', 'Concept']}}),
                   (d2)-[:MENTIONS]->(o), (d2)-[:MENTIONS]->(c)
            $$) AS (x ag_catalog.agtype)""")


@unittest.skipUnless(PG_TEST_DSN, SKIP_REASON)
class TestAgeAudit(unittest.TestCase):
    db: TempDatabase

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = TempDatabase().__enter__()
        cls.addClassCleanup(cls.db.__exit__, None, None, None)
        _seed(cls.db.dsn)

    def test_graph_shape(self) -> None:
        from evals.graph_audit.age import graph_shape
        from src.database.pg_search import read_only

        with read_only(self.db.dsn) as conn:
            shape = graph_shape(conn)
        self.assertEqual(shape["entities"], 3)
        self.assertEqual(shape["conflicted"], 1)
        self.assertEqual(shape["examples"][0]["name"], "Alpha School")
        self.assertEqual(shape["examples"][0]["types"], ["Organization", "Concept"])
        self.assertEqual(shape["examples"][0]["degree"], 4)  # FOUNDED, DISCUSSED, 2 MENTIONS
        rels = {r["type"]: r for r in shape["relationships"]}
        self.assertEqual(set(rels), {"FOUNDED", "DISCUSSED"})
        self.assertEqual(rels["FOUNDED"]["with_provenance"], 1)
        self.assertEqual(rels["DISCUSSED"]["with_provenance"], 0)

    def test_sample_uses_edge_source_then_co_mention(self) -> None:
        from evals.graph_audit.age import sample_claims

        records, population = sample_claims(n=10, seed=1, dsn=self.db.dsn)
        self.assertEqual(population, 2)
        by_pred = {r["predicate"]: r for r in records}
        founded = by_pred["FOUNDED"]
        self.assertEqual(founded["provenance"], "edge")
        self.assertEqual([e["evidence_id"] for e in founded["evidence"]], ["doc1"])
        self.assertEqual(founded["evidence"][0]["text"], "MacKenzie Price founded Alpha School.")
        self.assertTrue(founded["type_conflict"])
        self.assertEqual(founded["object_types"], ["Organization", "Concept"])
        self.assertNotIn("neo4j_type", founded["relationship_properties"])
        self.assertEqual(founded["relationship_properties"]["extract_model"], "gemini-flash-latest")
        discussed = by_pred["DISCUSSED"]
        self.assertEqual(discussed["provenance"], "co_mention")
        self.assertEqual([e["evidence_id"] for e in discussed["evidence"]], ["doc2"])

    def test_sample_predicate_filter_and_seed(self) -> None:
        from evals.graph_audit.age import sample_claims

        records, population = sample_claims(n=5, seed=3, predicates=["founded"], dsn=self.db.dsn)
        self.assertEqual((population, [r["predicate"] for r in records]), (1, ["FOUNDED"]))
        a, _ = sample_claims(n=1, seed=9, dsn=self.db.dsn)
        b, _ = sample_claims(n=1, seed=9, dsn=self.db.dsn)
        self.assertEqual(a[0]["claim_id"], b[0]["claim_id"])


@unittest.skipUnless(PG_TEST_DSN, SKIP_REASON)
class TestAuditReadsExtractionOutput(unittest.TestCase):
    """The audit over what src/pipeline/index_graph.py writes: no Neo4j anywhere."""

    def test_drops_report_and_sample_from_direct_extraction(self) -> None:
        import json

        from langchain_community.graphs.graph_document import GraphDocument, Node, Relationship
        from langchain_core.documents import Document

        from evals.graph_audit.__main__ import drop_report
        from evals.graph_audit.age import sample_claims
        from src.database import pg_store
        from src.database.age_graph import AgeGraphWriter, document_id
        from src.database.pg_search import read_only

        source = Document(page_content="MacKenzie Price founded Alpha School.", metadata={"title": "Ep 1"})
        rel = Relationship(source=Node(id="MacKenzie Price", type="Person"),
                           target=Node(id="Alpha School", type="Organization"), type="FOUNDED",
                           properties={"source_doc_id": document_id(source), "extract_model": "claude-haiku-4-5"})
        gd = GraphDocument(nodes=[Node(id="MacKenzie Price", type="Person"), Node(id="Alpha School", type="Organization")],
                           relationships=[rel], source=source)
        with TempDatabase() as db:
            pg_store.migrate(db.dsn)
            with pg_store.session(db.dsn) as conn:
                AgeGraphWriter(conn).write([gd])
                for key, model, raw, kept, dnt, drt in (
                    ("a", "claude-haiku-4-5", 4, 2, {"Place": 2}, {"LIVES_IN": 1}),
                    ("b", "gpt-5.1-mini", 2, 2, {"Place": 1, "Date": 1}, {}),
                ):
                    conn.execute(
                        "INSERT INTO ingested_docs (source_key, content_hash, status, extract_model, raw_nodes, "
                        "kept_nodes, raw_rels, kept_rels, rels_dropped_for_endpoint, dropped_node_types, "
                        "dropped_rel_types) VALUES (%s, 'h', 'completed', %s, 5, 3, %s, %s, 1, %s, %s)",
                        (key, model, raw, kept, json.dumps(dnt), json.dumps(drt)),
                    )
                conn.execute("INSERT INTO ingested_docs (source_key, content_hash, status) VALUES ('c', 'h', 'completed')")

            with read_only(db.dsn) as conn:
                report = drop_report(conn, top=5)
            self.assertEqual((report["docs"], report["raw_nodes"], report["kept_nodes"]), (2, 10, 6))
            self.assertEqual((report["raw_rels"], report["kept_rels"], report["endpoint_drops"]), (6, 4, 2))
            self.assertEqual(report["dropped_node_types"], [("Place", 3), ("Date", 1)])
            self.assertEqual(report["dropped_rel_types"], [("LIVES_IN", 1)])
            self.assertEqual([m["model"] for m in report["by_model"]], ["claude-haiku-4-5", "gpt-5.1-mini"])

            records, population = sample_claims(n=5, seed=1, dsn=db.dsn)
            self.assertEqual(population, 1)
            claim = records[0]
            self.assertEqual((claim["subject"], claim["predicate"], claim["object"]),
                             ("MacKenzie Price", "FOUNDED", "Alpha School"))
            self.assertEqual((claim["subject_types"], claim["object_types"]), (["Person"], ["Organization"]))
            self.assertEqual(claim["provenance"], "edge")
            self.assertEqual(claim["evidence"][0]["text"], "MacKenzie Price founded Alpha School.")
