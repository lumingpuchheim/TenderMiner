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

- **Who**: every distinct `winner_names` entry in the awards store
  (~5,300 exact-name companies; ~1,300 with ≥2 won lots). Identity is the
  exact name string — spelling variants of one firm count as two companies.
  Acceptable for aggregate numbers; clean before using any single row for
  outreach.
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

## What this is not

- Not model training input — winner identities never feed features
  (the v1 notice-only rule stands).
- Not the live track record — simulation rows are clearly a *simulation*
  of customers who never subscribed; the sales-grade proof for a real
  customer stays the delivery ledger.
