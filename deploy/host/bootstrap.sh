#!/usr/bin/env bash
# Sets up the VPS host (spec 0005 AC-1). Idempotent: every step checks before it changes and
# prints `ok <step>` or `changed <step>`; a run that changes nothing says so at the end.
#
#   first run, from the laptop:  scripts/deploy.sh --bootstrap <public IP> [--data-disk /dev/sdb]
#   later runs, on the box:      sudo /opt/kfs/current/deploy/host/bootstrap.sh [--data-disk /dev/sdb]
#
#   --release <sha>      the release folder to bootstrap from (default: the one /opt/kfs/current points to)
#   --data-disk <dev>    the extra disk for /srv/kfs. Without it the catalog stays on the system
#                        disk; a later run with it moves what is there onto the disk.
#
# Before the first run, paste deploy/tailscale-policy.hujson into the Tailscale admin console
# and create a one time auth key for tag:kfs (you paste it at the prompt; it is never stored).
set -euo pipefail

KFS_PROCESS=bootstrap
HOST_DIR=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
# shellcheck source=deploy/host/lib.sh
. "$HOST_DIR/lib.sh"

HOSTNAME_WANTED=kfs-vps
SWAPFILE=/swapfile
DATA_MOUNT=/srv/kfs
CHANGED=0
RELEASE=""
DATA_DISK=""

ok() { printf 'ok       %s\n' "$1"; }
changed() {
    printf 'changed  %s\n' "$1"
    CHANGED=1
}
die() {
    printf 'bootstrap: %s\n' "$*" >&2
    exit 1
}
usage() {
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 64
}

# put_file <path> <mode> <owner:group> <content>: writes only when different; 0 when it wrote.
put_file() {
    local path=$1 mode=$2 owner=$3 content=$4 tmp
    if [ -f "$path" ] && [ "$(cat "$path")" = "$content" ] &&
        [ "$(stat -c '%a %U:%G' "$path")" = "${mode#0} $owner" ]; then
        return 1
    fi
    mkdir -p "$(dirname "$path")"
    tmp=$(mktemp "$(dirname "$path")/.kfs.XXXXXX")
    printf '%s\n' "$content" >"$tmp"
    chmod "$mode" "$tmp"
    chown "$owner" "$tmp"
    mv -f "$tmp" "$path"
}

installed() {
    [ "$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null)" = "install ok installed" ]
}

apt_install() {
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$@" >/dev/null
}

# ask <prompt>: one line from the terminal, echoed.
ask() {
    local answer
    read -r -p "$1" answer </dev/tty
    printf '%s' "$answer"
}

# 1. Ubuntu 26.04 and root; UTC; host name.
step_system() {
    [ "$(id -u)" -eq 0 ] || die "run as root"
    # shellcheck source=/dev/null
    . /etc/os-release
    [ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 26.04 ] || die "needs Ubuntu 26.04, found ${PRETTY_NAME:-unknown}"
    if [ "$(timedatectl show -p Timezone --value)" = UTC ]; then ok "time zone UTC"; else
        timedatectl set-timezone UTC
        changed "time zone UTC"
    fi
    if [ "$(hostnamectl --static)" = "$HOSTNAME_WANTED" ]; then ok "host name $HOSTNAME_WANTED"; else
        hostnamectl set-hostname "$HOSTNAME_WANTED"
        changed "host name $HOSTNAME_WANTED"
    fi
    # cloud-init would set the provider's host name again at the next boot.
    if [ -d /etc/cloud/cloud.cfg.d ]; then
        if put_file /etc/cloud/cloud.cfg.d/99-kfs.cfg 0644 root:root "preserve_hostname: true"; then
            changed "cloud-init keeps the host name"
        else ok "cloud-init keeps the host name"; fi
    fi
    if ! grep -q "[[:space:]]$HOSTNAME_WANTED\$" /etc/hosts; then
        printf '127.0.1.1 %s\n' "$HOSTNAME_WANTED" >>/etc/hosts
        changed "/etc/hosts entry"
    fi
}

# 2. The kfs admin user: a console password, not in the docker group, sudo only for kfs-*.
step_user() {
    local first second status
    if id kfs >/dev/null 2>&1; then ok "user kfs"; else
        useradd --create-home --shell /bin/bash kfs
        changed "user kfs"
    fi
    status=$(passwd -S kfs | awk '{print $2}')
    if [ "$status" = P ]; then ok "kfs password"; else
        echo "Set the kfs password (for the OVHcloud KVM console; keep it in your password manager)."
        while true; do
            read -r -s -p "kfs password: " first </dev/tty
            echo
            read -r -s -p "again: " second </dev/tty
            echo
            if [ -n "$first" ] && [ "$first" = "$second" ]; then break; fi
            echo "empty or not the same, try again"
        done
        printf 'kfs:%s\n' "$first" | chpasswd
        unset first second
        changed "kfs password"
    fi
    if id -nG kfs | tr ' ' '\n' | grep -qx docker; then
        gpasswd -d kfs docker >/dev/null
        changed "kfs not in the docker group"
    else ok "kfs not in the docker group"; fi
    local rule="kfs ALL=(root) NOPASSWD: /usr/local/sbin/kfs-*" tmp
    if [ -f /etc/sudoers.d/kfs ] && [ "$(cat /etc/sudoers.d/kfs)" = "$rule" ]; then ok "sudoers kfs-* rule"; else
        tmp=$(mktemp)
        printf '%s\n' "$rule" >"$tmp"
        # sudo-rs (the 26.04 default) has visudo too; it allows a wildcard in the command path.
        visudo -c -f "$tmp" >/dev/null || die "sudoers rule did not validate"
        install -m 0440 -o root -g root "$tmp" /etc/sudoers.d/kfs
        rm -f "$tmp"
        changed "sudoers kfs-* rule"
    fi
    sudo -l -U kfs 2>/dev/null | grep -qF '/usr/local/sbin/kfs-*' ||
        die "sudo -l -U kfs does not list /usr/local/sbin/kfs-*; check /etc/sudoers.d/kfs"
}

# 3. Packages: chrony (the 26.04 default; timesyncd stays off), ufw, unattended-upgrades for security only, needrestart list only.
step_packages() {
    local missing=() pkg
    for pkg in chrony ufw unattended-upgrades jq curl; do
        installed "$pkg" || missing+=("$pkg")
    done
    if [ ${#missing[@]} -eq 0 ]; then ok "packages"; else
        apt-get update -q >/dev/null
        apt_install "${missing[@]}"
        changed "packages ${missing[*]}"
    fi
    if systemctl is-enabled systemd-timesyncd >/dev/null 2>&1; then
        systemctl disable --now systemd-timesyncd >/dev/null 2>&1
        changed "timesyncd disabled"
    else ok "timesyncd disabled"; fi
    if systemctl is-active --quiet chrony; then ok "chrony running"; else
        systemctl enable --now chrony >/dev/null 2>&1
        changed "chrony running"
    fi
    # #clear drops the distro's origin list (apt config lists append across files).
    # shellcheck disable=SC2016 # ${distro_id} is apt's variable, written literally
    if put_file /etc/apt/apt.conf.d/52kfs-unattended 0644 root:root '#clear Unattended-Upgrade::Allowed-Origins;
#clear Unattended-Upgrade::Origins-Pattern;
Unattended-Upgrade::Allowed-Origins {
        "${distro_id}:${distro_codename}-security";
};
Unattended-Upgrade::Automatic-Reboot "false";
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";'; then changed "unattended-upgrades security only"; else ok "unattended-upgrades security only"; fi
    if put_file /etc/needrestart/conf.d/90-kfs.conf 0644 root:root "\$nrconf{restart} = 'l';"; then
        changed "needrestart list only"
    else ok "needrestart list only"; fi
}

# 4. 2 GB swap, swappiness 10.
step_swap() {
    if swapon --show=NAME --noheadings | grep -qx "$SWAPFILE"; then ok "swapfile"; else
        if [ ! -f "$SWAPFILE" ]; then
            fallocate -l 2G "$SWAPFILE"
            chmod 0600 "$SWAPFILE"
            mkswap "$SWAPFILE" >/dev/null
        fi
        swapon "$SWAPFILE"
        changed "swapfile"
    fi
    if grep -q "^${SWAPFILE}[[:space:]]" /etc/fstab; then ok "swapfile in fstab"; else
        printf '%s none swap sw 0 0\n' "$SWAPFILE" >>/etc/fstab
        changed "swapfile in fstab"
    fi
    if put_file /etc/sysctl.d/90-kfs.conf 0644 root:root "vm.swappiness=10"; then
        sysctl -q -p /etc/sysctl.d/90-kfs.conf
        changed "swappiness 10"
    else ok "swappiness 10"; fi
}

# 5. The extra disk at /srv/kfs, by UUID, nofail; catalog/ and its marker. Optional.
step_data_disk() {
    if [ -z "$DATA_DISK" ]; then
        if mountpoint -q "$DATA_MOUNT"; then ok "data disk at $DATA_MOUNT"; else
            echo "skipped  data disk: the catalog lives on the system disk until you rerun with --data-disk"
        fi
        ensure_catalog
        return
    fi
    [ -b "$DATA_DISK" ] || die "$DATA_DISK is not a block device"
    local fstype uuid line tmp restart_recorder=0 parked
    fstype=$(blkid -o value -s TYPE "$DATA_DISK" || true)
    if [ -z "$fstype" ]; then
        [ "$(lsblk -n -o NAME "$DATA_DISK" | wc -l)" -eq 1 ] || die "$DATA_DISK has partitions; pass the partition or wipe it yourself"
        lsblk "$DATA_DISK"
        [ "$(ask "Format $DATA_DISK as ext4? Everything on it is lost. Type yes: ")" = yes ] || die "not formatted"
        mkfs.ext4 -q -L kfs-data "$DATA_DISK"
        fstype=ext4
        changed "formatted $DATA_DISK"
    fi
    uuid=$(blkid -o value -s UUID "$DATA_DISK")
    [ -n "$uuid" ] || die "$DATA_DISK has no UUID"
    line="UUID=$uuid $DATA_MOUNT $fstype defaults,nofail,x-systemd.device-timeout=30s 0 2"
    if grep -qxF "$line" /etc/fstab; then ok "data disk in fstab"; else
        sed -i "\#[[:space:]]${DATA_MOUNT}[[:space:]]#d" /etc/fstab
        printf '%s\n' "$line" >>/etc/fstab
        systemctl daemon-reload
        changed "data disk in fstab"
    fi
    if mountpoint -q "$DATA_MOUNT"; then ok "data disk mounted"; else
        # Data written to the system disk before the disk existed moves onto it.
        if [ -d "$DATA_MOUNT" ] && [ -n "$(ls -A "$DATA_MOUNT")" ]; then
            if [ -x /usr/local/sbin/kfs-compose ] && /usr/local/sbin/kfs-compose ps --status running --services 2>/dev/null | grep -qx recorder; then
                /usr/local/sbin/kfs-compose stop recorder
                restart_recorder=1
            fi
            tmp=$(mktemp -d)
            mount "$DATA_DISK" "$tmp"
            cp -a "$DATA_MOUNT/." "$tmp/"
            umount "$tmp"
            rmdir "$tmp"
            parked="$DATA_MOUNT.system-disk.$(date -u +%Y%m%dT%H%M%SZ)"
            mv "$DATA_MOUNT" "$parked"
            echo "         moved the existing catalog onto $DATA_DISK; the old copy is in $parked (delete it once you are happy)"
        fi
        mkdir -p "$DATA_MOUNT"
        mount "$DATA_MOUNT"
        changed "data disk mounted"
    fi
    ensure_catalog
    if [ -f "$DATA_MOUNT/catalog/.kfs-volume" ]; then ok "catalog marker"; else
        touch "$DATA_MOUNT/catalog/.kfs-volume"
        chown "$KFS_APP_UID:$KFS_APP_UID" "$DATA_MOUNT/catalog/.kfs-volume"
        changed "catalog marker"
    fi
    if [ "$restart_recorder" = 1 ]; then
        /usr/local/sbin/kfs-compose start recorder
    fi
}

# The catalog folder, owned by the app user (a Compose created bind source would be root's).
ensure_catalog() {
    if [ -d "$DATA_MOUNT/catalog" ] && [ "$(stat -c %u "$DATA_MOUNT/catalog")" = "$KFS_APP_UID" ]; then
        ok "catalog folder"
    else
        mkdir -p "$DATA_MOUNT/catalog"
        chown "$KFS_APP_UID:$KFS_APP_UID" "$DATA_MOUNT/catalog"
        changed "catalog folder"
    fi
}

# 6. Docker CE and the Compose plugin from Docker's apt repository; live-restore, log rotation.
step_docker() {
    local codename arch
    if installed docker-ce && installed docker-compose-plugin; then ok "docker ce"; else
        # shellcheck source=/dev/null
        codename=$(. /etc/os-release && echo "$VERSION_CODENAME")
        arch=$(dpkg --print-architecture)
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
        chmod a+r /etc/apt/keyrings/docker.asc
        printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' \
            "$arch" "$codename" >/etc/apt/sources.list.d/docker.list
        apt-get update -q >/dev/null
        apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        changed "docker ce"
    fi
    if put_file /etc/docker/daemon.json 0644 root:root '{"live-restore": true, "log-driver": "json-file", "log-opts": {"max-size": "20m", "max-file": "5"}}'; then
        systemctl restart docker
        changed "docker daemon.json"
    else ok "docker daemon.json"; fi
    systemctl is-enabled --quiet docker || systemctl enable --now docker >/dev/null 2>&1
}

# 7. Tailscale: tag:kfs, Tailscale SSH, host name kfs-vps; serve the dashboard on 443.
step_tailscale() {
    local state prefs keyfile codename
    if installed tailscale; then ok "tailscale package"; else
        # shellcheck source=/dev/null
        codename=$(. /etc/os-release && echo "$VERSION_CODENAME")
        curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/$codename.noarmor.gpg" -o /usr/share/keyrings/tailscale-archive-keyring.gpg
        curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/$codename.tailscale-keyring.list" -o /etc/apt/sources.list.d/tailscale.list
        apt-get update -q >/dev/null
        apt_install tailscale
        changed "tailscale package"
    fi
    systemctl is-enabled --quiet tailscaled || systemctl enable --now tailscaled >/dev/null 2>&1
    state=$(tailscale status --json 2>/dev/null | jq -r '.BackendState // ""')
    if [ "$state" = Running ]; then
        prefs=$(tailscale debug prefs)
        if [ "$(jq -r '.RunSSH' <<<"$prefs")" = true ] && [ "$(jq -r '.Hostname' <<<"$prefs")" = "$HOSTNAME_WANTED" ]; then
            ok "tailscale up"
        else
            tailscale set --ssh --hostname="$HOSTNAME_WANTED"
            changed "tailscale ssh and host name"
        fi
        jq -e '.AdvertiseTags // [] | index("tag:kfs")' <<<"$prefs" >/dev/null ||
            echo "warning  this node does not advertise tag:kfs; re-auth it with a tag:kfs key"
    else
        keyfile=$(mktemp /run/kfs-ts-key.XXXXXX)
        chmod 0600 "$keyfile"
        read -r -s -p "Tailscale auth key (one time, tag:kfs; not echoed, never stored): " key </dev/tty
        echo
        printf '%s' "$key" >"$keyfile"
        unset key
        # file: keeps the key out of the process arguments.
        if ! tailscale up --ssh --advertise-tags=tag:kfs --hostname="$HOSTNAME_WANTED" --auth-key="file:$keyfile"; then
            rm -f "$keyfile"
            die "tailscale up failed"
        fi
        rm -f "$keyfile"
        changed "tailscale up"
    fi
    if tailscale serve status --json 2>/dev/null | grep -q 'http://127.0.0.1:8000'; then ok "tailscale serve 443"; else
        tailscale serve --bg --https=443 http://127.0.0.1:8000 >/dev/null
        changed "tailscale serve 443"
    fi
}

ssh_disabled() {
    ! systemctl is-enabled --quiet ssh.socket 2>/dev/null && ! systemctl is-enabled --quiet ssh.service 2>/dev/null &&
        ! systemctl is-active --quiet ssh.service 2>/dev/null
}

# 8. Gate: you prove Tailscale SSH works before OpenSSH goes away.
step_gate() {
    if ssh_disabled; then
        ok "tailscale ssh gate (OpenSSH already off)"
        return
    fi
    echo
    echo "From your laptop, in a new terminal, log in over Tailscale:"
    echo "    tailscale ssh kfs@$HOSTNAME_WANTED"
    echo "Once that works, type confirmed here. Anything else stops before OpenSSH and the firewall change."
    [ "$(ask "> ")" = confirmed ] || die "not confirmed: OpenSSH and the firewall are untouched; fix Tailscale SSH and run again"
    changed "tailscale ssh confirmed"
}

# 9. OpenSSH off; ufw: deny inbound except tailscale0 and Tailscale's UDP port.
step_firewall() {
    if ssh_disabled; then ok "openssh disabled"; else
        # Stopping the service keeps this session: ssh.service kills only its listener.
        local unit
        for unit in ssh.socket ssh.service; do
            # A unit this image does not have is skipped.
            if systemctl cat "$unit" >/dev/null 2>&1; then
                systemctl disable --now "$unit" >/dev/null 2>&1
            fi
        done
        changed "openssh disabled"
    fi
    local status
    status=$(ufw status verbose)
    if grep -q '^Status: active' <<<"$status" &&
        grep -q 'deny (incoming), allow (outgoing)' <<<"$status" &&
        grep -Eq '^Anywhere on tailscale0 +ALLOW IN' <<<"$status" &&
        grep -Eq '^41641/udp +ALLOW IN' <<<"$status"; then
        ok "ufw"
    else
        ufw default deny incoming >/dev/null
        ufw default allow outgoing >/dev/null
        ufw allow in on tailscale0 >/dev/null
        ufw allow 41641/udp >/dev/null
        ufw --force enable >/dev/null
        changed "ufw"
    fi
}

# 10. Folders, the env file, the current release, host commands, units.
step_layout() {
    local spec path mode
    for spec in /etc/kfs:0755 "$KFS_SECRETS_DIR":0700 "$KFS_ROOT":0755 "$KFS_ROOT/releases":0755 \
        "$KFS_LOG_DIR":0755 "$KFS_STATE_DIR":0700; do
        path=${spec%:*}
        mode=${spec##*:}
        if [ -d "$path" ] && [ "$(stat -c '%a %U' "$path")" = "${mode#0} root" ]; then continue; fi
        install -d -m "$mode" -o root -g root "$path"
        changed "folder $path"
    done
    if [ -f "$KFS_LOG_DIR/deploys.jsonl" ]; then :; else
        install -m 0644 -o root -g root /dev/null "$KFS_LOG_DIR/deploys.jsonl"
        changed "deploy log"
    fi
    if [ -f "$KFS_ENV_FILE" ]; then ok "$KFS_ENV_FILE"; else
        install -m 0644 -o root -g root "$KFS_ROOT/releases/$RELEASE/deploy/kfs.env.example" "$KFS_ENV_FILE"
        changed "$KFS_ENV_FILE (from deploy/kfs.env.example)"
    fi
    # A deployed release stays current; bootstrap only sets it the first time.
    if [ -e "$KFS_ROOT/current" ]; then ok "current release $(kfs_current_sha | cut -c1-12)"; else
        ln -sfn "releases/$RELEASE" "$KFS_ROOT/current.new"
        mv -T "$KFS_ROOT/current.new" "$KFS_ROOT/current"
        changed "current release ${RELEASE:0:12}"
    fi
    local links
    links=$(kfs_link_host)
    if [ -z "$links" ]; then ok "host commands and units"; else
        changed "host commands and units: $(paste -sd ',' - <<<"$links")"
    fi
}

# 11. Baseline: TCP connect time to Kraken, clock offset. Changes nothing.
step_baseline() {
    local times median t
    times=$(for _ in $(seq 20); do
        # A failed try counts as 5 s (curl prints 0 for a connect that never happened).
        t=$(curl -s -o /dev/null -m 5 -w '%{time_connect}' https://futures.kraken.com/) || t=5
        echo "$t"
    done | sort -n)
    median=$(sed -n '10p;11p' <<<"$times" | awk '{s += $1} END {printf "%.1f", s / NR * 1000}')
    echo "baseline futures.kraken.com:443 median TCP connect ${median} ms (20 tries)"
    chronyc tracking | grep -E '^(Reference ID|System time|Leap status)' | sed 's/^/baseline /'
}

while [ $# -gt 0 ]; do
    case $1 in
        --release)
            [ $# -ge 2 ] || usage
            RELEASE=$2
            shift 2
            ;;
        --data-disk)
            [ $# -ge 2 ] || usage
            DATA_DISK=$2
            shift 2
            ;;
        *) usage ;;
    esac
done
[ -n "$RELEASE" ] || RELEASE=$(kfs_current_sha)
[ -n "$RELEASE" ] || die "pass --release <sha> (no /opt/kfs/current yet)"
kfs_is_sha "$RELEASE" || die "not a full commit sha: $RELEASE"
[ -f "$KFS_ROOT/releases/$RELEASE/compose.yaml" ] || die "no release bundle at $KFS_ROOT/releases/$RELEASE"

step_system
step_user
step_packages
step_swap
step_data_disk
step_docker
step_tailscale
step_gate
step_firewall
step_layout
step_baseline

echo
if [ "$CHANGED" = 0 ]; then
    echo "bootstrap: nothing changed"
else
    echo "bootstrap: done. Next, still on the box:"
    echo "    sudo /opt/kfs/current/deploy/host/place-secrets.sh"
    echo "then from the laptop: scripts/deploy.sh"
fi
