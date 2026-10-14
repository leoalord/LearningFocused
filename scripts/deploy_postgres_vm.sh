#!/usr/bin/env bash
# Create (or update) the LearningFocused Postgres VM: Postgres 16 + pgvector + Apache AGE
# on an e2-small Container-Optimized OS VM in the LearningFocus project.
#
# - No external IP. Cloud Run reaches it over Direct VPC egress on the default network.
# - Data on a separate persistent disk (weekly snapshots). Nightly pg_dump to the bucket.
# - Two passwords in Secret Manager: the `learningfocused` superuser (migrations/admin
#   only) and `learningfocused_app` (what Cloud Run uses). The app DSN is secret
#   learningfocused-pg-dsn. The app role itself is created by `pg_store migrate --app-role`.
# - The image is built once and the VM runs it by digest. PG_REBUILD_IMAGE=1 rebuilds it
#   (check the AGE version; a change needs `ALTER EXTENSION age UPDATE`).
# Re-running is safe: existing resources are kept. Never deletes anything.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

source "$(dirname "${BASH_SOURCE[0]}")/_gcp_env.sh"
ZONE="${GCP_ZONE:-${REGION}-b}"
NETWORK="${PG_NETWORK:-default}"
SUBNET="${PG_SUBNET:-default}"
VM="${PG_VM:-learningfocused-pg}"
MACHINE_TYPE="${PG_MACHINE_TYPE:-e2-small}"
DATA_DISK="${PG_DATA_DISK:-learningfocused-pg-data}"
DATA_DISK_SIZE="${PG_DATA_DISK_SIZE:-20GB}"
AR_REPO="${AR_REPO:-learningfocused}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/postgres:16-age-vector"
SA_NAME="${PG_SA:-learningfocused-pg}"
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
SECRET_PASSWORD="${SECRET_PG_PASSWORD:-learningfocused-pg-password}"
SECRET_APP_PASSWORD="${SECRET_PG_APP_PASSWORD:-learningfocused-pg-app-password}"
APP_ROLE="${PG_APP_ROLE:-learningfocused_app}"
SECRET_DSN="${SECRET_PG_DSN:-learningfocused-pg-dsn}"
SECRET_READER_PASSWORD="${SECRET_PG_READER_PASSWORD:-learningfocused-pg-reader-password}"
READER_ROLE="${PG_READER_ROLE:-learningfocused_mcp}"
SECRET_READER_DSN="${SECRET_PG_READER_DSN:-learningfocused-pg-dsn-reader}"
SNAPSHOT_POLICY="${PG_SNAPSHOT_POLICY:-learningfocused-pg-weekly}"

gcloud config set project "${PROJECT}" >/dev/null

echo ">> Enabling APIs on ${PROJECT}"
gcloud services enable compute.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com secretmanager.googleapis.com storage.googleapis.com \
  --project="${PROJECT}"

if ! gcloud artifacts repositories describe "${AR_REPO}" --location="${REGION}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo ">> Creating Artifact Registry ${AR_REPO}"
  gcloud artifacts repositories create "${AR_REPO}" --repository-format=docker \
    --location="${REGION}" --description="LearningFocused images" --project="${PROJECT}"
fi

if [[ "${PG_REBUILD_IMAGE:-0}" == "1" ]] || ! gcloud artifacts docker images describe "${IMAGE}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo ">> Building ${IMAGE} (Postgres 16 + pgvector + AGE)"
  gcloud builds submit "${ROOT}/deploy/postgres" --tag="${IMAGE}" --project="${PROJECT}"
else
  echo ">> Reusing existing ${IMAGE} (PG_REBUILD_IMAGE=1 to rebuild)"
fi
IMAGE_REF="$(gcloud artifacts docker images describe "${IMAGE}" --project="${PROJECT}" \
  --format='value(image_summary.fully_qualified_digest)')"
[[ "${IMAGE_REF}" == *@sha256:* ]] || { echo "Could not resolve the digest of ${IMAGE}" >&2; exit 1; }

ensure_password_secret() {
  local secret="$1"
  if ! gcloud secrets describe "${secret}" --project="${PROJECT}" >/dev/null 2>&1; then
    echo ">> Creating secret ${secret} (random password, never printed)"
    gcloud secrets create "${secret}" --replication-policy=automatic --project="${PROJECT}"
    openssl rand -base64 33 | tr -d '/+=\n' | head -c 40 \
      | gcloud secrets versions add "${secret}" --data-file=- --project="${PROJECT}" >/dev/null
  fi
}
ensure_password_secret "${SECRET_PASSWORD}"
ensure_password_secret "${SECRET_APP_PASSWORD}"
ensure_password_secret "${SECRET_READER_PASSWORD}"

if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo ">> Creating VM service account ${SA_EMAIL}"
  gcloud iam service-accounts create "${SA_NAME}" --display-name="LearningFocused Postgres VM" --project="${PROJECT}"
  sleep 10
fi
echo ">> Granting the VM: image pull, password read, dump upload and read"
gcloud artifacts repositories add-iam-policy-binding "${AR_REPO}" --location="${REGION}" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/artifactregistry.reader" --project="${PROJECT}" >/dev/null
gcloud secrets add-iam-policy-binding "${SECRET_PASSWORD}" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/secretmanager.secretAccessor" --project="${PROJECT}" >/dev/null
# --condition=None on every bucket binding: once the policy holds a conditional binding,
# gcloud refuses (or prompts for) unconditional adds.
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/storage.objectCreator" --project="${PROJECT}" \
  --condition=None >/dev/null
# `gcloud storage cp` resolves the destination with an objects.get and then an objects.list on
# the dump's name, so the VM needs both, limited to pg_dumps/ so it can't read the corpus.
# objects.list is checked against the bucket, so it is matched on the list prefix instead.
# The expression has a comma, which --condition can't parse, hence the file.
PG_DUMPS_CONDITION="$(mktemp)"
cat > "${PG_DUMPS_CONDITION}" <<EOF
title: pg-dumps-read
expression: >-
  resource.name.startsWith("projects/_/buckets/${BUCKET}/objects/pg_dumps/") ||
  api.getAttribute("storage.googleapis.com/objectListPrefix", "").startsWith("pg_dumps/")
EOF
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/storage.objectViewer" --project="${PROJECT}" \
  --condition-from-file="${PG_DUMPS_CONDITION}" >/dev/null
rm -f "${PG_DUMPS_CONDITION}"
if [[ -z "$(gcloud storage buckets describe "gs://${BUCKET}" --project="${PROJECT}" --format='value(lifecycle_config)')" ]]; then
  echo ">> Bucket lifecycle: delete pg_dumps/ objects after 30 days"
  LIFECYCLE="$(mktemp)"
  printf '%s' '{"rule":[{"action":{"type":"Delete"},"condition":{"age":30,"matchesPrefix":["pg_dumps/"]}}]}' > "${LIFECYCLE}"
  gcloud storage buckets update "gs://${BUCKET}" --lifecycle-file="${LIFECYCLE}" --project="${PROJECT}"
  rm -f "${LIFECYCLE}"
else
  echo ">> Bucket already has lifecycle rules; not changing them (add a pg_dumps/ age rule by hand)"
fi
for role in roles/logging.logWriter roles/monitoring.metricWriter; do
  gcloud projects add-iam-policy-binding "${PROJECT}" --member="serviceAccount:${SA_EMAIL}" \
    --role="${role}" --condition=None >/dev/null
done

echo ">> Private Google Access on ${SUBNET} (VM has no external IP)"
gcloud compute networks subnets update "${SUBNET}" --region="${REGION}" \
  --enable-private-ip-google-access --project="${PROJECT}"
SUBNET_RANGE="$(gcloud compute networks subnets describe "${SUBNET}" --region="${REGION}" \
  --project="${PROJECT}" --format='value(ipCidrRange)')"

# Create the rule, or bring an existing one back to these ports/ranges/tags.
ensure_firewall() {
  local name="$1" rules="$2" ranges="$3"
  local args=(--project="${PROJECT}" --rules="${rules}" --source-ranges="${ranges}" --target-tags=learningfocused-pg)
  if gcloud compute firewall-rules describe "${name}" --project="${PROJECT}" >/dev/null 2>&1; then
    gcloud compute firewall-rules update "${name}" "${args[@]}"
  else
    gcloud compute firewall-rules create "${name}" --network="${NETWORK}" --direction=INGRESS --action=ALLOW "${args[@]}"
  fi
}
echo ">> Firewall: Postgres from ${SUBNET_RANGE}; SSH and admin tunnels only through IAP"
ensure_firewall learningfocused-pg-allow-subnet tcp:5432 "${SUBNET_RANGE}"
ensure_firewall learningfocused-pg-allow-iap tcp:22,tcp:5432 35.235.240.0/20

if ! gcloud compute resource-policies describe "${SNAPSHOT_POLICY}" --region="${REGION}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo ">> Weekly snapshot schedule (keep 4)"
  gcloud compute resource-policies create snapshot-schedule "${SNAPSHOT_POLICY}" \
    --region="${REGION}" --weekly-schedule=sunday --start-time=04:00 \
    --max-retention-days=28 --on-source-disk-delete=keep-auto-snapshots --project="${PROJECT}"
fi

if ! gcloud compute disks describe "${DATA_DISK}" --zone="${ZONE}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo ">> Creating data disk ${DATA_DISK} (${DATA_DISK_SIZE})"
  gcloud compute disks create "${DATA_DISK}" --zone="${ZONE}" --size="${DATA_DISK_SIZE}" \
    --type=pd-balanced --resource-policies="${SNAPSHOT_POLICY}" --project="${PROJECT}"
fi

USER_DATA="$(mktemp)"
trap 'rm -f "${USER_DATA}"' EXIT
sed -e "s#__IMAGE__#${IMAGE_REF}#g" -e "s#__BUCKET__#${BUCKET}#g" \
    -e "s#__PROJECT__#${PROJECT}#g" -e "s#__SECRET__#${SECRET_PASSWORD}#g" \
    "${ROOT}/deploy/postgres/cloud-init.yaml" > "${USER_DATA}"

# Ship container logs and VM metrics to Cloud Logging/Monitoring (COS only does it when asked).
OPS_METADATA="google-logging-enabled=true,google-monitoring-enabled=true"

if gcloud compute instances describe "${VM}" --zone="${ZONE}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo ">> Updating boot config on ${VM}"
  gcloud compute instances add-metadata "${VM}" --zone="${ZONE}" --metadata="${OPS_METADATA}" \
    --metadata-from-file=user-data="${USER_DATA}" --project="${PROJECT}"
  # stop/start (not reset) so systemd stops Postgres cleanly before the reboot; cloud-init
  # re-applies the units on boot. The data disk is untouched.
  echo ">> Restarting ${VM} (clean shutdown)"
  gcloud compute instances stop "${VM}" --zone="${ZONE}" --project="${PROJECT}"
  gcloud compute instances start "${VM}" --zone="${ZONE}" --project="${PROJECT}"
else
  echo ">> Creating ${VM} (${MACHINE_TYPE}, no external IP)"
  gcloud compute instances create "${VM}" --zone="${ZONE}" --machine-type="${MACHINE_TYPE}" \
    --image-family=cos-stable --image-project=cos-cloud --boot-disk-size=10GB \
    --network="${NETWORK}" --subnet="${SUBNET}" --no-address \
    --disk="name=${DATA_DISK},device-name=pgdata,mode=rw,boot=no,auto-delete=no" \
    --service-account="${SA_EMAIL}" --scopes=cloud-platform \
    --tags=learningfocused-pg --shielded-secure-boot \
    --metadata="${OPS_METADATA}" \
    --metadata-from-file=user-data="${USER_DATA}" --project="${PROJECT}"
fi

PG_IP="$(gcloud compute instances describe "${VM}" --zone="${ZONE}" --project="${PROJECT}" \
  --format='value(networkInterfaces[0].networkIP)')"
# store_dsn <dsn secret> <role> <password secret>: keep a DSN secret pointing at this VM.
store_dsn() {
  local secret="$1" role="$2" password_secret="$3" password dsn current
  password="$(gcloud secrets versions access latest --secret="${password_secret}" --project="${PROJECT}")"
  dsn="postgresql://${role}:${password}@${PG_IP}:5432/learningfocused"
  if ! gcloud secrets describe "${secret}" --project="${PROJECT}" >/dev/null 2>&1; then
    gcloud secrets create "${secret}" --replication-policy=automatic --project="${PROJECT}"
  fi
  current="$(gcloud secrets versions access latest --secret="${secret}" --project="${PROJECT}" 2>/dev/null || true)"
  if [[ "${current}" != "${dsn}" ]]; then
    echo ">> Storing DSN in secret ${secret}"
    printf '%s' "${dsn}" | gcloud secrets versions add "${secret}" --data-file=- --project="${PROJECT}" >/dev/null
  fi
}
store_dsn "${SECRET_DSN}" "${APP_ROLE}" "${SECRET_APP_PASSWORD}"
store_dsn "${SECRET_READER_DSN}" "${READER_ROLE}" "${SECRET_READER_PASSWORD}"

echo
echo "VM:        ${VM} (${ZONE}, ${MACHINE_TYPE}, internal IP ${PG_IP})"
echo "Image:     ${IMAGE_REF}"
echo "Data disk: ${DATA_DISK} (weekly snapshots: ${SNAPSHOT_POLICY})"
echo "Dumps:     gs://${BUCKET}/pg_dumps/ nightly 03:30 UTC"
echo "DSN:       secret ${SECRET_DSN} (role ${APP_ROLE}, read-write; the daily job reads it as PG_DSN)"
echo "Reader:    secret ${SECRET_READER_DSN} (role ${READER_ROLE}, read-only; the MCP server reads it as PG_DSN)"
echo
echo "Next: apply the schema and create ${APP_ROLE} + ${READER_ROLE} through an IAP tunnel (see docs/postgres.md),"
echo "then ./scripts/pg_backup.sh dump-now and ./scripts/pg_backup.sh restore-test."
