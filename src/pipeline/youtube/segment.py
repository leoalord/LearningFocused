"""Topic-segment unique YouTube transcripts (podcast segment shape)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from src.config import YOUTUBE_SEGMENTED_DIR, YOUTUBE_TRANSCRIPTS_DIR, ensure_data_dirs
from src.pipeline.youtube.llm_config import DEFAULT_MODEL, get_segmentation_llm

SHORT_WORD_THRESHOLD = 200
SHORT_DURATION_THRESHOLD = 120.0

# Chars of formatted transcript per segmentation call. An hour-plus video would
# otherwise go out as one ~170k-char prompt and reliably hit the LLM timeout,
# and run.py's per-video `except` would leave it unsegmented. Windowing keeps the
# tail of the video visible, which truncating to a prefix would not.
SEGMENTATION_PROMPT_BUDGET = 120_000


class TopicSegment(BaseModel):
    topic_label: str = Field(description="A concise label for the topic discussed in this segment")
    start_time: float = Field(description="The start timestamp of the segment in seconds")
    end_time: float = Field(description="The end timestamp of the segment in seconds")
    summary: str = Field(description="A brief summary of what is discussed in this segment")


class SegmentationResponse(BaseModel):
    segments: list[TopicSegment] = Field(
        description="List of topic segments covering the entire transcript"
    )


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _format_transcript(entries: list[dict[str, Any]]) -> str:
    formatted = ""
    for segment in entries:
        speaker = segment.get("speaker_name", segment.get("speaker") or "Speaker")
        start = float(segment.get("start_time") or 0)
        text = segment.get("text") or ""
        formatted += f"[{start:.2f}s] {speaker}: {text}\n"
    return formatted


def _word_count(entries: list[dict[str, Any]]) -> int:
    return len(" ".join(e.get("text") or "" for e in entries).split())


def _budget_windows(
    entries: list[dict[str, Any]], budget: int = SEGMENTATION_PROMPT_BUDGET
) -> list[list[dict[str, Any]]]:
    """Split entries into consecutive windows whose formatted text fits `budget`.

    Everything shorter than one budget stays a single window, so the common case
    is one call with the prompt it had before.
    """
    windows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for entry in entries:
        line = len(_format_transcript([entry]))
        if current and size + line > budget:
            windows.append(current)
            current, size = [], 0
        current.append(entry)
        size += line
    if current:
        windows.append(current)
    return windows


def _single_segment(
    *,
    title: str,
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    content_parts = []
    speakers: set[str] = set()
    for entry in entries:
        speaker = entry.get("speaker_name") or entry.get("speaker")
        text = (entry.get("text") or "").strip()
        if speaker:
            speakers.add(str(speaker))
            content_parts.append(f"{speaker}: {text}")
        else:
            content_parts.append(text)
    start = float(entries[0].get("start_time") or 0) if entries else 0.0
    end = float(entries[-1].get("end_time") or start) if entries else 0.0
    content = " ".join(p for p in content_parts if p).strip()
    return [
        {
            "topic": title or "YouTube video",
            "start_time": start,
            "end_time": end,
            "summary": content[:400],
            "speakers": sorted(s for s in speakers if s),
            "content": content,
        }
    ]


def _attach_content(
    result_segments: list[TopicSegment],
    original: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    processed = []
    for segment in result_segments:
        segment_text = ""
        segment_speakers: set[str] = set()
        for entry in original:
            entry_start = float(entry.get("start_time") or 0)
            entry_end = float(entry.get("end_time") or entry_start)
            speaker = entry.get("speaker_name", entry.get("speaker"))
            text = entry.get("text") or ""
            overlaps = entry_start < segment.end_time and entry_end > segment.start_time
            contained = entry_start >= segment.start_time and entry_end <= segment.end_time
            midpoint = (entry_start + entry_end) / 2
            in_mid = segment.start_time <= midpoint <= segment.end_time
            if contained or (overlaps and in_mid):
                label = speaker or "Speaker"
                segment_text += f"{label}: {text} "
                if speaker:
                    segment_speakers.add(str(speaker))
        processed.append(
            {
                "topic": segment.topic_label,
                "start_time": segment.start_time,
                "end_time": segment.end_time,
                "summary": segment.summary,
                "speakers": list(segment_speakers),
                "content": segment_text.strip(),
            }
        )
    return processed


def segment_video(
    video_id: str,
    *,
    title: str,
    duration_seconds: int | None = None,
    force: bool = False,
    model: str = DEFAULT_MODEL,
) -> Path:
    ensure_data_dirs()
    out_path = YOUTUBE_SEGMENTED_DIR / f"{video_id}.json"
    if out_path.exists() and not force:
        return out_path

    transcript_path = YOUTUBE_TRANSCRIPTS_DIR / f"{video_id}.json"
    data = _load_json(transcript_path)
    entries = list(data.get("transcript") or [])
    duration: float | None = duration_seconds
    if duration is None:
        raw = data.get("meta_data", {}).get("duration_seconds")
        try:
            duration = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            duration = None

    use_fallback = (not entries) or _word_count(entries) < SHORT_WORD_THRESHOLD
    if duration is not None and duration <= SHORT_DURATION_THRESHOLD:
        use_fallback = True

    if use_fallback:
        processed = _single_segment(title=title, entries=entries)
    else:
        llm = get_segmentation_llm(model=model, temperature=0.0)
        parser = PydanticOutputParser(pydantic_object=SegmentationResponse)
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    """You are an expert content analyzer for educational YouTube videos.
Segment the transcript into coherent topic chunks covering the ENTIRE video.

Guidelines:
1. Identify major topic transitions.
2. Cover start to finish without gaps.
3. Timestamps must align with the provided [timestamp] markers.
4. Output valid JSON matching the specified structure.

{format_instructions}
""",
                ),
                ("user", "Video title: {title}\n\nTranscript:\n{transcript}"),
            ]
        )
        chain = prompt | llm | parser
        topic_segments: list[TopicSegment] = []
        for window in _budget_windows(entries):
            result = chain.invoke(
                {
                    "title": title,
                    "transcript": _format_transcript(window),
                    "format_instructions": parser.get_format_instructions(),
                }
            )
            topic_segments.extend(result.segments)
        processed = _attach_content(topic_segments, entries)
        if not processed:
            processed = _single_segment(title=title, entries=entries)

    payload = {
        "video_id": video_id,
        "title": title,
        "original_meta": data.get("meta_data") or {},
        "segments": processed,
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out_path
