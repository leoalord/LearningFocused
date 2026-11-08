"""Graph extraction audit: sample edges, audit them against source text, score.

Usage:
    uv run python -m evals.graph_audit health
    uv run python -m evals.graph_audit sample --n 100 --out evals/graph_audit/runs/sample.jsonl
    uv run python -m evals.graph_audit audit  --in evals/graph_audit/runs/sample.jsonl
    uv run python -m evals.graph_audit label  --in evals/graph_audit/runs/sample.jsonl
    uv run python -m evals.graph_audit score  --in evals/graph_audit/runs/sample.jsonl

`health` and `sample` read Neo4j (read-only). `audit` calls a hosted LLM (billed).
`label` and `score` are local only. Each command rewrites the JSONL in place, so
auditor results and human labels accumulate on the same records.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from evals.graph_audit.scoring import (
    DEFAULT_TYPE_DEFINITIONS,
    build_audit_input,
    excerpt_around,
    format_report,
    parse_auditor_json,
    score_records,
    strip_extraction_preamble,
)

HERE = Path(__file__).resolve().parent
PROMPT_PATH = HERE / "auditor_prompt.md"
DEFAULT_OUT = HERE / "runs" / "sample.jsonl"
BASE_LABELS = {"__Entity__", "__KGBuilder__"}


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


def _graph() -> Any:
    from src.database.neo4j_manager import get_graph

    return get_graph()


# ---------- health ----------

def cmd_health(args: argparse.Namespace) -> int:
    g = _graph()
    print("=== Extraction drop accounting (IngestedDoc, runs after this change) ===")
    rows = g.query(
        """
        MATCH (d:IngestedDoc) WHERE d.raw_nodes IS NOT NULL
        RETURN count(d) AS docs, sum(d.raw_nodes) AS raw_nodes, sum(d.kept_nodes) AS kept_nodes,
               sum(d.raw_rels) AS raw_rels, sum(d.kept_rels) AS kept_rels,
               sum(d.rels_dropped_for_endpoint) AS endpoint_drops
        """
    )
    r = rows[0] if rows else {}
    if not r.get("docs"):
        print("No documents with extraction stats yet. Re-run graph extraction to collect them.")
    else:
        def frac(a: int, b: int) -> str:
            return f"{a}/{b} ({a / b:.0%})" if b else "n/a"

        print(f"Documents with stats: {r['docs']}")
        print(f"Nodes kept:         {frac(r['kept_nodes'], r['raw_nodes'])}")
        print(f"Relationships kept: {frac(r['kept_rels'], r['raw_rels'])}")
        print(f"  dropped because an endpoint type was off-vocabulary: {r['endpoint_drops']}")
        for label, prop in (("node", "dropped_node_types"), ("relationship", "dropped_rel_types")):
            top = g.query(
                f"""
                MATCH (d:IngestedDoc) WHERE d.{prop} IS NOT NULL
                UNWIND d.{prop} AS entry
                WITH split(entry, ':') AS parts
                WITH parts[0] AS t, toInteger(parts[size(parts)-1]) AS c
                RETURN t, sum(c) AS n ORDER BY n DESC LIMIT $k
                """,
                params={"k": args.top},
            )
            print(f"Top off-vocabulary {label} types: " + (", ".join(f"{x['t']}x{x['n']}" for x in top) or "none"))
        by_model = g.query(
            """
            MATCH (d:IngestedDoc) WHERE d.raw_rels IS NOT NULL
            RETURN d.extract_model AS model, count(d) AS docs, sum(d.raw_rels) AS raw, sum(d.kept_rels) AS kept
            ORDER BY docs DESC
            """
        )
        for m in by_model:
            print(f"  model={m['model']}: docs={m['docs']} rels kept {frac(m['kept'], m['raw'])}")

    print("\n=== Graph shape (works on existing graphs) ===")
    multi = g.query(
        """
        MATCH (n:__Entity__)
        WITH n, [l IN labels(n) WHERE NOT l IN $base] AS types
        WITH count(n) AS total, sum(CASE WHEN size(types) > 1 THEN 1 ELSE 0 END) AS conflicted
        RETURN total, conflicted
        """,
        params={"base": sorted(BASE_LABELS)},
    )
    if multi and multi[0]["total"]:
        t, c = multi[0]["total"], multi[0]["conflicted"]
        print(f"Entities: {t}; carrying more than one type label: {c} ({c / t:.1%})")
        examples = g.query(
            """
            MATCH (n:__Entity__)
            WITH n, [l IN labels(n) WHERE NOT l IN $base] AS types
            WHERE size(types) > 1
            RETURN n.id AS id, types, COUNT { (n)--() } AS degree
            ORDER BY degree DESC LIMIT $k
            """,
            params={"base": sorted(BASE_LABELS), "k": args.top},
        )
        for e in examples:
            print(f"  {e['id']}: {e['types']} (degree {e['degree']})")
    rels = g.query(
        """
        MATCH (:__Entity__)-[r]->(:__Entity__)
        RETURN type(r) AS t, count(r) AS n,
               sum(CASE WHEN r.source_doc_id IS NULL THEN 0 ELSE 1 END) AS with_provenance
        ORDER BY n DESC
        """
    )
    total = sum(x["n"] for x in rels)
    print(f"Entity relationships: {total}")
    for x in rels:
        print(f"  {x['t']:20s} {x['n']:>7d} ({x['n'] / total:.0%})  with source_doc_id: {x['with_provenance']}")
    return 0


# ---------- sample ----------

_FETCH = """
UNWIND $ids AS rid
MATCH (a)-[r]->(b) WHERE elementId(r) = rid
OPTIONAL MATCH (d:Document {id: r.source_doc_id})
RETURN rid, a.id AS subject, [l IN labels(a) WHERE NOT l IN $base] AS subject_types,
       type(r) AS predicate, b.id AS object, [l IN labels(b) WHERE NOT l IN $base] AS object_types,
       properties(r) AS rprops,
       CASE WHEN d IS NULL THEN null ELSE {id: d.id, text: d.text, title: d.title} END AS edge_doc,
       CASE WHEN d IS NULL THEN
         [(c:Document)-[:MENTIONS]->(a) WHERE (c)-[:MENTIONS]->(b) | {id: c.id, text: c.text, title: c.title}][..$k]
       ELSE [] END AS co_docs
"""


def cmd_sample(args: argparse.Namespace) -> int:
    g = _graph()
    where = "type(r) <> 'MENTIONS'"
    params: dict[str, Any] = {}
    if args.predicate:
        where += " AND type(r) IN $preds"
        params["preds"] = args.predicate
    ids = [row["rid"] for row in g.query(
        f"MATCH (:__Entity__)-[r]->(:__Entity__) WHERE {where} RETURN elementId(r) AS rid", params=params
    )]
    if not ids:
        print("No entity relationships found.", file=sys.stderr)
        return 2
    rng = random.Random(args.seed)
    chosen = rng.sample(ids, min(args.n, len(ids)))
    rows = g.query(_FETCH, params={"ids": chosen, "base": sorted(BASE_LABELS), "k": args.max_docs})

    records: list[dict[str, Any]] = []
    for row in rows:
        names = [row["subject"], row["object"]]
        if row["edge_doc"]:
            provenance, docs = "edge", [row["edge_doc"]]
        elif row["co_docs"]:
            provenance, docs = "co_mention", row["co_docs"]
        else:
            provenance, docs = "none", []
        evidence = []
        for d in docs:
            text, truncated = excerpt_around(strip_extraction_preamble(d.get("text") or ""), names, args.max_chars)
            evidence.append({"evidence_id": d["id"], "title": d.get("title"), "text": text, "truncated": truncated})
        records.append({
            "claim_id": row["rid"],
            "subject": row["subject"],
            "subject_types": row["subject_types"],
            "predicate": row["predicate"],
            "object": row["object"],
            "object_types": row["object_types"],
            "type_conflict": len(row["subject_types"]) > 1 or len(row["object_types"]) > 1,
            "relationship_properties": row["rprops"],
            "provenance": provenance,
            "evidence": evidence,
            "auditor": None,
            "human": None,
        })
    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"{out} exists; pass --force to overwrite (it may hold labels).", file=sys.stderr)
        return 2
    _save(out, records)
    prov: dict[str, int] = {}
    for r in records:
        prov[r["provenance"]] = prov.get(r["provenance"], 0) + 1
    print(f"Sampled {len(records)} of {len(ids)} relationships (seed={args.seed}) -> {out}")
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

    h = sub.add_parser("health", help="Drop accounting + graph shape (read-only Neo4j).")
    h.add_argument("--top", type=int, default=10)
    h.set_defaults(func=cmd_health)

    s = sub.add_parser("sample", help="Random sample of entity relationships with source evidence.")
    s.add_argument("--n", type=int, default=100)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--predicate", action="append", help="Restrict to a relationship type (repeatable).")
    s.add_argument("--max-docs", type=int, default=3, help="Co-mention documents per claim when provenance is missing.")
    s.add_argument("--max-chars", type=int, default=6000, help="Excerpt length per evidence document.")
    s.add_argument("--out", default=str(DEFAULT_OUT))
    s.add_argument("--force", action="store_true")
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
