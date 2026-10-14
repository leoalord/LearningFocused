#!/usr/bin/env bash
# Knowledge-store backups. The VM already runs a nightly pg_dump to gs://$BUCKET/pg_dumps/.
#   dump-now      run that backup right now (over IAP SSH)
#   list          list dumps in the bucket
#   restore-test  copy the newest dump back from GCS, restore it into a scratch database on
#                 the VM, count rows and run a Cypher query, then drop it
# Never touches the live `learningfocused` database.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/_gcp_env.sh"
ZONE="${GCP_ZONE:-${REGION}-b}"
VM="${PG_VM:-learningfocused-pg}"

ssh_vm() {
  gcloud compute ssh "${VM}" --zone="${ZONE}" --project="${PROJECT}" --tunnel-through-iap --command="$1"
}

case "${1:-}" in
  dump-now)
    ssh_vm "sudo systemctl start pg-backup.service && sudo journalctl -u pg-backup.service -n 20 --no-pager"
    ;;
  list)
    gcloud storage ls -l "gs://${BUCKET}/pg_dumps/" --project="${PROJECT}"
    ;;
  restore-test)
    # Restores from the GCS copy (not the VM's local file) so this proves the off-box backup.
    LATEST="$(gcloud storage ls "gs://${BUCKET}/pg_dumps/*.dump" --project="${PROJECT}" | sort | tail -n 1)"
    [[ -n "${LATEST}" ]] || { echo "No dumps in gs://${BUCKET}/pg_dumps/" >&2; exit 1; }
    echo "Restore-testing ${LATEST}"
    ssh_vm "set -euo pipefail
      SCRATCH=/mnt/disks/pgdata/backups/restore-check
      sudo rm -rf \${SCRATCH} && sudo mkdir -p \${SCRATCH}
      sudo docker run --rm -v \${SCRATCH}:/restore gcr.io/google.com/cloudsdktool/google-cloud-cli:slim \
        gcloud storage cp '${LATEST}' /restore/latest.dump --project='${PROJECT}'
      sudo docker exec pg dropdb -U learningfocused --if-exists restore_check
      sudo docker exec pg createdb -U learningfocused restore_check
      sudo docker exec -i pg pg_restore -U learningfocused -d restore_check --no-owner --exit-on-error < \${SCRATCH}/latest.dump
      sudo docker exec pg psql -U learningfocused -d restore_check -v ON_ERROR_STOP=1 -c \
        \"SET search_path = public, ag_catalog; SELECT (SELECT count(*) FROM chunks) AS chunks, (SELECT count(*) FROM documents) AS documents, (SELECT count(*) FROM sources) AS sources; SELECT * FROM cypher('knowledge', \\\$\\\$ MATCH (n) RETURN count(n) \\\$\\\$) AS (graph_nodes agtype);\"
      sudo docker exec pg dropdb -U learningfocused restore_check
      sudo rm -rf \${SCRATCH}
      echo 'Restore test passed'"
    ;;
  *)
    echo "Usage: $(basename "$0") <dump-now|list|restore-test>" >&2
    exit 2
    ;;
esac
