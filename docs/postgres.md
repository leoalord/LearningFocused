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
| Image | `us-west1-docker.pkg.dev/<project>/learningfocused/postgres:16-age-vector` (`deploy/postgres/Dockerfile`) |
| Data | Separate disk `learningfocused-pg-data` (20 GB pd-balanced), weekly snapshots kept 4 weeks |
| Backups | Nightly `pg_dump -Fc` at 03:30 UTC to `gs://learningfocused-corpus/pg_dumps/` (3 days also kept on the disk) |
| Access | Port 5432 from the default subnet only (Cloud Run Direct VPC egress); SSH and admin tunnels only through IAP |
| Secrets | `learningfocused-pg-password` (random), `learningfocused-pg-dsn` (full DSN, read by Cloud Run as `PG_DSN`) |

Approximate cost: e2-small ~$12–13/month, disk + snapshots ~$2–3/month, no external IP charge.

## Deploy

```bash
./scripts/deploy_postgres_vm.sh          # builds the image, creates the VM, disk, firewall, secrets (idempotent)

# Apply the schema from your laptop through an IAP tunnel
gcloud compute start-iap-tunnel learningfocused-pg 5432 --local-host-port=localhost:6543 \
  --zone=us-west1-b --project=gen-lang-client-0304733057 &
PG_DSN="postgresql://learningfocused:$(gcloud secrets versions access latest --secret=learningfocused-pg-password --project=gen-lang-client-0304733057)@localhost:6543/learningfocused" \
  uv run python -m src.database.pg_store migrate

./scripts/pg_backup.sh dump-now          # first backup
./scripts/pg_backup.sh restore-test      # gate: a dump restores into a scratch database
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
