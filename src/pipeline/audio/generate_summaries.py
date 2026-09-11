import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from dotenv import load_dotenv
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from src.pipeline.audio.episode_ids import parse_season_episode

# LLM selection (primary + fallbacks) is configured via env vars in this helper module.
from src.pipeline.audio.llm_config import get_grouping_llm, get_combined_summary_llm

# Load environment variables
load_dotenv()


class EpisodeGroup(BaseModel):
    group_id: str = Field(
        description="A unique identifier for this group (e.g., 'S2E278-279 Alpha Haters Debate')"
    )
    filenames: List[str] = Field(description="List of filenames belonging to this group")
    reasoning: str = Field(
        description="Explanation of why these episodes are grouped, citing specific metadata clues"
    )


class GroupingResponse(BaseModel):
    groups: List[EpisodeGroup] = Field(description="List of episode groups identified")


class CombinedSummary(BaseModel):
    overview: str = Field(
        description="A cohesive narrative summary synthesizing the content from all episodes in the group."
    )
    themes: List[str] = Field(description="List of core themes and topics discussed.")
    key_takeaways: List[str] = Field(description="List of actionable or insightful takeaways for the listener.")
    value_proposition: str = Field(
        description="Explanation of why someone should listen to this content (the 'hook')."
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


def get_all_filenames(transcripts_dir: Path) -> List[str]:
    return [f.name for f in transcripts_dir.glob("*.json")]


def load_metadata(file_path: Path) -> Dict[str, Any]:
    if not file_path.exists():
        return {}
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


# Gemini 504'd when grouping ~332 filenames in one call. Keep batches in this range.
GROUPING_BATCH_SIZE = 60
GROUPING_BATCH_OVERLAP = 8


def episode_sort_key(filename: str) -> tuple[int, int, int, str]:
    """Sort numbered episodes (S2E290, S E76) before unnumbered titles, then by season/episode.

    Batching relies on this ordering: sequential episodes must land adjacent so
    overlapping windows can merge multi-part series that straddle a batch edge.
    """
    parsed = parse_season_episode(filename)
    if parsed:
        season, episode = parsed
        return (0, season, episode, filename.lower())
    return (1, 0, 0, filename.lower())


def iter_grouping_batches(
    filenames: Sequence[str],
    batch_size: int = GROUPING_BATCH_SIZE,
    overlap: int = GROUPING_BATCH_OVERLAP,
) -> List[List[str]]:
    """Split a sorted filename list into overlapping windows.

    Overlap at batch boundaries lets Part 1 / Part 2 series that straddle a cut
    still be grouped together, then merge_and_dedupe_groups assigns each file once.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    if overlap < 0:
        raise ValueError("overlap must be >= 0")
    if overlap >= batch_size:
        raise ValueError("overlap must be smaller than batch_size")

    ordered = list(filenames)
    n = len(ordered)
    if n == 0:
        return []
    if n <= batch_size:
        return [ordered]

    step = batch_size - overlap
    batches: List[List[str]] = []
    start = 0
    while True:
        end = min(start + batch_size, n)
        batches.append(ordered[start:end])
        if end >= n:
            break
        start += step
    return batches


def _is_grouping_timeout(exc: BaseException) -> bool:
    text = str(exc).lower()
    tokens = (
        "504",
        "timeout",
        "timed out",
        "deadline",
        "deadline exceeded",
        "gateway timeout",
    )
    return any(token in text for token in tokens)


def _unique_preserve_order(items: Sequence[str]) -> List[str]:
    seen: set[str] = set()
    unique: List[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def merge_and_dedupe_groups(
    filenames: Sequence[str],
    groups: Sequence[EpisodeGroup],
) -> List[EpisodeGroup]:
    """Union groups that share filenames (batch-boundary overlap), then assign each file once."""
    allowed = set(filenames)
    parent: dict[str, str] = {name: name for name in filenames}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    contributing: List[EpisodeGroup] = []
    for group in groups:
        valid = _unique_preserve_order([name for name in group.filenames if name in allowed])
        if not valid:
            continue
        for left, right in zip(valid, valid[1:]):
            union(left, right)
        contributing.append(
            EpisodeGroup(group_id=group.group_id, filenames=valid, reasoning=group.reasoning)
        )

    components: dict[str, List[str]] = defaultdict(list)
    for name in filenames:
        components[find(name)].append(name)

    groups_by_root: dict[str, List[EpisodeGroup]] = defaultdict(list)
    for group in contributing:
        groups_by_root[find(group.filenames[0])].append(group)

    merged: List[EpisodeGroup] = []
    used_ids: set[str] = set()
    for root, names in components.items():
        unique_names = _unique_preserve_order(names)
        related = groups_by_root.get(root, [])
        if related:
            multi = [g for g in related if len(g.filenames) > 1]
            pick = (multi or related)[0]
            group_id = pick.group_id
            extras = [g.group_id for g in related if g.group_id != pick.group_id]
            reasoning = pick.reasoning
            if extras:
                reasoning = f"{reasoning} (merged overlapping batch groups: {', '.join(extras)})"
        else:
            group_id = unique_names[0]
            reasoning = "Ungrouped leftover from batched grouping; assigned as a singleton."

        original_id = group_id
        suffix = 2
        while group_id in used_ids:
            group_id = f"{original_id}-{suffix}"
            suffix += 1
        used_ids.add(group_id)
        merged.append(EpisodeGroup(group_id=group_id, filenames=unique_names, reasoning=reasoning))

    merged.sort(key=lambda group: episode_sort_key(group.filenames[0] if group.filenames else group.group_id))
    return merged


def _build_episode_context(fname: str, metadata_dir: Path) -> Dict[str, Any]:
    meta = load_metadata(metadata_dir / fname)
    return {
        "filename": fname,
        "title": meta.get("title", fname),
        "summary": (
            meta.get("summary", "")[:200] + "..." if meta.get("summary") else "No summary"
        ),
        "published": meta.get("published", "Unknown date"),
        "episode_number": meta.get("itunes_episode", "Unknown"),
    }


def _group_single_batch(episodes_context: List[Dict[str, Any]], llm: Any) -> List[EpisodeGroup]:
    """Group one batch of episode metadata via get_grouping_llm. Do not send the full catalog."""
    parser = PydanticOutputParser(pydantic_object=GroupingResponse)
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """You are an expert content librarian.
Your task is to group podcast episodes that belong to the same multi-part series or specific topic conversation.

This list is one contiguous batch from a larger catalog. Group only among these episodes.

Guidelines:
1. Analyze the provided list of episodes, including their filenames, titles, summaries, and publication dates.
2. Group episodes that are explicitly labeled as "Part 1", "Part 2", etc.
3. Group episodes that share a very specific, unique topic or guest, even if "Part X" is missing from the title, especially if they were published sequentially.
4. Episodes that are standalone interviews or topics should be in their own single-episode group.
5. Every provided filename MUST be assigned to exactly one group.
6. Create a descriptive group_id for each group.
7. Do not invent filenames that are not in the list.

{format_instructions}
""",
            ),
            ("user", "Episode List:\n{episodes_json}"),
        ]
    )
    chain = prompt | llm | parser
    batch_filenames = [item["filename"] for item in episodes_context]
    try:
        result = chain.invoke(
            {
                "episodes_json": json.dumps(episodes_context, indent=2),
                "format_instructions": parser.get_format_instructions(),
            }
        )
        return result.groups
    except Exception as e:
        if _is_grouping_timeout(e):
            print(f"Error grouping episode batch ({len(batch_filenames)} files): {e}")
            raise
        print(f"Error grouping episode batch ({len(batch_filenames)} files): {e}")
        return [
            EpisodeGroup(group_id=f, filenames=[f], reasoning="Fallback error")
            for f in batch_filenames
        ]


def group_episodes(
    filenames: List[str],
    metadata_dir: Path,
    *,
    batch_size: int = GROUPING_BATCH_SIZE,
    overlap: int = GROUPING_BATCH_OVERLAP,
) -> List[EpisodeGroup]:
    print("Grouping episodes based on filenames and metadata...")
    if not filenames:
        return []

    ordered = sorted(filenames, key=episode_sort_key)
    batches = iter_grouping_batches(ordered, batch_size=batch_size, overlap=overlap)
    print(
        f"Grouping {len(ordered)} episodes in {len(batches)} batches "
        f"(batch_size={batch_size}, overlap={overlap})."
    )

    llm = get_grouping_llm(temperature=0.0)
    batch_groups: List[EpisodeGroup] = []
    for i, batch in enumerate(batches, start=1):
        print(f"  Grouping batch {i}/{len(batches)} ({len(batch)} episodes)...")
        context = [_build_episode_context(fname, metadata_dir) for fname in batch]
        batch_groups.extend(_group_single_batch(episodes_context=context, llm=llm))

    merged = merge_and_dedupe_groups(ordered, batch_groups)
    assigned = [name for group in merged for name in group.filenames]
    if sorted(assigned) != sorted(ordered):
        raise RuntimeError(
            "Batched grouping failed uniqueness check: every filename must appear in exactly one group."
        )
    print(f"Merged to {len(merged)} groups after overlap dedupe.")
    return merged


def generate_combined_summary(
    group: EpisodeGroup, transcripts_dir: Path, metadata_dir: Path
) -> Optional[Dict[str, Any]]:
    print(f"Generating summary for group: {group.group_id} ({len(group.filenames)} episodes)...")

    full_text = ""
    episodes_data = []

    sorted_filenames = sorted(group.filenames)

    for filename in sorted_filenames:
        transcript_path = transcripts_dir / filename
        metadata_path = metadata_dir / filename

        if not transcript_path.exists():
            print(f"Warning: File {filename} not found.")
            continue

        data = load_transcript(transcript_path)
        meta = load_metadata(metadata_path)

        episodes_data.append(
            {
                "filename": filename,
                "title": meta.get("title", data.get("meta_data", {}).get("og_file_name", filename)),
                "publisher_summary": meta.get("summary", ""),
                "published_date": meta.get("published", ""),
                "full_metadata": meta,
            }
        )

        transcript_text = format_transcript_for_llm(data.get("transcript", []))
        full_text += f"\n\n--- Start of Episode: {filename} ---\n\n"
        full_text += transcript_text

    if not full_text:
        return None

    llm = get_combined_summary_llm(temperature=0.1)

    parser = PydanticOutputParser(pydantic_object=CombinedSummary)

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """You are an expert podcast content curator.
Your task is to create a high-level summary for a group of related podcast episodes.
These episodes might be a multi-part series or a single episode.

Guidelines:
1. Synthesize the content from all provided transcripts into a cohesive narrative.
2. Identify the core themes and topics.
3. Extract key takeaways that provide value to the listener.
4. Explain WHY someone should listen to this (the "hook" or value proposition).
5. The output will be used for knowledge embedding and search, so be comprehensive yet concise.

{format_instructions}
""",
            ),
            ("user", "Podcast Transcripts:\n{transcript_text}"),
        ]
    )

    chain = prompt | llm | parser

    try:
        result = chain.invoke(
            {
                "transcript_text": full_text,
                "format_instructions": parser.get_format_instructions(),
            }
        )

        return {
            "id": group.group_id,
            "group_title": episodes_data[0]["title"] if episodes_data else group.group_id,
            "episode_count": len(episodes_data),
            "generated_content": result.model_dump(),
            "episodes": episodes_data,
            "type": "series" if len(episodes_data) > 1 else "episode",
        }

    except Exception as e:
        print(f"Error generating summary for {group.group_id}: {e}")
        return None


def save_groupings(groups: List[EpisodeGroup], output_dir: Path) -> None:
    grouping_data = [group.model_dump() for group in groups]
    output_path = output_dir / "episode_groupings.json"

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(grouping_data, f, indent=2)
    print(f"Saved grouping configuration to {output_path}")


def load_existing_processed_episodes(output_dir: Path) -> set[frozenset]:
    processed_groups: set[frozenset] = set()

    if not output_dir.exists():
        return processed_groups

    for filename in output_dir.glob("summary_*.json"):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                data = json.load(f)
                episodes = data.get("episodes", [])
                if episodes:
                    group_filenames = frozenset(
                        ep.get("filename") for ep in episodes if ep.get("filename")
                    )
                    if group_filenames:
                        processed_groups.add(group_filenames)
        except Exception as e:
            print(f"Warning: Could not read existing summary {filename}: {e}")

    return processed_groups


def process_combined_summaries(transcripts_dir: Path, metadata_dir: Path, output_dir: Path) -> None:
    """
    Main function to process and generate combined summaries for all transcripts.
    """
    filenames = get_all_filenames(transcripts_dir)
    if not filenames:
        print("No transcript files found.")
        return

    groups = group_episodes(filenames, metadata_dir)
    print(f"Identified {len(groups)} groups.")

    save_groupings(groups, output_dir)

    existing_processed_sets = load_existing_processed_episodes(output_dir)
    print(f"Found {len(existing_processed_sets)} existing summary groups.")

    for group in groups:
        current_group_set = frozenset(group.filenames)
        if current_group_set in existing_processed_sets:
            print(f"Skipping group '{group.group_id}' - already processed (exact match).")
            continue

        summary_data = generate_combined_summary(group, transcripts_dir, metadata_dir)
        if summary_data:
            safe_name = "".join(
                [c if c.isalnum() or c in (" ", "-", "_") else "" for c in group.group_id]
            ).strip()
            safe_name = safe_name.replace(" ", "_")[:50]
            output_path = output_dir / f"summary_{safe_name}.json"

            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(summary_data, f, indent=2)
            print(f"Saved summary to {output_path}")

            existing_processed_sets.add(current_group_set)


def main() -> None:
    from src.config import TRANSCRIPTS_DIR, METADATA_DIR, COMBINED_DIR

    process_combined_summaries(TRANSCRIPTS_DIR, METADATA_DIR, COMBINED_DIR)


if __name__ == "__main__":
    main()


