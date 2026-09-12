# Retrieval gold set

Gold questions plus a read-only retrieval eval runner (TASK-11).

Corpus: Future of Education podcast (Alpha School / Two Hour Learning) plus the Future of Education Substack. Questions were grounded in on-disk artifacts (`metadata_output/`, `segmented_transcripts/`, `substack_articles/`, `article_summaries/`), not invented episode numbers.

## Files

| File | Role |
| --- | --- |
| `gold_questions.json` | Canonical gold set (JSON array, 18 items) |
| `schema.example.json` | One-item copy of the schema for a later agent to copy |
| `run.py` | Read-only eval runner (`uv run python -m evals.run`) |
| `scoring.py` | Hit definition + miss classification helpers |
| `last_report.json` | Latest run output (gitignored; overwritten each run) |
| `last_report.example.json` | Tiny committed copy of the report shape |

## Item schema

Each object has four required fields:

| Field | Type | Purpose |
| --- | --- | --- |
| `id` | string | Stable item id. Ids starting with `2026-` are 2026-podcast items; `substack-` are Substack articles. |
| `question` | string | Natural-language query to send to retrieval. |
| `must_include` | string[] | Distinctive phrases that a **hit** must surface. Phrases are copied from real segmented transcripts or Substack text/summaries. |
| `source_hint` | string | Human hint for miss analysis (episode id, published date, artifact path). **Not used to score a hit.** |

See `schema.example.json` for a single-item example.

## How a later agent scores a hit

TASK-11 (or any later eval runner) should score **retrieval**, not free-form LLM answers.

1. Run the question against the retriever (Chroma / agent search). Collect the retrieved documents' text (page content; concatenating the top-k is fine).
2. Normalize both sides with case-folding (e.g. `text.casefold()`).
3. **Hit:** every string in `must_include` appears as a substring in the concatenated retrieved text.
4. **Miss:** any `must_include` string is absent. Do not award partial credit unless the runner explicitly reports a per-phrase breakdown as a diagnostic.

`source_hint` is for miss analysis only. After a miss, check whether any retrieved doc is the hinted episode (`episode_id` like `S2E335`) or Substack `doc_id`. A miss with the right source retrieved usually means the gold phrase is too strict; a miss with the wrong source means recall failed.

Primary metric: hit-rate = hits / 18. Report 2026 vs Substack slices if useful.

Do not treat `source_hint` as a required metadata match for the official hit. Metadata naming differs across stores (segment `episode_id` vs Substack `doc_id`), so phrase containment is the stable contract.

## Eval runner (TASK-11)

Requires an existing on-disk Chroma store (`chroma_db/`) and embedding credentials in `.env`. Each question issues billed `text-embedding-3-small` queries. The runner **does not** `--reset-chroma`, does not add documents, and does not run ingest pipelines. It opens the collection read-only (`create_collection_if_not_exists=False`) and does not create pipeline data directories.

```bash
uv run python -m evals.run
```

Optional flags: `--gold`, `--report`, `--max-segments` (default 5), `--max-summaries` (default 3). Defaults match `search_knowledge_base` (`query_summaries` then `query_segments`).

Stdout prints `N`, hit rate, missed ids, miss split, and each failed question (not only a percentage). JSON is written to `evals/last_report.json`.

Hit scoring is the contract above: concatenated retrieved `page_content`, case-fold, every `must_include` substring. `source_hint` is not used to score.

Miss types (analysis only; does not change the hit/miss label):

| `miss_type` | Meaning |
| --- | --- |
| `not_in_corpus` | Hinted source artifact missing on disk, or present but not indexed in Chroma. |
| `retriever_failed` | Hinted source is indexed (or phrases exist in that retrievable source) but top-k did not surface every `must_include` phrase. |

Scoring unit tests (no Chroma):

```bash
uv run pytest evals/test_scoring.py
```

### Report shape

`evals/last_report.json` (see also `last_report.example.json`) is an object with:

| Field | Meaning |
| --- | --- |
| `n`, `hits`, `hit_rate` | Official metrics (`hit_rate = hits / n`) |
| `missed_ids` | Failed question ids (empty list if none) |
| `miss_split` | Counts for `not_in_corpus` and `retriever_failed` |
| `slices` | `2026_podcast` / `substack` / `earlier_podcast` |
| `questions[]` | Per-item `hit`, `missing_phrases`, `miss_type`, `corpus` diagnostics |

The report stores titles and ids, not full retrieved text, so it stays small enough to commit.

## Mix in this set

- **18** questions total (within 15–20).
- **Nine** 2026-podcast items (`2026-*`): Alpha Anywhere, nine school models, ESAs / GT Anywhere, 4 Cs, Brain Lift, guides vs certified teachers, Texas Sports Academy / TimeBack, Pygmalion effect, India model test.
- **Five** Substack items (`substack-*`).
- **Four** earlier podcast items (2-hour / mastery, student day, guides vs lecturing, check charts).
