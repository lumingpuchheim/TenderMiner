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
report → **render every active customer** → refresh the dashboard.

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

Leave `min_relevance` at the receipt default (see §3) unless feedback says
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

## 3. The study side: embeddings, calibration, trust

These are **event-driven, not scheduled**. The loop reads their last
committed output; nothing waits on them.

| When | Run | Writes |
| --- | --- | --- |
| automatically each cycle | (inside `loop.py run`) | new lot vectors, `data/embeddings/<tag>/` |
| after changing the embedding model; after a big backfill; else ~monthly | `python calibrate.py` | `calibration_<tag>.md`, `trusted_codes_<tag>.json` (committed receipts) |
| curiosity / sales prep | `python calibrate.py --fingerprint "Firma GmbH"` | console only: the firm's named trades |

**Reading the receipt** (`calibration_<tag>.md`): the configuration table's
last rows are the shipping gate; "leakage" = share of wrong-trade lots that
would pass at the 90%-recall promise; "volume" = share of the whole market
the average profile admits. If a recalibration moves the defaults, update
`DEFAULT_MIN_RELEVANCE` / `DEFAULT_MIN_CODE_RELEVANCE` in `relevance.py` in
the same commit as the receipt.

**Rebuild everything from scratch** (new machine; sidecar deleted):

```
python embed.py --labels        # full backfill: ~40 min labels+lots MiniLM,
                                # several hours jina — checkpointed every
                                # 1000 lots, safe to interrupt and re-run
```

## 4. Switching the embedding model

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

## 5. Where things live (quick reference)

| Path | What | In git? |
| --- | --- | --- |
| `data/store/*.parquet` | the two tables (tenders, awards) | no (rebuildable) |
| `data/embeddings/<tag>/` | vectors: lots + CPV labels, per model | no (rebuildable) |
| `data/subscriptions.jsonl` | customers, versioned | **no — private** |
| `data/ledger/*.jsonl` | predictions, grades, deliveries (append-only) | no |
| `data/reports/…` | operator report, dashboard, customer HTML | no |
| `calibration_<tag>.md`, `trusted_codes_<tag>.json` | study receipts | **yes** |
| `cpv_2008_de.csv` | official CPV dictionary (German) | yes |
| `embed.py` / `calibrate.py` / `relevance.py` / `loop.py` | the programs | yes |
