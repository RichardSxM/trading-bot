#!/bin/sh
# Docker Desktop runs every container in one Linux VM with its own clock, and that clock
# falls behind while the Mac sleeps. The nodes then fail their startup clock check
# (MAX_CLOCK_OFFSET_MS). This keeps the VM clock synced, which fixes it for every container.
#
# On a real Linux host a container shares the host's own clock, so setting it here would
# change the machine's time. Refuse unless this is the Docker Desktop VM (a linuxkit kernel).
set -eu

kernel="$(uname -r)"
case "$kernel" in
    *linuxkit*) ;;
    *)
        echo "kfs-clock: kernel $kernel is not the Docker Desktop VM; refusing to set the clock" >&2
        exit 1
        ;;
esac

exec chronyd -d -f /etc/chrony/chrony.conf
