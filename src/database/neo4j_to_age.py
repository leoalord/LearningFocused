"""Copy the local Neo4j knowledge graph into the Postgres AGE graph `knowledge`.

Works at any graph size: nodes and relationships are streamed from Neo4j (no full load
into memory) and written to Postgres in batches, each in its own transaction. It can be
stopped and re-run: `age_import_map` remembers every Neo4j `elementId` already copied,
so a re-run only adds what is missing. Leave the Neo4j graph unchanged between an
interrupted run and its re-run, because Neo4j may reuse the ids of deleted items. It
never deletes anything in Neo4j or Postgres.

Run it from the machine that has Neo4j (Leo's Mac), with Postgres reachable through the
IAP tunnel (docs/postgres.md) and the admin DSN:

    NEO4J_URI=bolt://localhost:7687 NEO4J_USERNAME=neo4j NEO4J_PASSWORD=... \\
    PG_DSN=postgresql://learningfocused@localhost:6543/learningfocused \\
      uv run python -m src.database.neo4j_to_age --grant-to learningfocused_app

Mapping: each node gets one AGE label (its first label other than `__Entity__`;
AGE allows one) and keeps all Neo4j labels in a `labels` property plus its Neo4j
elementId in `neo4j_id`, plus `name_lc` (its `id` as a lower-case string) for lookups. Relationship types map to AGE edge labels. Names are reduced to
[A-Za-z0-9_] (AGE reserves a leading underscore); the import refuses to start if two
names would reduce to the same AGE label. `IngestedDoc` nodes are the indexer's
bookkeeping (which source documents were processed), not knowledge, so they and their
relationships are skipped.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Iterator

from src.database.pg_store import GRAPH_NAME

if TYPE_CHECKING:
    import psycopg

DEFAULT_BATCH_SIZE = 1000
IGNORED_LABELS = {"__Entity__", "__KGBuilder__"}
# Nodes with these labels are not copied (src/pipeline/index_neo4j.py bookkeeping).
SKIPPED_LABELS = ("IngestedDoc",)
DEFAULT_DROP_PROPERTIES = ("embedding",)
NAME_KEY = "name_lc"  # see node_properties; src/database/pg_search.py matches on it
MAP_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS age_import_map (
    nid text PRIMARY KEY,           -- 'n:<neo4j elementId>' or 'e:<neo4j elementId>'
    gid ag_catalog.graphid NOT NULL
)
"""


@dataclass
class NodeRow:
    nid: str
    labels: list[str]
    props: dict[str, Any]


@dataclass
class EdgeRow:
    rid: str
    type: str
    start: str
    end: str
    props: dict[str, Any]


@dataclass
class ImportStats:
    nodes_seen: int = 0
    nodes_written: int = 0
    edges_seen: int = 0
    edges_written: int = 0
    edges_missing_endpoint: int = 0
    labels: dict[str, int] = field(default_factory=dict)


def age_name(name: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name or "").lstrip("_")
    return cleaned or fallback


def primary_label(labels: list[str]) -> str:
    for label in labels:
        if label not in IGNORED_LABELS:
            return age_name(label, "Entity")
    return "Entity"


def name_collisions(labels: Iterable[str], rel_types: Iterable[str]) -> list[str]:
    """Problems that would make two Neo4j names share one AGE label, as readable lines."""
    problems = []
    vertex: dict[str, set[str]] = {}
    for label in labels:
        if label not in IGNORED_LABELS and label not in SKIPPED_LABELS:
            vertex.setdefault(age_name(label, "Entity"), set()).add(label)
    edge: dict[str, set[str]] = {}
    for rel_type in rel_types:
        edge.setdefault(age_name(rel_type, "RELATED_TO"), set()).add(rel_type)
    for kind, names in (("node labels", vertex), ("relationship types", edge)):
        for target, sources in sorted(names.items()):
            if len(sources) > 1:
                problems.append(f"{kind} {sorted(sources)} all become AGE label {target!r}")
    for target in sorted(set(vertex) & set(edge)):
        problems.append(f"node label {sorted(vertex[target])} and relationship type "
                        f"{sorted(edge[target])} both become AGE label {target!r}")
    return problems


def _jsonable(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None  # agtype has no NaN/Infinity literal
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)  # neo4j temporal/spatial types


def node_properties(row: NodeRow, drop: Iterable[str]) -> str:
    dropped = set(drop)
    props = {k: _jsonable(v) for k, v in row.props.items() if k not in dropped}
    props["labels"] = list(row.labels)
    props["neo4j_id"] = row.nid
    # Lower-cased string form of the entity name, which graph lookups match on. Neo4j
    # `id`s are usually strings but can be numbers or lists; a string copy keeps one odd
    # node from breaking toLower() for every query.
    name = props.get("id", props.get("name"))
    if name is not None:
        props[NAME_KEY] = (name if isinstance(name, str) else json.dumps(name)).lower()
    return json.dumps(props)


def edge_properties(row: EdgeRow, drop: Iterable[str]) -> str:
    dropped = set(drop)
    props = {k: _jsonable(v) for k, v in row.props.items() if k not in dropped}
    props["neo4j_type"] = row.type
    return json.dumps(props)


class AgeWriter:
    """Batch inserts straight into AGE's label tables (much faster than per-row CREATE)."""

    def __init__(self, conn: "psycopg.Connection", graph: str = GRAPH_NAME) -> None:
        self.conn = conn
        self.graph = graph
        self._labels: dict[tuple[str, str], tuple[int, str]] = {}

    def setup(self) -> None:
        self.conn.execute(MAP_TABLE_SQL)

    def _label(self, name: str, kind: str) -> tuple[int, str]:
        key = (name, kind)
        if key not in self._labels:
            sql = "SELECT l.id, l.seq_name, l.kind FROM ag_catalog.ag_label l JOIN ag_catalog.ag_graph g ON g.graphid = l.graph WHERE g.name = %s AND l.name = %s"
            row = self.conn.execute(sql, (self.graph, name)).fetchone()
            if row is None:
                create = "create_vlabel" if kind == "v" else "create_elabel"
                self.conn.execute(f"SELECT ag_catalog.{create}(%s, %s)", (self.graph, name))
                row = self.conn.execute(sql, (self.graph, name)).fetchone()
            assert row is not None
            if row[2] != kind:
                raise ValueError(f"AGE label {name!r} already exists as kind {row[2]!r}, need {kind!r}")
            self._labels[key] = (int(row[0]), str(row[1]))
        return self._labels[key]

    def write_vertices(self, label: str, rows: list[tuple[str, str]]) -> int:
        """rows: (map key, properties json). Returns how many were new."""
        label_id, seq = self._label(label, "v")
        cur = self.conn.execute(
            f"""
            WITH src AS MATERIALIZED (
                SELECT t.nid, t.props,
                       ag_catalog._graphid(%s, nextval(format('%%I.%%I', %s::text, %s::text)::regclass)) AS gid
                FROM unnest(%s::text[], %s::text[]) AS t(nid, props)
                WHERE NOT EXISTS (SELECT 1 FROM age_import_map m WHERE m.nid = t.nid)
            ), ins AS (
                INSERT INTO {self._table(label)} (id, properties) SELECT gid, props::ag_catalog.agtype FROM src
            )
            INSERT INTO age_import_map (nid, gid) SELECT nid, gid FROM src
            """,
            (label_id, self.graph, seq, [r[0] for r in rows], [r[1] for r in rows]),
        )
        return max(cur.rowcount, 0)

    def write_edges(self, label: str, rows: list[tuple[str, str, str, str]]) -> int:
        """rows: (map key, start map key, end map key, properties json). Returns how many were new."""
        label_id, seq = self._label(label, "e")
        cur = self.conn.execute(
            f"""
            WITH src AS MATERIALIZED (
                SELECT t.rid, t.props, s.gid AS start_id, e.gid AS end_id,
                       ag_catalog._graphid(%s, nextval(format('%%I.%%I', %s::text, %s::text)::regclass)) AS gid
                FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[]) AS t(rid, s_nid, e_nid, props)
                JOIN age_import_map s ON s.nid = t.s_nid
                JOIN age_import_map e ON e.nid = t.e_nid
                WHERE NOT EXISTS (SELECT 1 FROM age_import_map m WHERE m.nid = t.rid)
            ), ins AS (
                INSERT INTO {self._table(label)} (id, start_id, end_id, properties)
                SELECT gid, start_id, end_id, props::ag_catalog.agtype FROM src
            )
            INSERT INTO age_import_map (nid, gid) SELECT rid, gid FROM src
            """,
            (
                label_id, self.graph, seq,
                [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows],
            ),
        )
        return max(cur.rowcount, 0)

    def mapped(self, keys: list[str]) -> set[str]:
        rows = self.conn.execute("SELECT nid FROM age_import_map WHERE nid = ANY(%s)", (keys,)).fetchall()
        return {r[0] for r in rows}

    def _table(self, label: str) -> str:
        from psycopg import sql

        return sql.Identifier(self.graph, label).as_string(self.conn)


def _batches(rows: Iterable[Any], size: int) -> Iterator[list[Any]]:
    batch: list[Any] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def import_graph(
    nodes: Iterable[NodeRow],
    edges: Iterable[EdgeRow],
    *,
    dsn: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    drop_properties: Iterable[str] = DEFAULT_DROP_PROPERTIES,
) -> ImportStats:
    """Write streamed nodes, then edges, into AGE. Each batch commits on its own."""
    from src.database import pg_store

    drop = tuple(drop_properties)
    stats = ImportStats()
    with pg_store.session(dsn) as conn:
        pg_store.ensure_graph(conn)
        AgeWriter(conn).setup()

    for batch in _batches(nodes, batch_size):
        by_label: dict[str, list[tuple[str, str]]] = {}
        for row in batch:
            label = primary_label(row.labels)
            by_label.setdefault(label, []).append((f"n:{row.nid}", node_properties(row, drop)))
        with pg_store.session(dsn) as conn:
            writer = AgeWriter(conn)
            for label, rows in by_label.items():
                written = writer.write_vertices(label, rows)
                stats.nodes_written += written
                stats.labels[label] = stats.labels.get(label, 0) + len(rows)
        stats.nodes_seen += len(batch)
        print(f"nodes: {stats.nodes_seen} read, {stats.nodes_written} new")

    for batch in _batches(edges, batch_size):
        by_type: dict[str, list[tuple[str, str, str, str]]] = {}
        for edge in batch:
            label = age_name(edge.type, "RELATED_TO")
            by_type.setdefault(label, []).append(
                (f"e:{edge.rid}", f"n:{edge.start}", f"n:{edge.end}", edge_properties(edge, drop))
            )
        with pg_store.session(dsn) as conn:
            writer = AgeWriter(conn)
            for label, edge_rows in by_type.items():
                stats.edges_written += writer.write_edges(label, edge_rows)
            all_rows = [r for edge_rows in by_type.values() for r in edge_rows]
            present = writer.mapped(sorted({k for r in all_rows for k in (r[1], r[2])}))
            stats.edges_missing_endpoint += sum(1 for r in all_rows if r[1] not in present or r[2] not in present)
        stats.edges_seen += len(batch)
        print(f"edges: {stats.edges_seen} read, {stats.edges_written} new")
    return stats


_NODE_FILTER = "none(l IN labels(n) WHERE l IN $skip)"
_EDGE_FILTER = "none(l IN labels(a) + labels(b) WHERE l IN $skip)"


def read_neo4j(batch_size: int) -> tuple[Iterator[NodeRow], Iterator[EdgeRow], Any]:
    """Streaming readers over the Neo4j in NEO4J_URI/USERNAME/PASSWORD. Returns (nodes, edges, driver).

    Keyed by elementId(), the stable successor of the deprecated id().
    """
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"], auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"])
    )

    def nodes() -> Iterator[NodeRow]:
        with driver.session(fetch_size=batch_size) as session:
            result = session.run(
                f"MATCH (n) WHERE {_NODE_FILTER} "
                "RETURN elementId(n) AS nid, labels(n) AS labels, properties(n) AS props",
                skip=list(SKIPPED_LABELS),
            )
            for rec in result:
                yield NodeRow(str(rec["nid"]), list(rec["labels"]), dict(rec["props"]))

    def edges() -> Iterator[EdgeRow]:
        with driver.session(fetch_size=batch_size) as session:
            result = session.run(
                f"MATCH (a)-[r]->(b) WHERE {_EDGE_FILTER} RETURN elementId(r) AS rid, type(r) AS type, "
                "elementId(a) AS s, elementId(b) AS e, properties(r) AS props",
                skip=list(SKIPPED_LABELS),
            )
            for rec in result:
                yield EdgeRow(str(rec["rid"]), str(rec["type"]), str(rec["s"]), str(rec["e"]), dict(rec["props"]))

    return nodes(), edges(), driver


def neo4j_counts(driver: Any) -> dict[str, int]:
    """What the import should copy (bookkeeping nodes excluded, as in read_neo4j)."""
    skip = list(SKIPPED_LABELS)
    with driver.session() as session:
        n = session.run(f"MATCH (n) WHERE {_NODE_FILTER} RETURN count(n) AS c", skip=skip).single()["c"]
        r = session.run(f"MATCH (a)-[r]->(b) WHERE {_EDGE_FILTER} RETURN count(r) AS c", skip=skip).single()["c"]
    return {"nodes": int(n), "relationships": int(r)}


def neo4j_names(driver: Any) -> tuple[list[str], list[str]]:
    with driver.session() as session:
        labels = [r["label"] for r in session.run("CALL db.labels() YIELD label RETURN label")]
        types = [r["relationshipType"] for r in session.run(
            "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType")]
    return labels, types


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m src.database.neo4j_to_age", description=__doc__.splitlines()[0])
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument("--keep-property", action="append", default=[],
                   help="Keep a property dropped by default (default drops: embedding).")
    p.add_argument("--grant-to", metavar="ROLE",
                   help="Afterwards give this role (e.g. learningfocused_app) ownership of the graph tables.")
    args = p.parse_args(argv)
    if args.batch_size < 1:
        p.error("--batch-size must be >= 1")

    from src.database import pg_store

    nodes, edges, driver = read_neo4j(args.batch_size)
    try:
        problems = name_collisions(*neo4j_names(driver))
        if problems:
            print("Not importing: these Neo4j names would share an AGE label. Rename them in Neo4j first.")
            for problem in problems:
                print(f"  {problem}")
            return 2
        expected = neo4j_counts(driver)
        print(f"Neo4j: {expected}")
        drop = [d for d in DEFAULT_DROP_PROPERTIES if d not in set(args.keep_property)]
        stats = import_graph(nodes, edges, batch_size=args.batch_size, drop_properties=drop)
    finally:
        driver.close()
    with pg_store.session() as conn:
        row = conn.execute(
            "SELECT count(*) FILTER (WHERE nid LIKE 'n:%%'), count(*) FILTER (WHERE nid LIKE 'e:%%') FROM age_import_map"
        ).fetchone()
        mapped_nodes, mapped_edges = (int(row[0]), int(row[1])) if row else (0, 0)
        if args.grant_to:
            pg_store.grant_graph_ownership(conn, args.grant_to)
    print(json.dumps(asdict(stats), indent=2))
    print(f"AGE now holds {mapped_nodes} of {expected['nodes']} nodes and {mapped_edges} of {expected['relationships']} relationships.")
    if stats.edges_missing_endpoint:
        print(f"{stats.edges_missing_endpoint} relationships point at nodes that were not imported "
              "(the graph changed during the import?). Re-running won't add them.")
    complete = mapped_nodes >= expected["nodes"] and mapped_edges >= expected["relationships"]
    if mapped_nodes < expected["nodes"] or mapped_edges + stats.edges_missing_endpoint < expected["relationships"]:
        print("Incomplete: re-run to copy the rest (already-copied items are skipped).")
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
