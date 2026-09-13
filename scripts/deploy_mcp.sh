#!/usr/bin/env bash
# Deploy the FastMCP HTTP server to Cloud Run (inferpoker / us-west1).
# Does not deploy to GCP_PROJECT_ID from .env (that is a different project).
# Does not --reset-chroma, re-transcribe, or touch the podcast_downloads rsync.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

PROJECT="${GCP_BACKUP_PROJECT:-inferpoker}"
REGION="${GCP_REGION:-us-west1}"
SERVICE="${CLOUD_RUN_SERVICE:-learningfocused-mcp}"
SA_NAME="${CLOUD_RUN_SA:-learningfocused-mcp}"
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
BUCKET="${GCS_CORPUS_BUCKET:-inferpoker-learningfocused}"
AR_REPO="${AR_REPO:-learningfocused}"
SECRET_OPENAI="${SECRET_OPENAI:-learningfocused-openai-api-key}"
SECRET_GOOGLE="${SECRET_GOOGLE:-learningfocused-google-api-key}"
SECRET_ASSEMBLYAI="${SECRET_ASSEMBLYAI:-learningfocused-assemblyai-api-key}"
ENV_FILE="${ROOT}/.env"
UPDATE_SECRETS="${UPDATE_SECRETS:-0}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE} (needed to seed Secret Manager). Not committed." >&2
  exit 2
fi

gcloud config set project "${PROJECT}" >/dev/null

echo ">> Enabling APIs on ${PROJECT}"
gcloud services enable \
  run.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  storage.googleapis.com \
  --project="${PROJECT}"

if ! gcloud artifacts repositories describe "${AR_REPO}" --location="${REGION}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo ">> Creating Artifact Registry ${AR_REPO}"
  gcloud artifacts repositories create "${AR_REPO}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="LearningFocused MCP" \
    --project="${PROJECT}"
fi

if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo ">> Creating service account ${SA_EMAIL}"
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="LearningFocused MCP Cloud Run" \
    --project="${PROJECT}"
fi

echo ">> Granting ${SA_EMAIL} objectViewer on gs://${BUCKET}"
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.objectViewer" \
  --project="${PROJECT}" >/dev/null

read_env_value() {
  local key="$1"
  uv run python - "$ENV_FILE" "$key" <<'PY'
import sys
from pathlib import Path
from dotenv import dotenv_values
path, key = sys.argv[1], sys.argv[2]
vals = dotenv_values(path)
value = vals.get(key)
if not value:
    raise SystemExit(f"Missing {key} in {path}")
sys.stdout.write(value)
PY
}

ensure_secret() {
  local name="$1"
  local env_key="$2"
  if gcloud secrets describe "${name}" --project="${PROJECT}" >/dev/null 2>&1; then
    if [[ "${UPDATE_SECRETS}" != "1" ]]; then
      echo ">> Secret ${name} exists (set UPDATE_SECRETS=1 to add a version)"
      return 0
    fi
  else
    echo ">> Creating secret ${name}"
    gcloud secrets create "${name}" --replication-policy=automatic --project="${PROJECT}"
  fi
  echo ">> Adding secret version ${name} from .env ${env_key}"
  read_env_value "${env_key}" | gcloud secrets versions add "${name}" --data-file=- --project="${PROJECT}" >/dev/null
}

ensure_secret "${SECRET_OPENAI}" "OPENAI_API_KEY"
ensure_secret "${SECRET_GOOGLE}" "GOOGLE_API_KEY"
ensure_secret "${SECRET_ASSEMBLYAI}" "ASSEMBLYAI_API_KEY"

for secret in "${SECRET_OPENAI}" "${SECRET_GOOGLE}" "${SECRET_ASSEMBLYAI}"; do
  echo ">> Granting secretAccessor on ${secret}"
  gcloud secrets add-iam-policy-binding "${secret}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor" \
    --project="${PROJECT}" >/dev/null
done

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
echo ">> Cloud Run --source builds as ${COMPUTE_SA} (project Editor). Runtime SA is ${SA_EMAIL}."

echo ">> Deploying ${SERVICE} to Cloud Run (${REGION})"
gcloud run deploy "${SERVICE}" \
  --quiet \
  --source "${ROOT}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --allow-unauthenticated \
  --min-instances=1 \
  --max-instances=3 \
  --memory=2Gi \
  --cpu=1 \
  --timeout=300 \
  --cpu-boost \
  --no-cpu-throttling \
  --port=8080 \
  --set-env-vars="MCP_HOST=0.0.0.0,HYDRATE_CHROMA=1,GCS_CHROMA_BUCKET=${BUCKET},GCP_BACKUP_PROJECT=${PROJECT},CHROMA_DIR=/data/chroma_db,MCP_RATE_LIMIT_PER_MINUTE=60" \
  --set-secrets="OPENAI_API_KEY=${SECRET_OPENAI}:latest,GOOGLE_API_KEY=${SECRET_GOOGLE}:latest,ASSEMBLYAI_API_KEY=${SECRET_ASSEMBLYAI}:latest"

echo ">> Ensuring allUsers can invoke (public MCP)"
gcloud run services add-iam-policy-binding "${SERVICE}" \
  --region="${REGION}" \
  --member="allUsers" \
  --role="roles/run.invoker" \
  --project="${PROJECT}" >/dev/null

URL="$(gcloud run services describe "${SERVICE}" --region="${REGION}" --project="${PROJECT}" --format='value(status.url)')"
echo
echo "Service:  ${SERVICE}"
echo "Region:   ${REGION}"
echo "URL:      ${URL}"
echo "MCP:      ${URL}/mcp"
echo "Health:   ${URL}/health"
echo "Auth:     allUsers (unauthenticated)"
echo "Min inst: 1"
echo "Secrets:  ${SECRET_OPENAI}, ${SECRET_GOOGLE}, ${SECRET_ASSEMBLYAI}"
echo
echo "Cursor mcp.json:"
cat <<EOF
{
  "mcpServers": {
    "learningfocused": {
      "url": "${URL}/mcp"
    }
  }
}
EOF
