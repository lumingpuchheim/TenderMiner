#!/usr/bin/env bash
#
# Host hardening — the part of doc/OPERATIONS.md 4 that is about the machine,
# not the containers. Runs ON THE SERVER as root, idempotent, called by
# docker/bootstrap.sh (step 1b) or by hand:
#
#   sudo bash docker/harden.sh
#
# Written after the audit of 2026-08-15, which found the freshly bootstrapped
# OVH box in this state: SSH password login effectively ON (the `no` in
# sshd_config was dead — cloud-init's drop-in said `yes` and Include is read
# first, first value wins), the deploy user with a password set, passwordless
# sudo and the docker group (= root), 9,846 failed logins in two days, no
# fail2ban, no firewall (`iptables -P INPUT ACCEPT`), a pending reboot. One
# guessed password would have been root plus the whole state directory.
#
# What this does, and why each line is safe to re-run:
#   1. sshd: keys only, no root login. A drop-in named 00-* so it sorts BEFORE
#      50-cloud-init.conf — sshd takes the first value it meets, so a 99-*
#      file would be silently ignored (verified live: it was).
#   2. the deploy user's password is locked. Key auth and NOPASSWD sudo do not
#      need it; a password nobody uses is only something to be guessed.
#   3. fail2ban with the sshd jail — cheap, and it cuts the log noise so a
#      real event is visible in journalctl.
#   4. ufw: deny incoming, allow 22/80/443 (443/udp for HTTP/3). Docker's
#      published ports bypass ufw (they live in the FORWARD chain, which is why
#      the app must stay bound to 127.0.0.1 — docker-compose.yml, TM_APP_BIND)
#      but every host service, and every future accident on the host, is now
#      closed by default. Container egress and the edge's forwarding were
#      verified unaffected after enabling.
#   5. unattended-upgrades on, so the host patches itself.
#
# What it deliberately does not do: restrict 22 to one source IP (a laptop on
# a changing address would lock the operator out), or reboot (that is the
# operator's call; it prints when one is pending).

set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo bash $0" >&2; exit 1; }
DUSER="${1:-${SUDO_USER:-debian}}"

say() { printf '[harden] %s\n' "$*"; }

# 1. sshd ------------------------------------------------------------------
DROPIN=/etc/ssh/sshd_config.d/00-tm-hardening.conf
printf '%s\n' \
    'PasswordAuthentication no' \
    'KbdInteractiveAuthentication no' \
    'PermitRootLogin no' \
    'MaxAuthTries 4' > "$DROPIN"
sshd -t
systemctl reload ssh 2>/dev/null || systemctl reload sshd
# Trust the effective config, not the file: this is the check that caught the
# dead `no` in the first place.
eff="$(sshd -T | awk '$1=="passwordauthentication"{print $2}')"
[ "$eff" = no ] || { echo "sshd still reports passwordauthentication=$eff" >&2; exit 1; }
say "sshd: password login off, root login off (effective, per sshd -T)"

# 2. the deploy user's password ---------------------------------------------
if id "$DUSER" >/dev/null 2>&1; then
    passwd -l "$DUSER" >/dev/null
    say "user $DUSER: password locked (key + sudo unaffected)"
fi

# 3–5. packages --------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq fail2ban ufw unattended-upgrades >/dev/null
systemctl enable --now fail2ban >/dev/null 2>&1
systemctl enable --now unattended-upgrades >/dev/null 2>&1
say "fail2ban: $(systemctl is-active fail2ban); unattended-upgrades: $(systemctl is-active unattended-upgrades)"

# 4. ufw ---------------------------------------------------------------------
# Allow rules first, enable last: enabling with 22 closed ends the session
# that is doing the enabling. `--force` skips the interactive confirmation.
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
for r in 22/tcp 80/tcp 443/tcp 443/udp; do ufw allow "$r" >/dev/null; done
ufw --force enable >/dev/null
say "ufw: $(ufw status | head -1); inbound 22, 80, 443 only"

if [ -f /var/run/reboot-required ]; then
    say "NOTE: a reboot is pending (/var/run/reboot-required); containers"
    say "      come back by themselves (restart policies) — reboot when quiet."
fi
say "done"
