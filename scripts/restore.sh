#!/usr/bin/env bash
# Restore the RRC bot's database from a backup made by scripts/backup.sh.
#
#   scripts/restore.sh <archive.dump | gs://bucket/path/archive.dump> --yes
#
# DESTRUCTIVE: replaces every table in the target database with the archive's
# contents. The restore runs in a single transaction, so a failed restore
# leaves the database exactly as it was. In compose mode the bot is stopped
# during the restore and restarted afterwards if it was running.
#
# Environment: BACKUP_SOURCE=compose (default) | direct (libpq env vars), as in backup.sh.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_SOURCE="${BACKUP_SOURCE:-compose}"

log() { printf '%s restore: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }
usage() { echo "usage: $0 <archive.dump | gs://...> --yes" >&2; exit 2; }

[ $# -eq 2 ] || usage
src="$1"
[ "$2" = "--yes" ] || die "refusing to restore without --yes (this replaces all bot data)"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

case "$src" in
    gs://*)
        log "downloading $src"
        gcloud storage cp --quiet "$src" "$workdir/archive.dump" || die "download failed"
        archive="$workdir/archive.dump"
        ;;
    *)
        [ -f "$src" ] || die "no such file: $src"
        archive="$src"
        ;;
esac

restore_args=(--clean --if-exists --no-owner --single-transaction --exit-on-error)

case "$BACKUP_SOURCE" in
    compose)
        cd "$REPO_DIR"
        bot_was_running=0
        if docker compose --profile bot ps --status running --services | grep -qx bot; then
            bot_was_running=1
            log "stopping bot"
            docker compose --profile bot stop bot
        fi
        log "restoring $src into the compose database"
        if ! docker compose exec -T db \
            sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"' sh "${restore_args[@]}" \
            <"$archive"; then
            [ "$bot_was_running" = 1 ] && docker compose --profile bot start bot
            die "pg_restore failed; database left unchanged"
        fi
        if [ "$bot_was_running" = 1 ]; then
            log "starting bot"
            docker compose --profile bot start bot
        fi
        ;;
    direct)
        : "${PGDATABASE:?PGDATABASE must be set in direct mode}"
        log "restoring $src into $PGDATABASE"
        pg_restore -d "$PGDATABASE" "${restore_args[@]}" "$archive" ||
            die "pg_restore failed; database left unchanged"
        ;;
    *) die "BACKUP_SOURCE must be 'compose' or 'direct' (got '$BACKUP_SOURCE')" ;;
esac

log "done"
