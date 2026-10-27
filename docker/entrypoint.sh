#!/bin/sh
set -eu
if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
    echo "Applying database migrations..."
    alembic upgrade head
fi
exec "$@"
