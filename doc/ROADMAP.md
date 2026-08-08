# Roadmap — low-hanging prediction problems

Sibling problems to the single-bidder flag, found by auditing [`FIELDS.md`](../FIELDS.md)
for labels that are already extracted (2026-08-02). Selection rule: the label must be
a structured code or count in a notice the pipeline already parses — **no text
embeddings, no external data purchase, no new cleaning**. Each model is one CatBoost
run on data already on disk, and each ships as another column in the same weekly
subscription list ("few bidders · won't be cancelled · deadline stable · SME wins
here"), so every model raises willingness-to-pay at near-zero marginal data cost.

Explicitly out of scope: cost/price estimation (heavy cleaning, embedding-dependent,
expensive labels) and anything built on buyer-history aggregates (v1 trains on
notice-only features; see [`MODELING.md`](MODELING.md)).

Ranked by effort-to-value:

## 1. Dead tender prediction — "will this game get cancelled?"

- **Question at notice time:** will this procedure ever produce a winner?
- **Label (already extracted):** `result_code` / `decision_reason` in
  `awards.parquet` — eForms codes for "no winner chosen", "no tenders received",
  "all tenders rejected", procedure discontinued.
- **Why it sells:** a cancelled procedure is worse than a lost bid — every bidder
  paid full bid cost and nobody won the pot. ECA SR 28/2023 (already cited in
  [`BUSINESS_CASE.md`](BUSINESS_CASE.md)) shows failed/re-run procedures are common
  and rising.
- **Bonus angle:** `no-rece` (zero bids received) is the loneliest tender of all —
  the buyer almost always re-runs or negotiates. An alert "this died with no bids,
  watch for the rerun" is a free-hand signal.
- **Shape:** binary, identical feature set to the single-bidder model.

## 2. Deadline-extension / corrigendum prediction — "don't rush your bid"

- **Question at notice time:** will this deadline move / will the documents change?
- **Label (already extracted and linked):** `is_corrigendum`, `changed_notice_id`,
  `change_reasons`, `n_corrections_so_far` in `tenders.parquet`.
- **Why it sells:** bid teams burn weekends meeting deadlines that then slip. A
  notice likely to accumulate corrigenda also signals a chaotic buyer, which
  correlates with cancellation — problems 1 and 2 share features.
- **Shape:** binary (any correction) or "extension likely". Cheapest model on the
  list: label and features live in the same parquet file.

## 3. Effective competition — upgrade to the single-bidder target

- **Question:** how many *admissible* bids will show up (not just submitted)?
- **Labels (already extracted):** `n_tenders_inadmissible`,
  `n_tenders_abnormally_low` alongside `n_tenders` in `awards.parquet`.
- **Why it matters:** a tender with 6 bids where 4 are thrown out on formalities is
  effectively a 2-player game. Retraining the existing model on
  `n_tenders − n_tenders_inadmissible` makes the lonely-tender list strictly better
  with zero new data.
- **Side product:** a high predicted-inadmissible share is its own signal —
  "formality trap: competition looks scary but half the field disqualifies itself."
- **Shape:** not a new product; a target upgrade for the existing model.

## 4. SME-winnable classifier — "does David ever win here?"

- **Question at notice time:** does a small firm actually stand a chance?
- **Labels (already extracted):** `winner_size`, `n_tenders_sme` / `micro` /
  `small` / `medium` in `awards.parquet`.
- **Why it sells:** the buyer-declared `sme_suitable` flag (BT-726) is cheap talk;
  this model is trained on who *actually* won. Truth-serum filter for exactly the
  mid-size contractors the business case targets. Markets itself: "we tell small
  firms where small firms win."
- **Shape:** binary (SME wins yes/no).

## 5. Time-to-decision buckets — "when will you hear back?"

- **Question at notice time:** fast, normal, or glacial award decision?
- **Labels (already extracted):** `award_date`, `contract_signed_date` vs
  `deadline_date`.
- **Why it sells:** contractors plan crews and cash flow around award dates; a
  "glacial buyer" flag is a legitimate tiebreaker between two similar tenders.
- **Shape:** coarse 3-bucket classification. Weakest bid/no-bid signal of the five,
  hence last.

## Suggested order of attack

Start with **#1 (cancellation)**: cleanest poker story, label already in
`result_code`, reuses the single-bidder pipeline verbatim. Fold **#3** into the next
retrain of the existing model since it is a target change, not a new pipeline.
