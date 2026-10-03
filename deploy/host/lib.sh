# Shared helpers for the VPS host scripts (spec 0005). Sourced, never run; bash only.
# Not linked into /usr/local/sbin (only kfs-* files are).

KFS_ROOT=${KFS_ROOT:-/opt/kfs}
KFS_ENV_FILE=${KFS_ENV_FILE:-/etc/kfs/kfs.env}
KFS_SECRETS_DIR=${KFS_SECRETS_DIR:-/etc/kfs/secrets}
KFS_LOG_DIR=${KFS_LOG_DIR:-/var/log/kfs}
KFS_STATE_DIR=${KFS_STATE_DIR:-/var/lib/kfs}
# The uid of the `app` user in every image (docker/Dockerfile).
# shellcheck disable=SC2034 # used by the scripts that source this file
KFS_APP_UID=10001

# One JSON log line on stdout: kfs_log <level> <msg> [jq --arg pairs as key value ...]
kfs_log() {
    local level=$1 msg=$2
    shift 2
    # shellcheck disable=SC2016 # a jq filter: the $names are jq variables
    local args=() filter='{ts: $ts, level: $level, process: $process, msg: $msg}'
    while [ $# -ge 2 ]; do
        args+=(--arg "$1" "$2")
        filter="$filter + {\"$1\": \$$1}"
        shift 2
    done
    jq -nc --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg level "$level" \
        --arg process "${KFS_PROCESS:-kfs}" --arg msg "$msg" "${args[@]}" "$filter"
}

# The TRADING_ENV in the env file (demo when unset). The last line wins; never sourced.
kfs_trading_env() {
    local value
    value=$(sed -n 's/^[[:space:]]*TRADING_ENV[[:space:]]*=[[:space:]]*["'\'']\{0,1\}\([a-z]*\).*/\1/p' "$KFS_ENV_FILE" 2>/dev/null | tail -n 1)
    printf '%s\n' "${value:-demo}"
}

# The secret file names the release's Compose files mount, one per line (compose.live.yaml
# only in live). Read from the files, so a missing secret never stops the listing.
kfs_secret_names() {
    local dir=$1 files=("$1/compose.yaml" "$1/compose.vps.yaml")
    if [ "$(kfs_trading_env)" = live ]; then
        files+=("$dir/compose.live.yaml")
    fi
    grep -ho 'SECRETS_DIR[^}]*}/[A-Za-z0-9_]*' "${files[@]}" | sed 's#.*/##' | sort -u
}

kfs_is_sha() {
    [[ $1 =~ ^[0-9a-f]{40}$ ]]
}

# The commit the current release is, or nothing when there is none yet.
kfs_current_sha() {
    local target
    target=$(readlink -f "$KFS_ROOT/current" 2>/dev/null) || return 0
    [ -d "$target" ] && basename "$target"
    return 0
}

# Link every deploy/host/kfs-* of the current release into /usr/local/sbin, drop dangling
# kfs-* links, and install the release's systemd units. Prints one line per change.
kfs_link_host() {
    local src="$KFS_ROOT/current/deploy/host" file name link reload=0
    for file in "$src"/kfs-*; do
        [ -f "$file" ] || continue
        name=$(basename "$file")
        case $name in *.service | *.timer) continue ;; esac
        link="/usr/local/sbin/$name"
        if [ "$(readlink "$link" 2>/dev/null)" != "$src/$name" ]; then
            ln -sfn "$src/$name" "$link"
            echo "linked $link"
        fi
    done
    for link in /usr/local/sbin/kfs-*; do
        if [ -L "$link" ] && [ ! -e "$link" ]; then
            rm -f "$link"
            echo "removed $link"
        fi
    done
    for file in "$src"/*.service "$src"/*.timer; do
        [ -f "$file" ] || continue
        name=$(basename "$file")
        if ! cmp -s "$file" "/etc/systemd/system/$name"; then
            install -m 0644 "$file" "/etc/systemd/system/$name"
            echo "installed $name"
            reload=1
        fi
    done
    if [ "$reload" = 1 ]; then
        systemctl daemon-reload
        for file in "$src"/*.service "$src"/*.timer; do
            [ -f "$file" ] || continue
            name=$(basename "$file")
            systemctl enable --quiet "$name"
            case $name in *.timer) systemctl start "$name" ;; esac
        done
    fi
}
