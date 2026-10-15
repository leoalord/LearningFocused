# Postgres knowledge store (pgvector + Apache AGE)

One Postgres 16 database replaces Chroma (vectors) and local Neo4j (graph), and adds keyword search:

| Search style | How | Where |
| --- | --- | --- |
| Keyword | `chunks.tsv` (generated `tsvector`, GIN index), `websearch_to_tsquery` | `src/database/pg_schema.sql` |
| Vector | `chunks.embedding vector(1536)` (OpenAI `text-embedding-3-small`, same as Chroma), HNSW cosine index | same |
| Graph | Apache AGE graph `knowledge`, openCypher via `cypher()` | created by `pg_store.migrate` |

Tables: `sources` (every feed the daily job checks), `documents`, `chunks`, `ingest_runs`, `source_checks`.

## Where it runs

| | |
| --- | --- |
| Project | LearningFocus (`gen-lang-client-0304733057`), `us-west1-b` |
| VM | `learningfocused-pg`, e2-small, Container-Optimized OS, **no external IP** |
| Image | `us-west1-docker.pkg.dev/<project>/learningfocused/postgres:16-age-vector` (`deploy/postgres/Dockerfile`), run by digest. Built once; `PG_REBUILD_IMAGE=1` rebuilds (if AGE's version changes, run `ALTER EXTENSION age UPDATE`) |
| Data | Separate disk `learningfocused-pg-data` (20 GB pd-balanced), weekly snapshots kept 4 weeks |
| Backups | Nightly `pg_dump -Fc` at 03:30 UTC to `gs://learningfocused-corpus/pg_dumps/` (deleted after 30 days by a bucket lifecycle rule; 3 days also kept on the disk) |
| Access | Port 5432 from the default subnet only (Cloud Run Direct VPC egress); SSH and admin tunnels only through IAP |
| Roles | `learningfocused` (superuser: migrations and admin over IAP only) and `learningfocused_app` (no superuser; DML, graph reads/writes; what Cloud Run uses) |
| Secrets | `learningfocused-pg-password`, `learningfocused-pg-app-password`, `learningfocused-pg-reader-password` (random); `learningfocused-pg-dsn` (read-write app role, the daily job's `PG_DSN`); `learningfocused-pg-dsn-reader` (read-only `learningfocused_mcp`, max 15 connections, the MCP server's `PG_DSN`) |
| Logs | Container logs and VM metrics go to Cloud Logging/Monitoring (`google-logging-enabled` metadata) |

Approximate cost: e2-small ~$12–13/month, disk + snapshots ~$2–3/month, no external IP charge.

## Deploy

```bash
./scripts/deploy_postgres_vm.sh          # builds the image, creates the VM, disk, firewall, secrets (idempotent)

# Apply the schema and create the app role from your laptop through an IAP tunnel.
# Passwords go through libpq's PGPASSWORD, so they never appear in the command line or history.
gcloud compute start-iap-tunnel learningfocused-pg 5432 --local-host-port=localhost:6543 \
  --zone=us-west1-b --project=gen-lang-client-0304733057 &
# If the IAP tunnel hangs on "Testing if tunnel connection works", stop it (kill %1) so it frees
# port 6543, then forward the port over IAP SSH instead (run it once in the foreground first if
# gcloud still needs to create an SSH key):
#   gcloud compute ssh learningfocused-pg --zone=us-west1-b --project=gen-lang-client-0304733057 \
#     --tunnel-through-iap -- -N -L 6543:localhost:5432 &
export PGPASSWORD="$(gcloud secrets versions access latest --secret=learningfocused-pg-password --project=gen-lang-client-0304733057)"
export PG_APP_PASSWORD="$(gcloud secrets versions access latest --secret=learningfocused-pg-app-password --project=gen-lang-client-0304733057)"
export PG_READER_PASSWORD="$(gcloud secrets versions access latest --secret=learningfocused-pg-reader-password --project=gen-lang-client-0304733057)"
PG_DSN=postgresql://learningfocused@localhost:6543/learningfocused \
  uv run python -m src.database.pg_store migrate --app-role --reader-role
unset PGPASSWORD PG_APP_PASSWORD PG_READER_PASSWORD

./scripts/pg_backup.sh dump-now          # first backup; also proves the VM can write to the bucket
./scripts/pg_backup.sh restore-test      # gate: the GCS copy restores into a scratch database and Cypher works

Re-run `migrate --app-role --reader-role` after any admin-side graph import: AGE adds a table
per new label, the app role must own those tables to add more labels later, and the reader
role needs SELECT on them.
```

## Filling it: Chroma backfill, daily dual-write, Neo4j graph

**Vectors and text (from Chroma, no re-embedding).** Chroma already stores the `text-embedding-3-small` vectors, so the backfill copies them as they are:

```bash
# with the IAP tunnel and PGPASSWORD from "Deploy" above, on a machine with chroma_db/
PG_DSN=postgresql://learningfocused@localhost:6543/learningfocused \
  uv run python -m src.database.pg_sync            # add --prune to delete chunks no longer in Chroma
```

It is idempotent: each page of Chroma rows is compared with Postgres first (text, metadata, document), and vectors are read from Chroma only for new or changed chunks. Each chunk keeps its Chroma id and `type`. `documents` gets one row per podcast episode (`podcast:<episode id>_<title hash>`, the same key the Chroma ids use, because the feed reuses episode numbers; the bare `episode_id` is in `documents.metadata`), article or video (`doc_id`), or podcast series (`series:<group title>`), with `published_at` parsed from RSS, ISO or YouTube dates. Placeholder ids and titles ("unknown", "Unknown Title") never become a document or replace a real title.

`--prune` deletes chunks that are no longer in Chroma (Chroma's `--prune-stale-chroma` runs remove rows after re-segmenting or id changes) and documents left with no chunks. It is skipped, and the command exits 1, when Chroma is empty or the prune would delete more than 10% of chunks (`--max-prune-fraction`), since that points to an incomplete Chroma copy rather than retired content.

**Daily dual-write.** Once the daily job has `PG_DSN`, it runs the same sync with prune right after the sources (ledger: `pg_state.chroma_to_pg`, whose stats include `prune_skipped` when the 10% guard held a prune back). A Postgres failure is recorded and never fails ingest. Postgres then follows Chroma, deletes included, until the MCP server reads Postgres directly.

**Graph (from the local Neo4j, any size).** Run on the Mac that has Neo4j, with the tunnel open:

```bash
NEO4J_URI=bolt://localhost:7687 NEO4J_USERNAME=neo4j NEO4J_PASSWORD=... \
PG_DSN=postgresql://learningfocused@localhost:6543/learningfocused \
  uv run python -m src.database.neo4j_to_age --grant-to learningfocused_app
```

Nodes and relationships are streamed and written in batches (`--batch-size`, default 1000), each batch in its own transaction. `age_import_map` records every copied Neo4j `elementId`, so an interrupted run is finished by running it again. Don't change the Neo4j graph between an interrupted run and its re-run: Neo4j can reuse ids of deleted items, and a reused id would be taken as already copied. The script prints Neo4j's counts next to what AGE holds and exits non-zero until they match; relationships whose end nodes were not imported are reported separately, since re-running can't fix those. `IngestedDoc` bookkeeping nodes (and their relationships) are skipped. Before writing, it checks that no two Neo4j labels or relationship types reduce to the same AGE name. Each node gets one AGE label (its first label other than `__Entity__`) and keeps all its Neo4j labels in a `labels` property; `embedding` properties are dropped unless `--keep-property embedding`. `--grant-to` hands the new label tables to the app role so it can keep adding to the graph.

Neither tool deletes anything in Chroma or Neo4j.

## Serving it

`src/database/pg_search.py` holds the read-only retrieval (keyword, vector, hybrid, graph templates). The MCP server uses it when deployed with `KB_BACKEND=postgres`; the tools, the eval comparison and the switch are in [mcp_server.md](mcp_server.md). Once `deploy_postgres_vm.sh` has created the DSN secret, `./scripts/deploy_ingest_job.sh` also gives the daily job `PG_DSN` and VPC egress (`INGEST_PG=0` opts out).

## Local development

Any Postgres 16 with `vector` and `age` installed works, e.g. the same image:

```bash
docker build -t lf-postgres deploy/postgres
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=dev lf-postgres
export PG_DSN=postgresql://postgres:dev@localhost:5432/postgres
uv run python -m src.database.pg_store migrate
uv run python -m src.database.pg_store status
```

Tests that need Postgres run when `PG_TEST_DSN` points at a server where the role can create databases:

```bash
PG_TEST_DSN=postgresql://postgres:dev@localhost:5432/postgres uv run pytest -q
```
