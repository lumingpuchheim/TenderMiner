#!/usr/bin/env bash
#
# Proves the public site's publish path end to end, on Linux, with a real
# Caddy — doc/TRADE_PAGES.md 6. Run by .github/workflows/site-check.yml on
# every push that touches it; runnable by hand on any machine with Docker:
#
#   bash docker/site-check.sh
#
# What it sets up is the server, in miniature: caddy:2 mounts a directory as
# /srv/public and serves /srv/public/current — the same mount and the same
# root the edge uses (docker-compose.yml, docker/Caddyfile). This machine's
# python calls trade_pages.release on that directory, which is what the cycle
# and deploy.sh call (from inside the image, but the file operations are the
# same). Between steps, curl through Caddy. Needs Docker, curl, and the
# project's python dependencies (as the unit tests do).
#
# What it asserts, in order:
#   1. the legacy flat layout (today's server) is left alone by the first
#      release, and the new build is what Caddy serves;
#   2. a second release swaps in atomically, deletes the previous build AND
#      the flat layout — public/ then holds `current` and one directory;
#   3. a build that dies leaves the previous site serving and no half-build;
#   4. the mounted directory keeps its inode — never deleted, never recreated.
#
# Any failed assertion exits non-zero, which is what makes it a check rather
# than a demo.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
WORK="$(mktemp -d)"
PUB="$WORK/public"
PORT="${SITE_CHECK_PORT:-8099}"
NAME="tm-site-check-$$"
mkdir -p "$PUB"
chmod 777 "$PUB"           # the python container writes as uid 1000, caddy reads

fail() { printf 'site-check FAILED: %s\n' "$*" >&2; exit 1; }
say()  { printf '[site-check] %s\n' "$*"; }
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT

cat > "$WORK/Caddyfile" <<EOF
http://:$PORT {
	root * /srv/public/current
	file_server
}
EOF

# today's server: a flat layout under public/, served by an edge whose root
# was /srv/public. Here the edge already has the new root; what we check is
# that release does not pull the flat files from under an old edge.
echo flat > "$PUB/index.html"
mkdir -p "$PUB/gewerke"; echo flat > "$PUB/gewerke/index.html"
INO_BEFORE=$(stat -c %i "$PUB")

docker run -d --rm --name "$NAME" -p "127.0.0.1:$PORT:$PORT" \
    -v "$WORK/Caddyfile:/etc/caddy/Caddyfile:ro" \
    -v "$PUB:/srv/public:ro" caddy:2 >/dev/null
for _ in $(seq 1 30); do
    curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break || sleep 1
done

# release <marker>: a complete two-page site whose every file reads <marker>.
# Run with this machine's python (needs the project's dependencies, as the
# unit tests do): the same rename/unlink/rmtree calls the container makes,
# on the same directory Caddy is reading through its mount.
PY="${PYTHON:-python3}"
release() {
    "$PY" - "$PUB" "$1" <<'PY'
import sys
sys.path.insert(0, '.')
import trade_pages as tp
pub, m = sys.argv[1], sys.argv[2]
def write(d):
    (d / 'index.html').write_text(m)
    (d / 'gewerke').mkdir()
    (d / 'gewerke' / 'index.html').write_text(m)
tp.release(pub, write)
PY
}
dead_release() {
    "$PY" - "$PUB" <<'PY'
import sys
sys.path.insert(0, '.')
import trade_pages as tp
def die(d):
    (d / 'index.html').write_text('half')
    raise RuntimeError('the store went away mid-build')
try:
    tp.release(sys.argv[1], die)
except RuntimeError:
    sys.exit(0)
sys.exit(1)
PY
}
served() {   # served <path> -> "<code> <body>"
    local code body
    code=$(curl -s -o "$WORK/body" -w '%{http_code}' "http://127.0.0.1:$PORT/$1")
    body=$(cat "$WORK/body")
    printf '%s %s' "$code" "$body"
}
builds() { find "$PUB" -maxdepth 1 -name 'site-*' | wc -l; }

say "1. first release onto the flat layout"
release one
[ "$(served index.html)" = "200 one" ]         || fail "after release one, / served: $(served index.html)"
[ "$(served gewerke/index.html)" = "200 one" ] || fail "after release one, /gewerke/ served: $(served gewerke/index.html)"
[ "$(cat "$PUB/index.html")" = flat ]          || fail "first release must leave the flat layout for the old edge"
[ "$(builds)" = 1 ]                            || fail "expected one build, have $(builds)"
say "   serves 'one'; flat layout kept; one build"

say "2. second release: atomic swap, previous build and flat layout removed"
release two
[ "$(served index.html)" = "200 two" ]         || fail "after release two, / served: $(served index.html)"
[ "$(served gewerke/index.html)" = "200 two" ] || fail "after release two, /gewerke/ served: $(served gewerke/index.html)"
[ ! -e "$PUB/index.html" ]                     || fail "flat index.html should be swept by the second release"
[ ! -e "$PUB/gewerke" ]                        || fail "flat gewerke/ should be swept by the second release"
[ "$(builds)" = 1 ]                            || fail "expected one build after the swap, have $(builds)"
[ "$(ls -A "$PUB" | wc -l)" = 2 ]              || fail "public/ should hold current + one dir, holds: $(ls -A "$PUB")"
[ -L "$PUB/current" ]                          || fail "current is not a symlink"
case "$(readlink "$PUB/current")" in
    /*) fail "current is an absolute link ($(readlink "$PUB/current")); it must resolve inside the container too" ;;
esac
say "   serves 'two'; public/ = $(ls -A "$PUB" | tr '\n' ' ')"

say "3. a build that dies leaves 'two' serving and no half-build"
dead_release || fail "dead_release did not raise as expected"
[ "$(served index.html)" = "200 two" ]         || fail "after a dead build, / served: $(served index.html)"
[ "$(builds)" = 1 ]                            || fail "the half-build was not removed: $(ls -A "$PUB")"
say "   still serves 'two'; one build"

say "4. the mounted directory was never recreated"
[ "$(stat -c %i "$PUB")" = "$INO_BEFORE" ]     || fail "public/ inode changed — the mount would be dead"
say "   inode unchanged"

say "OK"
