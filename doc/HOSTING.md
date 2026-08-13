# HOSTING — what is missing before the backend goes live

Written 2026-08-10, from the operator's question "before I rent a VPS, can you
tell me what is missing?". This is the to-do inventory for moving the backend
onto an always-on machine, with the measurements that sized it. Companions:
[`LAUNCH.md`](LAUNCH.md) §4 (the hosting decision and the public/personal
rule), [`APP.md`](APP.md) (the app being hosted), [`STORAGE.md`](STORAGE.md)
6.5 (the container the whole thing runs in), [`RUNBOOK.md`](RUNBOOK.md) §1b–1d
(how to run it).

## 0. The shape of what is being hosted

Three parts, and only one of them needs to be awake:

| part | needs | uptime |
| --- | --- | --- |
| the app (`app.py`) | 15 MB idle (measured) | **24/7** — a customer clicks at 21:00 on a Sunday |
| the cycle (`loop.py` weekly) | **2.9 GB peak, ~28 min** in the container — was 6.1 GB before the embed fixes (measured 2026-08-13, §0a) | Mondays 08:15 |
| the public website | none — static files, zero forms, zero backend ([`LAUNCH.md`](LAUNCH.md) §4.1) | n/a, static host |

State that must live on the machine: `data/raw` 1.3 GB (irreplaceable —
TED only serves recent packages), the database 111 MB (irreplaceable — what we
promised customers), embeddings 513 MB + store 23 MB + models (all recomputed
by the cycle). **≈2 GB, growing ~90 MB/week** with the raw archive.

## 0a. What the cycle actually costs — measured 2026-08-13

One real `loop.py run --last 7d`, in the container, against a Docker volume
seeded from the true state, on the full 44,780-file archive. **The RAM figure
that mattered was wrong until this was run**, so the method is written down
with it.

**Read anonymous memory, not the container's charge.** cgroup v2 bills a
container for every file page it touches, so `memory.current` and
`memory.peak` include page cache — which the kernel reclaims under pressure
and which is therefore *not* a hardware requirement. The uncapped cycle
reported a 7,091 MB peak that way, and 5,500 MB of it was cache: it fell to
1,553 MB the moment the process exited. The number to size a machine on is
the `anon` line of `memory.stat`.

| stage | peak anonymous | wall clock |
| --- | --- | --- |
| **embeddings sidecar** | **6,071 MB** | ~2 min |
| download + store rebuild | 1,019 MB | 13 min (TED at 0.2–0.5 MB/s) |
| store load (parquet → frames) | 596 MB | 20 s |
| grading, training, delivery, simulation | 2,420–3,033 MB — the model stayed resident behind them | 13 min |

**The peak is one default, not a hardware floor.** [`embed.py`](../embed.py)
calls `embed_texts()` with `batch_size=64`, and `prep_text` truncates every
lot to 2,000 characters — so 64 maximum-length sequences are in flight at
once. Same 278 lots, same model, same output:

| `batch_size` | peak anonymous |
| --- | --- |
| 64 (default) | 6,071 MB |
| 16 | 2,380 MB |
| 8 | 1,672 MB |

Two things that look like levers and are not:

- **Cores do not change it.** 2 cores peaked at 1,517 MB against 8 cores'
  1,501 MB. A smaller VPS costs nothing here, a bigger one buys nothing.
- **ONNX reads the *host's* core count, not the container's.** On a 2-core
  box it still starts 8 threads and prints
  `pthread_setaffinity_np failed … Specify the number of threads explicitly`
  once per thread. Noise, not a fault — but set the thread count explicitly
  on a small machine.

### What it costs after the two fixes

`batch_size=16` and `unload_model()` (both in [`embed.py`](../embed.py)).
Every run below is one job **alone** in its own container under
`--memory=3g --memory-swap=0`, on the rebuilt image, against a volume seeded
from the real state:

| job, run alone | peak anonymous | outcome |
| --- | --- | --- |
| weekly cycle, 278 lots to embed | 2,958 MB | `[done]`, no OOM |
| replay, `--step 21`, 16 cutoffs | 2,287 MB | `exit=0`, 435 s |
| weekly cycle, nothing to embed | 1,044 MB | `[done]` |

Three things this table does not say, and should not be read as saying:

- **2,958 MB is 96% of the budget.** Nothing was killed, so true demand is
  below 3,072 MB; the sampler reads at 2 Hz, so each figure is a lower bound.
  The margin is a few hundred MB and the store grows ~90 MB/week.
- **The replay's 2,287 MB includes a model it should not have loaded.**
  Three uncached words pulled in the whole ONNX runtime — the same trap
  [`MEMORY_BUDGET.md`](MEMORY_BUDGET.md) documents, which measures the replay
  at 1,436 MB with a complete vocabulary. `embed_vocab.py` is the fix and it
  is worth ~850 MB.
- **The embed stage is 1.9 GB of the cycle's 3.0 GB.** What to do about that
  is below — the obvious answer was tried and does not work.

### One open of the model, for both jobs that need it

**Opening the model costs ~1.2 GB whether it is then handed 278 tender texts
or one word.** The expense is the opening. There are exactly two jobs that
need it, and nothing else in the system does — scoring, selection, the gate
and the reports all read numbers already on disk:

1. **lot texts**, once a week, in the cycle's embed stage;
2. **single words**, for the synonym fallback in evidence tier 3, when
   neither an exact nor a typo-tolerant match hits.

Job 2 used to open the model on its own, twice over: during **delivery** the
moment a keyword met a word no tender had used before, and again in the next
replay that met one. Delivery is the worst possible place for a surprise
gigabyte — that stage is writing customer reports, and on a 4 GB machine the
failure is not a worse report but no report.

`loop.py` now runs both jobs in one open and releases the model afterwards
(in a `finally`, so grading, training and delivery never inherit it).
Measured with **both** jobs given real work — 278 lots missing from the
sidecar, 500 words removed from the vocabulary cache:

| | before | after |
| --- | --- | --- |
| times the model is opened | 2 (embed, then delivery) | **1** |
| delivery | 2,334 MB | **1,721 MB** |
| peak anonymous | 2,958 MB | 2,924 MB |
| the word job itself | a fresh 1.2 GB open | **9 s**, no open |

The vocabulary scan over 24k lots costs ~35 s a week, paid even on weeks with
no new words. The operator's call, 2026-08-13: worth it.

What this does *not* do is lower the peak — that is the lot embedding, and
the words ride inside the same open. Its value is the rest of the week:
delivery stops risking a spike, and the replay drops from 2,287 MB to about
1,436 MB without anyone having to remember `embed_vocab.py`.

### The subprocess that did not help — tried 2026-08-13, reverted

Running the embed stage as its own process (the shape `loop.py` already uses
for `features.py`) looks like the fix: exit is the only reliable `free()`,
since dropping the model does not hand its ~1.2 GB back to the allocator. It
was built, proven output-identical, measured, and **reverted**. Both runs
`--skip-download`, 278 lots to embed, one job alone under a 3 GB cap:

| | in-process | subprocess |
| --- | --- | --- |
| **peak anonymous** | 2,958 MB | **2,957 MB** |
| delivery | 2,334 MB | 1,700 MB |
| dashboard | 2,107 MB | 1,541 MB |
| wall clock | 412 s | **491 s** |

**The cap is on the container, not the process.** A child runs in the same
cgroup, so its 2.4 GB is charged to the same budget — next to the parent's
572 MB of store frames, which it still holds. Moving an allocation into a
child hides it from `ps`, not from the limit that kills things.

What it did buy — a ~600 MB lighter tail — is bought by nothing, because
[`heavy_lock.py`](../heavy_lock.py) is what now guarantees no second job wants
that memory. Paying 79 s a week for headroom nothing can use is a worse trade
than not paying it. The sidecar was byte-identical both ways (`lots.npy` and
`lots_index.jsonl` compared by SHA-256), so this is a cost decision, not a
correctness one.

**If the peak has to come down**, the lever is not process boundaries. Either
embed fewer sequences at once (`batch_size=8` is worth ~85 MB, and the curve
is flat below 16), or take the embedding out of the cycle entirely and give it
its own slot at another hour — which the lock already makes safe, and which is
how the operator described the three jobs in the first place: *never done
simultaneously*.

### Why they cannot collide: one lock

The three jobs fit a 4 GB machine one at a time and **do not fit two at a
time** — 2,958 + 2,287 is 5.2 GB. Cron cannot collide with itself (one entry,
Monday 08:15, and `weekly.sh` chains its two steps with `&&`), and the
Windows task that used to run the cycle is confirmed `Disabled`. What was
unguarded is a person starting a replay at 08:20.

[`heavy_lock.py`](../heavy_lock.py) is that guard, and its docstring carries
the five properties that keep a lock from becoming a hung Monday. The
receipts, run in containers rather than argued:

| check | result |
| --- | --- |
| replay starts while the cycle holds the lock | refused in 7 s, exit 2, message names the reason |
| holder `SIGKILL`ed (what an OOM-killed cycle is) | next job acquired in **1 s** — no stale lock |
| cycle starts while a replay holds the lock | waited, logged `[lock] waiting …`, acquired when the replay left |

The asymmetry is deliberate: the cycle **waits** (up to an hour) because a
skipped Monday is a missed delivery, while the replay and the backfill
**fail immediately** because they are manual and cheap to repeat.
`embed_vocab.py --check` takes no lock at all — it loads no model and is
exactly what an operator wants to run *while* a cycle is going.

## 1. Decisions made here, so they stop being re-litigated

| date | decision | grounds |
| --- | --- | --- |
| 2026-08-10 | **One machine, both containers, SQLite stays.** | Measured: `ledger.append` holds the write lock 0.02 s per 100 rows / 0.96 s per 5,000; Python's sqlite3 driver already waits 5 s (`busy_timeout=5000` by default — the "fails immediately" worry was wrong); WAL reads never block (99,174-row count in 0.002 s while a writer held the lock). App write volume is single-digit rows per week. Nothing an engine change would buy is measurable at this scale. |
| 2026-08-10 | **The Postgres trigger, recorded instead of the decision:** the day the cycle and the app live on *different machines*, storage moves to Postgres. | SQLite is a file; two machines cannot share it (network filesystems corrupt its locking silently). The seam is two modules (`ledger.py`, `subscriptions.py`), which is what keeps the swap cheap — [`STORAGE.md`](STORAGE.md) §0. |
| 2026-08-10 | ~~**The laptop is the backup.**~~ **Superseded 2026-08-13 (operator): backups must be automatic and machine-to-machine** — provider daily image backup plus a nightly encrypted restic push to object storage at a different provider. [`OPERATIONS.md`](OPERATIONS.md) §3. The nightly `db.py --export-jsonl` survives as the restored format; the laptop copy is a bonus nothing depends on. | A backup that depends on a laptop being open is not reliable. |
| 2026-08-10 | **Deploy = merge to master.** A push-triggered GitHub Action SSHes to the server. **Amended 2026-08-13 (operator):** the action runs the deploy script of [`OPERATIONS.md`](OPERATIONS.md) §2 — images build under git-SHA tags, the switch happens only after the new container's `/healthz` passes, and rollback is redeploying the previous tag, never a git operation. | Rebuild is seconds (dependency layer cached, measured); the running cycle is untouched and picks the new code up next Monday. The worktree→master discipline stays the review gate for rolling forward. |
| 2026-08-10 | **Domain deferred.** Blocks printing letters/QR (it is baked into them), not hosting — the app runs on a bare IP or throwaway subdomain until then. | Operator: "i will decide later." |
| 2026-08-10 | Models never deploy. | The cycle retrains and promotes them **on the server** every Monday; only model *code* travels through git. Data never deploys either — the app reads the database per request ([`APP.md`](APP.md) §9). |

## 2. Blocking — must be fixed before the app faces the internet

**All six DONE 2026-08-11** (commit history + APP.md §10b carry the receipts;
e-mail transport is Resend, operator decision 2026-08-11). What remains of №4:
the operator's legal identity, supplied at deploy time via `TM_IMPRESSUM` —
the page shows a visible gap until it is set. Original list, for the record:

1. **Tokens leak into the access logs.** `wsgiref`'s request logging prints
   every path in full — and the path *is* the token: `GET /t/AIZqvIs…`
   straight into `docker logs`. [`APP.md`](APP.md) §3 allows the first 8
   characters only. Harmless on the laptop, a hole on a public box. Small fix
   (a logging override using `tokens.short`), plus: the stdlib server is
   single-threaded — swap to the threading variant or put the fix and the
   serving question behind the proxy decision together.
2. **The app accepts nothing.** Every POST answers 405 by design
   ([`APP.md`](APP.md) §10b): signup, feedback, stop and recall are
   display-only until the handlers of APP.md §4–6 exist. They need the three
   new subscription fields (`contact_state`, `email`, `consent_at`) — which
   land in `subscriptions.KNOWN` **in the same commit** that first writes
   them (CLAUDE.md rule). Hosted today, the app is a brochure.
3. **No e-mail can be sent.** The guarded mailer ([`APP.md`](APP.md) §7) does
   not exist. Needs the SMTP decision below, and brings the project's first
   server-side secret.
4. **The legal pages are placeholders.** A public German site needs a real
   Impressum (§5 TMG) and the full Art. 14 notice
   ([`LEGAL_BASIS_TARGET_LIST.md`](LEGAL_BASIS_TARGET_LIST.md)). Left
   deliberately empty rather than plausibly fake — but empty cannot go live.
5. **No TLS.** The proxy (Caddy or the host's) is unwritten; HSTS on
   ([`APP.md`](APP.md) §9). The app itself must never face the internet.
6. **No rate limit on token lookups.** [`APP.md`](APP.md) §3 calls it a lazy
   brake on enumeration; 192-bit randomness is the real defense. Small.

## 3. Before relying on it — first weeks on the server

7. **Deploy automation** (§1's GitHub Action). Until it exists, a deploy is
   three SSH commands by hand.
8. **Backups** (§1's plan). Two jobs to write: the nightly export line in
   [`docker/crontab`](../docker/crontab), the laptop's weekly pull task.
   Specified in [`OPERATIONS.md`](OPERATIONS.md) §3.
9. **Silent-failure alerting.** On the laptop a broken Monday gets noticed;
   on a server nobody is looking. `/healthz` already reports the cycle's age
   in days — a free uptime service pinging it, alerting when age > 8 days,
   is the whole build. Specified in [`OPERATIONS.md`](OPERATIONS.md) §1,
   which also makes `/healthz` answer 503 on staleness so a status-code
   pinger suffices.
10. **The big-append edge.** One `ledger.append` above ~25,000 rows holds the
    write lock past the app's 5 s patience (extrapolated from the
    measurements in §1). Normal weeks append hundreds; a backfill can cross
    it. Chunk the append — a few lines in `ledger.py`.

## 4. Open decisions — operator's, nothing here moves without them

| decision | blocks | note |
| --- | --- | --- |
| **SMTP provider** | №3, and therefore printing letters | now the long pole |
| **VPS provider & size** | everything in §3 | **4 GB works, one job at a time** (§0a): every heavy job now completes under a 3 GB cap — cycle 2,958 MB, replay 2,287 MB, quiet cycle 1,044 MB — where before the cycle needed 6.1 GB and was OOM-killed. The cycle is at 96% of that budget, so treat 4 GB as the floor, not the comfortable choice. Disk is the easy axis: 2.1 GB of state against 40 GB. Cores barely matter — 2 is fine |
| **Windows task cutover** | — | DONE 2026-08-11: the scheduler container runs the weekly cycle and the Windows task is disabled ([`RUNBOOK.md`](RUNBOOK.md) §1c) |
| ~~domain~~ | ~~letters only~~ | **DECIDED 2026-08-11: `murara.eu`** (registered at Infomaniak). `www.murara.eu` is the public site, `app.murara.eu` the token surface. Brand is **Murara**; TenderMining stays the repository and system name |

**Still to arrange for the domain**, none of it code: DNS pointing at the
host, `info@murara.eu` actually receiving mail (the landing page's only
call to action is that address — it must work before a single letter goes
out), and the sending domain verified at Resend, or every send is refused by
the API.

## 5. Order of work

№1 first (defect, small, receipts possible on the laptop). Then №2, which
unlocks №3's structure while the SMTP decision is pending. №4–6 are small and
independent. Everything in §2–3 is buildable and testable in Docker on the
laptop before any VPS exists — same as everything so far.
