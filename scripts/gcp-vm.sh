#!/usr/bin/env bash
# Run RRC bot operations on the Google Cloud VM over `gcloud compute ssh`.
#
#   scripts/gcp-vm.sh <PROJECT_ID> deploy --guild-id <SERVER_ID> [--branch <BRANCH>]
#   scripts/gcp-vm.sh <PROJECT_ID> update [--branch <BRANCH>]
#   scripts/gcp-vm.sh <PROJECT_ID> backup-now
#   scripts/gcp-vm.sh <PROJECT_ID> status
#   scripts/gcp-vm.sh <PROJECT_ID> ssh                  # interactive shell (for people)
#
# deploy/update fetch the code onto the VM (/opt/rrc-bot) and run scripts/vm-deploy.sh,
# which backs up first when already installed. ZONE env var overrides us-west1-b.
set -euo pipefail

REPO_URL="https://github.com/rejuvenationrunclub/RRC-V1.git"
VM=rrc-bot
ZONE="${ZONE:-us-west1-b}"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }

[ $# -ge 2 ] || usage
PROJECT="$1"
cmd="$2"
# Google project IDs: 6-30 chars, lowercase letters, digits, hyphens; start with a letter.
[[ "$PROJECT" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || die "invalid Google Cloud project ID: '$PROJECT'"
shift 2

branch="main"
guild_id=""
while [ $# -gt 0 ]; do
    case "$1" in
        --branch) [ $# -ge 2 ] || usage; branch="$2"; shift 2 ;;
        --guild-id) [ $# -ge 2 ] || usage; guild_id="$2"; shift 2 ;;
        *) usage ;;
    esac
done
case "$branch" in '' | -* | *[!A-Za-z0-9._/-]*) die "invalid branch name: '$branch'" ;; esac

remote() {
    gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT" --quiet --command "$1"
}

sync_code="sudo apt-get update -qq && sudo apt-get install -y -qq git >/dev/null \
&& { [ -d /opt/rrc-bot/.git ] || sudo git clone -q $REPO_URL /opt/rrc-bot; } \
&& sudo git -C /opt/rrc-bot fetch -q origin \
&& sudo git -C /opt/rrc-bot checkout -q $branch \
&& sudo git -C /opt/rrc-bot pull -q --ff-only origin $branch"

case "$cmd" in
    deploy)
        case "$guild_id" in '' | *[!0-9]*) die "deploy needs --guild-id <numeric Discord server ID>" ;; esac
        remote "$sync_code && sudo /opt/rrc-bot/scripts/vm-deploy.sh --guild-id $guild_id --bucket gs://rrc-backups-$PROJECT"
        ;;
    update)
        [ -z "$guild_id" ] || die "update keeps the existing server ID; use deploy to change it"
        remote "$sync_code && sudo /opt/rrc-bot/scripts/vm-deploy.sh"
        ;;
    backup-now)
        remote "sudo systemctl start rrc-backup.service && journalctl -u rrc-backup.service -n 8 --no-pager"
        echo "Off-site copies:"
        gcloud storage ls --project="$PROJECT" "gs://rrc-backups-$PROJECT/"
        ;;
    status)
        remote "cd /opt/rrc-bot && sudo docker compose --profile bot ps && sudo docker compose --profile bot logs --no-color --tail 30 bot"
        ;;
    ssh)
        gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT"
        ;;
    *) usage ;;
esac
