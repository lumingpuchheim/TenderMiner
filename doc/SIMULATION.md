# SIMULATION — every winner is a simulated customer

Status: implemented in [`simulation.py`](../simulation.py) — a self-contained
sidecar module (same pattern as `embed.py`). The loop calls
`simulation.simulate(...)` once per cycle; standalone use:

    python simulation.py check   # join simulations vs grades, print hit rates
    python simulation.py run     # one pass from the champion's ledger rows

Companion to [`SUBSCRIPTIONS.md`](SUBSCRIPTIONS.md) (the real-customer
layer); uses its vocabulary.

## Purpose

The customer layer proves the product one subscriber at a time; this layer
answers the business question at market scale: **for what share of real
construction companies would our weekly picks have been right?** Every
winner company in the awards store is treated as a simulated customer.
Their would-be picks are written down — one JSONL row per (company, pick),
**no rendering, no HTML** — and checked against outcomes as awards publish.
Two products of the same rows:

1. **The decision number**: "for X % of repeat winners, ≥3 of 5 picks ended
   with 0–1 bids", sliceable by trade and region — where the product works.
2. **Outreach assets**: every simulated customer is a real, named firm with
   a verifiable "had you subscribed on date D…" record (TED links included).

## Method

- **Who**: every COMPANY in the awards store, as `firms.py` resolves it —
  the registration number on the award notice (VAT or Handelsregister), with
  the name and postcode voting alongside it and only the number allowed to
  veto. Spelling variants of one firm are one company: `SVA GmbH` and `SVA
  System Vertrieb Alexander GmbH` share `DE185176948` and count once, while
  `Bechtle GmbH` and `Bechtle AG` do not and count twice. Over the store as
  of 2026-08-23 that is 22,029 winner spellings resolving to 17,080
  companies (CPV 45/48/72).

  This was the exact name string until 2026-08-23, and the note here used to
  say "acceptable for aggregate numbers; clean before using any single row
  for outreach". It was not acceptable: one firm counted as two understated
  every prospect's win history, and the operator's list held 881 companies
  that do not exist.
- **Their market**: derived from what they won — the set of `cpv3` trade
  codes and the set of NUTS-1 regions of their won lots. A company with no
  region information bids nationwide.
- **Their picks, product-faithful**: same rules as a real subscription —
  flagged lots only (the quality floor), ≥14 days to deadline
  (`--sim-min-deadline-days`), top `--sim-max-picks` (default 5) by score
  within their market.
- **Dedup**: one row per (company, procedure, lot) ever — a lot that stays
  open for weeks is simulated once; later cycles fill slots with next-best
  lots, which grows coverage instead of repeating rows.

## The ledger

`data/ledger/simulations.jsonl`, append-only, frozen at write time:

```jsonl
{"ts": "...", "company": "Weber Tiefbau GmbH", "procedure_id": "…",
 "lot_id": "LOT-0001", "notice_id": "…", "model": "m2026-08-04-…",
 "score": 0.83, "cpv3": "452", "place_nuts3": "DE212",
 "publication_number": "00517940-2026", "deadline_date": "2026-09-03"}
```

Volume: ≤ picks × companies per cycle (≈ 26k rows ≈ 8 MB in the first
week, less after dedup). No delivery-ledger rows, no customer artifacts.

## The check

`python simulation.py check` joins simulations ⋈ grades (both append-only) and
prints: simulated/graded pick counts, overall hit rate ("ended with 0–1
bids") vs the graded market's base rate, a per-trade table, and the
company-level view — among companies with ≥ `--min-company-picks` graded
picks: how many, median hit rate, share with a majority of picks right.
Grading itself is untouched: grades.jsonl grows lot-by-lot as awards
publish, and the join does the rest. The check is read-only and can be run
any time; the numbers simply firm up as outcomes arrive (~90-day median
award lag).

## The gate rides along (2026-08-10)

The simulation picked by coarse market (cpv3 x NUTS-1) plus the competition
flag — the relevance gate never saw a simulated pick. That left the gate's
only accumulating live evidence at 8 subscriptions while the machinery it
runs on (profiles, cores, phases 9c–9i) was measured on hand labels alone.
Now every simulated pick also receives a **gate verdict**, recorded in its
own append-only ledger `simulations_gate` (same natural key as
`simulation`: company, procedure_id, lot_id — one verdict forever, the
gate that was live when the verdict was written):

```jsonl
{"ts": "…", "company": "Weber Tiefbau GmbH", "procedure_id": "…",
 "lot_id": "LOT-0001", "verdict": "admit", "gate_pass": 1,
 "text": 0.71, "code": 1.0, "why": "evidence: estrich, zement"}
```

`verdict` is one of `admit` / `borderline` / `reject` (the real
`relevance.judge()`, profile built from the company's wins exactly as
onboarding would), or `no_profile` (no resolvable win in the sidecar —
the new-customer bootstrap case, recorded rather than skipped because its
size is itself a number we quote) or `not_in_sidecar` (the lot cannot be
judged). The PICK record is untouched — what the simulation would have
sent stays comparable across time; the verdict rides beside it.

The backlog is judged too, deliberately: picks already graded by
published awards get verdicts on the first pass, so the join below has
signal from day one instead of after the ~90-day award lag.

The check gains the split that motivates all of it: hit rates and
own-win rates per verdict. If gate-admitted picks end uncontested (and
won by the simulated customer) more often than gate-rejected ones, the
gate is buying precision at market scale, measured on outcomes — not on
labels. A failure in the verdict pass must never break the cycle: the
simulation is instrumentation, and the loop wraps it accordingly.

**The join is also stamped, not only rendered.** `verdict_outcomes()`
recomputes from scratch on every read, so the dashboard panel shows today's
answer and yesterday's is gone the moment a grade lands. Each cycle therefore
appends the answer to `gate_outcomes` — one row per (cycle `ts`, verdict),
a single shared `ts` per snapshot so a cycle is one addressable point:
`ts, verdict, graded, lonely_rate, own_wins, own_rate, picks_total,
verdicts_total`. The last two are the cycle-wide denominators, repeated on
every row of the snapshot, so `graded` stays readable a year later. The
natural key `(ts, verdict)` makes a re-run a no-op — a cycle can never
double a point in the series, or rewrite what was known that day. Rows are
written **even while every count is zero**: simulated picks start being
graded around October 2026, and a series that begins once the numbers are
interesting cannot show that they were zero before. The same shared
aggregation feeds both readers, so the panel and the table can never drift
apart.

The full history is also written to `data/reports/gate_outcomes.csv` each
cycle — one stable path, rebuilt from the ledger rather than appended to,
exactly the table's eight columns — for a consumer that has no sqlite. The
snapshot runs inside the same guard as the verdict pass: instrumentation
never costs a delivery cycle.

## What this is not

- Not model training input — winner identities never feed features
  (the v1 notice-only rule stands).
- Not the live track record — simulation rows are clearly a *simulation*
  of customers who never subscribed; the sales-grade proof for a real
  customer stays the delivery ledger.
