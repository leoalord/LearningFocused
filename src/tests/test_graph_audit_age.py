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
