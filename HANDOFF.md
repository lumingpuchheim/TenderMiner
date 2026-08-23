# Handoff — all-trades migration (IT/software), 2026-08-23

## Where you are

Worktree `C:\Users\user\workspace\TenderMining\.claude\worktrees\all-trades-it`,
branch `worktree-all-trades-it`, tree clean, **5 commits ahead of
origin/master and none of them pushed — deliberately.**

```
79fb0b7  the gate is measured per trade (backplay bar, calibrate --trade, flip receipt)
302056b  a CPV code is judged against random lots of its own division
3400254  merge origin/master (firm-identity work: firms.py)
aca3b6f  val_by_division moves to a 26-week window
d475cc7  the relevance benchmark learns IT: 1,270 cases across 420 firms
```

Read `pipeline/all-trades.md` (the store/model half) and
`pipeline/gate-per-trade.md` (the gate half) first. They are the spec; this
file is only the current position.

## What is DONE and live on the server

- **Store widened to CPV 45 + 48 + 72.** Backfill ran on the server (25,367
  new notices, archives kept, ~47 GB free). `cycle.py`'s default scope is
  `45,48,72`.
- **The flag day happened.** Champion `m2026-08-22-194907-mh`, 43,942 labeled
  lots, 1,279 features, 3,827 open lots scored. Both experiment arms promoted
  unconditionally under the announced schema-change rule.
- **Receipt B passed**: construction PR-AUC 0.2752 (45-only) -> 0.2929
  (widened) on the identical construction-only exam. Adding IT did not hurt
  construction; it helped slightly.
- **Receipt A** (the product): construction ~609 lots/week, single-bid 12.0%,
  top-fifth lift 2.31x. Software ~35/week, 35.9%, 1.19x. IT services ~57/week,
  24.2%, 1.61x.
- Cycle runtime was 4h32m, of which ~3h40m was one-time embedding of 18,664 IT
  lots. **Recurring is ~1h against the 90-minute gap before Monday's
  delivery** — not broken (delivery waits on the heavy lock) but tighter than
  before. WATCH MONDAY'S ACTUAL RUNTIME.

## What is NOT live, and must not become live by accident

- **The 1,270 IT benchmark cases are on the branch only.** The nightly
  backplay (04:00) re-measures whenever the benchmark file's hash moves, and
  until the per-trade bar ships with them it would judge knob values on
  pooled evidence. Benchmark and per-trade bar merge together or not at all.
- **The serving trusted-codes list is still the Aug-6 construction-era file**
  baked into the deployed image (56 trusted codes). Nothing has been swapped.

Three trusted-codes files exist:

| | where | what |
| --- | --- | --- |
| serving | in the image, `/app/trusted_codes_jina-v2-base-de.json` | Aug 6, 56 codes, construction era |
| rejected | `/data/scratch/calib/` on the server | pooled/diluted, 220 codes, ~122 certified by dilution — evidence only |
| candidate | `/data/scratch/calib2/` on the server | per-division, 95 codes — the one proposed |

Corrected calibration numbers (candidate): per-division cuts 45 0.479,
48 0.456, 72 0.475; recommended text bar back to **0.700** (the pooled run had
dragged it to 0.660, looser than the paying customer's own 0.68); recall
63.0%, leakage 1.7% (bar 2.2%).

## NEXT STEPS, in order

1. **Re-run the flip receipt — it was interrupted mid-run.** It is the gate on
   swapping the trusted-codes artifact. Run it DETACHED (it takes well over
   10 minutes) and read the output file, don't foreground it:

```bash
ssh debian@57.129.112.187 "docker run -d --rm --name tm-flips -v /home/debian/tm-state:/data -w /app -e PYTHONPATH=/app tendermining:4e1e7da sh -c 'python /data/scratch/receipt_gate_flips.py --data-dir /data --serving /app/trusted_codes_jina-v2-base-de.json --candidate /data/scratch/calib2/trusted_codes_jina-v2-base-de.json --benchmark /app/benchmark_relevance.jsonl > /data/logs/flips.log 2>&1'"
```

   `receipt_gate_flips.py` is already at `/data/scratch/` on the server, but
   re-copy it if you change it. It prints every construction benchmark verdict
   that changes between the two lists, as a readable title, and exits 1 if any
   case the operator has already read gets WORSE. Show the operator the flips.

2. **Re-run the calibration with the current `calibrate.py`.** The candidate
   list came from an earlier copy (`/data/scratch/calibrate_perdiv.py`) that
   has the per-division baseline but NOT the new `--trade` per-trade defaults.
   A fresh run produces the `defaults_by_trade` block (what a NEW customer of
   each trade should be given) and the receipt's per-trade table. Run detached
   into its own scratch dir; the previous full run took ~25 minutes.

3. **Show the operator two things and wait**: the flips, and the per-trade
   defaults table. Only then merge the branch to master and push (which
   auto-deploys via the GitHub Action) — benchmark, per-trade bar, corrected
   trusted codes and receipts in ONE move.

4. **Still open, lower priority**
   - The relevance thresholds are now *recommended* per trade but no IT
     customer uses them yet; IT friends start on the trade filter alone
     (`cpv_prefixes: ["72","48"]` + `profile_texts`, no `min_relevance`).
     Before turning the gate on for a real IT customer, show the operator a
     sample of how it judges IT lots.
   - `[shadow] guard sample skipped ('sub_id')` in the cycle log is
     PRE-EXISTING (also in the 2026-08-18 manual cycle), deliberately caught
     so it cannot fail a cycle. Not investigated. It means the gate guardrail
     sample is not being queued.
   - IT tenders carry non-IT contamination (a nutrition-coaching contract
     under CPV 72000000, electrical work under 72220000). Worth measuring —
     it bounds how much of the ~92 IT lots/week is really IT.
   - The `Leistung` section is empty on nearly every IT lot, so both readers
     and the gate lean on title + description there.

## Standing constraints (learned the hard way this session)

- **Never merge to master before the receipts are read.** The operator's fear
  is pooled evidence steering per-trade behaviour, and it was founded twice.
- Launch labeling/reading subagents in **batches of ~5**, not 14 — a single
  wave exhausted the session limit and killed all of them, and one died
  mid-write leaving a partial file that had to be discarded.
- Long server jobs go **detached** (`docker run -d --rm`), logging to
  `/data/logs/`, then wait on a background poll. Foregrounding them times out.
- Every throwaway docker image/container is removed in the same session
  (CLAUDE.md). All containers used here were `--rm`; nothing was left.
- The operator wants **plain English, no jargon, terse**; findings and the
  evidence behind them BEFORE any fix; and does not want decisions handed
  back when the evidence already settles them.
