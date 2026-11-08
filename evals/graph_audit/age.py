"""Read the served knowledge graph (Postgres Apache AGE) for the audit. Read-only.

Graph shape follows `src/database/age_graph.py` (extraction) and `src/database/neo4j_to_age.py`
(the one-off Neo4j copy), which write it the same way: each node has one AGE label and
keeps every original type label in a `labels` property; source passages are
`Document` nodes linked to entities by `MENTIONS`; relationship properties (including
`source_doc_id` from extraction) are copied as-is.
"""

from __future__ import annotations

import random
from collections import Counter
from typing import TYPE_CHECKING, Any, Sequence

from evals.graph_audit.scoring import excerpt_around, strip_extraction_preamble
from src.database import pg_store
from src.database.pg_search import _cypher, graph_labels, read_only

if TYPE_CHECKING:
    import psycopg

BASE_LABELS = frozenset({"__Entity__", "__KGBuilder__"})
DOCUMENT = "Document"
MENTIONS = "MENTIONS"


def entity_types(vertex: dict[str, Any]) -> list[str]:
    """Every type label an entity carries (AGE keeps only one as its label)."""
    props = vertex.get("properties") or {}
    labels = [lab for lab in (props.get("labels") or []) if lab not in BASE_LABELS]
    return labels or [str(vertex.get("label"))]


def _entity_name(vertex: dict[str, Any]) -> str:
    props = vertex.get("properties") or {}
    name = props.get("id", props.get("name", props.get("title")))
    return str(name) if name is not None else f"<node {vertex.get('id')}>"


def graph_shape(conn: "psycopg.Connection", graph: str = pg_store.GRAPH_NAME, top: int = 10) -> dict[str, Any]:
    """Entity type conflicts, relationship mix, and how many edges carry provenance."""
    labels = graph_labels(conn, graph)
    if not labels:
        return {"entities": 0, "conflicted": 0, "examples": [], "relationships": []}
    entities = _cypher(
        conn, graph,
        f"MATCH (n) WHERE label(n) <> '{DOCUMENT}' RETURN id(n), n",
        ["nid", "n"], {},
    )
    conflicted = [(nid, v) for nid, v in entities if len(entity_types(v)) > 1]
    examples: list[dict[str, Any]] = []
    if conflicted:
        degrees = dict(
            (int(nid), int(deg)) for nid, deg in _cypher(
                conn, graph,
                "MATCH (n)-[r]-() WHERE id(n) IN $ids RETURN id(n), count(r)",
                ["nid", "deg"], {"ids": [int(nid) for nid, _ in conflicted]},
            )
        )
        ranked = sorted(conflicted, key=lambda nv: -degrees.get(int(nv[0]), 0))[:top]
        examples = [
            {"name": _entity_name(v), "types": entity_types(v), "degree": degrees.get(int(nid), 0)}
            for nid, v in ranked
        ]
    rels = _cypher(
        conn, graph,
        f"MATCH ()-[r]->() WHERE label(r) <> '{MENTIONS}' RETURN label(r), count(r), count(r.source_doc_id)",
        ["t", "n", "with_provenance"], {},
    )
    relationships = sorted(
        ({"type": t, "count": int(n), "with_provenance": int(p)} for t, n, p in rels),
        key=lambda r: -r["count"],
    )
    return {
        "entities": len(entities),
        "conflicted": len(conflicted),
        "type_label_counts": dict(Counter(t for _, v in entities for t in entity_types(v)).most_common()),
        "examples": examples,
        "relationships": relationships,
    }


def _documents_by_id(conn: "psycopg.Connection", graph: str, doc_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    if not doc_ids:
        return {}
    rows = _cypher(
        conn, graph,
        f"MATCH (d:{DOCUMENT}) WHERE d.id IN $ids RETURN d.id, d.text, d.title",
        ["id", "text", "title"], {"ids": list(doc_ids)},
    )
    return {str(i): {"id": str(i), "text": t, "title": ti} for i, t, ti in rows}


def _co_mention_documents(conn: "psycopg.Connection", graph: str, a: int, b: int, k: int) -> list[dict[str, Any]]:
    rows = _cypher(
        conn, graph,
        f"MATCH (d:{DOCUMENT})-[:{MENTIONS}]->(a), (d)-[:{MENTIONS}]->(b) "
        f"WHERE id(a) = $a AND id(b) = $b RETURN d.id, d.text, d.title LIMIT {int(k)}",
        ["id", "text", "title"], {"a": a, "b": b},
    )
    return [{"id": str(i), "text": t, "title": ti} for i, t, ti in rows]


def sample_claims(
    *,
    n: int,
    seed: int,
    predicates: Sequence[str] | None = None,
    max_docs: int = 3,
    max_chars: int = 6000,
    dsn: str | None = None,
    graph: str = pg_store.GRAPH_NAME,
) -> tuple[list[dict[str, Any]], int]:
    """Seeded random sample of entity relationships with source evidence.

    Returns (records, population size). Evidence is the edge's own `source_doc_id`
    document when recorded, else up to `max_docs` documents mentioning both ends.
    """
    with read_only(dsn) as conn:
        labels = graph_labels(conn, graph)
        if not labels:
            return [], 0
        has_docs = labels.get(DOCUMENT) == "v" and labels.get(MENTIONS) == "e"
        population = _cypher(
            conn, graph,
            f"MATCH (a)-[r]->(b) WHERE label(r) <> '{MENTIONS}' AND label(a) <> '{DOCUMENT}' "
            f"AND label(b) <> '{DOCUMENT}' RETURN id(r), label(r)",
            ["rid", "t"], {},
        )
        wanted = {p.lower() for p in predicates} if predicates else None
        ids = sorted(int(rid) for rid, t in population if wanted is None or str(t).lower() in wanted)
        if not ids:
            return [], 0
        chosen = random.Random(seed).sample(ids, min(n, len(ids)))
        rows = _cypher(
            conn, graph,
            "MATCH (a)-[r]->(b) WHERE id(r) IN $ids RETURN id(r), a, r, b",
            ["rid", "a", "r", "b"], {"ids": chosen},
        )
        edge_doc_ids = sorted({
            str(r["properties"]["source_doc_id"]) for _, _, r, _ in rows
            if (r.get("properties") or {}).get("source_doc_id")
        })
        docs = _documents_by_id(conn, graph, edge_doc_ids) if has_docs else {}

        records: list[dict[str, Any]] = []
        order = {rid: i for i, rid in enumerate(chosen)}
        for rid, a, r, b in sorted(rows, key=lambda row: order.get(int(row[0]), 0)):
            props = dict(r.get("properties") or {})
            subject, obj = _entity_name(a), _entity_name(b)
            src = props.get("source_doc_id")
            if src and str(src) in docs:
                provenance, evidence_docs = "edge", [docs[str(src)]]
            elif has_docs:
                evidence_docs = _co_mention_documents(conn, graph, int(a["id"]), int(b["id"]), max_docs)
                provenance = "co_mention" if evidence_docs else "none"
            else:
                provenance, evidence_docs = "none", []
            evidence = []
            for d in evidence_docs:
                text, truncated = excerpt_around(
                    strip_extraction_preamble(d.get("text") or ""), [subject, obj], max_chars
                )
                evidence.append({"evidence_id": d["id"], "title": d.get("title"), "text": text, "truncated": truncated})
            s_types, o_types = entity_types(a), entity_types(b)
            records.append({
                "claim_id": f"age:{rid}",
                "subject": subject,
                "subject_types": s_types,
                "predicate": props.pop("neo4j_type", None) or r.get("label"),
                "object": obj,
                "object_types": o_types,
                "type_conflict": len(s_types) > 1 or len(o_types) > 1,
                "relationship_properties": props,
                "provenance": provenance,
                "evidence": evidence,
                "auditor": None,
                "human": None,
            })
        return records, len(ids)
