"""Episode id parsing and collision-free Chroma keys."""

from src.pipeline.audio.episode_ids import (
    UNKNOWN_EPISODE_ID,
    episode_key,
    parse_episode_id,
    parse_season_episode,
)
from src.pipeline.audio.generate_summaries import episode_sort_key
from src.pipeline.audio.index_chroma import create_transcript_documents


def test_parses_both_filename_conventions():
    assert parse_episode_id("S2E335 Alpha Anywhere Elite At-Home Academics.mp3") == "S2E335"
    assert parse_episode_id("S E18 Micro-School Commonly Asked Questions.mp3") == "S E18"
    assert parse_episode_id("SE7 Welcome to the Future of Education.mp3") == "SE7"


def test_space_form_is_not_truncated_to_S():
    """The old `.split(" ")[0]` collapsed 187 episodes onto the literal "S"."""
    assert parse_episode_id("S E76 What Are the Benefits of Play Therapy.mp3") != "S"


def test_artifacts_without_an_episode_number():
    assert parse_episode_id("Intro to Academics Accelerate your student.mp3") == UNKNOWN_EPISODE_ID
    assert parse_episode_id("") == UNKNOWN_EPISODE_ID


def test_episode_key_separates_reused_episode_numbers():
    """The feed reuses `S E2` across two different recordings."""
    left = episode_key("S E2", "S E2 Ep 2 - Do the Laundry! Preparing GT Kids for Real Life")
    right = episode_key("S E2", "S E2 Future of Education A New Way to Do School")
    assert left != right


def test_parse_season_episode_defaults_missing_season_to_one():
    assert parse_season_episode("S2E335 Alpha Anywhere.mp3") == (2, 335)
    assert parse_season_episode("S E76 Play Therapy.mp3") == (1, 76)
    assert parse_season_episode("Intro to Academics.mp3") is None


def test_sort_orders_S_E_episodes_numerically_not_lexically():
    """Batching merges multi-part series only when episodes sort adjacent."""
    names = [
        "S E100 Hundred.mp3",
        "S E2 Two.mp3",
        "S E20 Twenty.mp3",
        "S E3 Three.mp3",
    ]
    ordered = sorted(names, key=episode_sort_key)
    assert ordered == [
        "S E2 Two.mp3",
        "S E3 Three.mp3",
        "S E20 Twenty.mp3",
        "S E100 Hundred.mp3",
    ]


def test_sort_places_numbered_episodes_before_untitled_and_seasons_in_order():
    names = ["Intro to Life Skills.mp3", "S2E290 Later.mp3", "S E76 Earlier.mp3"]
    assert sorted(names, key=episode_sort_key) == [
        "S E76 Earlier.mp3",
        "S2E290 Later.mp3",
        "Intro to Life Skills.mp3",
    ]


def _segment(topic: str, start: float) -> dict:
    return {"topic": topic, "start_time": start, "end_time": start + 1, "content": "text"}


def test_indexer_repairs_stale_episode_id_without_resegmenting():
    docs = create_transcript_documents(
        {
            "episode_id": "S",  # what pre-fix segmentation wrote to disk
            "title": "S E18 Micro-School Commonly Asked Questions.mp3",
            "segments": [_segment("Intro", 0.4)],
        }
    )
    assert docs[0].metadata["episode_id"] == "S E18"


def test_same_topic_and_start_in_different_episodes_get_distinct_ids():
    shared = [_segment("Introduction", 0.4)]
    left = create_transcript_documents(
        {"episode_id": "S", "title": "S E109 What Can My Kids Learn From Failure.mp3", "segments": shared}
    )
    right = create_transcript_documents(
        {"episode_id": "S", "title": "S E129 How Teachers Can Lead During the Crisis.mp3", "segments": shared}
    )
    assert left[0].metadata["_chroma_id"] != right[0].metadata["_chroma_id"]
