"""Audio pipeline -> Chroma indexing helpers.

Pipeline-owned logic:
- how we convert audio artifacts (combined summaries + segmented transcripts) into Documents

DB-owned logic lives in `src.database.chroma_manager` (connect/query/add).
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List

from langchain_core.documents import Document

from src.config import COMBINED_DIR, SEGMENTED_DIR
from src.pipeline.audio.episode_ids import UNKNOWN_EPISODE_ID, episode_key, parse_episode_id


def _load_json_file(file_path: str) -> Dict[str, Any]:
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)

def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def create_summary_documents(summary_data: Dict[str, Any]) -> List[Document]:
    """Convert a combined summary object into multi-vector Documents for embedding."""
    documents: List[Document] = []

    group_title = summary_data.get("group_title", "Unknown Series")
    episode_count = summary_data.get("episode_count", 1)
    topics = summary_data.get("generated_content", {}).get("topics", [])

    content = summary_data.get("generated_content", {}) or {}
    # Note: audio generator currently uses overview/themes/key_takeaways/value_proposition,
    # but older embedder expected high_level_summary/why_listen/topics. Keep best-effort.
    high_level_summary = content.get("high_level_summary") or content.get("overview", "") or ""
    why_listen = content.get("why_listen") or content.get("value_proposition", "") or ""
    key_takeaways = content.get("key_takeaways", []) or []

    overview_text = f"Series Title: {group_title}\n\nOverview: {high_level_summary}"
    # Use group_title + type as unique identifier for ChromaDB deduplication
    overview_id = f"series_overview_{group_title}"
    documents.append(
        Document(
            page_content=overview_text,
            metadata={
                "type": "series_overview",
                "group_title": group_title,
                "episode_count": episode_count,
                "topics": ", ".join(topics) if isinstance(topics, list) else str(topics),
                "chroma_content_hash": _sha256_text(overview_text),
            },
        )
    )
    # Store ID in metadata for later use when adding to ChromaDB
    documents[-1].metadata["_chroma_id"] = overview_id

    if why_listen:
        motivation_text = f"Series Title: {group_title}\n\nWhy Listen: {why_listen}"
        motivation_id = f"series_motivation_{group_title}"
        documents.append(
            Document(
                page_content=motivation_text,
                metadata={
                    "type": "series_motivation",
                    "group_title": group_title,
                    "topics": ", ".join(topics) if isinstance(topics, list) else str(topics),
                    "chroma_content_hash": _sha256_text(motivation_text),
                },
            )
        )
        # Store ID in metadata for later use when adding to ChromaDB
        documents[-1].metadata["_chroma_id"] = motivation_id

    for i, takeaway in enumerate(key_takeaways):
        takeaway_text = f"Series Title: {group_title}\n\nKey Takeaway: {takeaway}"
        takeaway_id = f"key_takeaway_{group_title}_{i + 1}"
        documents.append(
            Document(
                page_content=takeaway_text,
                metadata={
                    "type": "key_takeaway",
                    "group_title": group_title,
                    "takeaway_index": i + 1,
                    "topics": ", ".join(topics) if isinstance(topics, list) else str(topics),
                    "chroma_content_hash": _sha256_text(takeaway_text),
                },
            )
        )
        # Store ID in metadata for later use when adding to ChromaDB
        documents[-1].metadata["_chroma_id"] = takeaway_id

    return documents


def create_transcript_documents(transcript_data: Dict[str, Any]) -> List[Document]:
    """Convert a segmented transcript object into Documents (one per topic segment)."""
    documents: List[Document] = []

    title = transcript_data.get("title", "Unknown Title")
    segments = transcript_data.get("segments", []) or []

    # Derive from the title rather than trusting the stored field: artifacts segmented
    # before the parser fix carry filename fragments like "S" or "Intro" there.
    episode_id = parse_episode_id(title)
    if episode_id == UNKNOWN_EPISODE_ID:
        episode_id = parse_episode_id(str(transcript_data.get("episode_id") or ""))
    key = episode_key(episode_id, title)

    for segment in segments:
        topic = segment.get("topic", "General")
        content = segment.get("content", "") or ""
        summary = segment.get("summary", "") or ""
        speakers = ", ".join(segment.get("speakers", []) or [])
        start_time = segment.get("start_time")
        end_time = segment.get("end_time")

        if not content:
            continue

        combined_text = f"Episode: {title}\nTopic: {topic}\nSummary: {summary}\n\nTranscript:\n{content}"
        # `key` disambiguates episodes that share an episode number (the feed reuses
        # `S E2` and `S2E215`), so segments from different recordings can't collide.
        # Fallback to topic index if start_time not available
        segment_id = f"transcript_segment_{key}_{start_time or 'unknown'}_{topic}"
        documents.append(
            Document(
                page_content=combined_text,
                metadata={
                    "type": "transcript_segment",
                    "episode_id": episode_id,
                    "title": title,
                    "topic": topic,
                    "speakers": speakers,
                    "start_time": start_time,
                    "end_time": end_time,
                    "chroma_content_hash": _sha256_text(combined_text),
                },
            )
        )
        # Store ID in metadata for later use when adding to ChromaDB
        documents[-1].metadata["_chroma_id"] = segment_id

    return documents


def collect_audio_documents() -> List[Document]:
    """Collect all audio-derived Documents for Chroma indexing."""
    documents: List[Document] = []

    # Combined summaries
    if COMBINED_DIR.exists():
        for filename in os.listdir(COMBINED_DIR):
            if filename.endswith(".json") and not filename.startswith("episode_groupings"):
                path = os.path.join(COMBINED_DIR, filename)
                try:
                    data = _load_json_file(path)
                    documents.extend(create_summary_documents(data))
                except Exception:
                    continue

    # Segmented transcripts
    if SEGMENTED_DIR.exists():
        for filename in os.listdir(SEGMENTED_DIR):
            if filename.endswith(".json"):
                path = os.path.join(SEGMENTED_DIR, filename)
                try:
                    data = _load_json_file(path)
                    documents.extend(create_transcript_documents(data))
                except Exception:
                    continue

    return documents


