#!/usr/bin/env bash
# Places the secret files on the VPS (spec 0005 AC-2, replacing spec 0003's manual steps).
#
#   sudo /opt/kfs/current/deploy/host/place-secrets.sh                 # ask for every missing secret
#   sudo /opt/kfs/current/deploy/host/place-secrets.sh --rotate <name> # replace one
#
# Asks, without echo, for each secret the Compose files name that is missing from
# /etc/kfs/secrets, plus ghcr_token (a classic GitHub PAT with only read:packages, expiring
# within a year; used by `docker login` on the host and mounted into no container). Writes
# each as owner 10001, mode 0400 (ghcr_token: root, 0400) and prints its name and the first 8
# hex of its sha256, never the value. Never overwrites a file unless you pass --rotate.
#
# The three dashboard login files are not typed in: when one is missing, the dashboard image
# of the current release makes them (`python -m dashboard.credentials --write`, no network)
# and shows the QR code for your authenticator app. To replace the login, delete all three
# and run this again.
set -euo pipefail

KFS_PROCESS=place-secrets
HOST_DIR=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
# shellcheck source=deploy/host/lib.sh
. "$HOST_DIR/lib.sh"

GHCR_USER=richardsxm
DASHBOARD_FILES=(dashboard_password_hash dashboard_totp_seed dashboard_session_key)
ROTATE=""

die() {
    printf 'place-secrets: %s\n' "$*" >&2
    exit 1
}

usage() {
    sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 64
}

while [ $# -gt 0 ]; do
    case $1 in
        --rotate)
            [ $# -ge 2 ] || usage
            ROTATE=$2
            shift 2
            ;;
        *) usage ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
RELEASE=$(kfs_current_sha)
[ -n "$RELEASE" ] || die "no /opt/kfs/current yet; run bootstrap.sh first"
RELEASE_DIR="$KFS_ROOT/releases/$RELEASE"
install -d -m 0700 -o root -g root "$KFS_SECRETS_DIR"

is_dashboard_file() {
    local name
    for name in "${DASHBOARD_FILES[@]}"; do [ "$1" = "$name" ] && return 0; done
    return 1
}

# Every secret to type in: what the Compose files name, minus the dashboard login, plus ghcr_token.
typed_names() {
    {
        kfs_secret_names "$RELEASE_DIR"
        echo ghcr_token
    } | while read -r name; do
        is_dashboard_file "$name" || echo "$name"
    done | sort -u
}

place() {
    local name=$1 value again tmp owner
    while true; do
        read -r -s -p "$name: " value </dev/tty
        echo
        if [ -z "$value" ]; then
            echo "refused: empty"
        elif [[ $value =~ [[:space:]] ]]; then
            echo "refused: contains whitespace"
        else
            read -r -s -p "$name again: " again </dev/tty
            echo
            [ "$value" = "$again" ] && break
            echo "refused: not the same"
        fi
    done
    owner=$KFS_APP_UID
    [ "$name" = ghcr_token ] && owner=0
    tmp=$(mktemp "$KFS_SECRETS_DIR/.incoming.XXXXXX")
    printf '%s' "$value" >"$tmp"
    chown "$owner:$owner" "$tmp"
    chmod 0400 "$tmp"
    mv -f "$tmp" "$KFS_SECRETS_DIR/$name"
    printf 'placed   %s fp=%s\n' "$name" "$(printf '%s' "$value" | sha256sum | cut -c1-8)"
    unset value again
}

names=$(typed_names)
if [ -n "$ROTATE" ]; then
    grep -qx "$ROTATE" <<<"$names" || die "unknown secret $ROTATE (known: $(paste -sd ' ' - <<<"$names"))"
    place "$ROTATE"
else
    while read -r name; do
        if [ -e "$KFS_SECRETS_DIR/$name" ]; then
            printf 'kept     %s fp=%s\n' "$name" "$(sha256sum <"$KFS_SECRETS_DIR/$name" | cut -c1-8)"
        else
            place "$name"
        fi
    done <<<"$names"
fi

# GHCR login for root's Docker (the deploy pulls with it). The token goes through stdin only.
if docker login ghcr.io -u "$GHCR_USER" --password-stdin <"$KFS_SECRETS_DIR/ghcr_token" >/dev/null 2>&1; then
    echo "ok       docker login ghcr.io"
else
    die "docker login ghcr.io failed; check ghcr_token (--rotate ghcr_token)"
fi

missing=0
for name in "${DASHBOARD_FILES[@]}"; do
    [ -e "$KFS_SECRETS_DIR/$name" ] || missing=1
done
if [ "$missing" = 1 ]; then
    prefix=$(sed -n 's/^[[:space:]]*KFS_IMAGE_PREFIX[[:space:]]*=[[:space:]]*//p' "$KFS_ENV_FILE" | tail -n 1)
    [ -n "$prefix" ] || die "KFS_IMAGE_PREFIX is not set in $KFS_ENV_FILE"
    image="${prefix}dashboard:$RELEASE"
    echo "Making the dashboard login with $image. Choose a strong password; keep it and the seed in your password manager."
    docker pull --quiet "$image" >/dev/null || die "cannot pull $image (has CI built $RELEASE?)"
    # Root inside, so it can write into the root only folder; it never overwrites a file.
    docker run --rm -it --network none --user 0:0 -v "$KFS_SECRETS_DIR:/out" "$image" \
        python -m dashboard.credentials --write
    for name in "${DASHBOARD_FILES[@]}"; do
        chown "$KFS_APP_UID:$KFS_APP_UID" "$KFS_SECRETS_DIR/$name"
        chmod 0400 "$KFS_SECRETS_DIR/$name"
    done
else
    echo "ok       dashboard login files"
fi

# Every file the Compose files name now exists, so the first deploy can start.
while read -r name; do
    [ -s "$KFS_SECRETS_DIR/$name" ] || die "still missing: $name"
done < <(kfs_secret_names "$RELEASE_DIR")
echo "place-secrets: every secret is in place"
