"""Daily ingest orchestrator (TASK-19).

One entrypoint walks Art19 RSS, Substack, and unique YouTube. Per-source
failures are isolated: one raising does not abort the others. Every run writes
a ledger (local + GCS). After ingest, artifacts + chroma_db are checksum-synced
to the same private bucket and the public MCP Cloud Run service is rolled so
warm instances rehydrate. Neo4j is skipped. Existing files are not re-transcribed.
The YouTube V1 cap is not raised. The MCP service is not scaled to zero.
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

from src.config import LEDGER_DIR, ensure_data_dirs
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
DEFAULT_SOURCES = (SOURCE_ART19, SOURCE_SUBSTACK, SOURCE_YOUTUBE)
MCP_SERVICE_DEFAULT = "learningfocused-mcp"
MCP_REGION_DEFAULT = "us-west1"
DEFAULT_ART19_DOWNLOAD_LIMIT = 5
CHROMA_SNAPSHOT_ENV = "CHROMA_SNAPSHOT_AT"

SourceRunner = Callable[[], dict[str, Any]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def _run_id(ts: datetime) -> str:
    return ts.strftime("%Y%m%dT%H%M%SZ")


def run_art19() -> dict[str, Any]:
    """Art19 / podcast RSS. Skip Neo4j. Do not --reset-chroma."""
    from src.pipeline.audio.run import main as audio_main

    limit = os.getenv("DAILY_ART19_DOWNLOAD_LIMIT", str(DEFAULT_ART19_DOWNLOAD_LIMIT))
    argv = ["--skip-neo4j", "--", "--mode", "daily", "--download-limit", str(limit)]
    print(f"\n=== SOURCE art19 ===\npython -m src.pipeline.audio.run {' '.join(argv)}")
    audio_main(argv)
    return {"argv": argv}


def run_substack() -> dict[str, Any]:
    """Substack daily ingest. Skip Neo4j. Do not --reset-chroma."""
    from src.pipeline.substack.run import main as substack_main

    argv = ["--skip-neo4j", "--", "--mode", "daily", "--ingest-limit", "10"]
    print(f"\n=== SOURCE substack ===\npython -m src.pipeline.substack.run {' '.join(argv)}")
    substack_main(argv)
    return {"argv": argv}


def run_youtube() -> dict[str, Any]:
    """Unique YouTube with existing skip.py + V1 cap. No --force. No --reset-chroma."""
    from src.pipeline.youtube.run import main as youtube_main

    argv: list[str] = []
    print("\n=== SOURCE youtube ===\npython -m src.pipeline.youtube.run")
    code = youtube_main(argv)
    if code not in (0, 1):
        raise RuntimeError(f"youtube.run exited {code} (refused or unexpected)")
    return {"argv": argv, "exit_code": code}


DEFAULT_RUNNERS: dict[str, SourceRunner] = {
    SOURCE_ART19: run_art19,
    SOURCE_SUBSTACK: run_substack,
    SOURCE_YOUTUBE: run_youtube,
}


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
    hydrate: bool = True,
    sync: bool = True,
    roll: bool = True,
    dry_run: bool = False,
    source_runners: dict[str, SourceRunner] | None = None,
) -> dict[str, Any]:
    load_dotenv()
    ensure_data_dirs()
    started = _utc_now()
    run_id = _run_id(started)
    wanted = tuple(sources or DEFAULT_SOURCES)
    runners = source_runners or DEFAULT_RUNNERS
    # Ledger goes to GCS when this process is already talking to the bucket.
    write_gcs = (not dry_run) and (hydrate or sync)

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
        "sources": {name: {"ok": None} for name in wanted},
        "gcs_sync": None,
        "mcp_roll": None,
        "errors": [],
    }
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
    persist_ledger(ledger, run_id, to_gcs=write_gcs)
    print(json.dumps({"run_id": run_id, "ok": ledger["ok"], "sources": {
        n: {"ok": (ledger["sources"][n] or {}).get("ok")} for n in wanted
    }}, indent=2))
    return ledger


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.pipeline.daily_ingest",
        description="Isolated daily ingest of Art19 + Substack + unique YouTube, then GCS sync + MCP roll.",
    )
    p.add_argument("--dry-run", action="store_true", help="Walk sources without calling pipelines, GCS, or MCP roll.")
    p.add_argument("--skip-hydrate", action="store_true", help="Do not pull chroma/artifacts from GCS (local laptop).")
    p.add_argument("--skip-sync", action="store_true", help="Do not upload artifacts/chroma to GCS.")
    p.add_argument("--skip-roll", action="store_true", help="Do not roll the public MCP Cloud Run service.")
    p.add_argument("--skip-art19", action="store_true")
    p.add_argument("--skip-substack", action="store_true")
    p.add_argument("--skip-youtube", action="store_true")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sources = []
    if not args.skip_art19:
        sources.append(SOURCE_ART19)
    if not args.skip_substack:
        sources.append(SOURCE_SUBSTACK)
    if not args.skip_youtube:
        sources.append(SOURCE_YOUTUBE)
    if not sources:
        print("No sources selected.")
        return 2
    ledger = run(
        sources=sources,
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
