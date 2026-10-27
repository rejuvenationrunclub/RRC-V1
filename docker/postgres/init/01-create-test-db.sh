#!/bin/sh
# Runs once, on first initialization of the pgdata volume.
# Creates a separate database for the automated test suite so tests never touch dev data.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
	CREATE DATABASE ${POSTGRES_DB}_test OWNER "$POSTGRES_USER";
SQL
