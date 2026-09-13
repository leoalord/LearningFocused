# Daily ingest (TASK-19)

One Cloud Run **Job** (not the public MCP service) walks Art19 RSS, Substack, and unique YouTube every day. Failures are isolated per source. A run ledger is written every time. After ingest, derived artifacts + `chroma_db/` are checksum-synced to `gs://inferpoker-learningfocused`, then `learningfocused-mcp` is rolled so warm instances rehydrate.

Does **not** `--reset-chroma`, re-transcribe existing files, raise the YouTube V1 cap, ingest `@thealphaschool` / UU dumps / Shorts-tab dumps, wipe Neo4j, or scale the MCP service to zero. Graph/Neo4j is skipped in the job (no Docker/Aura on Cloud Run).

Project is **inferpoker** / `us-west1`. Do not deploy to `.env` `GCP_PROJECT_ID=gen-lang-client-0304733057`.

## Entrypoint

```bash
uv run python -m src.pipeline.daily_ingest
```

| Flag | Role |
| --- | --- |
| `--dry-run` | Walk the orchestrator; do not call pipelines, GCS, or MCP roll |
| `--skip-hydrate` | Do not pull chroma/artifacts from GCS (laptop with a local corpus) |
| `--skip-sync` | Do not upload artifacts/chroma |
| `--skip-roll` | Do not roll `learningfocused-mcp` |
| `--skip-art19` / `--skip-substack` / `--skip-youtube` | Drop a source |

Isolation: each source is wrapped in `try/except`. Art19 raising does not skip Substack or YouTube. The process still writes the ledger and attempts GCS sync + MCP roll.

Wrapped runners (Neo4j always skipped; `--force` off; `--reset-chroma` off):

- Art19: `python -m src.pipeline.audio.run --skip-neo4j -- --mode daily --download-limit 5`
- Substack: `python -m src.pipeline.substack.run --skip-neo4j -- --mode daily --ingest-limit 10`
- YouTube: `python -m src.pipeline.youtube.run` (existing `skip.py` + V1 cap)

Audio download skips an MP3 when the matching transcript already exists, so the job can hydrate transcripts (~9 MiB) instead of `podcast_downloads/` (~6 GiB). A **new** RSS episode has no transcript, so it is downloaded and sent to AssemblyAI.

## Ledger

Every run writes:

- local `ingest_ledger/<run_id>.json` and `ingest_ledger/latest.json`
- `gs://inferpoker-learningfocused/ingest_ledger/<run_id>.json`
- `gs://inferpoker-learningfocused/ingest_ledger/latest.json`

Fields: `started_at` / `finished_at`, per-source `ok` / `error`, hydrate counts, GCS upload stats, MCP revision after roll.

## Cloud Run Job

| | |
| --- | --- |
| Job | `learningfocused-ingest` |
| Region | `us-west1` |
| Runtime SA | `learningfocused-ingest@inferpoker.iam.gserviceaccount.com` |
| Memory / CPU | 8 Gi / 2 |
| Task timeout | 4 hours (new episode = AssemblyAI + embeddings) |
| Auth | Not public. Scheduler OAuth / `roles/run.invoker` only |
| Image command | `/app/scripts/job_entrypoint.sh` → `python -m src.pipeline.daily_ingest` |

The public MCP service (`learningfocused-mcp`, `allUsers` invoker, `min-instances=1`) is unchanged by this deploy script. The job uses a **separate** SA with bucket **objectAdmin** so the public MCP SA can stay objectViewer.

```bash
./scripts/deploy_ingest_job.sh
EXECUTE_NOW=1 ./scripts/deploy_ingest_job.sh   # also execute once
```

Hydrate on each job start (not audio):

- `chroma_db/` (~267 MiB)
- `transcripts/`, `segmented_transcripts/`, `combined_summaries/`
- `substack_articles/`, `article_summaries/`
- `youtube_videos/`, `youtube_summaries/`

Then pipelines run with skip-existing. Then checksum upload of those prefixes. Then MCP roll.

## Cloud Scheduler

| | |
| --- | --- |
| Name | `learningfocused-ingest-daily` |
| Schedule | `0 8 * * *` |
| Timezone | `America/Los_Angeles` (08:00 Pacific) |
| Target | `POST https://run.googleapis.com/v2/projects/inferpoker/locations/us-west1/jobs/learningfocused-ingest:run` |
| Auth | OAuth as `learningfocused-ingest@…` |
| State | `ENABLED` |

The scheduler only *starts* the job (deadline 180s). The job itself may run for hours.

## MCP roll

After a successful GCS sync the job PATCHes `learningfocused-mcp` with `CHROMA_SNAPSHOT_AT=<run_id>`. That creates a **new revision** of the existing image (no image rebuild, no `--min-instances=0`). New instances hydrate `gs://…/chroma_db/` onto `/data/chroma_db`. Old warm instances drain. Public `/mcp` stays unauthenticated.

The ingest SA also needs `roles/artifactregistry.reader` on `cloud-run-source-deploy` so Cloud Run can pull the current MCP image when minting that revision. `./scripts/deploy_ingest_job.sh` grants it.

## “New RSS episode without a laptop”

The job cannot wait for S E357. Structural proof:

1. Art19 `download.py` skips existing transcripts and only downloads titles that are not already transcribed.
2. Cloud Run Job executions walk all three sources with skip-existing (no `--force`, no `--reset-chroma`, no 6 GiB audio pull). Scheduler run `learningfocused-ingest-75gz8` was created at 08:00 America/Los_Angeles by `learningfocused-ingest@` (not a laptop `gcloud`).
3. Scheduler `learningfocused-ingest-daily` is ENABLED, `0 8 * * *` America/Los_Angeles, targeting the job `:run` API.
4. Ledger + GCS prefixes are on the success path. MCP roll from the job SA currently 403s without Artifact Registry reader; a matching env-var roll (`CHROMA_SNAPSHOT_AT`) produced revision `learningfocused-mcp-00002-g9x` with `minScale=1`.

When E357 (or any new Art19 item) appears in the RSS feed, the next 08:00 Pacific run downloads that one MP3, transcribes it, upserts Chroma, uploads the snapshot, and rolls MCP — no laptop session required (after the Artifact Registry reader grant).

## Local

Keep the Mac awake if you hydrate/ingest locally (`caffeinate`). Prefer the Cloud Run Job for the daily path.

```bash
uv run python -m src.pipeline.daily_ingest --dry-run --skip-hydrate --skip-sync --skip-roll
uv run python -m src.tests.test_daily_ingest
```
