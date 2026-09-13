#!/usr/bin/env bash
# Deploy the daily ingest Cloud Run Job + Cloud Scheduler (inferpoker / us-west1).
# Separate from the public MCP service. Does not deploy to GCP_PROJECT_ID from .env.
# Does not --reset-chroma, re-transcribe, wipe Neo4j, or scale MCP to zero.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

PROJECT="${GCP_BACKUP_PROJECT:-inferpoker}"
REGION="${GCP_REGION:-us-west1}"
JOB="${CLOUD_RUN_JOB:-learningfocused-ingest}"
SCHEDULER_JOB="${CLOUD_SCHEDULER_JOB:-learningfocused-ingest-daily}"
SCHEDULER_SCHEDULE="${CLOUD_SCHEDULER_SCHEDULE:-0 8 * * *}"
SCHEDULER_TZ="${CLOUD_SCHEDULER_TZ:-America/Los_Angeles}"
SA_NAME="${INGEST_SA:-learningfocused-ingest}"
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
MCP_SA="${CLOUD_RUN_SA:-learningfocused-mcp}@${PROJECT}.iam.gserviceaccount.com"
MCP_SERVICE="${MCP_SERVICE:-learningfocused-mcp}"
BUCKET="${GCS_CORPUS_BUCKET:-inferpoker-learningfocused}"
SECRET_OPENAI="${SECRET_OPENAI:-learningfocused-openai-api-key}"
SECRET_GOOGLE="${SECRET_GOOGLE:-learningfocused-google-api-key}"
SECRET_ASSEMBLYAI="${SECRET_ASSEMBLYAI:-learningfocused-assemblyai-api-key}"
EXECUTE_NOW="${EXECUTE_NOW:-0}"

gcloud config set project "${PROJECT}" >/dev/null

echo ">> Enabling APIs on ${PROJECT}"
gcloud services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  storage.googleapis.com \
  iam.googleapis.com \
  --project="${PROJECT}"

if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo ">> Creating service account ${SA_EMAIL}"
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="LearningFocused daily ingest Cloud Run Job" \
    --project="${PROJECT}"
  echo ">> Waiting for ${SA_EMAIL} to propagate"
  for _ in 1 2 3 4 5 6; do
    if gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT}" >/dev/null 2>&1; then
      break
    fi
    sleep 5
  done
  sleep 8
fi

echo ">> Granting ${SA_EMAIL} objectAdmin on gs://${BUCKET} (job writes artifacts/chroma/ledger)"
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.objectAdmin" \
  --project="${PROJECT}" >/dev/null

for secret in "${SECRET_OPENAI}" "${SECRET_GOOGLE}" "${SECRET_ASSEMBLYAI}"; do
  echo ">> Granting secretAccessor on ${secret}"
  ok=0
  for _ in 1 2 3 4 5 6; do
    if gcloud secrets add-iam-policy-binding "${secret}" \
      --member="serviceAccount:${SA_EMAIL}" \
      --role="roles/secretmanager.secretAccessor" \
      --project="${PROJECT}" >/dev/null; then
      ok=1
      break
    fi
    echo "   retrying secret IAM (SA propagation)..."
    sleep 5
  done
  if [[ "${ok}" -ne 1 ]]; then
    echo "Failed to grant secretAccessor on ${secret}" >&2
    exit 1
  fi
done

echo ">> Granting ${SA_EMAIL} run.developer (roll ${MCP_SERVICE}, do not scale to zero)"
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.developer" \
  --condition=None \
  >/dev/null

AR_REPO_SOURCE="${AR_REPO_SOURCE:-cloud-run-source-deploy}"
echo ">> Granting ${SA_EMAIL} artifactregistry.reader on ${AR_REPO_SOURCE} (needed to mint a new MCP revision)"
gcloud artifacts repositories add-iam-policy-binding "${AR_REPO_SOURCE}" \
  --location="${REGION}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/artifactregistry.reader" \
  --project="${PROJECT}" \
  >/dev/null

echo ">> Granting ${SA_EMAIL} actAs on MCP runtime SA ${MCP_SA}"
gcloud iam service-accounts add-iam-policy-binding "${MCP_SA}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountUser" \
  --project="${PROJECT}" >/dev/null

echo ">> Deploying Cloud Run Job ${JOB} (${REGION})"
gcloud run jobs deploy "${JOB}" \
  --quiet \
  --source "${ROOT}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --command=/app/scripts/job_entrypoint.sh \
  --memory=8Gi \
  --cpu=2 \
  --task-timeout=4h \
  --max-retries=1 \
  --parallelism=1 \
  --tasks=1 \
  --set-env-vars="GCP_BACKUP_PROJECT=${PROJECT},GCS_CORPUS_BUCKET=${BUCKET},GCS_CHROMA_BUCKET=${BUCKET},MCP_SERVICE=${MCP_SERVICE},MCP_REGION=${REGION},HYDRATE_CHROMA=0,CHROMA_DIR=/app/chroma_db,DAILY_ART19_DOWNLOAD_LIMIT=5" \
  --set-secrets="OPENAI_API_KEY=${SECRET_OPENAI}:latest,GOOGLE_API_KEY=${SECRET_GOOGLE}:latest,ASSEMBLYAI_API_KEY=${SECRET_ASSEMBLYAI}:latest"

echo ">> Job is not public (no allUsers). Granting scheduler invoker on the job."
gcloud run jobs add-iam-policy-binding "${JOB}" \
  --region="${REGION}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.invoker" \
  --project="${PROJECT}" >/dev/null

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
SCHEDULER_AGENT="service-${PROJECT_NUMBER}@gcp-sa-cloudscheduler.iam.gserviceaccount.com"
echo ">> Cloud Scheduler agent actAs on ${SA_EMAIL}"
gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
  --member="serviceAccount:${SCHEDULER_AGENT}" \
  --role="roles/iam.serviceAccountUser" \
  --project="${PROJECT}" >/dev/null || true

URI="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB}:run"
SCHEDULER_COMMON=(
  --location="${REGION}"
  --schedule="${SCHEDULER_SCHEDULE}"
  --time-zone="${SCHEDULER_TZ}"
  --uri="${URI}"
  --http-method=POST
  --oauth-service-account-email="${SA_EMAIL}"
  --attempt-deadline=180s
  --project="${PROJECT}"
)

if gcloud scheduler jobs describe "${SCHEDULER_JOB}" --location="${REGION}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo ">> Updating Cloud Scheduler job ${SCHEDULER_JOB}"
  gcloud scheduler jobs update http "${SCHEDULER_JOB}" "${SCHEDULER_COMMON[@]}"
else
  echo ">> Creating Cloud Scheduler job ${SCHEDULER_JOB}"
  gcloud scheduler jobs create http "${SCHEDULER_JOB}" \
    "${SCHEDULER_COMMON[@]}" \
    --description="Daily LearningFocused ingest (Art19 + Substack + unique YouTube)"
fi

echo ">> Ensuring scheduler is ENABLED"
gcloud scheduler jobs resume "${SCHEDULER_JOB}" --location="${REGION}" --project="${PROJECT}" >/dev/null 2>&1 || true

echo
echo "Job:       ${JOB}"
echo "Region:    ${REGION}"
echo "SA:        ${SA_EMAIL}"
echo "Scheduler: ${SCHEDULER_JOB}"
echo "Schedule:  ${SCHEDULER_SCHEDULE} (${SCHEDULER_TZ})"
echo "Target:    ${URI}"
echo "MCP roll:  ${MCP_SERVICE} (min instances stay 1)"
echo
echo "Does not pull gs://${BUCKET}/podcast_downloads (6GB). Skip-existing uses hydrated transcripts."
echo "A new RSS episode is downloaded + transcribed; existing files are not re-transcribed."
echo

if [[ "${EXECUTE_NOW}" == "1" ]]; then
  echo ">> Executing ${JOB} once (skip-existing; no --reset-chroma)"
  gcloud run jobs execute "${JOB}" --region="${REGION}" --project="${PROJECT}" --wait
fi

echo "Describe scheduler:"
gcloud scheduler jobs describe "${SCHEDULER_JOB}" --location="${REGION}" --project="${PROJECT}" \
  --format='yaml(name,schedule,timeZone,state,httpTarget.uri,httpTarget.httpMethod,status)'
