# Shared GCP defaults for the LearningFocused scripts. Source it; don't run it.
# Everything for LearningFocused lives in the LearningFocus project, never in inferpoker
# (that project belongs to InferPoker).
#
# Overrides: GCP_BACKUP_PROJECT (historical name; it is the main project now),
# GCP_REGION, GCS_CORPUS_BUCKET. A stale .env or shell export still pointing at
# inferpoker is refused instead of silently deploying there.

LF_DEFAULT_PROJECT="gen-lang-client-0304733057"
LF_DEFAULT_REGION="us-west1"
LF_DEFAULT_BUCKET="learningfocused-corpus"

PROJECT="${GCP_BACKUP_PROJECT:-${LF_DEFAULT_PROJECT}}"
REGION="${GCP_REGION:-${LF_DEFAULT_REGION}}"
BUCKET="${GCS_CORPUS_BUCKET:-${LF_DEFAULT_BUCKET}}"

if [[ "${PROJECT}" == "inferpoker" || "${BUCKET}" == inferpoker-* ]]; then
  echo "Refusing to use project=${PROJECT} bucket=${BUCKET}: LearningFocused lives in ${LF_DEFAULT_PROJECT}." >&2
  echo "Unset or update GCP_BACKUP_PROJECT / GCS_CORPUS_BUCKET in your shell or .env." >&2
  exit 1
fi
if [[ "${PROJECT}" != "${LF_DEFAULT_PROJECT}" || "${BUCKET}" != "${LF_DEFAULT_BUCKET}" ]]; then
  echo "WARNING: using overrides project=${PROJECT} bucket=${BUCKET} (defaults ${LF_DEFAULT_PROJECT} / ${LF_DEFAULT_BUCKET})" >&2
fi
