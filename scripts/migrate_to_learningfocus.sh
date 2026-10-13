#!/usr/bin/env bash
# One-time move of the LearningFocused corpus from the old inferpoker bucket into
# the LearningFocus project (gen-lang-client-0304733057). Copies objects only:
# never deletes anything in inferpoker. Run before deploy_mcp.sh / deploy_ingest_job.sh.
set -euo pipefail

PROJECT="${GCP_BACKUP_PROJECT:-gen-lang-client-0304733057}"
REGION="${GCP_REGION:-us-west1}"
BUCKET="${GCS_CORPUS_BUCKET:-learningfocused-corpus}"
OLD_PROJECT="${OLD_GCP_PROJECT:-inferpoker}"
OLD_BUCKET="${OLD_GCS_CORPUS_BUCKET:-inferpoker-learningfocused}"
# Keep objects non-composite so restore is not gcloud-crc32c-only.
export CLOUDSDK_STORAGE_PARALLEL_COMPOSITE_UPLOAD_ENABLED=False

echo ">> Checking billing on ${PROJECT}"
if [[ "$(gcloud billing projects describe "${PROJECT}" --format='value(billingEnabled)')" != "True" ]]; then
  echo "Billing is not enabled on ${PROJECT}. Link a billing account first." >&2
  exit 1
fi

echo ">> Enabling Storage API on ${PROJECT}"
gcloud services enable storage.googleapis.com --project="${PROJECT}"

if ! gcloud storage buckets describe "gs://${BUCKET}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo ">> Creating private bucket gs://${BUCKET} (${REGION})"
  gcloud storage buckets create "gs://${BUCKET}" \
    --project="${PROJECT}" \
    --location="${REGION}" \
    --default-storage-class=STANDARD \
    --uniform-bucket-level-access \
    --public-access-prevention
fi

echo ">> Copying gs://${OLD_BUCKET} -> gs://${BUCKET} (checksum rsync, includes podcast_downloads and ingest_ledger)"
gcloud storage rsync "gs://${OLD_BUCKET}" "gs://${BUCKET}" \
  --recursive --checksums-only --project="${PROJECT}"

echo
echo "Copied. Next:"
echo "  ./scripts/deploy_mcp.sh          # seeds secrets from .env, deploys learningfocused-mcp"
echo "  ./scripts/deploy_ingest_job.sh   # daily job + scheduler (needs the MCP deploy first)"
echo
echo "Then point MCP clients at the new URL printed by deploy_mcp.sh."
echo "Only after the new setup is verified, retire the old resources in ${OLD_PROJECT}:"
echo "  gcloud scheduler jobs pause learningfocused-ingest-daily --location=${REGION} --project=${OLD_PROJECT}"
echo "  gcloud scheduler jobs delete learningfocused-ingest-daily --location=${REGION} --project=${OLD_PROJECT}"
echo "  gcloud run jobs delete learningfocused-ingest --region=${REGION} --project=${OLD_PROJECT}"
echo "  gcloud run services delete learningfocused-mcp --region=${REGION} --project=${OLD_PROJECT}"
echo "  gcloud storage rm -r gs://${OLD_BUCKET}"
echo "  gcloud projects remove-iam-policy-binding ${OLD_PROJECT} --member=serviceAccount:learningfocused-ingest@${OLD_PROJECT}.iam.gserviceaccount.com --role=roles/run.developer --condition=None"
echo "  gcloud iam service-accounts delete learningfocused-ingest@${OLD_PROJECT}.iam.gserviceaccount.com --project=${OLD_PROJECT}"
echo "  gcloud iam service-accounts delete learningfocused-mcp@${OLD_PROJECT}.iam.gserviceaccount.com --project=${OLD_PROJECT}"
echo "  gcloud secrets delete learningfocused-{openai,google,assemblyai}-api-key --project=${OLD_PROJECT}   # one at a time"
