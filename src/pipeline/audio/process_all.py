"""Audio pipeline runner (supports incremental backfill with minimal ceremony)."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from dotenv import load_dotenv

from src.config import (
    DOWNLOADS_DIR,
    METADATA_DIR,
    RSS_FEED_URL,
    SEGMENTED_DIR,
    TRANSCRIPTS_DIR,
    ensure_data_dirs,
)
from src.pipeline.audio.download import download_podcasts
from src.pipeline.audio.identify_speakers import identify_speakers, update_transcript_with_speakers
from src.pipeline.audio.segment_topics import segment_transcript
from src.pipeline.audio.transcribe import transcribe_audio


@dataclass(frozen=True)
class Stats:
    """Execution stats for a step run."""

    processed: int
    skipped: int
    failed: int


@dataclass(frozen=True)
class ProcessAllResult:
    """Summary of a `process_all` run (used by higher-level runners)."""

    mode: str
    transcripts_matched: int
    new_transcripts_created: int
    transcribe_failed: int


def _read_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _has_speaker_map(transcript_path: Path) -> bool:
    meta = _read_json(transcript_path).get("meta_data", {})
    speaker_map = meta.get("speaker_map")
    return bool(speaker_map)


def _has_segment(transcript_path: Path) -> bool:
    return (SEGMENTED_DIR / f"{transcript_path.stem}_segmented.json").exists()


def _take(items: Sequence[Path], limit: int | None) -> Sequence[Path]:
    return items if limit is None else items[: max(0, limit)]


def _run_over_transcripts(
    name: str,
    transcripts: Sequence[Path],
    *,
    should_run: Callable[[Path], bool],
    run_one: Callable[[Path], None],
    limit: int | None,
) -> Stats:
    """
    Run a step over a set of transcript files.

    This keeps the orchestration readable: filtering is handled by `should_run`, and
    errors are isolated per-file so one bad transcript doesn't stop the batch.
    """
    candidates = [t for t in transcripts if should_run(t)]
    candidates = list(_take(candidates, limit))
    print(f"\n=== {name} ===")
    print(f"Candidates: {len(candidates)}")

    processed = skipped = failed = 0
    for t in candidates:
        try:
            run_one(t)
            processed += 1
        except Exception as e:
            failed += 1
            print(f"{name} failed for {t.name}: {e}")

    # Note: skipped is computed relative to the provided transcript list (not just candidates)
    skipped = max(0, len(transcripts) - len([t for t in transcripts if should_run(t)]))
    print(f"{name} summary: processed={processed} skipped={skipped} failed={failed}")
    return Stats(processed=processed, skipped=skipped, failed=failed)


def _download(download_limit: int) -> None:
    print("\n=== DOWNLOAD ===")
    download_podcasts(
        RSS_FEED_URL,
        str(DOWNLOADS_DIR),
        metadata_dir=str(METADATA_DIR),
        limit=download_limit,
    )


def _transcribe_missing(transcribe_limit: int | None) -> tuple[list[Path], int]:
    print("\n=== TRANSCRIBE (missing only) ===")
    mp3s = sorted(DOWNLOADS_DIR.glob("*.mp3"))
    mp3s = list(_take(mp3s, transcribe_limit))
    created: list[Path] = []
    failed = 0

    for mp3 in mp3s:
        out = TRANSCRIPTS_DIR / f"{mp3.stem}.json"
        if out.exists():
            continue
        try:
            print(f"Transcribing: {mp3.name}")
            created_path = transcribe_audio(str(mp3), output_dir=str(TRANSCRIPTS_DIR))
            created.append(Path(created_path))
        except Exception as e:
            failed += 1
            print(f"Transcribe failed for {mp3.name}: {e}")

    print(f"Created transcripts: {len(created)}")
    if failed:
        print(f"Transcribe failures: {failed}")
    return created, failed


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""
    p = argparse.ArgumentParser(description="Audio pipeline runner")
    p.add_argument("--mode", choices=["daily", "backfill"], default="daily")
    p.add_argument("--transcripts-glob", default="*.json")
    p.add_argument(
        "--download-limit",
        type=int,
        default=None,
        help="Max NEW episode downloads this run (scans the full RSS feed). Default: no cap.",
    )
    p.add_argument("--transcribe-limit", type=int, default=None)
    p.add_argument("--identify-limit", type=int, default=None)
    p.add_argument("--segment-limit", type=int, default=None)
    p.add_argument("--identify-all", action="store_true")
    p.add_argument("--segment-all", action="store_true")
    p.add_argument("--skip-download", action="store_true")
    p.add_argument("--skip-transcribe", action="store_true")
    p.add_argument("--skip-identify", action="store_true")
    p.add_argument("--skip-segment", action="store_true")
    return p


def main(argv: Sequence[str] | None = None) -> ProcessAllResult:
    """Entry point for `python -m src.pipeline.audio.process_all`."""
    load_dotenv()
    ensure_data_dirs()
    args = build_parser().parse_args(argv)

    all_transcripts = sorted(TRANSCRIPTS_DIR.glob(args.transcripts_glob))
    all_transcripts = [t for t in all_transcripts if t.is_file()]
    print(f"Transcripts matched: {len(all_transcripts)} ({args.transcripts_glob!r})")

    new_transcripts: list[Path] = []
    transcribe_failed = 0
    if args.mode == "daily":
        if not args.skip_download:
            try:
                _download(download_limit=args.download_limit)
            except Exception as e:
                print(f"Download failed: {e}")
        if not args.skip_transcribe:
            new_transcripts, transcribe_failed = _transcribe_missing(transcribe_limit=args.transcribe_limit)

    candidates = all_transcripts if args.mode == "backfill" else new_transcripts

    if not args.skip_identify:
        def _identify_and_update(t: Path) -> None:
            speakers = identify_speakers(str(t))
            if not speakers:
                raise RuntimeError("No speakers identified (empty result).")
            update_transcript_with_speakers(str(t), speakers)

        _run_over_transcripts(
            "IDENTIFY SPEAKERS",
            candidates,
            should_run=(lambda t: args.identify_all or not _has_speaker_map(t)),
            run_one=_identify_and_update,
            limit=args.identify_limit,
        )

    if not args.skip_segment:
        _run_over_transcripts(
            "SEGMENT TOPICS",
            candidates,
            should_run=(lambda t: args.segment_all or not _has_segment(t)),
            run_one=(lambda t: segment_transcript(t, SEGMENTED_DIR)),
            limit=args.segment_limit,
        )

    print("\nDone.")
    return ProcessAllResult(
        mode=args.mode,
        transcripts_matched=len(all_transcripts),
        new_transcripts_created=len(new_transcripts),
        transcribe_failed=transcribe_failed,
    )


if __name__ == "__main__":
    main()


