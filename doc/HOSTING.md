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
| the cycle (`loop.py` weekly) | CPU/RAM for CatBoost + embeddings, ~3 min/week in the container (measured) | Mondays 08:15 |
| the public website | none — static files, zero forms, zero backend ([`LAUNCH.md`](LAUNCH.md) §4.1) | n/a, static host |

State that must live on the machine: `data/raw` 1.3 GB (irreplaceable —
TED only serves recent packages), the database 111 MB (irreplaceable — what we
promised customers), embeddings 513 MB + store 23 MB + models (all recomputed
by the cycle). **≈2 GB, growing ~90 MB/week** with the raw archive.

## 1. Decisions made here, so they stop being re-litigated

| date | decision | grounds |
| --- | --- | --- |
| 2026-08-10 | **One machine, both containers, SQLite stays.** | Measured: `ledger.append` holds the write lock 0.02 s per 100 rows / 0.96 s per 5,000; Python's sqlite3 driver already waits 5 s (`busy_timeout=5000` by default — the "fails immediately" worry was wrong); WAL reads never block (99,174-row count in 0.002 s while a writer held the lock). App write volume is single-digit rows per week. Nothing an engine change would buy is measurable at this scale. |
| 2026-08-10 | **The Postgres trigger, recorded instead of the decision:** the day the cycle and the app live on *different machines*, storage moves to Postgres. | SQLite is a file; two machines cannot share it (network filesystems corrupt its locking silently). The seam is two modules (`ledger.py`, `subscriptions.py`), which is what keeps the swap cheap — [`STORAGE.md`](STORAGE.md) §0. |
| 2026-08-10 | **The laptop is the backup.** Server exports the DB to text nightly (`db.py --export-jsonl`, exists); the laptop pulls the DB + the week's raw files weekly. | The laptop already holds a full copy today and is already on for the Monday task. Disk dies → rent a new server, push the copy up. No storage account, ~90 MB/week transferred. |
| 2026-08-10 | **Deploy = merge to master.** A push-triggered GitHub Action SSHes to the server: `git pull && docker compose build && docker compose up -d app`. | Rebuild is seconds (dependency layer cached, measured); the running cycle is untouched and picks the new code up next Monday. The worktree→master discipline becomes the deployment gate. |
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
9. **Silent-failure alerting.** On the laptop a broken Monday gets noticed;
   on a server nobody is looking. `/healthz` already reports the cycle's age
   in days — a free uptime service pinging it, alerting when age > 8 days,
   is the whole build.
10. **The big-append edge.** One `ledger.append` above ~25,000 rows holds the
    write lock past the app's 5 s patience (extrapolated from the
    measurements in §1). Normal weeks append hundreds; a backfill can cross
    it. Chunk the append — a few lines in `ledger.py`.

## 4. Open decisions — operator's, nothing here moves without them

| decision | blocks | note |
| --- | --- | --- |
| **SMTP provider** | №3, and therefore printing letters | now the long pole |
| **VPS provider & size** | everything in §3 | measured guidance: 2 CPU / 4 GB / 40 GB is comfortable; cycle ran in 2.9 min on 8 CPU / 8 GB |
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
