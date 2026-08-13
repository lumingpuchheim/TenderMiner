# OPERATIONS — keeping the one machine alive

Written 2026-08-13, from the operator's question "the VM must always be
available — how to check health, how to roll back, how to back up". This is
the strategy; nothing in it is built yet except where a line says so. It
builds on the decisions already recorded in [`HOSTING.md`](HOSTING.md) §1
(one machine, laptop is the backup, deploy = merge to master) and does not
re-open them. Companions: [`RUNBOOK.md`](RUNBOOK.md) (how to run the cycle),
[`STORAGE.md`](STORAGE.md) (what the data is), [`APP.md`](APP.md) §9 (the
app being kept up).

## 0. What "always available" actually requires

Three parts with three very different duties
([`HOSTING.md`](HOSTING.md) §0):

| part | duty | tolerated downtime |
| --- | --- | --- |
| the app | a customer clicks a token link at 21:00 on a Sunday | minutes, unattended |
| the cycle | one successful run per week, Monday 08:15 | days — but **never a silently missed Monday** |
| the public site | static, hosted elsewhere | not this machine's problem |

Targets this document designs for, stated so they can be disagreed with:

- **App restart: minutes, no human.** Docker's restart policies are already
  in `docker-compose.yml` (`restart: always` / `unless-stopped`, plus a
  container healthcheck) — the daemon is the first responder.
- **Dead machine: rebuilt in half a working day, by hand** (§4). No second
  server, no automatic failover — see §5 for why.
- **Data loss on total disk failure: at most 24 h of app writes** (nightly
  export) **and at most one week of raw archive** (weekly laptop pull) —
  and the archive gap is usually refetchable, because a week is inside the
  window TED still serves.

## 1. Health: three questions, one endpoint, one watcher

The three ways this machine fails quietly: the app stops answering, the
Monday cycle stops succeeding, the disk fills. All three surface at one
place — `/healthz` (exists, [`app.py`](../app.py)) — watched by one external
pinger.

1. **Is the app answering?** A free uptime service requests
   `https://app.murara.eu/healthz` every 5 minutes and alerts the
   operator's phone on failure. Pinging over HTTPS also covers certificate
   renewal (Caddy renews on its own; a renewal that failed anyway turns the
   probe red at expiry, not at customer-complaint time).
2. **Did the cycle run?** `/healthz` already reports `cycle_age_days` from
   the loop checkpoint. But it always answers 200, so a dumb pinger cannot
   see staleness. **Change to make (small): `/healthz` answers 503 when
   `cycle_age_days` > 8 or is unknown, body unchanged.** Then the same
   pinger that watches uptime also catches the failure mode
   [`HOSTING.md`](HOSTING.md) §3 №9 warns about — a server Monday failing
   with nobody looking. Unknown deliberately reads as red: a fresh deploy
   goes green by running the cycle once (§4 step 5), not by being excused.
3. **Is the disk filling?** State grows ~90 MB/week, exports (§3) add more.
   Same rule, same endpoint: 503 when free space on `/data` drops under
   2 GB. One threshold, alarmed weeks before it matters at this growth
   rate.

Escalation is three layers, cheapest first: dockerd restarts a crashed
container (exists) → the pinger tells the operator's phone (to arrange) →
the operator reads `/data/logs/` over SSH, where cron already writes the
evidence ([`docker/crontab`](../docker/crontab)'s redirect lesson).

What is deliberately NOT watched: per-stage cycle timing, memory graphs,
request rates. One person operates this; an alert that is not actionable at
a phone is noise. The cycle's own diagnostics stay in `loop_scheduled.log`
and `simcheck.log`, read when the phone buzzes, not before.

## 2. Rollback: code and data are different questions

**Code.** Deploy is `git pull && docker compose build && up -d`
([`HOSTING.md`](HOSTING.md) §1), so rollback is the same action from a
revert commit: `git revert`, merge, redeploy. Never a force-push (deny-listed
anyway), never image archaeology — git is the source of truth and the image
rebuild is seconds. Two clocks to keep in mind:

- The **app** picks the fix up at redeploy — minutes.
- The **cycle** picks code up next Monday. A bad merge discovered *after* a
  Monday means: revert, then rerun the cycle by hand with
  `--skip-download` ([`RUNBOOK.md`](RUNBOOK.md) §1b) — re-fetching is the
  only non-idempotent part of a rerun.

**Models.** Nothing to roll back and nothing to back up: models never
deploy, the server retrains and promotes them every Monday
([`HOSTING.md`](HOSTING.md) §1). A bad champion is rolled back by reverting
the code that trained it and re-running the cycle.

**The database.** The tested path already exists and is the backbone of
everything here: `python db.py --export-jsonl DIR` writes the whole record
as text, and `python db.py --migrate` rebuilds the database from it — that
round trip is the documented rollback for the SQLite migration itself
(CLAUDE.md). Restoring to any night = seed `data/` with that night's
export, `--migrate`, done. A binary snapshot (`VACUUM INTO`, never a plain
`cp` of a live WAL database) can sit beside the export as a convenience,
but the export is the copy that is trusted, because it is the one format a
person can read, grep and diff before restoring it.

**The raw archive** (`data/raw`) is append-only. There is no rollback
concept for it, only loss — which is what §3 bounds.

## 3. Backups: the 2026-08-10 decision, made concrete

"The laptop is the backup" stays. Two jobs turn it from a sentence into a
system:

**Nightly, on the server** (one line in [`docker/crontab`](../docker/crontab),
to write): at 02:30, `db.py --export-jsonl` into `/data/export/<date>/`,
plus a `VACUUM INTO` snapshot beside it. Keep 14 nights, prune older —
bounded at roughly a GB at today's database size, shrinking nothing that
matters (the exports compress well and the raw archive is not part of
them).

**Weekly, on the laptop** (a scheduled task, to write — the laptop is
already awake for Mondays): pull the newest export, the week's new
`data/raw` files, and the logs. ~90 MB/week over rsync/SFTP. The pull ends
by printing row counts per ledger from the pulled export next to last
week's — a backup nobody counts is a hope, not a backup.

**Restore drill, quarterly:** seed a scratch state directory from the
laptop copy (`--migrate` + raw), run one cycle against it in Docker. This
is not a new invention — it is exactly what the [`HOSTING.md`](HOSTING.md)
§0a measurement run already did once; the drill is repeating it on purpose
and noting the date in [`RUNBOOK.md`](RUNBOOK.md).

**Third copy** — open decision (§6): the same nightly export pushed to
Infomaniak storage (Swiss, flat-priced) would survive the burglar who takes
the laptop *and* the fire that takes the server. ~2 GB today. Not required
for launch at the current scale; recorded so it is a decision, not an
oversight.

## 4. The dead-machine runbook — RTO half a day

The design premise: the VM is cattle, `data/` is the pet. Everything on the
machine except `data/` is reproducible from git and public registries.

1. Rent a fresh VM (any provider — 2 cores suffice, RAM per the
   [`HOSTING.md`](HOSTING.md) §4 sizing decision), install Docker.
2. `git clone`, restore state: laptop's `data/raw` + newest export +
   `python db.py --migrate`, then `docker compose up -d`.
3. Point DNS at the new IP. **Prerequisite, one setting, do it now:**
   `app.murara.eu` TTL at 300 s — a 24 h TTL discovered on the day of the
   fire adds a day to the half-day.
4. Re-enter the secrets from the operator's password manager (`TM_*`,
   Resend key). They live in the environment, never in git — the full list
   belongs in a committed `.env.example` (to write, §6).
5. Run the cycle once by hand; `/healthz` goes green; the pinger confirms
   from outside.

Tokens in printed letters survive all of this: they live in the database,
not on the machine.

## 5. What is deliberately not built

No second server, no load balancer, no automatic failover, no Postgres (the
trigger for that is recorded: the day cycle and app live on different
machines, [`HOSTING.md`](HOSTING.md) §1), no orchestrator, no metrics
stack. The service's real-time surface is one lightweight app; the product
ships weekly by e-mail and letter. Hours of downtime are invisible to
customers; a lost `data/` directory is fatal. So the budget goes to copies
and drills, not to nines.

## 6. Open decisions — operator's

| decision | note |
| --- | --- |
| uptime watcher | any free tier with phone alerting; needs HTTPS + status-code checks only, because §1 makes `/healthz` carry the semantics |
| `/healthz` 503 rule | small code change; blocks the one-pinger design, so it is first in §7 |
| third copy at Infomaniak | ~2 GB, flat pricing; decide before the first paying customer |
| DNS TTL 300 for `app.murara.eu` | one setting at Infomaniak, zero cost, buys hours in §4 |

## 7. Order of work

1. The `/healthz` 503 rule (small; testable on the laptop by aging the
   checkpoint file).
2. The nightly export line + pruning in `docker/crontab`.
3. The laptop pull task, with the row-count comparison.
4. The pinger, once there is a public endpoint to ping.
5. One timed dead-machine drill (§4) before the first letter goes out —
   the only way to know the half-day is real.
