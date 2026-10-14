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
| Secrets | `learningfocused-pg-password`, `learningfocused-pg-app-password` (random), `learningfocused-pg-dsn` (app-role DSN, read by Cloud Run as `PG_DSN`) |
| Logs | Container logs and VM metrics go to Cloud Logging/Monitoring (`google-logging-enabled` metadata) |

Approximate cost: e2-small ~$12–13/month, disk + snapshots ~$2–3/month, no external IP charge.

## Deploy

```bash
./scripts/deploy_postgres_vm.sh          # builds the image, creates the VM, disk, firewall, secrets (idempotent)

# Apply the schema and create the app role from your laptop through an IAP tunnel.
# Passwords go through libpq's PGPASSWORD, so they never appear in the command line or history.
gcloud compute start-iap-tunnel learningfocused-pg 5432 --local-host-port=localhost:6543 \
  --zone=us-west1-b --project=gen-lang-client-0304733057 &
export PGPASSWORD="$(gcloud secrets versions access latest --secret=learningfocused-pg-password --project=gen-lang-client-0304733057)"
export PG_APP_PASSWORD="$(gcloud secrets versions access latest --secret=learningfocused-pg-app-password --project=gen-lang-client-0304733057)"
PG_DSN=postgresql://learningfocused@localhost:6543/learningfocused \
  uv run python -m src.database.pg_store migrate --app-role
unset PGPASSWORD PG_APP_PASSWORD

./scripts/pg_backup.sh dump-now          # first backup; also proves the VM can write to the bucket
./scripts/pg_backup.sh restore-test      # gate: the GCS copy restores into a scratch database and Cypher works

Re-run `migrate --app-role` after any admin-side graph import: AGE adds a table per new label,
and the app role must own those tables to add more labels later.
```

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
