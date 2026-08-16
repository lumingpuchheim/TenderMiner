#!/bin/sh
# The backplay rejector — doc/PARAMETERS.md §10, fired by docker/crontab on
# Sunday 04:00 as user `tm`.
#
# Sunday, not nightly: the evidence it reads changes weekly at best (awards
# lag deadlines by ~3 months, the benchmark grows when a human reads lots), so
# a nightly run would re-measure the same numbers and burn a 4-vCore VPS doing
# it. Sunday also puts the result in front of Monday 08:15 — the cycle's report
# carries the rejections, so the operator reads them where they already look
# instead of in a log.
#
# 04:00 keeps ninety minutes clear of the 02:30 backup, which is I/O-bound
# where this is CPU-bound, and four hours clear of the Monday cycle. The job
# also takes the heavy lock itself, so a collision waits rather than corrupts —
# the clock separation is politeness, the lock is the guarantee.
#
# WITH NO FILED QUESTION THIS EXITS IN A SECOND and says so. That is the
# expected state most weeks (PARAMETERS.md §8.1: a knob is live only while a
# question is open), and a job that is loud about doing nothing is how you know
# it is still wired in at all.
#
# Never fails the container: the exit status is logged, not propagated. A
# rejector that cannot measure is a week without a proposal, not an outage.
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
