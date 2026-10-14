#!/usr/bin/env bash
# One-time move of the LearningFocused corpus from the old inferpoker bucket into
# the LearningFocus project (gen-lang-client-0304733057). Copies objects only:
# never deletes anything in inferpoker. Run before deploy_mcp.sh / deploy_ingest_job.sh.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/_gcp_env.sh"
OLD_PROJECT="${OLD_GCP_PROJECT:-inferpoker}"
OLD_BUCKET="${OLD_GCS_CORPUS_BUCKET:-inferpoker-learningfocused}"
# Keep objects non-composite so restore is not gcloud-crc32c-only.
export CLOUDSDK_STORAGE_PARALLEL_COMPOSITE_UPLOAD_ENABLED=False

echo ">> Checking billing on ${PROJECT}"
billing="$(gcloud billing projects describe "${PROJECT}" --format='value(billingEnabled)')" || {
  echo "Cannot read billing for ${PROJECT} (Cloud Billing API off, or no billing permission)." >&2
  exit 1
}
if [[ "${billing}" != "True" ]]; then
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
else
  # An existing bucket must have the same protections a new one gets.
  read -r ubla pap < <(gcloud storage buckets describe "gs://${BUCKET}" --project="${PROJECT}" \
    --format='value(uniform_bucket_level_access,public_access_prevention)')
  if [[ "${ubla}" != "True" || "${pap}" != "enforced" ]]; then
    echo ">> Enforcing uniform access + public access prevention on existing gs://${BUCKET}"
    gcloud storage buckets update "gs://${BUCKET}" --project="${PROJECT}" \
      --uniform-bucket-level-access --public-access-prevention
  fi
fi

# Stop the old daily job first so nothing lands in the old bucket after the copy and
# the two corpora don't drift (or pay AssemblyAI twice). Pausing is reversible:
#   gcloud scheduler jobs resume learningfocused-ingest-daily --location=${REGION} --project=${OLD_PROJECT}
echo ">> Pausing the old daily scheduler in ${OLD_PROJECT}"
gcloud scheduler jobs pause learningfocused-ingest-daily --location="${REGION}" --project="${OLD_PROJECT}" \
  || echo "   (not found or already paused)"

# --project only sets the billing/quota project; reading the old bucket uses your own
# access to ${OLD_PROJECT}.
echo ">> Copying gs://${OLD_BUCKET} -> gs://${BUCKET} (checksum rsync, includes podcast_downloads and ingest_ledger)"
gcloud storage rsync "gs://${OLD_BUCKET}" "gs://${BUCKET}" \
  --recursive --checksums-only --project="${PROJECT}"

count_objects() {
  gcloud storage ls -r "gs://$1/**" --project="${PROJECT}" | grep -vc '/$' || true
}
old_count="$(count_objects "${OLD_BUCKET}")"
new_count="$(count_objects "${BUCKET}")"
echo ">> Objects: gs://${OLD_BUCKET}=${old_count} gs://${BUCKET}=${new_count}"
if (( new_count < old_count )); then
  echo "The copy is missing objects. Re-run this script; do not retire anything in ${OLD_PROJECT}." >&2
  exit 1
fi

echo
echo "Copied and verified. Next:"
echo "  ./scripts/deploy_mcp.sh          # seeds secrets from .env, deploys learningfocused-mcp"
echo "  ./scripts/deploy_ingest_job.sh   # daily job + scheduler (needs the MCP deploy first)"
echo
echo "Then point MCP clients at the new URL printed by deploy_mcp.sh."
echo "Only after the new setup is verified, retire the old resources in ${OLD_PROJECT}:"
echo "  gcloud scheduler jobs delete learningfocused-ingest-daily --location=${REGION} --project=${OLD_PROJECT}"
echo "  gcloud run jobs delete learningfocused-ingest --region=${REGION} --project=${OLD_PROJECT}"
echo "  gcloud run services delete learningfocused-mcp --region=${REGION} --project=${OLD_PROJECT}"
echo "  gcloud artifacts repositories delete learningfocused --location=${REGION} --project=${OLD_PROJECT}"
echo "  gcloud artifacts repositories remove-iam-policy-binding cloud-run-source-deploy --location=${REGION} --project=${OLD_PROJECT} --member=serviceAccount:learningfocused-ingest@${OLD_PROJECT}.iam.gserviceaccount.com --role=roles/artifactregistry.reader"
echo "    (keep cloud-run-source-deploy itself: InferPoker may use it)"
echo "  gcloud projects remove-iam-policy-binding ${OLD_PROJECT} --member=serviceAccount:learningfocused-ingest@${OLD_PROJECT}.iam.gserviceaccount.com --role=roles/run.developer --condition=None"
echo "  gcloud iam service-accounts delete learningfocused-ingest@${OLD_PROJECT}.iam.gserviceaccount.com --project=${OLD_PROJECT}"
echo "  gcloud iam service-accounts delete learningfocused-mcp@${OLD_PROJECT}.iam.gserviceaccount.com --project=${OLD_PROJECT}"
echo "  gcloud secrets delete learningfocused-openai-api-key --project=${OLD_PROJECT}"
echo "  gcloud secrets delete learningfocused-google-api-key --project=${OLD_PROJECT}"
echo "  gcloud secrets delete learningfocused-assemblyai-api-key --project=${OLD_PROJECT}"
echo "  gcloud storage rm -r gs://${OLD_BUCKET}"
