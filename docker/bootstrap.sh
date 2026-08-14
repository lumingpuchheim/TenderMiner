#!/usr/bin/env bash
#
# TenderMining new-server bootstrap — doc/OPERATIONS.md 4, scripted.
#
# The once-per-server step that wires a fresh Debian box into the push-to-deploy
# loop. Run it from any machine that can SSH to the server (the laptop's Git
# Bash counts) and that has `gh` logged in:
#
#   bash docker/bootstrap.sh 57.129.112.187          # user defaults to debian
#   bash docker/bootstrap.sh my.new.host someuser
#
# What it leaves behind:
#   * the repository cloned on the server (or fast-forwarded, if re-run)
#   * the state directory, and a .env started from .env.example pointing at it
#   * a CI-only SSH keypair whose public half is locked to deploy-ssh.sh by a
#     forced command — the key can deploy, roll back and report status, and
#     can do nothing else on the machine
#   * the four GitHub secrets the Action reads (the private key is uploaded,
#     then shredded locally; nothing of it remains on this machine)
#   * the first deploy live, proven by the same health gate every later push
#     will pass through, and the scheduler running on the same image
#   * the archive seeding itself, detached: two years of DEU construction
#     from TED's monthly packages (TM_BACKFILL_FROM overrides the 2024-08
#     start), then one offline cycle building store, embeddings and the
#     first champion — watch it with: tail -f <state>/logs/backfill.log
#
# Idempotent: re-running is safe and is also the key-rotation procedure — each
# run mints a fresh deploy key, replaces the authorized_keys line and updates
# the GitHub secret, so a leaked key dies at the next bootstrap.
#
# What it deliberately does NOT do, because a fresh box needs root for them
# exactly once (doc/HOSTING.md; the OVH Debian image ships neither):
#   sudo apt install -y git docker-ce ...     # see the check below
#   sudo usermod -aG docker <user>
# It checks both and stops with the command to run when one is missing.

set -euo pipefail

HOST="${1:?usage: bash docker/bootstrap.sh <host> [user]}"
DUSER="${2:-debian}"
TARGET="$DUSER@$HOST"

say() { printf '\n[bootstrap] %s\n' "$*"; }
die() { printf '[bootstrap] FAILED: %s\n' "$*" >&2; exit 1; }

# Everything is derived from the checkout this script sits in — the same
# repository the server will clone, so there is exactly one source of truth
# for "which repo" and it is git's own remote.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_URL="$(git -C "$ROOT" remote get-url origin)"
SLUG="$(printf '%s' "$REPO_URL" | sed -E 's#^.*github.com[:/]##; s#\.git$##')"
DIR="$(basename "$REPO_URL" .git)"
STATE="/home/$DUSER/tm-state"        # no sudo needed, outside the checkout
                                     # a deploy hard-resets

command -v gh >/dev/null || die "gh (GitHub CLI) is not installed — it is what \
sets the Action's secrets. https://cli.github.com"
gh auth status >/dev/null 2>&1 || die "gh is not logged in: run  gh auth login"

# ------------------------------------------------- 1. the server, prepared

say "checking the server ($TARGET) and preparing the checkout"
ssh "$TARGET" "REPO_URL='$REPO_URL' DIR='$DIR' STATE='$STATE' bash -s" <<'REMOTE'
set -eu
command -v git >/dev/null 2>&1 \
    || { echo "git is missing. Run on the server:  sudo apt install -y git"; exit 1; }
command -v docker >/dev/null 2>&1 \
    || { echo "docker is missing. Install it first (doc/HOSTING.md)"; exit 1; }
docker ps >/dev/null 2>&1 \
    || { echo "this user cannot reach the docker daemon. Run: sudo usermod -aG docker $USER  then log out and in"; exit 1; }

if [ -d "$DIR/.git" ]; then
    git -C "$DIR" fetch --quiet origin master
    git -C "$DIR" reset --hard --quiet origin/master
    echo "checkout updated: $DIR at $(git -C "$DIR" rev-parse --short HEAD)"
else
    git clone --quiet "$REPO_URL" "$DIR"
    echo "checkout created: $DIR at $(git -C "$DIR" rev-parse --short HEAD)"
fi

mkdir -p "$STATE"
cd "$DIR"
# Started, never overwritten: a re-run must not wipe the Resend key an
# operator already typed in.
[ -f .env ] || cp .env.example .env
if grep -q '^TM_STATE=' .env; then
    sed -i "s|^TM_STATE=.*|TM_STATE=$STATE|" .env
else
    printf 'TM_STATE=%s\n' "$STATE" >> .env
fi
echo "state directory: $STATE (written into .env)"
REMOTE

# ------------------------------------- 2. the CI key, minted and locked down

say "minting the CI deploy key"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ssh-keygen -q -t ed25519 -N '' -C "tm-ci-deploy" -f "$TMP/deploy_key"
PUB="$(cat "$TMP/deploy_key.pub")"

# The forced command is the security boundary (docker/deploy-ssh.sh): whatever
# the Action asks for lands in SSH_ORIGINAL_COMMAND and sshd runs deploy-ssh.sh
# instead. The marker comment is what makes re-runs replace instead of append.
say "installing the public half on the server (forced command)"
ssh "$TARGET" "DIR='$DIR' PUB='$PUB' bash -s" <<'REMOTE'
set -eu
install -d -m 700 ~/.ssh
touch ~/.ssh/authorized_keys
LINE="command=\"bash $HOME/$DIR/docker/deploy-ssh.sh\",no-pty,no-agent-forwarding,no-port-forwarding,no-X11-forwarding $PUB"
grep -v 'tm-ci-deploy' ~/.ssh/authorized_keys > ~/.ssh/authorized_keys.new || true
printf '%s\n' "$LINE" >> ~/.ssh/authorized_keys.new
mv ~/.ssh/authorized_keys.new ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
echo "authorized_keys: CI key installed, previous CI key (if any) replaced"
REMOTE

# Prove the boundary before trusting it: the key must run `status`, and must
# be refused anything that is not one of the three commands.
#
# -F /dev/null matters: the operator's ~/.ssh/config may (should) hold their
# personal key for this host, and with it in play ssh authenticates as the
# operator — proving nothing about the CI key. Found live on 2026-08-14: the
# first verification "failed" because it had silently logged in as a human.
say "verifying the forced command"
ci_ssh() { ssh -F /dev/null -i "$TMP/deploy_key" -o IdentitiesOnly=yes "$@"; }
ci_ssh "$TARGET" status \
    || die "the CI key could not run 'status' — forced command broken"
if ci_ssh "$TARGET" 'id' 2>/dev/null; then
    die "the CI key executed an arbitrary command — forced command NOT in effect"
fi
say "forced command verified: status works, arbitrary commands refused"

# ------------------------------------------------- 3. the GitHub secrets

say "setting the four Action secrets on $SLUG"
gh secret set DEPLOY_SSH_KEY --repo "$SLUG" < "$TMP/deploy_key"
gh secret set DEPLOY_HOST    --repo "$SLUG" --body "$HOST"
gh secret set DEPLOY_USER    --repo "$SLUG" --body "$DUSER"
# Pinned host key so the Action's first connection cannot be answered by an
# impostor. Scanned here, over the same channel the operator already trusts.
gh secret set DEPLOY_KNOWN_HOSTS --repo "$SLUG" \
    --body "$(ssh-keyscan -t ed25519 "$HOST" 2>/dev/null | grep -v '^#')"
say "secrets set; the private key exists nowhere but in that secret now"

# ------------------------------------------------- 4. first deploy, live

say "running the first deploy (build, health gate, switch)"
ssh "$TARGET" "cd $DIR && bash docker/deploy.sh" \
    || die "first deploy failed — the message above is deploy.sh's"

TAG="$(ssh "$TARGET" "cat $STATE/deploy/current")"
say "starting the scheduler on the proven image ($TAG)"
ssh "$TARGET" "cd $DIR && TM_TAG=$TAG docker compose --profile scheduler up -d --no-build scheduler"

# ------------------------------------------- 5. seed the archive, detached

# A fresh server owns its own data (operator decision 2026-08-14: nothing is
# copied from a laptop): two years of the production scope from TED's monthly
# packages, then one offline cycle to build the store, embeddings and first
# champion from it. Hours of work — the embedding dominates — so it runs
# detached on the server under the same heavy-job lock as the Monday cycle,
# and this script does not wait for it. Progress: tail the log below.
#
# Measured 2026-08-14 on a 4-vCore OVH VPS: the download half is ~12 min for
# 25 months (114,570 notices, 1.78M scanned). bulk.py checkpoints per month
# and skips complete ones, so a bootstrap re-run does not re-transfer.
BACKFILL_FROM="${TM_BACKFILL_FROM:-20240801}"
say "seeding the archive from $BACKFILL_FROM (detached; this is hours of work)"
ssh "$TARGET" "mkdir -p $STATE/logs && cd $DIR && TM_TAG=$TAG nohup docker compose run --rm -T tm \
  sh -c 'python bulk.py --from $BACKFILL_FROM --to \$(date +%Y%m%d) --country DEU --cpv 45 \
      && python loop.py run --last 7d --skip-download' \
  > $STATE/logs/backfill.log 2>&1 & echo '  backfill pid on server:' \$!"

say "done. From here on, push to master = deploy."
cat <<SUMMARY

  Still manual, by design:
    * secrets into the server's .env when they exist (RESEND_API_KEY,
      TM_IMPRESSUM):   ssh $TARGET   then edit $DIR/.env
    * the provider's daily backup checkbox (doc/OPERATIONS.md 3, layer 1)
    * DNS + TM_DOMAIN, the day the domain decision lands

  Backfill: ssh $TARGET "tail -f $STATE/logs/backfill.log"
  Health:   ssh $TARGET "curl -s localhost:8000/healthz"
            (red until the backfill's cycle finishes — OPERATIONS.md 4 step 5)
  Status:   gh workflow run deploy -f command=status   (or push to master)
SUMMARY
