#!/usr/bin/env bash
# Knowledge-store backups. The VM already runs a nightly pg_dump to gs://$BUCKET/pg_dumps/.
#   dump-now      run that backup right now (over IAP SSH)
#   list          list dumps in the bucket
#   restore-test  restore the newest dump into a scratch database on the VM, count rows, drop it
# Never touches the live `learningfocused` database.
set -euo pipefail

PROJECT="${GCP_BACKUP_PROJECT:-gen-lang-client-0304733057}"
ZONE="${GCP_ZONE:-us-west1-b}"
VM="${PG_VM:-learningfocused-pg}"
BUCKET="${GCS_CORPUS_BUCKET:-learningfocused-corpus}"

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
    ssh_vm 'set -euo pipefail
      LATEST=$(ls -1t /mnt/disks/pgdata/backups/*.dump | head -n 1)
      echo "Restoring ${LATEST} into restore_check"
      sudo docker exec pg dropdb -U learningfocused --if-exists restore_check
      sudo docker exec pg createdb -U learningfocused restore_check
      sudo docker exec -i pg pg_restore -U learningfocused -d restore_check --no-owner < "${LATEST}"
      sudo docker exec pg psql -U learningfocused -d restore_check -c "LOAD '"'"'age'"'"'; SELECT (SELECT count(*) FROM chunks) AS chunks, (SELECT count(*) FROM documents) AS documents, (SELECT count(*) FROM sources) AS sources, (SELECT count(*) FROM ag_catalog.ag_graph) AS graphs;"
      sudo docker exec pg dropdb -U learningfocused restore_check
      echo "Restore test passed"'
    ;;
  *)
    echo "Usage: $(basename "$0") <dump-now|list|restore-test>" >&2
    exit 2
    ;;
esac
