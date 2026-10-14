"""Throwaway Postgres databases for tests that need pgvector + AGE.

Set PG_TEST_DSN to a server where the test role may CREATE DATABASE and where the
`vector` and `age` extensions are installed (e.g. postgresql://postgres@localhost:5432/postgres).
Without it, Postgres-backed tests are skipped.
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit, urlunsplit

PG_TEST_DSN = os.getenv("PG_TEST_DSN")
SKIP_REASON = "PG_TEST_DSN not set (needs Postgres with pgvector + AGE)"


def _with_dbname(dsn: str, dbname: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


class TempDatabase:
    """Create a uniquely named database on enter, drop it on exit."""

    def __init__(self) -> None:
        if not PG_TEST_DSN:
            raise RuntimeError(SKIP_REASON)
        self.name = f"lf_test_{uuid.uuid4().hex[:12]}"
        self.dsn = _with_dbname(PG_TEST_DSN, self.name)

    def __enter__(self) -> "TempDatabase":
        import psycopg

        with psycopg.connect(PG_TEST_DSN, autocommit=True) as admin:
            admin.execute(f'CREATE DATABASE "{self.name}"')
        return self

    def __exit__(self, *exc: object) -> None:
        import psycopg

        with psycopg.connect(PG_TEST_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)')
