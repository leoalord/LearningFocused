import json
import os
import sys
from typing import Dict, List

from dotenv import load_dotenv
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import ChatPromptTemplate

from src.pipeline.audio.chunking import normalize_speaker
from src.pipeline.audio.llm_config import get_speaker_id_llm

# Load environment variables
load_dotenv()


def load_transcript(file_path: str) -> List[Dict]:
    """Loads the transcript from a JSON file."""
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("transcript", [])


def format_transcript_for_prompt(transcript_data: List[Dict]) -> str:
    """Formats the transcript data into a string for the LLM."""
    formatted_lines: list[str] = []
    for entry in transcript_data:
        speaker = entry.get("speaker") or "Unknown"
        text = entry.get("text", "")
        formatted_lines.append(f"Speaker {speaker}: {text}")
    return "\n".join(formatted_lines)


def identify_speakers(transcript_path: str) -> Dict[str, str]:
    """
    Identifies speakers in a podcast transcript using Gemini.

    Args:
        transcript_path: Path to the transcript JSON file.

    Returns:
        A dictionary mapping speaker labels (e.g., 'A') to names.
    """
    print(f"Processing: {transcript_path}")

    transcript_data = load_transcript(transcript_path)
    if not transcript_data:
        print("No transcript data found.")
        return {}

    transcript_text = format_transcript_for_prompt(transcript_data)

    llm = get_speaker_id_llm(temperature=0.0)

    system_prompt = """
    You are an intelligent assistant tasked with identifying speakers in a podcast transcript.

    Context:
    - The podcast is "The Future of Education" hosted by MacKenzie Price (spelled exactly that way).
    - MacKenzie Price is the main host.
    - There is a Narrator who typically provides the intro and outro segments. The line
      "Welcome to the Future of Education podcast hosted by MacKenzie Price..." is always the
      Narrator, never a guest, even when a guest's label is attached to it.
    - There may be one or more guests (co-hosts, interviewees, etc.).
    - There are up to 6 speakers.

    Your Task:
    1. Analyze the transcript text provided.
    2. Identify who each Speaker label (e.g., "A", "B", "C") corresponds to based on the content.
       - Clues: Self-introductions ("I'm [Name]"), roles (Host, Narrator), and context of the conversation.
    3. Output a JSON object mapping the speaker label to the identified name.

    Example Output:
    {{
        "A": "MacKenzie Price",
        "B": "Narrator",
        "C": "Guest Name"
    }}

    Use each person's name only, with no role suffix like "(Host)".
    Return ONLY valid JSON.
    """

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", system_prompt),
            ("user", "Here is the transcript:\n\n{transcript_text}"),
        ]
    )

    chain = prompt | llm | JsonOutputParser()

    try:
        result = chain.invoke({"transcript_text": transcript_text})
        return result
    except Exception as e:
        print(f"Error calling Gemini: {e}")
        return {}


def update_transcript_with_speakers(transcript_path: str, speaker_map: Dict[str, str]) -> None:
    """Updates the original JSON file with identified speaker names."""
    with open(transcript_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    speaker_map = {label: normalize_speaker(name) for label, name in speaker_map.items()}
    if "meta_data" not in data:
        data["meta_data"] = {}
    data["meta_data"]["speaker_map"] = speaker_map

    for entry in data.get("transcript", []):
        label = entry.get("speaker")
        if label in speaker_map:
            entry["speaker_name"] = speaker_map[label]

    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Updated transcript saved to: {transcript_path}")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m src.pipeline.audio.identify_speakers <path_to_transcript_json>")
        sys.exit(1)

    transcript_file = sys.argv[1]
    if not os.path.exists(transcript_file):
        print(f"File not found: {transcript_file}")
        sys.exit(1)

    speakers = identify_speakers(transcript_file)
    print("\nIdentified Speakers:")
    print(json.dumps(speakers, indent=2))

    if speakers:
        update_transcript_with_speakers(transcript_file, speakers)


if __name__ == "__main__":
    main()


