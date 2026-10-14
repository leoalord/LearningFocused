"""Feed state in Postgres: mirror the registry, read last checks, record each run.

Every function is a no-op when PG_DSN is unset, and the daily job treats any error
here as non-fatal (logged in the ledger) so bookkeeping never blocks ingest.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from src.feeds.registry import SourceConfig


def is_enabled() -> bool:
    from src.database import pg_store

    return pg_store.is_configured()


def sync_registry(sources: list[SourceConfig], *, dsn: str | None = None) -> int:
    """Upsert every registry entry into `sources` (sources.json wins on conflict)."""
    if dsn is None and not is_enabled():
        return 0
    from src.database import pg_store

    with pg_store.session(dsn) as conn:
        for s in sources:
            row = s.as_row()
            conn.execute(
                """
                INSERT INTO sources (key, kind, name, url, cadence, enabled, config, updated_at)
                VALUES (%(key)s, %(kind)s, %(name)s, %(url)s, %(cadence)s, %(enabled)s, %(config)s::jsonb, now())
                ON CONFLICT (key) DO UPDATE SET
                    kind = EXCLUDED.kind, name = EXCLUDED.name, url = EXCLUDED.url,
                    cadence = EXCLUDED.cadence, enabled = EXCLUDED.enabled,
                    config = EXCLUDED.config, updated_at = now()
                """,
                {**row, "config": json.dumps(row["config"])},
            )
    return len(sources)


def last_checked(*, dsn: str | None = None) -> dict[str, datetime]:
    if dsn is None and not is_enabled():
        return {}
    from src.database import pg_store

    with pg_store.session(dsn) as conn:
        rows = conn.execute(
            "SELECT key, last_checked_at FROM sources WHERE last_checked_at IS NOT NULL"
        ).fetchall()
    return {str(k): ts for k, ts in rows}


def record_run(ledger: dict[str, Any], *, dsn: str | None = None) -> bool:
    """Write the run and one row per source to ingest_runs / source_checks."""
    if dsn is None and not is_enabled():
        return False
    from src.database import pg_store
    from src.gcs_corpus import json_safe

    run_id = ledger["run_id"]
    with pg_store.session(dsn) as conn:
        conn.execute(
            """
            INSERT INTO ingest_runs (run_id, started_at, finished_at, ok, detail)
            VALUES (%s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (run_id) DO UPDATE SET finished_at = EXCLUDED.finished_at,
                ok = EXCLUDED.ok, detail = EXCLUDED.detail
            """,
            (
                run_id,
                ledger["started_at"],
                ledger.get("finished_at"),
                ledger.get("ok"),
                json.dumps(json_safe({k: v for k, v in ledger.items() if k != "sources"})),
            ),
        )
        for key, result in (ledger.get("sources") or {}).items():
            result = result or {}
            detail = result.get("detail") or {}
            skipped = bool(result.get("skipped") or (isinstance(detail, dict) and detail.get("skipped")))
            conn.execute(
                """
                INSERT INTO source_checks (run_id, source_key, ok, skipped, error, detail)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (run_id, source_key) DO UPDATE SET ok = EXCLUDED.ok,
                    skipped = EXCLUDED.skipped, error = EXCLUDED.error, detail = EXCLUDED.detail
                """,
                (run_id, key, result.get("ok"), skipped, result.get("error"), json.dumps(json_safe(result))),
            )
            if result.get("dry_run"):
                continue
            conn.execute(
                """
                UPDATE sources SET last_checked_at = now(),
                    last_ok_at = CASE WHEN %(ok)s THEN now() ELSE last_ok_at END,
                    last_error = CASE WHEN %(ok)s THEN NULL ELSE %(error)s END
                WHERE key = %(key)s
                """,
                {"ok": bool(result.get("ok")), "error": result.get("error"), "key": key},
            )
    return True
