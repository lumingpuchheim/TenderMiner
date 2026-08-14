#!/bin/sh
# The nightly backup — doc/OPERATIONS.md 3, layer 2. Fired by docker/crontab at
# 02:30 as user `tm`; everything echoes to the cron log the same way weekly.sh
# does, because a backup whose failures are silent is a hope, not a backup.
#
# Two halves, deliberately ordered:
#
#   1. EXPORT — always runs. `db.py --export-jsonl` writes the record as
#      readable text (the format the restore path `db.py --migrate` is tested
#      against) and a `VACUUM INTO` snapshot sits beside it. Never a plain `cp`
#      of the live database: a WAL database copied mid-write is silently torn.
#   2. PUSH — runs when restic is configured. Client-side encrypted to an
#      S3 bucket at a *different* provider than the VPS; retention pruned to
#      14 daily / 8 weekly / 12 monthly; on Sundays a --read-data-subset check
#      verifies the repository actually restores.
#
# Not configured is a state, not an error: a fresh server backs up locally and
# says so in the log, loudly, every night — so "we never got around to the
# bucket" cannot masquerade as "backups are running".
#
# Secrets reach this job via /data/.cron-env, written by the scheduler service
# at container start (docker-compose.yml): cron hands its jobs a bare
# environment, so the RESTIC_*/AWS_* values that compose read from .env would
# otherwise never arrive here.
set -u

DATA="${TM_DATA_DIR:-/data}"
LOGS="$DATA/logs"
mkdir -p "$LOGS"

if [ -f "$DATA/.cron-env" ]; then
    . "$DATA/.cron-env"        # `export ...` lines, quoted by `export -p`
fi

stamp=$(date '+%Y-%m-%d %H:%M:%S %Z')
echo "[nightly] backup starting $stamp"

if [ ! -f "$DATA/tendermining.db" ]; then
    echo "[nightly] no database yet ($DATA/tendermining.db) — nothing to export, done"
    exit 0
fi

# ---- 1. export --------------------------------------------------------------

EXPORT="$DATA/export/$(date +%Y-%m-%d)"
mkdir -p "$EXPORT"

python /app/db.py --export-jsonl "$EXPORT" || { echo "[nightly] EXPORT FAILED"; exit 1; }

# VACUUM INTO refuses to overwrite; a re-run the same night replaces the snapshot.
rm -f "$EXPORT/tendermining.db"
python - "$DATA/tendermining.db" "$EXPORT/tendermining.db" <<'PY' || { echo "[nightly] SNAPSHOT FAILED"; exit 1; }
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
con = sqlite3.connect(src)
con.execute("VACUUM INTO ?", (dst,))
con.close()
PY
echo "[nightly] export complete: $EXPORT"

# Local exports are a convenience, not the copy that matters — keep two nights,
# or /data grows by a database-size every day and the disk alarm of
# OPERATIONS.md 1 goes red for the wrong reason.
ls -dt "$DATA"/export/*/ 2>/dev/null | tail -n +3 | while read -r old; do
    echo "[nightly] pruning local export $old"
    rm -rf "$old"
done

# ---- 2. push ----------------------------------------------------------------

if [ -z "${RESTIC_REPOSITORY:-}" ] || [ -z "${RESTIC_PASSWORD:-}" ]; then
    echo "[nightly] NO OFF-SITE COPY: restic is not configured (RESTIC_* in .env)."
    echo "[nightly] The export above exists only on this machine."
    exit 0
fi

# First night against an empty bucket: initialise it. Cheap no-op afterwards.
restic snapshots >/dev/null 2>&1 || restic init || { echo "[nightly] RESTIC INIT FAILED"; exit 1; }

restic backup "$DATA/raw" "$DATA/export" "$DATA/logs" --quiet \
    || { echo "[nightly] RESTIC BACKUP FAILED"; exit 1; }
restic forget --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune --quiet \
    || { echo "[nightly] RESTIC PRUNE FAILED"; exit 1; }
echo "[nightly] off-site push complete"

# Sunday: prove the repository restores, not merely accepts writes.
if [ "$(date +%u)" = 7 ]; then
    echo "[nightly] Sunday verification (read-data-subset 5%)"
    restic check --read-data-subset=5% \
        || { echo "[nightly] RESTIC CHECK FAILED — the backup may not restore"; exit 1; }
    echo "[nightly] verification passed"
fi
