import json
import os
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from src.pipeline.audio.episode_ids import parse_episode_id
from src.pipeline.audio.llm_config import get_segmentation_llm

# Load environment variables
load_dotenv()


class TopicSegment(BaseModel):
    topic_label: str = Field(description="A concise label for the topic discussed in this segment")
    start_time: float = Field(description="The start timestamp of the segment in seconds")
    end_time: float = Field(description="The end timestamp of the segment in seconds")
    summary: str = Field(description="A brief summary of what is discussed in this segment")


class SegmentationResponse(BaseModel):
    segments: List[TopicSegment] = Field(
        description="List of topic segments covering the entire transcript"
    )


def load_transcript(file_path: Path) -> Dict[str, Any]:
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def format_transcript_for_llm(transcript_data: List[Dict[str, Any]]) -> str:
    """
    Formats the transcript into a readable string for the LLM.
    Includes timestamps to help the LLM identify boundaries.
    """
    formatted_text = ""
    for segment in transcript_data:
        speaker = segment.get("speaker_name", segment.get("speaker", "Unknown"))
        start = segment.get("start_time", 0)
        text = segment.get("text", "")
        formatted_text += f"[{start:.2f}s] {speaker}: {text}\n"
    return formatted_text


def segment_transcript(file_path: Path, output_dir: Path) -> None:
    print(f"Processing {file_path.name}...")

    data = load_transcript(file_path)
    transcript_text = format_transcript_for_llm(data.get("transcript", []))

    llm = get_segmentation_llm(temperature=0.0)

    parser = PydanticOutputParser(pydantic_object=SegmentationResponse)

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """You are an expert content analyzer for educational podcasts.
Your task is to segment the provided transcript into coherent topic chunks.

Guidelines:
1. Identify major topic transitions in the conversation.
2. Create segments that cover the ENTIRE transcript from start to finish without gaps.
3. Each segment should represent a distinct subject or discussion point.
4. For each segment, provide a concise topic label, exact start/end times based on the text provided, and a brief summary.
5. Timestamps should align with the provided [timestamp] markers in the text.
6. Ignore minor banter unless it constitutes a meaningful transition or intro/outro.
7. The output must be valid JSON matching the specified structure.

{format_instructions}
""",
            ),
            ("user", "{transcript}"),
        ]
    )

    chain = prompt | llm | parser

    try:
        result = chain.invoke(
            {
                "transcript": transcript_text,
                "format_instructions": parser.get_format_instructions(),
            }
        )

        processed_segments = []
        original_transcript = data.get("transcript", [])

        for segment in result.segments:
            segment_text = ""
            segment_speakers = set()

            for entry in original_transcript:
                entry_start = entry.get("start_time", 0)
                entry_end = entry.get("end_time", 0)

                if entry_start >= segment.start_time and entry_end <= segment.end_time:
                    segment_text += (
                        f"{entry.get('speaker_name', entry.get('speaker'))}: {entry.get('text')} "
                    )
                    segment_speakers.add(entry.get("speaker_name", entry.get("speaker")))
                elif entry_start < segment.end_time and entry_end > segment.start_time:
                    midpoint = (entry_start + entry_end) / 2
                    if segment.start_time <= midpoint <= segment.end_time:
                        segment_text += (
                            f"{entry.get('speaker_name', entry.get('speaker'))}: {entry.get('text')} "
                        )
                        segment_speakers.add(entry.get("speaker_name", entry.get("speaker")))

            processed_segments.append(
                {
                    "topic": segment.topic_label,
                    "start_time": segment.start_time,
                    "end_time": segment.end_time,
                    "summary": segment.summary,
                    "speakers": list(segment_speakers),
                    "content": segment_text.strip(),
                }
            )

        og_file_name = data.get("meta_data", {}).get("og_file_name", "")
        output_data = {
            "episode_id": parse_episode_id(og_file_name),
            "title": og_file_name,
            "original_meta": data.get("meta_data", {}),
            "segments": processed_segments,
        }

        output_path = output_dir / f"{file_path.stem}_segmented.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2)

        print(f"Saved segmented transcript to {output_path}")

    except Exception as e:
        print(f"Error processing {file_path.name}: {e}")


def main() -> None:
    from src.config import TRANSCRIPTS_DIR, SEGMENTED_DIR

    files = list(TRANSCRIPTS_DIR.glob("*.json"))
    if not files:
        print("No transcript files found.")
        return

    for file_path in files:
        segment_transcript(file_path, SEGMENTED_DIR)


if __name__ == "__main__":
    main()


