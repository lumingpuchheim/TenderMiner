# STORAGE — what belongs in the database, what stays a file

Status: decision document for [`REFACTOR.md`](REFACTOR.md) phase 2, written
2026-08-08 because the split was not obvious. Nothing here is implemented yet.
Section 5 is the part that needs an operator decision; sections 2 and 3 are
recommendations with their reasoning, so they can be overruled item by item.

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

Sandboxes (`tryout.py`, `replay.py`) now build a small database through
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
  directly; `explain.py`, `feedback.py --list` and the `subscriptions.py` CLI
  all still work on the file path.

## 5. Open calls — these need an operator decision

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

**Recommendation:** migrate immediately after a cycle completes, and have the
JSONL writers refuse to write once a `.migrated-*` marker exists — so the
worst case is a loud failure, not silent data loss. If a cycle is scheduled
during the window, disable the cron first.

### 5.4 Does `simulation` belong at all?

15,822 rows of market-scale simulated picks, used by `simulation.py check` and
`outreach.py`. It is a record of decisions, so section 1 says database.

**Recommendation: move it**, but last, and note that it is the one table whose
rows nobody has ever queried by key — it is read whole. If it turns out to be
a pure analysis artifact, it could equally stay a file.

## 6. Build order

Four steps, each verified against the pre-change code back-to-back before
landing — the discipline that kept phase 1 and the phase-2 pre-work clean:

1. `db.py` — schema, connection, migration, `--export-jsonl`, and a round-trip
   integrity check (export the migrated database, diff against the frozen
   original). Pure addition: nothing reads it yet, so it cannot break a cycle.
2. `subscription_version` + `customer`, behind the `subscriptions.py` boundary
   landed on 2026-08-08. **No caller changes** — that is what the boundary
   bought.
3. The small per-customer ledgers: `delivery`, `learned_ref`, `gate_config`.
4. The bulk ledgers: `prediction`, `grade`, `simulation` — where the 51 MB
   double parse disappears.

## 7. Decision log

Record the calls here as they are made, so the reasoning outlives the
conversation.

| date | decision | by |
|---|---|---|
| 2026-08-07 | SQLite replaces the JSONL ledgers completely, not just subscriptions | operator |
| 2026-08-08 | callers name a subscription *home directory*, never a storage file; `write_sandbox` replaces hand-written sandbox files | implemented, see REFACTOR.md phase 2 pre-work |
| | 5.1 registry / CURRENT: | |
| | 5.2 loop_checkpoint: | |
| | 5.3 migration window: | |
| | 5.4 simulation: | |
