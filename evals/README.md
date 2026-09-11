# Retrieval gold set

Gold questions for later retrieval scoring (TASK-11). This directory is the gold set + schema only — it is not an eval runner.

Corpus: Future of Education podcast (Alpha School / Two Hour Learning) plus the Future of Education Substack. Questions were grounded in on-disk artifacts (`metadata_output/`, `segmented_transcripts/`, `substack_articles/`, `article_summaries/`), not invented episode numbers.

## Files

| File | Role |
| --- | --- |
| `gold_questions.json` | Canonical gold set (JSON array, 18 items) |
| `schema.example.json` | One-item copy of the schema for a later agent to copy |

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

## Mix in this set

- **18** questions total (within 15–20).
- **Nine** 2026-podcast items (`2026-*`): Alpha Anywhere, nine school models, ESAs / GT Anywhere, 4 Cs, Brain Lift, guides vs certified teachers, Texas Sports Academy / TimeBack, Pygmalion effect, India model test.
- **Five** Substack items (`substack-*`).
- **Four** earlier podcast items (2-hour / mastery, student day, guides vs lecturing, check charts).
