#!/bin/sh
# The full production replay, scripted — the 2026-08-24 hand-run made
# repeatable. Runs ON THE SERVER HOST (like deploy.sh); it starts a container.
#
#   bash docker/replay.sh                    # full replay, then latest.json + site
#   nohup bash docker/replay.sh > /dev/null 2>&1 &   # the usual way: it is ~5 hours
#   bash docker/replay.sh --measure-only     # replay + peak only; nothing swapped
#   TM_REPLAY_FROM=2026-05-01 bash docker/replay.sh --measure-only
#                                            # the fast peak check, ~80 min
#
# The numbers behind the caps (doc/MEMORY_BUDGET.md, "The 4x store"): peak
# 4,835 MB measured 2026-08-24 at the 118k-row store. `--memory 6g` means a
# regression dies alone in its cgroup — the kernel never picks an innocent
# victim like app.py, which is what its global OOM kill on 2026-08-23 19:23
# nearly did. `--memory-swap 10g` lets a transient overshoot spill to the
# swapfile (docker/swap.sh) instead of dying five hours in, which is what the
# 23:26 kill that night actually did — one cutoff from the end.
#
# rewind_all takes the heavy lock itself with wait=0, so this fails fast and
# loudly when the cycle, backplay or another replay holds it. The output
# document is dated by START day; a run crossing midnight keeps its name.
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# The state directory, resolved the same way deploy.sh resolves it: the
# environment first, then .env (compose's own file), then the checkout.
if [ -z "${TM_STATE:-}" ] && [ -f .env ]; then
    TM_STATE="$(grep -E '^TM_STATE=' .env | tail -n1 | cut -d= -f2-)"
fi
STATE="${TM_STATE:-$REPO/data}"

TAG="${TM_TAG:-$(bash docker/deploy.sh status | awk '/^current:/{print $2}')}"
[ -n "$TAG" ] || { echo "[replay] no deployed tag and no TM_TAG — nothing to run"; exit 2; }
FROM="${TM_REPLAY_FROM:-2024-11-01}"
DAY="$(date +%F)"
OUT="replay/full-$DAY.json"
LOG="$STATE/logs/replay-full-$DAY.log"
mkdir -p "$STATE/logs" "$STATE/replay"

echo "[replay] tendermining:$TAG from $FROM -> $STATE/$OUT (log: $LOG)"
if ! docker run --rm --name "tm-replay-$DAY" \
    --memory 6g --memory-swap 10g \
    -e TZ=Europe/Bucharest -e TM_DATA_DIR=/data \
    -v "$STATE":/data "tendermining:$TAG" \
    python memory_receipt.py --label replay-full -- \
    python rewind_all.py --from "$FROM" --sub nobody --out "/data/$OUT" \
    > "$LOG" 2>&1; then
    echo "[replay] FAILED — tail of $LOG:"
    tail -5 "$LOG"
    exit 1
fi
grep '^\[replay-full\] PEAK' "$LOG" || true

if [ "${1:-}" = "--measure-only" ]; then
    echo "[replay] measure-only: $STATE/$OUT written, latest.json untouched"
    exit 0
fi

# latest.json is swapped only behind a non-empty document from a zero exit —
# same guard chain7 used. trade_pages.py re-renders the forecast section
# from it; a failure there leaves the new latest.json in force (the document
# is the product, the pages are a renderer over it).
[ -s "$STATE/$OUT" ] || { echo "[replay] $STATE/$OUT is empty — latest.json untouched"; exit 1; }
cp "$STATE/$OUT" "$STATE/replay/latest.json"
docker run --rm -e TZ=Europe/Bucharest -e TM_DATA_DIR=/data \
    -v "$STATE":/data "tendermining:$TAG" python trade_pages.py \
    >> "$LOG" 2>&1
echo "[replay] done: latest.json is the $DAY replay, site rebuilt"
