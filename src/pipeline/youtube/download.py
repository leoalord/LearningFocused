"""Download unique YouTube items: captions + optional audio extract.

Refuses channel URLs, UU uploads playlists, @thealphaschool, and full-tab dumps.
Never downloads a full MP4; audio extract is m4a only when captions are missing.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from src.config import (
    YOUTUBE_AUDIO_DIR,
    YOUTUBE_CAPTIONS_DIR,
    YOUTUBE_METADATA_DIR,
    ensure_data_dirs,
)
from src.pipeline.youtube.constants import (
    FOE_CHANNEL_ID,
    FOE_UPLOADS_PLAYLIST_ID,
)

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

_FORBIDDEN_URL_NEEDLES = (
    "thealphaschool",
    "/@future_of_education/videos",
    "/@future_of_education/shorts",
    f"list={FOE_UPLOADS_PLAYLIST_ID}",
    f"list=UU{FOE_CHANNEL_ID[2:]}",
    "/channel/",
)


class RefusedDownloadError(ValueError):
    """Raised when a download target is out of scope for TASK-5."""


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def assert_safe_video_id(video_id: str) -> str:
    vid = (video_id or "").strip()
    if not VIDEO_ID_RE.fullmatch(vid):
        raise RefusedDownloadError(f"Refusing non-video-id target: {video_id!r}")
    return vid


def assert_safe_url(url: str) -> str:
    lowered = (url or "").lower()
    for needle in _FORBIDDEN_URL_NEEDLES:
        if needle.lower() in lowered:
            raise RefusedDownloadError(f"Refusing forbidden URL ({needle}): {url}")
    if re.search(r"list=UU[A-Za-z0-9_-]+", url or ""):
        raise RefusedDownloadError(f"Refusing UU uploads playlist dump: {url}")
    if re.search(r"youtube\.com/@thealphaschool", lowered):
        raise RefusedDownloadError(f"Refusing @thealphaschool: {url}")
    return url


def _run_yt_dlp(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = ["yt-dlp", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError(f"yt-dlp failed ({proc.returncode}): {proc.stderr[-3000:]}")
    return proc


def yt_dlp_flat(url: str, *, allow_videos_tab: bool = False) -> list[dict[str, Any]]:
    """Metadata-only playlist listing. Never a UU dump or @thealphaschool.

    Videos-tab listing is opt-in (`allow_videos_tab`) so we can match unique long-form
    titles without downloading the tab. Shorts-tab (188) remains refused.
    """
    lowered = (url or "").lower()
    if "thealphaschool" in lowered:
        raise RefusedDownloadError(f"Refusing @thealphaschool: {url}")
    if re.search(r"list=UU[A-Za-z0-9_-]+", url or ""):
        raise RefusedDownloadError(f"Refusing UU uploads playlist dump: {url}")
    if "/@future_of_education/shorts" in lowered:
        raise RefusedDownloadError(f"Refusing Shorts-tab dump: {url}")
    if "/@future_of_education/videos" in lowered and not allow_videos_tab:
        raise RefusedDownloadError(f"Refusing Videos-tab URL without allow_videos_tab: {url}")
    if "/channel/" in lowered:
        raise RefusedDownloadError(f"Refusing channel URL: {url}")
    proc = _run_yt_dlp(
        ["--flat-playlist", "--skip-download", "-j", "--no-warnings", url]
    )
    items: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        items.append(json.loads(line))
    return items


def fetch_video_metadata(video_id: str) -> dict[str, Any]:
    vid = assert_safe_video_id(video_id)
    url = watch_url(vid)
    proc = _run_yt_dlp(
        ["-j", "--skip-download", "--no-warnings", "--no-playlist", url]
    )
    data = json.loads(proc.stdout)
    return data


def write_metadata_sidecar(video_id: str, payload: dict[str, Any]) -> Path:
    ensure_data_dirs()
    path = YOUTUBE_METADATA_DIR / f"{video_id}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def find_caption_file(video_id: str) -> Path | None:
    if not YOUTUBE_CAPTIONS_DIR.exists():
        return None
    matches = sorted(YOUTUBE_CAPTIONS_DIR.glob(f"{video_id}*.vtt"))
    if not matches:
        return None

    def _rank(p: Path) -> tuple[int, str]:
        name = p.name.lower()
        # Prefer official over auto-generated.
        auto = 1 if "auto" in name or "orig" in name else 0
        return (auto, name)

    return sorted(matches, key=_rank)[0]


def download_captions(video_id: str) -> Path | None:
    vid = assert_safe_video_id(video_id)
    ensure_data_dirs()
    existing = find_caption_file(vid)
    if existing:
        return existing
    out_tmpl = str(YOUTUBE_CAPTIONS_DIR / vid)
    _run_yt_dlp(
        [
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            "en.*,en",
            "--convert-subs",
            "vtt",
            "--skip-download",
            "--no-playlist",
            "--no-warnings",
            "-o",
            out_tmpl,
            watch_url(vid),
        ],
        check=False,
    )
    return find_caption_file(vid)


def find_audio_file(video_id: str) -> Path | None:
    if not YOUTUBE_AUDIO_DIR.exists():
        return None
    for ext in (".m4a", ".mp3", ".webm", ".opus"):
        path = YOUTUBE_AUDIO_DIR / f"{video_id}{ext}"
        if path.exists():
            return path
    return None


def download_audio_extract(video_id: str) -> Path:
    """Extract audio only (no MP4 dump) for unique items that lack captions."""
    vid = assert_safe_video_id(video_id)
    ensure_data_dirs()
    existing = find_audio_file(vid)
    if existing:
        return existing
    out_tmpl = str(YOUTUBE_AUDIO_DIR / f"{vid}.%(ext)s")
    _run_yt_dlp(
        [
            "-f",
            "bestaudio[ext=m4a]/bestaudio/best",
            "--extract-audio",
            "--audio-format",
            "m4a",
            "--no-playlist",
            "--no-warnings",
            "-o",
            out_tmpl,
            watch_url(vid),
        ]
    )
    audio = find_audio_file(vid)
    if audio is None:
        raise FileNotFoundError(f"Audio extract missing after yt-dlp for {vid}")
    return audio


def compact_info(raw: dict[str, Any]) -> dict[str, Any]:
    video_id = str(raw.get("id") or raw.get("video_id") or "")
    duration = raw.get("duration")
    try:
        duration_seconds = int(duration) if duration not in (None, "", "NA") else None
    except (TypeError, ValueError):
        duration_seconds = None
    channel_id = raw.get("channel_id") or raw.get("uploader_id") or ""
    channel = raw.get("channel") or raw.get("uploader") or ""
    handle = raw.get("uploader_id") or raw.get("channel") or ""
    if isinstance(handle, str) and handle and not handle.startswith("@") and handle.startswith("UC"):
        handle = raw.get("uploader") or handle
    webpage = raw.get("webpage_url") or raw.get("url") or (watch_url(video_id) if video_id else "")
    return {
        "video_id": video_id,
        "title": raw.get("title") or "",
        "url": webpage,
        "duration_seconds": duration_seconds,
        "channel_id": channel_id,
        "channel": channel,
        "channel_handle": handle if isinstance(handle, str) else str(handle),
        "upload_date": raw.get("upload_date"),
        "playlist_id": raw.get("playlist_id"),
    }


def sleep_politely(seconds: float = 0.5) -> None:
    time.sleep(seconds)
