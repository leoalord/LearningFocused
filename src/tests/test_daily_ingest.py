"""Daily ingest isolation, ledger, and skip-existing audio download."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.pipeline.audio.download import download_podcasts
from src.pipeline.daily_ingest import build_parser, run


class FakeEntry:
    def __init__(self, title: str, audio_url: str = "http://example.invalid/ep.mp3") -> None:
        self.title = title
        self.summary = ""
        self.published = "Mon, 01 Jan 2024 00:00:00 GMT"
        self.links = [SimpleNamespace(type="audio/mpeg", href=audio_url)]
        self.id = title

    def get(self, key, default=None):
        if key == "links":
            return [{"type": getattr(link, "type", None), "href": getattr(link, "href", None)} for link in self.links]
        return getattr(self, key, default)

    def __contains__(self, key: object) -> bool:
        return hasattr(self, str(key))


class FakeFeed:
    bozo = False

    def __init__(self, titles: list[str]) -> None:
        self.entries = [FakeEntry(t) for t in titles]


class TestAudioSkipExistingTranscript(unittest.TestCase):
    def test_skips_download_when_transcript_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            downloads = tmp_path / "mp3"
            metadata = tmp_path / "meta"
            transcripts = tmp_path / "transcripts"
            downloads.mkdir()
            metadata.mkdir()
            transcripts.mkdir()
            (transcripts / "S2E001 Hello.json").write_text("{}", encoding="utf-8")
            feed = FakeFeed(["S2E001 Hello"])
            session = SimpleNamespace(get=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download")))
            with (
                patch("src.pipeline.audio.download.feedparser.parse", return_value=feed),
                patch("src.pipeline.audio.download.get_session", return_value=session),
            ):
                stats = download_podcasts(
                    "https://example.invalid/rss",
                    str(downloads),
                    metadata_dir=str(metadata),
                    transcripts_dir=str(transcripts),
                )
            self.assertEqual(stats["new"], 0)
            self.assertEqual(stats["skipped_existing"], 1)
            self.assertEqual(list(downloads.glob("*.mp3")), [])

    def test_downloads_when_transcript_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            downloads = tmp_path / "mp3"
            metadata = tmp_path / "meta"
            transcripts = tmp_path / "transcripts"
            downloads.mkdir()
            metadata.mkdir()
            transcripts.mkdir()

            class FakeResp:
                def raise_for_status(self) -> None:
                    return None

                def iter_content(self, chunk_size: int = 8192):
                    yield b"ID3"

            session = SimpleNamespace(get=lambda *a, **k: FakeResp())
            feed = FakeFeed(["Brand New Episode"])
            with (
                patch("src.pipeline.audio.download.feedparser.parse", return_value=feed),
                patch("src.pipeline.audio.download.get_session", return_value=session),
                patch("src.pipeline.audio.download.time.sleep", return_value=None),
            ):
                stats = download_podcasts(
                    "https://example.invalid/rss",
                    str(downloads),
                    metadata_dir=str(metadata),
                    transcripts_dir=str(transcripts),
                )
            self.assertEqual(stats["new"], 1)
            self.assertTrue((downloads / "Brand New Episode.mp3").is_file())


class TestDailyIngestIsolation(unittest.TestCase):
    def setUp(self) -> None:
        gcs = patch("src.pipeline.daily_ingest._write_ledger_gcs", return_value={"dated": "gs://x", "latest": "gs://x"})
        gcs.start()
        self.addCleanup(gcs.stop)

    def test_one_source_failure_does_not_abort_others(self) -> None:
        order: list[str] = []

        def art19() -> dict:
            order.append("art19")
            raise RuntimeError("art19 exploded")

        def substack() -> dict:
            order.append("substack")
            return {"ingested": 0}

        def youtube() -> dict:
            order.append("youtube")
            return {"exit_code": 0}

        with tempfile.TemporaryDirectory() as tmp:
            ledger_dir = Path(tmp)
            with patch("src.pipeline.daily_ingest.LEDGER_DIR", ledger_dir):
                ledger = run(
                    sources=("art19", "substack", "youtube"),
                    hydrate=False,
                    sync=False,
                    roll=False,
                    dry_run=False,
                    source_runners={"art19": art19, "substack": substack, "youtube": youtube},
                )
            latest = json.loads((ledger_dir / "latest.json").read_text(encoding="utf-8"))
            self.assertTrue((ledger_dir / "latest.json").is_file())
            self.assertEqual(latest["run_id"], ledger["run_id"])

        self.assertEqual(order, ["art19", "substack", "youtube"])
        self.assertFalse(ledger["sources"]["art19"]["ok"])
        self.assertTrue(ledger["sources"]["substack"]["ok"])
        self.assertTrue(ledger["sources"]["youtube"]["ok"])
        self.assertIn("art19 exploded", ledger["sources"]["art19"]["error"])
        self.assertFalse(ledger["ok"])

    def test_ledger_written_when_all_sources_fail(self) -> None:
        def boom() -> dict:
            raise RuntimeError("nope")

        with tempfile.TemporaryDirectory() as tmp:
            ledger_dir = Path(tmp)
            with patch("src.pipeline.daily_ingest.LEDGER_DIR", ledger_dir):
                ledger = run(
                    sources=("art19", "substack"),
                    hydrate=False,
                    sync=False,
                    roll=False,
                    source_runners={"art19": boom, "substack": boom},
                )
            files = list(ledger_dir.glob("*.json"))
        self.assertGreaterEqual(len(files), 2)
        self.assertFalse(ledger["sources"]["art19"]["ok"])
        self.assertFalse(ledger["sources"]["substack"]["ok"])

    def test_hydrate_failure_skips_sources(self) -> None:
        called = []

        def art19() -> dict:
            called.append("art19")
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("src.pipeline.daily_ingest.LEDGER_DIR", Path(tmp)),
                patch("src.pipeline.daily_ingest.hydrate_artifacts", side_effect=RuntimeError("gcs down")),
            ):
                ledger = run(
                    sources=("art19",),
                    hydrate=True,
                    sync=False,
                    roll=False,
                    source_runners={"art19": art19},
                )
        self.assertEqual(called, [])
        self.assertTrue(ledger["sources"]["art19"].get("skipped"))
        self.assertFalse(ledger["hydrate"]["ok"])

    def test_dry_run_does_not_call_runners(self) -> None:
        called = []

        def art19() -> dict:
            called.append("art19")
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            with patch("src.pipeline.daily_ingest.LEDGER_DIR", Path(tmp)):
                ledger = run(
                    sources=("art19",),
                    hydrate=False,
                    sync=False,
                    roll=False,
                    dry_run=True,
                    source_runners={"art19": art19},
                )
        self.assertEqual(called, [])
        self.assertTrue(ledger["sources"]["art19"]["dry_run"])
        self.assertTrue(ledger["ok"] or ledger["sources"]["art19"]["ok"])

    def test_cli_skip_flags(self) -> None:
        args = build_parser().parse_args(["--skip-youtube", "--skip-hydrate", "--dry-run"])
        self.assertTrue(args.skip_youtube)
        self.assertFalse(args.skip_art19)
        self.assertTrue(args.dry_run)


if __name__ == "__main__":
    unittest.main()
