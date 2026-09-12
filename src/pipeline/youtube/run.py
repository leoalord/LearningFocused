"""YouTube unique-ingest runner (TASK-5).

Ingests a documented V1 cap of unique videos (not RSS twins), writes artifacts,
and upserts `youtube_*` Chroma types. Refuses --channel, --reset-chroma, UU dumps,
@thealphaschool, and the 188 Shorts tab.

Default model for summarize/segment: gemini-flash-latest.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from dotenv import load_dotenv

from src.config import YOUTUBE_DIR, YOUTUBE_METADATA_DIR, ensure_data_dirs
from src.pipeline.youtube.constants import (
    BUCKET_APPEARANCE,
    BUCKET_BRANDING,
    BUCKET_SHORT,
    BUCKET_UNIQUE_LONGFORM,
    FOE_CHANNEL_ID,
    FOE_HANDLE,
    PLAYLIST_ABOUT_GIFTED_MINDS,
    PLAYLIST_APPEARANCES,
    PLAYLIST_GIFTED_MINDS_EPISODES,
    PLAYLIST_SHORTS_CURATED,
    PLAYLIST_URLS,
    SKIP_PROOF_TITLE_HINT,
    SKIP_PROOF_VIDEO_ID,
    SOURCE_TYPE_SHORT,
    SOURCE_TYPE_VIDEO,
    UNIQUE_LONGFORM_TITLE_HINTS,
    V1_APPEARANCES,
    V1_BRANDING,
    V1_SHORTS,
    V1_UNIQUE_LONGFORM,
    VIDEOS_TAB_URL,
)
from src.pipeline.youtube.download import (
    RefusedDownloadError,
    assert_safe_video_id,
    compact_info,
    download_audio_extract,
    download_captions,
    fetch_video_metadata,
    find_audio_file,
    find_caption_file,
    sleep_politely,
    watch_url,
    write_metadata_sidecar,
    yt_dlp_flat,
)
from src.pipeline.youtube.skip import SkipDecision, load_rss_entries, skip_decision
from src.pipeline.youtube.transcribe import (
    transcript_exists,
    transcribe_from_audio,
    transcribe_from_captions,
)

PIPELINE_PLAN = """
YouTube pipeline (TASK-5 unique ingest)

Channel in scope: @future_of_education (UCKHZkY1J1NyKypLn80Pj19A)
Out of scope: @thealphaschool, full UU-playlist / full-channel ingest, 188 Shorts tab

Chroma type (retrieval role):
  youtube_transcript_segment   -> query_segments
  youtube_summary_overview     -> query_summaries

source_type (asset class):
  youtube_video | youtube_short

Idempotency: _chroma_id includes YouTube video_id
  youtube_transcript_segment_{video_id}_{start_or_chunk}
  youtube_summary_overview_{video_id}

Skip (do not ingest as new corpus):
  RSS title matches (strip S2E### / Ep #N) OR duration±5s same-recording
  (≥8 min WITH a title cue) OR Gifted Minds Episodes playlist OR RSS intro twins.
  Never skip on duration alone.

V1 cap (default): all appearances (6) + 3 Gifted Minds branding + 3 unique long-form + 3 curated Shorts.
Refused: --channel, --reset-chroma, UU dump, @thealphaschool, Shorts-tab bulk.
""".strip()


@dataclass
class Candidate:
    video_id: str
    title: str
    bucket: str
    playlist_id: str | None = None
    duration_seconds: int | None = None
    channel_id: str = ""
    channel_handle: str = ""
    url: str = ""
    upload_date: str = ""

    @property
    def source_type(self) -> str:
        return SOURCE_TYPE_SHORT if self.bucket == BUCKET_SHORT else SOURCE_TYPE_VIDEO


@dataclass
class IngestResult:
    ingested: list[str] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    v1_cap: dict[str, Any] = field(default_factory=dict)


def _flat_items(url: str, *, allow_videos_tab: bool = False) -> list[dict[str, Any]]:
    items = yt_dlp_flat(url, allow_videos_tab=allow_videos_tab)
    compact = []
    for raw in items:
        info = compact_info(raw)
        if info.get("video_id"):
            compact.append(info)
    return compact


def _episodes_playlist_ids() -> set[str]:
    items = _flat_items(PLAYLIST_URLS["gifted_minds_episodes"])
    return {str(i["video_id"]) for i in items if i.get("video_id")}


def select_v1_candidates(
    *,
    rss_entries: list[dict[str, Any]],
    episodes_ids: set[str],
    longform_limit: int = V1_UNIQUE_LONGFORM,
    appearances_limit: int = V1_APPEARANCES,
    branding_limit: int = V1_BRANDING,
    shorts_limit: int = V1_SHORTS,
) -> tuple[list[Candidate], list[SkipDecision]]:
    """Build the V1 ingest set in contract order: long-form, appearances, branding, shorts."""
    skipped: list[SkipDecision] = []
    selected: list[Candidate] = []
    seen: set[str] = set()

    def consider(
        info: dict[str, Any],
        bucket: str,
        playlist_id: str | None,
        *,
        apply_skip: bool,
        allow_same_recording: bool = True,
    ) -> SkipDecision | None:
        video_id = str(info.get("video_id") or "")
        title = str(info.get("title") or "")
        if not video_id or video_id in seen:
            return None
        duration = info.get("duration_seconds")
        try:
            duration_i = int(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration_i = None
        if apply_skip:
            decision = skip_decision(
                video_id=video_id,
                title=title,
                duration_seconds=duration_i,
                rss_entries=rss_entries,
                episodes_playlist_ids=episodes_ids,
                playlist_id=playlist_id,
                allow_same_recording=allow_same_recording,
            )
            if decision:
                skipped.append(decision)
                seen.add(video_id)
                return decision
        cand = Candidate(
            video_id=video_id,
            title=title,
            bucket=bucket,
            playlist_id=playlist_id,
            duration_seconds=duration_i,
            channel_id=str(info.get("channel_id") or ""),
            channel_handle=str(info.get("channel_handle") or ""),
            url=str(info.get("url") or watch_url(video_id)),
        )
        selected.append(cand)
        seen.add(video_id)
        return None

    # 1. Unique long-form: match inventory hints in hint order (not Videos-tab order).
    videos_tab = _flat_items(VIDEOS_TAB_URL, allow_videos_tab=True)
    longform_added = 0
    for hint in UNIQUE_LONGFORM_TITLE_HINTS:
        if longform_added >= longform_limit:
            break
        info = next(
            (i for i in videos_tab if hint in str(i.get("title") or "").lower()),
            None,
        )
        if not info:
            continue
        before = len(selected)
        consider(info, BUCKET_UNIQUE_LONGFORM, None, apply_skip=True, allow_same_recording=True)
        if len(selected) > before:
            longform_added += 1

    # 2. Appearances (playlist order: off-channel first, then Jenga).
    # Same-recording vs FoE RSS is not applied: these are guest clips, not episode uploads.
    appearances = _flat_items(PLAYLIST_URLS["appearances"])
    app_added = 0
    for info in appearances:
        if app_added >= appearances_limit:
            break
        before = len(selected)
        consider(
            info,
            BUCKET_APPEARANCE,
            PLAYLIST_APPEARANCES,
            apply_skip=True,
            allow_same_recording=False,
        )
        if len(selected) > before:
            app_added += 1

    # 3. Gifted Minds branding (skip RSS intro twins + episodes playlist).
    about = _flat_items(PLAYLIST_URLS["about_gifted_minds"])
    brand_added = 0
    for info in about:
        if brand_added >= branding_limit:
            break
        before = len(selected)
        consider(info, BUCKET_BRANDING, PLAYLIST_ABOUT_GIFTED_MINDS, apply_skip=True)
        if len(selected) > before:
            brand_added += 1

    # 4. Curated Shorts only (not the 188). Shorts are unique format vs RSS.
    shorts = _flat_items(PLAYLIST_URLS["shorts_curated"])
    short_added = 0
    for info in shorts:
        if short_added >= shorts_limit:
            break
        before = len(selected)
        consider(info, BUCKET_SHORT, PLAYLIST_SHORTS_CURATED, apply_skip=False)
        if len(selected) > before:
            short_added += 1

    return selected, skipped


def record_skip_proof(
    rss_entries: list[dict[str, Any]],
    episodes_ids: set[str],
) -> SkipDecision | None:
    """Evaluate a known RSS duplicate (Alpha Anywhere / S2E335) without ingesting it."""
    try:
        raw = fetch_video_metadata(SKIP_PROOF_VIDEO_ID)
        info = compact_info(raw)
    except Exception:
        info = {
            "video_id": SKIP_PROOF_VIDEO_ID,
            "title": SKIP_PROOF_TITLE_HINT,
            "duration_seconds": None,
        }
    return skip_decision(
        video_id=SKIP_PROOF_VIDEO_ID,
        title=str(info.get("title") or SKIP_PROOF_TITLE_HINT),
        duration_seconds=info.get("duration_seconds"),  # type: ignore[arg-type]
        rss_entries=rss_entries,
        episodes_playlist_ids=episodes_ids,
        playlist_id=None,
    )


def _enrich_candidate(cand: Candidate) -> Candidate:
    if cand.title and cand.duration_seconds is not None and cand.channel_id:
        if not cand.url:
            cand.url = watch_url(cand.video_id)
        return cand
    raw = fetch_video_metadata(cand.video_id)
    info = compact_info(raw)
    cand.title = cand.title or str(info.get("title") or "")
    if cand.duration_seconds is None:
        cand.duration_seconds = info.get("duration_seconds")  # type: ignore[assignment]
    cand.channel_id = cand.channel_id or str(info.get("channel_id") or "")
    handle = str(info.get("channel_handle") or "")
    if handle.startswith("@"):
        cand.channel_handle = handle
    elif cand.channel_id == FOE_CHANNEL_ID:
        cand.channel_handle = FOE_HANDLE
    else:
        cand.channel_handle = handle or cand.channel_handle
    cand.url = str(info.get("url") or watch_url(cand.video_id))
    cand.upload_date = cand.upload_date or str(info.get("upload_date") or "")
    return cand


def persist_sidecar(cand: Candidate, *, skip: SkipDecision | None = None, status: str = "ingest") -> Path:
    payload = {
        "video_id": cand.video_id,
        "title": cand.title,
        "url": cand.url or watch_url(cand.video_id),
        "canonical_url": cand.url or watch_url(cand.video_id),
        "duration_seconds": cand.duration_seconds,
        "bucket": cand.bucket,
        "source_type": cand.source_type,
        "channel_id": cand.channel_id,
        "channel_handle": cand.channel_handle or (FOE_HANDLE if cand.channel_id == FOE_CHANNEL_ID else cand.channel_handle),
        "playlist_id": cand.playlist_id,
        # `index_chroma._base_meta` reads this for the document's published date.
        "upload_date": cand.upload_date or None,
        "status": status,
        "skip_reason": skip.skip_reason if skip else None,
        "skip_matched_rss_title": skip.matched_rss_title if skip else None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return write_metadata_sidecar(cand.video_id, payload)


def ingest_one(cand: Candidate, *, force: bool = False) -> str:
    """Download captions (or audio), transcribe, segment, summarize one unique video."""
    from src.pipeline.youtube.segment import segment_video
    from src.pipeline.youtube.summarize import summarize_video

    cand = _enrich_candidate(cand)
    persist_sidecar(cand, status="ingest")

    caption = find_caption_file(cand.video_id) or download_captions(cand.video_id)
    transcript_path = transcript_exists(cand.video_id)
    if transcript_path is None or force:
        if caption is not None:
            transcribe_from_captions(cand.video_id, caption)
        else:
            audio = find_audio_file(cand.video_id) or download_audio_extract(cand.video_id)
            transcribe_from_audio(cand.video_id, audio, force=force)

    segment_video(
        cand.video_id,
        title=cand.title,
        duration_seconds=cand.duration_seconds,
        force=force,
    )
    summarize_video(
        cand.video_id,
        title=cand.title,
        url=cand.url or watch_url(cand.video_id),
        bucket=cand.bucket,
        source_type=cand.source_type,
        force=force,
    )
    return cand.video_id


def write_skip_log(skipped: list[SkipDecision], extra: dict[str, Any] | None = None) -> Path:
    ensure_data_dirs()
    path = YOUTUBE_DIR / "skip_log.json"
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "skipped": [asdict(s) for s in skipped],
        "extra": extra or {},
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def index_youtube_only(*, prune_stale: bool = False) -> None:
    from src.pipeline.index_chroma import update_chroma_db

    update_chroma_db(
        reset=False,
        include_audio=False,
        include_articles=False,
        include_youtube=True,
        confirm_reset=None,
        prune_stale=prune_stale,
    )


V1_TOTAL_CAP = V1_UNIQUE_LONGFORM + V1_APPEARANCES + V1_BRANDING + V1_SHORTS


def _refused_from_args(args: argparse.Namespace) -> list[str]:
    reasons: list[str] = []
    if getattr(args, "channel", None):
        reasons.append("--channel is refused (no full-channel ingest)")
    if getattr(args, "reset_chroma", False):
        reasons.append("--reset-chroma is refused (TASK-5 upserts youtube_* only; never wipes Chroma)")
    playlist = getattr(args, "playlist", None)
    if playlist:
        lowered = playlist.lower()
        if "list=uu" in lowered or lowered.startswith("uu"):
            reasons.append("--playlist UU uploads dump is refused")
        if "thealphaschool" in lowered:
            reasons.append("@thealphaschool is refused")
        if "/shorts" in lowered and "list=pl" not in lowered:
            reasons.append("Shorts-tab bulk dump is refused; use curated playlist only")
        if not reasons:
            # Selection always uses the curated V1 playlists, so a benign --playlist
            # would be silently ignored and the operator would think it took effect.
            reasons.append("--playlist is not wired to selection; use --video-id instead")

    # The per-bucket limits exist to tune the V1 mix, not to raise the total.
    for flag, value, cap in (
        ("--longform-limit", args.longform_limit, V1_UNIQUE_LONGFORM),
        ("--appearances-limit", args.appearances_limit, V1_APPEARANCES),
        ("--branding-limit", args.branding_limit, V1_BRANDING),
        ("--shorts-limit", args.shorts_limit, V1_SHORTS),
    ):
        if value > cap:
            reasons.append(f"{flag}={value} exceeds the V1 cap of {cap}")
        elif value < 0:
            reasons.append(f"{flag}={value} must not be negative")
    if len(args.video_id) > V1_TOTAL_CAP:
        reasons.append(
            f"--video-id given {len(args.video_id)} times, over the V1 total cap of {V1_TOTAL_CAP}"
        )
    return reasons


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.pipeline.youtube.run",
        description=(
            "Ingest unique YouTube videos (V1 cap). Refuses --channel, --reset-chroma, "
            "UU dumps, @thealphaschool, and the 188 Shorts tab."
        ),
        epilog=PIPELINE_PLAN,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--print-plan", action="store_true", help="Print the ingest contract and exit.")
    p.add_argument("--dry-run", action="store_true", help="Resolve V1 candidates + skip proof; do not download.")
    p.add_argument("--skip-chroma", action="store_true", help="Skip Chroma upsert.")
    p.add_argument(
        "--prune-stale-chroma",
        action="store_true",
        help=(
            "After upsert, delete youtube_transcript_segment rows the on-disk artifacts "
            "no longer produce (needed after a document-id change). Only safe when this "
            "machine holds the complete youtube_videos/segmented set."
        ),
    )
    p.add_argument("--force", action="store_true", help="Redo transcript/segment/summary even if files exist.")
    p.add_argument(
        "--allow-empty-rss",
        action="store_true",
        help="Ingest even if the podcast RSS feed is empty (disables duplicate-skip rules).",
    )
    p.add_argument("--longform-limit", type=int, default=V1_UNIQUE_LONGFORM)
    p.add_argument("--appearances-limit", type=int, default=V1_APPEARANCES)
    p.add_argument("--branding-limit", type=int, default=V1_BRANDING)
    p.add_argument("--shorts-limit", type=int, default=V1_SHORTS)
    p.add_argument(
        "--video-id",
        action="append",
        default=[],
        help="Ingest a specific video_id (still skipped if RSS duplicate). Repeatable.",
    )
    # Refused flags: fail closed.
    p.add_argument("--channel", metavar="URL", default=None, help="REFUSED. Full-channel ingest is out of scope.")
    p.add_argument("--reset-chroma", action="store_true", help="REFUSED. Never wipe chroma_db.")
    p.add_argument("--playlist", metavar="URL", default=None, help="REFUSED if UU dump / Shorts tab / @thealphaschool.")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)

    refused = _refused_from_args(args)
    if refused:
        print("Refusing to run ingest:", file=sys.stderr)
        for reason in refused:
            print(f"  - {reason}", file=sys.stderr)
        print("See docs/youtube_pipeline.md.", file=sys.stderr)
        return 2

    if args.print_plan:
        print(PIPELINE_PLAN)
        return 0

    ensure_data_dirs()
    print("Loading RSS for skip matching...")
    rss_entries = load_rss_entries()
    print(f"  RSS episodes: {len(rss_entries)}")
    if not rss_entries and not args.allow_empty_rss:
        # feedparser never raises: a DNS failure or 5xx yields zero entries, which
        # makes every title/same-recording skip rule a no-op and silently ingests
        # the podcast twins this pipeline exists to exclude.
        print(
            "Refusing to run ingest: the podcast RSS feed returned no episodes, so "
            "duplicate-skip rules would all pass. Retry, or pass --allow-empty-rss "
            "to ingest without duplicate protection.",
            file=sys.stderr,
        )
        return 2
    print("Loading Gifted Minds Episodes playlist (skip-by-id)...")
    episodes_ids = _episodes_playlist_ids()
    print(f"  Episodes playlist ids: {len(episodes_ids)}")

    proof = record_skip_proof(rss_entries, episodes_ids)
    if proof:
        print(
            f"Skip proof: {proof.video_id} ({proof.title!r}) -> {proof.skip_reason}"
            + (f" matched {proof.matched_rss_title!r}" if proof.matched_rss_title else "")
        )
    else:
        print(f"WARNING: skip proof video {SKIP_PROOF_VIDEO_ID} was NOT skipped", file=sys.stderr)

    if args.video_id:
        candidates: list[Candidate] = []
        skipped: list[SkipDecision] = []
        for requested in args.video_id:
            # Normalize before the id reaches filenames and Chroma document ids.
            vid = assert_safe_video_id(requested)
            raw = fetch_video_metadata(vid)
            info = compact_info(raw)
            decision = skip_decision(
                video_id=vid,
                title=str(info.get("title") or ""),
                duration_seconds=info.get("duration_seconds"),  # type: ignore[arg-type]
                rss_entries=rss_entries,
                episodes_playlist_ids=episodes_ids,
            )
            if decision:
                skipped.append(decision)
                print(f"Skipping {vid}: {decision.skip_reason}")
                continue
            candidates.append(
                Candidate(
                    video_id=vid,
                    title=str(info.get("title") or ""),
                    bucket=BUCKET_UNIQUE_LONGFORM,
                    duration_seconds=info.get("duration_seconds"),  # type: ignore[arg-type]
                    channel_id=str(info.get("channel_id") or ""),
                    channel_handle=str(info.get("channel_handle") or ""),
                    url=str(info.get("url") or watch_url(vid)),
                )
            )
    else:
        print("Selecting V1 candidates...")
        candidates, skipped = select_v1_candidates(
            rss_entries=rss_entries,
            episodes_ids=episodes_ids,
            longform_limit=args.longform_limit,
            appearances_limit=args.appearances_limit,
            branding_limit=args.branding_limit,
            shorts_limit=args.shorts_limit,
        )

    # Never ingest the skip-proof duplicate even if it slipped into candidates.
    candidates = [c for c in candidates if c.video_id != SKIP_PROOF_VIDEO_ID]
    if proof:
        skipped.append(proof)

    v1_cap = {
        "unique_longform": [c.video_id for c in candidates if c.bucket == BUCKET_UNIQUE_LONGFORM],
        "appearance": [c.video_id for c in candidates if c.bucket == BUCKET_APPEARANCE],
        "gifted_minds_branding": [c.video_id for c in candidates if c.bucket == BUCKET_BRANDING],
        "short": [c.video_id for c in candidates if c.bucket == BUCKET_SHORT],
        "counts": {
            "unique_longform": sum(1 for c in candidates if c.bucket == BUCKET_UNIQUE_LONGFORM),
            "appearance": sum(1 for c in candidates if c.bucket == BUCKET_APPEARANCE),
            "gifted_minds_branding": sum(1 for c in candidates if c.bucket == BUCKET_BRANDING),
            "short": sum(1 for c in candidates if c.bucket == BUCKET_SHORT),
            "total": len(candidates),
        },
        "titles": {c.video_id: c.title for c in candidates},
    }
    print(f"V1 cap: {json.dumps(v1_cap['counts'], indent=2)}")
    for c in candidates:
        print(f"  [{c.bucket}] {c.video_id}  {c.title}")

    write_skip_log(skipped, extra={"v1_cap": v1_cap, "skip_proof": asdict(proof) if proof else None})
    (YOUTUBE_METADATA_DIR / "_v1_cap.json").write_text(
        json.dumps(v1_cap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    if args.dry_run:
        print("Dry run: no downloads / no Chroma writes.")
        return 0

    result = IngestResult(skipped=[asdict(s) for s in skipped], v1_cap=v1_cap)
    for cand in candidates:
        print(f"\n=== INGEST {cand.video_id} ({cand.bucket}) {cand.title} ===")
        try:
            ingest_one(cand, force=args.force)
            result.ingested.append(cand.video_id)
            sleep_politely(0.4)
        except Exception as exc:  # noqa: BLE001
            print(f"FAILED {cand.video_id}: {exc}", file=sys.stderr)
            result.failed.append({"video_id": cand.video_id, "error": str(exc)})

    print(f"\nIngested {len(result.ingested)} / {len(candidates)} (failed {len(result.failed)})")
    if not args.skip_chroma:
        print("\n=== CHROMA UPSERT (youtube_* only; no reset, no audio/Substack) ===")
        index_youtube_only(prune_stale=args.prune_stale_chroma)
    else:
        print("Skipping Chroma upsert (--skip-chroma).")

    print(json.dumps({"ingested": result.ingested, "failed": result.failed, "v1_cap": v1_cap["counts"]}, indent=2))
    return 0 if not result.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
