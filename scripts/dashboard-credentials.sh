#!/bin/sh
# Makes the dashboard login files (spec 0004): an argon2id password hash, an authenticator
# seed (shown as a QR code), and a session key. Runs in the dashboard image with no secrets
# mounted, so it works before they exist. Never overwrites a file. Runs as your own uid, so
# it can write into a secrets folder you own.
#
#   scripts/dashboard-credentials.sh                  # ask for a password, print only
#   scripts/dashboard-credentials.sh --write          # also write the files into ${SECRETS_DIR:-./secrets}
#   scripts/dashboard-credentials.sh --dev --write    # laptop: random password, printed once
#
# On the VPS the folder belongs to root, so run `sudo scripts/dashboard-credentials.sh --write`,
# then chown 10001 and chmod 0400 the three files, as spec 0003 does.
set -eu
exec docker compose run --rm -it --user "$(id -u):$(id -g)" dashboard-credentials python -m dashboard.credentials "$@"
