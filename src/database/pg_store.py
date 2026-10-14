"""Postgres knowledge store adapter (pgvector + Apache AGE + full-text).

Thin connect/migrate helpers. Retrieval lives in `src/database/pg_search.py` and
sync/backfill in the pipeline modules, the same split as chroma_manager/index_chroma.

Connection: `PG_DSN` (e.g. postgresql://learningfocused:...@10.138.0.5:5432/learningfocused).
Cloud Run gets it from the `learningfocused-pg-dsn` secret; locally put it in `.env`.
"""

from __future__ import annotations

import argparse
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv

SCHEMA_PATH = Path(__file__).with_name("pg_schema.sql")
GRAPH_NAME = "knowledge"
EMBEDDING_DIMS = 1536

load_dotenv()


def get_dsn() -> str:
    dsn = os.getenv("PG_DSN")
    if not dsn:
        raise RuntimeError("PG_DSN is not set (Postgres knowledge store DSN).")
    return dsn


def is_configured() -> bool:
    return bool(os.getenv("PG_DSN"))


def _prepare_session(conn) -> None:
    """Load AGE for this session and register the pgvector type adapter."""
    with conn.cursor() as cur:
        cur.execute("LOAD 'age'")
        cur.execute('SET search_path = public, ag_catalog, "$user"')
    try:
        from pgvector.psycopg import register_vector

        register_vector(conn)
    except Exception:  # noqa: BLE001
        # Before `migrate` the vector type may not exist yet.
        pass


def connect(dsn: str | None = None, *, autocommit: bool = False):
    import psycopg

    conn = psycopg.connect(dsn or get_dsn(), autocommit=autocommit)
    _prepare_session(conn)
    return conn


@contextmanager
def session(dsn: str | None = None) -> Iterator["object"]:
    """Connection that commits on success and rolls back on error."""
    conn = connect(dsn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_graph(conn, graph: str = GRAPH_NAME) -> bool:
    """Create the AGE graph if missing. Returns True when it was created."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s", (graph,))
        if cur.fetchone():
            return False
        cur.execute("SELECT ag_catalog.create_graph(%s)", (graph,))
        return True


def migrate(dsn: str | None = None) -> dict[str, object]:
    """Apply pg_schema.sql and create the graph. Idempotent."""
    import psycopg

    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with psycopg.connect(dsn or get_dsn(), autocommit=True) as conn:
        conn.execute(sql)
        _prepare_session(conn)
        created = ensure_graph(conn)
    return {"schema": str(SCHEMA_PATH.name), "graph": GRAPH_NAME, "graph_created": created}


def status(dsn: str | None = None) -> dict[str, object]:
    with session(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT extname, extversion FROM pg_extension WHERE extname IN ('vector', 'age') ORDER BY extname"
            )
            extensions = dict(cur.fetchall())
            counts: dict[str, int] = {}
            for table in ("sources", "documents", "chunks", "ingest_runs"):
                cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed table names
                counts[table] = int(cur.fetchone()[0])
    return {"extensions": extensions, "counts": counts}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m src.database.pg_store")
    p.add_argument("command", choices=["migrate", "status"])
    args = p.parse_args(argv)
    if args.command == "migrate":
        print(migrate())
    else:
        print(status())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
