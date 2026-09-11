## Pipelines

This package contains the processing pipelines for different source types.

- **Audio/Podcast pipeline**: `src/pipeline/audio/`
- **Substack pipeline**: `src/pipeline/substack/`
- **YouTube pipeline**: `src/pipeline/youtube/` (TASK-4 stub; design in `docs/youtube_pipeline.md`)

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

- **YouTube pipeline stub** (prints the plan; refuses downloads / full-channel ingest / Chroma writes):

```bash
uv run python -m src.pipeline.youtube.run --help
```

### Notes

- The audio pipeline is the current production path.
- The Substack pipeline is intentionally separate to keep ingestion concerns isolated.
