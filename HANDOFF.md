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
| candidate | `/data/scratch/calib2/` on the server | per-division, 95 codes — REJECTED by the flip receipt, 10 construction cases worse |

Corrected calibration numbers (candidate): per-division cuts 45 0.479,
48 0.456, 72 0.475; recommended text bar back to **0.700** (the pooled run had
dragged it to 0.660, looser than the paying customer's own 0.68); recall
63.0%, leakage 1.7% (bar 2.2%).

## What happened on 2026-08-23, afternoon

**The flip receipt ran to the end, and it FAILS.** 1,712 construction lots
judged under both trusted-codes lists; 17 verdicts change: 7 fixed, 10 worse.
The full list with titles is in `/data/logs/flips.log` on the server. The
seven losses are not borderline lots — SPIE SAG on Hoch-/Mittelspannung, Nahm
and HADI on heating, Johmann on sanitary, Beck on Zimmer-/Holzbau, Heinrich
Schmid on Bodenbeschichtung, Schandert on Sonnenschutz: each is the firm's
own trade, thrown out. The seven gains are almost all one narrow shape,
floor-covering lots correctly dropped. **The candidate list is not swapped and
must not be.** Trusted codes went 45: 56 -> 86, plus 7 codes in 48 and 2 in
72; which of those two movements causes the losses is NOT yet established.

**The trade groups are now one per division** (operator: "there are three
categories from CPV and there are only two values... treat some separately so
that one never interferes the other"). 48 and 72 were one group on a
shared-firms argument; that argument has the same shape as the pooled numbers
this work exists to stop. `evidence.TRADE_GROUPS` is now
`{'45': 'construction', '48': 'software', '72': 'it-services'}` and everything
downstream keys off that table, so calibrate searches three sub-stores,
`judge_run` tallies three, and `judge_read` takes the worst of pooled and
three trades. `calibrate.py --trade GROUP` (repeatable) searches one group
without re-running the others.

Sizes, for what each bar will rest on: division 45 has 81,011 lots and 4,528
firms with >= 3 awarded lots; 72 has 7,106 and 335; 48 has 4,722 and 157.
Software is the thin one — its numbers will be coarser than construction's,
and `backplay.TRADE_MIN_NEG` may keep it from binding at all. Not a reason to
merge it into 72; a reason to read its row with its firm count beside it.

## The three-trade calibration ran. Software fails the leakage bar.

`/data/scratch/calib3/` on the server, 2026-08-23 15:59, three groups:

| trade | min_relevance | min_code_relevance | recall | leakage | firms | positives |
| --- | --- | --- | --- | --- | --- | --- |
| construction | 0.700 | 0.775 | 64.5% | 1.5% | 4,528 | 30,712 |
| it-services | 0.680 | 0.675 | 56.5% | 0.9% | 335 | 2,714 |
| software | 0.680 | 0.675 | 58.2% | **3.0%** | 157 | 1,544 |

**Software leaks 3.0% against the standing 2.2% refusal, and that is the
BEST point in the whole grid for software** — least leakage at the volume
floor. Construction 1.5% and IT services 0.9% put the pooled number at 1.7%,
which looks fine and hides it completely. Had 48 and 72 stayed one group,
software's 3.0% would have been averaged with IT services' 0.9% over twice
the firms and a software customer would have been given a bar no software
evidence ever cleared. That is the split earning itself on the first run.

So construction and IT services have a defensible recommendation and
**software does not have one yet**. Three explanations are still open and
this run cannot separate them: 157 firms is thin; division 48 mixes licence
resellers, hardware suppliers and real software houses (see the ACP case
below); or software genuinely needs a bar tighter than TEXT_GRID's 0.70
ceiling, which the search cannot reach.

Also from this run: construction's own code bar rises 0.675 -> 0.775 once it
is measured alone. And calib3's trusted-code list is the SAME 95 codes as
calib2's, verified set-equal — so the failed flip receipt stands unchanged
and needs no re-run.

## NEXT STEPS, in order

1. **Decide what software gets.** It cannot be 0.680 at 3.0%. Cheapest test
   of the three explanations, in order: (a) raise TEXT_GRID's ceiling above
   0.70 and re-run `calibrate.py --trade software` alone (~5 min, cannot
   touch the other two trades' numbers) — if the leakage falls under 2.2% at
   0.72-0.75, software simply needs a tighter bar; (b) if it does not, the
   negatives are the suspect, not the threshold.

2. **Re-read the doubtful division-48 labels.** ACP IT Solutions AG is
   marked `out` on 00621440-2025, a framework for used Microsoft volume
   licences, while ACP's own wins include 'Lieferung Lizenzen für Microsoft
   Windows'. If labels like that are wrong, software's leakage is measured
   against a bad exam. Operator was offered a re-read of the doubtful 48
   pairs; not yet answered.

3. **The trusted-codes swap is still blocked** by the flip receipt (7 fixed,
   10 worse; the losses are firms' own trades). Before proposing any swap,
   build a candidate list restricted to division 45 and see whether the seven
   losses survive — that separates 'IT codes entered the file' from
   'construction gained 30 codes of its own'.

4. **Still open, lower priority**
   - No IT customer uses the gate yet; IT friends run on the trade filter
     alone (`cpv_prefixes: ["72","48"]` + `profile_texts`, no
     `min_relevance`). Before the gate is turned on for a real IT customer,
     show the operator a sample of how it judges IT lots.
   - `[shadow] guard sample skipped ('sub_id')` in the cycle log is
     PRE-EXISTING and deliberately caught so it cannot fail a cycle. It means
     the gate guardrail sample is not being queued. Not investigated.
   - IT tenders carry non-IT contamination (nutrition coaching under CPV
     72000000, electrical work under 72220000). Worth measuring — it bounds
     how much of the ~92 IT lots/week is really IT.
   - The `Leistung` section is empty on nearly every IT lot, so both readers
     and the gate lean on title + description there.
   - WATCH MONDAY'S CYCLE RUNTIME (~1h recurring against a 90-minute gap).

## Benchmark coverage, by division (after the IT merge)

| division | labeled cases | in | out | store lots |
| --- | --- | --- | --- | --- |
| 45 construction | 1,752 | 592 | 1,160 | 81,011 |
| 48 software | 566 | 279 | 287 | 4,722 |
| 72 IT services | 704 | 291 | 413 | 7,106 |

Per lot in the store, IT and software are already sampled about five times
harder than construction. `label_packets_it.py` skips (pub, firm) pairs
already in the benchmark, so raising its FRACTION and re-running yields only
fresh packets — that is the way to deepen the sample without duplicating it.

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
