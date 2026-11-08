### Substack pipeline (`src/pipeline/substack/`)

This package implements a **text-first ingestion pipeline** for Substack articles, parallel to the audio pipeline under `src/pipeline/audio/`.

## What it does (current state)

- **Ingest** RSS items from a Substack feed
  - Persist **metadata**, **raw HTML**, and **normalized text** (markdown-ish)
  - Idempotent by **canonical URL** + **content hash**
- **Summarize + tag**
  - Generates a structured summary object (thesis/overview/themes/takeaways + extracted entities when explicit in text)
- **Index (optional)**
  - **Chroma**: Lean indexing by default (2 vectors per article):
    - `article_text` (full article text)
    - `article_summary_overview` (thesis + overview + themes/keywords)
  - **Graph**: article text goes through LLM graph extraction into the Postgres AGE graph (`src/pipeline/index_graph.py`).

## Artifacts (where files land)

Configured in `src/config.py`:

- `substack_articles/metadata/<doc_id>.json`
- `substack_articles/html/<doc_id>.html`
- `substack_articles/text/<doc_id>.md`
- `article_summaries/<doc_id>_summary.json`

## Key modules

- **`download_articles.py`**: RSS → canonicalize URL → persist metadata/html/text.
- **`summarize_articles.py`**: LLM summary + tags → persist `article_summaries/*_summary.json`.
- **`process_all.py`**: Orchestrates ingest + summarization (no DB writes).
- **`run.py`**: Full runner that calls `process_all` then optionally updates Chroma.
- **`index_chroma.py`**: Pipeline-owned logic for converting Substack artifacts into `langchain_core.documents.Document` objects.
- **`index_neo4j.py`**: Pipeline-owned logic for converting Substack artifacts into `Document` objects for graph extraction (the name is historical; the graph is in Postgres).

## How to run

Run the full Substack pipeline (ingest + summaries + Chroma indexing):

```bash
uv run python -m src.pipeline.substack.run -- --mode daily --ingest-limit 10
```

Run an end-to-end test (ingest 10 + summarize + index + graph extraction for 10 articles):

```bash
uv run python -m src.pipeline.substack.run --graph-article-limit 10 -- --mode daily --ingest-limit 10 --allow-updates
```

Run processing only (no Chroma indexing):

```bash
uv run python -m src.pipeline.substack.run --skip-chroma -- --mode daily --ingest-limit 10
```

Backfill ingest (no limit):

```bash
uv run python -m src.pipeline.substack.process_all -- --mode backfill
```

## Graph extraction

Substack `run.py` runs graph extraction into the Postgres AGE graph (articles only) unless `--skip-graph` (old name `--skip-neo4j` still works). To extract over audio and Substack together:

```bash
uv run python -m src.pipeline.index_graph --max-docs 50
```

## Environment variables

- **`GOOGLE_API_KEY`**: required for summarization (Gemini via `langchain_google_genai`)
- **`OPENAI_API_KEY`**: required for Chroma embedding (OpenAI embeddings)
- **Graph extraction** (unless `--skip-graph`):
  - `PG_DSN` and an API key for a model in `LF_NEO4J_GRAPH_MODELS` (no Neo4j needed)
  - Optional schema expansion:
    - `NEO4J_SCHEMA_PROFILE=core|extended` (default: `extended`)
    - `NEO4J_ALLOWED_NODES` / `NEO4J_ALLOWED_RELATIONSHIPS` (comma-separated overrides)
