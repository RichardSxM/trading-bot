#!/bin/sh
# Backup stub (spec 0003 AC-12): proves the backup service receives its secret files, then idles.
# The real job (nightly downsample, pg_dump, restic to Backblaze B2) arrives with feature 7.
# Fingerprints are the first 8 hex of each value's sha256, never the value.
set -eu

log() {
    printf '{"ts":"%s","level":"%s","process":"backup","logger":"backup","msg":"%s"%s}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "$3"
}

for name in postgres_password restic_password b2_key_id b2_application_key; do
    file="/run/secrets/$name"
    if [ ! -e "$file" ]; then reason=missing; elif [ ! -s "$file" ]; then reason=empty; else reason=; fi
    if [ -n "$reason" ]; then
        log CRITICAL "secret unavailable, refusing to start" ",\"code\":\"secret_unavailable\",\"secret\":\"$name\",\"reason\":\"$reason\""
        exit 1
    fi
    fp=$(printf %s "$(cat "$file")" | sha256sum | cut -c1-8)
    log INFO "secret loaded" ",\"secret\":\"$name\",\"fp\":\"$fp\""
done

log WARNING "backup not configured" ""
exec sleep infinity
