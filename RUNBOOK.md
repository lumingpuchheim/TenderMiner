# RUNBOOK — how to operate TenderMining

What to type, when, and what you get back. Component internals live in
[`README.md`](README.md) (pipeline programs) and the design docs
([`ONLINE_LEARNING.md`](ONLINE_LEARNING.md), [`SUBSCRIPTIONS.md`](SUBSCRIPTIONS.md),
[`RELEVANCE.md`](RELEVANCE.md)); this file is only about running things.

## 1. The routine: one command

```
python loop.py run --last 7d
```

One cycle does everything, in order: download the window's notices → rebuild
the store → **update the embedding sidecar** (new lots only, ~5 min) → grade
outcomes → retrain/promote the model → score open lots → write the operator
report → **render every active customer** → **simulate every winner company**
(picks to `data/ledger/simulations.jsonl`, ~2 s, see
[`SIMULATION.md`](SIMULATION.md)) → refresh the dashboard.

- Run it on the cadence you already use (weekly `--last 7d`; daily `--last 2d`
  also works — deliveries are idempotent per day, nothing double-sends).
- Everything customer-facing lands under
  `data/reports/subscriptions/<sub_id>/report_<date>.html` (+ annex).
- Operator artifacts: `data/reports/report_<date>.md`, `data/reports/dashboard.html`.
- **You never schedule the embedding or the gate separately** — they ride
  inside the cycle. If the sidecar is somehow broken, the cycle prints
  `[deliver] relevance gate unavailable … delivering ungated` and continues;
  fix at leisure, nothing is lost.

Re-run a cycle without re-downloading (e.g. after editing a subscription):

```
python loop.py run --last 7d --skip-download
```

**Scheduled on this laptop** (Windows Task Scheduler, task
`TenderMining weekly loop`): every **Monday 08:15**, the cycle above
followed by the simulation scorecard —

```
python loop.py run --last 7d          >> data\logs\loop_scheduled.log
python simulation.py check           >> data\logs\simcheck.log
```

The simcheck log accumulates one dated block per week; watch the hit rate
firm up there as awards publish (~90-day median lag). Nothing else is
scheduled by design: calibration and backtests are event-driven (§4, §3),
and the embedding sidecar rides inside the cycle.

## 2. Customers: add, change, render

A customer is lines in `data/subscriptions.jsonl` (private, gitignored,
append-only — never edit a line, append a higher `version`).

**Add a customer** — append one line. With win history, `profile_refs` are the
publication numbers of their won tenders' contract notices; without history,
one `profile_texts` sentence does it:

```jsonl
{"sub_id": "mueller-elektro", "version": 1, "effective_from": "2026-08-10",
 "name": "Müller Elektrotechnik GmbH",
 "cpv_prefixes": ["45"], "nuts_prefixes": ["DE2"],
 "profile_refs": ["00123456-2026", "00234567-2026"],
 "min_relevance": 0.482,
 "min_deadline_days": 14, "max_picks": 5, "avoid_n": 5, "active": true}
```

Leave `min_relevance` at the receipt default (see §4) unless feedback says
otherwise. Omitting it entirely disables the relevance gate for that customer
(pure CPV/region filter, old behaviour).

**Change a customer** (new trade reference, new threshold, wider region) —
append the same `sub_id` with `version: n+1` and a new `effective_from`.
**Deactivate** — append a version with `"active": false`.

**Render** — rendering is not a separate program: the next `loop.py run`
renders every active subscription (all of them, in milliseconds — one run,
many views). To see a customer's report *now* after editing their line:

```
python loop.py run --last 7d --skip-download
```

and open `data/reports/subscriptions/<sub_id>/report_<date>.html`. There is
deliberately no per-customer switch: every render also appends the delivery
ledger rows that make the track record auditable, and those must stay
complete for every active customer.

## 3. Testing a change against the pilot

Four committed tools, ordered by cost. Reach for the cheapest one that
answers your question; none of them touches the real ledgers or reports.

| Question | Run | Cost |
| --- | --- | --- |
| What would customer X's report look like if a subscription field changed? | `python tryout.py --sub <sub_id> --set FIELD=VALUE` | seconds |
| Why did lot Y pass / fail X's gate? What trade does its text read as? | `python explain.py --sub <sub_id> <TED-number> …` | seconds |
| Would we have recommended this firm's historical solo win, knowing only the past? | `python playback.py --firm "Firma GmbH"` | ~10 min |
| Does a gate/model change make picks better overall? | `python backtest.py` | hours |

- **`tryout.py`** re-renders one customer from the last cycle's prediction
  ledger inside a disposable sandbox (`data/tryout/<sub_id>/`, recreated per
  run) and prints the picks with their gate scores. `--set` is repeatable
  (`--set min_deadline_days=0 --set min_relevance=0.6`); `--keep-expired`
  also shows lots whose deadline has passed. The real subscription file is
  never modified — overrides live only in the sandbox.
- **`explain.py`** prints the profile fingerprint (hard/soft labels), each
  lot's pass path through the gate ladder, and its text→label projections.
  With no TED numbers it explains the profile references themselves — the
  sanity check that a profile reads as the customer's trade.
- **`playback.py`** rebuilds an as-of world before the target's deadline
  (store, trust list, thresholds, model — all time-isolated) and replays
  that cycle: was the win in the market, was it a pick.
- **`backtest.py`** replays every weekly cutoff and grades all picks
  against published outcomes; its report lands in
  `data/reports/backtest_<date>.md`.

Rule of thumb: after editing a subscription, `tryout.py`; when a verdict
surprises you, `explain.py`; before shipping a gate change, `backtest.py`
(and `calibrate.py` for the receipt).

## 4. The study side: embeddings, calibration, trust

These are **event-driven, not scheduled**. The loop reads their last
committed output; nothing waits on them.

| When | Run | Writes |
| --- | --- | --- |
| automatically each cycle | (inside `loop.py run`) | new lot vectors, `data/embeddings/<tag>/` |
| after changing the embedding model; after a big backfill; else ~monthly | `python calibrate.py` | `calibration_<tag>.md`, `trusted_codes_<tag>.json` (committed receipts) |
| curiosity / sales prep | `python calibrate.py --fingerprint "Firma GmbH"` | console only: the firm's named trades |

**Reading the receipt** (`calibration_<tag>.md`): the configuration table's
last rows are the shipping gate; "leakage" = share of wrong-trade lots that
would pass; "volume" = share of the whole market the average profile
admits. If a recalibration moves the defaults, update the `DEFAULT_*`,
`SOFT_*` and `TRADE_READ_*` constants in `relevance.py` in the same commit
as the receipt, and append new subscription versions for customers with an
explicit `min_relevance`.

**Rebuild everything from scratch** (new machine; sidecar deleted):

```
python embed.py --labels        # full backfill: ~40 min labels+lots MiniLM,
                                # several hours jina — checkpointed every
                                # 1000 lots, safe to interrupt and re-run
```

## 5. Switching the embedding model

The active model is the committed default `MODEL_TAG` in `embed.py`; the
`EMBED_MODEL` env var overrides it per-run so a new sidecar can build while
the old one keeps serving:

```
EMBED_MODEL=<new-tag> python embed.py --labels    # 1. full backfill, background-able
EMBED_MODEL=<new-tag> python calibrate.py         # 2. receipts for the new tag
# 3. compare receipts; if the new model wins:
#    - flip the default MODEL_TAG in embed.py
#    - update the two DEFAULT_* thresholds in relevance.py from the new receipt
#    - append new subscription versions for customers with explicit min_relevance
#    - commit code + receipts together, push
```

New models must first be added to the `MODELS` registry in `embed.py`
(name + dimensions). Old sidecars stay on disk untouched — a flip is one
reviewable commit, and rolling back is flipping the constant back.

## 6. Where things live (quick reference)

| Path | What | In git? |
| --- | --- | --- |
| `data/store/*.parquet` | the two tables (tenders, awards) | no (rebuildable) |
| `data/embeddings/<tag>/` | vectors: lots + CPV labels, per model | no (rebuildable) |
| `data/subscriptions.jsonl` | customers, versioned | **no — private** |
| `data/ledger/*.jsonl` | predictions, grades, deliveries, simulations (append-only) | no |
| `data/reports/…` | operator report, dashboard, customer HTML | no |
| `calibration_<tag>.md`, `trusted_codes_<tag>.json` | study receipts | **yes** |
| `cpv_2008_de.csv` | official CPV dictionary (German) | yes |
| `embed.py` / `calibrate.py` / `relevance.py` / `loop.py` / `simulation.py` | the programs | yes |
| `tryout.py` / `explain.py` / `playback.py` / `backtest.py` | the test tools (§3) | yes |
| `data/tryout/`, `data/playback_asof/`, `data/backtest_world/` | disposable test sandboxes | no |
