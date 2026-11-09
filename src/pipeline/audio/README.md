## Audio pipeline (`src/pipeline/audio/`)

Canonical implementation for the podcast/audio workflow.

### Design (how to read this package)

- `process_all.py` is the **processing-only** runner (files in → files out).
- `run.py` is the **orchestrator** that calls `process_all`, then optionally runs indexing (Chroma/Neo4j).
- Indexing logic is split:
  - Pipeline-owned “what to index”: `index_chroma.py`, `index_neo4j.py`
  - DB-owned “how to store/query”: `src/database/chroma_manager.py`, `src/database/neo4j_manager.py`

### Full pipeline

The canonical runner is `src.pipeline.audio.run`:

```bash
uv run python -m src.pipeline.audio.run
```

To skip indexing (Chroma/Neo4j) and only produce files:

```bash
uv run python -m src.pipeline.audio.run --skip-chroma --skip-neo4j
```

### Neo4j schema profile (graph extraction)

By default the Neo4j graph extraction uses the **extended** relationship schema (good for learning science + research expansion).

If you want a smaller MVP relationship set for a run:

```bash
uv run python -m src.pipeline.audio.run --neo4j-schema-profile core
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

### Chunks (what retrieval and graph extraction see)

Index-time chunks are built from `transcripts/` by `chunking.py`, not copied from the
topic segments. Topic segments supply the boundaries and the `Topic:` header line:

- text and speaker names come from the transcript (`speaker_name`), with the host's
  name normalized to "MacKenzie Price" and the "Welcome to the Future of Education
  podcast hosted by…" intro credited to the Narrator even when diarization merged it
  into another turn;
- every utterance is included (the old segments dropped those between LLM timestamps);
- a topic over 800 tokens is split into even pieces at turn or sentence boundaries,
  a topic under 150 tokens merges into the next one, and a host question at the end
  of a chunk moves to the chunk holding its answer;
- turns over 1,500 words are split at sentences and flagged `long_turn` (diarization
  returned most of the episode as one turn; in live Q+A episodes the label covers
  several people);
- the LLM's generated summary is not embedded.

Chunk ids are `transcript_segment_<episode key>_<start seconds>`. After changing the
chunker, re-index and prune once; the prune replaces most `transcript_segment` rows,
so the 10% guard must be lifted for that run:

```bash
uv run python -m src.pipeline.index_pg --no-articles --prune-stale --max-prune-fraction 1
```
