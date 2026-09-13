## Pipelines

This package contains the processing pipelines for different source types.

- **Audio/Podcast pipeline**: `src/pipeline/audio/`
- **Substack pipeline**: `src/pipeline/substack/`
- **YouTube pipeline**: `src/pipeline/youtube/` (unique ingest; design in `docs/youtube_pipeline.md`)
- **Daily ingest** (all three, isolated): `src/pipeline/daily_ingest.py` (Cloud Run Job + Scheduler; `docs/daily_ingest.md`)

### Recommended entrypoints

- **Run the full audio pipeline**:

```bash
uv run python -m src.pipeline.audio.run
```

- **Run the processing-only audio orchestrator** (download → transcribe → identify → segment):

```bash
uv run python -m src.pipeline.audio.process_all
```

- **Run the full Substack pipeline** (ingest + summarize; optional indexing via flags):

```bash
uv run python -m src.pipeline.substack.run -- --mode daily --ingest-limit 10
```

- **YouTube unique ingest** (V1 cap; refuses `--channel` / `--reset-chroma` / UU dump):

```bash
uv run python -m src.pipeline.youtube.run --help
uv run python -m src.pipeline.youtube.run --dry-run
uv run python -m src.pipeline.youtube.run
```

- **Daily ingest** (Art19 + Substack + unique YouTube, isolated failures, ledger, GCS, MCP roll):

```bash
uv run python -m src.pipeline.daily_ingest --dry-run --skip-hydrate --skip-sync --skip-roll
./scripts/deploy_ingest_job.sh
```

### Notes

- The audio pipeline is the current production path.
- The Substack pipeline is intentionally separate to keep ingestion concerns isolated.
