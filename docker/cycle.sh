#!/bin/sh
# The cycle — the update — followed, only if it succeeded, by the simulation
# scorecard. Cron runs it Monday 07:00 (docker/crontab); it may also be run by
# hand any day, since nothing in it mails anyone (RUNBOOK 1). The sending is
# docker/deliver.sh, 90 minutes later.
#
# Until 2026-08-18 this file was weekly.sh and its cycle (then loop.py) also
# delivered every customer at the end. The shape is the Windows scheduled
# task's action line from 2026-08-01, and three things about it are
# load-bearing and are reproduced here:
#
#   1. The `&&`. The simulation check runs ONLY if the cycle succeeded. A failed
#      cycle that still appended a scorecard block would put a dated heading in
#      simcheck.log with no run behind it, and that log is read weeks later as
#      the hit rate firms up — a heading nobody can now explain is worse than a
#      gap.
#   2. Both logs are APPENDED, never truncated. simcheck.log accumulates one
#      dated block per week and its whole value is the accumulation.
#   3. The dated heading goes in before the check, so a check that dies halfway
#      still leaves the date it died on.
#
# Everything is also echoed to stdout, so `docker logs` and the log file tell
# the same story — cron's own mail spool does not exist in a container.
set -u

DATA="${TM_DATA_DIR:-/data}"
LOGS="$DATA/logs"
mkdir -p "$LOGS"

# Cron hands a job almost no environment, so the values the scheduler service
# holds reach it through this file — the same way deliver.sh and nightly.sh
# get theirs. The cycle needed none of it until 2026-08-18, when it began to
# end with the salesman's "Heute schreiben" mail (doc/SALES.md 5): without
# RESEND_API_KEY and TM_SALES_OWNER that mail is refused and the cycle prints
# it, which is a silent-looking failure of a visible feature.
if [ -f "$DATA/.cron-env" ]; then
    . "$DATA/.cron-env"
fi

# Extra arguments for the cycle, empty on a normal Monday. This exists for the
# two cases where re-fetching would be wrong: re-running a Monday that died
# after the download (`--skip-download`, the runbook's advice), and any run
# against a state directory without a full `data/raw` archive — features.py
# rebuilds the store from the ENTIRE archive, so a cycle with a partial one
# quietly replaces a 22 MB store with an 810 KB one and then dies in
# single_bidder with `KeyError: 'n_tenders'`. That is not a hypothetical; it is
# what the first test of this script did.
EXTRA="${TM_CYCLE_ARGS:-${TM_WEEKLY_ARGS:-}}"

started=$(date '+%Y-%m-%d %H:%M:%S %Z')
echo "[cron] cycle starting $started${EXTRA:+ (extra args: $EXTRA)}"

# shellcheck disable=SC2086  # EXTRA is a deliberate word-split argument list
if python /app/cycle.py run --last 7d $EXTRA >> "$LOGS/cycle.log" 2>&1; then
    echo "[cron] cycle ok — appending the simulation scorecard"
    # `date` unquoted-formatted like cmd.exe's %date% would look; the point is a
    # legible separator, not a parseable field.
    echo "=== $(date '+%a %Y-%m-%d') ===" >> "$LOGS/simcheck.log"
    if python /app/simulation.py check >> "$LOGS/simcheck.log" 2>&1; then
        echo "[cron] done $(date '+%Y-%m-%d %H:%M:%S %Z')"
    else
        status=$?
        echo "[cron] simulation check failed (exit $status) — see $LOGS/simcheck.log"
        exit $status
    fi
else
    status=$?
    # Deliberately loud and deliberately not retried: a failed cycle is
    # idempotent to re-run by hand (`--skip-download` if the archive is fine),
    # and a cron loop retrying a cycle that trains a model is how two of them
    # end up training at once. deliver.sh at 08:30 will find last week's
    # predictions, refuse, and say so.
    echo "[cron] CYCLE FAILED (exit $status) — simulation check skipped."
    echo "[cron] tail -50 $LOGS/cycle.log"
    tail -50 "$LOGS/cycle.log" 2>/dev/null
    exit $status
fi
