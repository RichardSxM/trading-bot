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
# The same line is appended to $KFS_LOG_DIR/host.jsonl, which the alerter follows (spec 0013);
# a failed append never fails the script, since the journal still has the stdout line.
kfs_log() {
    local level=$1 msg=$2 line
    shift 2
    # shellcheck disable=SC2016 # a jq filter: the $names are jq variables
    local args=() filter='{ts: $ts, level: $level, process: $process, msg: $msg}'
    while [ $# -ge 2 ]; do
        args+=(--arg "$1" "$2")
        filter="$filter + {\"$1\": \$$1}"
        shift 2
    done
    line=$(jq -nc --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg level "$level" \
        --arg process "${KFS_PROCESS:-kfs}" --arg msg "$msg" "${args[@]}" "$filter") || return
    # Never fatal: a deploy whose terminal side is gone keeps going (kfs-deploy ignores PIPE).
    printf '%s\n' "$line" 2>/dev/null || true
    if [ -d "$KFS_LOG_DIR" ] && [ -w "$KFS_LOG_DIR" ]; then
        (umask 022 && printf '%s\n' "$line" >>"$KFS_LOG_DIR/host.jsonl") 2>/dev/null || true
    fi
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
    # host.jsonl rotation (spec 0013 Build plan 3.1); logrotate reads the folder on its own timer.
    if [ -f "$src/logrotate-kfs" ] && ! cmp -s "$src/logrotate-kfs" /etc/logrotate.d/kfs; then
        install -m 0644 "$src/logrotate-kfs" /etc/logrotate.d/kfs
        echo "installed /etc/logrotate.d/kfs"
    fi
    if [ "$reload" = 1 ]; then
        systemctl daemon-reload
        for file in "$src"/*.service "$src"/*.timer; do
            [ -f "$file" ] || continue
            # A unit a timer starts (kfs-autoheal.service) has no [Install] and is not enabled.
            grep -q '^\[Install\]' "$file" || continue
            name=$(basename "$file")
            systemctl enable --quiet "$name"
            case $name in *.timer) systemctl start "$name" ;; esac
        done
    fi
}

# The dashboard image's control CLI (spec 0005), from the release in KFS_CONTROL_RELEASE
# (default: the current one). One JSON line on stdout; its exit code is the CLI's.
kfs_control() {
    local release=${KFS_CONTROL_RELEASE:-$(kfs_current_sha)}
    KFS_RELEASE=$release "$KFS_ROOT/releases/$release/deploy/host/kfs-compose" \
        run --rm --no-deps -T dashboard python -m dashboard.control_cli "$@"
}

# 0 when every service of the current release is healthy (migrate: exited 0); prints the ones
# that are not. One off `kfs-compose run` containers (kfs_control, the soak's samples) share
# their service's name and health check but serve nothing, so they are left out.
kfs_services_healthy() {
    local compose="$KFS_ROOT/current/deploy/host/kfs-compose" ps expected bad
    ps=$("$compose" ps -a --format json | jq -sc 'if length == 1 and (.[0] | type) == "array" then .[0] else . end
        | map(select((.Labels // "") | split(",") | index("com.docker.compose.oneoff=True") | not))')
    expected=$("$compose" config --services)
    bad=$(jq -r --arg expected "$expected" '
        ($expected | split("\n") | map(select(. != ""))) as $want
        | (map({key: .Service, value: .}) | from_entries) as $by
        | $want[]
        | . as $s
        | $by[$s] as $c
        | if $c == null then "\($s): no container"
          elif $s == "migrate" then (if $c.State == "exited" and $c.ExitCode == 0 then empty else "\($s): \($c.State) exit \($c.ExitCode)" end)
          elif ($c.Health // "") != "" then (if $c.Health == "healthy" then empty else "\($s): \($c.Health)" end)
          elif $c.State == "running" then empty
          else "\($s): \($c.State)" end' <<<"$ps")
    if [ -n "$bad" ]; then
        echo "$bad" | paste -sd ' ' -
        return 1
    fi
}

# Return codes of kfs_pause_and_wait; KFS_DRAIN_REASON says why in words.
# shellcheck disable=SC2034 # used by the scripts that source this file
KFS_DRAIN_NO_STATE=10 KFS_DRAIN_PAUSE_FAILED=11 KFS_DRAIN_PAUSE_LIFTED=12 KFS_DRAIN_NOT_FLAT=13 KFS_DRAIN_REASON=""
KFS_PAUSED_BY_US=false
KFS_WAITED_S=0
KFS_PAUSE_RESULT_S=30

# Unpause only a pause we sent (AC-11), and only while it is still ours: a Pause pressed on the
# dashboard during the drain takes the pause over (`pause_source` becomes `dashboard`), and that
# one stays. Best effort: a failure is logged, and the trader stays paused, which is safe and
# shows on the dashboard.
kfs_unpause_if_ours() {
    [ "$KFS_PAUSED_BY_US" = true ] || return 0
    local state source
    if ! state=$(kfs_control state); then
        kfs_log ERROR "trader_state unreadable, the trader stays paused; press Resume on the dashboard"
        return 0
    fi
    # Not paused (yet) still gets the unpause: a pause that was not confirmed may still be queued,
    # and the stream keeps order, so the unpause lands after it. Unpausing an active trader is a
    # no-op.
    source=$(jq -r '.data.pause_source // ""' <<<"$state")
    if [ "$(jq -r '.data.paused // false' <<<"$state")" = true ] && [ "$source" != deploy ]; then
        kfs_log INFO "pause taken over, leaving it in place" source "$source"
        return 0
    fi
    if kfs_control unpause --source deploy --wait-result "$KFS_PAUSE_RESULT_S" >/dev/null; then
        kfs_log INFO "trader unpaused"
    else
        kfs_log ERROR "unpause failed, the trader stays paused; press Resume on the dashboard"
    fi
}

# kfs_pause_and_wait <now: true|false> <wait: 15m>
# Pause entries (source deploy) unless already paused, then wait until the trader holds no
# position and no entry order (AC-8). 0 when flat, or at once with now=true. Otherwise one of
# the KFS_DRAIN_* codes, with any pause we sent already lifted again (a lifted pause sends
# nothing). Sets KFS_PAUSED_BY_US and KFS_WAITED_S.
# shellcheck disable=SC2034 # KFS_DRAIN_REASON is read by the scripts that source this file
kfs_pause_and_wait() {
    local now=$1 wait=$2 state paused rc start
    KFS_PAUSED_BY_US=false
    KFS_WAITED_S=0
    KFS_DRAIN_REASON=""
    if state=$(kfs_control state); then
        paused=$(jq -r '.data.paused // false' <<<"$state")
    elif [ "$now" = true ]; then
        kfs_log WARNING "trader_state unreadable; --now sends the pause anyway"
        paused=false
    else
        # Includes a running release from before spec 0005, which publishes none (fail closed).
        KFS_DRAIN_REASON="trader_state missing or stale; use --now to skip the drain"
        return "$KFS_DRAIN_NO_STATE"
    fi
    if [ "$paused" = true ]; then
        kfs_log INFO "trader already paused, leaving that pause in place"
    elif kfs_control pause --source deploy --wait-result "$KFS_PAUSE_RESULT_S" >/dev/null; then
        KFS_PAUSED_BY_US=true
        kfs_log INFO "trader paused" source deploy
    elif [ "$now" = true ]; then
        # It may still execute later; unpause afterwards all the same.
        KFS_PAUSED_BY_US=true
        kfs_log WARNING "pause not confirmed; --now goes on"
    else
        KFS_PAUSED_BY_US=true
        kfs_unpause_if_ours
        KFS_DRAIN_REASON="the trader did not confirm the pause within ${KFS_PAUSE_RESULT_S}s"
        return "$KFS_DRAIN_PAUSE_FAILED"
    fi
    if [ "$now" = true ]; then
        kfs_log WARNING "--now: not waiting for flat"
        return 0
    fi
    start=$SECONDS
    rc=0
    kfs_control wait-flat --timeout "$wait" >/dev/null || rc=$?
    KFS_WAITED_S=$((SECONDS - start))
    case $rc in
        0)
            kfs_log INFO "trader flat" waited_s "$KFS_WAITED_S"
            return 0
            ;;
        3)
            KFS_DRAIN_REASON="the pause was lifted during the wait (Resume pressed)"
            return "$KFS_DRAIN_PAUSE_LIFTED"
            ;;
        2)
            kfs_unpause_if_ours
            KFS_DRAIN_REASON="not flat within $wait"
            return "$KFS_DRAIN_NOT_FLAT"
            ;;
        *)
            kfs_unpause_if_ours
            KFS_DRAIN_REASON="trader_state missing or stale during the wait"
            return "$KFS_DRAIN_NO_STATE"
            ;;
    esac
}
