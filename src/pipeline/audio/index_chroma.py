"""Audio pipeline -> Chroma indexing helpers.

Pipeline-owned logic:
- how we convert audio artifacts (combined summaries + transcripts) into Documents

DB-owned logic lives in `src.database.chroma_manager` (connect/query/add).
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List

from langchain_core.documents import Document

from src.config import COMBINED_DIR, SEGMENTED_DIR, TRANSCRIPTS_DIR
from src.pipeline.audio.chunking import chunk_header, chunk_transcript
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
    """One Document per LLM topic segment. Only for segmented files with no raw transcript;
    `create_transcript_chunk_documents` is the normal path."""
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
        segment_id = f"transcript_segment_{key}_{start_time if start_time is not None else 'unknown'}_{topic}"
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


def create_transcript_chunk_documents(
    transcript_data: Dict[str, Any], segmented_data: Dict[str, Any] | None = None
) -> List[Document]:
    """Raw transcript (+ optional LLM topic segments) -> sized, speaker-labelled chunks.

    See `src.pipeline.audio.chunking`. The topic label is a header line and metadata;
    the generated summary is not embedded.
    """
    meta = transcript_data.get("meta_data", {}) or {}
    title = meta.get("og_file_name") or (segmented_data or {}).get("title") or "Unknown Title"
    episode_id = parse_episode_id(title)
    if episode_id == UNKNOWN_EPISODE_ID and segmented_data:
        episode_id = parse_episode_id(str(segmented_data.get("episode_id") or ""))
    key = episode_key(episode_id, title)
    segments = (segmented_data or {}).get("segments", []) or []

    documents: List[Document] = []
    seen: set[str] = set()
    for i, chunk in enumerate(chunk_transcript(transcript_data.get("transcript", []) or [], segments)):
        text = f"{chunk_header(title, chunk)}\n\n{chunk.render()}"
        chunk_id = f"transcript_segment_{key}_{chunk.start:.2f}"
        while chunk_id in seen:
            chunk_id += "+"
        seen.add(chunk_id)
        documents.append(
            Document(
                page_content=text,
                metadata={
                    "type": "transcript_segment",
                    "episode_id": episode_id,
                    "title": title,
                    "topic": chunk.topic or "General",
                    "topics": " | ".join(chunk.topics),
                    "speakers": ", ".join(chunk.speakers),
                    "start_time": chunk.start,
                    "end_time": chunk.end,
                    "chunk_index": i,
                    "long_turn": chunk.long_turn,
                    "chroma_content_hash": _sha256_text(text),
                    "_chroma_id": chunk_id,
                },
            )
        )
    return documents


def collect_transcript_chunk_documents(unreadable: List[str] | None = None) -> List[Document]:
    """Chunks for every raw transcript, joined to its segmented file by name.

    Segmented files without a raw transcript fall back to one chunk per topic segment.
    """
    documents: List[Document] = []
    chunked_stems: set[str] = set()
    if TRANSCRIPTS_DIR.exists():
        for path in sorted(TRANSCRIPTS_DIR.glob("*.json")):
            try:
                data = _load_json_file(str(path))
                seg_path = SEGMENTED_DIR / f"{path.stem}_segmented.json"
                seg = _load_json_file(str(seg_path)) if seg_path.exists() else None
                documents.extend(create_transcript_chunk_documents(data, seg))
                chunked_stems.add(path.stem)
            except Exception as exc:
                if unreadable is not None:
                    unreadable.append(f"{path}: {exc}")
    if SEGMENTED_DIR.exists():
        for seg_path in sorted(SEGMENTED_DIR.glob("*_segmented.json")):
            if seg_path.name.removesuffix("_segmented.json") in chunked_stems:
                continue
            try:
                documents.extend(create_transcript_documents(_load_json_file(str(seg_path))))
            except Exception as exc:
                if unreadable is not None:
                    unreadable.append(f"{seg_path}: {exc}")
    return documents


def collect_audio_documents(unreadable: List[str] | None = None) -> List[Document]:
    """Collect all audio-derived Documents for Chroma indexing.

    Unreadable artifacts are skipped, and their paths are appended to `unreadable`
    when provided. Callers that prune Chroma by the ids collected here must treat a
    non-empty list as "corpus incomplete" and not delete the rows they couldn't see.
    """
    documents: List[Document] = []

    # Combined summaries
    if COMBINED_DIR.exists():
        for filename in sorted(os.listdir(COMBINED_DIR)):
            if filename.endswith(".json") and not filename.startswith("episode_groupings"):
                path = os.path.join(COMBINED_DIR, filename)
                try:
                    data = _load_json_file(path)
                    documents.extend(create_summary_documents(data))
                except Exception as exc:
                    if unreadable is not None:
                        unreadable.append(f"{path}: {exc}")
                    continue

    # Transcript chunks (raw transcripts, topic labels from segmented files)
    documents.extend(collect_transcript_chunk_documents(unreadable))

    return documents


