#!/bin/sh
# Starts Redis with one ACL user per service (spec 0003).
#
# Runs as root (the image only drops privileges when its first argument is redis-server), so
# it can read the mounted password files whatever their owner. It refuses to start when a
# file is missing, empty, too short, or has whitespace, so a missing file never becomes an
# empty password. Only sha256 hashes are written, to an ACL file on a tmpfs; no password
# ever appears in a process argument. Then it hands over to the image's own entrypoint,
# which runs Redis as the unprivileged `redis` user.
set -eu

acl=/run/redis-acl/users.acl

refuse() {
    printf '{"ts":"%s","level":"CRITICAL","process":"redis","logger":"redis-acl","msg":"secret unavailable, refusing to start","code":"secret_unavailable","secret":"%s","reason":"%s"}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" >&2
    exit 1
}

# Prints the sha256 of /run/secrets/<name> (trailing newline trimmed), or refuses.
hash_of() {
    file="/run/secrets/$1"
    [ -e "$file" ] || refuse "$1" missing
    [ -f "$file" ] || refuse "$1" not_a_file
    value=$(cat "$file") || refuse "$1" unreadable
    [ -n "$value" ] || refuse "$1" empty
    case "$value" in *[[:space:]]*) refuse "$1" whitespace ;; esac
    [ "${#value}" -ge 16 ] || refuse "$1" too_short
    printf %s "$value" | sha256sum | cut -d ' ' -f 1
}

trader=$(hash_of redis_trader_password)
dashboard=$(hash_of redis_dashboard_password)

# default: no login, PING only (the healthcheck and the apps' startup reachability probe).
# trader: everything except admin commands and flushes (spec 0001: a FLUSHDB wipes both envs).
# dashboard: read snapshot keys, append to the control stream, nothing else.
umask 077
cat >"$acl" <<ACL
user default on nopass resetkeys resetchannels -@all +ping
user trader on #$trader ~* &* +@all -@admin -flushall -flushdb
user dashboard on #$dashboard resetkeys resetchannels %R~*:snapshot:* %W~*:control -@all +@read +xadd +ping +hello +auth +client|setname +client|setinfo
ACL
chown redis:redis "$acl"
chmod 0400 "$acl"

exec docker-entrypoint.sh redis-server --aclfile "$acl" "$@"
