#!/usr/bin/env bash
#
# TenderMining deploy — doc/OPERATIONS.md 2. Runs ON THE SERVER, called by the
# GitHub Action (.github/workflows/deploy.yml) or by hand over SSH:
#
#   bash docker/deploy.sh              # build HEAD, prove it, switch to it
#   bash docker/deploy.sh rollback     # switch back to the previous tag
#   bash docker/deploy.sh status       # what is current, what is previous
#
# The mechanism is the one OPERATIONS.md 2 fixed: every deploy is an immutable
# image tagged with its git SHA, the previous image stays on disk, and traffic
# switches only after the new image has proved itself. Rolling back switches the
# pointer back — it is never a git operation and never a rebuild.
#
# Deliberately not a compose `build`: compose would retag `latest` in place,
# which is exactly the mutation that makes "put yesterday's artifact back"
# impossible. Compose still runs the containers; it is handed a tag.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

IMAGE=tendermining

# Where /data comes from, in priority order: the environment (a laptop test),
# the server's .env — the same file compose reads, so the probe and the real
# containers can never disagree about the state directory — then the checkout.
# Not `source`d: .env is compose syntax, not shell (TM_MAIL_FROM carries < >).
if [ -z "${TM_STATE:-}" ] && [ -f .env ]; then
    TM_STATE="$(grep -E '^TM_STATE=' .env | tail -n1 | cut -d= -f2-)"
fi
STATE="${TM_STATE:-$REPO/data}"
BOOK="$STATE/deploy"                 # current / previous tag, on the state volume
KEEP=5                               # images to keep; older ones are pruned
PROBE_PORT="${TM_PROBE_PORT:-8001}"  # loopback only, never published
PROBE_DEADLINE=60                    # seconds, per OPERATIONS.md 2 step 2

say() { printf '[deploy] %s\n' "$*"; }
die() { printf '[deploy] FAILED: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- the gate
#
# What the probe accepts, and why it is NOT simply "200 from /healthz".
#
# /healthz answers 503 for two very different reasons (app.py, OPERATIONS.md 1):
# the image cannot see its state directory, or the weekly cycle is stale. The
# first is a broken deploy. The second is not — it is a missed Monday, and it
# says nothing at all about the image being installed.
#
# Gating on the raw status code would therefore deadlock the system precisely
# when it is most needed: a cycle that failed for a week would refuse to deploy
# the fix for that cycle, and the very first deploy onto an empty server could
# never switch at all, because a machine that has never run a cycle is stale by
# definition. So the gate reads the body and requires the part the new image is
# actually responsible for:
#
#   * it answers at all, within the deadline  -> the process starts and serves
#   * disk_free_mb is a number, not "unknown" -> it can see and stat /data
#
# Staleness is reported and ignored here. It is the pinger's job (OPERATIONS.md
# 1), which watches from outside and pages the operator — a different question
# asked by a different watcher, which is why one endpoint can carry both.
# The probe container's name, cleaned up by the EXIT trap below. A RETURN trap
# would not do: `die` exits rather than returns, which is precisely the path a
# failed probe takes — and leaving a probe container holding the port would make
# the next deploy fail for a reason that has nothing to do with the next deploy.
PROBE_NAME=''
cleanup() { [ -n "$PROBE_NAME" ] && docker rm -f "$PROBE_NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

probe() {
    local tag="$1" body='' code='' out=''
    PROBE_NAME="tm-probe-$tag"
    say "probing $IMAGE:$tag on 127.0.0.1:$PROBE_PORT"

    # --rm so a passing probe leaves no container behind; the IMAGE stays on
    # disk for inspection, which is what OPERATIONS.md 2 step 3 asks for.
    docker run -d --rm --name "$PROBE_NAME" \
        -e "TZ=${TM_TZ:-Europe/Bucharest}" \
        -v "$STATE:/data" \
        -p "127.0.0.1:$PROBE_PORT:8000" \
        "$IMAGE:$tag" python app.py --port 8000 >/dev/null \
        || die "the new image did not start at all"

    local waited=0
    while [ "$waited" -lt "$PROBE_DEADLINE" ]; do
        # Body and status code out of one request, and no temp file: a writable
        # shared /tmp is one more thing that can differ between this server and
        # whatever machine an operator debugs from.
        out=$(curl -s -w '\n%{http_code}' \
              "http://127.0.0.1:$PROBE_PORT/healthz" || true)
        code="${out##*$'\n'}"
        if [ -n "$code" ] && [ "$code" != 000 ]; then
            body="${out%$'\n'*}"
            break
        fi
        sleep 2
        waited=$((waited + 2))
    done

    [ -n "$body" ] || die "no answer from /healthz within ${PROBE_DEADLINE}s — \
the image is on disk as $IMAGE:$tag and the old container is still serving"

    say "  HTTP $code: $(printf '%s' "$body" | tr '\n' ' ')"

    printf '%s' "$body" | grep -qE 'disk_free_mb=[0-9]+' \
        || die "the new image cannot read its state directory (disk_free_mb \
unknown) — /data is not mounted or not readable. Not switching."

    if printf '%s' "$body" | grep -q 'cycle_age_days=unknown'; then
        say "  note: no cycle has ever run here. Deploy proceeds; /healthz stays"
        say "        red until the first cycle (OPERATIONS.md 4 step 5)."
    fi
    say "gate passed"
}

# ------------------------------------------------------------- the switch

# A running deployment's /data mount is its identity. If this invocation would
# point the containers somewhere else, it is not a deploy — it is a mistyped
# TM_STATE or a .env that never got copied, and the failure is silent: the app
# serves an empty database and next Monday's cycle runs against nothing. Neither
# raises an error, both look like a successful deploy, and the discovery is a
# customer's.
#
# Learned the hard way, 2026-08-14: `docker compose up` matches on the project
# name (`tendermining`), not on the directory it is run from, so a deploy run
# from a second checkout silently rehomed the scheduler of the first.
#
# Override with TM_FORCE_STATE=1 when the move is deliberate — and on Docker
# Desktop, where the daemon rewrites host paths (/run/desktop/mnt/host/c/...)
# and the comparison cannot succeed.
check_state_matches_running() {
    local running src
    running=$(docker compose ps -q app 2>/dev/null | head -1)
    [ -n "$running" ] || running=$(docker compose --profile scheduler ps -q \
                                   scheduler 2>/dev/null | head -1)
    [ -n "$running" ] || return 0        # nothing running: this is a first deploy
    src=$(docker inspect "$running" --format \
          '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' \
          2>/dev/null || true)
    [ -n "$src" ] || return 0
    if [ "$src" != "$STATE" ] && [ "${TM_FORCE_STATE:-0}" != 1 ]; then
        die "the running containers hold /data from
    $src
but this deploy would give them
    $STATE
Refusing: one of the two is wrong, and guessing which costs a week of reports.
Fix TM_STATE (or .env), or set TM_FORCE_STATE=1 if the move is intended."
    fi
}

switch_to() {
    local tag="$1"
    # The scheduler is behind a compose profile and may or may not be running.
    # Recreating it matters: OPERATIONS.md 2 step 5 — next Monday must run the
    # same image the app just proved, never the one from two deploys ago.
    local profiles=() services=(app)
    if [ -n "$(docker compose --profile scheduler ps -q scheduler 2>/dev/null)" ]; then
        profiles=(--profile scheduler)
        services+=(scheduler)
        say "scheduler is running; it will be recreated on $tag too"
    fi
    # The edge rides deploys the same way: its image is caddy:2, not ours, but
    # its CONFIG (docker/Caddyfile, mounts) arrives through git — recreating it
    # here is what makes a Caddyfile change deploy itself instead of waiting
    # for a hand-restart nobody remembers.
    if [ -n "$(docker compose --profile edge ps -q edge 2>/dev/null)" ]; then
        profiles+=(--profile edge)
        services+=(edge)
        say "edge is running; it will be recreated for the new config too"
    fi
    TM_TAG="$tag" docker compose "${profiles[@]}" up -d --no-build "${services[@]}" \
        || die "compose refused to start $tag — the old containers may be down; \
run 'bash docker/deploy.sh rollback'"
}

# ----------------------------------------------------------- the public site
#
# www.murara.eu is files under $STATE/public, not part of the image, because
# they are computed from the database — which only the server has. So a
# push that changes a page template used to change nothing a visitor sees
# until the next Monday cycle rebuilt them (operator, 2026-08-15: "I thought a
# git push rebuilds the image and the website gets updated"). Now the deploy
# rebuilds them with the image it has just proved, exactly as the cycle does.
#
# Before the switch, not after: the edge is recreated in switch_to, and it
# serves $STATE/public/current — building first means that link exists the
# moment the new edge starts. trade_pages.release makes the rebuild
# all-or-nothing (a new directory, one link rename, old directory removed).
#
# Non-fatal, by the same rule loop.py applies: the image is proved and the
# code is what this deploy is for; a page-build failure keeps the previous
# site serving and is retried by the cycle. It says so loudly, though.
build_site() {
    local tag="$1"
    say "building the public site with $IMAGE:$tag -> $STATE/public/current"
    if docker run --rm \
        -e "TZ=${TM_TZ:-Europe/Bucharest}" \
        -v "$STATE:/data" \
        "$IMAGE:$tag" python trade_pages.py; then
        say "site built"
    else
        say "WARNING: site build failed — the previous site keeps serving;"
        say "         the Monday cycle will rebuild it. Deploy continues."
    fi
}

record() {
    mkdir -p "$BOOK"
    [ -f "$BOOK/current" ] && cp "$BOOK/current" "$BOOK/previous" || true
    printf '%s\n' "$1" > "$BOOK/current"
}

prune() {
    # Keep the last $KEEP tags so `rollback` always has something to switch to,
    # and so an image that served last month can still be inspected. Never
    # touches the tag currently recorded as current or previous.
    local cur prev
    cur="$(cat "$BOOK/current" 2>/dev/null || true)"
    prev="$(cat "$BOOK/previous" 2>/dev/null || true)"
    docker images "$IMAGE" --format '{{.Tag}}' | tail -n "+$((KEEP + 1))" \
    | while read -r t; do
        # Never prune what is serving or what rollback would switch to, however
        # old they are: a machine that has not deployed in months must still be
        # able to roll back.
        if [ "$t" = "$cur" ] || [ "$t" = "$prev" ] || [ "$t" = latest ]; then
            continue
        fi
        say "pruning $IMAGE:$t"
        docker rmi "$IMAGE:$t" >/dev/null 2>&1 || true
      done
}

# --------------------------------------------------------------- commands

cmd_deploy() {
    command -v curl >/dev/null || die "curl is not installed (apt install curl)"
    [ -d "$STATE" ] || die "state directory $STATE does not exist"

    git rev-parse --git-dir >/dev/null 2>&1 || die "not a git checkout: $REPO"
    check_state_matches_running
    local tag
    tag="$(git rev-parse --short HEAD)"
    say "deploying $tag ($(git log -1 --format=%s))"

    # A build failure changes nothing: the running container is untouched, and
    # this exits non-zero so the Action goes red (OPERATIONS.md 2 step 1 and 3).
    docker build -t "$IMAGE:$tag" . || die "build failed — nothing switched"

    probe "$tag"
    build_site "$tag"
    switch_to "$tag"
    record "$tag"
    prune
    say "live on $tag"
}

cmd_rollback() {
    check_state_matches_running
    [ -f "$BOOK/previous" ] || die "no previous tag recorded in $BOOK"
    local tag
    tag="$(cat "$BOOK/previous")"
    docker image inspect "$IMAGE:$tag" >/dev/null 2>&1 \
        || die "image $IMAGE:$tag is gone (pruned?) — nothing to roll back to"
    say "rolling back to $tag — no rebuild, this is the artifact that served before"
    probe "$tag"
    # No site rebuild here, on purpose: an image from before 2026-08-15 builds
    # the site by deleting and recreating $STATE/public — the directory the
    # edge mounts — which leaves the edge serving nothing. The site keeps the
    # pages of the tag being rolled back from; the next deploy or Monday cycle
    # regenerates them. (Rolling the SCHEDULER back to such an image has the
    # same hazard on Monday — roll forward instead where you can.)
    switch_to "$tag"
    # Swap the pointers rather than appending: rolling back twice returns to
    # where you were, which is what an operator under pressure expects.
    local cur
    cur="$(cat "$BOOK/current" 2>/dev/null || true)"
    printf '%s\n' "$tag" > "$BOOK/current"
    [ -n "$cur" ] && printf '%s\n' "$cur" > "$BOOK/previous"
    say "live on $tag"
}

cmd_status() {
    printf 'current:  %s\n' "$(cat "$BOOK/current" 2>/dev/null || echo none)"
    printf 'previous: %s\n' "$(cat "$BOOK/previous" 2>/dev/null || echo none)"
    printf '\nimages:\n'
    docker images "$IMAGE" --format '  {{.Tag}}\t{{.CreatedSince}}\t{{.Size}}'
    printf '\ncontainers:\n'
    docker compose ps --format '  {{.Service}}\t{{.Status}}' 2>/dev/null || true
}

case "${1:-deploy}" in
    deploy)   cmd_deploy ;;
    rollback) cmd_rollback ;;
    status)   cmd_status ;;
    *)        die "unknown command '$1' (deploy | rollback | status)" ;;
esac
