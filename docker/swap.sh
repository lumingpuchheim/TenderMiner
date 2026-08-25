#!/bin/sh
# Swap for the heavy jobs — host setup, idempotent, safe to re-run.
#
# Why this exists: the server has 8 GB of RAM and shipped with no swap at
# all. On 2026-08-23 the kernel's OOM killer ended a five-hour replay one
# cutoff from the end (5.2 GB resident), and its 19:23 global kill picked a
# victim by score — next time that could be app.py mid-request or the cycle
# mid-write. The replay's peak is a transient at the last cutoffs; swap turns
# "killed after five hours" into "a slow final half-hour". vm.swappiness=10
# keeps the kernel from swapping proactively: this is a safety margin, not
# extra RAM, and a job that *lives* in swap is a job the box is too small
# for (doc/MEMORY_BUDGET.md).
#
#   sudo sh docker/swap.sh            # create + enable a 4 GB swapfile
#   sudo sh docker/swap.sh 8          # a different size in GB (only if absent)
#   sh docker/swap.sh status          # what is active; no root needed
#
# Re-running is a no-op when the swapfile is active. Undo:
#   sudo swapoff /swapfile && sudo rm /swapfile
#   (and remove its /etc/fstab line and /etc/sysctl.d/99-tendermining-swap.conf)
set -eu

SWAPFILE=/swapfile
SYSCTL=/etc/sysctl.d/99-tendermining-swap.conf

if [ "${1:-}" = "status" ]; then
    swapon --show 2>/dev/null || echo "[swap] none active"
    free -h
    exit 0
fi

SIZE_GB="${1:-4}"

if swapon --show=NAME --noheadings 2>/dev/null | grep -qx "$SWAPFILE"; then
    echo "[swap] $SWAPFILE already active — nothing to do"
    free -h
    exit 0
fi

if [ ! -f "$SWAPFILE" ]; then
    echo "[swap] creating ${SIZE_GB}G $SWAPFILE"
    # fallocate is instant; dd is the fallback for filesystems without it
    fallocate -l "${SIZE_GB}G" "$SWAPFILE" 2>/dev/null \
        || dd if=/dev/zero of="$SWAPFILE" bs=1M count=$((SIZE_GB * 1024)) status=none
fi
chmod 600 "$SWAPFILE"
# not active (checked above), so a stale or half-made file is safe to format
mkswap "$SWAPFILE" >/dev/null
swapon "$SWAPFILE"

# survive a reboot: fstab for the file, sysctl.d for the swappiness
if ! grep -q "^$SWAPFILE " /etc/fstab; then
    echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
fi
echo "vm.swappiness=10" > "$SYSCTL"
sysctl -q -p "$SYSCTL"

echo "[swap] active:"
free -h
