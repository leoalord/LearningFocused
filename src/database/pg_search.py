"""Read-only retrieval over the Postgres knowledge store: keyword, vector, hybrid, graph.

- keyword: Postgres full-text search (`chunks.tsv`, `websearch_to_tsquery`, ts_rank_cd)
- vector: pgvector cosine distance on the stored text-embedding-3-small vectors
- hybrid: both lists fused with Reciprocal Rank Fusion (RRF, k=60)
- graph: a fixed set of read-only query templates over the AGE graph `knowledge`.
  Callers pick a template and pass values; they never send query text, and values
  travel as bound parameters.

Every connection is read-only with a statement timeout, so nothing here can change data.
Results come back as LangChain `Document`s (summaries, segments), the same shape the
Chroma helpers return, so the MCP tools and evals work with either backend.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator, Sequence

from src.database import pg_store
from src.database.chroma_manager import DEFAULT_SEGMENT_TYPES, DEFAULT_SUMMARY_TYPES

if TYPE_CHECKING:
    import psycopg
    from langchain_core.documents import Document

SEARCH_MODES = ("vector", "keyword", "hybrid")
GRAPH_TEMPLATES = ("schema", "find_entities", "neighbors", "connections", "mentions")
RRF_K = 60
# How many candidates each list contributes to hybrid fusion, per result wanted.
HYBRID_CANDIDATES_PER_RESULT = 4
MAX_GRAPH_HOPS = 3
EMBEDDING_MODEL = "text-embedding-3-small"
_AGTYPE_SUFFIX = re.compile(r"(?<=[}\]])::(?:vertex|edge|path)")
_LABEL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def statement_timeout_ms() -> int:
    try:
        return max(100, int(os.getenv("PG_STATEMENT_TIMEOUT_MS") or 5000))
    except ValueError:
        return 5000


@contextmanager
def read_only(dsn: str | None = None) -> Iterator["psycopg.Connection"]:
    """A read-only connection with a statement timeout; rolled back and closed after use."""
    # Startup options, so they hold for every transaction on this connection.
    conn = pg_store.connect(
        dsn, extra_options=f"-c statement_timeout={statement_timeout_ms()} -c default_transaction_read_only=on"
    )
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


_EMBEDDINGS: Any = None


def embed_query(text: str) -> Any:
    """The query vector, from the same model that embedded the corpus."""
    import numpy as np
    from langchain_openai import OpenAIEmbeddings

    global _EMBEDDINGS
    if _EMBEDDINGS is None:
        _EMBEDDINGS = OpenAIEmbeddings(model=EMBEDDING_MODEL)
    return np.asarray(_EMBEDDINGS.embed_query(text), dtype=np.float32)


@dataclass
class Hit:
    id: str
    type: str
    content: str
    metadata: dict[str, Any]
    score: float

    def to_document(self) -> "Document":
        from langchain_core.documents import Document

        return Document(page_content=self.content, metadata={**self.metadata, "chunk_id": self.id,
                                                             "score": round(self.score, 6)})


def _hits(rows: Sequence[tuple[Any, ...]]) -> list[Hit]:
    return [Hit(str(r[0]), str(r[1]), str(r[2] or ""), dict(r[3] or {}), float(r[4] or 0.0)) for r in rows]


def keyword_search(conn: "psycopg.Connection", query: str, *, types: Sequence[str], k: int) -> list[Hit]:
    """Full-text match (web-search syntax: quotes, OR, -exclude), best ts_rank_cd first."""
    rows = conn.execute(
        """
        SELECT c.id, c.type, c.content, c.metadata, ts_rank_cd(c.tsv, q) AS score
        FROM chunks c, websearch_to_tsquery('english', %s) q
        WHERE c.tsv @@ q AND c.type = ANY(%s)
        ORDER BY score DESC, c.id
        LIMIT %s
        """,
        (query, list(types), k),
    ).fetchall()
    return _hits(rows)


_VECTOR_VERSION: dict[str, tuple[int, ...]] = {}


def _pgvector_version(conn: "psycopg.Connection") -> tuple[int, ...]:
    key = conn.info.dsn
    if key not in _VECTOR_VERSION:
        row = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
        parts = re.findall(r"\d+", str(row[0])) if row else []
        _VECTOR_VERSION[key] = tuple(int(p) for p in parts)
    return _VECTOR_VERSION[key]


def vector_search(conn: "psycopg.Connection", embedding: Any, *, types: Sequence[str], k: int) -> list[Hit]:
    """Nearest chunks by cosine distance; score is cosine similarity."""
    # The HNSW index is searched before the type filter applies, so widen the candidate
    # list, and on pgvector >= 0.8 let the scan continue until k rows pass the filter.
    conn.execute(f"SET LOCAL hnsw.ef_search = {max(100, min(1000, k * 10))}")
    if _pgvector_version(conn) >= (0, 8):
        conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
    rows = conn.execute(
        """
        SELECT * FROM (
            SELECT c.id, c.type, c.content, c.metadata, 1 - (c.embedding <=> %s) AS score
            FROM chunks c
            WHERE c.embedding IS NOT NULL AND c.type = ANY(%s)
            ORDER BY c.embedding <=> %s
            LIMIT %s
        ) nearest
        ORDER BY score DESC, id
        """,
        (embedding, list(types), embedding, k),
    ).fetchall()
    return _hits(rows)


def rrf_fuse(rankings: Sequence[Sequence[Hit]], k: int, *, rrf_k: int = RRF_K) -> list[Hit]:
    """Reciprocal Rank Fusion: each list adds 1 / (rrf_k + rank) for every hit it holds."""
    fused: dict[str, Hit] = {}
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            fused.setdefault(hit.id, hit)
            scores[hit.id] = scores.get(hit.id, 0.0) + 1.0 / (rrf_k + rank)
    ordered = sorted(scores, key=lambda i: (-scores[i], i))[:k]
    return [Hit(fused[i].id, fused[i].type, fused[i].content, fused[i].metadata, scores[i]) for i in ordered]


def _search(conn: "psycopg.Connection", query: str, mode: str, embedding: Any,
            types: Sequence[str], k: int) -> list[Hit]:
    if k <= 0:
        return []
    if mode == "keyword":
        return keyword_search(conn, query, types=types, k=k)
    if mode == "vector":
        return vector_search(conn, embedding, types=types, k=k)
    wide = k * HYBRID_CANDIDATES_PER_RESULT
    return rrf_fuse(
        [vector_search(conn, embedding, types=types, k=wide), keyword_search(conn, query, types=types, k=wide)],
        k,
    )


def retrieve(
    query: str,
    *,
    mode: str = "vector",
    max_segments: int,
    max_summaries: int,
    dsn: str | None = None,
) -> tuple[list["Document"], list["Document"]]:
    """(summaries, segments) for a query, split by chunk type like the Chroma helpers."""
    if mode not in SEARCH_MODES:
        raise ValueError(f"mode must be one of {SEARCH_MODES}, got {mode!r}")
    if not query or (max_segments <= 0 and max_summaries <= 0):
        return [], []
    embedding = embed_query(query) if mode in ("vector", "hybrid") else None
    with read_only(dsn) as conn:
        summaries = _search(conn, query, mode, embedding, DEFAULT_SUMMARY_TYPES, max_summaries)
        segments = _search(conn, query, mode, embedding, DEFAULT_SEGMENT_TYPES, max_segments)
    return [h.to_document() for h in summaries], [h.to_document() for h in segments]


def fetch_documents(
    source_id: str,
    *,
    max_segments: int,
    max_summaries: int,
    dsn: str | None = None,
) -> tuple[list["Document"], list["Document"]]:
    """Chunks of one source: a documents.doc_id, a Substack/YouTube doc_id, or a podcast episode_id.

    Segments come back in transcript order (start_time), not by similarity.
    """
    ids = list(dict.fromkeys([source_id, source_id.replace(" ", "")]))
    sql = """
        SELECT c.id, c.type, c.content, c.metadata, 0.0
        FROM chunks c
        WHERE c.type = ANY(%s)
          AND (c.doc_id = ANY(%s) OR c.metadata->>'doc_id' = ANY(%s) OR c.metadata->>'episode_id' = ANY(%s))
        ORDER BY CASE WHEN c.metadata->>'start_time' ~ '^[0-9]+(\\.[0-9]+)?$'
                      THEN (c.metadata->>'start_time')::float8 END NULLS LAST, c.id
        LIMIT %s
    """
    with read_only(dsn) as conn:
        summaries = _hits(conn.execute(sql, (DEFAULT_SUMMARY_TYPES, ids, ids, ids, max(max_summaries, 0))).fetchall())
        segments = _hits(conn.execute(sql, (DEFAULT_SEGMENT_TYPES, ids, ids, ids, max(max_segments, 0))).fetchall())
    return [h.to_document() for h in summaries], [h.to_document() for h in segments]


# --- graph -------------------------------------------------------------------------


def parse_agtype(value: Any) -> Any:
    """agtype text (`{...}::vertex`, `[...]::path`, `"x"`) -> Python values."""
    if value is None:
        return None
    text = _AGTYPE_SUFFIX.sub("", str(value))
    try:
        return json.loads(text)
    except ValueError:
        return text


def _clip(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + " ...[truncated]..."
    if isinstance(value, list):
        return [_clip(v, limit) for v in value]
    if isinstance(value, dict):
        return {k: _clip(v, limit) for k, v in value.items()}
    return value


def _entity(vertex: Any, limit: int) -> dict[str, Any]:
    """A vertex as {label, name, properties}, without bulky or internal properties."""
    if not isinstance(vertex, dict):
        return {"value": vertex}
    props = dict(vertex.get("properties") or {})
    for internal in ("labels", "neo4j_id", "embedding"):
        props.pop(internal, None)
    name = props.pop("id", None) or props.get("name") or props.get("title")
    return {"label": vertex.get("label"), "name": name, "properties": _clip(props, limit)}


def _cypher(conn: "psycopg.Connection", graph: str, query: str, columns: Sequence[str],
            params: dict[str, Any]) -> list[list[Any]]:
    """Run one fixed template. `query` is always a constant from this module."""
    from psycopg import sql

    stmt = sql.SQL("SELECT * FROM ag_catalog.cypher({}, $$ " + query + " $$, %s) AS ({})").format(
        sql.Literal(graph),
        sql.SQL(", ").join(sql.SQL("{} ag_catalog.agtype").format(sql.Identifier(c)) for c in columns),
    )
    rows = conn.execute(stmt, (json.dumps(params),)).fetchall()
    return [[parse_agtype(v) for v in row] for row in rows]


def graph_labels(conn: "psycopg.Connection", graph: str = pg_store.GRAPH_NAME) -> dict[str, str]:
    """{label name: 'v' | 'e'} for the graph's user labels."""
    rows = conn.execute(
        "SELECT l.name, l.kind FROM ag_catalog.ag_label l JOIN ag_catalog.ag_graph g ON g.graphid = l.graph "
        "WHERE g.name = %s AND l.name NOT LIKE '\\_ag\\_%%'",
        (graph,),
    ).fetchall()
    return {str(r[0]): str(r[1]) for r in rows}


def graph_query(
    template: str,
    *,
    entity: str = "",
    other_entity: str = "",
    label: str = "",
    max_hops: int = 2,
    limit: int = 10,
    snippet_limit: int = 900,
    dsn: str | None = None,
    graph: str = pg_store.GRAPH_NAME,
) -> dict[str, Any]:
    """Run one read-only graph template. Entity matching ignores case.

    - schema: node labels and relationship types, with counts
    - find_entities: entities whose name contains `entity` (optionally only `label`)
    - neighbors: everything one relationship away from the entity named `entity`
    - connections: paths of up to `max_hops` relationships between `entity` and `other_entity`
    - mentions: source passages (Document nodes) that mention the entity named `entity`
    """
    if template not in GRAPH_TEMPLATES:
        raise ValueError(f"template must be one of {GRAPH_TEMPLATES}, got {template!r}")
    from psycopg import sql

    limit = max(1, min(int(limit), 50))
    with read_only(dsn) as conn:
        labels = graph_labels(conn, graph)
        if template == "schema":
            counts = []
            for name, kind in sorted(labels.items()):
                row = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(graph, name))).fetchone()
                counts.append({"name": name, "kind": "node" if kind == "v" else "relationship",
                               "count": int(row[0]) if row else 0})
            return {"template": template, "rows": counts}
        if not entity.strip():
            raise ValueError(f"{template} needs `entity`")
        params = {"q": entity.strip(), "limit": limit}
        if template == "find_entities":
            match = "(n)"
            if label:
                if labels.get(label) != "v" or not _LABEL_NAME.match(label):
                    raise ValueError(f"unknown node label {label!r}; see the schema template")
                match = f"(n:{label})"
            rows = _cypher(conn, graph, f"MATCH {match} WHERE toLower(n.id) CONTAINS toLower($q) "
                           "RETURN n LIMIT $limit", ["n"], params)
            return {"template": template, "rows": [_entity(r[0], snippet_limit) for r in rows]}
        if template == "neighbors":
            rows = _cypher(
                conn, graph,
                "MATCH (n)-[r]-(m) WHERE toLower(n.id) = toLower($q) "
                "RETURN startNode(r) = n, type(r), m LIMIT $limit",
                ["outgoing", "relationship", "other"], params,
            )
            return {"template": template, "entity": entity, "rows": [
                {"direction": "out" if r[0] else "in", "relationship": r[1], "other": _entity(r[2], snippet_limit)}
                for r in rows
            ]}
        if template == "mentions":
            rows = _cypher(
                conn, graph,
                "MATCH (d:Document)-[:MENTIONS]->(n) WHERE toLower(n.id) = toLower($q) RETURN d LIMIT $limit",
                ["d"], params,
            ) if labels.get("Document") == "v" and labels.get("MENTIONS") == "e" else []
            return {"template": template, "entity": entity, "rows": [_entity(r[0], snippet_limit) for r in rows]}
        # connections
        if not other_entity.strip():
            raise ValueError("connections needs `other_entity`")
        hops = max(1, min(int(max_hops), MAX_GRAPH_HOPS))
        params["b"] = other_entity.strip()
        rows = _cypher(
            conn, graph,
            f"MATCH p = (a)-[*1..{hops}]-(b) WHERE toLower(a.id) = toLower($q) AND toLower(b.id) = toLower($b) "
            "RETURN p LIMIT $limit",
            ["p"], params,
        )
        paths = []
        for (path,) in rows:
            steps = path if isinstance(path, list) else []
            paths.append({
                "nodes": [_entity(s, snippet_limit) for s in steps[0::2]],
                "relationships": [s.get("label") for s in steps[1::2] if isinstance(s, dict)],
            })
        return {"template": template, "entity": entity, "other_entity": other_entity, "rows": paths}
