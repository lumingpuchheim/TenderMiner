# TenderMining — Modeling Approach

How to train a model that estimates a tender's **price** and **expected number of
bidders** at call time, and verifies the estimate against the later award.

This document assumes the data source is already in place — TED notices are fetchable via
[`ted_download_sample.py`](../ted_download_sample.py) (see [`README.md`](../README.md) and
[`FINDINGS_ted.md`](FINDINGS_ted.md)). It describes the method, not the download.

## 1. Goal

For an open tender that has just been published (award not yet known), predict:

- **Price** — the final contract value (EUR).
- **Expected bidders** — how many tenders will be received.

Then, once the award is published, **verify** the prediction against reality.

## 2. Data structure this relies on

### 2.1 The unit of observation is the LOT, not the notice

A procurement procedure is often split into **lots** (*Lose*) — independently awardable
pieces with their own scope, own bidders, own winner, and own value. Real examples from
our data: a catering notice with 4 lots (one per school/Kita site, awarded €138k–€547k
separately); a building project split into numbered trade lots (Los 3 masonry … Los 14
outdoor works), each with its own Leistungsverzeichnis/GAEB and its own award.

Therefore **one training row = one lot**:

- a single-lot procedure contributes exactly one row (the common, trivial case);
- a 14-lot building project contributes up to 14 rows, each with the lot's own
  description, quantities, value, and bid count.

Treating a multi-lot notice as one row would smear all trades' features together and
train, e.g., the metalwork price against the electrician's quantities — wrong by
construction. eForms encodes this explicitly: field suffixes `-proc` (procedure-level,
shared across lots), `-lot` (per lot), `-glo` (lots group). But beware: a multi-value
array in the *search API* is **not** proof of a lot-level breakdown. Verified against
the XML: `received-submissions ['4','4','4','0','0']` on notice `516890-2026` is a
**single lot's per-type statistics** (tenders=4, t-esubm=4, t-sme=4, t-oth-eea=0,
t-no-eea=0), while `['4','3']` on `517845-2026` really is two lots' totals (4 and 3).
The flat array cannot distinguish the two cases — see §2.3.

### 2.2 Notices and the join

On TED, one procurement is a sequence of **separate** notices sharing one
`procedure-identifier`:

```
        join on procedure-identifier (+ lot id)
cn-standard (tender / call)  ───────────►  can-standard (award)
   lots defined, inputs known                per-lot results revealed
```

- `notice-type` says which a record is (`cn-standard` = tender, `can-standard` = award).
- `procedure-identifier` (a shared UUID) says which records **belong together**.
- The **lot identifier** (e.g. `LOT-0003`) keys the row *within* the procedure; the
  row join is `(procedure-identifier, lot-id)`.

The usable training set is lots whose procedure has **both** a tender and an award: the
tender's lot supplies the inputs, the award's matching lot result supplies the labels.

### 2.3 Getting per-lot rows out of TED (the flat-array problem and its fix)

**The problem:** the TED *search API* returns lot fields as flat lists — all lots'
values thrown together per field, without saying which value belongs to which lot, and
with differing list lengths (observed: 4 CPV entries, 3 winner sizes, 10 submission
numbers on one notice). Per-lot rows cannot be reconstructed from that. Worse, the flat
lists can *look* multi-lot when they are not: notice `517829-2026` shows 4 CPVs and 3
winner sizes in the search response but is a **single-lot** procedure in its XML — the
duplicates are main+additional classifications and consortium members flattened
together.

**The fix (verified against the live service): fetch the notice XML for every award.**

An earlier draft proposed a "Route 1" that trained on single-lot procedures straight
from the search-API flat lists ("one value per field, nothing to misalign"). That is
**wrong** for the bidder-count field: `received-submissions-type-val` on a *single-lot*
notice routinely holds 3–5 values, because eForms publishes a per-**type** statistics
breakdown per lot (`StatisticsCode` `tenders` / `t-esubm` / `t-sme` / `t-oth-eea` /
`t-no-eea` …), and the search API flattens away the codes. Observed on live notices:

| Notice | Lots | Flat array | Actual meaning (from XML) |
| --- | --- | --- | --- |
| `516890-2026` | 1 | `[4, 4, 4, 0, 0]` | type breakdown of one lot: tenders=4, t-esubm=4, t-sme=4, rest 0 |
| `515758-2026` | 1 | `[6, 6, 3]` | type breakdown: tenders=6, t-esubm=6, t-sme=3 |
| `517845-2026` | 2 | `[4, 3]` | per-lot totals: LOT-0001=4, LOT-0002=3 |

Identical-looking arrays, different semantics; the flat list alone cannot be
disambiguated. (Max-of-array recovers the total for single-lot notices, since subtypes
cannot exceed it — but knowing the notice *is* single-lot already requires the XML.)

So the bidder-count **label** must come from the notice XML for **all** award notices,
single-lot included. Every search result links its full eForms XML
(`https://ted.europa.eu/en/notice/<number>/xml` — verified: no login, one GET, ~26 KB).
The XML is explicitly structured: `<cac:ProcurementProjectLot>` blocks hold each lot's
own fields, each `<efac:LotResult>` names the lot it belongs to
(`<efac:TenderLot><cbc:ID>LOT-0001`), and its `<efac:ReceivedSubmissionsStatistics>`
blocks carry labeled counts (`StatisticsCode` + `StatisticsNumeric`) — take
`tenders` as the bid count; the SME / e-submission / cross-border splits come free.
Ingestion flow: search API to *find* notices (filtered, cheap) → one GET per notice
for the XML → parse lots and statistics from the XML.

(A structured per-lot form also exists in the German OCDS feed, but that is a second
data source — rejected; TED stays the single source.)

## 3. Features (inputs) — from the tender's lot, no LLM

Extracted by **direct JSON key access** (no text parsing, no LLM). This is the baseline.
Each row combines the **lot's own fields** with the **procedure-level fields shared by
all lots** of the notice:

| Concept | TED field | Level | Encoding |
| --- | --- | --- | --- |
| Budget hint | `estimated-value-lot` | lot | EUR, log-scaled |
| Category | lot's `classification-cpv` | lot | first 4–5 digits, categorical |
| Region | `place-of-performance-subdiv-lot` | lot | NUTS code, one-hot |
| Effort / duration | `contract-duration-period-lot` | lot | normalise to days |
| Reach | `gpa-lot` | lot | boolean (WTO-covered) |
| Structure | `framework-agreement-lot` | lot | none / framework |
| Award logic | `award-criterion-number-weight-lot` | lot | price-only vs quality-weighted |
| Lot context | number of lots in the procedure; this lot's position | procedure | integers |
| Procedure | `procedure-type` | procedure | categorical (open / restricted / negotiated) |
| Nature | `contract-nature` | procedure | works / services / supplies |
| Buyer | `organisation-name-buyer`, `buyer-country` | procedure | categorical / id |

Text for the optional NLP layer is likewise lot-first: `title-lot`/`description-lot`
for this lot, with `description-proc` as shared context.

**Optional LLM/NLP layer (not the baseline):** TF-IDF or embeddings over `description-proc`
can be concatenated to the structured features if they add signal — but the structured
features stand alone first.

## 4. Targets (labels) — from the award's matching lot result

| Target | Source (award side) | Notes |
| --- | --- | --- |
| Price | the lot's own awarded value (OCDS: `contracts[]` with matching `relatedLots`; eForms: the LotResult value) | convert all to one currency (EUR); `total-value` is the whole notice — do NOT use it as a per-lot label in multi-lot procedures |
| Expected bidders | the lot's `ReceivedSubmissionsStatistics` block in the notice XML, code `tenders` | never from the search API's flat array — it mixes per-lot totals with per-type breakdowns indistinguishably (§2.3) |

## 5. Leakage rule (critical)

Inputs must be limited to **what a bidder sees on the call**. Never feed award-side fields
(`total-value`, `received-submissions-type-val`, `winner-size`, `winner-country`) as
inputs — they are only known after the outcome. They are **targets**, not features.

## 6. Training pipeline

1. **Collect** historical `cn-standard` and `can-standard` notices for a scope
   (e.g. `--country DEU --cpv 45`, a date range), resolving lot structure from the
   structure-preserving source (§2.2).
2. **Join** on `(procedure-identifier, lot-id)`; keep lots whose procedure has both
   notice types and whose lot has an award result.
3. **Build the row — one per lot:** lot + procedure features from the tender (§3),
   labels from the award's matching lot result (§4).
4. **Clean:** normalise currency to EUR; normalise duration to days; add missing-value
   flags and impute; clip/winsorise value outliers (framework ceilings, unit artefacts).
5. **Split by time AND by procedure:** train on older, test on newer (mimics the real
   task). Additionally, sibling lots of one procedure are highly correlated (same buyer,
   site, date) — all lots of a procedure must land in the **same** split, never spread
   across train and test, or the model gets graded on near-duplicates of its training
   rows.
6. **Fit two baseline regressors** (no LLM), e.g. gradient-boosted trees:
   - price model: features → `log(total-value)`
   - bidders model: features → `received-submissions` count
7. **(Optional)** add the text layer from §3 and compare against the structured baseline.

## 7. Estimation (inference) on a live tender

Given a freshly published `cn-standard` with no award yet:

1. Resolve its lots; extract the §3 features per lot (same code as training).
2. Predict price and expected bidders **per lot**.
3. Store each prediction with `(procedure-identifier, lot-id)`.

## 8. Verification against the award

When the matching `can-standard` is published later:

1. Look it up by `(procedure-identifier, lot-id)`.
2. Read the lot's actual awarded value and received-submissions entry (§4).
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

## 10. Two-stage procedures and the participation deadline — 2026-08-20

A `neg-w-call` or `restricted` contract notice has **no offer deadline**:
firms first request participation by `cac:ParticipationRequestReceptionPeriod/
cbc:EndDate` (eForms BT-1311), and only the invited ones later bid. 5,656
tender rows in the 2023-11→2026-08 store (~7 % of lots) have this shape, and
their lots end 0-1 bids at ~29-36 % — three times the market — because few
firms apply. They were invisible for a bad reason: the extractor read only
`TenderSubmissionDeadlinePeriod/EndDate` (BT-131), so `deadline_date` was
null, the replay's openness filter (`deadline >= cutoff`) dropped NaT
forever, and the delivery promise (`subscriptions.deadline_ok`) refused a
dateless lot. Found when the operator refused to believe "5 % of tenders
have no deadline" — correctly.

The rule since 2026-08-20: **the actionable date is the offer deadline, or
for two-stage procedures the participation-request deadline** —
`single_bidder.action_deadline` computes it, and one boundary each uses it:

* `features.py` extracts `participation_deadline_date` (own column;
  `deadline_date` keeps meaning the offer deadline; the `date` role gives
  the model `span__participation_deadline_date` — a feature-schema flag day,
  handled by `learn()`'s named unconditional promotion);
* the replay and live scoring treat a lot as open while its actionable date
  is in the future (a genuinely dateless lot still fails soft — scored
  live, never promised);
* `deadline_ok` honours the promise against the actionable date, so these
  lots can be picks;
* every display says what the date is: „Teilnahmeantrag bis 16.03." instead
  of „Frist 16.03." (`util.frist`).

The replayed record must be re-measured after this lands (the model has
never been graded on the segment); until then the pages simply do not carry
it, which only understates.
