## Audio pipeline (`src/pipeline/audio/`)

Canonical implementation for the podcast/audio workflow.

### Design (how to read this package)

- `process_all.py` is the **processing-only** runner (files in → files out).
- `run.py` is the **orchestrator** that calls `process_all`, then optionally indexes into Postgres (chunks + embeddings, then LLM graph extraction into the AGE graph).
- Indexing logic is split:
  - Pipeline-owned “what to index”: `index_chroma.py`, `index_neo4j.py` (passages for graph extraction; the name is historical)
  - Shared indexers: `src/pipeline/index_pg.py` (chunks) and `src/pipeline/index_graph.py` (graph, written by `src/database/age_graph.py`)

### Full pipeline

The canonical runner is `src.pipeline.audio.run`:

```bash
uv run python -m src.pipeline.audio.run
```

To skip indexing and only produce files:

```bash
uv run python -m src.pipeline.audio.run --skip-index --skip-graph
```

### Graph extraction (Postgres AGE)

Unless `--skip-graph` (old name `--skip-neo4j` still works), the runner sends new or changed transcript segments through LLM graph extraction and writes the result into the Postgres AGE graph `knowledge` (billed; needs `PG_DSN`, no Neo4j). In daily mode it is skipped when the run produced no new transcripts, unless `--run-graph-without-new-transcripts`. `--graph-audio-limit N` reads only the first N transcripts. See `docs/postgres.md`.

By default extraction uses the **extended** relationship schema (good for learning science + research expansion). For a smaller relationship set:

```bash
uv run python -m src.pipeline.audio.run --graph-schema-profile core
```

### Standalone audio orchestrator

Runs: download → transcribe → identify speakers → segment topics

```bash
uv run python -m src.pipeline.audio.process_all
```

### Backfill / catch-up runs

Use **backfill mode** to only run missing steps across existing `transcripts/` (and to batch work with limits).

- **Backfill speaker identification** (only when `speaker_map` is missing), 25 at a time:

```bash
uv run python -m src.pipeline.audio.process_all --mode backfill --skip-download --skip-transcribe --skip-segment --identify-limit 25
```

- **Backfill topic segmentation** (only when `*_segmented.json` is missing), 10 at a time:

```bash
uv run python -m src.pipeline.audio.process_all --mode backfill --skip-download --skip-transcribe --skip-identify --segment-limit 10
```

- **Scope to a subset of transcripts** (example: only Season 2 episodes if your filenames start with `S2`):

```bash
uv run python -m src.pipeline.audio.process_all --mode backfill --transcripts-glob 'S2*.json' --identify-limit 25
uv run python -m src.pipeline.audio.process_all --mode backfill --transcripts-glob 'S2*.json' --segment-limit 10 --skip-identify
```

### Re-run everything (override the default "only missing" behavior)

```bash
uv run python -m src.pipeline.audio.process_all --mode backfill --identify-all --identify-limit 25
uv run python -m src.pipeline.audio.process_all --mode backfill --segment-all --segment-limit 10 --skip-identify
```

### Individual steps

```bash
uv run python -m src.pipeline.audio.download
uv run python -m src.pipeline.audio.transcribe /path/to/episode.mp3
uv run python -m src.pipeline.audio.identify_speakers /path/to/transcript.json
uv run python -m src.pipeline.audio.segment_topics
uv run python -m src.pipeline.audio.generate_summaries
```

### Outputs (filesystem)

- `podcast_downloads/`: downloaded `.mp3`
- `transcripts/`: AssemblyAI transcripts (`.json`)
- `segmented_transcripts/`: topic segments (`*_segmented.json`)
- `combined_summaries/`: group summaries (`summary_*.json`)
