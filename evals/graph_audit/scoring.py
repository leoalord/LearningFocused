"""Pure helpers for the graph audit: evidence prep, verdict parsing, scoring.

No Neo4j or network access here, so everything is unit-testable.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

GROUNDING = ("direct", "resolved_reference", "ambiguous", "absent")
TYPE_JUDGEMENT = ("correct", "incorrect", "unclear")
IDENTITY = ("supported", "ambiguous", "unsupported", "not_assessed")
VERDICTS = ("supported", "needs_qualification", "unsupported", "insufficient_evidence")

# Failure categories, in precedence order for picking a single primary cause.
CAT_HALLUCINATED = "hallucinated_endpoint"  # an endpoint is absent from the evidence
CAT_MISSING_EVIDENCE = "missing_evidence"  # insufficient_evidence verdict
CAT_WRONG_TYPE = "wrong_entity_type"  # subject or object type judged incorrect
CAT_BAD_IDENTITY = "bad_identity_link"  # linked to the wrong graph entity
CAT_AMBIGUOUS = "ambiguous_endpoint"
CAT_WRONG_PREDICATE = "wrong_predicate"  # endpoints fine, relationship not supported
CAT_LOST_CONDITION = "lost_condition"  # needs_qualification
CAT_CLEAN = "clean"
CATEGORY_ORDER = (
    CAT_HALLUCINATED,
    CAT_MISSING_EVIDENCE,
    CAT_WRONG_TYPE,
    CAT_BAD_IDENTITY,
    CAT_AMBIGUOUS,
    CAT_WRONG_PREDICATE,
    CAT_LOST_CONDITION,
    CAT_CLEAN,
)

DEFAULT_TYPE_DEFINITIONS: dict[str, str] = {
    "Person": "A specific, named human individual.",
    "Organization": "A named company, school, nonprofit, agency, or other institution.",
    "Concept": "An idea, method, theory, practice, or abstract topic.",
    "Tool": "A specific named product, software, app, device, or curriculum resource.",
    "Event": "A specific occurrence bounded in time (conference, launch, study, episode).",
}

_PREAMBLE_HEAD = "You are extracting a knowledge graph"


def strip_extraction_preamble(text: str) -> str:
    """Remove the extraction instructions the pipeline prepends to Document text.

    `index_neo4j._wrap_for_graph_extraction` writes: spec ("...Rules:\\n...\\n"),
    a blank line, an optional "Provenance:" block ending in a blank line, then
    the real content. Document nodes store that whole string as `text`.
    """
    if not text.startswith(_PREAMBLE_HEAD):
        return text
    rules = text.find("Rules:\n")
    if rules == -1:
        return text
    end = text.find("\n\n", rules)
    if end == -1:
        return text
    rest = text[end + 2 :]
    if rest.startswith("Provenance:\n"):
        pend = rest.find("\n\n")
        rest = rest[pend + 2 :] if pend != -1 else ""
    return rest


def excerpt_around(text: str, names: Iterable[str], max_chars: int) -> tuple[str, bool]:
    """Return (excerpt, truncated). Centers on the first spot both names appear.

    If the text fits, it is returned whole. Otherwise picks the window that
    contains the most of `names` (case-insensitive), preferring windows near the
    first mention of the rarer name.
    """
    if len(text) <= max_chars:
        return text, False
    low = text.casefold()
    positions: list[list[int]] = []
    for name in names:
        n = name.casefold().strip()
        if not n:
            continue
        positions.append([m.start() for m in re.finditer(re.escape(n), low)])
    anchors = sorted({p for ps in positions for p in ps})
    best_start = 0
    best_score = -1
    for a in anchors or [0]:
        start = max(0, min(a - max_chars // 2, len(text) - max_chars))
        end = start + max_chars
        score = sum(1 for ps in positions if any(start <= p < end for p in ps))
        if score > best_score:
            best_score, best_start = score, start
    return text[best_start : best_start + max_chars], True


def build_audit_input(record: Mapping[str, Any], type_definitions: Mapping[str, str]) -> str:
    """User message for the auditor: claim, vocabulary, and evidence as JSON."""
    vocab = {t: type_definitions.get(t, "(no definition provided)") for t in type_definitions}
    for t in list(record.get("subject_types", [])) + list(record.get("object_types", [])):
        vocab.setdefault(t, "(not in configured vocabulary)")
    payload = {
        "claim": {
            "subject": record["subject"],
            "subject_types": record.get("subject_types", []),
            "predicate": record["predicate"],
            "object": record["object"],
            "object_types": record.get("object_types", []),
        },
        "entity_type_vocabulary": vocab,
        "provenance": record.get("provenance"),
        "evidence": record.get("evidence", []),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def parse_auditor_json(text: str) -> dict[str, Any]:
    """Extract and validate the auditor's JSON object. Raises ValueError if unusable."""
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        raise ValueError("no JSON object in auditor output")
    data = json.loads(m.group(0))
    checks = {
        "subject_grounding": GROUNDING,
        "object_grounding": GROUNDING,
        "subject_type": TYPE_JUDGEMENT,
        "object_type": TYPE_JUDGEMENT,
        "identity_link": IDENTITY,
        "verdict": VERDICTS,
    }
    for key, allowed in checks.items():
        val = str(data.get(key, "")).strip().lower()
        if val not in allowed:
            raise ValueError(f"{key}={data.get(key)!r} not in {allowed}")
        data[key] = val
    return data


def failure_categories(a: Mapping[str, Any]) -> list[str]:
    """All failure categories an auditor (or human) judgement falls into."""
    cats: list[str] = []
    grounding = (a.get("subject_grounding"), a.get("object_grounding"))
    verdict = a.get("verdict")
    if "absent" in grounding:
        cats.append(CAT_HALLUCINATED)
    if verdict == "insufficient_evidence":
        cats.append(CAT_MISSING_EVIDENCE)
    if "incorrect" in (a.get("subject_type"), a.get("object_type")):
        cats.append(CAT_WRONG_TYPE)
    if a.get("identity_link") == "unsupported":
        cats.append(CAT_BAD_IDENTITY)
    if "ambiguous" in grounding:
        cats.append(CAT_AMBIGUOUS)
    if verdict == "unsupported" and not ({"absent", "ambiguous"} & set(grounding)):
        cats.append(CAT_WRONG_PREDICATE)
    if verdict == "needs_qualification":
        cats.append(CAT_LOST_CONDITION)
    return cats or [CAT_CLEAN]


def primary_category(a: Mapping[str, Any]) -> str:
    cats = set(failure_categories(a))
    return next(c for c in CATEGORY_ORDER if c in cats)


def is_accepted(a: Mapping[str, Any]) -> bool:
    """A claim is accepted only if supported as written with no wrong type."""
    return a.get("verdict") == "supported" and "incorrect" not in (
        a.get("subject_type"),
        a.get("object_type"),
    )


@dataclass
class Rate:
    num: int
    den: int

    @property
    def value(self) -> float | None:
        return self.num / self.den if self.den else None

    def to_dict(self) -> dict[str, Any]:
        return {"num": self.num, "den": self.den, "rate": self.value}


def _breakdown(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        val = r.get(key)
        if isinstance(val, list):
            val = "|".join(sorted(val)) or "(none)"
        groups[str(val)].append(r)
    out: dict[str, dict[str, Any]] = {}
    for g, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        out[g] = {
            "n": len(rs),
            "accepted": Rate(sum(is_accepted(r["auditor"]) for r in rs), len(rs)).to_dict(),
            "primary_category": dict(Counter(primary_category(r["auditor"]) for r in rs)),
        }
    return out


def score_records(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate auditor results, and compare against human labels when present."""
    audited = [r for r in records if isinstance(r.get("auditor"), Mapping) and not r["auditor"].get("error")]
    errors = [r for r in records if isinstance(r.get("auditor"), Mapping) and r["auditor"].get("error")]
    report: dict[str, Any] = {
        "n_records": len(records),
        "n_audited": len(audited),
        "n_audit_errors": len(errors),
        "n_unaudited": len(records) - len(audited) - len(errors),
    }
    if audited:
        multi: Counter[str] = Counter()
        for r in audited:
            multi.update(failure_categories(r["auditor"]))
        report["auditor"] = {
            "accepted": Rate(sum(is_accepted(r["auditor"]) for r in audited), len(audited)).to_dict(),
            "verdicts": dict(Counter(r["auditor"]["verdict"] for r in audited)),
            "primary_category": dict(Counter(primary_category(r["auditor"]) for r in audited)),
            "any_category": dict(multi),
            "entity_type_conflict_in_graph": Rate(
                sum(bool(r.get("type_conflict")) for r in audited), len(audited)
            ).to_dict(),
            "by_predicate": _breakdown(audited, "predicate"),
            "by_provenance": _breakdown(audited, "provenance"),
            "by_subject_types": _breakdown(audited, "subject_types"),
            "by_extract_model": _breakdown(
                [{**r, "extract_model": (r.get("relationship_properties") or {}).get("extract_model")} for r in audited],
                "extract_model",
            ),
        }

    labeled = [r for r in audited if isinstance(r.get("human"), Mapping) and r["human"].get("verdict")]
    if labeled:
        tp = fp = fn = tn = 0
        verdict_matrix: dict[str, Counter[str]] = defaultdict(Counter)
        field_agree: dict[str, Rate] = {}
        for r in labeled:
            h, a = r["human"], r["auditor"]
            good, acc = is_accepted(h), is_accepted(a)
            tp += good and acc
            fp += (not good) and acc
            fn += good and not acc
            tn += (not good) and not acc
            verdict_matrix[h["verdict"]][a["verdict"]] += 1
        for fld in ("verdict", "subject_type", "object_type"):
            pairs = [(r["human"].get(fld), r["auditor"].get(fld)) for r in labeled if r["human"].get(fld)]
            field_agree[fld] = Rate(sum(h == a for h, a in pairs), len(pairs))
        report["human_vs_auditor"] = {
            "n_labeled": len(labeled),
            # Of claims the human judged good, how many does the extractor+graph get right? (graph precision)
            "graph_precision_human": Rate(tp + fn, len(labeled)).to_dict(),
            # Of claims the auditor accepted, how many are actually bad? (dirty data getting through)
            "dirty_pass_rate": Rate(fp, tp + fp).to_dict(),
            # Of claims the human judged good, how many did the auditor reject? (auditor too strict)
            "false_reject_rate": Rate(fn, tp + fn).to_dict(),
            "confusion": {"accept_good": tp, "accept_bad": fp, "reject_good": fn, "reject_bad": tn},
            "verdict_matrix_human_rows_auditor_cols": {k: dict(v) for k, v in verdict_matrix.items()},
            "field_agreement": {k: v.to_dict() for k, v in field_agree.items()},
            "human_primary_category": dict(Counter(primary_category(r["human"]) for r in labeled)),
        }
    return report


def format_report(report: Mapping[str, Any]) -> str:
    """Human-readable summary of `score_records` output."""

    def pct(rate: Mapping[str, Any]) -> str:
        v = rate.get("rate")
        return f"{rate['num']}/{rate['den']} ({v:.0%})" if v is not None else "n/a"

    lines = [
        f"Records: {report['n_records']}  audited: {report['n_audited']}  "
        f"errors: {report['n_audit_errors']}  unaudited: {report['n_unaudited']}"
    ]
    a = report.get("auditor")
    if a:
        lines.append(f"Auditor accepted: {pct(a['accepted'])}")
        lines.append(f"Verdicts: {a['verdicts']}")
        lines.append("Primary failure category:")
        for cat in CATEGORY_ORDER:
            if cat in a["primary_category"]:
                lines.append(f"  {cat:24s} {a['primary_category'][cat]}")
        lines.append(f"Entities carrying more than one type label: {pct(a['entity_type_conflict_in_graph'])}")
        lines.append("By predicate (n, accepted):")
        for k, v in list(a["by_predicate"].items())[:12]:
            lines.append(f"  {k:24s} n={v['n']:<4d} {pct(v['accepted'])}")
        lines.append("By provenance:")
        for k, v in a["by_provenance"].items():
            lines.append(f"  {k:24s} n={v['n']:<4d} {pct(v['accepted'])}")
    h = report.get("human_vs_auditor")
    if h:
        lines.append(f"\nHuman labels: {h['n_labeled']}")
        lines.append(f"  Graph precision (human says good): {pct(h['graph_precision_human'])}")
        lines.append(f"  Dirty pass rate (auditor accepted, human says bad): {pct(h['dirty_pass_rate'])}")
        lines.append(f"  False reject rate (human says good, auditor rejected): {pct(h['false_reject_rate'])}")
        lines.append(f"  Confusion: {h['confusion']}")
        lines.append(f"  Field agreement: { {k: pct(v) for k, v in h['field_agreement'].items()} }")
        lines.append(f"  Human primary category: {h['human_primary_category']}")
    else:
        lines.append("\nNo human labels yet: run `label` to measure the auditor itself.")
    return "\n".join(lines)
