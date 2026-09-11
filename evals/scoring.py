"""Pure scoring helpers for the retrieval gold set.

Hit definition (must match evals/README.md):
- retrieval only (concatenated retrieved page_content)
- case-fold both sides
- hit iff every must_include substring appears in that concatenated text
- source_hint is never used to score a hit
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

MISS_NOT_IN_CORPUS = "not_in_corpus"
MISS_RETRIEVER_FAILED = "retriever_failed"

_EPISODE_IN_FILENAME = re.compile(r"^(S2E\d+|S E\d+)\b")
_UNIQUE_EPISODE_ID = re.compile(r"^S\d*E\d+$")


@dataclass(frozen=True)
class GoldQuestion:
    id: str
    question: str
    must_include: tuple[str, ...]
    source_hint: str


@dataclass(frozen=True)
class SourceHintInfo:
    raw: str
    artifact_paths: tuple[str, ...]
    episode_token: str | None
    doc_id: str | None


@dataclass(frozen=True)
class PhraseScore:
    phrase: str
    hit: bool


@dataclass(frozen=True)
class HitResult:
    hit: bool
    phrase_scores: tuple[PhraseScore, ...]
    missing_phrases: tuple[str, ...]


@dataclass
class CorpusStatus:
    artifact_exists: dict[str, bool] = field(default_factory=dict)
    disk_present: bool = False
    indexed: bool = False
    phrases_in_hinted_source: bool = False
    hinted_episode_id: str | None = None
    hinted_title: str | None = None
    hinted_doc_id: str | None = None
    retrieved_episode_ids: list[str] = field(default_factory=list)
    retrieved_doc_ids: list[str] = field(default_factory=list)
    hinted_source_in_results: bool = False


def load_gold_questions(path: Path) -> list[GoldQuestion]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"Gold set must be a JSON array, got {type(raw).__name__}")
    items: list[GoldQuestion] = []
    for i, obj in enumerate(raw):
        if not isinstance(obj, Mapping):
            raise ValueError(f"Gold item {i} is not an object")
        must = obj.get("must_include")
        if not isinstance(must, list) or not all(isinstance(p, str) for p in must):
            raise ValueError(f"Gold item {obj.get('id', i)} has invalid must_include")
        items.append(
            GoldQuestion(
                id=str(obj["id"]),
                question=str(obj["question"]),
                must_include=tuple(must),
                source_hint=str(obj.get("source_hint") or ""),
            )
        )
    return items


def concatenate_retrieved_text(page_contents: Iterable[str]) -> str:
    return "\n".join(page_contents)


def score_hit(*, must_include: Sequence[str], retrieved_text: str) -> HitResult:
    """Official hit: every must_include phrase is a case-folded substring."""
    haystack = retrieved_text.casefold()
    scores: list[PhraseScore] = []
    missing: list[str] = []
    for phrase in must_include:
        found = phrase.casefold() in haystack
        scores.append(PhraseScore(phrase=phrase, hit=found))
        if not found:
            missing.append(phrase)
    return HitResult(
        hit=not missing,
        phrase_scores=tuple(scores),
        missing_phrases=tuple(missing),
    )


def parse_source_hint(source_hint: str) -> SourceHintInfo:
    parts = [p.strip() for p in source_hint.split("|") if p.strip()]
    paths: list[str] = []
    episode_token: str | None = None
    doc_id: str | None = None
    for part in parts:
        if part.startswith("http://") or part.startswith("https://"):
            continue
        if "/" in part and not part.startswith("podcast ") and not part.startswith("substack "):
            paths.append(part)
            name = Path(part).name
            ep = _EPISODE_IN_FILENAME.match(name)
            if ep and episode_token is None:
                episode_token = ep.group(1)
            stem = Path(part).stem
            if part.startswith("substack_articles/") or part.startswith("article_summaries/"):
                if stem.endswith("_summary"):
                    stem = stem[: -len("_summary")]
                doc_id = stem
    if episode_token is None:
        head = parts[0] if parts else source_hint
        m = re.search(r"\b(S2E\d+|S E\d+)\b", head)
        if m:
            episode_token = m.group(1)
    return SourceHintInfo(
        raw=source_hint,
        artifact_paths=tuple(paths),
        episode_token=episode_token,
        doc_id=doc_id,
    )


def is_unique_episode_id(episode_id: str | None) -> bool:
    if not episode_id:
        return False
    return bool(_UNIQUE_EPISODE_ID.fullmatch(episode_id.replace(" ", "")))


def hinted_artifact_text(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    if path.suffix.lower() == ".md":
        return path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        chunks: list[str] = []
        if isinstance(data, dict):
            title = data.get("title")
            if title:
                chunks.append(str(title))
            for seg in data.get("segments") or []:
                if not isinstance(seg, dict):
                    continue
                chunks.append(str(seg.get("topic") or ""))
                chunks.append(str(seg.get("summary") or ""))
                chunks.append(str(seg.get("content") or ""))
            generated = data.get("generated_content")
            if isinstance(generated, dict):
                chunks.append(json.dumps(generated, ensure_ascii=False))
        else:
            chunks.append(json.dumps(data, ensure_ascii=False))
        return "\n".join(chunks)
    return path.read_text(encoding="utf-8", errors="replace")


def phrases_present_in_text(*, must_include: Sequence[str], text: str) -> bool:
    haystack = text.casefold()
    return all(phrase.casefold() in haystack for phrase in must_include)


def classify_miss(*, disk_present: bool, indexed: bool, phrases_in_corpus: bool = False) -> str:
    """Split misses into 'not in corpus' vs 'retriever failed'.

    - not_in_corpus: hinted source artifact missing on disk, or present on disk
      but not indexed (retriever cannot see it).
    - retriever_failed: hinted source is indexed, or the gold phrases exist in
      the retrievable corpus, but top-k retrieval did not surface must_include.

    ``disk_present`` is recorded for the report; indexing (or phrases already
    in the retrievable corpus) is what distinguishes a retriever miss.
    """
    del disk_present  # documented on the report; indexing decides the bucket
    if indexed or phrases_in_corpus:
        return MISS_RETRIEVER_FAILED
    return MISS_NOT_IN_CORPUS


def slice_name(question_id: str) -> str:
    if question_id.startswith("2026-"):
        return "2026_podcast"
    if question_id.startswith("substack-"):
        return "substack"
    return "earlier_podcast"


def retrieved_meta_ids(metadatas: Sequence[Mapping[str, Any]]) -> tuple[list[str], list[str]]:
    episode_ids: list[str] = []
    doc_ids: list[str] = []
    for meta in metadatas:
        eid = meta.get("episode_id")
        if eid:
            episode_ids.append(str(eid))
        did = meta.get("doc_id")
        if did:
            doc_ids.append(str(did))
    return episode_ids, doc_ids


def hinted_source_retrieved(
    *,
    hint: SourceHintInfo,
    hinted_title: str | None,
    metadatas: Sequence[Mapping[str, Any]],
) -> bool:
    titles = {str(m.get("title") or "") for m in metadatas if m.get("title")}
    episode_ids = {str(m.get("episode_id") or "") for m in metadatas if m.get("episode_id")}
    doc_ids = {str(m.get("doc_id") or "") for m in metadatas if m.get("doc_id")}
    if hint.doc_id and hint.doc_id in doc_ids:
        return True
    if hinted_title and hinted_title in titles:
        return True
    if hint.episode_token and is_unique_episode_id(hint.episode_token) and hint.episode_token in episode_ids:
        return True
    return False
