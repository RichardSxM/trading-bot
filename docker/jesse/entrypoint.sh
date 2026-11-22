#!/bin/sh
# Writes Jesse's .env inside the container from the mounted secret (spec 0033 AC-11), never on the host
# and never in the image, then runs the command. The dashboard password and the MCP token derive from it.
set -eu

secret=/run/secrets/jesse_password
if [ ! -r "$secret" ]; then
    echo "jesse: no readable secret at $secret (run scripts/dev-secrets.sh)" >&2
    exit 78
fi
project=/home/jesse/project
mkdir -p "$project/strategies" "$project/storage"
umask 077
password=$(cat "$secret")
cat >"$project/.env" <<ENV
PASSWORD=$password
APP_PORT=9000
POSTGRES_HOST=jesse-postgres
POSTGRES_NAME=jesse_db
POSTGRES_PORT=5432
POSTGRES_USERNAME=jesse_user
POSTGRES_PASSWORD=$password
TIMESCALEDB_ENABLED=TRUE
ENV
exec "$@"
