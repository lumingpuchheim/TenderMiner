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
# Two traps this script exists to remember (both cost an outage on
# 2026-08-17): `caddy hash-password` reads a LINE, so the value needs a
# trailing newline or it sees EOF; and Compose expands `$` inside .env
# values, so every `$` of the hash is doubled on the way in.

set -euo pipefail

CMD="${1:?usage: bash docker/admin-password.sh status|set [host] [user]}"
shift 1
KEY=TM_ADMIN_PASSWORD
HOST="${1:-${TM_SERVER:-}}"
DUSER="${2:-${TM_SERVER_USER:-debian}}"
[ -n "$HOST" ] || { echo "[admin-password] no server: pass it or set TM_SERVER" >&2; exit 2; }
TARGET="$DUSER@$HOST"
DIR="${TM_SERVER_DIR:-TenderMiner}"

# Where the hash is written, and what must be recreated to see it. After the
# env.d/ migration (doc/SECRETS.md §1) this becomes admin.env; the script
# changes by one line and nothing else.
TARGET_FILE="${TM_ADMIN_ENV_FILE:-.env}"
SERVICES=edge

say() { printf '[admin-password] %s\n' "$*"; }

MIN_CHARS=12

read_masked() {   # $1 prompt -> the typed value on stdout, one * per character
    # Not `read -rs`: a prompt that shows nothing at all leaves the operator
    # guessing whether the keyboard is even reaching it (operator, 2026-08-18).
    # One asterisk per character, backspace erases, Enter ends.
    local prompt="$1" value='' ch src=/dev/stdin
    # `[ -r /dev/tty ]` is not the test: the file can exist and still refuse to
    # open when there is no controlling terminal. Opening it IS the test.
    if { exec 3</dev/tty; } 2>/dev/null; then src=/dev/fd/3; fi
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
    exec 3<&- 2>/dev/null || true
    printf '
' >&2
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
    local first second
    first="$(read_masked '  Password:        ')"
    if [ "${#first}" -lt "$MIN_CHARS" ]; then
        say "too short (${#first} of $MIN_CHARS characters) - nothing changed"
        exit 2
    fi
    second="$(read_masked '  Repeat:          ')"
    if [ "$first" != "$second" ]; then
        say 'the two entries differ - nothing changed'
        exit 2
    fi
    printf '
' >&2
    printf '%s' "$first"
}

connect() {
    # Reach the server BEFORE asking for anything. ssh may want the key's
    # passphrase, and two password prompts in a row with no way to tell them
    # apart is what this ordering exists to prevent (operator, 2026-08-18):
    # ssh asks first, labelled by ssh, and only then does this script ask.
    printf '[admin-password] connecting to %s ...\n' "$TARGET" >&2
    # -n: stdin stays untouched. Without it this ssh reads the very input the
    # password is about to arrive on and forwards it to the remote command —
    # which ate the piped password and would have eaten keystrokes too.
    ssh -n "$TARGET" "test -f \"\$HOME/$DIR/$TARGET_FILE\"" \
        || { say "cannot reach $TARGET, or $TARGET_FILE is not there"; exit 2; }
    printf '[admin-password] connected.\n' >&2
}


cmd_status() {
    ssh "$TARGET" "DIR='$DIR' FILE='$TARGET_FILE' bash -s" <<'REMOTE'
set -eu
cd "$HOME/$DIR" || { echo "[admin-password] no checkout at ~/$DIR" >&2; exit 2; }
[ -f "$FILE" ] || { echo "[admin-password] no $FILE on the server" >&2; exit 2; }
v="$(sed -n "s/^TM_ADMIN_HASH=//p" "$FILE" | tail -1)"
u="$(sed -n "s/^TM_ADMIN_USER=//p" "$FILE" | tail -1)"
printf 'file:  %s (mode %s, owner %s)
' "$FILE" "$(stat -c %a "$FILE")" "$(stat -c %U "$FILE")"
printf 'user:  %s
' "${u:-murara (the Caddyfile default)}"
if [ -n "$v" ]; then
    printf 'hash:  set, %s characters, starts %s
' "${#v}" "$(printf '%.1s' "$v")"
else
    printf 'hash:  EMPTY - /admin refuses every password
'
fi
printf 'edge:  %s
' "$(docker compose ps --format '{{.Status}}' edge | head -1)"
REMOTE
}

# The remote half of `set`. It travels base64-encoded inside the ssh command
# string, which leaves ssh's **stdin** free to carry the value — and, because
# nothing here passes through a shell twice, quoting means what it says. (First version sent this as a heredoc — and then
# `cat` on the far side read an already-consumed stdin and every write was a
# silent no-op.)
REMOTE_SET=$(cat <<'REMOTE'
set -eu
cd "$HOME/$DIR" || { echo "[admin-password] no checkout at ~/$DIR" >&2; exit 2; }
password="$(cat)"
[ -n "$password" ] || { echo "[admin-password] empty; nothing written" >&2; exit 2; }
case "$FILE" in
    .env) git check-ignore -q .env || { echo "[admin-password] .env is not gitignored - refusing" >&2; exit 2; } ;;
esac

# The password itself never lands anywhere: Caddy hashes it (bcrypt) and only
# the hash is stored. -i, so the plaintext travels on stdin.
# A trailing newline is required: hash-password reads a LINE, and without one
# it sees EOF and refuses ("Error: EOF"). --plaintext would put the password
# in the remote process list, which is exactly what stdin avoids.
value=$(printf "%s
" "$password" | docker run --rm -i caddy caddy hash-password 2>/dev/null | tail -1)
[ -n "$value" ] || { echo "[admin-password] hashing failed" >&2; exit 2; }
unset password
# Compose expands $ inside .env values; a bcrypt hash written raw arrives
# mangled and no password ever matches (doc/ADMIN.md 5a). Quoted sed: the
# unquoted version substitutes the shell PID and cost an outage.
case "$FILE" in
    .env) value=$(printf %s "$value" | sed 's/[$]/$$/g') ;;
esac
KEY=TM_ADMIN_HASH

umask 077
touch "$FILE"
grep -v "^$KEY=" "$FILE" > "$FILE.new" || true
printf "%s=%s
" "$KEY" "$value" >> "$FILE.new"
mv "$FILE.new" "$FILE"
chmod 600 "$FILE"
echo "[admin-password] $KEY written to $FILE; recreating: $SERVICES"
STATE=$(sed -n s/^TM_STATE=//p .env | tail -1)
TM_TAG=$(cat "$STATE/deploy/current" 2>/dev/null || echo latest)     docker compose up -d --force-recreate $SERVICES >/dev/null 2>&1
for s in $SERVICES; do
    printf "  %s: %s
" "$s" "$(docker compose ps --format {{.Status}} $s | head -1)"
done
REMOTE
)

cmd_set() {     # the PASSWORD arrives on stdin and only on stdin
    local b64; b64="$(printf %s "$REMOTE_SET" | base64 | tr -d '
')"
    ssh "$TARGET" "DIR='$DIR' FILE='$TARGET_FILE' SERVICES='$SERVICES'                    bash -c \"\$(printf %s '$b64' | base64 -d)\""
}

case "$CMD" in
    status) cmd_status ;;
    set)    connect; read_value "$KEY" | cmd_set; say 'now:'; cmd_status ;;
    *)      say "unknown command $CMD (status | set)"; exit 2 ;;
esac
