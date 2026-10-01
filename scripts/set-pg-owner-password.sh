#!/bin/sh
# Sets the Postgres owner role's password to the mounted secret file (spec 0003).
#
# Run it once after the owner password file changes: on rotation, or on an existing data
# volume that was created with an older password (POSTGRES_PASSWORD_FILE only applies when
# the volume is first initialised). It connects inside the postgres container over the local
# socket, which needs no password, and the value travels only through a heredoc on stdin,
# never a command line argument.
#
#   docker compose up -d postgres && scripts/set-pg-owner-password.sh
set -eu

docker compose exec -T postgres sh -s <<'SCRIPT'
set -eu
pw=$(cat /run/secrets/postgres_password)
case "$pw" in
    *"'"* | *\\*)
        echo "the owner password may not contain a quote or a backslash" >&2
        exit 1
        ;;
esac
# A failing statement is never echoed into the server log with the password in it.
psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
SET log_min_error_statement = panic;
SET log_statement = none;
ALTER ROLE "$POSTGRES_USER" PASSWORD '$pw';
SQL
echo "owner password set"
SCRIPT
