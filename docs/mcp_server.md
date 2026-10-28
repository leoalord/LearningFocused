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
# Cloud Run: MCPAdapter("<service URL>/mcp")
```

## Public tools

The tools depend on `KB_BACKEND` (set by `./scripts/deploy_mcp.sh`):

| Tool | `chroma` (legacy local backend) | `postgres` (deploy default) |
| --- | --- | --- |
| `search` | `query_summaries` + `query_segments` → typed `SearchResults` / `SourceChunk` | Same shape; ranking from `KB_SEARCH_MODE` (`hybrid` deploy default; `vector` and `keyword` also supported) |
| `fetch` | Same helpers, filtered by `episode_id` or `doc_id` | Chunks of that `episode_id` / `doc_id`, transcript order |
| `keyword_search` | not offered | Postgres full-text (`"phrase"`, `OR`, `-word`), ranked by `ts_rank_cd` |
| `vector_search` | not offered | pgvector cosine similarity on the stored `text-embedding-3-small` vectors |
| `hybrid_search` | not offered | keyword + vector fused with Reciprocal Rank Fusion (k=60) |
| `graph_query` | not offered | Fixed templates over the AGE graph: `schema`, `find_entities`, `neighbors`, `connections` (1-3 hops), `mentions` |

`graph_query` never takes query text: the caller picks a template and passes values (entity names, a label checked against the graph's own labels, hop count), which travel as bound parameters. `connections` returns shortest paths first and doesn't route through source passages (`MENTIONS`). Every Postgres connection is read-only (`default_transaction_read_only`, and a role that can only SELECT) with a statement timeout (`PG_STATEMENT_TIMEOUT_MS`, default 5000). On the chroma backend, graph / Cypher stay off this surface (`query_knowledge_graph` remains a local LangChain `@tool` for CLIs only).

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

The same gold set runs against Postgres (needs `PG_DSN`, e.g. through the IAP tunnel in [postgres.md](postgres.md)). Compare these with the Chroma number before switching the public service:

```bash
uv run python -m evals.run --backend postgres --report evals/report_pg_vector.json
uv run python -m evals.run --backend postgres --mode hybrid --report evals/report_pg_hybrid.json
uv run python -m evals.run --backend postgres --mode keyword --report evals/report_pg_keyword.json
```

## Cloud Run (LearningFocus / us-west1)

One GCP project: LearningFocus (`gen-lang-client-0304733057`, the scripts' default; `GCP_BACKUP_PROJECT` overrides it, see `scripts/_gcp_env.sh`). Do **not** deploy to `inferpoker`; that project belongs to InferPoker.

| | |
| --- | --- |
| Service | `learningfocused-mcp` |
| Region | `us-west1` |
| Project | `gen-lang-client-0304733057` |
| HTTPS MCP | `<service URL>/mcp` (printed by `./scripts/deploy_mcp.sh`) |
| Health | `<service URL>/health` |
| Auth | Public unauthenticated (`allUsers` `roles/run.invoker`) |
| Warm | `--min-instances=1`, `--no-cpu-throttling` |
| Memory | 2Gi / 1 CPU |
| Vectors | Hydrate `gs://learningfocused-corpus/chroma_db/` onto `/data/chroma_db` at startup; `get_vector_store()` still uses local `CHROMA_DIR` |

Secret Manager (values never committed):

- `learningfocused-openai-api-key` → `OPENAI_API_KEY` (required; embeddings)
- `learningfocused-google-api-key` → `GOOGLE_API_KEY`
- `learningfocused-assemblyai-api-key` → `ASSEMBLYAI_API_KEY`

Runtime SA `learningfocused-mcp@gen-lang-client-0304733057.iam.gserviceaccount.com` has `roles/storage.objectViewer` on the private bucket and `roles/secretmanager.secretAccessor` on those secrets.

```bash
./scripts/deploy_mcp.sh
```

Hydrate is `python -m src.mcp_server.hydrate` in `scripts/mcp_entrypoint.sh`. It copies the snapshot; it does not `--reset-chroma`.

The daily ingest **job** (`learningfocused-ingest`) is a separate Cloud Run Job. It writes new chunks straight into Postgres, which this service reads live, so it no longer rolls the service. See `docs/daily_ingest.md`.

Smoke the public endpoint (Streamable HTTP initialize):

```bash
URL="$(gcloud run services describe learningfocused-mcp --region=us-west1 --project=gen-lang-client-0304733057 --format='value(status.url)')"
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

### Switching the service to Postgres

After the Postgres VM is filled (see [postgres.md](postgres.md)) and the eval holds:

```bash
./scripts/deploy_mcp.sh                       # Postgres, hybrid ranking, min instances 0, 1Gi
KB_SEARCH_MODE=vector ./scripts/deploy_mcp.sh # explicitly select vector ranking
```

On `postgres` the service:

- skips the Chroma hydrate and connects as the read-only `learningfocused_mcp` role (`PG_DSN` from the `learningfocused-pg-dsn-reader` secret; SELECT only, read-only transactions, at most 15 connections), over Direct VPC egress (private ranges only);
- opens at most 4 connections per instance (`PG_MAX_CONNECTIONS`) and takes 8 requests per instance (`MCP_CONCURRENCY`), with 5s connect and statement timeouts; database errors reach clients as a short "busy / unavailable" message, never with hosts or roles;
- defaults to 0 minimum instances with 1Gi and CPU throttling, which removes most of the always-on Cloud Run cost (override with `MCP_MIN_INSTANCES` / `MCP_MEMORY`). The trade-off is a cold start of a few seconds on the first request after it has been idle.

`./scripts/deploy_mcp.sh` now defaults to `KB_BACKEND=postgres KB_SEARCH_MODE=hybrid` (what the live service runs) and refuses `KB_BACKEND=chroma`: the daily job writes Postgres only, so the Chroma snapshot no longer gets new content. It still warns when the job isn't wired to Postgres.
