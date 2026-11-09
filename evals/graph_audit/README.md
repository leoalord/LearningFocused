# Graph extraction audit

Measures knowledge-graph extraction quality and separates two failure modes:

- **Losing claims.** The extractor proposes a node or edge with a type outside the
  allowed vocabulary and the schema filter drops it.
- **Dirty data.** A claim gets into the graph but the source doesn't support it:
  an invented endpoint, a wrong entity type, a wrong predicate or direction, or a
  lost condition.

The audit reads the **served graph in Postgres AGE** (`PG_DSN`), the one the MCP
server and agents query. Extraction still writes to Neo4j and
`src/database/neo4j_to_age.py` copies it across, carrying relationship properties
and every type label (in a `labels` property, since AGE allows one label per node).

A single "approval rate" mixes these up. This tool reports them separately, and
checks the LLM auditor against blind human labels so you know how far to trust it.

## What the pipeline records now

`src/pipeline/index_neo4j.py` runs `LLMGraphTransformer` with `strict_mode=False`
and filters in `src/pipeline/graph_quality.py` so every drop is counted:

| Where | Field | Meaning |
| --- | --- | --- |
| `IngestedDoc` | `raw_nodes` / `kept_nodes`, `raw_rels` / `kept_rels` | What the model proposed vs what passed the schema filter |
| `IngestedDoc` | `dropped_node_types`, `dropped_rel_types` | Off-vocabulary types, as `"Type:count"` strings |
| `IngestedDoc` | `rels_dropped_for_endpoint`, `extract_model` | Edges lost because an endpoint was dropped; which model in the fallback chain ran |
| entity relationships | `source_doc_id`, `extract_model`, `ingest_run_id` | The `Document.id` the edge was first extracted from (set on create) |

Each batch also logs a `schema filter:` line, and the run ends with totals.

The `IngestedDoc` registry stays with extraction (Neo4j today) and is not copied to
AGE, so drop counts come from the `drops` command. Edge provenance reaches AGE on
the next `neo4j_to_age` run.

Graphs built before this change have none of these fields. `health` still
reports graph shape for them, and `sample` falls back to documents that mention
both endpoints (`provenance: co_mention`), which is weaker evidence.

## Workflow

```bash
# 1. Served graph (AGE): entities with conflicting type labels, edge-type mix, provenance coverage
uv run python -m evals.graph_audit health

# 1b. Extraction drop rates and off-vocab types (ingest registry, Neo4j today)
uv run python -m evals.graph_audit drops

# 2. Random sample of AGE entity relationships with source excerpts (read-only)
uv run python -m evals.graph_audit sample --n 100

# 3. LLM auditor (billed). Default claude-opus-4-5; override with --models or LF_GRAPH_AUDIT_MODELS
uv run python -m evals.graph_audit audit

# 4. Blind human labels in the terminal (auditor output hidden). 50+ is enough to start
uv run python -m evals.graph_audit label

# 5. Report
uv run python -m evals.graph_audit score --report evals/graph_audit/runs/report.json
```

All commands default to `evals/graph_audit/runs/sample.jsonl` (gitignored, since
it holds source excerpts). `audit` and `label` save after every record, so you can
stop and resume.

If your entity types differ from Person/Organization/Concept/Tool/Event, set
`NEO4J_ALLOWED_NODES` or pass `--type-defs types.json` (`{"Type": "definition"}`).
The auditor judges types against those definitions.

## Reading the report

| Metric | Answers |
| --- | --- |
| `drops`: nodes / relationships kept | Are we throwing away a lot of what the model found? Check the top off-vocab types: they may be types to add to the schema |
| `health`: entities with >1 type label | Mislabeling already in the graph. Entities merge on name, so "X" tagged Person once and Organization once becomes one node with both labels |
| Auditor primary category | Why rejected claims fail: `hallucinated_endpoint`, `wrong_entity_type`, `bad_identity_link`, `wrong_predicate`, `lost_condition`, `missing_evidence` |
| By predicate / provenance / extract model | Whether one edge type (often `DISCUSSED`) or one fallback model causes most failures |
| Dirty pass rate | Of claims the auditor accepted, the share a human judged bad. High means bad data gets through |
| False reject rate | Of claims a human judged good, the share the auditor rejected. High means the auditor is too strict, and low approval understates quality |
| Graph precision (human) | The share of sampled graph edges that are actually correct |

A claim counts as **accepted** only when the verdict is `supported` and neither
endpoint type is `incorrect`. `needs_qualification` counts as not accepted but
is reported separately as `lost_condition`.

The auditor prompt is `auditor_prompt.md`. It adds entity-type checks to the
source-grounded claim audit, and tells the auditor whether the evidence is the
edge's exact source or only a co-mention.
