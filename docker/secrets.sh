#!/usr/bin/env bash
# TenderMining secrets — doc/SECRETS.md §3.
#
# Moves FILES, never values: one file per concern under env.d/. This script
# never parses a value, never takes one as an argument, never prints one.
# The password manager is the durable copy; both trees are caches of it.
#
#   bash docker/secrets.sh list          # server: file, key, set|EMPTY, first character
#   bash docker/secrets.sh diff          # per key: same / DIFFERS / only here / only on the server
#   bash docker/secrets.sh push [file…]  # laptop -> server, then recreate what reads them
#   bash docker/secrets.sh pull [file…]  # server -> laptop (never over a newer local file)
#
# The server is named as bootstrap.sh names it: TM_SERVER / TM_SERVER_USER,
# or two trailing arguments.
#
#   TM_SERVER=57.129.112.187 bash docker/secrets.sh diff
#   bash docker/secrets.sh push mail.env 57.129.112.187 debian
#
# Transport is **tar over ssh**, not rsync: neither this laptop nor the server
# has rsync (checked 2026-08-18), and what the spec asks rsync for — file
# bodies never appearing in a process list on either machine — is exactly what
# a stream gives.
#
# Remote code travels base64-encoded inside the ssh command string, so quoting
# is decided once here and never again by a second shell.
#
# Which services must be recreated is read from docker-compose.yml's
# `env_file:` blocks, not from a table in this script: a new file wired into a
# service is picked up without touching this file. `docker compose config`
# cannot answer it — it resolves env_file away entirely.

set -euo pipefail

CMD="${1:?usage: bash docker/secrets.sh list|diff|push [file…]|pull [file…] [host] [user]}"
shift 1

FILES=()
while [ $# -gt 0 ]; do
    case "$1" in
        *.env) FILES+=("$1"); shift ;;
        *)     break ;;
    esac
done
HOST="${1:-${TM_SERVER:-}}"
DUSER="${2:-${TM_SERVER_USER:-debian}}"
[ -n "$HOST" ] || { echo "[secrets] no server: pass it or set TM_SERVER" >&2; exit 2; }
TARGET="$DUSER@$HOST"
DIR="${TM_SERVER_DIR:-TenderMiner}"
LOCAL_D="${TM_ENV_D:-$HOME/.murara/env.d}"
REMOTE_D=/etc/murara/env.d

# Everything is resolved from the script's own location, never from the
# directory it was called in: `services_for` reads the compose file, and a
# run from anywhere else silently found no services and recreated nothing.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="$REPO/docker-compose.yml"

say() { printf '[secrets] %s\n' "$*"; }
die() { printf '[secrets] %s\n' "$*" >&2; exit 2; }

# $1 = shell code to run on the server, with D and DIR already set there.
# -n: stdin stays free, so a caller can stream a tar into ssh separately.
run_remote() {
    local b64
    b64="$(printf %s "$1" | base64 | tr -d '\n')"
    ssh -n "$TARGET" "D='$REMOTE_D' DIR='$DIR' bash -c \"\$(printf %s '$b64' | base64 -d)\""
}

# ------------------------------------------------------------------ remote halves

REMOTE_LIST=$(cat <<'REMOTE'
set -eu
cd "$D" 2>/dev/null || { echo "[secrets] no $D on the server"; exit 0; }
printf '%-14s %-24s %-6s %s\n' FILE KEY STATE FIRST
for f in *.env; do
    [ -e "$f" ] || continue
    while IFS= read -r line; do
        case "$line" in ''|'#'*) continue ;; esac
        k=${line%%=*}; v=${line#*=}
        if [ -n "$v" ]; then
            printf '%-14s %-24s %-6s %.1s...\n' "$f" "$k" set "$v"
        else
            printf '%-14s %-24s %-6s\n' "$f" "$k" EMPTY
        fi
    done < "$f"
    m=$(stat -c %a "$f")
    [ "$m" = 600 ] || printf '%-14s ** mode %s, expected 600 **\n' "$f" "$m"
done
REMOTE
)

# Per key, a hash of the value — never the value. Computed the same way on
# both sides and compared here, which is what lets `diff` say DIFFERS without
# anyone seeing what differs.
FINGERPRINT=$(cat <<'REMOTE'
set -eu
cd "$D" 2>/dev/null || exit 0
for f in *.env; do
    [ -e "$f" ] || continue
    while IFS= read -r line; do
        case "$line" in ''|'#'*) continue ;; esac
        k=${line%%=*}; v=${line#*=}
        if [ -n "$v" ]; then
            h=$(printf %s "$v" | sha256sum | cut -c1-12)
        else
            h='(empty)'
        fi
        printf '%s|%s %s\n' "$f" "$k" "$h"
    done < "$f"
done
REMOTE
)

REMOTE_RESTART=$(cat <<'REMOTE'
set -eu
cd "$HOME/$DIR"
STATE=$(sed -n 's/^TM_STATE=//p' .env | tail -1)
TAG=$(cat "$STATE/deploy/current" 2>/dev/null || echo latest)
# Every profile named: without them compose silently skips a profiled service
# and the old file stays loaded, which reads exactly like a push that worked.
TM_TAG="$TAG" docker compose --profile edge --profile scheduler \
    up -d --force-recreate $SERVICES 2>&1 | grep -viE 'warn|network|volume' || true
for s in $SERVICES; do
    printf '  %s: %s\n' "$s" "$(docker compose --profile edge --profile scheduler ps --format '{{.Status}}' "$s" | head -1)"
done
REMOTE
)

# ------------------------------------------------------------------ inventory

local_files() {
    [ -d "$LOCAL_D" ] || return 0
    (cd "$LOCAL_D" && ls -1 *.env 2>/dev/null) || true
}

remote_files() {
    run_remote 'cd "$D" 2>/dev/null && ls -1 *.env 2>/dev/null || true'
}

chosen() {   # the files this run is about: those named, else all there are
    if [ "${#FILES[@]}" -gt 0 ]; then printf '%s\n' "${FILES[@]}"; else "$1"; fi
}

services_for() {   # file -> services whose env_file names it, or that extend one
    # `extends:` is followed one level (2026-08-18): site.env is listed on the
    # base `tm`, and app/scheduler inherit it — restarting `tm` alone would
    # recreate nothing that runs and read exactly like a push that worked.
    awk -v want="$1" '
        /^  [a-z][a-z0-9_-]*:/ { svc = $1; sub(":", "", svc) }
        /^    extends: / && svc != "" { base[svc] = $2 }
        /path: .*env\.d\// {
            n = split($0, p, "/")
            if (p[n] == want && svc != "") has[svc] = 1
        }
        END {
            # `tm` itself is the base nothing runs as: its image CMD is a
            # full cycle, so `up -d tm` would START one. Only the services
            # that extend it are ever recreated.
            for (s in has) if (s != "tm") print s
            for (s in base) if (base[s] in has) print s
        }' "$COMPOSE" | sort -u | tr '\n' ' '
}

# ------------------------------------------------------------------ commands

cmd_list() { run_remote "$REMOTE_LIST"; }

cmd_diff() {
    local here there
    here="$(D="$LOCAL_D" bash -c "$FINGERPRINT" | sort || true)"
    there="$(run_remote "$FINGERPRINT" | sort || true)"
    printf '%-14s %-24s %s\n' FILE KEY STATE
    join -a1 -a2 -o 0,1.2,2.2 -e '-' \
         <(printf '%s\n' "$here"  | grep . | sort) \
         <(printf '%s\n' "$there" | grep . | sort) \
    | while read -r key h t; do
        [ -n "$key" ] || continue
        f="${key%%|*}"; k="${key#*|}"
        if   [ "$h" = '-' ]; then state='only on the server'
        elif [ "$t" = '-' ]; then state='only here'
        elif [ "$h" = "$t" ]; then state='same'
        else state='DIFFERS'
        fi
        printf '%-14s %-24s %s\n' "$f" "$k" "$state"
    done
    printf '\nhere:   %s\nserver: %s\n' "$LOCAL_D" "$REMOTE_D"
}

cmd_push() {
    local files svc all=''
    files="$(chosen local_files | grep . || true)"
    [ -n "$files" ] || die "nothing to push: $LOCAL_D has no *.env"
    for f in $files; do
        [ -f "$LOCAL_D/$f" ] || die "$LOCAL_D/$f does not exist"
    done
    say "pushing $(echo $files | tr '\n' ' ')to $TARGET:$REMOTE_D"
    # The bodies travel inside the tar stream; no value is ever an argument.
    tar -C "$LOCAL_D" -cf - $files \
        | ssh "$TARGET" "umask 077 && mkdir -p $REMOTE_D && tar -C $REMOTE_D -xf - && chmod 600 $REMOTE_D/*.env"
    for f in $files; do
        svc="$(services_for "$f")"
        [ -n "$svc" ] && all="$all $svc"
    done
    all="$(printf '%s' "$all" | tr ' ' '\n' | grep . | sort -u | tr '\n' ' ' || true)"
    if [ -n "$all" ]; then
        say "recreating: $all"
        run_remote "SERVICES='$all'
$REMOTE_RESTART"
    else
        say 'no service lists these files — nothing recreated'
    fi
    cmd_list
}

cmd_pull() {
    local files lt rt
    files="$(chosen remote_files | grep . || true)"
    [ -n "$files" ] || die "nothing to pull: $REMOTE_D has no *.env"
    mkdir -p "$LOCAL_D"; chmod 700 "$LOCAL_D"
    for f in $files; do
        # doc/SECRETS.md §3: never over a newer local file. The laptop is
        # where the operator edits, so a newer local copy is unpushed work,
        # not a stale one.
        if [ -f "$LOCAL_D/$f" ]; then
            lt="$(stat -c %Y "$LOCAL_D/$f")"
            rt="$(run_remote "stat -c %Y \"\$D/$f\" 2>/dev/null || echo 0" | tr -d '\r')"
            if [ "$lt" -gt "${rt:-0}" ]; then
                say "$f: the copy here is newer — skipped (push it, or delete it first)"
                continue
            fi
        fi
        say "pulling $f"
        ssh -n "$TARGET" "tar -C $REMOTE_D -cf - $f" | tar -C "$LOCAL_D" -xf -
        chmod 600 "$LOCAL_D/$f"
    done
    ls -l "$LOCAL_D"
}

case "$CMD" in
    list) cmd_list ;;
    diff) cmd_diff ;;
    push) cmd_push ;;
    pull) cmd_pull ;;
    *)    die "unknown command $CMD (list | diff | push | pull)" ;;
esac
