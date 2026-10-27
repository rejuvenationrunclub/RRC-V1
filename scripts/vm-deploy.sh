#!/usr/bin/env bash
# Deploy or update the RRC bot on its Google Cloud VM. Idempotent; run it for
# first install, after `git pull`, and after rotating the Discord token.
#
#   sudo /opt/rrc-bot/scripts/vm-deploy.sh [--guild-id <SERVER_ID>] [--bucket gs://<bucket>]
#
# What it does:
#   1. one-time host setup (scripts/gcp-vm-bootstrap.sh)
#   2. backs up the database first if the bot is already installed
#   3. creates .env on first run (random database password), records the guild ID
#   4. reads the Discord token from Secret Manager into .env (never printed)
#   5. installs the nightly backup timer (off-site copy to --bucket)
#   6. builds and starts the containers, recreating the bot if its settings changed
#   7. waits for "Connected as" in the bot log and reports the result
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Test hook: stage system files (/etc, /var) under this directory and skip host setup.
PREFIX="${RRC_ROOT_PREFIX:-}"
ENV_FILE="$REPO_DIR/.env"
SECRET="${RRC_TOKEN_SECRET:-discord-token}"
CONNECT_TIMEOUT_S="${RRC_CONNECT_TIMEOUT_S:-180}"

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() { echo "usage: $0 [--guild-id <SERVER_ID>] [--bucket gs://<bucket>]" >&2; exit 2; }

guild_id=""
bucket=""
while [ $# -gt 0 ]; do
    case "$1" in
        --guild-id) [ $# -ge 2 ] || usage; guild_id="$2"; shift 2 ;;
        --bucket) [ $# -ge 2 ] || usage; bucket="$2"; shift 2 ;;
        *) usage ;;
    esac
done
case "$guild_id" in '' | *[!0-9]*) [ -z "$guild_id" ] || die "--guild-id must be the numeric server ID" ;; esac
case "$bucket" in '' | gs://?*) ;; *) die "--bucket must look like gs://<bucket-name>" ;; esac

dc() { (cd "$REPO_DIR" && docker compose --profile bot "$@"); }

env_get() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1; }
env_set() {
    # Rewrite via a temp file in the same directory: atomic, owner-only, value never echoed.
    local tmp
    tmp="$(mktemp "$ENV_FILE.XXXXXX")"
    grep -v "^$1=" "$ENV_FILE" >"$tmp" || true
    printf '%s=%s\n' "$1" "$2" >>"$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$ENV_FILE"
}
fingerprint() { sha256sum "$ENV_FILE" | cut -d' ' -f1; }

if [ -z "$PREFIX" ]; then
    [ "$(id -u)" -eq 0 ] || die "run with sudo"
    "$REPO_DIR/scripts/gcp-vm-bootstrap.sh"
fi

umask 077

# --- 2. Safety backup before changing a running installation -----------------
if dc ps --status running --services 2>/dev/null | grep -qx db; then
    log "Backing up the database before deploying"
    (
        # shellcheck source=/dev/null
        [ -f "$PREFIX/etc/rrc-backup.env" ] && . "$PREFIX/etc/rrc-backup.env"
        export GCS_BUCKET="${GCS_BUCKET:-}"
        BACKUP_DIR="$PREFIX/var/backups/rrc" "$REPO_DIR/scripts/backup.sh"
    ) || die "pre-deploy backup failed; nothing was changed"
fi

# --- 3. Settings file ---------------------------------------------------------
if [ ! -f "$ENV_FILE" ]; then
    log "Creating .env with a random database password"
    cp "$REPO_DIR/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    password="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    env_set POSTGRES_PASSWORD "$password"
    # Used only by host-side tools; the bot container gets its own URL from docker-compose.yml.
    env_set DATABASE_URL "postgresql+asyncpg://rrc:$password@localhost:5432/rrc"
    env_set TEST_DATABASE_URL ""
    unset password
fi
chmod 600 "$ENV_FILE"
before="$(fingerprint)"

[ -n "$guild_id" ] && env_set DISCORD_GUILD_ID "$guild_id"
[ -n "$(env_get DISCORD_GUILD_ID)" ] ||
    die "no Discord server ID yet; re-run with --guild-id <SERVER_ID>"

# --- 4. Discord token from Secret Manager -------------------------------------
if ! token="$(gcloud secrets versions access latest --secret="$SECRET" --quiet 2>/dev/null)" ||
    [ -z "${token//[[:space:]]/}" ]; then
    die "no Discord token in Secret Manager secret '$SECRET' yet. Add it in the Google Cloud console (Security > Secret Manager > $SECRET > + NEW VERSION), then re-run this script."
fi
env_set DISCORD_TOKEN "$token"
unset token

after="$(fingerprint)"

# --- 5. Nightly backups --------------------------------------------------------
install -d "$PREFIX/etc/systemd/system"
if [ -n "$bucket" ]; then
    printf 'GCS_BUCKET=%s\n' "$bucket" >"$PREFIX/etc/rrc-backup.env"
    chmod 644 "$PREFIX/etc/rrc-backup.env"
elif [ ! -f "$PREFIX/etc/rrc-backup.env" ]; then
    log "WARNING: no --bucket given; backups will stay on this VM only"
fi
install -m 644 "$REPO_DIR/deploy/systemd/rrc-backup.service" "$REPO_DIR/deploy/systemd/rrc-backup.timer" \
    "$PREFIX/etc/systemd/system/"
systemctl daemon-reload
systemctl enable --now rrc-backup.timer >/dev/null

# --- 6. Containers -------------------------------------------------------------
old_bot="$(dc ps -q bot 2>/dev/null || true)"
started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
log "Building and starting containers (first build takes a few minutes)"
dc up -d --build
if [ "$before" != "$after" ]; then
    log "Settings changed; recreating the bot"
    dc up -d --force-recreate bot
fi
new_bot="$(dc ps -q bot 2>/dev/null || true)"

# --- 7. Verify -------------------------------------------------------------------
log "Waiting for the bot to connect to Discord"
deadline=$((SECONDS + CONNECT_TIMEOUT_S))
while [ "$SECONDS" -lt "$deadline" ]; do
    recent="$(dc logs --no-color --since "$started_at" bot 2>/dev/null || true)"
    case "$recent" in
        *"Connected as"*)
            log "SUCCESS: $(printf '%s\n' "$recent" | grep -o 'Connected as.*' | tail -n 1)"
            log "Now type /rrc ping in your Discord server."
            exit 0
            ;;
        *PrivilegedIntentsRequired*)
            die "Discord rejected the Server Members intent. Developer Portal > Bot > enable 'Server Members Intent', then re-run."
            ;;
        *"Improper token"* | *LoginFailure*)
            die "Discord rejected the token. Reset it in the Developer Portal, add it as a NEW VERSION of secret '$SECRET', then re-run."
            ;;
        *"Missing required environment variable"*)
            die "the bot reports missing settings: $(printf '%s\n' "$recent" | grep -o 'Missing required.*' | tail -n 1)"
            ;;
    esac
    if [ -n "$old_bot" ] && [ "$old_bot" = "$new_bot" ] && dc ps --status running --services | grep -qx bot; then
        log "SUCCESS: bot unchanged and still running. Check with /rrc ping in Discord."
        exit 0
    fi
    sleep 3
done
die "the bot did not report 'Connected as' within ${CONNECT_TIMEOUT_S}s. Inspect: sudo docker compose -f $REPO_DIR/docker-compose.yml --profile bot logs --tail 50 bot"
