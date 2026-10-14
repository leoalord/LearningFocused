"""Postgres knowledge store adapter (pgvector + Apache AGE + full-text).

Thin connect/migrate helpers. Retrieval lives in `src/database/pg_search.py` and
sync/backfill in the pipeline modules, the same split as chroma_manager/index_chroma.

Connection: `PG_DSN` (e.g. postgresql://learningfocused_app:...@10.138.0.5:5432/learningfocused).
Cloud Run gets it from the `learningfocused-pg-dsn` secret; locally put it in `.env`.
The DSN names the non-superuser app role that `migrate --app-role` creates; the
`learningfocused` superuser is only for migrations and admin work over IAP.
"""

from __future__ import annotations

import argparse
import os
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from dotenv import load_dotenv

if TYPE_CHECKING:
    import psycopg

SCHEMA_PATH = Path(__file__).with_name("pg_schema.sql")
GRAPH_NAME = "knowledge"
EMBEDDING_DIMS = 1536
APP_ROLE_DEFAULT = "learningfocused_app"
# Set per connection (startup option), so a rollback can't drop ag_catalog from the path.
# AGE itself is preloaded by the server (shared_preload_libraries), so no LOAD is needed.
_SESSION_OPTIONS = '-c search_path=public,ag_catalog,"$user"'

load_dotenv()


def get_dsn() -> str:
    dsn = os.getenv("PG_DSN")
    if not dsn:
        raise RuntimeError("PG_DSN is not set (Postgres knowledge store DSN).")
    return dsn


def is_configured() -> bool:
    return bool(os.getenv("PG_DSN"))


def _register_vector(conn: "psycopg.Connection") -> None:
    """Register the pgvector adapter (numpy arrays <-> vector)."""
    import psycopg
    from pgvector.psycopg import register_vector

    try:
        register_vector(conn)
    except psycopg.ProgrammingError:
        # Before `migrate` creates the extension the vector type doesn't exist yet.
        pass


def connect(dsn: str | None = None, *, autocommit: bool = False) -> "psycopg.Connection":
    import psycopg

    conn = psycopg.connect(dsn or get_dsn(), autocommit=autocommit, options=_SESSION_OPTIONS)
    _register_vector(conn)
    return conn


@contextmanager
def session(dsn: str | None = None) -> Iterator["psycopg.Connection"]:
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


def ensure_graph(conn: "psycopg.Connection", graph: str = GRAPH_NAME) -> bool:
    """Create the AGE graph if missing. Returns True when it was created."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s", (graph,))
        if cur.fetchone():
            return False
        cur.execute("SELECT ag_catalog.create_graph(%s)", (graph,))
        return True


def ensure_app_role(conn: "psycopg.Connection", role: str, password: str, graph: str = GRAPH_NAME) -> None:
    """Create/refresh the non-superuser role the app connects as.

    It gets DML on the public tables, read access to AGE's catalog, and ownership of the
    graph's label tables (see grant_graph_ownership).
    """
    from psycopg import sql

    ident = sql.Identifier(role)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        verb = "ALTER" if cur.fetchone() else "CREATE"
        cur.execute(
            sql.SQL(verb + " ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                ident, sql.Literal(password)
            )
        )
        cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(conn.info.dbname), ident))
        for stmt in (
            "GRANT USAGE ON SCHEMA public TO {r}",
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {r}",
            "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {r}",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {r}",
            "GRANT USAGE ON SCHEMA ag_catalog TO {r}",
            "GRANT SELECT ON ALL TABLES IN SCHEMA ag_catalog TO {r}",
            "GRANT USAGE, CREATE ON SCHEMA {g} TO {r}",
            "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA {g} TO {r}",
        ):
            cur.execute(sql.SQL(stmt).format(r=ident, g=sql.Identifier(graph)))
    grant_graph_ownership(conn, role, graph)


def grant_graph_ownership(conn: "psycopg.Connection", role: str, graph: str = GRAPH_NAME) -> None:
    """Make `role` own every label table and sequence of the graph.

    AGE adds a label as a child table of the graph's base tables, which only their owner
    may do, so this runs again after an admin-side import creates new labels.
    """
    from psycopg import sql

    with conn.cursor() as cur:
        cur.execute(
            # Sequences owned by a label table (OWNED BY) follow their table automatically.
            "SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND (c.relkind = 'r' OR (c.relkind = 'S' AND NOT EXISTS ("
            "SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'a')))",
            (graph,),
        )
        for relname, relkind in cur.fetchall():
            kind = "TABLE" if relkind == "r" else "SEQUENCE"
            cur.execute(sql.SQL("ALTER " + kind + " {}.{} OWNER TO {}").format(
                sql.Identifier(graph), sql.Identifier(relname), sql.Identifier(role)))


def migrate(dsn: str | None = None, *, app_role: str | None = None,
            app_password: str | None = None) -> dict[str, object]:
    """Apply pg_schema.sql, create the graph and (optionally) the app role. Idempotent."""
    if app_role and not app_password:
        raise ValueError("app_role needs app_password (set PG_APP_PASSWORD).")
    sql_text = SCHEMA_PATH.read_text(encoding="utf-8")
    with connect(dsn, autocommit=True) as conn:
        conn.execute(sql_text)
        _register_vector(conn)
        created = ensure_graph(conn)
        if app_role:
            ensure_app_role(conn, app_role, app_password or "")
    return {"schema": str(SCHEMA_PATH.name), "graph": GRAPH_NAME, "graph_created": created,
            "app_role": app_role}


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
                row = cur.fetchone()
                counts[table] = int(row[0]) if row else 0
    return {"extensions": extensions, "counts": counts}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m src.database.pg_store")
    p.add_argument("command", choices=["migrate", "status"])
    p.add_argument("--app-role", nargs="?", const=APP_ROLE_DEFAULT, default=None,
                   help=f"also create/refresh the app login role (default name {APP_ROLE_DEFAULT}); "
                        "password from PG_APP_PASSWORD")
    args = p.parse_args(argv)
    if args.command == "migrate":
        print(migrate(app_role=args.app_role, app_password=os.getenv("PG_APP_PASSWORD")))
    else:
        print(status())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
