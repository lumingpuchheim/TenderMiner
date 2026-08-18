#!/bin/sh
# The delivery — the sending. Cron runs it Monday 08:30 (docker/crontab),
# ninety minutes after cycle.sh, and it is the ONE scheduled job that reaches
# a customer (RUNBOOK 1). It reads the predictions the cycle wrote; it trains
# nothing, and if the newest prediction is older than a day it refuses and
# says so — a dead 07:00 cycle means no mail, never a mail job that quietly
# retrains.
set -u

DATA="${TM_DATA_DIR:-/data}"
LOGS="$DATA/logs"
mkdir -p "$LOGS"

# The mail secrets (RESEND_API_KEY, TM_MAIL_FROM, TM_APP_URL) reach this job
# the way nightly.sh gets its backup secrets: via /data/.cron-env, written by
# the scheduler service at start — cron itself hands out a bare environment.
# Without the file the delivery still runs; the reports are written and every
# send is refused loudly (delivering.py prints it, the ledger records it).
if [ -f "$DATA/.cron-env" ]; then
    . "$DATA/.cron-env"
fi

EXTRA="${TM_DELIVER_ARGS:-}"

started=$(date '+%Y-%m-%d %H:%M:%S %Z')
echo "[cron] delivery starting $started${EXTRA:+ (extra args: $EXTRA)}"

# shellcheck disable=SC2086  # EXTRA is a deliberate word-split argument list
if python /app/deliver.py run $EXTRA >> "$LOGS/deliver.log" 2>&1; then
    echo "[cron] delivery done $(date '+%Y-%m-%d %H:%M:%S %Z')"
else
    status=$?
    # Exit 2 is the staleness refusal (deliver.py); anything else is a
    # failure. Neither is retried here: a delivery is idempotent per day, so
    # once the cause is fixed `docker compose run --rm tm python deliver.py run`
    # by hand sends exactly what the schedule would have.
    if [ "$status" -eq 2 ]; then
        echo "[cron] DELIVERY REFUSED — predictions are stale (no cycle since last week?)."
    else
        echo "[cron] DELIVERY FAILED (exit $status)."
    fi
    echo "[cron] tail -30 $LOGS/deliver.log"
    tail -30 "$LOGS/deliver.log" 2>/dev/null
    exit $status
fi
