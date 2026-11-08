"""Schema filter drop accounting and edge provenance (no Neo4j / network)."""

from __future__ import annotations

from hashlib import md5

from langchain_core.documents import Document
from langchain_neo4j.graphs.graph_document import GraphDocument, Node, Relationship

from src.pipeline.graph_quality import ExtractionStats, filter_to_schema
from src.pipeline.index_graph import apply_schema_and_provenance

NODES = ["Person", "Organization", "Concept"]
RELS = ["FOUNDED", "ADVOCATES_FOR"]


def _graph_doc() -> GraphDocument:
    mac = Node(id="MacKenzie Price", type="Person")
    alpha = Node(id="Alpha School", type="Organization")
    austin = Node(id="Austin", type="Place")  # off-vocabulary
    mastery = Node(id="Mastery learning", type="concept")  # case differs, still allowed
    rels = [
        Relationship(source=mac, target=alpha, type="FOUNDED"),
        Relationship(source=alpha, target=austin, type="FOUNDED"),  # endpoint off-vocab
        Relationship(source=mac, target=mastery, type="LOVES"),  # rel type off-vocab
        Relationship(source=mac, target=mastery, type="advocates_for"),
    ]
    return GraphDocument(
        nodes=[mac, alpha, austin, mastery],
        relationships=rels,
        source=Document(page_content="MacKenzie Price founded Alpha School in Austin."),
    )


def test_filter_counts_each_drop_reason() -> None:
    gd = _graph_doc()
    nodes, rels, stats = filter_to_schema(gd.nodes, gd.relationships, allowed_nodes=NODES, allowed_relationships=RELS)
    assert [n.id for n in nodes] == ["MacKenzie Price", "Alpha School", "Mastery learning"]
    assert [(r.source.id, r.type) for r in rels] == [("MacKenzie Price", "FOUNDED"), ("MacKenzie Price", "advocates_for")]
    assert (stats.raw_nodes, stats.kept_nodes, stats.raw_rels, stats.kept_rels) == (4, 3, 4, 2)
    assert stats.dropped_node_types == {"Place": 1}
    assert stats.dropped_rel_types == {"LOVES": 1}
    assert stats.rels_dropped_for_endpoint == 1


def test_stats_add_and_record_are_flat() -> None:
    a = ExtractionStats(raw_nodes=2, kept_nodes=1)
    a.dropped_node_types["Place"] += 1
    b = ExtractionStats(raw_nodes=3, kept_nodes=3, raw_rels=1, kept_rels=0)
    b.dropped_rel_types["LOVES"] += 1
    a.add(b)
    rec = a.to_record()
    assert rec["raw_nodes"] == 5 and rec["kept_nodes"] == 4 and rec["raw_rels"] == 1
    assert rec["dropped_node_types"] == ["Place:1"] and rec["dropped_rel_types"] == ["LOVES:1"]
    assert all(isinstance(v, (int, list)) for v in rec.values())


def test_provenance_stamped_with_writer_document_id() -> None:
    gd = _graph_doc()
    out, per_doc = apply_schema_and_provenance(
        [gd], allowed_nodes=NODES, allowed_relationships=RELS, extract_model="m1", ingest_run_id="run-1"
    )
    expected_id = md5(gd.source.page_content.encode("utf-8")).hexdigest()
    assert gd.source.metadata["id"] == expected_id  # same id Neo4jGraph would assign
    assert len(per_doc) == 1 and per_doc[0].kept_rels == 2
    for rel in out[0].relationships:
        assert rel.properties == {"source_doc_id": expected_id, "extract_model": "m1", "ingest_run_id": "run-1"}


def test_provenance_keeps_existing_document_id() -> None:
    gd = _graph_doc()
    gd.source.metadata["id"] = "fixed"
    out, _ = apply_schema_and_provenance(
        [gd], allowed_nodes=NODES, allowed_relationships=RELS, extract_model="m1", ingest_run_id=None
    )
    assert {r.properties["source_doc_id"] for r in out[0].relationships} == {"fixed"}
    assert all("ingest_run_id" not in r.properties for r in out[0].relationships)
