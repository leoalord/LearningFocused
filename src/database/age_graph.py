"""Write extracted knowledge graphs straight into the Postgres AGE graph `knowledge`.

`src/pipeline/index_graph.py` extracts graph documents with LangChain's
`LLMGraphTransformer` and hands them to `AgeGraphWriter`, which stores them in the
shape `src/database/neo4j_to_age.py` produces from a Neo4j graph, so
`src/database/pg_search.py` and the MCP server read both the same way:

- Entities: one vertex per entity `id`, merged by exact `id` across every entity label
  (what Neo4j's `MERGE (:__Entity__ {id})` does). AGE allows one label per vertex, so the
  vertex keeps the label of the type it was first written with; every type it has been
  given is in its `labels` property (`["__Entity__", "Person", ...]`), and later
  properties are merged in (`SET n += props`). `name_lc` holds the lower-cased id.
- Documents: one `Document` vertex per source passage, keyed by `id` (the source's
  `metadata["id"]`, else the md5 of its text, as `Neo4jGraph.add_graph_documents` does),
  with the passage in `text` and its metadata as properties. A `MENTIONS` edge links it
  to every entity extracted from it.
- Relationships: one edge per (source, type, target). Properties are set when the edge
  is created and never overwritten (Neo4j's `apoc.merge.relationship` with on-create
  properties), so `source_doc_id` / `extract_model` / `ingest_run_id` name the passage
  and run that first produced the edge.

`AgeGraphWriter.write` runs inside the caller's transaction and takes a transaction-level
advisory lock, so two extraction runs can't create the same entity twice.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from hashlib import md5
from typing import TYPE_CHECKING, Any

from src.database.pg_store import GRAPH_NAME

if TYPE_CHECKING:
    import psycopg

ENTITY_BASE_LABEL = "__Entity__"
DOCUMENT_LABEL = "Document"
MENTIONS = "MENTIONS"
NAME_KEY = "name_lc"  # src/database/pg_search.py matches entity names on it
_LOCK_KEY = "learningfocused:age_graph_writer"


def age_name(name: str, fallback: str) -> str:
    """A label name AGE accepts: [A-Za-z0-9_] with no leading underscore."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name or "").lstrip("_")
    return cleaned or fallback


def jsonable(value: Any) -> Any:
    """Plain JSON value for an agtype property."""
    if isinstance(value, float) and not math.isfinite(value):
        return None  # agtype has no NaN/Infinity literal
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)  # datetimes, paths, neo4j temporal types


def name_lc(name: Any) -> str | None:
    """Lower-cased string form of an entity name. Non-string ids get their JSON form, so
    one odd node can't break `toLower()` for every query."""
    if name is None:
        return None
    return (name if isinstance(name, str) else json.dumps(name)).lower()


def ensure_label(conn: "psycopg.Connection", graph: str, name: str, kind: str) -> tuple[int, str]:
    """(label id, sequence name) of a vertex ('v') or edge ('e') label, created if missing."""
    sql = (
        "SELECT l.id, l.seq_name, l.kind FROM ag_catalog.ag_label l "
        "JOIN ag_catalog.ag_graph g ON g.graphid = l.graph WHERE g.name = %s AND l.name = %s"
    )
    row = conn.execute(sql, (graph, name)).fetchone()
    if row is None:
        create = "create_vlabel" if kind == "v" else "create_elabel"
        conn.execute(f"SELECT ag_catalog.{create}(%s, %s)", (graph, name))
        row = conn.execute(sql, (graph, name)).fetchone()
    assert row is not None
    if row[2] != kind:
        raise ValueError(f"AGE label {name!r} already exists as kind {row[2]!r}, need {kind!r}")
    return int(row[0]), str(row[1])


def document_id(source: Any) -> str:
    """Id of a source passage's Document vertex (stored back on its metadata)."""
    existing = source.metadata.get("id")
    if existing:
        return str(existing)
    doc_id = md5(source.page_content.encode("utf-8")).hexdigest()
    source.metadata["id"] = doc_id
    return doc_id


def relationship_label(rel_type: str) -> str:
    """Neo4jGraph's relationship type normalization, then reduced to an AGE label."""
    return age_name(rel_type.replace("`", "").replace(" ", "_").upper(), "RELATED_TO")


@dataclass
class GraphWriteStats:
    documents_created: int = 0
    documents_updated: int = 0
    entities_created: int = 0
    entities_updated: int = 0
    mentions_created: int = 0
    edges_created: int = 0
    edges_existing: int = 0

    def add(self, other: "GraphWriteStats") -> None:
        for key, value in asdict(other).items():
            setattr(self, key, getattr(self, key) + value)

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass
class _Vertex:
    gid: str | None  # graphid as text; None until inserted
    label: str
    props: dict[str, Any]
    dirty: bool = False


def _agtype(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)  # property maps print as plain JSON
        except ValueError:
            pass
    from src.database.pg_search import parse_agtype

    return parse_agtype(value)


class AgeGraphWriter:
    """Upsert LangChain graph documents (nodes, relationships, source) into AGE."""

    def __init__(self, conn: "psycopg.Connection", graph: str = GRAPH_NAME) -> None:
        self.conn = conn
        self.graph = graph
        self._labels: dict[tuple[str, str], tuple[int, str]] = {}

    # ---------- low-level ----------

    def _label(self, name: str, kind: str) -> tuple[int, str]:
        key = (name, kind)
        if key not in self._labels:
            self._labels[key] = ensure_label(self.conn, self.graph, name, kind)
        return self._labels[key]

    def _table(self, label: str) -> Any:
        from psycopg import sql

        return sql.Identifier(self.graph, label)

    def _existing_labels(self) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT l.name, l.kind FROM ag_catalog.ag_label l JOIN ag_catalog.ag_graph g ON g.graphid = l.graph "
            "WHERE g.name = %s",
            (self.graph,),
        ).fetchall()
        return {str(r[0]): str(r[1]) for r in rows}

    def _cypher(self, query: str, columns: Sequence[str], params: dict[str, Any]) -> list[list[Any]]:
        from psycopg import sql

        stmt = sql.Composed([
            sql.SQL("SELECT * FROM ag_catalog.cypher("), sql.Literal(self.graph),
            sql.SQL(", $$ " + query.replace("%", "%%") + " $$, %s) AS ("),
            sql.SQL(", ").join(sql.SQL("{} ag_catalog.agtype").format(sql.Identifier(c)) for c in columns),
            sql.SQL(")"),
        ])
        return [[_agtype(v) for v in row] for row in self.conn.execute(stmt, (json.dumps(params),)).fetchall()]

    def _lookup(self, ids: list[str], *, documents: bool) -> dict[str, _Vertex]:
        """Existing vertices by `id`: Document vertices, or entities (anything else).

        Plain SQL over the label tables: AGE evaluates a cypher `n.id IN $ids` row by row,
        which takes seconds on a 100k-vertex graph; this takes milliseconds.
        """
        from psycopg import sql

        if not ids:
            return {}
        labels = self._existing_labels()
        if documents:
            if labels.get(DOCUMENT_LABEL) != "v":
                return {}
            table, extra = self._table(DOCUMENT_LABEL), sql.Composed([])
        else:
            table = self._table("_ag_label_vertex")  # parent of every vertex label table
            extra = sql.SQL(" AND c.relname <> {}").format(sql.Literal(DOCUMENT_LABEL))
        rows = self.conn.execute(
            sql.SQL(
                "SELECT v.id::text, c.relname, v.properties::text FROM {} v JOIN pg_class c ON c.oid = v.tableoid "
                "WHERE (v.properties ->> '\"id\"'::ag_catalog.agtype)::text = ANY(%s::text[]){} ORDER BY v.id"
            ).format(table, extra),
            (ids,),
        ).fetchall()
        found: dict[str, _Vertex] = {}
        for gid, label, props_text in rows:
            props = dict(_agtype(props_text) or {})
            key = props.get("id")
            key = key if isinstance(key, str) else json.dumps(key) if key is not None else None
            if key is not None and key not in found:
                found[key] = _Vertex(str(gid), str(label), props)
        return found

    def _insert_vertices(self, label: str, vertices: list[_Vertex]) -> None:
        from psycopg import sql

        label_id, seq = self._label(label, "v")
        rows = self.conn.execute(
            sql.SQL(
                "INSERT INTO {} (id, properties) "
                "SELECT ag_catalog._graphid(%s, nextval(format('%%I.%%I', %s::text, %s::text)::regclass)), "
                "p::ag_catalog.agtype FROM unnest(%s::text[]) WITH ORDINALITY AS t(p, ord) ORDER BY ord "
                "RETURNING id::text"
            ).format(self._table(label)),
            (label_id, self.graph, seq, [json.dumps(v.props) for v in vertices]),
        ).fetchall()
        # INSERT ... SELECT ... ORDER BY returns rows in insertion order.
        for vertex, row in zip(vertices, rows):
            vertex.gid = str(row[0])

    def _update_vertex(self, vertex: _Vertex) -> None:
        from psycopg import sql

        self.conn.execute(
            sql.SQL("UPDATE {} SET properties = %s::ag_catalog.agtype WHERE id = %s::text::ag_catalog.graphid")
            .format(self._table(vertex.label)),
            (json.dumps(vertex.props), vertex.gid),
        )

    def _merge_edges(self, label: str, edges: dict[tuple[str, str], dict[str, Any]]) -> tuple[int, int]:
        """Create edges (start gid, end gid) -> props that don't exist yet. (created, existing)."""
        from psycopg import sql

        if not edges:
            return 0, 0
        label_id, seq = self._label(label, "e")
        starts = [k[0] for k in edges]
        ends = [k[1] for k in edges]
        present = {
            (str(r[0]), str(r[1]))
            for r in self.conn.execute(
                sql.SQL(
                    "SELECT e.start_id::text, e.end_id::text FROM {} e "
                    "JOIN unnest(%s::text[], %s::text[]) AS t(s, o) "
                    "ON e.start_id = t.s::ag_catalog.graphid AND e.end_id = t.o::ag_catalog.graphid"
                ).format(self._table(label)),
                (starts, ends),
            ).fetchall()
        }
        new = [(k, v) for k, v in edges.items() if k not in present]
        if new:
            self.conn.execute(
                sql.SQL(
                    "INSERT INTO {} (id, start_id, end_id, properties) "
                    "SELECT ag_catalog._graphid(%s, nextval(format('%%I.%%I', %s::text, %s::text)::regclass)), "
                    "t.s::ag_catalog.graphid, t.o::ag_catalog.graphid, t.p::ag_catalog.agtype "
                    "FROM unnest(%s::text[], %s::text[], %s::text[]) AS t(s, o, p)"
                ).format(self._table(label)),
                (label_id, self.graph, seq, [k[0] for k, _ in new], [k[1] for k, _ in new],
                 [json.dumps(v) for _, v in new]),
            )
        return len(new), len(edges) - len(new)

    # ---------- public ----------

    def lock(self) -> None:
        """Serialize graph writers for the rest of the current transaction."""
        self.conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{_LOCK_KEY}:{self.graph}",))

    def existing_document_ids(self, ids: Iterable[str]) -> set[str]:
        return set(self._lookup(sorted(set(ids)), documents=True))

    def write(self, graph_documents: Sequence[Any]) -> GraphWriteStats:
        """Upsert graph documents (objects with `.nodes`, `.relationships`, `.source`)."""
        stats = GraphWriteStats()
        if not graph_documents:
            return stats
        self.lock()

        # Collect what this call wants to exist.
        doc_props: dict[str, dict[str, Any]] = {}
        entity_types: dict[str, list[str]] = {}       # entity id -> node types, in first-seen order
        endpoint_types: dict[str, list[str]] = {}     # entity id -> types seen only on relationship ends
        entity_props: dict[str, dict[str, Any]] = {}  # entity id -> properties to merge in
        mentions: list[tuple[str, str]] = []           # (document id, entity id)
        rels: list[tuple[str, str, str, dict[str, Any]]] = []  # (label, source id, target id, props)

        def add_entity(entity_id: str, entity_type: str, typed: bool) -> None:
            clean = entity_type.replace("`", "")
            # Relationship endpoints don't add a type label in Neo4j; their type only names
            # the label of a vertex that has to be created for them.
            types = (entity_types if typed else endpoint_types).setdefault(entity_id, [])
            if clean and clean not in types:
                types.append(clean)

        for gd in graph_documents:
            doc_id: str | None = None
            if gd.source is not None:
                doc_id = document_id(gd.source)
                props = {"text": gd.source.page_content, **jsonable(dict(gd.source.metadata))}
                doc_props[doc_id] = {**doc_props.get(doc_id, {}), **props}
            for node in gd.nodes:
                entity_id = str(node.id)
                add_entity(entity_id, str(node.type), typed=True)
                entity_props.setdefault(entity_id, {}).update(jsonable(dict(getattr(node, "properties", None) or {})))
                if doc_id is not None:
                    mentions.append((doc_id, entity_id))
            for rel in gd.relationships:
                for end in (rel.source, rel.target):
                    add_entity(str(end.id), str(end.type), typed=False)
                rels.append((relationship_label(str(rel.type)), str(rel.source.id), str(rel.target.id),
                             jsonable(dict(rel.properties or {}))))

        # Documents.
        docs = self._lookup(sorted(doc_props), documents=True)
        new_docs: list[_Vertex] = []
        for doc_id, props in doc_props.items():
            full = {**props, "id": doc_id, "labels": [DOCUMENT_LABEL], NAME_KEY: name_lc(doc_id)}
            if doc_id in docs:
                vertex = docs[doc_id]
                merged = {**vertex.props, **full, "labels": vertex.props.get("labels") or [DOCUMENT_LABEL]}
                if merged != vertex.props:
                    vertex.props = merged
                    self._update_vertex(vertex)
                    stats.documents_updated += 1
            else:
                vertex = _Vertex(None, DOCUMENT_LABEL, full)
                docs[doc_id] = vertex
                new_docs.append(vertex)
        if new_docs:
            self._insert_vertices(DOCUMENT_LABEL, new_docs)
            stats.documents_created += len(new_docs)

        # Entities.
        wanted = list(dict.fromkeys([*entity_types, *endpoint_types]))
        entities = self._lookup(sorted(wanted), documents=False)
        to_insert: dict[str, list[_Vertex]] = {}
        for entity_id in wanted:
            types = entity_types.get(entity_id) or endpoint_types.get(entity_id, [])[:1]
            new_props = entity_props.get(entity_id, {})
            if entity_id in entities:
                vertex = entities[entity_id]
                labels = list(vertex.props.get("labels") or [ENTITY_BASE_LABEL, vertex.label])
                labels += [t for t in entity_types.get(entity_id, []) if t not in labels]
                merged = {**vertex.props, **new_props, "labels": labels}
                if merged != vertex.props:
                    vertex.props = merged
                    self._update_vertex(vertex)
                    stats.entities_updated += 1
            else:
                label = age_name(types[0], "Entity") if types else "Entity"
                if label == DOCUMENT_LABEL:
                    label = "Entity"  # never mix entities into the source-passage label
                props = {**new_props, "id": entity_id, "labels": [ENTITY_BASE_LABEL, *types],
                         NAME_KEY: name_lc(entity_id)}
                vertex = _Vertex(None, label, props)
                entities[entity_id] = vertex
                to_insert.setdefault(label, []).append(vertex)
        for label, vertices in to_insert.items():
            self._insert_vertices(label, vertices)
            stats.entities_created += len(vertices)

        # Edges: MENTIONS first, then extracted relationships.
        mention_edges: dict[tuple[str, str], dict[str, Any]] = {
            (str(docs[d].gid), str(entities[e].gid)): {} for d, e in mentions
        }
        created, _ = self._merge_edges(MENTIONS, mention_edges)
        stats.mentions_created += created

        by_label: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
        for label, source_id, target_id, props in rels:
            key = (str(entities[source_id].gid), str(entities[target_id].gid))
            by_label.setdefault(label, {}).setdefault(key, props)  # first occurrence wins
        for label, edges in by_label.items():
            created, existing = self._merge_edges(label, edges)
            stats.edges_created += created
            stats.edges_existing += existing
        return stats

    def delete_documents(self, where_cypher: str) -> int:
        """DETACH DELETE Document vertices matching a fixed WHERE clause on `d`."""
        if self._existing_labels().get(DOCUMENT_LABEL) != "v":
            return 0
        rows = self._cypher(f"MATCH (d:{DOCUMENT_LABEL}) WHERE {where_cypher} DETACH DELETE d RETURN 1",
                            ["x"], {})
        return len(rows)
