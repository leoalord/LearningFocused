#!/usr/bin/env bash
# Deploy the FastMCP HTTP server to Cloud Run (LearningFocus gen-lang-client-0304733057 / us-west1).
# Deploys to the LearningFocus project (same as GCP_PROJECT_ID in .env).
# Does not --reset-chroma, re-transcribe, or touch the podcast_downloads rsync.
#
# KB_BACKEND picks the store the server reads:
#   chroma (default)  hydrate the Chroma snapshot from GCS at boot; min 1 instance, 2Gi.
#   postgres          read the Postgres VM live over Direct VPC egress (PG_DSN from the
#                     learningfocused-pg-dsn secret, made by deploy_postgres_vm.sh); adds the
#                     keyword/vector/hybrid/graph tools; no hydrate, so it can scale to zero.
# Roll back to Chroma at any time with KB_BACKEND=chroma ./scripts/deploy_mcp.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

source "$(dirname "${BASH_SOURCE[0]}")/_gcp_env.sh"
SERVICE="${CLOUD_RUN_SERVICE:-learningfocused-mcp}"
SA_NAME="${CLOUD_RUN_SA:-learningfocused-mcp}"
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
AR_REPO="${AR_REPO:-learningfocused}"
SECRET_OPENAI="${SECRET_OPENAI:-learningfocused-openai-api-key}"
SECRET_GOOGLE="${SECRET_GOOGLE:-learningfocused-google-api-key}"
SECRET_ASSEMBLYAI="${SECRET_ASSEMBLYAI:-learningfocused-assemblyai-api-key}"
ENV_FILE="${ROOT}/.env"
UPDATE_SECRETS="${UPDATE_SECRETS:-0}"
KB_BACKEND="${KB_BACKEND:-chroma}"
KB_SEARCH_MODE="${KB_SEARCH_MODE:-vector}"
SECRET_DSN="${SECRET_PG_DSN:-learningfocused-pg-dsn}"
NETWORK="${PG_NETWORK:-default}"
SUBNET="${PG_SUBNET:-default}"

case "${KB_BACKEND}" in
  chroma)
    MIN_INSTANCES="${MCP_MIN_INSTANCES:-1}"
    MEMORY="${MCP_MEMORY:-2Gi}"
    ;;
  postgres)
    MIN_INSTANCES="${MCP_MIN_INSTANCES:-0}"
    MEMORY="${MCP_MEMORY:-1Gi}"
    ;;
  *)
    echo "KB_BACKEND must be chroma or postgres (got ${KB_BACKEND})" >&2
    exit 2
    ;;
esac
case "${KB_SEARCH_MODE}" in
  vector|keyword|hybrid) ;;
  *) echo "KB_SEARCH_MODE must be vector, keyword or hybrid (got ${KB_SEARCH_MODE})" >&2; exit 2 ;;
esac
if [[ "${KB_BACKEND}" == "chroma" && "${MIN_INSTANCES}" -lt 1 ]]; then
  echo "The chroma backend hydrates at boot and the daily roll refuses min-instances 0; use MCP_MIN_INSTANCES>=1." >&2
  exit 2
fi

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

RUNTIME_SECRETS=("${SECRET_OPENAI}" "${SECRET_GOOGLE}" "${SECRET_ASSEMBLYAI}")
if [[ "${KB_BACKEND}" == "postgres" ]]; then
  if ! gcloud secrets describe "${SECRET_DSN}" --project="${PROJECT}" >/dev/null 2>&1; then
    echo "Secret ${SECRET_DSN} not found. Run ./scripts/deploy_postgres_vm.sh first." >&2
    exit 1
  fi
  RUNTIME_SECRETS+=("${SECRET_DSN}")
fi

for secret in "${RUNTIME_SECRETS[@]}"; do
  echo ">> Granting secretAccessor on ${secret}"
  gcloud secrets add-iam-policy-binding "${secret}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor" \
    --project="${PROJECT}" >/dev/null
done

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
echo ">> Cloud Run --source builds as ${COMPUTE_SA} (project Editor). Runtime SA is ${SA_EMAIL}."

ENV_VARS="MCP_HOST=0.0.0.0,GCS_CHROMA_BUCKET=${BUCKET},GCP_BACKUP_PROJECT=${PROJECT},CHROMA_DIR=/data/chroma_db,MCP_RATE_LIMIT_PER_MINUTE=60,KB_BACKEND=${KB_BACKEND},KB_SEARCH_MODE=${KB_SEARCH_MODE}"
SECRETS="OPENAI_API_KEY=${SECRET_OPENAI}:latest,GOOGLE_API_KEY=${SECRET_GOOGLE}:latest,ASSEMBLYAI_API_KEY=${SECRET_ASSEMBLYAI}:latest"
if [[ "${KB_BACKEND}" == "postgres" ]]; then
  # Private IPs only go through the VPC (the Postgres VM has no external IP); the
  # OpenAI embedding calls still leave directly. Its firewall allows this subnet.
  ENV_VARS+=",HYDRATE_CHROMA=0"
  SECRETS+=",PG_DSN=${SECRET_DSN}:latest"
  BACKEND_FLAGS=(--network="${NETWORK}" --subnet="${SUBNET}" --vpc-egress=private-ranges-only --cpu-throttling)
else
  ENV_VARS+=",HYDRATE_CHROMA=1"
  # Chroma serves from instance memory/disk, so keep CPU always on; drop any VPC egress.
  BACKEND_FLAGS=(--clear-network --no-cpu-throttling)
fi

echo ">> Deploying ${SERVICE} to Cloud Run (${REGION}, backend ${KB_BACKEND}, min instances ${MIN_INSTANCES})"
gcloud run deploy "${SERVICE}" \
  --quiet \
  --source "${ROOT}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --allow-unauthenticated \
  --min-instances="${MIN_INSTANCES}" \
  --max-instances=3 \
  --memory="${MEMORY}" \
  --cpu=1 \
  --timeout=300 \
  --cpu-boost \
  "${BACKEND_FLAGS[@]}" \
  --port=8080 \
  --set-env-vars="${ENV_VARS}" \
  --set-secrets="${SECRETS}"

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
echo "Backend:  ${KB_BACKEND} (search mode ${KB_SEARCH_MODE})"
echo "Min inst: ${MIN_INSTANCES}"
echo "Secrets:  ${RUNTIME_SECRETS[*]}"
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
