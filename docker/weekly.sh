#!/bin/sh
# The Monday cycle, as the Windows scheduled task "TenderMining weekly loop"
# has run it since 2026-08-01. That task's action is one cmd.exe line:
#
#   python loop.py run --last 7d          >> data\logs\loop_scheduled.log 2>&1
#   && (echo === %date% === >> data\logs\simcheck.log)
#   && python simulation.py check         >> data\logs\simcheck.log 2>&1
#
# Three things about it are load-bearing and are reproduced here:
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

# The mail secrets (RESEND_API_KEY, TM_MAIL_FROM, TM_APP_URL) reach this job
# the way nightly.sh gets its backup secrets: via /data/.cron-env, written by
# the scheduler service at start — cron itself hands out a bare environment.
# Without the file the cycle still runs; the reports are written and every
# send is refused loudly (delivering.py prints it, the ledger records it).
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
EXTRA="${TM_WEEKLY_ARGS:-}"

started=$(date '+%Y-%m-%d %H:%M:%S %Z')
echo "[cron] weekly cycle starting $started${EXTRA:+ (extra args: $EXTRA)}"

# shellcheck disable=SC2086  # EXTRA is a deliberate word-split argument list
if python /app/loop.py run --last 7d $EXTRA >> "$LOGS/loop_scheduled.log" 2>&1; then
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
    # end up training at once.
    echo "[cron] CYCLE FAILED (exit $status) — simulation check skipped."
    echo "[cron] tail -50 $LOGS/loop_scheduled.log"
    tail -50 "$LOGS/loop_scheduled.log" 2>/dev/null
    exit $status
fi
