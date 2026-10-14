# GCS corpus backup (TASK-15)

The gitignored corpus lives in **one private GCS bucket**. This is an off-laptop backup, not the MCP endpoint. Not Hugging Face. Not Chroma Cloud.

## Bucket

| | |
|---|---|
| Bucket | `gs://learningfocused-corpus` |
| Project | LearningFocus (`gen-lang-client-0304733057`) |
| Location | `us-west1` |
| Access | Uniform bucket-level access, public access prevention **enforced**, no public objects |
| Storage class | Standard |

Everything for LearningFocused lives in the LearningFocus project (`gen-lang-client-0304733057`, the same project as `.env` `GCP_PROJECT_ID`). Do **not** put LearningFocused resources in `inferpoker`; that project belongs to InferPoker. The corpus was first created in `gs://inferpoker-learningfocused`; `./scripts/migrate_to_learningfocus.sh` copies it here.

## Moving from inferpoker (one time)

```bash
./scripts/migrate_to_learningfocus.sh   # creates gs://learningfocused-corpus, copies every object from gs://inferpoker-learningfocused
./scripts/deploy_mcp.sh                 # new MCP service in LearningFocus
./scripts/deploy_ingest_job.sh          # new daily job + scheduler in LearningFocus
```

The script pauses the old daily scheduler (reversible) so the two buckets can't drift, then only copies; it never deletes anything in `inferpoker`. It prints the cleanup commands to run once the new setup is verified.

Before running it, remove any `GCP_BACKUP_PROJECT=inferpoker`, `GCS_CORPUS_BUCKET=inferpoker-learningfocused` or `GCS_CHROMA_BUCKET=inferpoker-learningfocused` from your shell and `.env`. The scripts refuse those values (`scripts/_gcp_env.sh`); the Python code prefers `GCS_CHROMA_BUCKET` over `GCS_CORPUS_BUCKET`, so a stale value there would win silently.

`deploy_mcp.sh` builds with `--source`, which runs as the project's default compute service account. In a new project that account may lack the roles it needs (if the org disables automatic IAM grants); if the build fails with a permission error, grant it `roles/cloudbuild.builds.builder`.

## Prefixes (local dir name = object prefix)

| Prefix | Local dir | Role |
|---|---|---|
| `chroma_db/` | `chroma_db/` | PersistentClient snapshot (`chroma.sqlite3` + collection binaries) |
| `transcripts/` | `transcripts/` | Derived transcripts |
| `segmented_transcripts/` | `segmented_transcripts/` | Segmented transcripts |
| `combined_summaries/` | `combined_summaries/` | Episode summaries |
| `substack_articles/` | `substack_articles/` | Substack article text |
| `article_summaries/` | `article_summaries/` | Article summaries |
| `youtube_videos/` | `youtube_videos/` | YouTube ingest text |
| `youtube_summaries/` | `youtube_summaries/` | YouTube summaries |
| `podcast_downloads/` | `podcast_downloads/` | Source audio (MP3) |
| `ingest_ledger/` | `ingest_ledger/` | Daily ingest run ledger (TASK-19) |

Not uploaded: `neo4j/`, `.env`, `.venv`, `.git`, `*.pem`, `metadata_output/` (optional; not required to recover transcripts/summaries/chroma).

## Backup (this laptop)

Keep the Mac awake (`caffeinate`). Prefer checksum rsync:

```bash
# Session-level: prevent idle sleep while uploads run
caffeinate -dims &

# chroma_db + derived artifacts (wait until complete)
./scripts/gcs_backup.sh artifacts

# audio (~6GB): start in background; do not block other work
./scripts/gcs_backup.sh audio --bg
# PID: /tmp/learningfocused-gcs-podcast-rsync.pid
# log: /tmp/learningfocused-gcs-podcast-rsync.log
```

Equivalent raw commands:

```bash
PROJECT=gen-lang-client-0304733057
BUCKET=learningfocused-corpus
EXCLUDE='(^|/)\.DS_Store$'

gcloud storage rsync ./chroma_db gs://${BUCKET}/chroma_db \
  --recursive --checksums-only --exclude="${EXCLUDE}" --project="${PROJECT}"

# same for transcripts, segmented_transcripts, combined_summaries,
# substack_articles, article_summaries, youtube_videos, youtube_summaries
```

## Restore without this laptop

On a new machine with `gcloud` authenticated to LearningFocus (`gen-lang-client-0304733057`):

```bash
git clone git@github.com:leoalord/LearningFocused.git
cd LearningFocused
uv sync

# Restore the Chroma PersistentClient store (do not --reset-chroma, do not wipe Neo4j)
./scripts/gcs_backup.sh restore-chroma

# Restore derived text artifacts
./scripts/gcs_backup.sh restore-artifacts

# Optional: restore audio
gcloud storage rsync gs://learningfocused-corpus/podcast_downloads ./podcast_downloads \
  --recursive --checksums-only --exclude='(^|/)\.DS_Store$' --project=gen-lang-client-0304733057
```

After `restore-chroma`, local code can open `chroma_db/` with the existing `PersistentClient` / `get_vector_store()` path. No Chroma Cloud. No Hugging Face.

## Verify

`gcloud storage ls --recursive` prints prefix headers (`gs://bucket/dir:`) that inflate `wc -l`. Count objects:

```bash
# object count (strip `ls` prefix headers that end in ':')
gcloud storage ls --recursive --project=gen-lang-client-0304733057 gs://learningfocused-corpus/chroma_db | grep -v ':$' | wc -l
gcloud storage du --summarize --readable-sizes gs://learningfocused-corpus
```

Uploads set `CLOUDSDK_STORAGE_PARALLEL_COMPOSITE_UPLOAD_ENABLED=False` so `chroma.sqlite3` is a normal object (MD5), not a composite.
