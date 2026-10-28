# Daily ingest (TASK-19)

One Cloud Run **Job** (not the public MCP service) checks every feed in [`src/feeds/sources.json`](feeds.md) that is due: Art19 RSS, Substack and the whole `@future_of_education` YouTube channel daily, social accounts once configured. Failures are isolated per source. Each source embeds its new and changed chunks straight into Postgres (`src/pipeline/index_pg.py`), where `learningfocused-mcp` reads them live, so the job needs `PG_DSN`. A run ledger is written every time. After ingest, the derived text artifacts are checksum-synced to `gs://learningfocused-corpus`. Chroma is no longer written; the old `chroma_db/` snapshot in the bucket is left as it was.

Does **not** re-transcribe existing files, take more than `max_new_per_run` new YouTube videos per run, ingest `@thealphaschool` / UU dumps / Shorts-tab dumps, wipe Neo4j, or touch the MCP service. Graph/Neo4j is skipped in the job (no Docker/Aura on Cloud Run).

Project is LearningFocus (`gen-lang-client-0304733057`, the scripts' default; `GCP_BACKUP_PROJECT` overrides it, see `scripts/_gcp_env.sh`) / `us-west1`. Do not deploy to `inferpoker`; that project belongs to InferPoker.

## Entrypoint

```bash
uv run python -m src.pipeline.daily_ingest
```

| Flag | Role |
| --- | --- |
| `--dry-run` | Walk the orchestrator; do not call pipelines or GCS |
| `--skip-hydrate` | Do not pull artifacts from GCS (laptop with a local corpus) |
| `--skip-sync` | Do not upload artifacts |
| `--source KEY` | Run only this registry key, due or not (repeatable) |
| `--skip KEY` | Skip a registry key (repeatable) |
| `--skip-art19` / `--skip-substack` / `--skip-youtube` | Shorthands for `--skip` |

Isolation: each source is wrapped in `try/except`. Art19 raising does not skip Substack or YouTube. The process still writes the ledger and attempts the GCS sync.

Wrapped runners (Neo4j always skipped; `--force` off). Limits come from each source's `config` in `sources.json`; a bad value fails only that source.

- `podcast_rss` (Art19): `python -m src.pipeline.audio.run --skip-neo4j -- --mode daily --download-limit <download_limit>` (`DAILY_ART19_DOWNLOAD_LIMIT` env overrides)
- `substack`: `python -m src.pipeline.substack.run --skip-neo4j -- --mode daily --ingest-limit <ingest_limit>`
- `youtube_channel`: `python -m src.pipeline.youtube.run --feed --max-new <max_new_per_run>` (videos tab + the two playlists, deduplicated against podcast episodes by `skip.py`; any failed video fails this source, and a video that fails 3 runs is set aside, see [feeds.md](feeds.md))
- `instagram` / `x` / `linkedin` / `tiktok`: `src/feeds/social.py` (skipped until accounts and a credential are set)

If `sources.json` can't be parsed, the job logs the error and runs the three core feeds anyway.

Audio download skips an MP3 when the matching transcript already exists, so the job can hydrate transcripts (~9 MiB) instead of `podcast_downloads/` (~6 GiB). A **new** RSS episode has no transcript, so it is downloaded and sent to AssemblyAI.

## Ledger

Every run writes:

- local `ingest_ledger/<run_id>.json` and `ingest_ledger/latest.json`
- `gs://learningfocused-corpus/ingest_ledger/<run_id>.json`
- `gs://learningfocused-corpus/ingest_ledger/latest.json`

Fields: `started_at` / `finished_at`, per-source `ok` / `error`, hydrate counts, GCS upload stats, `pg_state`.

The run also mirrors `sources.json` into the `sources` table, reads each source's last check (for weekly cadence), and writes `ingest_runs` + `source_checks`. A bookkeeping error is recorded under `pg_state` and never fails the run. Indexing is part of each source: if Postgres is unreachable, that source fails, and the next run catches up (every run compares the whole on-disk corpus with Postgres; unchanged chunks are not re-embedded).

## Cloud Run Job

| | |
| --- | --- |
| Job | `learningfocused-ingest` |
| Region | `us-west1` |
| Runtime SA | `learningfocused-ingest@gen-lang-client-0304733057.iam.gserviceaccount.com` |
| Memory / CPU | 8 Gi / 2 |
| Task timeout | 4 hours (new episode = AssemblyAI + embeddings) |
| Auth | Not public. Scheduler OAuth / `roles/run.invoker` only |
| Image command | `/app/scripts/job_entrypoint.sh` → `python -m src.pipeline.daily_ingest` |

The public MCP service (`learningfocused-mcp`, `allUsers` invoker, `min-instances=1`) is unchanged by this deploy script. The job uses a **separate** SA with bucket **objectAdmin** so the public MCP SA can stay objectViewer.

```bash
./scripts/deploy_ingest_job.sh
EXECUTE_NOW=1 ./scripts/deploy_ingest_job.sh   # also execute once
```

Hydrate on each job start (not audio; the old `chroma_db/` snapshot is no longer synced, since chunks live in Postgres):

- `transcripts/`, `segmented_transcripts/`, `combined_summaries/`
- `substack_articles/`, `article_summaries/`
- `youtube_videos/`, `youtube_summaries/`

Then pipelines run with skip-existing, indexing new chunks straight into Postgres (each pipeline first checks that Postgres answers and the login can write, so nothing is downloaded or transcribed when it can't be indexed). Then checksum upload of those prefixes. No MCP roll: the MCP service reads Postgres live.

## Cloud Scheduler

| | |
| --- | --- |
| Name | `learningfocused-ingest-daily` |
| Schedule | `0 8 * * *` |
| Timezone | `America/Los_Angeles` (08:00 Pacific) |
| Target | `POST https://run.googleapis.com/v2/projects/gen-lang-client-0304733057/locations/us-west1/jobs/learningfocused-ingest:run` |
| Auth | OAuth as `learningfocused-ingest@…` |
| State | `ENABLED` |

The scheduler only *starts* the job (deadline 180s). The job itself may run for hours.

## MCP service

The job no longer rolls `learningfocused-mcp`: the service reads Postgres live, so new chunks are searchable as soon as a source commits them. `./scripts/deploy_ingest_job.sh` no longer grants the ingest SA `roles/run.developer`, Artifact Registry reader or actAs on the MCP SA, which only the roll needed; bindings granted by earlier deploys stay until removed by hand.

## “New RSS episode without a laptop”

The job cannot wait for S E357. Structural proof:

1. Art19 `download.py` skips existing transcripts and only downloads titles that are not already transcribed.
2. Cloud Run Job executions walk all three sources with skip-existing (no `--force`, no 6 GiB audio pull). Scheduler run `learningfocused-ingest-75gz8` was created at 08:00 America/Los_Angeles by `learningfocused-ingest@` (not a laptop `gcloud`).
3. Scheduler `learningfocused-ingest-daily` is ENABLED, `0 8 * * *` America/Los_Angeles, targeting the job `:run` API.
4. (Proof from the original `inferpoker` deployment, before the move to LearningFocus.) After Artifact Registry reader was applied on `cloud-run-source-deploy`, job execution `learningfocused-ingest-ctwxh` (runtime SA `learningfocused-ingest@`) wrote `mcp_roll.ok: true` and minted revision `learningfocused-mcp-00004-lhp` with `serving.knative.dev/creator: learningfocused-ingest@inferpoker.iam.gserviceaccount.com` and `minScale=1`. Public `/health` 200 and unauthenticated `/mcp` still work.

When E357 (or any new Art19 item) appears in the RSS feed, the next 08:00 Pacific run downloads that one MP3, transcribes it, and embeds its chunks into Postgres, where MCP can search them — no laptop session required.

## Local

Keep the Mac awake if you hydrate/ingest locally (`caffeinate`). Prefer the Cloud Run Job for the daily path.

```bash
uv run python -m src.pipeline.daily_ingest --dry-run --skip-hydrate --skip-sync
uv run python -m src.tests.test_daily_ingest
```
