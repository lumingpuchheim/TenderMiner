# STORAGE — what belongs in the database, what stays a file

Status: the specification for [`REFACTOR.md`](REFACTOR.md) phase 2, revised
2026-08-08 after the operator stated the real goal (see 0). **If you read one
section, read 6 — it is the only section that says what to do.** 0 is the goal,
1-4 are the reasoning and the split, 4b-4e are receipts for work already done,
5 records decisions with defaults so nothing blocks, 7 is the log.

Supersedes: the earlier "build order" and "finish plan" sections, written before
the cloud requirement and contradicting each other. Section 6 is now the only
plan.

## 0. The goal, and what it demands of the design

The operator's requirement, stated 2026-08-08:

> A system that is **robust and easy to maintain**, which will eventually run
> **in the cloud** — on a platform not yet chosen, and deliberately not being
> chosen yet.

Three things follow, and they override anything written before them.

**The vendor decision comes last, not first.** Choosing now means building for a
platform that may not be chosen. What must be true instead is a short list of
vendor-neutral properties, after which hosting is a shopping decision rather
than a project. Those properties are section 6.

**A filesystem path is the wrong handle for storage.** Everything here currently
takes a data *directory*. In the cloud there is no meaningful directory — there
is a connection string, a bucket, and credentials. Wherever this document says
`data/...`, read it as "today's default location", not as part of the design.
The location must come from configuration, and must not live inside the code
checkout.

**The engine is a way-station; the seam is the asset.** SQLite is right for
today — one operator, one scheduled cycle, eight customers. It stops being right
the moment a second writer exists (a signup form writing while the cycle runs)
or a second container does. What makes that swap cheap is that only two modules
touch storage — [`subscriptions.py`](../subscriptions.py) and
[`ledger.py`](../ledger.py) — while 221 call sites across 19 files merely pass a
location through. Protect that seam; the engine behind it is replaceable.

### Not the cycle's host: Vercel

Recorded because it was asked. Vercel cannot run this cycle: function execution
is capped in minutes while the cycle spends minutes training CatBoost and
embedding with a sentence-transformer; there is no persistent filesystem, so the
database, parquet store, embeddings and model binaries would be fetched and
pushed on every run; and the deployment bundle is size-limited, which `torch`
alone exceeds. It is not a tuning problem but a category mismatch.

It is the right tool for the half of the system that does not exist yet — a site
where each customer reads their weekly report and track record from the
database, instead of receiving HTML files. Batch elsewhere, front end there.

Platforms that do fit the cycle, for when the decision is due: **Render** or
**Railway** (a scheduled container as a first-class primitive, managed Postgres,
one dashboard), or **Modal** (schedule and dependencies declared in Python).
Object storage — R2, B2 — for the parquet store, embeddings, models and archive.

## 1. The principle

One sentence decides most of it:

> **The database holds what WE decided and promised. Files hold what the world
> told us, and anything we can recompute.**

A prediction, a delivery, a grade, a subscription version — those are claims
this system made, on a date, to a named customer. They are queried per
customer, joined to each other, must never be silently edited, and are the
evidence behind every number in a customer's report. That is a database.

A downloaded notice, the parquet store built from it, an embedding matrix, a
lexicon cache — those are either the world's data or a pure function of it. If
they are lost, we re-download or recompute. Putting them in a database buys
nothing and costs the tools that read them natively.

Two consequences worth stating, because they are the whole reason this is not
just tidying:

- **Append-only stops depending on discipline.** Today "never edit a
  subscription version" is a rule people follow. As a table with a trigger
  rejecting `UPDATE`/`DELETE`, it is a rule the storage enforces.
- **PII becomes separable.** Eight real firms are in the system as of
  2026-08-07. Their names sit inside the same object as their CPV filters, in
  a file designed never to be edited. A `customer` table splits identity
  (mutable, deletable) from the market filter (append-only), so an erasure
  request is one row rather than a contradiction.

## 2. Into SQLite — `data/tendermining.db`

Row counts measured 2026-08-08.

| table | from | rows | access pattern that justifies it |
|---|---|---|---|
| `customer` | *new* (split out of subscriptions) | 8 | mutable identity + contact + billing; deletable on request |
| `subscription_version` | `subscriptions.jsonl` | 13 | "newest version with `effective_from <= D`" becomes one query instead of six reimplementations; append-only by trigger |
| `prediction` | `predictions.jsonl` — **51 MB** | 90,604 | parsed **twice per cycle** today (dedup set in `predict_open`, receipt fallback in `deliver`) and growing without bound. Becomes two indexed lookups |
| `grade` | `grades.jsonl` | 7 | joined to predictions and deliveries on every report |
| `delivery` | `deliveries.jsonl` | 55 | the customer's track record and the billing surface; always read per `sub_id` |
| `learned_ref` | `learned_refs.jsonl` | 5 | always read as "refs for this sub as of this date" — an indexed query |
| `gate_config` | `gate_configs.jsonl` | 1 | resolves the `gate_config` fingerprint stamped on delivery rows |
| `simulation` | `simulations.jsonl` — 5.8 MB | 15,822 | market-scale pick record, joined against grades by `simulation.py check` |

## 3. Staying files

| stays as | what | why not the database |
|---|---|---|
| `data/raw/xml/**` | the downloaded notice archive | the world's data, re-fetchable, enormous, never queried by key |
| `data/store/tenders.parquet` (21 MB), `awards.parquet` (1 MB) | the store, rebuilt from raw by `features.py` | **the field roles live in the parquet schema** — that is the mechanism `build_features` selects features by, and moving to SQL would mean reinventing it. pandas and CatBoost also read parquet natively |
| `data/embeddings/**` | vector matrices (`.npz`) + sidecars | numeric arrays. SQLite blobs are strictly worse than `np.load` |
| `data/evidence_df.json` (2 MB), `data/trade_dicts_<config>.json` | derived lexicon caches | pure functions of the store, rewritten wholesale, never read by key. The dictionary cache names itself after a hash of every input it is derived from (switches + `cpv_trade_roots.txt`), so a stale one cannot be found rather than being wrongly matched — see `evidence.dict_cache_path` |
| `data/admin_index.json` (1–3 MB) | every winner's trade and numbers, for the operator's page (doc/ADMIN.md §4) | a pure function of the store and the evidence rules, rewritten wholesale by the cycle and by every deploy (`python admin.py --build`), read once per process. **Never written by a request.** Carries the store mtimes and a rules-plus-vocabulary stamp, so the page can say when it is older than the store; delete it and the page says the trade search is not ready until the next build |
| `models/<id>/model.cbm`, `meta.json` | trained model binaries | binaries; CatBoost loads from a path |
| `data/reports/**` | rendered customer HTML and the markdown cycle report | the deliverable itself |
| `data/logs/**` | ingest state, discovery state, `drift_latest.json` | download-pipeline operational churn. `download.py`/`bulk.py` are standalone crawlers; entangling them with the product database risks the ingest path for no gain |
| `benchmark_relevance.jsonl` | the hand-labeled relevance benchmark | **deliberately a file.** It is operator judgment, reviewed in `git diff` and versioned with the code that it grades. It is not runtime state |
| `FIELDS.md`, `calibration_<tag>.md`, `trusted_codes_<tag>.json`, `cpv_2008_de.csv`, `cpv_trade_roots.txt` | generated receipts and reference data | committed artifacts; their audit trail is git |

## 4. What moving costs, and the mitigation

A database is binary. `cat`, `grep`, `tail -f` and `git diff` stop working on
the ledgers, and those are real tools that get used when something looks wrong
at 9pm.

Mitigation, and it is not optional:

- **`db.py --export-jsonl`** regenerates every ledger as the append-only text
  file it is today, on demand. The promise "an auditor sees exactly what we
  see" survives, and so does `grep`.
- **Migration keeps the originals.** Each source file is renamed
  `<name>.jsonl.migrated-<date>`, never deleted. Rollback is renaming them
  back.
- **A frozen file must not stay readable.** A leftover `subscriptions.jsonl`
  that still parses is worse than a missing one: a caller gets plausible,
  silently out-of-date customers and no error. The loader already refuses a
  home holding both a live file and a migrated marker (landed 2026-08-08).

## 4b. Measured: what it actually costs (step 1, 2026-08-08)

Migrating the real ledgers (90,604 predictions, 55 deliveries, 13 subscription
versions, 7 grades, 5 learned refs, 1 gate config):

| | size |
|---|---|
| source JSONL replaced | **51 MB** |
| database, first attempt | 107 MB |
| database, `raw` zlib-compressed | **82 MB** |
| of which the compressed `raw` column | 34 MB (53.2 MB plain, 1.57x) |
| of which typed columns + 6 indexes | ~48 MB |

So the database is **1.6x the text it replaces**. That is the price of the
`raw`/typed redundancy, and it is the right price *for this step*, whose whole
job is a provable migration — `--verify` exports and compares against the
originals line by line, which is only possible because `raw` is verbatim.

Compression is modest (1.57x) because rows are ~590 bytes each; zlib has
little context to work with per row. It still recovered 25 MB and is paid only
by `--export`/`--verify`, never by a query.

**The optimisation, deliberately deferred:** drop `raw` from `prediction`
only, give `why_lonely`/`why_crowded` real columns so typed coverage is
complete, and reconstruct export from columns instead of verbatim text. That
lands the database at ~48 MB — *below* the source text — keeping verbatim
fidelity for the customer-facing ledgers (all of which together are under
100 KB) and accepting reconstructed-not-verbatim export for the bulk scoring
log. It can be done later without a second migration, because the frozen
originals are kept. It is not done now because it would trade away the one
property step 1 exists to deliver.

What the move already bought, measured on the same database:

- champion-model row lookup: **2 ms** via index (was: parse 51 MB)
- single-lot receipt lookup: **0.1 ms** (was: parse 51 MB, in `deliver`)
- `UPDATE`/`DELETE` on any ledger table: refused by trigger
- `UPDATE`/`DELETE` on `customer`: allowed, as designed for erasure

## 4c. Step 2 landed: subscriptions read from the database (2026-08-08)

`subscriptions.py` now resolves a home directory to a database when one is
there and to `subscriptions.jsonl` when it is not. **No caller changed** —
that is what the phase-2 pre-work bought.

There is deliberately **no flag day**. The live `data/` directory has no
database yet, so nothing moved until an operator runs `python db.py
--migrate`; both paths are supported and the switch is a decision, not a
deployment.

The market filter comes back from `raw` verbatim, so a caller cannot tell a
database row from the ledger line it was migrated from. Identity — `name`,
`award_names` — is overlaid from `customer` instead, which is the point of the
split: rename a customer and every past version reports the new name, while
their historical market stays exactly as it was.

**The database wins when both exist**, because migration deliberately does not
delete the originals. That preference is only safe with a cross-check, so
`read_all` raises when the file holds a `(sub_id, version)` the database does
not — an operator editing the file after migrating would otherwise have the
edit silently ignored, which is the exact failure this module exists to
prevent. The message names the fix (`python db.py --migrate`).

Sandboxes (`preview_report.py`, `rewind_report.py`) now build a small database through
`write_sandbox`, so they exercise the shipped read path instead of a second
format only sandboxes understand.

Receipts:

- **File and database reads are identical dicts.** All 13 versions, at six
  as-of dates spanning the subscription history (1, 2, 2, 8, 8, 8 active):
  equal element by element, and `read_all` returns them in the same order.
- **Rendered reports byte-identical** for `jebsen-blitzschutz`, `beck` and
  `n3bau`, run back-to-back against the pre-change code — with the sandbox
  now being a database rather than a file.
- Drift guard, frozen-marker guard and the sandbox round trip each tested
  directly; `explain_verdict.py`, `feedback.py --list` and the `subscriptions.py` CLI
  all still work on the file path.

## 4d. The migration was run on live data (2026-08-08)

`python db.py --migrate` against the real `data/`, on operator instruction.

| | |
|---|---|
| migrated | 13 subscription versions, 90,604 predictions, 55 deliveries, 7 grades, 5 learned refs |
| derived | 8 customers, from the newest version of each subscription |
| `gate_configs` | no source file yet — no cycle had run since the stamp shipped |
| database size | 81 MB |
| duration | 18 seconds |

`--verify` passed: every table exported back out reproduces its source file
line for line. The six source files' MD5 sums are **unchanged** — migration
does not touch them. `subscriptions.py` then reported
`data	endermining.db [db]` and `preview_report.py` rendered `beck` and
`jebsen-blitzschutz` identically to the pre-migration run.

### The first cycle on the database

`python loop.py run --last 7d --skip-download`, same day. Promoted
`m2026-08-08-081530`, scored 3,873 open lots, all four drift monitors ok
(single-bid rate 0.103 in a 0.019–0.229 band; score PSI 0.009 against a 0.25
warning). Delivered 21 recommendations to six of eight customers;
`jebsen-blitzschutz` (1 lot matched) and `polat-real-estate` (2) had nothing
clear the bar, which is the product working rather than failing.

Phase 3's stamp appeared on real rows for the first time: **16 of the 21 new
delivery rows carry `gate_config=7d29fa0dce`, `gate_mode=evidence`**, and the
registry recorded that configuration with a timestamp. The other 5 are
`brueckenbau-demo`, the one ungated subscription — no gate judged those lots,
so there is correctly nothing to stamp.

### And immediately, the argument for step 3

One cycle later the database's copies of the ledgers are already behind:

| table | in database | in file | file ahead by |
|---|---|---|---|
| `prediction` | 90,604 | 94,477 | 3,873 |
| `delivery` | 55 | 76 | 21 |

Harmless, because nothing reads those tables — and exactly the point. Two
copies of the same ledger with only one of them true is a state to leave
quickly, not to live in.

## 4e. Step 3 landed: the small ledgers (2026-08-08)

[`ledger.py`](../ledger.py) is `subscriptions.py`'s shape one layer over: a
caller names a **home directory** and a ledger by name, and the module decides
whether that home's records are in the database or still in
`<home>/ledger/<name>.jsonl`.

    rows = ledger.read(paths.deliveries_home, 'deliveries')
    ledger.append(paths.deliveries_home, 'deliveries', new_rows)

Switched over: `delivery`, `learned_ref`, `gate_config`. `loop.Paths.deliveries`
(a file) became `Paths.deliveries_home` (a directory); `feedback.read_learned`
and `append_learned` go through it; `preview_report.py` and `rewind_report.py` build their
sandbox with `ledger.start()`, so a scratch world is the shipped storage rather
than a private format. It is generic over the ledger name, so step 4 is a
call-site change with no new storage logic.

**The staleness guard earned its keep immediately.** Pointed at the live `data/`
it refused to read: the delivery file held 76 rows against the database's 55,
because a cycle had run since the migration. Those 21 rows would have been
invisible to a database-backed read — a customer's retrospective missing its
most recent week. The guard compares row COUNTS rather than content: these are
append-only logs, so "the file has more lines than the table has rows" is
exactly the question, and it costs one `COUNT(*)` instead of diffing 90,000
rows per read.

The live database was then brought current — `db.py --migrate` took in the
3,873 new predictions, 21 deliveries and 1 gate config, and `--verify` still
reports byte-faithful.

Receipts: file-backed and database-backed reads return identical rows for all
three ledgers; appending the same row to both gives identical read-back;
appending it twice to the database writes 0 the second time, so a re-run cannot
duplicate a customer's record. `preview_report.py` renders `beck` and `n3bau`
byte-identically against the pre-change code, run back-to-back, with the
sandbox now containing nothing but `tendermining.db`.

## 5. Decisions — all have a default, none blocks section 6

Each carries a recommendation that is now the **default**: work proceeds on it
unless overruled. Nothing in section 6 waits on any of these.

### 5.1 `models/registry.jsonl` (21 rows) and `models/CURRENT`

`registry.jsonl` is a record of promotion decisions, which by the principle in
section 1 argues for the database. But it lives next to the model binaries,
`CURRENT` is a one-line champion pointer read once per cycle, and both are
written by `learn()` in the same breath as `model.cbm`.

**Recommendation: leave both as files, this phase.** Moving them entangles the
storage swap with the training path — the one part of the system that just had
a flag day (the CPV depth change, 2026-08-07) — and buys no query we need. The
registry is read whole, once, by the dashboard.

*Overrule this if* you want "which model produced this pick, and why was it
promoted" answerable in one SQL join. That is a real reporting win, just not
one we need yet.

### 5.2 `data/logs/loop_checkpoint.json`

Small mutable cycle state (`last_success_at`, `last_success_to`,
`last_shuffled_check`).

**Recommendation: leave it as a file.** Moving it into the database would let a
whole cycle be one transaction — genuinely attractive — but that is a
different project: it also requires the ledger writes to become one
transaction, which changes the failure semantics of `loop.py run`. Not this
phase.

*Overrule this if* you want atomic cycles now, in which case it should be one
piece of work with 5.1 and the ledger writes together.

### 5.3 When to run the migration

A cron cycle that fires mid-migration would append to a file about to be
frozen, and those rows would be lost.

**Partly answered by how step 2 was built (2026-08-08).** Because storage is
dual-read — database when present, file when not — there was no window to hit
for subscriptions: the migration ran on live data mid-week with no cron
coordination and no flag day, and deleting the database would have reverted
it.

That holds for every step where the file remains readable. It stops holding at
two points: **step 4**, when predictions and grades become load-bearing, and
**the freeze**, when the files stop being readable at all. For those two:
migrate right after a cycle completes, and disable the cron for the window.

### 5.4 `simulation` — DECIDED and DONE 2026-08-08

15,835 rows of market-scale simulated picks, read by `simulation.py check` and
`outreach.py`. The operator settled it: *"I want to see the numbers."* Simulated
picks joined against outcomes **are** numbers, and leaving the one ledger outside
the system so it could not be joined to the others was the wrong side of the
line.

Now the `simulation` table, unique on `(company, procedure_id, lot_id)` — which
is the dedup rule `simulation.py` was keeping in a Python set. `outreach.py`
counted the file's lines to get each company's product volume; through
`ledger.py` now, or it would have undercounted by every cycle since the table
landed.

Receipts: 15,835 rows migrated, `--verify` byte-faithful, `ledger.read` returns
rows identical to the frozen file, `outreach.simulated_picks` agrees across all
3,451 companies, and `simulation.py check` still reports "15,835 simulated picks
· 3,451 companies".

**All eight ledgers are now in the database.** `models/registry.jsonl`,
`models/CURRENT` and `loop_checkpoint.json` remain files by decision (5.1, 5.2).

### 5.5 Erasure is incomplete — found by the tests (2026-08-08)

**Open, and it contradicts a claim made repeatedly above.** Section 1 says
splitting `customer` out of `subscription_version` makes an erasure request "one
row rather than a contradiction". Writing the test for that found two reasons it
is not true yet.

First, `DELETE FROM customer` **failed outright**: `customer_id TEXT REFERENCES
customer(customer_id)` made it a foreign-key violation. Fixed in schema 3 by
making the reference soft — a plain pointer, no constraint. `ON DELETE SET NULL`
cannot help, because setting it would be an `UPDATE` on an append-only table.
Erasing a customer now leaves their market history with a `customer_id` that
resolves to nothing, which is the right outcome: the person goes, the business
record stays.

Second, and still open: **the name is also inside `raw`.** Every
`subscription_version` row keeps its verbatim original line, which contains
`"name": "..."`, and that table is append-only. So deleting the customer row
removes the identity the read path *overlays* but not the copy in storage.

Two ways out, neither done:

- **Strip identity from `raw` at write time** and re-inject it from `customer` on
  export. PII would then exist in exactly one place, making the claim true by
  construction. The cost is that export becomes a reconstruction rather than a
  verbatim replay — and after an erasure it would legitimately differ from the
  frozen original, which `--verify` must be taught to expect.
- **Make erasure an explicit, audited exception** to append-only that rewrites
  the affected `raw` blobs.

The first is cleaner. Until one is done, the honest statement is: the *structure*
for erasure exists and the customer row is deletable, but a determined reader can
still recover the name from storage. `tests/test_storage.py` asserts that
current reality rather than the intention, so the gap cannot be quietly
forgotten.

## 6. What to do next — the only plan

Five items, in order. Each is useful on its own, none is wasted whichever host
is eventually chosen, and **none needs a decision from the operator.** Items 1,
2 and 5 exist because of section 0; items 3 and 4 finish phase 2.

### 6.1 Move the data root out of the checkout (small)

One environment variable names it, defaulting outside the repository. Today
`--data-dir` defaults to `data/`, a folder inside the code checkout, so code and
state are one directory.

*Why it serves the goal:* it is what the operator asked for directly, and
nothing can be containerised until it is true — an image ships code, and state
must outlive any container. It also lets backups treat the two differently.

### 6.2 Fix the three SQLite-only things in `db.py` (small)

`ORDER BY rowid` for append order (Postgres has no `rowid`; needs an explicit
sequence column), `PRAGMA table_info` for column discovery, and the
`RAISE(ABORT)` append-only triggers.

*Why now:* the first two are nearly free today and expensive after 6.4, when
90,000 rows go through this code every cycle. This is what keeps Postgres
available without committing to it.

### 6.3 Write the tests — DONE 2026-08-08

`tests/test_storage.py`, 41 tests, stdlib `unittest` (pytest is not installed
here, and a suite that needs installing is a suite that does not get run):

    python -m unittest discover -t . -s tests

No real data: every test builds its own temporary directory, so nothing reads
`data/`, nothing needs the 81 MB database, nothing touches the network, and a
failure can never be "your store is in an odd state". They assert *behaviours* —
identical rows from either storage, an idempotent append, a loud refusal — so the
engine can be replaced underneath them, which is what makes section 0's Postgres
swap checkable rather than hopeful.

Covered: the validator's rejections and its deliberate tolerance of retired
fields; as-of resolution including deactivation-by-new-version; the market filter
including the deep-CPV-prefix bug and pandas NaN; file-versus-database reads
returning identical dicts; identity overlaid from `customer` while the filter
stays frozen; idempotent migration and append; dense `seq`; every guard (file
ahead of database, storage-file path passed in, frozen marker, schema
downgrade); the append-only triggers; export fidelity including heterogeneous
rows; the sandbox round trip; and configuration resolution order.

**Three real defects, found before the suite was even committed:**

1. `DELETE FROM customer` failed on a foreign key, making the erasure the
   customer table exists for impossible (see 5.5, fixed in schema 3).
2. The erasure gap in 5.5 itself — the name survives in `raw`.
3. `db.stale_tables` was referenced by the plan but did not exist; it had been
   written and then correctly reverted with the abandoned export work, and
   nothing noticed the hole.

Two of the ten initial failures were the *test's* fault, and worth recording:
`UPDATE`/`DELETE` against an **empty** table fires no row trigger and so raises
nothing, which looked exactly like the append-only guard being absent. Every
ledger table is seeded now, and a second test asserts the seeding so the first
cannot go vacuous.

### 6.4 Finish the migration: `prediction` and `grade` — DONE 2026-08-08

**The plan said three reads. There were thirteen, across five files** —
`loop.py` (eight), `render_dashboard.py`, `simulation.py` (two) and
`preview_report.py`. The sizing above was wrong and is corrected here rather than
quietly grown.

`ledger.py` gained four targeted queries so the cycle stops asking for 94,000
rows to answer a narrow question: `prediction_keys` (the dedup rule),
`predictions_by_lot(lots=…)` (grading needs only lots whose award just
published), `prediction_titles` (the receipt fallback) and
`prediction_scores_since` (the drift window is a WHERE clause). Each keeps a
file branch that is the *original* code, deliberately: while a ledger can still
live in a file, "the two paths agree" must stay checkable, and the file branch
is what the tests compare against.

`loop.Paths.predictions`/`.grades` (two files) became `Paths.ledger_home` (a
directory). `rewind_report.py` writes its sandbox world through `ledger.append`
instead of truncating two files.

**The dangerous part was the three peripheral readers.** `render_dashboard.py`,
`simulation.py` and `preview_report.py` each parsed `predictions.jsonl` directly. Once
the cycle writes to the database the file stops growing — so left alone they
would have shown a market weeks out of date, silently, with no error anywhere.
Converted in the same change.

One behaviour subtlety: the score-distribution drift monitor used a snapshot of
the ledger taken *before* `predict_open` appended. A query runs after, so this
cycle's own rows are now inside the trailing window; they are excluded
explicitly, keeping the comparison "this cycle against the month before it".

Receipts — a full offline cycle on a **clone** of the live data, old code and
new, same inputs:

| | old code | new code |
|---|---|---|
| predictions after the cycle | file 98,350 / db 94,477 | file 94,477 / **db 98,350** |
| the 3,873 new rows | — | **identical** (ignoring `ts` and model id) |
| cycle report | — | **identical** (model id normalised) |
| grade, learn, predict, all four drift monitors, all eight deliveries | — | **identical decisions** |

**The rollback is tested, not described.** Export the database over the text
ledgers (`predictions.jsonl` back to 98,350 rows), delete the database
entirely, and run the *previous* code: it reads all 98,350 rows and completes a
normal cycle. So the database being load-bearing costs one command to undo.

### 6.4b Housekeeping: the as-of scratch worlds are swept — DONE 2026-08-08

203.8 MB apiece, the second largest thing under `data/` after the notice
archive, and entirely reconstructible: `asof.py` rewrites a world per cutoff
(a filtered copy of the parquet store plus a full copy of the embeddings)
and rebuilds it from the real store on every rewind. Nothing reads one in
between. Since phase 5 they live under `data/asof/<program>/`; the sweep
covers those per subdirectory, plus the three pre-phase-5 homes
(`backtest_world`, `playback_asof`, `replay_asof`) until they stop existing
on operator machines.

Swept by the cycle on the same 30-day rule as the discovery cache. **Age is
the safety catch, not a policy** — a rewind runs for half an hour, so fresh
files are never touched.

Receipts: `tests/test_housekeeping.py`, seven tests covering both sweeps — a
fresh world left alone, an aged one removed, an absent one not an error,
`prune_caches` never raising, and for the discovery cache: fresh survives, both
the old and new locations are cleared (or the relocation leaves 1.13 GB behind),
and `--dry-run` deletes nothing. Kept in its own file because importing `loop`
pulls in pandas, numpy and CatBoost, and the storage suite is worth keeping free
of the ML stack.

### 6.5 A Dockerfile that runs one cycle — DONE 2026-08-10

`docker run` produces a week's reports with nothing from the operator's laptop.

*Why it is last and why it matters:* it converts hosting from a project into a
purchase. It is also the honest test of 6.1 and 6.2 — if state is truly
configured and nothing reaches into the checkout, this is short; if it is long,
they were not finished.

It was short, and it found exactly two things. Docker arrived on the machine on
2026-08-10 (engine 29.6.2, `linux/x86_64`, an 8-CPU / 8.2 GB VM), so this item
stopped being a claim and became [`Dockerfile`](../Dockerfile),
[`docker-compose.yml`](../docker-compose.yml),
[`requirements.txt`](../requirements.txt) and
[`.dockerignore`](../.dockerignore). How to run it: [`RUNBOOK.md`](RUNBOOK.md)
§1b.

**The receipts.**

- **Build**: `tendermining:latest`, 1.62 GB. ~17 minutes cold, nearly all of it
  pip resolving CatBoost, onnxruntime, scipy and pyarrow; seconds after a code
  edit, because dependencies are their own layer. No `torch` — fastembed runs
  the sentence-transformer through onnxruntime, which is the difference between
  1.6 GB and the ~5 GB that ruled Vercel out in section 0.
- **Tests**: `docker run --rm tendermining:latest python -m unittest discover -t
  . -s tests` → 83 tests, OK, 1.2 s. The same 83 the laptop runs.
- **One full cycle**, `--last 7d --skip-download`, against a 672 MB copy of the
  real state mounted at `/data`, **with `--network none`** so the run could not
  reach TED or HuggingFace even by accident: first line
  `[config] data root: /data [$TM_DATA_DIR]` — and *no* "inside the code
  checkout" warning, which is 6.1's test passing in one line. 28,973 tender
  rows, sidecar current at 24,023 lots, a candidate model trained
  (`m2026-08-10-124827`, val PR-AUC 0.532 vs champion 0.669 → champion kept,
  correctly), 4,686 open rows scored, all four drift checks green, the operator
  report, all 8 subscriptions rendered, the simulation and the dashboard.
  `[done]` in 2.9 minutes.
- **The same cycle again with `--read-only --tmpfs /tmp`**, i.e. a container
  whose entire filesystem is immutable except the two mounts and a scratch
  `/tmp`. Identical output, `[done]`. That is the strongest form of the claim
  this item exists to make: *nothing* the cycle writes lands anywhere but the
  configured state.
- **The embedding model is copied from the laptop, not downloaded.** 309 MB
  seeded into a named volume from `%TEMP%\fastembed_cache`; loads in 2.1 s with
  the network off, and with the network *on* the volume is still 309 MB and
  nothing is re-fetched. fastembed logs `Local file sizes do not match the
  metadata` against a hand-copied cache and then uses it — there is no
  HuggingFace metadata to compare against. Cosmetic.

**What the honest test found.** 6.1 held for the data root: all 19 `--data-dir`
defaults across 18 programs already come from `config.data_root()`, and not one
of them had to move. Two things did:

1. **The model registry was still named relative to the working directory.**
   `--models-dir` defaulted to the string `'models'` in [`loop.py`](../loop.py),
   [`simulation.py`](../simulation.py) and [`preview_report.py`](../preview_report.py), and
   [`render_dashboard.py`](../render_dashboard.py) fell back to
   `REPO / 'models'`. In a container that is `/app/models` — inside the image.
   The cycle would have trained a model, promoted it, written `registry.jsonl`
   into the code directory, and lost all of it when the container exited; the
   next container would have started from an empty registry and seen no
   champion to beat. Now `config.models_root()` with `TM_MODELS_DIR`, the same
   three steps as `data_root` and the same CWD-relative default, so **nothing
   moves on the laptop**. The image sets it to `/data/models`, which is why one
   mounted volume carries the whole state. Two tests, including one asserting
   the two roots are independent — 5.1 is still undecided, and a registry that
   relocated itself the day someone set `TM_DATA_DIR` would take the promoted
   model with it.
2. **CatBoost wrote into the checkout on every fit.** `catboost_info/`,
   per-iteration tsv logs nobody reads — which is why `.gitignore` has a line
   for it. `allow_writing_files=False` in
   [`single_bidder.py`](../single_bidder.py) `make_model`; it affects logging
   only, never the fitted model, and it is what lets the read-only receipt above
   exist at all.

**The schedule now exists in the container too**, added 2026-08-10 on the
operator's request: `cron` in the image, `docker/crontab` firing
`docker/weekly.sh` at Monday 08:15, behind a compose profile so it cannot start
by accident. `weekly.sh` reproduces the Windows task's action line including the
`&&` — the simulation scorecard is appended only if the cycle succeeded, because
a dated heading with no run behind it is worse than a gap in a log that is read
weeks later.

*Receipt:* the committed crontab was edited in place inside a container to fire
two minutes out and the real daemon ran it — same file, same `tm` user field,
same redirect. `[cron] weekly cycle starting` streamed to `docker logs`, the
cycle finished, and `simcheck.log` gained `=== Mon 2026-08-10 ===` followed by
the scorecard, which only happens when the cycle exits 0.

**Cron cost three attempts, and every failure was silent.** Worth recording,
because each one produces a container that looks perfectly healthy:

1. **No output anywhere.** cron hands a job's stdout to the local mail
   transport; a container has none. The first run fired, failed, and reported
   nothing. Fixed by redirecting the job to a file and having the scheduler
   service `tail -F` it to stdout.
2. **`> /proc/1/fd/1` made it worse.** The usual container trick needs the job
   to run as the same user as PID 1 — this job runs as `tm` while the daemon is
   root, so the shell could not open the target and the job died *before*
   `weekly.sh` started. A silence one step earlier than the silence it replaced.
3. **The job ran in UTC.** `CRON_TZ` decides when cron fires; it does not reach
   the command, which gets a bare environment. The run that finally worked
   stamped itself `17:10:02 UTC` while the container clock read `20:10 EEST`.
   Harmless for `loop.py` (it dates from `now_utc()`), not harmless for
   `bulk.py` and `download.py`, which pick the download window with
   `date.today()` — between midnight and 03:00 local the container would have
   asked TED for the wrong day. `TZ` is now set in the crontab's env block
   alongside `CRON_TZ`.

Also added: `TM_WEEKLY_ARGS`, so `weekly.sh` can be re-run with
`--skip-download`. Found by running the weekly command against a state directory
with no `data/raw` — `features.py` rebuilds the store from the *entire* archive,
so a partial one silently replaced a 22 MB store with an 810 KB one and then
died in `single_bidder` with `KeyError: 'n_tenders'`. Exactly the failure §6.5's
"Deliberately not in this plan" predicts for deleting the archive, reached from
the other direction.

Reading the real task rather than this document's summary of it turned up
something worth writing down: **the laptop is on UTC+3 (`GTB Standard Time`),
not German time.** The image had been given `TZ=Europe/Berlin` on the assumption
that German notices meant a German clock. `loop.py` dates its own reports from
`now_utc()` and would not have noticed, but `bulk.py` and `download.py` choose
the download window with `date.today()`, and `rewind_all.py` and `calibrate.py`
name their receipts the same way — an hour off, and once a day a whole date off.
Now `Europe/Bucharest`, overridable with `TM_TZ`.

**What this still does not do — and the recommendation.** The live Monday task is
untouched: it still runs the laptop's Python, and the container has only ever
been pointed at a *copy* of the state. Cutting over is an operator decision, and
the runbook (§1c) argues for keeping the **Windows trigger** and giving it a
`docker run` action rather than switching cron on, for as long as the host is a
laptop. The existing task has `StartWhenAvailable`, so a Monday spent asleep runs
on waking; it refuses to start on battery and stops if unplugged; it has a 6-hour
execution limit; and it survives a reboot, which a container does not while
Docker Desktop's `AutoStart` is off. Cron in a container has none of that and is
the right shape only once the host is always on. Whichever is chosen, the other
must be disabled in the same sitting.

### Deliberately not in this plan

**Wiring the ledger export into the cycle.** Specified, started, stopped: it
wrote to `data/export/` inside the checkout, which 6.1 is about to move, and
exporting on top of the live files would today overwrite the authoritative
`predictions.jsonl` with the database's stale copy — data loss dressed as a
backup. It returns after 6.1 and 6.4, writing to the configured location.
`python db.py --export-jsonl DIR` already does it on demand.

**Deleting the raw notice archive.** 1.25 GB over 43,671 files, and
`features.py` rebuilds the parquet store from the **entire** archive every cycle
([`loop.py`](../loop.py), `download()`). Delete it and the next cycle rebuilds
the store from one week: 28,148 tender rows become a few hundred, the 90,000
predictions no longer join to any lot, and every `profile_refs` stops resolving.
The store is also a lossy projection — any field not yet extracted exists only
in the XML. Keep it.

*If space is wanted:* `data/logs` is 1.13 GB in 1,151 files, nearly the size of
the archive and mostly append-only logging nobody reads. That is the place to
look.

## 7. Decision log

Record the calls here as they are made, so the reasoning outlives the
conversation.

| date | decision | by |
|---|---|---|
| 2026-08-07 | SQLite replaces the JSONL ledgers completely, not just subscriptions | operator |
| 2026-08-08 | callers name a subscription *home directory*, never a storage file; `write_sandbox` replaces hand-written sandbox files | implemented, see REFACTOR.md phase 2 pre-work |
| 2026-08-08 | step 1: `db.py` — schema, migration, byte-faithful export, `--verify` | implemented |
| 2026-08-08 | step 2: subscriptions read from the database, dual-read, no flag day | implemented |
| 2026-08-08 | **migration run on live `data/`**, verified, first cycle green | operator instruction |
| 2026-08-08 | step 3: `ledger.py` — deliveries, learned refs, gate configs read/append through storage | implemented |
| 2026-08-08 | **goal stated**: robust, easy to maintain, eventually cloud-hosted, vendor deliberately undecided | operator |
| 2026-08-08 | Vercel ruled out for the cycle (execution cap, no persistent filesystem, bundle size); kept in mind for a customer front end | operator question, answered |
| 2026-08-08 | raw notice archive is NOT deleted — the store is rebuilt from the whole archive every cycle | operator question, answered |
| 2026-08-08 | document restructured: section 6 is the only plan; the old build order and finish plan removed as contradictory | revision |
| 2026-08-10 | 6.5: image built and a full cycle run in it, read-only rootfs, `--network none`, against a copy of the state | implemented, receipts in 6.5 |
| 2026-08-10 | the model registry gets its own variable (`TM_MODELS_DIR`), not a subdirectory of the data root — 5.1 is still open | implemented |
| 2026-08-10 | the weekly schedule stays on the laptop's Python until a cycle has run green in the container against the real `data/` | deferred, operator's call |
| | 5.1 registry / CURRENT: | |
| | 5.2 loop_checkpoint: | |
| | 5.3 migration window: | |
| | 5.4 simulation: | |
