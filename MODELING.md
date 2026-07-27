# TenderMining — Modeling Approach

How to train a model that estimates a tender's **price** and **expected number of
bidders** at call time, and verifies the estimate against the later award.

This document assumes the data source is already in place — TED notices are fetchable via
[`ted_download_sample.py`](ted_download_sample.py) (see [`README.md`](README.md) and
[`FINDINGS_ted.md`](FINDINGS_ted.md)). It describes the method, not the download.

## 1. Goal

For an open tender that has just been published (award not yet known), predict:

- **Price** — the final contract value (EUR).
- **Expected bidders** — how many tenders will be received.

Then, once the award is published, **verify** the prediction against reality.

## 2. Data structure this relies on

On TED, one procurement is a sequence of **separate** notices sharing one
`procedure-identifier`:

```
        join on procedure-identifier
cn-standard (tender / call)  ───────────►  can-standard (award)
   known at bidding time                     revealed at the outcome
```

- `notice-type` says which a record is (`cn-standard` = tender, `can-standard` = award).
- `procedure-identifier` (a shared UUID) says which records **belong together**.

The usable training set is procedures that have **both** a tender and an award: the tender
supplies the inputs, the award supplies the labels.

## 3. Features (inputs) — from the tender, no LLM

Extracted by **direct JSON key access** (no text parsing, no LLM). This is the baseline.
All are structured eForms fields that exist for any tender (bridge, IT, catering alike):

| Concept | TED field (on the tender) | Encoding |
| --- | --- | --- |
| Budget hint | `estimated-value-lot` / `-glo` | EUR, log-scaled |
| Category | `classification-cpv` | first 4–5 digits, categorical |
| Category breadth | count of distinct `classification-cpv` | integer |
| Region | `place-of-performance-subdiv-lot` | NUTS code, one-hot |
| Effort / duration | `contract-duration-period-lot` | normalise to days |
| Procedure | `procedure-type` | categorical (open / restricted / negotiated) |
| Nature | `contract-nature` | works / services / supplies |
| Reach | `gpa-lot` | boolean (WTO-covered) |
| Structure | `framework-agreement-lot` | none / framework |
| Award logic | `award-criterion-type-glo`, `-number-weight-lot` | price-only vs quality-weighted |
| Buyer | `organisation-name-buyer`, `buyer-country` | categorical / id |

**Optional LLM/NLP layer (not the baseline):** TF-IDF or embeddings over `description-proc`
can be concatenated to the structured features if they add signal — but the structured
features stand alone first.

## 4. Targets (labels) — from the award

| Target | TED field (on the award) | Notes |
| --- | --- | --- |
| Price | `total-value` (+ `total-value-cur`) | convert all to one currency (EUR) |
| Expected bidders | `received-submissions-type-val` | take the max of the breakdown = total bids |

## 5. Leakage rule (critical)

Inputs must be limited to **what a bidder sees on the call**. Never feed award-side fields
(`total-value`, `received-submissions-type-val`, `winner-size`, `winner-country`) as
inputs — they are only known after the outcome. They are **targets**, not features.

## 6. Training pipeline

1. **Collect** historical `cn-standard` and `can-standard` notices for a scope
   (e.g. `--country DEU --cpv 45`, a date range).
2. **Join** on `procedure-identifier`; keep procedures that have both notice types.
3. **Build the row:** features from the tender (§3), labels from the award (§4).
4. **Clean:** normalise currency to EUR; normalise duration to days; add missing-value
   flags and impute; clip/winsorise value outliers (framework ceilings, unit artefacts).
5. **Split by time:** train on older procedures, test on newer ones (mimics the real task
   and prevents leakage). No random shuffle across time.
6. **Fit two baseline regressors** (no LLM), e.g. gradient-boosted trees:
   - price model: features → `log(total-value)`
   - bidders model: features → `received-submissions` count
7. **(Optional)** add the text layer from §3 and compare against the structured baseline.

## 7. Estimation (inference) on a live tender

Given a freshly published `cn-standard` with no award yet:

1. Extract the §3 features from its JSON (same code as training).
2. Predict price and expected bidders.
3. Store the prediction with the `procedure-identifier`.

## 8. Verification against the award

When the matching `can-standard` is published later:

1. Look it up by `procedure-identifier`.
2. Read the actual `total-value` and `received-submissions-type-val`.
3. Compare to the stored prediction.
   - Price: MAE / MAPE on value (or on `log` value).
   - Bidders: MAE, and accuracy of the single-bidder flag (0/1 bidder vs many).
4. Track error over time as a rolling metric; retrain periodically.

## 9. Honest limitations

- **Coverage is uneven** — some notices omit estimated value, duration, or bid counts;
  handle as missing, don't assume presence.
- **Award-only / tender-only procedures** are unusable for training (no label, or no
  pre-award features) — only matched pairs count.
- **Multilingual text** — if the optional NLP layer is used, descriptions are in the
  buyer's language; filter by country or use a multilingual model.
- **Regime bias** — TED is above-threshold only; below-threshold German contracts live on
  oeffentlichevergabe.de and are out of this model's scope.
