#!/usr/bin/env bash
# Backup (or restore) the gitignored LearningFocused corpus to ONE private GCS bucket.
# Independent of MCP serve. Does not touch neo4j/, .env, .venv, .git, or *.pem.
set -euo pipefail

PROJECT="${GCP_BACKUP_PROJECT:-inferpoker}"
BUCKET="${GCS_CORPUS_BUCKET:-inferpoker-learningfocused}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXCLUDE='(^|/)\.DS_Store$'
# Keep objects non-composite so restore is not gcloud-crc32c-only.
export CLOUDSDK_STORAGE_PARALLEL_COMPOSITE_UPLOAD_ENABLED="${CLOUDSDK_STORAGE_PARALLEL_COMPOSITE_UPLOAD_ENABLED:-False}"

# Prefixes inside gs://$BUCKET (same names as local dirs).
FULL_PREFIXES=(
  chroma_db
  transcripts
  segmented_transcripts
  combined_summaries
  substack_articles
  article_summaries
  youtube_videos
  youtube_summaries
)
AUDIO_PREFIX=podcast_downloads

usage() {
  cat <<USAGE
Usage: $(basename "$0") <artifacts|audio|all|restore-chroma|restore-artifacts>

  artifacts         rsync chroma_db + derived text dirs (wait until done)
  audio             rsync podcast_downloads (large; use --bg to background)
  all               artifacts then audio
  restore-chroma    pull chroma_db/ snapshot into ./chroma_db
  restore-artifacts pull derived dirs (not audio)

Flags:
  --bg              for audio: nohup + caffeinate in background
  --dry-run         pass --dry-run to gcloud storage rsync

Env:
  GCS_CORPUS_BUCKET   default ${BUCKET}
  GCP_BACKUP_PROJECT  default ${PROJECT}
USAGE
}

DRY_RUN=0
BG=0
MODE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --bg) BG=1; shift ;;
    -h|--help) usage; exit 0 ;;
    artifacts|audio|all|restore-chroma|restore-artifacts)
      MODE="$1"; shift ;;
    *) echo "Unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -z "${MODE}" ]]; then
  usage
  exit 2
fi

rsync_flags=(--recursive --checksums-only --exclude="${EXCLUDE}" --project="${PROJECT}")
if [[ "${DRY_RUN}" -eq 1 ]]; then
  rsync_flags+=(--dry-run)
fi

rsync_up() {
  local prefix="$1"
  local src="${ROOT}/${prefix}"
  local dest="gs://${BUCKET}/${prefix}"
  if [[ ! -d "${src}" ]]; then
    echo "SKIP missing local dir: ${src}" >&2
    return 0
  fi
  echo ">> rsync ${src} -> ${dest}"
  gcloud storage rsync "${src}" "${dest}" "${rsync_flags[@]}"
}

rsync_down() {
  local prefix="$1"
  local dest="${ROOT}/${prefix}"
  local src="gs://${BUCKET}/${prefix}"
  mkdir -p "${dest}"
  echo ">> rsync ${src} -> ${dest}"
  gcloud storage rsync "${src}" "${dest}" "${rsync_flags[@]}"
}

upload_artifacts() {
  for prefix in "${FULL_PREFIXES[@]}"; do
    rsync_up "${prefix}"
  done
}

upload_audio() {
  if [[ "${BG}" -eq 1 ]]; then
    local log="${GCS_AUDIO_LOG:-/tmp/learningfocused-gcs-podcast-rsync.log}"
    local pidfile="${GCS_AUDIO_PID:-/tmp/learningfocused-gcs-podcast-rsync.pid}"
    echo ">> background audio rsync -> ${log} (pidfile ${pidfile})"
    nohup caffeinate -s gcloud storage rsync \
      "${ROOT}/${AUDIO_PREFIX}" "gs://${BUCKET}/${AUDIO_PREFIX}" \
      "${rsync_flags[@]}" >"${log}" 2>&1 &
    echo $! >"${pidfile}"
    echo "audio rsync PID=$(cat "${pidfile}") log=${log}"
  else
    rsync_up "${AUDIO_PREFIX}"
  fi
}

case "${MODE}" in
  artifacts) upload_artifacts ;;
  audio) upload_audio ;;
  all)
    upload_artifacts
    upload_audio
    ;;
  restore-chroma) rsync_down chroma_db ;;
  restore-artifacts)
    for prefix in "${FULL_PREFIXES[@]}"; do
      [[ "${prefix}" == chroma_db ]] && continue
      rsync_down "${prefix}"
    done
    ;;
esac
