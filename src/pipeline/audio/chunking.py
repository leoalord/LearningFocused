"""Build retrieval and graph chunks from raw podcast transcripts.

Chunks used to be the LLM's topic segments, which vary from empty to 6,000+ tokens,
often end on a host's question with the answer in the next chunk, drop utterances
that fall between segment timestamps, and keep A/B speaker labels when segmentation
ran before speaker ID. See docs/transcription_chunking_audit.md.

Here chunks are packed deterministically from the transcript's utterances:

- long utterances are split at sentence boundaries (diarization sometimes returns a
  whole episode as one utterance, which left every other topic segment empty); chunks
  from turns over LONG_TURN_WORDS are flagged `long_turn` because their speaker label
  may cover several people;
- chunks aim for `target_tokens`, never exceed `max_tokens` unless one sentence does,
  repeat the previous chunk's last short utterance for context, and never end on a
  question that a different speaker answers next;
- every utterance lands in at least one chunk;
- speaker names come from the transcript (`speaker_name`), normalized;
- the LLM topic label is kept as metadata and a header line; its summary is not
  embedded, because it was generated rather than said.

Pure functions only: no file or network access.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Mapping, Sequence

HOST_NAME = "MacKenzie Price"
NARRATOR = "Narrator"
UNATTRIBUTED = "Unattributed"

DEFAULT_TARGET_TOKENS = 450  # only without topic segments; with them, chunks follow topics
DEFAULT_MAX_TOKENS = 800
DEFAULT_MIN_TOKENS = 150
DEFAULT_MAX_UNIT_TOKENS = 250
OVERLAP_MAX_TOKENS = 120
# Diarization sometimes returns most of an episode as one turn (the audit found 17,
# 1,700-5,557 words). Usually that is a solo episode and the label is right, but in
# live Q+A episodes it merges several people, so such turns are flagged, not relabelled.
LONG_TURN_WORDS = 1500
NARRATOR_INTRO_WINDOW_SECONDS = 240.0

# The ASR spells the host's name several ways (Mackenzie 1,063, MacKenzie 198,
# McKenzie 132, McKinsey 13). "McKinsey" alone is left alone: it is also the firm.
_HOST_TEXT_PATTERNS = [
    (re.compile(r"\b(?:Mackenzie|McKenzie|Mckenzie|McKinsey|Makenzie)\s+Price\b"), HOST_NAME),
    (re.compile(r"\b(?:Mackenzie|McKenzie|Mckenzie)\b"), "MacKenzie"),
]
_SPEAKER_SUFFIX = re.compile(r"\s*\((?:host|co-?host|guest|narrator)\)\s*$", re.IGNORECASE)
_HOST_SPEAKER = re.compile(r"^(?:mac|mc|ma)k[ei]n[sz]i?e(?:y)?(?:\s+price)?$", re.IGNORECASE)

_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=[\"'(\[]?[A-Z0-9])")
_NARRATOR_INTRO = re.compile(r"welcome to (?:the )?future of education(?: podcast)?,? hosted by", re.IGNORECASE)
_NARRATOR_INTRO_END = re.compile(r"here'?s (?:our|your) host|join us as we|let'?s (?:get started|dive in)", re.IGNORECASE)
NARRATOR_INTRO_MAX_SENTENCES = 4


@lru_cache(maxsize=1)
def _encoder() -> Any:
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")  # text-embedding-3-small's tokenizer


def count_tokens(text: str) -> int:
    return len(_encoder().encode(text, disallowed_special=()))


def normalize_text(text: str) -> str:
    """Canonical spelling of the host's name in transcript text."""
    for pattern, repl in _HOST_TEXT_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def normalize_speaker(name: str | None) -> str:
    """Canonical speaker label: one spelling for the host, no "(Host)" suffixes."""
    if not name or not str(name).strip():
        return UNATTRIBUTED
    cleaned = _SPEAKER_SUFFIX.sub("", str(name).strip())
    if _HOST_SPEAKER.match(cleaned):
        return HOST_NAME
    if cleaned.lower() in {"narrator", "announcer", "intro narrator"}:
        return NARRATOR
    return normalize_text(cleaned)


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_END.split(text.strip())]
    return [p for p in parts if p]


@dataclass(frozen=True)
class Unit:
    """One speaker turn, or a sentence-bounded piece of a long one."""

    speaker: str
    text: str
    start: float
    end: float
    tokens: int
    long_turn: bool = False

    def render(self) -> str:
        return f"{self.speaker}: {self.text}"


@dataclass
class Chunk:
    units: list[Unit]
    topic: str | None = None
    topic_index: int | None = None
    topics: list[str] = field(default_factory=list)  # every topic segment the chunk overlaps
    overlap_units: int = 0

    @property
    def start(self) -> float:
        return self.units[0].start

    @property
    def end(self) -> float:
        return self.units[-1].end

    @property
    def tokens(self) -> int:
        return sum(u.tokens for u in self.units)

    @property
    def speakers(self) -> list[str]:
        seen: dict[str, None] = {}
        for u in self.units:
            seen.setdefault(u.speaker, None)
        return list(seen)

    @property
    def long_turn(self) -> bool:
        return any(u.long_turn for u in self.units)

    def render(self) -> str:
        return "\n".join(u.render() for u in self.units)


@dataclass
class ChunkStats:
    utterances: int = 0
    long_turns: int = 0
    narrator_intro_fixed: int = 0
    chunks: int = 0
    question_carries: int = 0
    tokens: list[int] = field(default_factory=list)


def _piece_times(start: float, end: float, pieces: Sequence[str]) -> list[tuple[float, float]]:
    """Interpolate times for pieces of one utterance by character share (no word times on disk)."""
    total = sum(len(p) for p in pieces) or 1
    out, cursor = [], start
    for p in pieces:
        span = (end - start) * len(p) / total
        out.append((cursor, cursor + span))
        cursor += span
    return out


def _pack_sentences(sentences: Sequence[str], max_tokens: int) -> list[str]:
    pieces: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for s in sentences:
        t = count_tokens(s)
        if current and current_tokens + t > max_tokens:
            pieces.append(" ".join(current))
            current, current_tokens = [], 0
        current.append(s)
        current_tokens += t
    if current:
        pieces.append(" ".join(current))
    return pieces


def _narrator_split(sentences: list[str]) -> tuple[list[str], list[str], list[str]] | None:
    """(before, narrator intro, after) when the intro is inside someone else's turn."""
    for i, s in enumerate(sentences):
        if _NARRATOR_INTRO.search(s):
            j = i
            while j < len(sentences) - 1 and j - i + 1 < NARRATOR_INTRO_MAX_SENTENCES:
                if _NARRATOR_INTRO_END.search(sentences[j]):
                    break
                j += 1
            if not _NARRATOR_INTRO_END.search(sentences[j]):
                j = i  # no clear end: relabel only the "Welcome to ... hosted by" sentence
            return sentences[:i], sentences[i : j + 1], sentences[j + 1 :]
    return None


def transcript_units(
    utterances: Sequence[Mapping[str, Any]],
    *,
    max_unit_tokens: int = DEFAULT_MAX_UNIT_TOKENS,
    stats: ChunkStats | None = None,
) -> list[Unit]:
    """Utterances -> speaker-labelled units no longer than `max_unit_tokens` (unless one sentence is)."""
    stats = stats if stats is not None else ChunkStats()
    rows = [u for u in utterances if str(u.get("text") or "").strip()]
    units: list[Unit] = []
    for u in rows:
        stats.utterances += 1
        speaker = normalize_speaker(u.get("speaker_name") or u.get("speaker"))
        text = normalize_text(" ".join(str(u.get("text")).split()))
        start = float(u.get("start_time") or 0.0)
        end = float(u.get("end_time") or start)
        long_turn = len(text.split()) > LONG_TURN_WORDS
        stats.long_turns += long_turn
        sentences = split_sentences(text)

        # (speaker, sentences) groups for this utterance.
        groups: list[tuple[str, list[str]]] = [(speaker, sentences)]
        if start < NARRATOR_INTRO_WINDOW_SECONDS and speaker != NARRATOR:
            split = _narrator_split(sentences)
            if split:
                before, intro, after = split
                stats.narrator_intro_fixed += 1
                groups = [(speaker, before), (NARRATOR, intro), (speaker, after)]

        pieces: list[tuple[str, str]] = [
            (spk, piece) for spk, sents in groups if sents for piece in _pack_sentences(sents, max_unit_tokens)
        ]
        for (spk, piece), (s, e) in zip(pieces, _piece_times(start, end, [p for _, p in pieces])):
            units.append(Unit(spk, piece, round(s, 2), round(e, 2), count_tokens(f"{spk}: {piece}"), long_turn))
    return units


def _ends_on_question_answered_by_other(chunk_units: Sequence[Unit], nxt: Unit) -> bool:
    last = chunk_units[-1]
    return last.text.rstrip().endswith("?") and last.speaker != nxt.speaker


def unit_topics(units: Sequence[Unit], segments: Sequence[Mapping[str, Any]]) -> list[int | None]:
    """Index of the topic segment holding each unit's midpoint (nearest segment if in a gap)."""
    spans = [(float(s.get("start_time") or 0.0), float(s.get("end_time") or 0.0)) for s in segments]
    if not spans:
        return [None] * len(units)
    out: list[int | None] = []
    for u in units:
        mid = (u.start + u.end) / 2
        inside = [i for i, (s, e) in enumerate(spans) if s <= mid <= e]
        out.append(inside[0] if inside else min(range(len(spans)), key=lambda i: min(abs(mid - spans[i][0]), abs(mid - spans[i][1]))))
    return out


def _balanced_targets(units: Sequence[Unit], topics: Sequence[int | None], max_tokens: int) -> list[int]:
    """Per-unit size target: its topic's tokens spread evenly over as few chunks as fit under the cap."""
    totals: dict[int | None, int] = {}
    for u, t in zip(units, topics):
        totals[t] = totals.get(t, 0) + u.tokens
    per_topic = {t: -(-total // -(-total // max_tokens)) if total else max_tokens for t, total in totals.items()}
    return [per_topic[t] for t in topics]


def pack_chunks(
    units: Sequence[Unit],
    *,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    topics: Sequence[int | None] | None = None,
    min_tokens: int = DEFAULT_MIN_TOKENS,
    stats: ChunkStats | None = None,
) -> list[Chunk]:
    """Greedy packing with question carry-over and one unit of overlap.

    With `topics` (a topic index per unit), chunks break at topic changes once they
    hold `min_tokens`, a topic over `max_tokens` is split into even pieces, and no
    unit repeats across a topic change. Without it, chunks aim for `target_tokens`.
    """
    stats = stats if stats is not None else ChunkStats()
    targets = _balanced_targets(units, topics, max_tokens) if topics is not None else [target_tokens] * len(units)
    chunks: list[Chunk] = []
    current: list[Unit] = []
    overlap = 0

    def close(next_unit: Unit, topic_change: bool) -> tuple[list[Unit], int]:
        """Emit `current`; return the units the next chunk starts with and how many repeat."""
        nonlocal current
        carry: list[Unit] = []
        if len(current) - overlap > 1 and _ends_on_question_answered_by_other(current, next_unit):
            carry = [current.pop()]  # the question moves to the chunk with its answer
            stats.question_carries += 1
        chunks.append(Chunk(list(current), overlap_units=overlap))
        if carry:
            return carry, 0
        last = current[-1]
        if not topic_change and len(current) - overlap > 1 and last.tokens <= OVERLAP_MAX_TOKENS:
            return [last], 1
        return [], 0

    for i, unit in enumerate(units):
        size = sum(u.tokens for u in current)
        fresh = sum(u.tokens for u in current[overlap:])
        topic_change = topics is not None and i > 0 and topics[i] != topics[i - 1]
        if current and fresh > 0 and (
            size + unit.tokens > max_tokens
            or (topic_change and fresh >= min_tokens)
            # close when this unit would overshoot the target by more than it undershoots
            or (not topic_change and size + unit.tokens / 2 > targets[i - 1 if i else 0])
        ):
            current, overlap = close(unit, topic_change)
        current.append(unit)
    if len(current) > overlap:  # otherwise only the repeated unit is left, already in the previous chunk
        chunks.append(Chunk(list(current), overlap_units=overlap))

    # A short tail reads better merged into its predecessor when that stays under the cap.
    if len(chunks) >= 2:
        tail, prev = chunks[-1], chunks[-2]
        fresh_units = tail.units[tail.overlap_units:]
        fresh_tokens = sum(u.tokens for u in fresh_units)
        if fresh_tokens < min_tokens and prev.tokens + fresh_tokens <= max_tokens:
            prev.units.extend(fresh_units)
            chunks.pop()

    stats.chunks += len(chunks)
    stats.tokens.extend(c.tokens for c in chunks)
    return chunks


def assign_topics(chunks: Sequence[Chunk], segments: Sequence[Mapping[str, Any]]) -> None:
    """Label each chunk with the LLM topic segment it overlaps most (nearest if none).

    `topics` lists every segment the chunk overlaps, so a topic shorter than a chunk
    still appears somewhere.
    """
    spans = [
        (i, float(s.get("start_time") or 0.0), float(s.get("end_time") or 0.0), s.get("topic"))
        for i, s in enumerate(segments)
    ]
    if not spans:
        return
    for chunk in chunks:
        def score(span: tuple[int, float, float, Any]) -> tuple[float, float]:
            _, s, e, _ = span
            overlap = min(e, chunk.end) - max(s, chunk.start)
            mid = (chunk.start + chunk.end) / 2
            return (overlap, -min(abs(mid - s), abs(mid - e)))

        idx, _, _, topic = max(spans, key=score)
        chunk.topic, chunk.topic_index = (str(topic) if topic else None), idx
        chunk.topics = [str(t) for _, s, e, t in spans if t and min(e, chunk.end) > max(s, chunk.start)]
        if chunk.topic and chunk.topic not in chunk.topics:
            chunk.topics.insert(0, chunk.topic)


def chunk_transcript(
    utterances: Sequence[Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]] = (),
    *,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    stats: ChunkStats | None = None,
) -> list[Chunk]:
    stats = stats if stats is not None else ChunkStats()
    units = transcript_units(utterances, max_unit_tokens=min(DEFAULT_MAX_UNIT_TOKENS, max_tokens), stats=stats)
    topics = unit_topics(units, segments) if segments else None
    chunks = pack_chunks(units, target_tokens=target_tokens, max_tokens=max_tokens, topics=topics, stats=stats)
    assign_topics(chunks, segments)
    return chunks


def chunk_header(title: str, chunk: Chunk) -> str:
    lines = [f"Episode: {title}"]
    if chunk.topic:
        lines.append(f"Topic: {chunk.topic}")
    lines.append(f"Speakers: {', '.join(chunk.speakers)}")
    return "\n".join(lines)
