# Future of Education Knowledge Engine

Build a queryable knowledge base from a corpus of podcast audio + transcripts + derived summaries + Substack articles. It supports:

- **Filesystem artifacts** (ground truth outputs)
- **Chroma** (semantic retrieval)
- **Neo4j** (entities + relationships)
- **CLI agents** for Q&A and research

## Stores (high level)

- **Filesystem**: raw MP3s, transcripts, segmented topics, generated summaries
- **GCS backup** (off-laptop; one private bucket): see `docs/gcs_backup.md`
- **Chroma**: embeddings for transcript segments + summary-like docs + Substack text/summaries
- **Neo4j**: graph extraction over Documents (good for “what’s connected to what?”)

## Quick start

### Prereqs

- **Python 3.12+**
- **uv** for dependency/env management
- **Docker** (optional) if running Neo4j locally

### Environment

Create a `.env` in the project root:

```bash
# Transcription (audio pipeline)
ASSEMBLYAI_API_KEY=your_key_here

# LLMs & embeddings
GOOGLE_API_KEY=your_gemini_key
OPENAI_API_KEY=your_openai_key

# Neo4j (only needed if running graph indexing / graph queries)
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=your_password
```

### Install

```bash
uv sync
```

### Run Neo4j locally (optional)

```bash
docker compose up -d
```

## Pipelines (building the corpus)

### Audio/podcast pipeline (canonical runner)

```bash
uv run python -m src.pipeline.audio.run
```

Skip indexing (filesystem artifacts only):

```bash
uv run python -m src.pipeline.audio.run --skip-chroma --skip-neo4j
```

See deeper docs: `src/pipeline/audio/README.md`.

### Substack pipeline

```bash
uv run python -m src.pipeline.substack.run -- --mode daily --ingest-limit 10
```

See deeper docs: `src/pipeline/substack/README.md`.

## Databases (populate/searchable stores)

- **Chroma** (vectors; builds/updates from pipeline artifacts):

```bash
uv run python -m src.database.chroma_manager
```

- **Neo4j** (graph; builds/updates from pipeline artifacts):

```bash
uv run python -m src.database.neo4j_manager
```

## Agents (query + research)

### Deep Research Agent (LangGraph)

- Interactive chat:

```bash
uv run python -m src.deep_research_agent.chat_cli
```

- One-shot run:

```bash
uv run python -m src.deep_research_agent.run
```

See deeper docs: `src/deep_research_agent/README.md`.

### React Agent (quick Q&A)

```bash
uv run python -m src.react_agent.chat_cli
```

See deeper docs: `src/react_agent/README.md`.

### Public MCP (Streamable HTTP)

Same FastMCP 4 binary locally and on Cloud Run. Tools are `search` and `fetch`. See `docs/mcp_server.md`.

```bash
uv run python -m src.mcp_server
```

Cursor config is a URL: `http://127.0.0.1:8000/mcp`. Evals stay in-process: `uv run python -m evals.run`.

### Local chat UI (answer + source cards)

Browser demo: ask a question, see the answer plus citation cards (`SourceChunk` title + episode or URL). Keys stay in `.env` on the server.

```bash
uv run python -m src.ui
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). See `src/ui/README.md`.

## Inspecting and visualizing the stores

### Chroma (vector DB)

- Quick sanity check + search:

```bash
uv run python -m src.analysis.inspect_chroma --query "parent burnout"
```

- Visualize clusters (writes `analysis_outputs/`):

```bash
uv run python -m src.analysis.visualize_chroma --limit 1200 --color-by type
```

- Investigate a point by id (no embedding API calls needed):

```bash
uv run python -m src.analysis.chroma_lookup get "<ID>"
uv run python -m src.analysis.chroma_lookup neighbors "<ID>" --k 10
```

### Neo4j (knowledge graph)

- Quick schema + hub nodes:

```bash
uv run python -m src.analysis.inspect_graph
```

- Richer insights (counts, hubs, degree distribution, co-mentions):

```bash
uv run python -m src.analysis.graph_insights --top-k 25 --explain
```

- Investigate an entity:

```bash
uv run python -m src.analysis.investigate_entity "MacKenzie Price"
```

## Project structure (high level)

- **`src/pipeline/`**: ingestion and processing pipelines (audio + Substack)
- **`src/database/`**: thin DB adapters/orchestrators (Chroma + Neo4j)
- **`src/analysis/`**: inspection/visualization tooling
- **`src/deep_research_agent/`**: LangGraph deep research CLI agent
- **`src/react_agent/`**: lightweight ReAct-style CLI agent
- **`src/ui/`**: local FastAPI chat UI (source cards)
- **`src/mcp_server/`**: public FastMCP 4 Streamable HTTP server (`search` + `fetch`)

## Notes

- Generated analysis artifacts land in `analysis_outputs/` and are git-ignored.
- The detailed, “how to run this source” docs live alongside implementations under `src/pipeline/*/README.md`.
