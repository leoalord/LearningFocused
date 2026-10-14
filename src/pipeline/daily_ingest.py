"""Daily ingest orchestrator (TASK-19).

One entrypoint checks every feed in the registry (src/feeds/sources.json) that is
due today: Art19 podcast RSS, Substack, the whole @future_of_education YouTube
channel (new uploads, deduplicated against podcast episodes), and social accounts
once they are configured. Per-source failures are isolated: one raising does not
abort the others. Every run writes a ledger (local + GCS) and, when PG_DSN is set,
rows in ingest_runs / source_checks. After ingest, artifacts + chroma_db are
checksum-synced to the private bucket and the public MCP Cloud Run service is rolled
so warm instances rehydrate. Neo4j is skipped. Existing files are not re-transcribed.
The MCP service is not scaled to zero.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from dotenv import load_dotenv

from src.config import LEDGER_DIR, RSS_FEED_URL, SUBSTACK_FEED_URL, ensure_data_dirs
from src.feeds import state as feed_state
from src.feeds.registry import SOCIAL_KINDS, SourceConfig, due_sources, load_sources
from src.gcs_corpus import (
    LEDGER_PREFIX,
    default_bucket,
    default_project,
    hydrate_artifacts,
    json_safe,
    upload_artifacts,
    upload_bytes,
)

SOURCE_ART19 = "art19"
SOURCE_SUBSTACK = "substack"
SOURCE_YOUTUBE = "youtube"
MCP_SERVICE_DEFAULT = "learningfocused-mcp"
MCP_REGION_DEFAULT = "us-west1"
DEFAULT_ART19_DOWNLOAD_LIMIT = 5
DEFAULT_SUBSTACK_INGEST_LIMIT = 10
DEFAULT_YOUTUBE_MAX_NEW = 10
CHROMA_SNAPSHOT_ENV = "CHROMA_SNAPSHOT_AT"

SourceRunner = Callable[[], dict[str, Any]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def _run_id(ts: datetime) -> str:
    return ts.strftime("%Y%m%dT%H%M%SZ")


def run_art19(download_limit: int = DEFAULT_ART19_DOWNLOAD_LIMIT) -> dict[str, Any]:
    """Art19 / podcast RSS. Skip Neo4j. Do not --reset-chroma."""
    from src.pipeline.audio.run import main as audio_main

    limit = os.getenv("DAILY_ART19_DOWNLOAD_LIMIT", str(download_limit))
    argv = ["--skip-neo4j", "--", "--mode", "daily", "--download-limit", str(limit)]
    print(f"\n=== SOURCE art19 ===\npython -m src.pipeline.audio.run {' '.join(argv)}")
    audio_main(argv)
    return {"argv": argv}


def run_substack(ingest_limit: int = DEFAULT_SUBSTACK_INGEST_LIMIT) -> dict[str, Any]:
    """Substack daily ingest. Skip Neo4j. Do not --reset-chroma."""
    from src.pipeline.substack.run import main as substack_main

    argv = ["--skip-neo4j", "--", "--mode", "daily", "--ingest-limit", str(ingest_limit)]
    print(f"\n=== SOURCE substack ===\npython -m src.pipeline.substack.run {' '.join(argv)}")
    substack_main(argv)
    return {"argv": argv}


def run_youtube(max_new: int = DEFAULT_YOUTUBE_MAX_NEW) -> dict[str, Any]:
    """Whole-channel YouTube feed (videos tab + listed playlists) with skip.py dedupe.

    No --force. No --reset-chroma. At most `max_new` new videos per run.
    """
    from src.pipeline.youtube.run import main as youtube_main

    argv = ["--feed", "--max-new", str(max_new)]
    print(f"\n=== SOURCE youtube ===\npython -m src.pipeline.youtube.run {' '.join(argv)}")
    code = youtube_main(argv)
    if code not in (0, 1):
        raise RuntimeError(f"youtube.run exited {code} (refused or unexpected)")
    return {"argv": argv, "exit_code": code}


# Used only when sources.json can't be read, so a bad edit never stops the core feeds.
FALLBACK_REGISTRY = [
    SourceConfig(key=SOURCE_ART19, kind="podcast_rss", name="Art19", url=RSS_FEED_URL),
    SourceConfig(key=SOURCE_SUBSTACK, kind="substack", name="Substack", url=SUBSTACK_FEED_URL),
    SourceConfig(key=SOURCE_YOUTUBE, kind="youtube_channel", name="YouTube"),
]


def _int_config(source: SourceConfig, name: str, default: int) -> int:
    value = source.config.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"source {source.key}: config.{name} must be a non-negative integer, got {value!r}")
    return value


def _require_builtin_url(source: SourceConfig, builtin: str) -> None:
    # The audio and Substack pipelines read their feed URL from src.config; a second
    # podcast or Substack needs those pipelines parameterised first.
    if source.url and source.url.rstrip("/") != builtin.rstrip("/"):
        raise NotImplementedError(
            f"source {source.key}: only {builtin} is supported for kind {source.kind} today"
        )


def runner_for(source: SourceConfig) -> SourceRunner:
    """Build the runner for one registry entry. Config errors raise when it runs,
    so they land in that source's ledger entry instead of stopping the job."""

    def run_source() -> dict[str, Any]:
        if source.kind == "podcast_rss":
            _require_builtin_url(source, RSS_FEED_URL)
            return run_art19(_int_config(source, "download_limit", DEFAULT_ART19_DOWNLOAD_LIMIT))
        if source.kind == "substack":
            _require_builtin_url(source, SUBSTACK_FEED_URL)
            return run_substack(_int_config(source, "ingest_limit", DEFAULT_SUBSTACK_INGEST_LIMIT))
        if source.kind == "youtube_channel":
            return run_youtube(_int_config(source, "max_new_per_run", DEFAULT_YOUTUBE_MAX_NEW))
        if source.kind in SOCIAL_KINDS:
            from src.feeds.social import run_social

            return run_social(source)
        raise NotImplementedError(f"source {source.key}: no runner for kind {source.kind}")

    return run_source


def _pg_step(ledger: dict[str, Any], name: str, fn: Callable[[], Any]) -> Any:
    """Postgres bookkeeping never blocks ingest: record the outcome and carry on."""
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001
        ledger["pg_state"][name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        ledger["errors"].append(f"pg_state.{name}: {exc}")
        print(f"WARNING: Postgres feed state ({name}) failed: {exc}")
        return None
    ledger["pg_state"][name] = {"ok": True}
    return result


def _load_registry(ledger: dict[str, Any]) -> list[SourceConfig]:
    try:
        return load_sources()
    except Exception as exc:  # noqa: BLE001
        ledger["errors"].append(f"registry: {type(exc).__name__}: {exc}")
        print(f"WARNING: could not read sources.json, using the built-in feeds: {exc}")
        return list(FALLBACK_REGISTRY)


def _write_ledger_local(payload: dict[str, Any], run_id: str) -> Path:
    ensure_data_dirs()
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    body = json.dumps(json_safe(payload), indent=2, ensure_ascii=False) + "\n"
    latest = LEDGER_DIR / "latest.json"
    dated = LEDGER_DIR / f"{run_id}.json"
    latest.write_text(body, encoding="utf-8")
    dated.write_text(body, encoding="utf-8")
    print(f"Ledger written: {dated}")
    return dated


def _write_ledger_gcs(payload: dict[str, Any], run_id: str) -> dict[str, str]:
    body = json.dumps(json_safe(payload), indent=2, ensure_ascii=False).encode("utf-8")
    prefix = LEDGER_PREFIX.strip("/")
    uris = {
        "dated": upload_bytes(object_name=f"{prefix}/{run_id}.json", data=body),
        "latest": upload_bytes(object_name=f"{prefix}/latest.json", data=body),
    }
    return uris


def persist_ledger(payload: dict[str, Any], run_id: str, *, to_gcs: bool) -> dict[str, Any]:
    local = _write_ledger_local(payload, run_id)
    out: dict[str, Any] = {"local": str(local)}
    if to_gcs:
        try:
            out["gcs"] = _write_ledger_gcs(payload, run_id)
        except Exception as exc:  # noqa: BLE001
            out["gcs_error"] = f"{type(exc).__name__}: {exc}"
            print(f"WARNING: ledger GCS upload failed: {exc}")
    return out


def _run_isolated(name: str, fn: SourceRunner) -> dict[str, Any]:
    started = _utc_now()
    t0 = time.monotonic()
    try:
        detail = fn() or {}
        return {
            "ok": True,
            "started_at": _iso(started),
            "finished_at": _iso(_utc_now()),
            "duration_s": round(time.monotonic() - t0, 3),
            "detail": json_safe(detail),
        }
    except SystemExit as exc:
        code = exc.code
        if code in (0, None):
            return {
                "ok": True,
                "started_at": _iso(started),
                "finished_at": _iso(_utc_now()),
                "duration_s": round(time.monotonic() - t0, 3),
                "detail": {"system_exit": 0},
            }
        return {
            "ok": False,
            "started_at": _iso(started),
            "finished_at": _iso(_utc_now()),
            "duration_s": round(time.monotonic() - t0, 3),
            "error": f"SystemExit({code!r})",
            "traceback": traceback.format_exc(),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "started_at": _iso(started),
            "finished_at": _iso(_utc_now()),
            "duration_s": round(time.monotonic() - t0, 3),
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }


def roll_mcp_service(
    *,
    project: str | None = None,
    region: str | None = None,
    service: str | None = None,
    snapshot_at: str,
) -> dict[str, Any]:
    """Force a new MCP revision so hydrate re-runs. Does not scale to zero."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    project_id = project or default_project()
    region_id = region or os.getenv("MCP_REGION") or os.getenv("GCP_REGION") or MCP_REGION_DEFAULT
    service_id = (
        service
        or os.getenv("MCP_SERVICE")
        or os.getenv("CLOUD_RUN_SERVICE")
        or MCP_SERVICE_DEFAULT
    )

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if hasattr(credentials, "with_quota_project"):
        credentials = credentials.with_quota_project(project_id)
    session = AuthorizedSession(credentials)
    url = f"https://run.googleapis.com/v2/projects/{project_id}/locations/{region_id}/services/{service_id}"
    got = session.get(url, timeout=60)
    if not got.ok:
        raise RuntimeError(f"MCP GET {got.status_code}: {got.text[:2000]}")
    body = got.json()

    scaling = (body.get("scaling") or {})
    template_scaling = (body.get("template") or {}).get("scaling") or {}
    min_count = scaling.get("minInstanceCount", template_scaling.get("minInstanceCount"))
    try:
        min_n = int(min_count) if min_count is not None else 1
    except (TypeError, ValueError):
        min_n = 1
    if min_n < 1:
        raise RuntimeError(
            f"Refusing to roll {service_id}: minInstanceCount={min_count!r} would scale MCP to zero"
        )

    containers = (body.get("template") or {}).get("containers") or []
    if not containers:
        raise RuntimeError(f"Service {service_id} has no containers")
    env = list(containers[0].get("env") or [])
    found = False
    for item in env:
        if item.get("name") == CHROMA_SNAPSHOT_ENV:
            item["value"] = snapshot_at
            item.pop("valueSource", None)
            found = True
            break
    if not found:
        env.append({"name": CHROMA_SNAPSHOT_ENV, "value": snapshot_at})
    containers[0]["env"] = env

    patch = {"template": {"containers": containers}}
    patched = session.patch(
        url,
        params={"updateMask": "template.containers"},
        json=patch,
        timeout=120,
    )
    if not patched.ok:
        raise RuntimeError(f"MCP PATCH {patched.status_code}: {patched.text[:2000]}")
    operation = patched.json()
    op_name = operation.get("name")
    if op_name and not operation.get("done"):
        _wait_run_operation(session, op_name, timeout_s=600)

    refreshed = session.get(url, timeout=60)
    refreshed.raise_for_status()
    latest = refreshed.json()
    revision = latest.get("latestReadyRevision") or latest.get("latestCreatedRevision")
    print(f"Rolled {service_id}: latestReadyRevision={revision} {CHROMA_SNAPSHOT_ENV}={snapshot_at}")
    return {
        "service": service_id,
        "region": region_id,
        "project": project_id,
        "revision": revision,
        "snapshot_at": snapshot_at,
        "min_instance_count": min_n,
    }


def _wait_run_operation(session: Any, op_name: str, *, timeout_s: int) -> None:
    url = f"https://run.googleapis.com/v2/{op_name}" if not str(op_name).startswith("http") else op_name
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = session.get(url, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("done"):
            if payload.get("error"):
                raise RuntimeError(f"Cloud Run operation failed: {payload['error']}")
            return
        time.sleep(5)
    raise TimeoutError(f"Timed out waiting for {op_name}")


def run(
    *,
    sources: Sequence[str] | None = None,
    skip: Sequence[str] = (),
    hydrate: bool = True,
    sync: bool = True,
    roll: bool = True,
    dry_run: bool = False,
    source_runners: dict[str, SourceRunner] | None = None,
    registry: list[SourceConfig] | None = None,
) -> dict[str, Any]:
    """Run `sources` (registry keys), or with sources=None every registry source due today.

    `source_runners` replaces the registry runners (tests). With PG_DSN set, the registry
    is mirrored to Postgres, weekly sources use their last check from there, and the run
    is recorded; none of that can fail the run.
    """
    load_dotenv()
    ensure_data_dirs()
    started = _utc_now()
    run_id = _run_id(started)
    # Ledger goes to GCS when this process is already talking to the bucket.
    write_gcs = (not dry_run) and (hydrate or sync)
    use_pg = (not dry_run) and feed_state.is_enabled()

    ledger: dict[str, Any] = {
        "run_id": run_id,
        "started_at": _iso(started),
        "finished_at": None,
        "ok": False,
        "dry_run": dry_run,
        "project": default_project(),
        "bucket": default_bucket(),
        "schedule_note": "Cloud Scheduler: 0 8 * * * America/Los_Angeles (learningfocused-ingest-daily)",
        "hydrate": None,
        "sources": {},
        "gcs_sync": None,
        "mcp_roll": None,
        "pg_state": {},
        "errors": [],
    }

    if registry is None:
        registry = _load_registry(ledger)
    by_key = {s.key: s for s in registry}
    if sources is None:
        last_checked: dict[str, datetime] = {}
        if use_pg:
            _pg_step(ledger, "sync_registry", lambda: feed_state.sync_registry(registry or []))
            last_checked = _pg_step(ledger, "last_checked", feed_state.last_checked) or {}
        due = due_sources(registry, now=started, last_checked=last_checked)
        sources = [s.key for s in due]
    wanted = tuple(name for name in sources if name not in set(skip))
    runners: dict[str, SourceRunner] = (
        dict(source_runners) if source_runners is not None
        else {key: runner_for(src) for key, src in by_key.items()}
    )
    ledger["sources"] = {name: {"ok": None} for name in wanted}
    persist_ledger(ledger, run_id, to_gcs=write_gcs)

    if hydrate and not dry_run:
        try:
            ledger["hydrate"] = {"ok": True, "counts": hydrate_artifacts()}
        except Exception as exc:  # noqa: BLE001
            ledger["hydrate"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            ledger["errors"].append(f"hydrate: {exc}")
            print(f"Hydrate failed; skipping sources to avoid re-downloading the audio dump: {exc}")
            for name in wanted:
                ledger["sources"][name] = {
                    "ok": False,
                    "skipped": True,
                    "error": "hydrate_failed",
                }
            ledger["finished_at"] = _iso(_utc_now())
            if use_pg:
                _pg_step(ledger, "record_run", lambda: feed_state.record_run(ledger))
            persist_ledger(ledger, run_id, to_gcs=True)
            return ledger
    elif dry_run:
        ledger["hydrate"] = {"ok": True, "dry_run": True}
    else:
        ledger["hydrate"] = {"ok": True, "skipped": True}

    persist_ledger(ledger, run_id, to_gcs=write_gcs)

    for name in wanted:
        print(f"\n----- isolated source: {name} -----")
        if dry_run:
            ledger["sources"][name] = {"ok": True, "dry_run": True}
        else:
            fn = runners.get(name)
            if fn is None:
                ledger["sources"][name] = {"ok": False, "error": f"unknown source {name}"}
            else:
                ledger["sources"][name] = _run_isolated(name, fn)
        persist_ledger(ledger, run_id, to_gcs=write_gcs)

    if sync and not dry_run:
        try:
            ledger["gcs_sync"] = {"ok": True, "prefixes": upload_artifacts()}
        except Exception as exc:  # noqa: BLE001
            ledger["gcs_sync"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            ledger["errors"].append(f"gcs_sync: {exc}")
            print(f"GCS sync failed: {exc}")
    elif dry_run:
        ledger["gcs_sync"] = {"ok": True, "dry_run": True}
    else:
        ledger["gcs_sync"] = {"ok": True, "skipped": True}

    persist_ledger(ledger, run_id, to_gcs=write_gcs)

    sync_ok = bool(ledger.get("gcs_sync") and ledger["gcs_sync"].get("ok"))
    if roll and not dry_run and sync_ok:
        try:
            ledger["mcp_roll"] = {
                "ok": True,
                **roll_mcp_service(snapshot_at=run_id),
            }
        except Exception as exc:  # noqa: BLE001
            ledger["mcp_roll"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            ledger["errors"].append(f"mcp_roll: {exc}")
            print(f"MCP roll failed: {exc}")
    elif dry_run:
        ledger["mcp_roll"] = {"ok": True, "dry_run": True}
    elif not sync_ok:
        ledger["mcp_roll"] = {"ok": False, "skipped": True, "reason": "gcs_sync_failed"}
    else:
        ledger["mcp_roll"] = {"ok": True, "skipped": True}

    source_ok = all(bool((ledger["sources"].get(n) or {}).get("ok")) for n in wanted)
    roll_ok = bool(ledger.get("mcp_roll") and ledger["mcp_roll"].get("ok"))
    ledger["ok"] = bool(source_ok and sync_ok and roll_ok and (ledger.get("hydrate") or {}).get("ok"))
    ledger["finished_at"] = _iso(_utc_now())
    if use_pg:
        _pg_step(ledger, "record_run", lambda: feed_state.record_run(ledger))
    persist_ledger(ledger, run_id, to_gcs=write_gcs)
    print(json.dumps({"run_id": run_id, "ok": ledger["ok"], "sources": {
        n: {"ok": (ledger["sources"][n] or {}).get("ok")} for n in wanted
    }}, indent=2))
    return ledger


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.pipeline.daily_ingest",
        description="Check every due feed in src/feeds/sources.json (isolated), then GCS sync + MCP roll.",
    )
    p.add_argument("--source", action="append", dest="only", metavar="KEY",
                   help="Run only this registry key, due or not (repeatable).")
    p.add_argument("--dry-run", action="store_true", help="Walk sources without calling pipelines, GCS, or MCP roll.")
    p.add_argument("--skip-hydrate", action="store_true", help="Do not pull chroma/artifacts from GCS (local laptop).")
    p.add_argument("--skip-sync", action="store_true", help="Do not upload artifacts/chroma to GCS.")
    p.add_argument("--skip-roll", action="store_true", help="Do not roll the public MCP Cloud Run service.")
    p.add_argument("--skip-art19", action="store_true")
    p.add_argument("--skip-substack", action="store_true")
    p.add_argument("--skip-youtube", action="store_true")
    p.add_argument("--skip", action="append", default=[], metavar="KEY", help="Skip this registry key (repeatable).")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    skip = list(args.skip)
    for flag, key in ((args.skip_art19, SOURCE_ART19), (args.skip_substack, SOURCE_SUBSTACK),
                      (args.skip_youtube, SOURCE_YOUTUBE)):
        if flag:
            skip.append(key)
    if args.only and all(key in skip for key in args.only):
        print("No sources selected.")
        return 2
    ledger = run(
        sources=args.only,
        skip=skip,
        hydrate=not args.skip_hydrate,
        sync=not args.skip_sync,
        roll=not args.skip_roll,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        return 0
    return 0 if ledger.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
