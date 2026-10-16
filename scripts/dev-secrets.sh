#!/bin/sh
# Creates the laptop's development secret files (spec 0003), one random value each.
# Never overwrites a file, and never creates a Kraken file: Kraken keys exist only on the VPS.
# The Pushover and healthchecks.io files are placeholders: the laptop alerter runs in dry run
# (ALERTS_DELIVERY=log) and never loads them (spec 0013 AC-13).
#
# The folder is 0700 (only you), the files 0444: a Compose file secret without swarm is a
# plain bind mount that keeps the host owner and mode, so the container's app user (uid 10001)
# must be able to read a file your laptop user owns.
#
#   scripts/dev-secrets.sh                 # into ./secrets
#   SECRETS_DIR=/some/dir scripts/dev-secrets.sh
set -eu

dir="${SECRETS_DIR:-./secrets}"
names="
postgres_owner_password
postgres_trader_password
postgres_dashboard_password
postgres_backup_password
postgres_lab_password
postgres_alerter_password
redis_trader_password
redis_dashboard_password
redis_alerter_password
restic_password
b2_key_id
b2_application_key
dashboard_session_key
pushover_app_token
pushover_user_key
hc_alerter_url
"

mkdir -p "$dir"
chmod 0700 "$dir"
if [ ! -w "$dir" ]; then
    echo "cannot write to $dir" >&2
    exit 1
fi

for name in $names; do
    file="$dir/$name"
    if [ -e "$file" ]; then
        echo "kept     $name"
        continue
    fi
    (umask 077 && printf %s "$(openssl rand -hex 32)" >"$file")
    chmod 0444 "$file"
    echo "created  $name"
done

# The dashboard password hash and authenticator seed need the credentials command (spec 0004).
if [ ! -e "$dir/dashboard_password_hash" ] || [ ! -e "$dir/dashboard_totp_seed" ]; then
    echo "next: docker compose build dashboard && scripts/dashboard-credentials.sh --dev --write"
fi
