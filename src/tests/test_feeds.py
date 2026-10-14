"""Feed registry, due logic, social stubs, and daily_ingest wiring."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.feeds.registry import SourceConfig, due_sources, is_due, load_sources
from src.feeds.social import SourceNotImplemented, run_social
from src.tests.pg_testutil import PG_TEST_DSN, SKIP_REASON, TempDatabase

MONDAY = datetime(2026, 10, 12, 15, tzinfo=timezone.utc)
TUESDAY = MONDAY + timedelta(days=1)


class TestRegistry(unittest.TestCase):
    def test_shipped_registry_loads(self) -> None:
        sources = {s.key: s for s in load_sources()}
        self.assertTrue({"art19", "substack", "youtube", "instagram"} <= set(sources))
        self.assertEqual(sources["youtube"].url, "https://www.youtube.com/@future_of_education/videos")
        self.assertEqual(sources["youtube"].config, {"max_new_per_run": 10})
        for key in ("instagram", "x", "linkedin", "tiktok"):
            self.assertFalse(sources[key].enabled, key)

    def test_rejects_bad_cadence_and_kind(self) -> None:
        with self.assertRaises(ValueError):
            SourceConfig.from_dict({"key": "a", "kind": "podcast_rss", "cadence": "hourly"})
        with self.assertRaises(ValueError):
            SourceConfig.from_dict({"key": "a", "kind": "myspace"})

    def test_due_rules(self) -> None:
        daily = SourceConfig(key="d", kind="substack", name="d")
        weekly = SourceConfig(key="w", kind="x", name="w", cadence="weekly")
        manual = SourceConfig(key="m", kind="x", name="m", cadence="manual")
        off = SourceConfig(key="o", kind="x", name="o", enabled=False)
        self.assertTrue(is_due(daily, now=TUESDAY, last_checked_at=TUESDAY))
        self.assertFalse(is_due(manual, now=MONDAY, last_checked_at=None))
        self.assertFalse(is_due(off, now=MONDAY, last_checked_at=None))
        self.assertTrue(is_due(weekly, now=MONDAY, last_checked_at=None))
        self.assertFalse(is_due(weekly, now=TUESDAY, last_checked_at=None))
        self.assertFalse(is_due(weekly, now=TUESDAY, last_checked_at=TUESDAY - timedelta(days=3)))
        self.assertTrue(is_due(weekly, now=TUESDAY, last_checked_at=TUESDAY - timedelta(days=7)))
        keys = [s.key for s in due_sources([daily, weekly, manual, off], now=TUESDAY)]
        self.assertEqual(keys, ["d"])
        # With Postgres state, a weekly source that was never checked runs now.
        keys = [s.key for s in due_sources([daily, weekly], now=TUESDAY, last_checked={})]
        self.assertEqual(keys, ["d", "w"])


class TestSocial(unittest.TestCase):
    def test_skips_until_accounts_and_credential(self) -> None:
        src = SourceConfig(key="ig", kind="instagram", name="ig", config={"credential_env": "LF_TEST_IG"})
        self.assertTrue(run_social(src)["skipped"])
        src = SourceConfig(key="ig", kind="instagram", name="ig",
                           config={"accounts": ["mackenzieprice"], "credential_env": "LF_TEST_IG"})
        with patch.dict("os.environ", {"LF_TEST_IG": ""}):
            self.assertIn("credential", run_social(src)["reason"])
        with patch.dict("os.environ", {"LF_TEST_IG": "token"}):
            with self.assertRaises(SourceNotImplemented):
                run_social(src)


class TestDailyIngestRegistry(unittest.TestCase):
    def setUp(self) -> None:
        p = patch("src.pipeline.daily_ingest._write_ledger_gcs", return_value={})
        p.start()
        self.addCleanup(p.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = patch("src.pipeline.daily_ingest.LEDGER_DIR", Path(tmp.name))
        p.start()
        self.addCleanup(p.stop)
        p = patch.dict("os.environ", {"PG_DSN": ""})
        p.start()
        self.addCleanup(p.stop)

    def _run(self, **kw):
        from src.pipeline.daily_ingest import run

        return run(hydrate=False, sync=False, roll=False, **kw)

    def test_youtube_runs_whole_channel_feed(self) -> None:
        from src.pipeline import daily_ingest

        calls: list[list[str]] = []
        with patch("src.pipeline.youtube.run.main", side_effect=lambda argv: calls.append(argv) or 0):
            ledger = self._run(sources=["youtube"])
        self.assertTrue(ledger["sources"]["youtube"]["ok"])
        self.assertEqual(calls, [["--feed", "--max-new", "10"]])
        self.assertEqual(daily_ingest.DEFAULT_YOUTUBE_MAX_NEW, 10)

    def test_youtube_video_failures_fail_the_source(self) -> None:
        with patch("src.pipeline.youtube.run.main", return_value=1):
            ledger = self._run(sources=["youtube"])
        self.assertFalse(ledger["sources"]["youtube"]["ok"])
        self.assertIn("videos failed", ledger["sources"]["youtube"]["error"])

    def test_other_youtube_channels_are_refused(self) -> None:
        registry = [SourceConfig(key="yt2", kind="youtube_channel", name="y",
                                 url="https://www.youtube.com/@thealphaschool/videos")]
        with patch("src.pipeline.youtube.run.main", side_effect=AssertionError("must not run")):
            ledger = self._run(registry=registry)
        self.assertFalse(ledger["sources"]["yt2"]["ok"])

    def test_duplicate_source_runs_once(self) -> None:
        ran: list[str] = []
        ledger = self._run(sources=["art19", "art19"], source_runners={"art19": lambda: ran.append("a") or {}})
        self.assertEqual(ran, ["a"])
        self.assertEqual(list(ledger["sources"]), ["art19"])

    def test_fallback_registry_is_not_mirrored_to_postgres(self) -> None:
        with (
            patch("src.pipeline.daily_ingest.load_sources", side_effect=ValueError("bad json")),
            patch("src.feeds.state.is_enabled", return_value=True),
            patch("src.feeds.state.sync_registry") as sync,
            patch("src.feeds.state.last_checked", return_value={}),
            patch("src.feeds.state.record_run", return_value=True),
        ):
            ledger = self._run(source_runners={"art19": lambda: {}, "substack": lambda: {}, "youtube": lambda: {}})
        sync.assert_not_called()
        self.assertEqual(list(ledger["sources"]), ["art19", "substack", "youtube"])

    def test_dual_write_runs_after_sources_and_is_non_fatal(self) -> None:
        registry = [SourceConfig(key="art19", kind="podcast_rss", name="a")]
        order: list[str] = []
        with (
            patch("src.feeds.state.is_enabled", return_value=True),
            patch("src.feeds.state.sync_registry"),
            patch("src.feeds.state.last_checked", return_value={}),
            patch("src.feeds.state.record_run", return_value=True),
            patch("src.pipeline.daily_ingest.sync_chroma_to_postgres",
                  side_effect=lambda: order.append("pg") or {"chunks_written": 3}),
        ):
            ledger = self._run(registry=registry, source_runners={"art19": lambda: order.append("art19") or {}})
        self.assertEqual(order, ["art19", "pg"])
        self.assertEqual(ledger["pg_state"]["chroma_to_pg"], {"ok": True, "stats": {"chunks_written": 3}})
        with (
            patch("src.feeds.state.is_enabled", return_value=True),
            patch("src.feeds.state.sync_registry"),
            patch("src.feeds.state.last_checked", return_value={}),
            patch("src.feeds.state.record_run", return_value=True),
            patch("src.pipeline.daily_ingest.sync_chroma_to_postgres", side_effect=RuntimeError("pg down")),
        ):
            ledger = self._run(registry=registry, source_runners={"art19": lambda: {}})
        self.assertTrue(ledger["sources"]["art19"]["ok"])
        self.assertFalse(ledger["pg_state"]["chroma_to_pg"]["ok"])

    def test_due_selection_and_skip(self) -> None:
        registry = [
            SourceConfig(key="art19", kind="podcast_rss", name="a"),
            SourceConfig(key="youtube", kind="youtube_channel", name="y"),
            SourceConfig(key="x", kind="x", name="x", cadence="weekly", config={"accounts": []}),
        ]
        ran: list[str] = []
        runners = {k: (lambda k=k: ran.append(k) or {}) for k in ("art19", "youtube", "x")}
        with patch("src.pipeline.daily_ingest._utc_now", return_value=TUESDAY):
            ledger = self._run(registry=registry, skip=["youtube"], source_runners=runners)
        self.assertEqual(ran, ["art19"])  # youtube skipped, weekly x not due on Tuesday
        self.assertEqual(list(ledger["sources"]), ["art19"])

    def test_bad_source_config_fails_only_that_source(self) -> None:
        registry = [
            SourceConfig(key="art19", kind="podcast_rss", name="a", url="https://example.com/other.rss"),
            SourceConfig(key="ig", kind="instagram", name="ig", config={"accounts": []}),
        ]
        ledger = self._run(registry=registry)
        self.assertFalse(ledger["sources"]["art19"]["ok"])
        self.assertIn("only https://rss.art19.com", ledger["sources"]["art19"]["error"])
        self.assertTrue(ledger["sources"]["ig"]["ok"])
        self.assertTrue(ledger["sources"]["ig"]["detail"]["skipped"])

    def test_broken_registry_falls_back_to_core_feeds(self) -> None:
        with (
            patch("src.pipeline.daily_ingest.load_sources", side_effect=ValueError("bad json")),
            patch("src.pipeline.daily_ingest.due_sources", side_effect=lambda r, **_: r),
        ):
            ledger = self._run(dry_run=True)
        self.assertEqual(list(ledger["sources"]), ["art19", "substack", "youtube"])
        self.assertTrue(any("bad json" in e for e in ledger["errors"]))

    def test_postgres_errors_do_not_fail_the_run(self) -> None:
        registry = [SourceConfig(key="art19", kind="podcast_rss", name="a")]
        with (
            patch("src.feeds.state.is_enabled", return_value=True),
            patch("src.feeds.state.sync_registry", side_effect=RuntimeError("db down")),
            patch("src.feeds.state.last_checked", side_effect=RuntimeError("db down")),
            patch("src.feeds.state.record_run", side_effect=RuntimeError("db down")),
        ):
            ledger = self._run(registry=registry, source_runners={"art19": lambda: {}})
        self.assertTrue(ledger["sources"]["art19"]["ok"])
        self.assertFalse(ledger["pg_state"]["record_run"]["ok"])
        self.assertFalse(ledger["pg_state"]["sync_registry"]["ok"])


@unittest.skipUnless(PG_TEST_DSN, SKIP_REASON)
class TestFeedStatePostgres(unittest.TestCase):
    def test_sync_record_and_last_checked(self) -> None:
        from src.database import pg_store
        from src.feeds import state

        registry = load_sources()
        with TempDatabase() as db:
            pg_store.migrate(db.dsn)
            self.assertEqual(state.sync_registry(registry, dsn=db.dsn), len(registry))
            state.sync_registry(registry, dsn=db.dsn)  # idempotent
            self.assertEqual(state.last_checked(dsn=db.dsn), {})
            ledger = {
                "run_id": "20261014T150000Z",
                "started_at": "2026-10-14T15:00:00Z",
                "finished_at": "2026-10-14T15:05:00Z",
                "ok": False,
                "sources": {
                    "art19": {"ok": True, "detail": {}},
                    "youtube": {"ok": False, "error": "RuntimeError: boom"},
                    "instagram": {"ok": True, "detail": {"skipped": True}},
                },
            }
            ledger["sources"]["x"] = {"ok": False, "skipped": True, "error": "hydrate_failed"}
            self.assertTrue(state.record_run(ledger, dsn=db.dsn))
            checked = state.last_checked(dsn=db.dsn)
            # x never ran (hydrate failed), so it stays due.
            self.assertEqual(set(checked), {"art19", "youtube", "instagram"})
            with pg_store.session(db.dsn) as conn:
                rows = dict(conn.execute("SELECT key, last_error FROM sources WHERE key IN ('art19', 'youtube')").fetchall())
                skipped = conn.execute(
                    "SELECT skipped FROM source_checks WHERE source_key = 'instagram'"
                ).fetchone()[0]
                ok_at = dict(conn.execute("SELECT key, last_ok_at FROM sources WHERE key IN ('art19', 'instagram')").fetchall())
            self.assertIsNone(rows["art19"])
            self.assertEqual(rows["youtube"], "RuntimeError: boom")
            self.assertTrue(skipped)
            self.assertIsNotNone(ok_at["art19"])
            self.assertIsNone(ok_at["instagram"])  # skipped is not a success


if __name__ == "__main__":
    unittest.main()
