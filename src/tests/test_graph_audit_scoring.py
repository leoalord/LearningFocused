"""Graph audit helpers: preamble stripping, excerpting, parsing, scoring."""

from __future__ import annotations

import json

import pytest
from langchain_core.documents import Document

from evals.graph_audit.scoring import (
    CAT_CLEAN,
    CAT_HALLUCINATED,
    CAT_LOST_CONDITION,
    CAT_WRONG_PREDICATE,
    CAT_WRONG_TYPE,
    build_audit_input,
    excerpt_around,
    failure_categories,
    format_report,
    is_accepted,
    parse_auditor_json,
    primary_category,
    score_records,
    strip_extraction_preamble,
)
from src.pipeline.index_neo4j import _wrap_for_graph_extraction, build_graph_extraction_spec


def _j(verdict: str = "supported", s_type: str = "correct", o_type: str = "correct",
       sg: str = "direct", og: str = "direct", identity: str = "not_assessed") -> dict[str, str]:
    return {"verdict": verdict, "subject_type": s_type, "object_type": o_type,
            "subject_grounding": sg, "object_grounding": og, "identity_link": identity}


@pytest.mark.parametrize("meta", [{"title": "Ep 1", "topic": "AI tutors"}, {}])
def test_strip_preamble_round_trips_pipeline_wrapper(meta: dict[str, str]) -> None:
    spec = build_graph_extraction_spec(allowed_nodes=["Person"], allowed_relationships=["FOUNDED"])
    body = "Episode: Ep 1\n\nMacKenzie founded Alpha.\n\nSecond paragraph."
    wrapped = _wrap_for_graph_extraction(Document(page_content=body, metadata=meta), graph_extraction_spec=spec)
    assert strip_extraction_preamble(wrapped.page_content) == body


def test_strip_preamble_leaves_plain_text() -> None:
    assert strip_extraction_preamble("just text") == "just text"


def test_excerpt_prefers_window_with_both_names() -> None:
    text = "Alpha " + "x" * 500 + " Alpha and Price together " + "y" * 500
    out, truncated = excerpt_around(text, ["Alpha", "Price"], 100)
    assert truncated and "Alpha" in out and "Price" in out


def test_excerpt_short_text_untouched() -> None:
    assert excerpt_around("short", ["a"], 100) == ("short", False)


def test_parse_auditor_json_validates_and_normalizes() -> None:
    raw = "Here you go:\n" + json.dumps({**_j(verdict="Supported"), "reason": "ok", "evidence_ids": []})
    assert parse_auditor_json(raw)["verdict"] == "supported"
    with pytest.raises(ValueError):
        parse_auditor_json(json.dumps(_j(verdict="maybe")))
    with pytest.raises(ValueError):
        parse_auditor_json("no json")


def test_failure_categories() -> None:
    assert failure_categories(_j()) == [CAT_CLEAN]
    assert CAT_WRONG_TYPE in failure_categories(_j(s_type="incorrect"))
    assert failure_categories(_j(verdict="unsupported")) == [CAT_WRONG_PREDICATE]
    # Absent endpoint is hallucination, not a predicate problem.
    assert failure_categories(_j(verdict="unsupported", og="absent")) == [CAT_HALLUCINATED]
    assert primary_category(_j(verdict="needs_qualification", o_type="incorrect")) == CAT_WRONG_TYPE
    assert primary_category(_j(verdict="needs_qualification")) == CAT_LOST_CONDITION


def test_accepted_requires_supported_and_no_wrong_type() -> None:
    assert is_accepted(_j())
    assert not is_accepted(_j(o_type="incorrect"))
    assert is_accepted(_j(o_type="unclear"))
    assert not is_accepted(_j(verdict="needs_qualification"))


def test_build_audit_input_flags_off_vocab_types() -> None:
    rec = {"subject": "A", "subject_types": ["Person", "Organization"], "predicate": "FOUNDED",
           "object": "B", "object_types": ["Place"], "provenance": "edge", "evidence": []}
    payload = json.loads(build_audit_input(rec, {"Person": "human"}))
    assert payload["entity_type_vocabulary"]["Place"] == "(not in configured vocabulary)"
    assert payload["claim"]["subject_types"] == ["Person", "Organization"]


def test_score_records_separates_dirty_pass_from_false_reject() -> None:
    def rec(pred: str, auditor: dict, human: dict | None) -> dict:
        return {"predicate": pred, "provenance": "edge", "subject_types": ["Person"], "type_conflict": False,
                "relationship_properties": {"extract_model": "m"}, "auditor": auditor, "human": human}

    records = [
        rec("FOUNDED", _j(), _j()),  # accept, good
        rec("FOUNDED", _j(), _j(s_type="incorrect")),  # accept, bad -> dirty pass
        rec("DISCUSSED", _j(verdict="unsupported"), _j()),  # reject, good -> false reject
        rec("DISCUSSED", _j(verdict="unsupported"), _j(verdict="unsupported")),  # reject, bad
        rec("DISCUSSED", _j(verdict="needs_qualification"), None),  # unlabeled
        rec("FOUNDED", {"error": "boom"}, None),
        {"predicate": "X", "auditor": None},
    ]
    rep = score_records(records)
    assert (rep["n_audited"], rep["n_audit_errors"], rep["n_unaudited"]) == (5, 1, 1)
    assert rep["auditor"]["accepted"]["num"] == 2
    assert rep["auditor"]["by_predicate"]["DISCUSSED"]["n"] == 3
    h = rep["human_vs_auditor"]
    assert h["confusion"] == {"accept_good": 1, "accept_bad": 1, "reject_good": 1, "reject_bad": 1}
    assert h["dirty_pass_rate"]["rate"] == 0.5
    assert h["false_reject_rate"]["rate"] == 0.5
    assert "Dirty pass rate" in format_report(rep)
