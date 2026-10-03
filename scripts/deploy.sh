#!/usr/bin/env bash
# Deploy a tested commit to the VPS (spec 0005), from the laptop.
#
#   scripts/deploy.sh [sha]                    # default: origin/main
#   scripts/deploy.sh --bootstrap <public IP> [--data-disk <device>] [--user ubuntu|root]
#
# A deploy streams the release bundle of <sha> over Tailscale SSH (kfs@$KFS_HOST, default
# kfs-vps) and runs `kfs-deploy run` there. The images come from CI (GHCR), so the commit
# must be on origin/main and its CI images job must have passed. Exit codes come from the
# VPS: 0 deployed, 1 unhealthy, 3 another deploy is running, 4 refused before any change.
#
# --bootstrap is the first run on a fresh Ubuntu 24.04 VPS, over OVHcloud's public SSH. It
# streams the bundle of origin/main and runs deploy/host/bootstrap.sh there with a terminal.
# Paste deploy/tailscale-policy.hujson into the Tailscale admin console first. Once
# bootstrap has disabled OpenSSH, run it again on the box instead:
#   sudo /opt/kfs/current/deploy/host/bootstrap.sh [--data-disk <device>]
set -euo pipefail

# The files a release needs on the VPS. No app code: that lives in the images.
BUNDLE=(compose.yaml compose.vps.yaml compose.live.yaml docker/redis docker/backup deploy)
KFS_HOST=${KFS_HOST:-kfs-vps}

die() {
    echo "deploy: $*" >&2
    exit 1
}

usage() {
    sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 64
}

cd "$(git rev-parse --show-toplevel)"

# The full sha of $1 (default origin/main), refused unless it is on origin/main.
resolve() {
    local ref=${1:-origin/main} sha
    git fetch --quiet origin
    sha=$(git rev-parse --verify --quiet "$ref^{commit}") || die "unknown commit $ref"
    git merge-base --is-ancestor "$sha" origin/main || die "$sha is not on origin/main; only CI built commits deploy"
    echo "$sha"
}

bootstrap() {
    local ip="" disk=() user=ubuntu sha
    while [ $# -gt 0 ]; do
        case $1 in
            --data-disk)
                [ $# -ge 2 ] || usage
                disk=(--data-disk "$2")
                shift 2
                ;;
            --user)
                [ $# -ge 2 ] || usage
                user=$2
                shift 2
                ;;
            -*) usage ;;
            *)
                [ -z "$ip" ] || usage
                ip=$1
                shift
                ;;
        esac
    done
    [ -n "$ip" ] || usage
    sha=$(resolve origin/main)
    echo "bootstrap: sending release ${sha:0:12} to $user@$ip"
    # shellcheck disable=SC2029 # $sha expands here on purpose (a checked 40 hex commit)
    git archive --format=tar "$sha" "${BUNDLE[@]}" |
        ssh "$user@$ip" "sudo mkdir -p /opt/kfs/releases/$sha && sudo tar -x --no-same-owner -C /opt/kfs/releases/$sha"
    # ${disk[@]+...}: macOS bash 3.2 treats an empty array as unset under set -u.
    exec ssh -t "$user@$ip" sudo "/opt/kfs/releases/$sha/deploy/host/bootstrap.sh" --release "$sha" ${disk[@]+"${disk[@]}"}
}

deploy() {
    local sha
    [ $# -le 1 ] || usage
    case ${1:-} in -*) usage ;; esac
    command -v tailscale >/dev/null || die "tailscale CLI not found"
    sha=$(resolve "${1:-}")
    echo "deploy: sending release ${sha:0:12} to kfs@$KFS_HOST"
    git archive --format=tar "$sha" "${BUNDLE[@]}" |
        tailscale ssh "kfs@$KFS_HOST" sudo /usr/local/sbin/kfs-deploy receive "$sha"
    # Its exit code is the deploy's; pass it through.
    exec tailscale ssh "kfs@$KFS_HOST" sudo /usr/local/sbin/kfs-deploy run "$sha"
}

case ${1:-} in
    --bootstrap)
        shift
        bootstrap "$@"
        ;;
    -h | --help) usage ;;
    *) deploy "$@" ;;
esac
