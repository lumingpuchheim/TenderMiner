# OPERATIONS — keeping the one machine alive

Written 2026-08-13, from the operator's question "the VM must always be
available — how to check health, how to roll back, how to back up". Revised
the same day after two operator corrections: **no dependency on the laptop**
(backups must be automatic, machine-to-machine) and **no git-revert as the
rollback mechanism** (rollback is switching back to the previous image,
Vercel-style; git stays the way code rolls *forward*). This supersedes the
"laptop is the backup" decision of 2026-08-10 recorded in
[`HOSTING.md`](HOSTING.md) §1.

This is the strategy; nothing in it is built yet except where a line says
so. Companions: [`RUNBOOK.md`](RUNBOOK.md) (how to run the cycle),
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

Targets, stated so they can be disagreed with:

- **App restart: minutes, no human.** Docker's restart policies are already
  in `docker-compose.yml` (`restart: always` / `unless-stopped`, plus a
  container healthcheck) — the daemon is the first responder.
- **A bad deploy never takes the app down:** the old image keeps serving
  until the new one has proven healthy (§2).
- **Data loss on total machine failure: at most 24 hours**, restored from
  storage that no laptop, house or single provider account is part of (§3).
- **Dead machine: rebuilt in half a working day, by hand** (§4). No second
  server, no automatic failover — see §5 for why.

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
3. **Is the disk filling?** State grows ~90 MB/week. Same rule, same
   endpoint: 503 when free space on `/data` drops under 2 GB — alarmed
   weeks before it matters at this growth rate.

The same 503 rule is what the deploy gate of §2 probes: one health
definition, used by the pinger from outside and the deploy script from
inside, so "healthy" cannot mean two different things.

Escalation is three layers, cheapest first: dockerd restarts a crashed
container (exists) → the pinger tells the operator's phone (to arrange) →
the operator reads `/data/logs/` over SSH, where cron already writes the
evidence ([`docker/crontab`](../docker/crontab)'s redirect lesson).

What is deliberately NOT watched: per-stage cycle timing, memory graphs,
request rates. One person operates this; an alert that is not actionable at
a phone is noise.

## 2. Rollback: images switch, git only rolls forward

Operator decision 2026-08-13: rollback must not be a git operation. A
revert commit is a *new build* — it can itself fail to build, and it
rewrites nothing about the artifact that was already known to work. The
mechanism is the one Vercel made familiar: **every deploy is an immutable
image, the previous image stays on disk, and traffic switches only after
the new build has proven itself. Rolling back is switching the pointer
back.**

Concretely, on this machine (**built 2026-08-14**: [`docker/deploy.sh`](../docker/deploy.sh),
driven on push by [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml)
through the forced-command key of [`docker/deploy-ssh.sh`](../docker/deploy-ssh.sh);
a new server is wired up once by [`docker/bootstrap.sh`](../docker/bootstrap.sh)):

1. **Build under a unique tag.** `docker build -t murara/app:<git-sha>`.
   A build failure changes nothing: the running container is untouched —
   this much is already true of compose today, and the tag makes it
   explicit.
2. **Prove the new image before it gets traffic.** Start the new container
   alongside the old on an internal port, probe *its* `/healthz` (the §1
   rule, so "healthy" includes "sees the data directory") until it passes
   or a 60 s deadline kills it. Only then repoint and recreate; Caddy in
   front means the switch is one upstream change, invisible to a customer
   mid-click.
3. **Failure = no switch.** The probe failing leaves the old container
   serving, the failed image on disk for inspection, and a loud exit code
   for the GitHub Action to surface. The bad deploy is an incident report,
   never an outage.
4. **Rollback = redeploy the previous tag.** The script records
   `current` and `previous` tags in `/data/deploy/`; `deploy.sh rollback`
   re-runs step 2 against `previous`. No git involved, no rebuild, the
   artifact that switches in is bit-for-bit the one that served yesterday.
   Keep the last 5 tags, prune older.
5. **The cycle rides the same tags.** The scheduler container is recreated
   at deploy time too, so next Monday runs the same image the app proved.
   A bad Monday whose cause is code: `deploy.sh rollback`, then rerun by
   hand with `--skip-download` ([`RUNBOOK.md`](RUNBOOK.md) §1b).

Git's role shrinks to what it is good at: `master` is how code rolls
*forward*, and the worktree→master discipline is the review gate. Fixing
the bug that forced a rollback is a normal forward merge, deployed through
the same gate as everything else.

**Models** need no rollback machinery: they never deploy, the server
retrains and promotes them every Monday ([`HOSTING.md`](HOSTING.md) §1). A
bad champion is rolled back by deploying the previous image and re-running
the cycle.

**The database** is not rolled back by redeploying images — restoring it to
an earlier night is a data operation (§3), deliberately separate: rolling
back code must never silently roll back customer data with it.

## 3. Backups: automatic, machine-to-machine, no laptop

Operator decision 2026-08-13: the laptop is out of the loop. Two
independent layers, both automatic, protecting against different failures:

**Layer 1 — the provider's own VPS backup (one checkbox, whole machine).**
OVH includes a free daily automated backup with every VPS (24 h retention),
stored on servers separate from the one hosting the VPS; the Premium option
extends that to 7 rolling daily backups with a schedulable window, priced
by VPS disk size. Infomaniak's Public Cloud offers instance snapshots
similarly. Whichever provider hosts the VM (open decision,
[`HOSTING.md`](HOSTING.md) §4), turn the daily image backup on — it is the
fastest path back from "the machine broke" and costs either nothing or a
few euros. **Its limit, stated plainly: it lives in the same datacentre
and the same provider account.** It is the recovery convenience, not the
disaster copy.

**Layer 2 — nightly encrypted push to object storage at a *different*
provider (the disaster copy).** One cron line in
[`docker/crontab`](../docker/crontab) (to write), at 02:30:

1. `python db.py --export-jsonl` into `/data/export/<date>/` — the record
   as readable text, the format the restore path (`db.py --migrate`) is
   tested against, plus a `VACUUM INTO` snapshot beside it (never a plain
   `cp` of a live WAL database).
2. `restic backup` of `/data/raw`, `/data/export`, `/data/logs` to an
   S3-compatible bucket. restic encrypts client-side (the exports contain
   customer data — they must not sit readable in any bucket), deduplicates
   (the append-only archive uploads ~13 MB/day, not 1.3 GB), keeps a
   retention policy (`--keep-daily 14 --keep-weekly 8 --keep-monthly 12`),
   and can verify itself.
3. `restic check` weekly (a `--read-data-subset` pass), its result echoed
   into the cron log — an unverified backup is a hope, not a backup.

The bucket lives at whichever of the two providers does *not* host the VM:
VM at OVH → bucket at Infomaniak Swiss Backup (S3-compatible, Swiss,
flat-priced — ~CHF 2/month at the 50 GB tier against ~2 GB of state
today); VM at Infomaniak → OVH Object Storage. Cross-provider is the
point: no single account compromise, billing failure or datacentre event
reaches both copies. The restic password goes in the operator's password
manager — it is the one secret without which layer 2 is unreadable, and it
must never live only on the machine it protects.

Embeddings, models and the store are deliberately not in layer 2: the
cycle recomputes all of them from `raw` + the database
([`HOSTING.md`](HOSTING.md) §0).

**Restore drill, quarterly:** `restic restore` into a scratch directory on
any machine, `db.py --migrate`, run one cycle against it in Docker. This
is what the [`HOSTING.md`](HOSTING.md) §0a measurement already did once
from a local copy; the drill repeats it from the bucket on purpose and
notes the date in [`RUNBOOK.md`](RUNBOOK.md). Specified step by step,
with preconditions and what gets written down, in [`DRILL.md`](DRILL.md).

## 4. The dead-machine runbook — RTO half a working day

The design premise: the VM is cattle, `data/` is the pet. Everything on the
machine except `data/` is reproducible from git and public registries.

**Machine broke, provider fine:** restore yesterday's image from layer 1
in the provider panel — minutes to an hour, done. The rest of this section
is for the worse case.

**Machine and provider backup both gone:**

1. Rent a fresh VM (any provider — 2 cores suffice, RAM per the
   [`HOSTING.md`](HOSTING.md) §4 sizing decision), install Docker.
2. `git clone`; `restic restore` the newest snapshot from the bucket;
   `python db.py --migrate`; `docker compose up -d`.
3. Point DNS at the new IP. **Prerequisite, one setting, do it now:**
   `app.murara.eu` TTL at 300 s — a 24 h TTL discovered on the day of the
   fire adds a day to the half-day.
4. Re-arm the secrets from the operator's password manager: the spec is
   [`SECRETS.md`](SECRETS.md) — `bash docker/secrets.sh sync` (to build)
   pulls each key from the manager and writes it into the server's `.env`
   over SSH; until then, `ssh` in and edit `.env` by hand. They live in
   the environment, never in git; the full list is
   [`.env.example`](../.env.example).
5. Run the cycle once by hand; `/healthz` goes green; the pinger confirms
   from outside.

Tokens in printed letters survive all of this: they live in the database,
not on the machine.

**The host itself** (added 2026-08-15, after an audit of the first server
found SSH password login effectively on — cloud-init's drop-in outranked
the `no` in `sshd_config` — the deploy user with a password, no firewall,
no fail2ban, and ~5,000 guessing attempts a day): `bootstrap.sh` now runs
[`docker/harden.sh`](../docker/harden.sh) as its step 1b. Keys-only SSH,
root login off, the deploy user's password locked, fail2ban on the sshd
jail, ufw with only 22/80/443 open, unattended-upgrades. Idempotent, so it
is also the "did the box drift?" check: `sudo bash docker/harden.sh`. The
deploy user stays root-equivalent (docker group + passwordless sudo) by
design — the point is that only a key reaches it.

## 5. What is deliberately not built

No second server, no load balancer, no automatic failover, no Postgres
(the trigger for that is recorded: the day cycle and app live on different
machines, [`HOSTING.md`](HOSTING.md) §1), no orchestrator, no metrics
stack. The service's real-time surface is one lightweight app; the product
ships weekly by e-mail and letter. Hours of downtime are invisible to
customers; a lost `data/` directory is fatal. So the budget goes to copies,
drills and the deploy gate — not to nines.

## 6. Open decisions — operator's

| decision | note |
| --- | --- |
| VM provider | unchanged from [`HOSTING.md`](HOSTING.md) §4; this document works with either, and fixes only that the backup bucket goes to the *other* one |
| provider backup tier | OVH: free daily (24 h) vs Premium (7 days, priced by disk); either satisfies layer 1 |
| uptime watcher | any free tier with phone alerting; needs HTTPS + status-code checks only, because §1 makes `/healthz` carry the semantics |
| DNS TTL 300 for `app.murara.eu` | one setting at Infomaniak, zero cost, buys hours in §4 |

## 7. Order of work

1. ~~The `/healthz` 503 rule~~ **DONE 2026-08-14** (app.py; red on stale
   cycle, low disk, or an unreadable state directory — unknown reads as red).
2. ~~The deploy script of §2~~ **DONE 2026-08-14**, plus what §4 steps 1–2
   assumed would stay manual: `docker/bootstrap.sh` wires a fresh server into
   the push-to-deploy loop in one run, and re-running it is the key-rotation
   procedure. One nuance vs. the spec above: the gate does not require a green
   `/healthz` (a stale cycle would then block deploying the fix for the stale
   cycle, and a first deploy could never switch) — it requires the part the
   image is responsible for: the app answers and can read `/data`. Staleness
   stays the pinger's question.
3. The nightly export + restic line in `docker/crontab`, against a real
   bucket; then the weekly `restic check`.
4. The pinger, once there is a public endpoint to ping.
5. One timed dead-machine drill (§4, worse case) before the first letter
   goes out — the only way to know the half-day is real.
