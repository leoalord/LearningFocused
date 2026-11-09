"""Podcast transcript chunking: sizes, question carry-over, speakers, coverage, topics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.pipeline.audio.chunking import (
    HOST_NAME,
    NARRATOR,
    ChunkStats,
    chunk_header,
    chunk_transcript,
    normalize_speaker,
    normalize_text,
    pack_chunks,
    transcript_units,
    unit_topics,
)
from src.pipeline.audio.index_chroma import create_transcript_chunk_documents


def _u(speaker: str, text: str, start: float, end: float) -> dict:
    return {"speaker": "A", "speaker_name": speaker, "text": text, "start_time": start, "end_time": end}


def _sentences(n: int, word: str = "learning") -> str:
    return " ".join(f"Sentence {i} talks about {word} in some detail here." for i in range(n))


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Mackenzie Price", HOST_NAME),
        ("McKenzie Price (Host)", HOST_NAME),
        ("mackenzie", HOST_NAME),
        ("Narrator", NARRATOR),
        ("", "Unattributed"),
        ("Jessica Leahy", "Jessica Leahy"),
    ],
)
def test_normalize_speaker(raw: str, expected: str) -> None:
    assert normalize_speaker(raw) == expected


def test_normalize_text_fixes_host_name_but_not_the_firm() -> None:
    out = normalize_text("I'm McKenzie Price. Mackenzie said McKinsey published a report.")
    assert out == "I'm MacKenzie Price. MacKenzie said McKinsey published a report."


def test_narrator_intro_inside_a_guest_turn_is_relabelled() -> None:
    utt = [
        _u("Jessica Leahy", "Welcome to the Future of Education podcast hosted by Mackenzie Price, the co founder "
           "of Two Hour Learning. Here's our host, Mackenzie Price. Thanks for having me.", 19.0, 34.0),
    ]
    stats = ChunkStats()
    units = transcript_units(utt, stats=stats)
    assert [u.speaker for u in units] == [NARRATOR, "Jessica Leahy"]
    assert units[0].text.endswith("Here's our host, MacKenzie Price.")
    assert stats.narrator_intro_fixed == 1
    assert units[0].start == 19.0 and units[-1].end == 34.0


def test_long_turn_is_split_flagged_and_fully_covered() -> None:
    text = _sentences(400)  # ~4,000 words: what diarization collapse looks like
    utt = [_u("MacKenzie Price", "Intro line.", 0, 5), _u("MacKenzie Price", text, 5, 1800)]
    segments = [{"topic": f"T{i}", "start_time": i * 180.0, "end_time": (i + 1) * 180.0} for i in range(10)]
    chunks = chunk_transcript(utt, segments)
    assert len(chunks) >= 8
    assert all(c.tokens <= 800 for c in chunks)
    assert any(c.long_turn for c in chunks)
    # Every topic segment gets content now, instead of 9 empty ones.
    assert {t for c in chunks for t in c.topics} == {f"T{i}" for i in range(10)}
    joined = " ".join(c.render() for c in chunks)
    for i in (0, 199, 399):
        assert f"Sentence {i} talks" in joined


def test_question_moves_to_the_chunk_with_its_answer() -> None:
    utt = [
        _u("MacKenzie Price", _sentences(12), 0, 100),
        _u("MacKenzie Price", "So what struggles do parents face with technology?", 100, 105),
        _u("Guest Person", "Well, absolutely. Addiction is the first one. " + _sentences(12, "phones"), 105, 220),
    ]
    units = transcript_units(utt)
    stats = ChunkStats()
    # Target reached right after the question, so a naive packer would end the chunk on it.
    chunks = pack_chunks(units, target_tokens=units[0].tokens + units[1].tokens, max_tokens=500, stats=stats)
    assert stats.question_carries == 1
    assert chunks[0].units[-1].text.endswith("detail here.")
    assert [u.text.split(".")[0] for u in chunks[1].units[:2]] == [
        "So what struggles do parents face with technology?", "Well, absolutely"]


def test_short_last_unit_repeats_as_overlap() -> None:
    utt = [
        _u("A Person", _sentences(30), 0, 100),
        _u("B Person", "Right.", 100, 101),
        _u("A Person", _sentences(30, "school"), 101, 200),
    ]
    chunks = pack_chunks(transcript_units(utt), target_tokens=300, max_tokens=500)
    assert len(chunks) >= 2
    repeated = [c for c in chunks[1:] if c.overlap_units]
    assert repeated and repeated[0].units[0].text in chunks[chunks.index(repeated[0]) - 1].render()


def test_chunks_follow_topic_boundaries_and_merge_tiny_topics() -> None:
    utt = [
        _u("A Person", _sentences(20, "pacing"), 0, 100),  # topic 0, ~250 tokens
        _u("A Person", "Quick aside.", 100, 102),  # topic 1, tiny: merges forward
        _u("B Person", _sentences(20, "guides"), 102, 200),  # topic 2
        _u("A Person", _sentences(150, "mastery"), 200, 900),  # topic 3, ~1,800 tokens: split evenly
    ]
    segments = [
        {"topic": "Pacing", "start_time": 0, "end_time": 100},
        {"topic": "Aside", "start_time": 100, "end_time": 102},
        {"topic": "Guides", "start_time": 102, "end_time": 200},
        {"topic": "Mastery", "start_time": 200, "end_time": 900},
    ]
    chunks = chunk_transcript(utt, segments)
    assert [c.topic for c in chunks][:2] == ["Pacing", "Guides"]
    assert "Quick aside." in chunks[1].render() and "Aside" in chunks[1].topics
    mastery = [c for c in chunks if c.topic == "Mastery"]
    assert len(mastery) in (3, 4)  # ~1,700 tokens split near the even target of ~560
    assert all(c.tokens <= 800 for c in chunks)
    # No unit repeats across a topic change; within a topic, short units may.
    assert chunks[1].overlap_units == 0 and chunks[2].overlap_units == 0


def test_unit_topics_uses_nearest_segment_for_gaps() -> None:
    units = transcript_units([_u("A", "One.", 0, 2), _u("A", "Two.", 40, 42), _u("A", "Three.", 95, 97)])
    segments = [{"start_time": 0, "end_time": 10}, {"start_time": 90, "end_time": 100}]
    assert unit_topics(units, segments) == [0, 0, 1]


def test_header_has_title_topic_and_named_speakers() -> None:
    utt = [_u("Mackenzie Price", "Hello there.", 0, 2), _u("Carol Melleta", "Hi.", 2, 3)]
    (chunk,) = chunk_transcript(utt, [{"topic": "Intro", "start_time": 0, "end_time": 10, "summary": "x"}])
    header = chunk_header("S E101 Managing Technology.mp3", chunk)
    assert header == "Episode: S E101 Managing Technology.mp3\nTopic: Intro\nSpeakers: MacKenzie Price, Carol Melleta"


def test_documents_use_current_speaker_names_not_segment_letters() -> None:
    transcript = {
        "meta_data": {"og_file_name": "S E104 The Revolutionary 2-Hour Learning System.mp3"},
        "transcript": [_u("Mackenzie Price", "Welcome everyone.", 0, 3), _u("Jay Lyons", "Glad to be here.", 3, 6)],
    }
    segmented = {"title": "S E104 The Revolutionary 2-Hour Learning System.mp3", "segments": [
        {"topic": "Intro", "start_time": 0, "end_time": 6, "summary": "Generated summary", "speakers": ["A", "B"],
         "content": "A: Welcome everyone. B: Glad to be here."}]}
    (doc,) = create_transcript_chunk_documents(transcript, segmented)
    assert "MacKenzie Price: Welcome everyone." in doc.page_content
    assert "A:" not in doc.page_content and "Generated summary" not in doc.page_content
    assert doc.metadata["speakers"] == "MacKenzie Price, Jay Lyons"
    assert doc.metadata["episode_id"] == "S E104"
    assert doc.metadata["_chroma_id"] == "transcript_segment_" + doc.metadata["_chroma_id"].split("_", 2)[2]


def test_real_transcripts_stay_in_bounds() -> None:
    root = Path(__file__).resolve().parents[2] / "transcripts"
    files = sorted(root.glob("S2E339*.json"))[:1] + sorted(root.glob("S E101 *.json"))[:1]
    if not files:
        pytest.skip("transcripts/ not present")
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        docs = create_transcript_chunk_documents(data)
        ids = [d.metadata["_chroma_id"] for d in docs]
        assert len(ids) == len(set(ids))
        words = sum(len(u["text"].split()) for u in data["transcript"])
        kept = sum(len(d.page_content.split("\n\n", 1)[1].split()) for d in docs)
        assert kept >= words  # nothing dropped (overlap and speaker prefixes only add)
