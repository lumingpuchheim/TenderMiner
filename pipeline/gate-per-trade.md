# Component: The gate measures per trade

Decided with the operator 2026-08-23, after the widened store moved two
numbers nobody asked to move. Companion to [`all-trades.md`](all-trades.md);
this is the relevance-gate half of the migration.

## 1. The finding

The operator's fear, verified: **pooled numbers steer per-trade behaviour.**
Two demonstrations from the first widened calibration:

- The code-trust baseline fell 0.330 -> 0.284 because "two random lots" began
  to mean one construction and one IT job, and 122 construction codes were
  certified by that dilution alone. Fixed 2026-08-23: a code is judged
  against random lots of its own division (`calibrate.code_trust`).
- The recommended text bar fell 0.700 -> 0.660 on the pooled search — looser
  than the one paying customer's stored 0.68, and looser only because IT
  entered the average. Quarantined, never installed.

The remaining exposure was the nightly rejector: `HARD_BAR` (2.2% wrong-trade
leakage, the operator's standing refusal) enforced on the POOLED benchmark
would let construction leak 2.4% behind IT's 1.4%. The IT benchmark cases
(1,270, merged on this branch) are deliberately kept off master until the
per-trade split below ships with them in one move.

## 2. Two kinds of settings — only one splits by trade

**Numbers in the customer's record** (`min_relevance`, `min_code_hard`,
`min_code_soft`): already per subscription; the calibration now *recommends*
them per trade, and a new customer draws from their trade's table. Nothing
about an existing subscription moves.

**Rules of the machine** (`CONVICT_BODY_MIN`, `CONVICTION_NOMINATES`, …): one
code path for everyone. These are not split; they are *measured* per trade,
and a proposed value must clear the bar in every trade with enough evidence —
clearing it on average counts for nothing. A rule that cannot satisfy both
trades at once is a finding, and only then does that specific rule gain a
per-trade version.

## 3. Trade groups

`evidence.TRADE_GROUPS`: one group per CPV division in the store — 45 ->
`construction`, 48 -> `software`, 72 -> `it-services`. Three divisions,
three bars, and **no group is ever a merge of two divisions** (operator,
2026-08-23: "treat some separately so that one never interferes the other").

48 and 72 were briefly one group, argued from shared language and shared
firms. That argument has the same shape as the pooled numbers this component
exists to stop: a bar measured across two divisions is a bar neither of them
chose, and a division that grows drags the other's number with it. A division
admitted to the store later gets its own group in the same commit.

Measurement-internal, from `cpv_main` — never customer-facing, which stays
title-words by standing rule. A firm's group is the majority division of its
embedded wins; firms with no majority stay in the pooled numbers only, which
after the split includes firms winning evenly across software and IT
services. That is the price of the split and it is paid by the measurement,
never by a customer — a subscription's market is its own `cpv_prefixes`.

## 4. What changes

- **`calibrate.py`** runs its threshold search once per trade group over that
  group's sub-store (wins, negatives, admitted-volume pool and baselines all
  inside the group — the world a customer of that trade actually lives in).
  One receipt with a table per trade; the trusted-codes JSON gains a
  `defaults` block keyed by trade. The per-division trust cut (2026-08-23) is
  unchanged by the grouping: a code's baseline was already its division's.
- **`evidence.judge_run`** tallies recall and leakage per trade beside the
  pooled numbers; the judge JSON carries `by_trade` per configuration row.
- **`backplay.judge_read`** sets each measurement's `leakage` to the WORST of
  the pooled number and any trade with at least `TRADE_MIN_NEG` negatives,
  recording `leakage_by_trade` and naming the binding trade. One enforcement
  point: `rejects()` and `knobs`' HARD_BAR checks inherit the per-trade bar
  without a line of their logic changing, and every rejection line names the
  trade that caused it.
- **`receipt_gate_flips.py`** — the receipt gating the artifact swap: every
  construction benchmark case, and the paying customer's recent market,
  judged under the SERVING trusted-codes list and under the corrected one;
  every verdict that flips is listed with its title for the operator to read.
  Text-channel scores cannot move (a lot's embedding depends only on its own
  text), so flips isolate the code-channel change.

## 5. Order

Receipts before artifacts, artifacts before benchmark: flip receipt read by
the operator -> corrected trusted codes + per-trade calibration + per-trade
backplay + IT benchmark merge together -> nightly backplay meets the IT cases
only with the per-trade bar already in force.
