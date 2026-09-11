"""Duplicate-podcast skip rules for unique YouTube ingest.

Reuses `normalize`, `score`, `parse_hms`, `after_colon`, and `load_rss` from
`scripts/inventory_youtube_rss.py`. Never skip on duration alone.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import RSS_FEED_URL
from src.pipeline.youtube.constants import (
    PLAYLIST_GIFTED_MINDS_EPISODES,
    RSS_INTRO_TWIN_TITLES,
)

_INVENTORY_PATH = Path(__file__).resolve().parents[3] / "scripts" / "inventory_youtube_rss.py"


def load_inventory_helpers():
    """Load the TASK-3 inventory helper module (normalize/score/parse_hms/load_rss)."""
    spec = importlib.util.spec_from_file_location("inventory_youtube_rss", _INVENTORY_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load inventory helpers from {_INVENTORY_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_helpers = load_inventory_helpers()
normalize = _helpers.normalize
score = _helpers.score
parse_hms = _helpers.parse_hms
after_colon = _helpers.after_colon
load_rss = _helpers.load_rss
tokens = _helpers.tokens

SKIP_RSS_TITLE_MATCH = "rss_title_match"
SKIP_SAME_RECORDING = "same_recording"
SKIP_EPISODES_PLAYLIST = "gifted_minds_episodes_playlist"
SKIP_RSS_INTRO_TWIN = "rss_intro_twin"

SAME_RECORDING_MIN_SECONDS = 480
SAME_RECORDING_DELTA_SECONDS = 5
TITLE_CUE_THRESHOLD = 0.22


@dataclass(frozen=True)
class SkipDecision:
    video_id: str
    title: str
    skip_reason: str
    matched_rss_title: str | None = None
    detail: str | None = None


def load_rss_entries(url: str = RSS_FEED_URL) -> list[dict[str, Any]]:
    payload = load_rss(url)
    if isinstance(payload, dict):
        return list(payload.get("entries") or [])
    return list(payload)


def is_rss_intro_twin(yt_title: str) -> bool:
    yt_n = normalize(yt_title)
    for twin in RSS_INTRO_TWIN_TITLES:
        if yt_n == normalize(twin):
            return True
    return False


def rss_title_match(yt_title: str, rss_entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    yt_n = normalize(yt_title)
    if not yt_n:
        return None
    for rss in rss_entries:
        rss_title = rss.get("title") or ""
        s = score(rss_title, yt_title)
        # High-confidence title match: exact normalize, after-colon, or Gifted Minds intro map.
        if s >= 0.97:
            return {"rss_title": rss_title, "title_score": s}
        if yt_n == normalize(rss_title) or yt_n == normalize(after_colon(rss_title)):
            return {"rss_title": rss_title, "title_score": s}
    return None


def same_recording_match(
    yt_title: str,
    yt_duration: int | None,
    rss_entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Duration ±5s AND ≥8 min AND a title cue. Duration alone is not a skip."""
    if yt_duration is None or yt_duration < SAME_RECORDING_MIN_SECONDS:
        return None
    best: dict[str, Any] | None = None
    for rss in rss_entries:
        rss_dur = parse_hms(rss.get("itunes_duration"))
        if rss_dur is None:
            continue
        if abs(int(yt_duration) - int(rss_dur)) > SAME_RECORDING_DELTA_SECONDS:
            continue
        rss_title = rss.get("title") or ""
        title_s = score(rss_title, yt_title)
        if title_s < TITLE_CUE_THRESHOLD:
            continue
        # SequenceMatcher can hit 0.22 on unrelated titles that share a stem
        # ("school" vs "homeschool"). Require at least one distinctive token overlap
        # so duration collisions are never skipped on duration alone.
        if not (tokens(rss_title) & tokens(yt_title)):
            continue
        if best is None or title_s > float(best["title_score"]):
            best = {
                "rss_title": rss_title,
                "title_score": title_s,
                "rss_duration": rss_dur,
                "yt_duration": yt_duration,
            }
    return best


def skip_decision(
    *,
    video_id: str,
    title: str,
    duration_seconds: int | None,
    rss_entries: list[dict[str, Any]],
    episodes_playlist_ids: set[str],
    playlist_id: str | None = None,
    allow_same_recording: bool = True,
) -> SkipDecision | None:
    """Return a skip decision if this video must not be ingested as new corpus."""
    if video_id in episodes_playlist_ids or playlist_id == PLAYLIST_GIFTED_MINDS_EPISODES:
        return SkipDecision(
            video_id=video_id,
            title=title,
            skip_reason=SKIP_EPISODES_PLAYLIST,
            detail="Gifted Minds Episodes playlist (already in RSS)",
        )

    if is_rss_intro_twin(title):
        return SkipDecision(
            video_id=video_id,
            title=title,
            skip_reason=SKIP_RSS_INTRO_TWIN,
            detail="About Gifted Minds intro overlaps RSS Intro to {X}",
        )

    title_hit = rss_title_match(title, rss_entries)
    if title_hit:
        return SkipDecision(
            video_id=video_id,
            title=title,
            skip_reason=SKIP_RSS_TITLE_MATCH,
            matched_rss_title=str(title_hit["rss_title"]),
            detail=f"title_score={title_hit['title_score']}",
        )

    if allow_same_recording:
        rec = same_recording_match(title, duration_seconds, rss_entries)
        if rec:
            return SkipDecision(
                video_id=video_id,
                title=title,
                skip_reason=SKIP_SAME_RECORDING,
                matched_rss_title=str(rec["rss_title"]),
                detail=(
                    f"title_score={rec['title_score']} "
                    f"yt={rec['yt_duration']}s rss={rec['rss_duration']}s"
                ),
            )
    return None
