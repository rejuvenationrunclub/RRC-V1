#!/usr/bin/env bash
# Create (or verify) every Google Cloud resource the RRC bot needs. Idempotent.
#
#   scripts/gcp-provision.sh <PROJECT_ID> [ZONE]        # ZONE default: us-west1-b
#
# Runs wherever the gcloud CLI is logged in: Windows (Git Bash), macOS, Linux
# or Cloud Shell. The project must already be linked to a billing account.
#
# Creates:
#   - a dedicated, least-privilege service account for the VM (rrc-bot-vm)
#   - a private backup bucket gs://rrc-backups-<PROJECT_ID> (30-day lifecycle)
#   - an empty Secret Manager secret "discord-token" (you add the value yourself)
#   - the e2-micro VM "rrc-bot" on a 30 GB *standard* disk (free-tier shapes only)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

step() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() { echo "usage: $0 <PROJECT_ID> [ZONE]   (ZONE in us-west1, us-central1 or us-east1)" >&2; exit 2; }

if [ $# -lt 1 ] || [ $# -gt 2 ]; then usage; fi
PROJECT="$1"
ZONE="${2:-us-west1-b}"
# Google project IDs: 6-30 chars, lowercase letters, digits, hyphens; start with a letter.
[[ "$PROJECT" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || die "invalid Google Cloud project ID: '$PROJECT'"

# Free-tier e2-micro is only free in these three regions; anything else is billed.
case "$ZONE" in
    us-west1-? | us-central1-? | us-east1-?) ;;
    *) die "zone '$ZONE' is outside the free-tier regions (us-west1, us-central1, us-east1)" ;;
esac
REGION="${ZONE%-*}"

VM=rrc-bot
SA_NAME=rrc-bot-vm
SA="$SA_NAME@$PROJECT.iam.gserviceaccount.com"
BUCKET="gs://rrc-backups-$PROJECT"
SECRET=discord-token

g() { gcloud --project="$PROJECT" --quiet "$@"; }
exists() { g "$@" >/dev/null 2>&1; }

# IAM can take a few seconds to see a brand-new service account.
retry() {
    local attempt
    for attempt in 1 2 3 4 5 6; do
        if "$@" >/dev/null; then return 0; fi
        [ "$attempt" -lt 6 ] && sleep 5
    done
    die "command failed after retries: $*"
}

step "Checking billing on project $PROJECT"
billing="$(g billing projects describe "$PROJECT" --format='value(billingEnabled)' 2>/dev/null || true)"
[ "$billing" = "True" ] || die "project '$PROJECT' has no billing account linked. Link one: gcloud billing projects link $PROJECT --billing-account=<ACCOUNT_ID>  (list accounts: gcloud billing accounts list)"

step "Enabling APIs (compute, storage, secret manager, iam)"
g services enable compute.googleapis.com storage.googleapis.com \
    secretmanager.googleapis.com iam.googleapis.com

step "Service account $SA"
exists iam service-accounts describe "$SA" ||
    g iam service-accounts create "$SA_NAME" --display-name="RRC bot VM"
for role in roles/logging.logWriter roles/monitoring.metricWriter; do
    retry g projects add-iam-policy-binding "$PROJECT" \
        --member="serviceAccount:$SA" --role="$role" --condition=None
done

step "Backup bucket $BUCKET"
exists storage buckets describe "$BUCKET" ||
    g storage buckets create "$BUCKET" --location="$REGION" \
        --default-storage-class=STANDARD --uniform-bucket-level-access --public-access-prevention
# Relative path on purpose: works with Windows-native gcloud under Git Bash.
(cd "$REPO_DIR" && g storage buckets update "$BUCKET" --lifecycle-file=deploy/gcs-lifecycle.json >/dev/null)
# Write + read (for restores) but not delete: a compromised VM cannot wipe the backups.
for role in roles/storage.objectCreator roles/storage.objectViewer; do
    retry g storage buckets add-iam-policy-binding "$BUCKET" --member="serviceAccount:$SA" --role="$role"
done

step "Secret Manager secret '$SECRET' (value added by you, never by scripts)"
exists secrets describe "$SECRET" ||
    g secrets create "$SECRET" --replication-policy=user-managed --locations="$REGION"
retry g secrets add-iam-policy-binding "$SECRET" \
    --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor

step "VM $VM in $ZONE"
if exists compute instances describe "$VM" --zone="$ZONE"; then
    echo "VM already exists; leaving it unchanged."
else
    g compute instances create "$VM" --zone="$ZONE" \
        --machine-type=e2-micro \
        --image-family=debian-12 --image-project=debian-cloud \
        --boot-disk-size=30GB --boot-disk-type=pd-standard \
        --service-account="$SA" --scopes=cloud-platform \
        --metadata=enable-oslogin=TRUE \
        --shielded-secure-boot
fi

has_token="no"
if g secrets versions list "$SECRET" --filter='state=ENABLED' --format='value(name)' 2>/dev/null | grep -q .; then
    has_token="yes"
fi

step "Done"
cat <<EOF
Project:         $PROJECT
VM:              $VM ($ZONE, e2-micro, 30 GB standard disk)
Service account: $SA
Backup bucket:   $BUCKET
Discord token:   secret '$SECRET' - value present: $has_token
EOF
if [ "$has_token" = "no" ]; then
    cat <<EOF

NEXT (a person, not a script): add the bot token as the secret's value at
  https://console.cloud.google.com/security/secret-manager/secret/$SECRET/versions?project=$PROJECT
  -> "+ NEW VERSION" -> paste the token -> "ADD NEW VERSION"
EOF
fi
