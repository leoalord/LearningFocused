"""Transcripts for unique YouTube videos: official captions first, else AssemblyAI.

Never re-transcribes podcast RSS files.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import YOUTUBE_TRANSCRIPTS_DIR, ensure_data_dirs


_TS_RE = re.compile(
    r"(?:(\d{2}):)?(\d{2}):(\d{2})[.,](\d{3})\s+-->\s+(?:(\d{2}):)?(\d{2}):(\d{2})[.,](\d{3})"
)
_TAG_RE = re.compile(r"<[^>]+>")


def _ts_to_seconds(h: str | None, m: str, s: str, ms: str) -> float:
    hours = int(h or 0)
    return hours * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_vtt(text: str) -> list[dict[str, Any]]:
    """Parse a WebVTT file into timestamped cues (YouTube auto-subs included)."""
    cues: list[dict[str, Any]] = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        match = _TS_RE.search(line)
        if not match:
            i += 1
            continue
        start = _ts_to_seconds(match.group(1), match.group(2), match.group(3), match.group(4))
        end = _ts_to_seconds(match.group(5), match.group(6), match.group(7), match.group(8))
        i += 1
        text_lines: list[str] = []
        while i < len(lines) and lines[i].strip():
            cleaned = _TAG_RE.sub("", lines[i]).strip()
            if cleaned and cleaned.upper() != "WEBVTT":
                text_lines.append(cleaned)
            i += 1
        cue_text = " ".join(text_lines).strip()
        if cue_text:
            cues.append({"start_time": start, "end_time": end, "text": cue_text, "speaker": None})
    return _dedupe_rolling_cues(cues)


def _dedupe_rolling_cues(cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """YouTube auto VTT repeats rolling phrases; keep new tail text when possible."""
    if not cues:
        return []
    out: list[dict[str, Any]] = []
    prev_text = ""
    for cue in cues:
        text = " ".join((cue.get("text") or "").split())
        if not text:
            continue
        if text == prev_text:
            if out:
                out[-1]["end_time"] = cue["end_time"]
            continue
        if prev_text and text.startswith(prev_text):
            addition = text[len(prev_text) :].strip()
            if addition:
                cue = {**cue, "text": addition}
            else:
                if out:
                    out[-1]["end_time"] = cue["end_time"]
                continue
        elif prev_text and prev_text in text:
            # Rolling caption grew in the middle; take the suffix after prev.
            idx = text.find(prev_text)
            addition = (text[:idx] + text[idx + len(prev_text) :]).strip()
            if addition:
                cue = {**cue, "text": addition}
            else:
                continue
        out.append({**cue, "text": " ".join((cue.get("text") or "").split())})
        prev_text = text
    return out


def captions_to_transcript_payload(
    *,
    video_id: str,
    caption_path: Path,
    source: str = "official_or_auto_captions",
) -> dict[str, Any]:
    raw = caption_path.read_text(encoding="utf-8", errors="replace")
    cues = parse_vtt(raw)
    duration = 0.0
    if cues:
        duration = float(cues[-1].get("end_time") or 0.0)
    return {
        "meta_data": {
            "video_id": video_id,
            "source": source,
            "caption_file": str(caption_path),
            "date_transcribed": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": duration,
        },
        "transcript": cues,
    }


def write_transcript(video_id: str, payload: dict[str, Any]) -> Path:
    ensure_data_dirs()
    path = YOUTUBE_TRANSCRIPTS_DIR / f"{video_id}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def transcribe_from_captions(video_id: str, caption_path: Path) -> Path:
    payload = captions_to_transcript_payload(video_id=video_id, caption_path=caption_path)
    return write_transcript(video_id, payload)


def transcribe_from_audio(video_id: str, audio_path: Path) -> Path:
    """AssemblyAI transcription of unique YT audio only (not podcast RSS files)."""
    from src.pipeline.audio.transcribe import transcribe_audio

    ensure_data_dirs()
    existing = YOUTUBE_TRANSCRIPTS_DIR / f"{video_id}.json"
    if existing.exists():
        return existing
    out = transcribe_audio(str(audio_path), output_dir=str(YOUTUBE_TRANSCRIPTS_DIR))
    path = Path(out)
    # Normalize payload so downstream always has video_id.
    data = json.loads(path.read_text(encoding="utf-8"))
    meta = data.setdefault("meta_data", {})
    meta["video_id"] = video_id
    meta["source"] = "assemblyai"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def transcript_exists(video_id: str) -> Path | None:
    path = YOUTUBE_TRANSCRIPTS_DIR / f"{video_id}.json"
    return path if path.exists() else None
