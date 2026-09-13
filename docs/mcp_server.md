# Public FastMCP server (TASK-16 / TASK-17 / TASK-18)

Public product is a **FastMCP 4** Streamable HTTP server. Same binary locally and on Cloud Run. Protocol MCP `2026-07-28`, `stateless_http=True`, endpoint **`/mcp`**.

Not stdio. Not SSE. Not `langchain-mcp-adapters`. Not `to_fastmcp`. Not Chroma Cloud. Not Hugging Face.

## Local

Pins (already in `pyproject.toml` / `uv.lock`): `fastmcp>=4.0.3`, `langchain[mcp]>=1.4.0`.

```bash
uv run python -m src.mcp_server
```

Listens on **http://127.0.0.1:8000/mcp** (`transport="http"`, `stateless_http=True`). Health: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health).

Cursor (`~/.cursor/mcp.json` or project `.cursor/mcp.json`) is a **url**, not a command:

```json
{
  "mcpServers": {
    "learningfocused-local": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

LangChain consumer (same protocol, client role):

```python
from langchain.mcp import MCPAdapter

adapter = MCPAdapter("http://127.0.0.1:8000/mcp")
# Cloud Run: MCPAdapter("https://learningfocused-mcp-920577997010.us-west1.run.app/mcp")
```

## Public tools

Only two tools. Graph / Cypher stay off this surface (`query_knowledge_graph` remains a local LangChain `@tool` for CLIs only).

| Tool | Role |
| --- | --- |
| `search` | `query_summaries` + `query_segments` → typed `SearchResults` / `SourceChunk` |
| `fetch` | Same helpers, filtered by `episode_id` or `doc_id` |

Caps (TASK-18), env-overridable:

| Cap | Default | Env |
| --- | --- | --- |
| `max_segments` | default 5, max **8** | `MCP_MAX_SEGMENTS` |
| `max_summaries` | default 3, max **5** | `MCP_MAX_SUMMARIES` |
| snippet characters | **900** | `MCP_SNIPPET_LIMIT` |
| query length | 500 | (code) |
| IP rate limit | **60 requests / 60s** per IP (X-Forwarded-For) | `MCP_RATE_LIMIT_PER_MINUTE` (0 disables) |

Every `search` / `fetch` hit includes attribution fields on `SourceChunk` (`title`, `canonical_url`, `doc_id`, `episode_id`, …) and a bounded `snippet`. Full transcripts are not returned.

Agent and local chat UI are unchanged: they still use LangChain `@tool` wrappers in `src/deep_research_agent/tools.py`.

## Evals

`uv run python -m evals.run` must stay **15/18**. It calls `src.mcp_server.retrieval.retrieve_documents` **in-process** (the function `search` wraps). It does **not** start an HTTP server.

```bash
uv run python -m evals.run
```

## Cloud Run (inferpoker / us-west1)

One GCP project: **inferpoker**. Do **not** deploy to `.env` `GCP_PROJECT_ID=gen-lang-client-0304733057`.

| | |
| --- | --- |
| Service | `learningfocused-mcp` |
| Region | `us-west1` |
| Project | `inferpoker` |
| HTTPS MCP | `https://learningfocused-mcp-920577997010.us-west1.run.app/mcp` |
| Alternate URL | `https://learningfocused-mcp-z2rzib77rq-uw.a.run.app/mcp` |
| Health | `https://learningfocused-mcp-920577997010.us-west1.run.app/health` |
| Auth | Public unauthenticated (`allUsers` `roles/run.invoker`) |
| Warm | `--min-instances=1`, `--no-cpu-throttling` |
| Memory | 2Gi / 1 CPU |
| Vectors | Hydrate `gs://inferpoker-learningfocused/chroma_db/` onto `/data/chroma_db` at startup; `get_vector_store()` still uses local `CHROMA_DIR` |

Secret Manager (values never committed):

- `learningfocused-openai-api-key` → `OPENAI_API_KEY` (required; embeddings)
- `learningfocused-google-api-key` → `GOOGLE_API_KEY`
- `learningfocused-assemblyai-api-key` → `ASSEMBLYAI_API_KEY`

Runtime SA `learningfocused-mcp@inferpoker.iam.gserviceaccount.com` has `roles/storage.objectViewer` on the private bucket and `roles/secretmanager.secretAccessor` on those secrets.

```bash
./scripts/deploy_mcp.sh
```

Hydrate is `python -m src.mcp_server.hydrate` in `scripts/mcp_entrypoint.sh`. It copies the snapshot; it does not `--reset-chroma`.

Smoke the public endpoint (Streamable HTTP initialize):

```bash
URL="$(gcloud run services describe learningfocused-mcp --region=us-west1 --project=inferpoker --format='value(status.url)')"
# currently: https://learningfocused-mcp-920577997010.us-west1.run.app
curl -sS -D - -o /tmp/mcp-init.out \
  -X POST "${URL}/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2026-07-28","capabilities":{},"clientInfo":{"name":"smoke","version":"0.1"}}}'
```

Or:

```python
from langchain.mcp import MCPAdapter
MCPAdapter("https://<service-url>/mcp")
```
