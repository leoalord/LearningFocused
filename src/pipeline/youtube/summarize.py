"""Per-video YouTube overview summaries (not series rollups)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from src.config import YOUTUBE_SUMMARIES_DIR, YOUTUBE_TRANSCRIPTS_DIR, ensure_data_dirs
from src.pipeline.youtube.llm_config import DEFAULT_MODEL, get_summary_llm


class YoutubeOverview(BaseModel):
    overview: str = Field(description="Cohesive overview suitable for embedding.")
    thesis: str = Field(description="1–3 sentence thesis of the video.")
    themes: list[str] = Field(description="3–10 themes/topics.")
    key_takeaways: list[str] = Field(description="3–8 concrete takeaways.")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _transcript_text(data: dict[str, Any]) -> str:
    parts = []
    for entry in data.get("transcript") or []:
        text = (entry.get("text") or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def summarize_video(
    video_id: str,
    *,
    title: str,
    url: str,
    bucket: str,
    source_type: str,
    force: bool = False,
    model: str = DEFAULT_MODEL,
) -> Path:
    ensure_data_dirs()
    out_path = YOUTUBE_SUMMARIES_DIR / f"{video_id}.json"
    if out_path.exists() and not force:
        return out_path

    transcript_path = YOUTUBE_TRANSCRIPTS_DIR / f"{video_id}.json"
    data = _load_json(transcript_path)
    text = _transcript_text(data)
    if not text.strip():
        raise ValueError(f"Empty transcript for {video_id}")

    # Keep prompt bounded for long videos.
    excerpt = text if len(text) < 24000 else text[:24000] + "\n...[truncated]..."

    llm = get_summary_llm(model=model, temperature=0.1)
    parser = PydanticOutputParser(pydantic_object=YoutubeOverview)
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """You are an expert education content curator building a searchable knowledge base.

Summarize this unique YouTube video. Be specific and concrete.
Do not invent studies, statistics, or named entities not in the transcript.

Output MUST follow this schema:
{format_instructions}
""",
            ),
            (
                "user",
                "Title: {title}\nURL: {url}\nBucket: {bucket}\n\nTranscript:\n{text}\n",
            ),
        ]
    )
    chain = prompt | llm | parser
    result = chain.invoke(
        {
            "title": title,
            "url": url,
            "bucket": bucket,
            "text": excerpt,
            "format_instructions": parser.get_format_instructions(),
        }
    )

    payload: dict[str, Any] = {
        "video_id": video_id,
        "doc_id": video_id,
        "title": title,
        "url": url,
        "canonical_url": url,
        "bucket": bucket,
        "source_type": source_type,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "generated_content": result.model_dump(),
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out_path
