# REFACTOR — separating the two questions, and giving subscriptions a home

Status: phase 0 (the two bugs) **implemented** 2026-08-06; phase 1
(`subscriptions.py`) **implemented** 2026-08-07. Phases 2-5 are specified
here and not started. Uses the vocabulary of
[`ONLINE_LEARNING.md`](ONLINE_LEARNING.md) and
[`SUBSCRIPTIONS.md`](SUBSCRIPTIONS.md) (**component** = a box that always
runs; **phase** = build order in time, never a scope cut). Nothing here
changes what the system decides — every phase below is a behaviour-preserving
move, and the one place where behaviour *does* change (phase 0, defect 1) is a
promise the docs already made and the code failed to keep.

## What this is, in one paragraph

TenderMining answers two questions per lot: **is it my business** (the
relevance gate, [`relevance.py`](relevance.py) /
[`evidence.py`](evidence.py)) and **is it uncontested** (the CatBoost model,
[`single_bidder.py`](single_bidder.py)). As *estimators* those two are cleanly
separated already — `judge()` never sees the score, the model never sees the
profile. What is tangled is the **composer**: the code that slices a market,
asks both questions, ranks, caps, renders and records lives in one 250-line
function, and a second, drifted copy of it lives in the backtest. This
document names the seam, records the defects that the tangle produced, and
recommends a real home for subscription state before the first paying
customer arrives.

## The tangle, precisely

[`loop.deliver()`](loop.py) does four jobs in one function body:

1. resolve the subscription and filter this cycle's scored lots to its slice
2. run the relevance gate over the slice
3. rank and tier by the competition score, cap by `max_picks`
4. render two HTML documents and append delivery-ledger rows

The proof that this is the defect and not a style preference:
[`backtest.py`](backtest.py) contains the same algorithm written a second time
— slice filter, `rel.judge`, sort by score, `flag`, `[:MAX_PICKS]` — and the
two copies **already disagree** (see defect 1). The backtest therefore does
not measure the selection logic that ships. [`replay.py`](replay.py) makes a
third, partial copy and monkeypatches `loop.now_utc` to borrow the rest.

This problem was already solved once, one layer down: `evidence._sweep` and
the runtime ladder share `relevance._evidence_verdict` precisely so that "the
sweep measures the shipped code, not a replica". The same discipline applies
here.

### The seam

| module | owns | called by |
|---|---|---|
| `subscriptions.py` ✅ | field validation, as-of resolution, market filter | loop, backtest, replay, tryout, explain, feedback |
| `relevance.py` | the verdict only; returns a `Verdict` object, config passed in | composer, explain, evidence harnesses |
| `select.py` | slice → gate → rank → cap → `SliceResult(picks, borderline, market)`. No I/O, no HTML, ~40 lines | loop, backtest, replay |
| `render.py` | `SliceResult` → report HTML, annex HTML, delivery rows | loop, tryout |

`deliver()` then reads: for each sub → `select.for_sub(...)` →
`render.report(...)` → append ledger. The backtest calls `select.for_sub` and
measures the shipped selection by construction, which is the whole point.

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
[`backtest.py`](backtest.py) implements it that way against `cpv_main`, which
is how the two copies drifted apart. `jebsen-blitzschutz` v1 shipped
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

[`subscriptions.py`](subscriptions.py) now owns the three questions that were
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

## Phase 2 — subscriptions move to SQLite

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

## Phase 3 — `GateConfig`, and stamping it on every delivery

The gate's calibration is mutable module state. `backtest.py` assigns
`rel.TRUSTED_CODES`, `rel.SOFT_FLOOR` and `rel.SOFT_CONSENSUS`;
`evidence.judge_run` assigns `rel.GATE_MODE` inside a loop. Two consequences:

1. Two configurations cannot be judged in one process. The nomination-bar
   sweep already works around this by hand.
2. **The delivery ledger does not record which configuration judged a pick.**
   `_gate_stamp` writes `profile_version` and `embed_model_tag` but not
   `GATE_MODE`, `NOMINATION_BAR`, `EVIDENCE_NOMINATION_MIN` or
   `BORDERLINE_ADMIT_P`. The flip from `embedding` to `evidence` went live on
   2026-08-06; delivery rows from before and after that flip are
   indistinguishable in the ledger. For a product whose pitch *is* the
   receipt, that is the gap to close first.

A frozen `GateConfig` dataclass passed into `Gate`/`judge` gives both the
parallel-configuration ability and a config hash to stamp, for free.

## Phase 4 — `select.py`, then `render.py`

The extraction described under **The seam**, in that order, deleting the
backtest's and replay's copies of the selection loop as each lands. This is
the phase that makes the backtest trustworthy; it is last because it is worth
doing on top of a validated subscription model and an explicit gate config,
not underneath them.

## Open, not scheduled

- **`predictions.jsonl` is 50 MB and fully parsed twice per cycle** — once for
  the dedup `seen` set in `predict_open`, once for the receipt fallback
  `pred_info` in `deliver`. It grows linearly forever. The answer is probably
  Parquet partitioned by cycle date, not SQLite, and it is independent of
  everything above.
- **Circular imports papered over with function-level imports** (`loop` ↔
  `feedback`, `relevance` ↔ `evidence`, `relevance` → `feedback`). Not a
  problem in itself; it is the layering pointing at where phases 1 and 4
  belong. Expect these to resolve themselves rather than needing their own
  work.
- **Profile growth is still unbounded** (noted in `feedback.py`): a firm with
  3 wins and one with 300 derive very different lexicons, and derivation cost
  is linear in references. A union-time cap belongs in the subscription
  repository once phase 2 lands.
