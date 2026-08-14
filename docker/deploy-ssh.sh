#!/usr/bin/env bash
#
# The forced command behind the CI deploy key — doc/OPERATIONS.md 2.
#
# The GitHub Action holds a private key whose public half sits in the server's
# ~/.ssh/authorized_keys prefixed with:
#
#   command="bash /home/debian/TenderMiner/docker/deploy-ssh.sh",no-pty,\
#   no-agent-forwarding,no-port-forwarding,no-X11-forwarding ssh-ed25519 AAAA...
#
# That prefix is the security boundary and it is the reason this file exists. A
# plain deploy key would give anyone who obtains the GitHub secret a root-capable
# shell on the VPS (the deploy user is in the `docker` group, and the docker
# group can mount the host filesystem into a container). With the forced command,
# the key can run exactly three things and nothing else — whatever the client
# asks for is in SSH_ORIGINAL_COMMAND, and sshd runs THIS instead.
#
# Test it the way an attacker would, from the laptop that holds the key:
#   ssh -i deploy_key debian@HOST 'cat /etc/shadow'   -> refused, exit 2

set -euo pipefail

# Self-locating, like deploy.sh: the checkout is wherever this file lives,
# which is what lets bootstrap.sh write one authorized_keys line that stays
# correct whatever the clone directory is called.
REPO="${TM_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cmd="${SSH_ORIGINAL_COMMAND:-deploy}"

case "$cmd" in
    deploy|rollback|status) ;;
    *) printf 'refused: %s\n' "$cmd" >&2
       printf 'this key may run: deploy | rollback | status\n' >&2
       exit 2 ;;
esac

cd "$REPO" || { printf 'no checkout at %s\n' "$REPO" >&2; exit 1; }

if [ "$cmd" = deploy ]; then
    # Hard reset, deliberately: this checkout is a deployment artifact, not a
    # workspace. Nobody edits here, so there is nothing to preserve — and a
    # deploy that quietly merged local changes would build something that
    # exists in no commit, which is the one thing the git-SHA tag must never
    # be able to lie about.
    git fetch --quiet origin master
    git reset --hard --quiet origin/master
    printf '[deploy-ssh] checkout now at %s\n' "$(git rev-parse --short HEAD)"
fi

exec bash docker/deploy.sh "$cmd"
