# REFACTOR — separating the two questions, and giving subscriptions a home

Status (2026-08-13):

| phase | what | state |
| --- | --- | --- |
| 0 | the two bugs | **done** 2026-08-06 |
| 1 | `subscriptions.py`, still file-backed | **done** 2026-08-07 |
| 3 | `GateConfig`, stamped on every delivery | **done** 2026-08-07 |
| 2 | subscriptions and ledgers move to SQLite | **done** 2026-08-08 |
| 5 | `asof.py`, the one rewind engine + the program renames | **done** 2026-08-12 |
| 4a | `selection.py`, the one selection | **done** 2026-08-13 |
| 4b | `render.py` | specified, **next due** |

Phases are build order in time, never a scope cut (vocabulary of
[`ONLINE_LEARNING.md`](ONLINE_LEARNING.md) and
[`SUBSCRIPTIONS.md`](SUBSCRIPTIONS.md)). They ran out of numeric order
twice, both times deliberately: 3 before 2 because the ledger could not say
which rules produced a pick, while the storage problems were ones the system
would have rather than had; 5 before 4 because 5 is behaviour-preserving
(receipt: byte-identity) while 4 changes the backtest's measured numbers (it
fixes defect 1) and had to land alone — which it did, as 4a.

Nothing here changes what the system decides — every phase is a
behaviour-preserving move, and the one place where behaviour *did* change
(phase 0, defect 1) is a promise the docs already made and the code failed
to keep.

## What this is, in one paragraph

TenderMining answers two questions per lot: **is it my business** (the
relevance gate, [`relevance.py`](../relevance.py) /
[`evidence.py`](../evidence.py)) and **is it uncontested** (the CatBoost model,
[`single_bidder.py`](../single_bidder.py)). As *estimators* those two are cleanly
separated already — `judge()` never sees the score, the model never sees the
profile. The tangles were one layer up, and there were two of them: the
**composer** (the code that slices a market, asks both questions, ranks,
caps, renders and records lived in one 250-line function, with a drifted
copy in the all-lots rewind — the selection half is closed, phase 4a; the
rendering half is phase 4b), and the **rewind machinery** (three programs
each materialised their own past — closed,
phase 5). This document names the seams, records the defects the tangles
produced, and gave subscription state a real home before the first paying
customer arrives (phase 2).

## The tangle, precisely

[`loop.deliver()`](../loop.py) did four jobs in one function body — steps 1
to 3 now live in [`selection.py`](../selection.py) (phase 4a) and step 4 is
what phase 4b is for:

1. resolve the subscription and filter this cycle's scored lots to its slice
2. run the relevance gate over the slice
3. rank and tier by the competition score, cap by `max_picks`
4. render two HTML documents and append delivery-ledger rows

The proof that this was the defect and not a style preference:
[`rewind_all.py`](../rewind_all.py) (backtest.py before the phase-5 renames)
contained the same algorithm written a second time — slice filter,
`rel.judge`, sort by score, `flag`, `[:MAX_PICKS]` — and the two copies
**already disagreed** (see defect 1). The backtest therefore did not measure
the selection logic that ships. [`rewind_report.py`](../rewind_report.py)
(replay.py) made a third, partial copy and monkeypatches `loop.now_utc` to
borrow the rest. All of it is gone as of phase 4a; step 4 below (the render)
is what is left.

This problem was already solved once, one layer down: `evidence._sweep` and
the runtime ladder share `relevance._evidence_verdict` precisely so that "the
sweep measures the shipped code, not a replica". The same discipline applies
here.

### The seam

| module | owns | called by |
|---|---|---|
| `subscriptions.py` ✅ | field validation, as-of resolution, market filter | loop, rewind_all, rewind_report, preview_report, explain_verdict, feedback |
| `relevance.py` ✅ config | the verdict only; config passed in (a `Verdict` object is still open) | composer, explain_verdict, evidence harnesses |
| `asof.py` ✅ | the materialised past: filtered store, calibration inside it, pre-cutoff refs and champion | rewind_all, rewind_win, rewind_report |
| `selection.py` ✅ | slice → gate → rank → cap → `SliceResult(market, ranked, picks, borderline, judged)`. No I/O, no HTML | loop, rewind_all |
| `render.py` | `SliceResult` → report HTML, annex HTML, delivery rows | loop, preview_report |

`deliver()` then reads: for each sub → `selection.for_sub(...)` →
`render.report(...)` → append ledger. The all-lots rewind calls
`selection.for_sub` and measures the shipped selection by construction, which
is the whole point.

**The module is `selection.py`, not `select.py`.** `select` is a
standard-library module, and a `select.py` at the repository root shadows it
for the whole process — `import subprocess` then dies inside `selectors.py`
with `module 'select' has no attribute 'select'`. Receipt, in the image that
actually runs the cycle: with the file named `select.py`,
`docker run tendermining:latest python -c "import subprocess"` fails, i.e.
the scheduler would not have started. The name in this document was wrong,
not the design.

Two smaller cuts in the same direction:

- **`judge()` returns a positional 6-tuple** whose 6th element duplicates its
  4th — `_judge_evidence` ends with `return passed, borderline, text, c_hard,
  why, c_hard` purely to fit the embedding ladder's shape. Callers unpack it
  blind and then index it positionally (`(judged.get(k) or (None, None,
  None))[2]`; `scores[0], scores[1]` in `_gate_stamp`). A `Verdict` dataclass
  removes the shape coupling and lets the two ladders become two functions
  selected by config instead of two branches sharing a return signature.
- **`relevance.Gate.__init__`** is a read-model *and* a profile factory: it
  loads the embedding and label sidecars, the trust list, and then reads
  `tenders.parquet` into eight parallel object arrays and stashes `_lots` so
  that `build_profile` can derive and cache trade lexicons. Profile
  construction currently writes to disk (`evidence_df.json`,
  `trade_dicts.json`). That derivation belongs outside the profile builder.

## Phase 0 — the two bugs (done, 2026-08-06)

Both are independent of every refactor above and were fixed first.

### Defect 1: a CPV prefix deeper than 3 digits matched nothing

`loop._matches` filtered on the stamped `cpv3` slicing key, which is three
characters, so `'453'.startswith('453123')` is `False` — for *every* lot.
[`SUBSCRIPTIONS.md`](SUBSCRIPTIONS.md) promises full-CPV prefix matching ("a
lot matches if its CPV starts with any listed prefix") and
[`rewind_all.py`](../rewind_all.py) implements it that way against `cpv_main`,
which is how the two copies drifted apart. `jebsen-blitzschutz` v1 shipped
`cpv_prefixes: ["453123"]` (Blitzschutz) and addressed an empty market,
silently: no error, no empty-slice warning, just a subscription that could
never have a pick.

Fixed by matching against `cpv_main`, the full code, which `predict_open` has
stamped on every ledger row since the phase-3 relevance commit (`ec32f16`).
`cpv3` survives as the fallback for rows written before that stamp — and it is
live code, not a formality: **all 4303** ledger rows for the current champion
predate the stamp, and `tryout.py` replays exactly those rows. Under the
fallback a prefix longer than the key it would be tested against cannot be
proven and does not match, per the keyless-row rule.

Receipts:

- No behaviour change for anything shipping. Both active subscriptions, over
  the champion's real 4303 ledger rows: `brueckenbau-demo` (`452`) 1978
  matched before, 1978 after; `jebsen-blitzschutz` (`45`) 4303 before, 4303
  after. `tryout.py --sub jebsen-blitzschutz` renders identically.
- What the fix recovers, once a cycle writes `cpv_main`-stamped rows: a
  `453123` subscription addresses **107** stored lots (`45312310`: 103;
  `4531`: 2323) where the old filter matched 0.

### Defect 2: per-lot side tables keyed by `id(row)`

`deliver()` kept two side tables — `judged` (the gate's verdict, read back for
the "warum Ihr Geschäft" column and for the delivery-row gate stamp) and
`verdicts` (the annex's traffic light) — keyed by the CPython object identity
of the row dict. That is correct today only because `latest` holds every row
object alive for the whole delivery pass. Any change that copies, re-reads or
regenerates a row between write and read attaches **another lot's verdict to a
customer's pick**, with no error: a wrong reason next to a real
recommendation, which is the single worst failure mode this product has.

Fixed with `_lot_key(row) -> (procedure_id, lot_id)` — the pair `latest` is
already keyed by, so uniqueness per pass is structural. Same values, no
behaviour change.

## Phase 1 — `subscriptions.py`, still file-backed (done, 2026-08-07)

[`subscriptions.py`](../subscriptions.py) now owns the three questions that were
folklore. The as-of resolution rule turned out to be implemented **six** times
(`loop.deliver`, `loop.learn_references`, `tryout`, `explain`, `replay`,
`feedback`) and the market filter twice; all of them now call one
implementation. `loop.load_subscriptions`, `loop._matches` and
`loop._cpv_matches` are deleted. Storage is unchanged — still lines in
`data/subscriptions.jsonl` — which is the point: phase 2 changes this file's
internals and nothing else.

`python subscriptions.py` validates the file and prints what is in force: the
cheapest possible check after editing a subscription.

**Validation arrived, deliberately asymmetric.** An *unknown* field raises
(it is a typo, and a silently ignored typo is discovered from a wrong report
weeks later). A *retired* field warns once per field per load and is ignored:
the file is append-only by design, so a v2 line carrying `avoid_n` is a true
historical record, and refusing to read it would mean the system can no longer
read its own history. `cpv_prefixes` entries are checked to be 2-8 digits —
the class of error that gave `jebsen-blitzschutz` v1 an empty market — and
`min_relevance`/`min_code_*` to be in [0, 1].

One deliberate behaviour change: a misspelled `tryout.py --set` now exits with
the list of known fields instead of rendering the unchanged report, which
looked exactly like "that setting made no difference".

Two latent bugs fell out of the consolidation. `backtest.replay` picked the
last *active* line per subscription, so a customer deactivated by a newer
version would have been resurrected by an older active one; `resolve()`
applies `active` to the version in force, as SUBSCRIPTIONS.md specifies. And
`in_market` normalises pandas NaN, which the store-row path reached via
`str(row.get('cpv_main') or '')` — NaN is truthy, so that produced the string
`'nan'` and worked only by accident.

Receipts — a pure refactor has to prove it changed nothing:

- **Market filter, old vs new, zero disagreements.** All 8 active
  subscriptions against loop's deleted `_matches` over 6,000 real ledger rows
  at both `min_days` settings, and against backtest's deleted inline filter
  over 4,000 store rows: identical verdicts everywhere.
- **Rendered output byte-identical.** `tryout.py` for `jebsen-blitzschutz`,
  `brueckenbau-demo`, `beck` and `n3bau` — report *and* annex HTML diff clean
  against renders captured from the pre-refactor code.
- All 13 versions in the live file validate; the 8 in force resolve
  unchanged; `explain.py`, `feedback.py --list` and the backtest's gated-sub
  discovery all run through the new loader.

It also removed a circular import: `feedback.py` no longer imports `loop`
just to borrow its subscription loader.

## Phase 2 pre-work — the storage boundary (done, 2026-08-08)

Landed before the SQLite swap so that concurrent work on the gate is not
disturbed by it, and so phase 2 changes exactly one file.

- **Callers name a home directory, never a file.** `load(data_dir, as_of)`,
  `one(data_dir, as_of, sub_id)`, `read_all(data_dir)`. `storage()` resolves
  the directory to whatever the format currently is, and *raises* if handed
  something that looks like a storage file — reaching past the interface fails
  loudly instead of half-working. `loop.Paths.subscriptions` (a file) became
  `Paths.subs_home` (a directory).
- **`write_sandbox(dir, [sub])`** replaces the two places that wrote
  `subscriptions.jsonl` by hand (`tryout.py`, `replay.py`). Rows are validated
  on the way in: a sandbox that cannot be read back is worse than useless,
  because its report still looks real.
- **A migrated file must not stay readable.** The migration will rename
  storage to `subscriptions.jsonl.migrated-<date>`; a home containing both the
  marker and a live file raises rather than guessing. Silent stale customers
  are the failure mode being designed out.
- **The contract is in `CLAUDE.md`**, which is now *tracked*. It was untracked,
  which meant no worktree contained it — so an agent following its own
  worktree rule could never read the rules. Committing it is what makes the
  contract reach the other agents at all.

The second `CLAUDE.md` rule is the sharper one and is live independently of
storage: **a new subscription field must be added to `KNOWN` in the same commit
that starts using it**, because validation rejects unknown fields and the
rejection is not scoped to the line carrying it — an unknown field stops
delivery for every customer.

Receipts: `tryout.py` renders for `jebsen-blitzschutz`, `beck` and `n3bau` —
report and annex HTML byte-identical, run back-to-back against the pre-change
code. The legacy-file guard, the file-path rejection and `write_sandbox`
validation each tested directly.

## Phase 2 — subscriptions move to SQLite (done, 2026-08-08)

Implemented essentially as specified below: `db.py` carries the
`customer` / `subscription_version` split, the ledger tables, and the
append-only triggers (a plain `sqlite3` repository, schema version 3);
`python db.py --migrate` is the importer, `--export-jsonl` the tested
rollback, and `data/tendermining.db` has been the record since
(`CLAUDE.md`). The JSONL files remain as frozen pre-migration snapshots
that `ledger.read` refuses to serve stale. The specification is kept as
written — it is the reasoning record:

Everything the current design earned is kept: versioned and never edited,
`effective_from`, as-of reconstruction, append-only audit. JSONL is the wrong
*container* for it, for reasons that all arrive with the first paying
customer:

- **Five reimplementations of "newest version ≤ D"**, because there is no
  query layer.
- **No schema** — defect 1 and the dead fields above.
- **Single-writer assumption.** Today one cron job. Soon: a signup path, an
  operator edit, and the customer-reply flow the annex already promises
  ("Antworten Sie mit der TED-Nummer — Ihr Profil lernt daraus"), all writing
  while a cycle runs.
- **PII sits in the same object as the market filters**, in a plaintext file
  that is *designed* never to be edited. Real customers mean name, contact,
  billing and consent — and a right-to-erasure request against an
  append-only-by-principle record. Name that conflict before signup, not
  after.
- **Fixtures and real customers share one file.** `brueckenbau-demo` is a test
  fixture; the next line will be a real firm.

Recommendation: `data/tendermining.db`, SQLite in WAL mode, plain `sqlite3`
behind a repository module — no ORM at this size.

```
customer             -- mutable, erasable: name, contact, billing, consent,
                     -- award_names. PII isolated, so an erasure request is
                     -- one row, not an archaeology project
subscription_version -- PK (sub_id, version); customer_id, effective_from,
                     -- active, cpv_prefixes, nuts_prefixes,
                     -- min_deadline_days, max_picks, gate params,
                     -- created_at, created_by.
                     -- Append-only enforced by a trigger rejecting
                     -- UPDATE/DELETE — the guarantee stops depending on
                     -- everybody's discipline
delivery             -- from deliveries.jsonl: joined to grades and to
                     -- subscriptions on every report and every invoice
learned_ref          -- from learned_refs.jsonl: per-sub, always queried
                     -- by as_of
```

Why this and not the alternatives:

- **as-of becomes one SQL statement** behind `Repository.as_of(date)`, and the
  five copies collapse.
- **ACID**: the `learn → deliver` sequence within one cycle stops being two
  unsynchronised file appends.
- **SQLite over Postgres**: one operator, one machine, a cron job. Zero ops,
  backup is a file copy, and the SQL stays portable if a web signup ever
  justifies a server.
- **Not per-customer YAML/TOML**: no as-of query, same validation gap.
- **Not an external SaaS**: billing there eventually, fine — but the market
  filters are the product's core state and stay local.
- `predictions.jsonl` and `grades.jsonl` **stay where they are** in this
  phase. They are bulk, write-once-per-cycle, read-whole, and their
  append-only audit story is genuine. The 50 MB problem below is a separate
  change with a separate answer.

Migration is small: a `--from-jsonl` importer that validates every existing
line and inserts it, run once, with the JSONL kept as the pre-migration
artifact. Test fixtures move into the repo (committed, tiny) behind
`Repository.from_rows([...])` — which also retires `tryout.py`'s `shutil`
sandbox dance, since an in-memory repository with one overridden subscription
is exactly what that code emulates today.

## Phase 3 — `GateConfig`, stamped on every delivery (done, 2026-08-07)

The gate's rules were mutable module state, and the ledger could not say
which rules picked a lot.

`GateConfig` is a frozen dataclass holding every tunable that changes a
verdict. The constants stay exactly where they were — they carry the decision
history that gives each number its meaning — and the dataclass takes them as
its defaults. `Gate(data_dir, config=...)` carries one; `build_profile(...)`
and `judge(...)` take an optional `config=` that defaults to the gate's, so
every existing call site is unchanged and a single loaded gate can serve two
configurations at once.

**The stamp.** `_gate_stamp` now writes `gate_config` (a 10-char hash of the
whole rule set) and `gate_mode` on every delivery row, and `deliver()` appends
each newly seen configuration to `<ledger>/gate_configs.jsonl` — so the hash
resolves from the data directory alone, not from git archaeology over
whichever commit was deployed that week. The registry is scoped to the
delivery ledger rather than the data dir, so `tryout.py` and `replay.py`
sandboxes cannot append a configuration to the record of what customers were
actually served under. `python relevance.py` prints the live configuration and
its fingerprint, marking any field an environment variable is overriding.

That last part matters more than it did when this phase was written. The gate
had one env switch (`GATE_MODE`) then; by the time the phase was built it had
three — `SIMILARITY_NOMINATES` and `CONVICTION_NOMINATES` arrived on
2026-08-07 with the phase-8i/8k decisions. Three environment variables that
silently change what a customer is shown, and nothing on the row to say which
way they were set. All three are config fields now, so flipping one changes
the fingerprint and the change is visible in the ledger forever.

**No more global mutation.** The three rewind programs (then `backtest.py`,
`playback.py` and `replay.py`)
assigned to `rel.TRUSTED_CODES` / `rel.SOFT_FLOOR` / `rel.SOFT_CONSENSUS` /
`rel.TRADE_READ_*` to install their as-of calibration; `evidence.py` assigned
`rel.GATE_MODE` in three functions. All of them build configs now. This was
not optional: once the constants are only read as dataclass defaults at import
time, assigning to them afterwards is a silent no-op, so the as-of calibration
in both time-isolation harnesses would have quietly stopped applying.

Receipts:

- **The gate benchmark is identical.** `evidence.py --judge-benchmark`, all
  122 cases, BOTH gate modes, old code vs config-threaded code: byte-identical
  output.
- **Rendered reports are identical.** `tryout.py` for `jebsen-blitzschutz` and
  `beck`, report and annex HTML.
- Delivery rows now carry `gate_config: 7d29fa0dce`, `gate_mode: evidence`,
  and the registry line expands that hash to all 20 fields.

*Method note, learned the hard way:* both comparisons must be run
back-to-back. The first attempt compared a baseline captured 40 minutes
earlier and showed a 20-case "regression" that was entirely the store and
lexicon caches moving underneath — re-running the OLD code reproduced the
"new" numbers exactly. On a live system, an equivalence test is only evidence
if both arms see the same data.

## Phase 4a — `selection.py`, the one selection (done, 2026-08-13)

The extraction described under **The seam**, deleting every copy of the
selection loop as it landed. This is the phase that makes the all-lots rewind
trustworthy; it comes after the others because it is worth doing on top of a
validated subscription model, an explicit gate config and a shared rewind
engine, not underneath them.

### The duplication this phase removed

Four copies of slice → gate → rank → cap:

1. `loop.deliver()` — the one that ships;
2. `rewind_all.replay()` — the one that *measures* the one that ships, and
   had drifted from it (defect 1);
3. `rewind_report.py`'s `--scan` — sorted pool, NUTS `startswith`,
   `rel.judge`, break at five;
4. `rewind_report.py`'s render path, which already borrowed `loop.deliver`
   wholesale by monkeypatching the clock, and so needed no change.

### What is behaviour-preserving, and what is not

**Delivery: preserved, and the receipt is byte equality.** All five
subscriptions with a tryout render — `beck`, `brueckenbau-demo`,
`gokser-fubodentechnik`, `n3bau`, `yg-baustoffe` — produce identical report
and annex HTML before and after, clock frozen to the day the before-copies
were taken. (The copies were rendered on Linux and the after-copies on
Windows, so the diff is `--strip-trailing-cr`; nothing else differs.)

**The rewind: deliberately not.** The old copy applied a fixed 14-day
deadline horizon and a fixed cap of five picks to every subscription. The
shipped selection reads both off the subscription line — and **six of the
eight live subscriptions promise no deadline horizon at all**
(`min_deadline_days` = 0; only `brueckenbau-demo` and `jebsen-blitzschutz`
promise 14). The backtest was therefore holding six customers to a promise
they had never been given, and reporting the result as their number. The cap
is a no-op: every live subscription is at the default five.

What this does **not** touch is the global forecast statistic — precision,
recall, base rate, lift, and the per-CPV3 table. Those are computed from
`flagged` and `scored`, which are per-lot and have no subscription in them.
Any movement there would be a bug in this phase, not a finding.

### The name

`selection.py`, not `select.py` as this document specified — see the boxed
note under **The seam**. The stdlib collision is not a style objection: the
scheduler's image cannot `import subprocess` with a `select.py` at the
repository root.

## Phase 4b — `render.py` (next due)

`SliceResult` → report HTML, annex HTML, delivery rows. `deliver()` is still
~200 lines of HTML assembly with the selection lifted out of it; the annex,
the receipts block and the borderline band are three renderers sharing one
function body. Behaviour-preserving, with the same byte-equality receipt
phase 4a used.

## Phase 5 — `asof.py`, the one rewind engine (done, 2026-08-12)

Specified and implemented the same day, one migration step per commit, each
with its receipt (listed at the end of this section as obtained).

### The duplication this phase removed

Three programs rebuild the past, one per zoom level: `backtest.py` (every
weekly cutoff → the statistic), `playback.py` (one past win → would we have
caught it?), `replay.py` (one customer at one past Monday → the report we
would have sent, and its grading) — the pre-rename names, kept in this
section because they are what the duplication looked like. The zoom levels
are the non-redundant part and they stayed. What was redundant is the rewind
machinery itself — each program materialised its own past world,
near-verbatim, in its own scratch directory:

1. filter both store parquets to `publication_date < D` (pyarrow filter,
   which preserves the per-column role metadata `load_with_roles` needs — a
   subtlety only `playback.py` still documents);
2. copy the embeddings sidecar whole;
3. run `calibrate.calibrate` inside the world, write `trusted_codes_asof.json`
   (the same dict comprehension, three times), build the `GateConfig`;
4. resolve a firm's pre-D wins to profile references;
5. train the champion strictly inside the world.

That is ~170 copy-pasted lines of the most leakage-sensitive code in the
repository, and the copies are already rotting independently: the
crash-safety fix of 2026-08-11 (write `.partial`, then rename — an
interrupted run used to leave a truncated parquet that poisoned every later
run) exists only in `backtest.py`; the role-metadata warning only in
`playback.py`. Three copies means a fix lands in one and waits in the others
until it hurts.

### What `asof.py` is

**A library, not a program.** It answers no question and has no CLI. Its one
concept is *a materialised past you can trust*: given the full store and a
cutoff D, it hands back the world as it stood before D, plus the artifacts
every rewind question needs — a gate config calibrated inside that world, a
firm's pre-D references, a champion trained pre-D.

```python
w = asof.World(data_dir, work_dir)   # sidecar copied once, reused after
w.rewind(D)                          # store filtered to < D; caches dropped
cfg = w.calibrated_config('F')       # or 'H' — calibrate + trust list, inside
gate = w.gate(cfg)
refs = w.refs_for_firm(name)         # pre-D resolvable wins, as profile refs
model = w.model()                    # champion trained on pre-D labels only
```

`rewind()` is what `rewind_all.py`'s weekly loop needs: refilter the store,
keep the sidecar, drop the gate/model caches — but **not** the calibration,
because how often to recalibrate (`RECAL_EVERY`) is the caller's cadence
decision, not the engine's. `WorldTooThin` propagates; what to do about it
stays policy per program (the statistic skips the cutoff, the other two stop
with advice).

The guarantees the module owns, and the disclosures that live with it:

- the filtered store preserves schema and role metadata, and is written
  crash-safe (`.partial` + rename) — for every caller, not one;
- the sidecar is copied whole, once, and therefore outruns the world it sits
  in — the disclosed residual that used to be buried in `backtest.py`'s
  docstring lives here now, next to the code it discloses;
- nothing inside the world can read a publication dated ≥ D; the module
  never touches outcomes at all — **grading stays at the call sites**, which
  join to the full store *after* the as-of work is done.

### What `asof.py` is not

Not a fourth program. Not a renderer — phase 5 keeps the rule of
`TRADE_PAGES.md` §6d: engines produce worlds, programs produce data,
renderers display. Not the selection algorithm — slicing, judging, ranking
and capping are **phase 4's seam** (`select.py`), which both `loop.py` and
the rewind programs will call; until phase 4 lands, that composition stays
duplicated at the call sites, visibly, rather than being absorbed here where
it would pre-empt the better cut.

### Recorded differences, preserved verbatim

The engine must not silently unify what the three programs deliberately (or
accidentally) do differently. Each is kept exactly as found, and each
unification, if ever wanted, is its own decision:

- **Gate recipe:** `rewind_all.py` and `rewind_win.py` calibrate
  configuration F, `rewind_report.py` configuration H. Why the split exists
  is not documented anywhere; ask the operator before ever unifying it.
- **Bar placement:** the all-lots rewind drops a customer's own three bars
  so the as-of calibration on the config decides (`as_of_profile`'s
  docstring says why); the win rewind puts F's bars on the synthetic
  subscription; the report rewind puts none on a firm's. Profile *assembly*
  therefore stays at the call sites — the engine only resolves references.
- **Directory layout:** the old `playback_asof/data/` extra nesting is gone;
  the engine settles one layout (`<work>/store/`, `<work>/embeddings/`,
  `<work>/trusted_codes_asof.json`). Behaviour-neutral.
- **Scratch homes:** one parent, one subdirectory per program —
  `data/asof/all/`, `data/asof/win/`, `data/asof/report/` — so two rewinds
  can run at once and a half-hour run is never lost to a collision. The
  housekeeping sweep (`test_housekeeping.py`) followed the move and retires
  the three pre-phase-5 homes by the same age rule.

### Migration — one program per step, receipts as obtained

Prerequisite (met first): the document work of `TRADE_PAGES.md` §6d landed,
so step 1's receipt — the replay's JSON document — was trustworthy before
the refactor started.

1. **Engine extracted from `backtest.py`** (the most-exercised copy), which
   became its first reader. Receipt: `--step 56` replay before and after,
   documents **byte-identical** (1,091,788 bytes, including `generated` —
   same day). The receipt run itself caught a real bug: without `--sub`,
   `subscriptions.load`'s retired-field warnings printed ahead of the
   stdout guard and corrupted the document; guard widened, test added for
   exactly that branch.
2. **`playback.py` ported.** Receipt: the default Jebsen run before and
   after, output identical except the elapsed-seconds line.
3. **`replay.py` ported** — config H proving the recipe is really a
   parameter. Receipt: `jebsen-blitzschutz` at `--cutoff 2026-03-25
   --check-date 2026-08-01`, console identical except two elapsed-time
   lines, both annex HTML files **byte-identical**.
4. **The three inline copies deleted, and the view programs renamed — all
   five.** Operator's call (2026-08-12): `backtest`, `playback`, `replay`,
   `explain` and `tryout` described nothing of what a program does — three
   were near-synonyms for the same rewind, and none said what question it
   answers. A name must say the direction and the thing produced:

   | old | new | what it does |
   | --- | --- | --- |
   | `backtest.py` | `rewind_all.py` | rewind every week, grade every alarm → the statistic |
   | `playback.py` | `rewind_win.py` | rewind one past win → would we have caught it? |
   | `replay.py` | `rewind_report.py` | rewind one customer's Monday → the report we would have sent, graded |
   | `explain.py` | `explain_verdict.py` | why this lot passes or fails this customer's gate, now |
   | `tryout.py` | `preview_report.py` | one customer's report from the current scores, with overrides, sandboxed |

   The two families read as what they are: `rewind_*` share the engine and
   the direction; `preview_report` ↔ `rewind_report` are the same question
   in opposite directions. All `git mv`, docs updated in the same commit.
   Receipt: full suite green (232); store-filtering `pq.write_table` exists
   only in `asof.py` (`features.py` builds the real store, which is not a
   rewind); `backtest_world`/`playback_asof`/`replay_asof` survive only in
   the housekeeping sweep that retires them, and the old program names only
   in history sections like this one.

The time-isolation properties have their own test file
(`tests/test_asof.py`): pre-cutoff-only store, role metadata surviving the
filter, an interrupted rewind leaving a repairable world (the 2026-08-11
truncated-parquet failure, now impossible to reintroduce silently), caches
dropped on rewind, calibration deliberately kept, the F/H recipe mappings.
None of the three copies had any of these tests — which is rather the point.

### The principle this phase and phase 4 both serve

**One writer, many readers**: `loop.py` is the single scheduled writer
(deliveries, ledgers, learning); every question-answering view, forward
(`explain_verdict.py`, `preview_report.py`) or rewinding (`rewind_*.py`), is
a read-only program over a shared engine. The 2×3 map of question ×
direction is `METHODS.md` §0. Explicitly rejected: one `rewind.py` with
`--all/--win/--report` modes — a mode-switch program is `loop.deliver()`'s
tangle mirrored backwards. Open, cheap and optional: `rewind_win.py` is
nearly a special case of `rewind_report.py`; folding 3 → 2 is a small diff
now that both are thin.

## Open, not scheduled

Re-audited 2026-08-12, item by item against the code. Two of the three were
stale; the ruling on each is recorded rather than the item silently deleted.

- **`predictions.jsonl` parsed twice per cycle — RESOLVED by phase 2**, and
  not the way this item predicted. It guessed "Parquet partitioned by cycle
  date, not SQLite"; what actually happened is the 2026-08-08 migration put
  predictions in the database, `predict_open`'s dedup set became one
  four-column `SELECT` (`ledger.prediction_keys`) and `deliver`'s receipt
  fallback a filtered `SELECT` (`ledger.prediction_titles`). The 50 MB file
  still exists as a frozen pre-migration snapshot that `ledger.read` refuses
  to serve stale. Nothing parses it. Kept here as a reminder that a
  speculated fix in an open-items list is a guess, not a plan.
- **Circular imports papered over with function-level imports — HALF
  RESOLVED.** `loop` ↔ `feedback` is no longer a cycle: `feedback` stopped
  importing `loop` (phases 1-2 gave it `subscriptions`/`ledger` to import
  instead, which was the point). Still cyclic today: `relevance` ↔
  `evidence` and `relevance` ↔ `feedback`, papered over in the same
  function-level way. Unchanged expectation, now with the phase named:
  phase 4 (`select.py`/`render.py`) is the layering that should dissolve
  them; if it lands and they persist, then they earn their own work.
- **Profile growth is still unbounded — OPEN, and gated on a receipt, not on
  a phase.** This item used to wait for "the subscription repository once
  phase 2 lands"; phase 2 landed and the cap rightly did not, because
  `feedback.py`'s note has the better rule: capping is a union-time decision
  (keep the newest N; the ledger stays a complete history), and it stays
  unbuilt **until a receipt shows growth actually moving leakage or cost**
  (RELEVANCE.md phase 9). The largest live profile today is 8 references, so
  the receipt cannot yet exist. What would trigger it: a customer whose
  reference count reaches the hundreds, or `build_profile` visibly slowing a
  cycle.
