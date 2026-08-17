#!/bin/sh
# The backplay rejector — doc/PARAMETERS.md §10-11, fired by docker/crontab
# NIGHTLY at 04:00 as user `tm`.
#
# Nightly, not weekly, so that whatever moved the evidence — a benchmark label
# read on Tuesday, the Monday store — is measured the next night rather than
# up to a week later. Not wasteful: backplay.py compares the evidence stamp
# (benchmark blob, store files, champion fingerprint) with the one each
# question's last measurement stood on, and re-measures only when it moved.
# The usual night is one line per question and exit in a second — which is
# how you know it is still wired in at all.
#
# Which questions: the queue (`knobs.queue()`), the program's own — one live
# knob per bucket rotating through `knobs.KNOBS`; nobody files one by hand.
# `python backplay.py --show` prints the grids, what was rejected and why.
#
# 04:00 keeps ninety minutes clear of the 02:30 backup, which is I/O-bound
# where this is CPU-bound, and four hours clear of the Monday cycle. The job
# also takes the heavy lock itself, so a collision waits rather than corrupts —
# the clock separation is politeness, the lock is the guarantee.
#
# Never fails the container: the exit status is logged, not propagated. A
# rejector that cannot measure is a night without a measurement, not an outage.
set -u

DATA="${TM_DATA_DIR:-/data}"
LOGS="$DATA/logs"
mkdir -p "$LOGS"

stamp=$(date '+%Y-%m-%d %H:%M:%S %Z')
echo "[backplay] starting $stamp"

if [ ! -f "$DATA/tendermining.db" ]; then
    echo "[backplay] no database yet ($DATA/tendermining.db) — nothing to measure, done"
    exit 0
fi

cd /app || { echo "[backplay] no /app checkout"; exit 0; }

python backplay.py
status=$?

if [ "$status" -eq 0 ]; then
    echo "[backplay] done $(date '+%H:%M:%S %Z')"
else
    echo "[backplay] FAILED (exit $status) — no candidate was rejected on this run"
fi
exit 0
