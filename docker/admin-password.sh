#!/usr/bin/env bash
# The operator's /admin password — doc/ADMIN.md §5, doc/SECRETS.md.
#
# One key, one job, deliberately NOT the general secrets tool: SECRETS.md's
# `secrets.sh` moves whole files (env.d/, rsync) and never parses a value.
# This one has to parse: the *password* must never be stored, only a bcrypt
# hash of it, and the hashing happens on the server inside Caddy's own image.
# It survives the env.d/ migration by pointing TARGET_FILE at admin.env.
#
#   TM_SERVER=57.129.112.187 bash docker/admin-password.sh status
#   TM_SERVER=57.129.112.187 bash docker/admin-password.sh set     # hidden prompt
#
# The server is named the way bootstrap.sh names it: <host> [user], through
# TM_SERVER / TM_SERVER_USER or the trailing arguments.
#
# Where the password comes from: TM_SECRET_SOURCE, a command that prints it
# (SECRETS.md lists one line per password manager); unset, a hidden prompt.
# Either way it travels over ssh's **stdin only** — no process list on either
# machine, no shell history, no temporary file — is hashed on the server by
# `caddy hash-password`, and only the hash is written.
#
# One trap this script exists to remember (it cost an outage on 2026-08-17):
# `caddy hash-password` reads a LINE, so the value needs a trailing newline or
# it sees EOF.
#
# The second trap is gone by design. The credential used to be an environment
# variable, which meant Compose expanded every `$` of the hash on the way in
# (so it had to be written doubled) and, worse, the edge only saw the file when
# its container was CREATED: a password could sit written-but-not-in-force
# until the next deploy recreated the edge, which is exactly what happened on
# 2026-08-18. Now the edge IMPORTS the credential from a Caddy snippet file
# and re-reads it on `caddy reload` — no escaping, no recreate, and this
# script does not exit until the live edge has accepted the new password.

set -euo pipefail

CMD="${1:?usage: bash docker/admin-password.sh status|set [host] [user]}"
shift 1
KEY=TM_ADMIN_PASSWORD
HOST="${1:-${TM_SERVER:-}}"
DUSER="${2:-${TM_SERVER_USER:-debian}}"
[ -n "$HOST" ] || { echo "[admin-password] no server: pass it or set TM_SERVER" >&2; exit 2; }
TARGET="$DUSER@$HOST"
DIR="${TM_SERVER_DIR:-TenderMiner}"

# Where the credential is written: ONE line, `<user> <bcrypt hash>`, in Caddy's
# own syntax, imported by docker/Caddyfile from the directory compose mounts
# read-only into the edge. Not an env file — nothing interpolates it, so the
# hash is stored exactly as `caddy hash-password` printed it.
# The directory is created by bootstrap.sh; TM_ADMIN_CADDY_FILE overrides the
# path for a machine that keeps it elsewhere.
TARGET_FILE="${TM_ADMIN_CADDY_FILE:-/etc/murara/caddy.d/admin.caddy}"

say() { printf '[admin-password] %s\n' "$*"; }

MIN_CHARS=12

read_masked() {   # $1 prompt -> the typed value on stdout, one * per character
    # Not `read -rs`: a prompt that shows nothing at all leaves the operator
    # guessing whether the keyboard is even reaching it (operator, 2026-08-18).
    # One asterisk per character, backspace erases, Enter ends.
    local prompt="$1" value='' ch src=/dev/stdin
    # `[ -r /dev/tty ]` is not the test: the file can exist and still refuse to
    # open when there is no controlling terminal. Opening it IS the test.
    local opened=0
    if { exec 3</dev/tty; } 2>/dev/null; then src=/dev/fd/3; opened=1; fi
    printf '%s' "$prompt" >&2
    while IFS= read -rsn1 ch <"$src"; do
        case "$ch" in
            '')  break ;;                                        # Enter
            $''|$'')                                       # Backspace
                 if [ -n "$value" ]; then
                     value="${value%?}"; printf ' ' >&2
                 fi ;;
            *)   value="$value$ch"; printf '*' >&2 ;;
        esac
    done
    # Only close what was opened: `exec 3<&-` on an fd that was never opened
    # is a redirection error, and a redirection error in `exec` KILLS the
    # shell — silently, here, because of the 2>/dev/null. It ate everything
    # after this line whenever no terminal was attached.
    [ "$opened" = 1 ] && exec 3<&-
    printf '\n' >&2
    printf '%s' "$value"
}

read_value() {   # -> the password on stdout, from the manager or a prompt
    local key="$1"
    if [ -n "${TM_SECRET_SOURCE:-}" ]; then
        # `$1` inside TM_SECRET_SOURCE is the key name — the convention that
        # keeps this script free of manager-specific code (SECRETS.md).
        bash -c "$TM_SECRET_SOURCE" _ "$key"
        return
    fi
    {
        printf '
  ------------------------------------------------------
'
        printf '  NEW PASSWORD FOR THE WEB PAGE https://app.murara.eu/admin
'
        printf '  (user %s). This is NOT your SSH key passphrase.
'                "${TM_ADMIN_USER:-murara}"
        printf '  ------------------------------------------------------
'
        printf '  - you choose it now; at least %s characters
' "$MIN_CHARS"
        printf '  - it is stored NOWHERE in plain text: the server keeps
'
        printf '    only a bcrypt hash, so save it in your password manager

'
    } >&2
    # Ask again instead of exiting: a password typed one character short cost
    # the whole run, ssh connection and passphrase included (operator,
    # 2026-08-18). Three tries, then stop; nothing is written either way.
    local first second tries=0
    while : ; do
        tries=$((tries + 1))
        first="$(read_masked '  Password:        ')"
        if [ -z "$first" ]; then
            say 'nothing typed - stopped, nothing changed'
            exit 2
        elif [ "${#first}" -lt "$MIN_CHARS" ]; then
            printf '  -> too short: %s of %s characters.

'                    "${#first}" "$MIN_CHARS" >&2
        else
            second="$(read_masked '  Repeat:          ')"
            if [ "$first" = "$second" ]; then
                break
            fi
            printf '  -> the two entries differ.

' >&2
        fi
        if [ "$tries" -ge 3 ]; then
            say 'three tries, stopped - nothing changed'
            exit 2
        fi
        printf '  Please try again.

' >&2
    done
    printf '\n' >&2
    printf '%s' "$first"
}

cmd_status() {
    ssh "$TARGET" "DIR='$DIR' FILE='$TARGET_FILE' bash -s" <<'REMOTE'
set -eu
cd "$HOME/$DIR" || { echo "[admin-password] no checkout at ~/$DIR" >&2; exit 2; }
printf 'file:  %s
' "$FILE"
if [ -f "$FILE" ]; then
    printf '       exists, mode %s, owner %s
' "$(stat -c %a "$FILE")" "$(stat -c %U "$FILE")"
    u="$(awk 'NF && $1 !~ /^#/ {print $1; exit}' "$FILE")"
    h="$(awk 'NF && $1 !~ /^#/ {print $2; exit}' "$FILE")"
    printf 'user:  %s
' "${u:-NONE - /admin refuses every password}"
    if [ -n "$h" ]; then
        printf 'hash:  set, %s characters, starts %s
' "${#h}" "$(printf '%.4s' "$h")"
    else
        printf 'hash:  EMPTY - /admin refuses every password
'
    fi
else
    # Not an error: the Caddyfile imports a glob, so a machine without this
    # file serves /admin as a closed door rather than failing to start.
    printf '       DOES NOT EXIST - /admin refuses every password
'
fi
printf 'edge:  %s
' "$(docker compose --profile edge ps --format '{{.Status}}' edge | head -1)"
# What the RUNNING edge would use: the file as Caddy last read it. Config and
# file agreeing is the whole point of the file-imported credential, and this
# line is where a disagreement would show.
if docker compose --profile edge exec -T edge test -f /etc/caddy/secrets/admin.caddy 2>/dev/null; then
    printf 'edge sees the file: yes
'
else
    printf 'edge sees the file: NO - check the mount in docker-compose.yml
'
fi
REMOTE
}


REMOTE_SET=$(cat <<'REMOTE'
set -eu
cd "$HOME/$DIR" || { echo "[admin-password] no checkout at ~/$DIR" >&2; exit 2; }
d="$(dirname "$FILE")"
[ -d "$d" ] || { echo "[admin-password] $d does not exist - run bootstrap.sh" >&2; exit 2; }
# The handshake: the caller waits for this line before it asks the operator
# for anything, so "connected" is a fact and not a hope — and then blocks here
# until the password arrives over the same connection.
echo READY
password="$(cat)"
[ -n "$password" ] || { echo "[admin-password] empty; nothing written" >&2; exit 2; }

# The password itself never lands anywhere: Caddy hashes it (bcrypt) and only
# the hash is stored. -i, so the plaintext travels on stdin.
# A trailing newline is required: hash-password reads a LINE, and without one
# it sees EOF and refuses ("Error: EOF"). --plaintext would put the password
# in the remote process list, which is exactly what stdin avoids.
value=$(printf "%s\n" "$password" | docker run --rm -i caddy caddy hash-password 2>/dev/null | tail -1)
[ -n "$value" ] || { echo "[admin-password] hashing failed" >&2; exit 2; }
# `password` is NOT unset here: this run does not end at "written", it ends
# when the live edge has accepted the new password. Unset the moment it has.

# The user keeps whatever the file already said; a fresh file gets `murara`.
user="$(awk 'NF && $1 !~ /^#/ {print $1; exit}' "$FILE" 2>/dev/null || true)"
user="${user:-murara}"

# Caddy's own syntax, one line, no interpolation anywhere on the way in —
# which is why there is no `$`-doubling here any more. Written to a temporary
# file first and moved into place: the edge mounts the DIRECTORY, so the
# rename is atomic and the container sees the new file, never a half-written
# one.
umask 077
printf '%s %s\n' "$user" "$value" > "$FILE.new"
chmod 600 "$FILE.new"

# Validate BEFORE the file is in place. A malformed credential cannot take the
# edge down through a reload (reload refuses and keeps the running config),
# but it can through the next container recreate — a deploy, a reboot — and
# that failure would arrive hours later with no obvious cause. So the check
# happens here, against the real Caddyfile, with the new file staged where the
# edge would read it.
TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT
cp "$FILE.new" "$TMPD/admin.caddy"
if ! out="$(docker run --rm \
        -v "$PWD/docker/Caddyfile:/etc/caddy/Caddyfile:ro" \
        -v "$TMPD:/etc/caddy/secrets:ro" \
        caddy:2 caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1)"; then
    rm -f "$FILE.new"
    echo "[admin-password] the new credential does not validate - NOTHING changed" >&2
    printf '%s\n' "$out" | tail -3 >&2
    exit 2
fi
mv "$FILE.new" "$FILE"
echo "[admin-password] written to $FILE; reloading the edge"

# A RELOAD, not a recreate. Caddy re-reads the imported file and swaps the
# config in place: no restart, no dropped connection, no deploy needed — and
# nothing else about the edge changes. If the reload fails, the edge goes on
# serving the config it already has, so the old password stays in force and
# the check below says so.
docker compose --profile edge exec -T edge \
    caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1 | tail -2 || true

# ------------------------------------------------------------ in force?
#
# The file is the truth, but "written" is not "in force" — that gap is the
# whole reason this section exists. On 2026-08-18 a `set` wrote the hash at
# 08:22 and the edge kept the old one until a deploy recreated it at 08:47;
# in between, the page refused the new password and accepted the old one, and
# nothing anywhere said so. So the run ends by asking the live edge, over TLS
# on the real name, with the password it has just hashed — through
# `--config -` on stdin, because `-u user:pass` would put the password in the
# process list, which is the one thing this script exists to avoid.
HOST="$(sed -n 's/^TM_DOMAIN=//p' .env | tail -1)"
HOST="${HOST:-app.murara.eu}"
code="$(printf 'user = "%s:%s"\n' "$user" "$password" |
        curl -s -o /dev/null -w '%{http_code}' --max-time 20 --config - \
             --resolve "$HOST:443:127.0.0.1" "https://$HOST/admin" || true)"
unset password
printf '\n  file:  %s (mode %s, owner %s)\n' \
       "$FILE" "$(stat -c %a "$FILE")" "$(stat -c %U "$FILE")"
printf '  user:  %s\n' "$user"
printf '  hash:  %s characters\n' "${#value}"
case "$code" in
    200) printf '  door:  IN FORCE - the live edge accepts the new password\n' ;;
    401) printf '  door:  NOT IN FORCE - the edge still refuses it\n' >&2
         printf '         The credential is in %s but the running edge has\n' "$FILE" >&2
         printf '         another one. Reload it by hand and run status again:\n' >&2
         printf '         docker compose --profile edge exec edge \\n' >&2
         printf '           caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile\n' >&2
         exit 3 ;;
    *)   printf '  door:  UNPROVEN - the edge answered %s, not 200 or 401\n' "${code:-nothing}" >&2
         printf '         The credential is written; whether it is in force is\n' >&2
         printf '         unknown. Check https://%s/admin by hand.\n' "$HOST" >&2
         exit 3 ;;
esac
REMOTE
)


# One ssh, not two. The old shape opened a connection to check the server and
# a second one to write, so a passphrase-protected key asked twice, once on
# each side of the password prompt (operator, 2026-08-18). Now the connection
# is opened ONCE as a coprocess: ssh asks for the passphrase at that moment,
# the remote half answers READY over the same pipe, and only then does this
# script ask for the password — which travels down that already-open pipe.
#
# (`ssh-add` once per session removes the passphrase prompt entirely; ssh
# never reads a passphrase from stdin, always from the terminal, so the two
# never mix.)
cmd_set() {
    local pw marker
    printf '[admin-password] connecting to %s ...
' "$TARGET" >&2
    local b64; b64="$(printf %s "$REMOTE_SET" | base64 | tr -d '
')"
    coproc SSH { ssh "$TARGET" "DIR='$DIR' FILE='$TARGET_FILE'                                 bash -c \"\$(printf %s '$b64' | base64 -d)\"" 2>&1; }
    # Blocks until the remote half is actually running — after any passphrase.
    if ! IFS= read -r marker <&"${SSH[0]}" || [ "$marker" != READY ]; then
        say "no connection to $TARGET (${marker:-no answer})"
        wait "${SSH_PID:-}" 2>/dev/null || true
        exit 2
    fi
    printf '[admin-password] connected.
' >&2

    pw="$(read_value "$KEY")" || exit 2
    printf '%s
' "$pw" >&"${SSH[1]}"
    unset pw
    eval "exec ${SSH[1]}>&-"          # EOF, so the remote `cat` returns
    cat <&"${SSH[0]}" >&2             # written / reloading / in force
    # The coprocess may already have reaped itself by now, and `set -u` turns
    # a missing SSH_PID into a failure at the very last line of a successful
    # run. Wait only if there is still something to wait for.
    wait "${SSH_PID:-}" 2>/dev/null || true
}


case "$CMD" in
    status) cmd_status ;;
    set)    cmd_set ;;
    *)      say "unknown command $CMD (status | set)"; exit 2 ;;
esac
