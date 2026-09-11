"""YouTube pipeline -> Chroma indexing helpers.

Document ids (idempotency):
  youtube_transcript_segment_{video_id}_{start_seconds_or_chunk}
  youtube_summary_overview_{video_id}
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from langchain_core.documents import Document

from src.config import YOUTUBE_METADATA_DIR, YOUTUBE_SEGMENTED_DIR, YOUTUBE_SUMMARIES_DIR
from src.pipeline.youtube.constants import SOURCE_TYPE_SHORT, SOURCE_TYPE_VIDEO
from src.pipeline.youtube.download import watch_url


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Chroma metadata must be scalars; drop None."""
    cleaned: dict[str, Any] = {}
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            cleaned[key] = value
        else:
            cleaned[key] = str(value)
    return cleaned


def _sidecar_meta(video_id: str) -> dict[str, Any]:
    path = YOUTUBE_METADATA_DIR / f"{video_id}.json"
    if path.exists():
        return _load_json(path)
    return {}


def _base_meta(video_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    side = _sidecar_meta(video_id)
    title = (extra or {}).get("title") or side.get("title") or "Unknown YouTube video"
    url = side.get("url") or side.get("canonical_url") or watch_url(video_id)
    bucket = side.get("bucket") or (extra or {}).get("bucket") or "unique_longform"
    source_type = side.get("source_type")
    if not source_type:
        source_type = SOURCE_TYPE_SHORT if bucket == "short" else SOURCE_TYPE_VIDEO
    duration = side.get("duration_seconds")
    try:
        duration_seconds = int(duration) if duration is not None else 0
    except (TypeError, ValueError):
        duration_seconds = 0
    channel_id = side.get("channel_id") or ""
    channel_handle = side.get("channel_handle") or ""
    published_at = side.get("published_at") or side.get("upload_date")
    playlist_id = side.get("playlist_id")
    is_short = "true" if source_type == SOURCE_TYPE_SHORT else "false"
    meta = {
        "source_type": source_type,
        "video_id": video_id,
        "doc_id": video_id,
        "title": title,
        "url": url,
        "canonical_url": url,
        "duration_seconds": duration_seconds,
        "bucket": bucket,
        "channel_id": channel_id,
        "channel_handle": channel_handle,
        "is_short": is_short,
        "playlist_id": playlist_id,
        "published_at": published_at,
    }
    if extra:
        meta.update({k: v for k, v in extra.items() if v is not None})
    return _clean_meta(meta)


def create_summary_document(summary: dict[str, Any]) -> Document:
    video_id = str(summary.get("video_id") or summary.get("doc_id") or "unknown")
    title = summary.get("title") or "Unknown YouTube video"
    content = summary.get("generated_content") or {}
    overview = content.get("overview") or ""
    thesis = content.get("thesis") or ""
    themes = content.get("themes") or []
    takeaways = content.get("key_takeaways") or []
    page = (
        f"YouTube: {title}\n"
        f"Thesis: {thesis}\n\n"
        f"Overview:\n{overview}\n\n"
        f"Themes: {', '.join(themes) if isinstance(themes, list) else themes}\n"
        f"Takeaways: {'; '.join(takeaways) if isinstance(takeaways, list) else takeaways}\n"
    )
    meta = _base_meta(
        video_id,
        extra={
            "title": title,
            "bucket": summary.get("bucket"),
            "source_type": summary.get("source_type"),
            "type": "youtube_summary_overview",
            "chroma_content_hash": _sha256_text(page),
        },
    )
    meta["type"] = "youtube_summary_overview"
    doc = Document(page_content=page, metadata=meta)
    doc.metadata["_chroma_id"] = f"youtube_summary_overview_{video_id}"
    return doc


def create_segment_documents(segmented: dict[str, Any]) -> list[Document]:
    video_id = str(segmented.get("video_id") or "unknown")
    title = segmented.get("title") or "Unknown YouTube video"
    documents: list[Document] = []
    for idx, segment in enumerate(segmented.get("segments") or []):
        content = (segment.get("content") or "").strip()
        if not content:
            continue
        topic = segment.get("topic") or "General"
        summary = segment.get("summary") or ""
        start_time = segment.get("start_time")
        end_time = segment.get("end_time")
        try:
            start_int = int(float(start_time)) if start_time is not None else None
        except (TypeError, ValueError):
            start_int = None
        chunk_key = f"{start_int}" if start_int is not None else f"{idx:03d}"
        page = f"YouTube: {title}\nTopic: {topic}\nSummary: {summary}\n\nTranscript:\n{content}"
        meta = _base_meta(
            video_id,
            extra={
                "title": title,
                "type": "youtube_transcript_segment",
                "topic": topic,
                "start_time": start_time,
                "end_time": end_time,
                "chroma_content_hash": _sha256_text(page),
            },
        )
        meta["type"] = "youtube_transcript_segment"
        doc = Document(page_content=page, metadata=meta)
        doc.metadata["_chroma_id"] = f"youtube_transcript_segment_{video_id}_{chunk_key}"
        documents.append(doc)
    return documents


def collect_youtube_documents() -> list[Document]:
    documents: list[Document] = []
    if YOUTUBE_SUMMARIES_DIR.exists():
        for path in sorted(YOUTUBE_SUMMARIES_DIR.glob("*.json")):
            try:
                documents.append(create_summary_document(_load_json(path)))
            except Exception:
                continue
    if YOUTUBE_SEGMENTED_DIR.exists():
        for path in sorted(YOUTUBE_SEGMENTED_DIR.glob("*.json")):
            try:
                documents.extend(create_segment_documents(_load_json(path)))
            except Exception:
                continue
    return documents
