#!/usr/bin/env bash
# Back up the RRC bot's PostgreSQL database.
#
# Writes a compressed, verified pg_dump archive (custom format) to BACKUP_DIR,
# optionally copies it to a Google Cloud Storage bucket, and prunes local
# archives older than RETENTION_DAYS. Safe to run from cron/systemd.
#
# Environment (all optional):
#   BACKUP_DIR       where archives go               (default /var/backups/rrc)
#   RETENTION_DAYS   delete local archives older than (default 14)
#   GCS_BUCKET       off-site copy target, e.g. gs://rrc-backups-123/nightly
#   BACKUP_SOURCE    compose (default): dump the docker compose "db" service
#                    direct: run pg_dump on this host using libpq env vars
#                            (PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE)
#
# Exit status is non-zero if the dump, verification or upload fails, so the
# systemd unit shows as failed and the failure is visible in `systemctl status`.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/rrc}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
GCS_BUCKET="${GCS_BUCKET:-}"
BACKUP_SOURCE="${BACKUP_SOURCE:-compose}"

log() { printf '%s backup: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

case "$RETENTION_DAYS" in
    '' | *[!0-9]*) die "RETENTION_DAYS must be a whole number of days (got '$RETENTION_DAYS')" ;;
esac

# Archives contain member data: keep them readable by the owner only.
umask 077
mkdir -p "$BACKUP_DIR"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="$BACKUP_DIR/rrc-$stamp.dump"
partial="$archive.partial"
trap 'rm -f "$partial"' EXIT

dump() {
    case "$BACKUP_SOURCE" in
        compose)
            (cd "$REPO_DIR" && docker compose exec -T db \
                sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom')
            ;;
        direct)
            pg_dump --format=custom
            ;;
        *) die "BACKUP_SOURCE must be 'compose' or 'direct' (got '$BACKUP_SOURCE')" ;;
    esac
}

verify() {
    # pg_restore --list parses the whole table of contents; a truncated or
    # corrupt archive fails here instead of on the day we need it.
    case "$BACKUP_SOURCE" in
        compose) (cd "$REPO_DIR" && docker compose exec -T db pg_restore --list) <"$1" >/dev/null ;;
        direct) pg_restore --list "$1" >/dev/null ;;
    esac
}

log "dumping database (source: $BACKUP_SOURCE)"
dump >"$partial" || die "pg_dump failed"
[ -s "$partial" ] || die "pg_dump produced an empty file"
verify "$partial" || die "archive failed verification"
mv "$partial" "$archive"
log "wrote $archive ($(du -h "$archive" | cut -f1))"

if [ -n "$GCS_BUCKET" ]; then
    log "copying to ${GCS_BUCKET%/}/"
    gcloud storage cp --quiet "$archive" "${GCS_BUCKET%/}/" || die "upload to $GCS_BUCKET failed"
fi

pruned="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'rrc-*.dump' -mtime +"$RETENTION_DAYS" -print -delete | wc -l)"
log "pruned $pruned local archive(s) older than $RETENTION_DAYS days"
log "done"
