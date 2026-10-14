#!/usr/bin/env bash
# Create (or update) the LearningFocused Postgres VM: Postgres 16 + pgvector + Apache AGE
# on an e2-small Container-Optimized OS VM in the LearningFocus project.
#
# - No external IP. Cloud Run reaches it over Direct VPC egress on the default network.
# - Data on a separate persistent disk (weekly snapshots). Nightly pg_dump to the bucket.
# - Password in Secret Manager; the full DSN is stored as secret learningfocused-pg-dsn.
# Re-running is safe: existing resources are kept. Never deletes anything.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

PROJECT="${GCP_BACKUP_PROJECT:-gen-lang-client-0304733057}"
REGION="${GCP_REGION:-us-west1}"
ZONE="${GCP_ZONE:-${REGION}-b}"
NETWORK="${PG_NETWORK:-default}"
SUBNET="${PG_SUBNET:-default}"
VM="${PG_VM:-learningfocused-pg}"
MACHINE_TYPE="${PG_MACHINE_TYPE:-e2-small}"
DATA_DISK="${PG_DATA_DISK:-learningfocused-pg-data}"
DATA_DISK_SIZE="${PG_DATA_DISK_SIZE:-20GB}"
BUCKET="${GCS_CORPUS_BUCKET:-learningfocused-corpus}"
AR_REPO="${AR_REPO:-learningfocused}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/postgres:16-age-vector"
SA_NAME="${PG_SA:-learningfocused-pg}"
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
SECRET_PASSWORD="${SECRET_PG_PASSWORD:-learningfocused-pg-password}"
SECRET_DSN="${SECRET_PG_DSN:-learningfocused-pg-dsn}"
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

echo ">> Building ${IMAGE} (Postgres 16 + pgvector + AGE)"
gcloud builds submit "${ROOT}/deploy/postgres" --tag="${IMAGE}" --project="${PROJECT}"

if ! gcloud secrets describe "${SECRET_PASSWORD}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo ">> Creating secret ${SECRET_PASSWORD} (random password, never printed)"
  gcloud secrets create "${SECRET_PASSWORD}" --replication-policy=automatic --project="${PROJECT}"
  openssl rand -base64 33 | tr -d '/+=\n' | head -c 40 \
    | gcloud secrets versions add "${SECRET_PASSWORD}" --data-file=- --project="${PROJECT}" >/dev/null
fi

if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo ">> Creating VM service account ${SA_EMAIL}"
  gcloud iam service-accounts create "${SA_NAME}" --display-name="LearningFocused Postgres VM" --project="${PROJECT}"
  sleep 10
fi
echo ">> Granting the VM: image pull, password read, dump upload"
gcloud artifacts repositories add-iam-policy-binding "${AR_REPO}" --location="${REGION}" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/artifactregistry.reader" --project="${PROJECT}" >/dev/null
gcloud secrets add-iam-policy-binding "${SECRET_PASSWORD}" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/secretmanager.secretAccessor" --project="${PROJECT}" >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/storage.objectCreator" --project="${PROJECT}" >/dev/null
for role in roles/logging.logWriter roles/monitoring.metricWriter; do
  gcloud projects add-iam-policy-binding "${PROJECT}" --member="serviceAccount:${SA_EMAIL}" \
    --role="${role}" --condition=None >/dev/null
done

echo ">> Private Google Access on ${SUBNET} (VM has no external IP)"
gcloud compute networks subnets update "${SUBNET}" --region="${REGION}" \
  --enable-private-ip-google-access --project="${PROJECT}"
SUBNET_RANGE="$(gcloud compute networks subnets describe "${SUBNET}" --region="${REGION}" \
  --project="${PROJECT}" --format='value(ipCidrRange)')"

ensure_firewall() {
  local name="$1"; shift
  if ! gcloud compute firewall-rules describe "${name}" --project="${PROJECT}" >/dev/null 2>&1; then
    gcloud compute firewall-rules create "${name}" --project="${PROJECT}" --network="${NETWORK}" "$@"
  fi
}
echo ">> Firewall: Postgres from ${SUBNET_RANGE}; SSH and admin tunnels only through IAP"
ensure_firewall learningfocused-pg-allow-subnet --direction=INGRESS --action=ALLOW \
  --rules=tcp:5432 --source-ranges="${SUBNET_RANGE}" --target-tags=learningfocused-pg
ensure_firewall learningfocused-pg-allow-iap --direction=INGRESS --action=ALLOW \
  --rules=tcp:22,tcp:5432 --source-ranges=35.235.240.0/20 --target-tags=learningfocused-pg

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
sed -e "s#__IMAGE__#${IMAGE}#g" -e "s#__BUCKET__#${BUCKET}#g" \
    -e "s#__PROJECT__#${PROJECT}#g" -e "s#__SECRET__#${SECRET_PASSWORD}#g" \
    "${ROOT}/deploy/postgres/cloud-init.yaml" > "${USER_DATA}"

if gcloud compute instances describe "${VM}" --zone="${ZONE}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo ">> Updating boot config on ${VM} and restarting Postgres (data disk untouched)"
  gcloud compute instances add-metadata "${VM}" --zone="${ZONE}" \
    --metadata-from-file=user-data="${USER_DATA}" --project="${PROJECT}"
  gcloud compute instances reset "${VM}" --zone="${ZONE}" --project="${PROJECT}"
else
  echo ">> Creating ${VM} (${MACHINE_TYPE}, no external IP)"
  gcloud compute instances create "${VM}" --zone="${ZONE}" --machine-type="${MACHINE_TYPE}" \
    --image-family=cos-stable --image-project=cos-cloud --boot-disk-size=10GB \
    --network="${NETWORK}" --subnet="${SUBNET}" --no-address \
    --disk="name=${DATA_DISK},device-name=pgdata,mode=rw,boot=no,auto-delete=no" \
    --service-account="${SA_EMAIL}" --scopes=cloud-platform \
    --tags=learningfocused-pg --shielded-secure-boot \
    --metadata-from-file=user-data="${USER_DATA}" --project="${PROJECT}"
fi

PG_IP="$(gcloud compute instances describe "${VM}" --zone="${ZONE}" --project="${PROJECT}" \
  --format='value(networkInterfaces[0].networkIP)')"
PASSWORD="$(gcloud secrets versions access latest --secret="${SECRET_PASSWORD}" --project="${PROJECT}")"
DSN="postgresql://learningfocused:${PASSWORD}@${PG_IP}:5432/learningfocused"
if ! gcloud secrets describe "${SECRET_DSN}" --project="${PROJECT}" >/dev/null 2>&1; then
  gcloud secrets create "${SECRET_DSN}" --replication-policy=automatic --project="${PROJECT}"
fi
CURRENT_DSN="$(gcloud secrets versions access latest --secret="${SECRET_DSN}" --project="${PROJECT}" 2>/dev/null || true)"
if [[ "${CURRENT_DSN}" != "${DSN}" ]]; then
  echo ">> Storing DSN in secret ${SECRET_DSN}"
  printf '%s' "${DSN}" | gcloud secrets versions add "${SECRET_DSN}" --data-file=- --project="${PROJECT}" >/dev/null
fi
unset PASSWORD DSN CURRENT_DSN

echo
echo "VM:        ${VM} (${ZONE}, ${MACHINE_TYPE}, internal IP ${PG_IP})"
echo "Image:     ${IMAGE}"
echo "Data disk: ${DATA_DISK} (weekly snapshots: ${SNAPSHOT_POLICY})"
echo "Dumps:     gs://${BUCKET}/pg_dumps/ nightly 03:30 UTC"
echo "DSN:       secret ${SECRET_DSN} (Cloud Run reads it as PG_DSN)"
echo
echo "Next: apply the schema from inside the VPC, e.g. through an IAP tunnel:"
echo "  gcloud compute start-iap-tunnel ${VM} 5432 --local-host-port=localhost:6543 --zone=${ZONE} --project=${PROJECT} &"
echo "  PG_DSN=postgresql://learningfocused:<password>@localhost:6543/learningfocused uv run python -m src.database.pg_store migrate"
echo "Then prove a restore works: ./scripts/pg_backup.sh restore-test"
