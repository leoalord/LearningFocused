"""Read-only retrieval eval against the on-disk Chroma store.

Usage:
    uv run python -m evals.run
    uv run python -m evals.run --report evals/last_report.json

Does not reset, add, or delete Chroma documents. Queries the same helpers
the FastMCP search/fetch tools wrap (query_segments + query_summaries)
in-process — no HTTP MCP client.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.scoring import (
    CorpusStatus,
    GoldQuestion,
    SourceHintInfo,
    classify_miss,
    concatenate_retrieved_text,
    hinted_artifact_text,
    hinted_source_retrieved,
    is_well_formed_episode_id,
    load_gold_questions,
    parse_source_hint,
    phrases_present_in_text,
    retrieved_meta_ids,
    score_hit,
    slice_name,
)
from src.config import CHROMA_DIR, PROJECT_ROOT
from src.database.chroma_manager import COLLECTION_NAME
from src.mcp_server.retrieval import retrieve_documents

DEFAULT_GOLD = PROJECT_ROOT / "evals" / "gold_questions.json"
DEFAULT_REPORT = PROJECT_ROOT / "evals" / "last_report.json"
DEFAULT_MAX_SEGMENTS = 5
DEFAULT_MAX_SUMMARIES = 3


def _fail(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def _assert_chroma_readable() -> None:
    sqlite = CHROMA_DIR / "chroma.sqlite3"
    if not CHROMA_DIR.exists() or not sqlite.exists():
        _fail(
            f"Chroma store not found at {CHROMA_DIR}. "
            "This eval is read-only and will not create or reset the collection."
        )


def _connect_collection():
    import chromadb

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    try:
        return client.get_collection(name=COLLECTION_NAME)
    except Exception as exc:  # pragma: no cover - environment-dependent
        _fail(f"Could not open Chroma collection {COLLECTION_NAME!r}: {exc}")


def _first_where(collection, where: dict[str, Any]) -> bool:
    try:
        res = collection.get(where=where, limit=1, include=["metadatas"])
    except Exception as exc:
        print(f"warning: metadata probe failed for {where}: {exc}", file=sys.stderr)
        return False
    return bool(res.get("ids"))


def _load_hinted_json(path: Path) -> dict[str, Any] | None:
    if path.suffix.lower() != ".json" or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def inspect_corpus(
    *,
    question: GoldQuestion,
    hint: SourceHintInfo,
    retrieved_metadatas: list[dict[str, Any]],
    collection,
) -> CorpusStatus:
    status = CorpusStatus()
    status.hinted_doc_id = hint.doc_id
    status.hinted_episode_id = hint.episode_token

    artifact_text_parts: list[str] = []
    for rel in hint.artifact_paths:
        path = PROJECT_ROOT / rel
        exists = path.is_file()
        status.artifact_exists[rel] = exists
        if not exists:
            continue
        artifact_text_parts.append(hinted_artifact_text(path))
        payload = _load_hinted_json(path)
        if payload:
            title = payload.get("title")
            if title:
                status.hinted_title = str(title)
            eid = payload.get("episode_id")
            if eid and is_well_formed_episode_id(str(eid)):
                status.hinted_episode_id = str(eid)
            doc_id = payload.get("doc_id")
            if doc_id:
                status.hinted_doc_id = str(doc_id)

    status.disk_present = any(status.artifact_exists.values()) if status.artifact_exists else False
    combined_hinted = "\n".join(artifact_text_parts)
    status.phrases_in_hinted_source = bool(combined_hinted) and phrases_present_in_text(
        must_include=question.must_include,
        text=combined_hinted,
    )

    indexed = False
    if status.hinted_doc_id:
        indexed = indexed or _first_where(collection, {"doc_id": status.hinted_doc_id})
    if status.hinted_title:
        indexed = indexed or _first_where(collection, {"title": status.hinted_title})
    if status.hinted_episode_id and is_well_formed_episode_id(status.hinted_episode_id):
        indexed = indexed or _first_where(collection, {"episode_id": status.hinted_episode_id})
    status.indexed = indexed

    status.retrieved_episode_ids, status.retrieved_doc_ids = retrieved_meta_ids(retrieved_metadatas)
    status.hinted_source_in_results = hinted_source_retrieved(
        hint=hint,
        hinted_title=status.hinted_title,
        metadatas=retrieved_metadatas,
    )
    return status


def retrieve(question: str, *, max_segments: int, max_summaries: int):
    # Same retrieve_documents() the FastMCP search tool wraps (in-process).
    return retrieve_documents(
        question,
        max_segments=max_segments,
        max_summaries=max_summaries,
    )


def evaluate(
    questions: list[GoldQuestion],
    *,
    max_segments: int,
    max_summaries: int,
) -> dict[str, Any]:
    _assert_chroma_readable()
    collection = _connect_collection()

    per_question: list[dict[str, Any]] = []
    hits = 0
    missed_ids: list[str] = []
    miss_split = {"not_in_corpus": 0, "retriever_failed": 0}
    slices: dict[str, dict[str, Any]] = {}

    for q in questions:
        try:
            summaries, segments = retrieve(
                q.question,
                max_segments=max_segments,
                max_summaries=max_summaries,
            )
        except Exception as exc:
            missed_ids.append(q.id)
            miss_split["retriever_failed"] = miss_split.get("retriever_failed", 0) + 1
            sl = slice_name(q.id)
            bucket = slices.setdefault(sl, {"n": 0, "hits": 0, "missed_ids": []})
            bucket["n"] += 1
            bucket["missed_ids"].append(q.id)
            per_question.append(
                {
                    "id": q.id,
                    "question": q.question,
                    "hit": False,
                    "error": str(exc),
                    "missing_phrases": list(q.must_include),
                    "phrase_hits": {p: False for p in q.must_include},
                    "miss_type": "retriever_failed",
                    "retrieved_count": {"summaries": 0, "segments": 0},
                    "retrieved_titles": [],
                    "corpus": {
                        "artifact_exists": {},
                        "disk_present": False,
                        "indexed": False,
                        "phrases_in_hinted_source": False,
                        "hinted_episode_id": None,
                        "hinted_title": None,
                        "hinted_doc_id": None,
                        "retrieved_episode_ids": [],
                        "retrieved_doc_ids": [],
                        "hinted_source_in_results": False,
                    },
                }
            )
            continue
        docs = list(summaries) + list(segments)
        page_contents = [d.page_content or "" for d in docs]
        retrieved_text = concatenate_retrieved_text(page_contents)
        hit_result = score_hit(must_include=q.must_include, retrieved_text=retrieved_text)
        metadatas = [dict(d.metadata or {}) for d in docs]
        hint = parse_source_hint(q.source_hint)
        corpus = inspect_corpus(
            question=q,
            hint=hint,
            retrieved_metadatas=metadatas,
            collection=collection,
        )

        miss_type = None
        if not hit_result.hit:
            miss_type = classify_miss(
                disk_present=corpus.disk_present,
                indexed=corpus.indexed,
                phrases_in_corpus=corpus.phrases_in_hinted_source,
            )
            missed_ids.append(q.id)
            miss_split[miss_type] = miss_split.get(miss_type, 0) + 1
        else:
            hits += 1

        sl = slice_name(q.id)
        bucket = slices.setdefault(sl, {"n": 0, "hits": 0, "missed_ids": []})
        bucket["n"] += 1
        if hit_result.hit:
            bucket["hits"] += 1
        else:
            bucket["missed_ids"].append(q.id)

        per_question.append(
            {
                "id": q.id,
                "question": q.question,
                "hit": hit_result.hit,
                "missing_phrases": list(hit_result.missing_phrases),
                "phrase_hits": {ps.phrase: ps.hit for ps in hit_result.phrase_scores},
                "miss_type": miss_type,
                "retrieved_count": {
                    "summaries": len(summaries),
                    "segments": len(segments),
                },
                "retrieved_titles": [str((d.metadata or {}).get("title") or (d.metadata or {}).get("group_title") or "") for d in docs],
                "corpus": {
                    "artifact_exists": corpus.artifact_exists,
                    "disk_present": corpus.disk_present,
                    "indexed": corpus.indexed,
                    "phrases_in_hinted_source": corpus.phrases_in_hinted_source,
                    "hinted_episode_id": corpus.hinted_episode_id,
                    "hinted_title": corpus.hinted_title,
                    "hinted_doc_id": corpus.hinted_doc_id,
                    "retrieved_episode_ids": corpus.retrieved_episode_ids,
                    "retrieved_doc_ids": corpus.retrieved_doc_ids,
                    "hinted_source_in_results": corpus.hinted_source_in_results,
                },
            }
        )

    n = len(questions)
    for bucket in slices.values():
        bucket["hit_rate"] = (bucket["hits"] / bucket["n"]) if bucket["n"] else 0.0

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "command": "uv run python -m evals.run",
        "retriever": {
            "helpers": [
                "src.mcp_server.retrieval.retrieve_documents",
                "query_summaries",
                "query_segments",
            ],
            "max_segments": max_segments,
            "max_summaries": max_summaries,
            "hit_definition": (
                "casefold; every must_include substring in concatenated retrieved page_content; "
                "source_hint not used for scoring"
            ),
        },
        "n": n,
        "hits": hits,
        "hit_rate": (hits / n) if n else 0.0,
        "missed_ids": missed_ids,
        "miss_split": miss_split,
        "slices": slices,
        "questions": per_question,
    }


def format_summary(report: dict[str, Any]) -> str:
    n = report["n"]
    hits = report["hits"]
    rate = report["hit_rate"]
    missed = report["missed_ids"]
    split = report["miss_split"]
    lines = [
        f"N={n}  hits={hits}  hit_rate={rate:.3f} ({hits}/{n})",
        f"Missed ids: {missed if missed else '(none)'}",
        f"Miss split: not_in_corpus={split.get('not_in_corpus', 0)}  "
        f"retriever_failed={split.get('retriever_failed', 0)}",
    ]
    failed = [q for q in report["questions"] if not q["hit"]]
    if failed:
        lines.append("Failed questions:")
        for q in failed:
            missing = ", ".join(q["missing_phrases"]) or "(none)"
            lines.append(
                f"  - {q['id']}  miss_type={q['miss_type']}  "
                f"missing=[{missing}]  indexed={q['corpus']['indexed']}  "
                f"disk={q['corpus']['disk_present']}"
            )
    else:
        lines.append("Failed questions: (none)")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run retrieval eval against Chroma (read-only).")
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD, help="Path to gold_questions.json")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="JSON report output path")
    parser.add_argument("--max-segments", type=int, default=DEFAULT_MAX_SEGMENTS)
    parser.add_argument("--max-summaries", type=int, default=DEFAULT_MAX_SUMMARIES)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    gold_path = args.gold if args.gold.is_absolute() else PROJECT_ROOT / args.gold
    report_path = args.report if args.report.is_absolute() else PROJECT_ROOT / args.report
    if not gold_path.is_file():
        _fail(f"Gold set not found: {gold_path}")
    if report_path.resolve() == gold_path.resolve():
        _fail("--report must not point at the gold set")
    questions = load_gold_questions(gold_path)
    report = evaluate(
        questions,
        max_segments=args.max_segments,
        max_summaries=args.max_summaries,
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(format_summary(report))
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
