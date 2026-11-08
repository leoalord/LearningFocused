"""Graph extraction audit: sample edges, audit them against source text, score.

Usage:
    uv run python -m evals.graph_audit health
    uv run python -m evals.graph_audit drops
    uv run python -m evals.graph_audit sample --n 100 --out evals/graph_audit/runs/sample.jsonl
    uv run python -m evals.graph_audit audit  --in evals/graph_audit/runs/sample.jsonl
    uv run python -m evals.graph_audit label  --in evals/graph_audit/runs/sample.jsonl
    uv run python -m evals.graph_audit score  --in evals/graph_audit/runs/sample.jsonl

`health` and `sample` read the served graph in Postgres AGE, and `drops` the
`ingested_docs` extraction registry next to it (all read-only, `PG_DSN`).
`audit` calls a hosted LLM (billed).
`label` and `score` are local only. Each command rewrites the JSONL in place, so
auditor results and human labels accumulate on the same records.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from evals.graph_audit.scoring import (
    DEFAULT_TYPE_DEFINITIONS,
    build_audit_input,
    format_report,
    parse_auditor_json,
    score_records,
)

HERE = Path(__file__).resolve().parent
PROMPT_PATH = HERE / "auditor_prompt.md"
DEFAULT_OUT = HERE / "runs" / "sample.jsonl"


# ---------- io ----------

def _load(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _save(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def _type_definitions(path: str | None) -> dict[str, str]:
    if path:
        return dict(json.loads(Path(path).read_text(encoding="utf-8")))
    env = os.getenv("NEO4J_ALLOWED_NODES")
    if env:
        names = [t.strip() for t in env.split(",") if t.strip()]
        return {t: DEFAULT_TYPE_DEFINITIONS.get(t, "(no definition provided)") for t in names}
    return dict(DEFAULT_TYPE_DEFINITIONS)


# ---------- health ----------

def cmd_health(args: argparse.Namespace) -> int:
    """Shape of the served graph (Postgres AGE). Works on graphs built before provenance."""
    from evals.graph_audit.age import graph_shape
    from src.database.pg_search import read_only

    with read_only(args.dsn) as conn:
        shape = graph_shape(conn, top=args.top)
    t, c = shape["entities"], shape["conflicted"]
    if not t:
        print("The AGE graph has no entities.")
        return 0
    print("=== Served graph (Postgres AGE) ===")
    print(f"Entities: {t}; carrying more than one type label: {c} ({c / t:.1%})")
    print("Type labels: " + ", ".join(f"{k}x{v}" for k, v in shape["type_label_counts"].items()))
    for e in shape["examples"]:
        print(f"  {e['name']}: {e['types']} (degree {e['degree']})")
    total = sum(r["count"] for r in shape["relationships"])
    print(f"Entity relationships: {total}")
    for r in shape["relationships"]:
        share = r["count"] / total if total else 0
        print(f"  {r['type']:20s} {r['count']:>7d} ({share:.0%})  with source_doc_id: {r['with_provenance']}")
    print("\nExtraction drop counts live in the ingest registry; see `drops`.")
    return 0


# ---------- drops ----------

def drop_report(conn: Any, top: int = 10) -> dict[str, Any]:
    """Schema-filter drop totals from `ingested_docs` (src/pipeline/index_graph.py)."""
    row = conn.execute(
        """
        SELECT count(*), coalesce(sum(raw_nodes), 0), coalesce(sum(kept_nodes), 0), coalesce(sum(raw_rels), 0),
               coalesce(sum(kept_rels), 0), coalesce(sum(rels_dropped_for_endpoint), 0)
        FROM ingested_docs WHERE raw_nodes IS NOT NULL
        """
    ).fetchone()
    keys = ("docs", "raw_nodes", "kept_nodes", "raw_rels", "kept_rels", "endpoint_drops")
    report: dict[str, Any] = dict(zip(keys, (int(v) for v in row)))
    for column in ("dropped_node_types", "dropped_rel_types"):
        report[column] = [
            (str(t), int(n)) for t, n in conn.execute(
                f"SELECT e.key, sum(e.value::int) AS n FROM ingested_docs d, jsonb_each_text(d.{column}) e "  # noqa: S608
                "GROUP BY e.key ORDER BY n DESC, e.key LIMIT %s",
                (top,),
            ).fetchall()
        ]
    report["by_model"] = [
        {"model": m, "docs": int(d), "raw": int(r), "kept": int(k)}
        for m, d, r, k in conn.execute(
            "SELECT extract_model, count(*), coalesce(sum(raw_rels), 0), coalesce(sum(kept_rels), 0) "
            "FROM ingested_docs WHERE raw_rels IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1"
        ).fetchall()
    ]
    return report


def cmd_drops(args: argparse.Namespace) -> int:
    """Schema-filter drop accounting from the `ingested_docs` extraction registry (Postgres)."""
    from src.database.pg_search import read_only

    with read_only(args.dsn) as conn:
        r = drop_report(conn, top=args.top)
    if not r["docs"]:
        print("No documents with extraction stats yet. Run graph extraction (src.pipeline.index_graph) to collect them.")
        return 0

    def frac(a: int, b: int) -> str:
        return f"{a}/{b} ({a / b:.0%})" if b else "n/a"

    print(f"Documents with stats: {r['docs']}")
    print(f"Nodes kept:         {frac(r['kept_nodes'], r['raw_nodes'])}")
    print(f"Relationships kept: {frac(r['kept_rels'], r['raw_rels'])}")
    print(f"  dropped because an endpoint type was off-vocabulary: {r['endpoint_drops']}")
    for label, key in (("node", "dropped_node_types"), ("relationship", "dropped_rel_types")):
        print(f"Top off-vocabulary {label} types: " + (", ".join(f"{t}x{n}" for t, n in r[key]) or "none"))
    for m in r["by_model"]:
        print(f"  model={m['model']}: docs={m['docs']} rels kept {frac(m['kept'], m['raw'])}")
    return 0


# ---------- sample ----------

def cmd_sample(args: argparse.Namespace) -> int:
    from evals.graph_audit.age import sample_claims

    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"{out} exists; pass --force to overwrite (it may hold labels).", file=sys.stderr)
        return 2
    records, population = sample_claims(
        n=args.n, seed=args.seed, predicates=args.predicate,
        max_docs=args.max_docs, max_chars=args.max_chars, dsn=args.dsn,
    )
    if not records:
        print("No entity relationships found in the AGE graph.", file=sys.stderr)
        return 2
    _save(out, records)
    prov: dict[str, int] = {}
    for r in records:
        prov[r["provenance"]] = prov.get(r["provenance"], 0) + 1
    print(f"Sampled {len(records)} of {population} relationships (seed={args.seed}) -> {out}")
    print(f"Provenance: {prov}  (edge = exact source doc; co_mention = docs mentioning both, pre-provenance data)")
    return 0


# ---------- audit ----------

def cmd_audit(args: argparse.Namespace) -> int:
    from langchain_core.messages import HumanMessage, SystemMessage

    from src.llm.content import format_message_content
    from src.llm.factory import RetryConfig, create_chat_models, parse_model_list, retryable_invoke_models

    path = Path(args.inp)
    records = _load(path)
    defs = _type_definitions(args.type_defs)
    system = PROMPT_PATH.read_text(encoding="utf-8")
    model_names = parse_model_list(args.models or os.getenv("LF_GRAPH_AUDIT_MODELS"), default=["claude-opus-4-5"])
    models = create_chat_models(model_names, temperature=0.0, max_tokens=1500, timeout=120)
    retry = RetryConfig(max_attempts=3, initial_backoff_seconds=1.0, max_backoff_seconds=20.0, jitter_ratio=0.2)

    todo = [r for r in records if args.redo or not r.get("auditor") or r["auditor"].get("error")]
    if args.limit:
        todo = todo[: args.limit]
    print(f"Auditing {len(todo)} of {len(records)} records with {model_names[0]} (fallbacks: {model_names[1:]})")
    for i, r in enumerate(todo, 1):
        msgs = [SystemMessage(content=system), HumanMessage(content=build_audit_input(r, defs))]
        try:
            resp = retryable_invoke_models(models, msgs, retry=retry)
            text = format_message_content(getattr(resp, "content", resp))
            result = parse_auditor_json(text)
            result["model"] = getattr(resp, "response_metadata", {}).get("model_name") or model_names[0]
        except Exception as e:  # noqa: BLE001 - record and move on
            result = {"error": f"{type(e).__name__}: {e}"}
        r["auditor"] = result
        _save(path, records)  # incremental, so an interrupt loses at most one call
        tag = result.get("error") or f"{result['verdict']} s_type={result['subject_type']} o_type={result['object_type']}"
        print(f"[{i}/{len(todo)}] {r['subject']} -{r['predicate']}-> {r['object']}: {tag}")
    return 0


# ---------- label ----------

_VERDICT_KEYS = {"s": "supported", "q": "needs_qualification", "u": "unsupported", "i": "insufficient_evidence"}
_TYPE_KEYS = {"c": "correct", "x": "incorrect", "?": "unclear"}
_GROUND_KEYS = {"d": "direct", "r": "resolved_reference", "a": "ambiguous", "n": "absent"}


def _ask(prompt: str, keys: Mapping[str, str]) -> str | None:
    while True:
        raw = input(f"{prompt} [{'/'.join(keys)}, b=skip, quit=save+exit]: ").strip().lower()
        if raw == "quit":
            raise KeyboardInterrupt
        if raw == "b":
            return None
        if raw in keys:
            return keys[raw]


def cmd_label(args: argparse.Namespace) -> int:
    """Blind human labeling: the auditor's verdict is never shown."""
    path = Path(args.inp)
    records = _load(path)
    todo = [r for r in records if args.relabel or not (r.get("human") or {}).get("verdict")]
    print(f"{len(todo)} records to label. Auditor output is hidden so labels stay independent.\n")
    try:
        for i, r in enumerate(todo, 1):
            print("=" * 80)
            print(f"[{i}/{len(todo)}] provenance={r['provenance']}")
            print(f"CLAIM: {r['subject']} {r['subject_types']}  --{r['predicate']}-->  {r['object']} {r['object_types']}")
            for ev in r["evidence"]:
                print(f"\n--- evidence {ev['evidence_id'][:12]} {ev.get('title') or ''}{' (truncated)' if ev['truncated'] else ''}")
                print(ev["text"][: args.show_chars])
            print()
            verdict = _ask("Relationship verdict s=supported q=needs_qualification u=unsupported i=insufficient", _VERDICT_KEYS)
            if verdict is None:
                continue
            human: dict[str, Any] = {"verdict": verdict}
            human["subject_type"] = _ask(f"Subject type {r['subject_types']} c=correct x=incorrect ?=unclear", _TYPE_KEYS)
            human["object_type"] = _ask(f"Object type {r['object_types']} c=correct x=incorrect ?=unclear", _TYPE_KEYS)
            human["subject_grounding"] = _ask("Subject in text? d=direct r=resolved a=ambiguous n=absent", _GROUND_KEYS)
            human["object_grounding"] = _ask("Object in text? d=direct r=resolved a=ambiguous n=absent", _GROUND_KEYS)
            note = input("Note (optional): ").strip()
            if note:
                human["note"] = note
            r["human"] = human
            _save(path, records)
    except (KeyboardInterrupt, EOFError):
        print("\nSaved. Re-run to continue.")
    return 0


# ---------- score ----------

def cmd_score(args: argparse.Namespace) -> int:
    path = Path(args.inp)
    report = score_records(_load(path))
    print(format_report(report))
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nJSON report -> {args.report}")
    return 0


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv()
    p = argparse.ArgumentParser(prog="python -m evals.graph_audit", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("health", help="Served graph shape: type conflicts, edge mix, provenance (AGE).")
    h.add_argument("--top", type=int, default=10)
    h.add_argument("--dsn", help="Postgres DSN; default PG_DSN.")
    h.set_defaults(func=cmd_health)

    dr = sub.add_parser("drops", help="Schema-filter drop accounting from the ingested_docs registry (Postgres).")
    dr.add_argument("--top", type=int, default=10)
    dr.add_argument("--dsn", help="Postgres DSN; default PG_DSN.")
    dr.set_defaults(func=cmd_drops)

    s = sub.add_parser("sample", help="Random sample of entity relationships with source evidence.")
    s.add_argument("--n", type=int, default=100)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--predicate", action="append", help="Restrict to a relationship type (repeatable).")
    s.add_argument("--max-docs", type=int, default=3, help="Co-mention documents per claim when provenance is missing.")
    s.add_argument("--max-chars", type=int, default=6000, help="Excerpt length per evidence document.")
    s.add_argument("--out", default=str(DEFAULT_OUT))
    s.add_argument("--force", action="store_true")
    s.add_argument("--dsn", help="Postgres DSN; default PG_DSN.")
    s.set_defaults(func=cmd_sample)

    a = sub.add_parser("audit", help="Run the LLM auditor over a sample (billed).")
    a.add_argument("--in", dest="inp", default=str(DEFAULT_OUT))
    a.add_argument("--models", help="Comma-separated; default LF_GRAPH_AUDIT_MODELS or claude-opus-4-5.")
    a.add_argument("--type-defs", help="JSON file mapping entity type -> definition.")
    a.add_argument("--limit", type=int)
    a.add_argument("--redo", action="store_true", help="Re-audit records that already have a result.")
    a.set_defaults(func=cmd_audit)

    lb = sub.add_parser("label", help="Blind human labeling in the terminal.")
    lb.add_argument("--in", dest="inp", default=str(DEFAULT_OUT))
    lb.add_argument("--show-chars", type=int, default=3000)
    lb.add_argument("--relabel", action="store_true")
    lb.set_defaults(func=cmd_label)

    sc = sub.add_parser("score", help="Summarize auditor results and human agreement.")
    sc.add_argument("--in", dest="inp", default=str(DEFAULT_OUT))
    sc.add_argument("--report", help="Also write the JSON report here.")
    sc.set_defaults(func=cmd_score)

    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
