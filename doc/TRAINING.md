# TRAINING — single-bidder classifier

Status: implemented in [`single_bidder.py`](../single_bidder.py) and trained every cycle by
[`loop.py`](../loop.py) (`learn()`). Complements [`MODELING.md`](MODELING.md)
(older, broader price+bidders sketch — where it says "first 4–5 digits" of the CPV,
the CPV depth section below supersedes it); this document is the concrete recipe for
the first model. Field roles: [`FIELDS.md`](../FIELDS.md). EDA:
[`eda_single_bidder.ipynb`](../eda_single_bidder.ipynb).

## Label

**Insufficient competition** = lot received **0 or 1 bids** (`awards.n_tenders <= 1`).

- Zero-bid lots are verified failed procedures (`clos-nw` / no-rece) — positives.
- Drop rows flagged `winner_but_zero_tenders` (reporting errors).
- Base rate ~12% → imbalanced classification.

## Dataset assembly

1. `tenders.parquet` ⋈ `awards.parquet` on `(procedure_id, lot_id)`.
2. Dedupe tenders first: keep the latest revision per key (or run the extractor with
   `--deduplicate`).
3. Features come from `tenders` columns ONLY. Everything in `awards` is post-outcome —
   using any of it (except the label) is leakage by construction.

## Features

Select mechanically by role (embedded in the parquet schema, see FIELDS.md):

- `numeric`, `bool`, `categorical` → use as-is.
- `hierarchical` → expand to prefix columns, then treat as categorical:
  `cpv_main` → `cpv3/cpv4/cpv6/cpv8` (full depth — see **CPV depth** below),
  `cpv_additional` → `cpv2/cpv3/cpv4` (shallow, and it must stay shallow — same
  section), `nuts1/nuts2/nuts3`, postal-zone first digit. The tree picks its
  own level per split — the standard way to learn hierarchical categories.
- `entity` (`buyer_name`) → **excluded in v1** (1,561 buyers, most with one lot —
  any encoding would memorize labels; see leakage rules 4-5). Returns in v2 as an
  award-date-aware historical rate.
- `key`, `label`, `plumbing`, `text` → never features. `text`/`nested` columns feed
  future engineered features (LLM pass), not the baseline.
- Missingness is often informative (`bid_bond_required` null ≠ False) — keep nulls as
  their own category, do not impute.

## Model

**CatBoostClassifier** (gradient-boosted trees; standard for tabular data at this
size). Why: raw string categoricals via `cat_features`, native nulls,
`auto_class_weights` for the 12% positive rate, usable probabilities.

```python
model = CatBoostClassifier(
    cat_features=CATEGORICAL_COLS,   # roles categorical + hierarchical prefixes; NO buyer_name
    one_hot_max_size=1024,           # leakage rule 4: must EXCEED max categorical cardinality
    auto_class_weights='Balanced',
    eval_metric='PRAUC',
    verbose=False,
)

# Leakage rule 4 guard: above one_hot_max_size CatBoost silently switches a column
# to CTR target statistics. Assert before every fit:
assert max(X[c].nunique() for c in CATEGORICAL_COLS) <= model.get_params()['one_hot_max_size']
```

`one_hot_max_size` must exceed the largest categorical cardinality — the engineered
columns are NOT small (first training run, 2026-07-31, cpv45 Jan–Jun extract:
`selection_criteria_types` joined-combo 650 categories, `cpv_additional__cpv4` 438,
`place_nuts3__nuts3` 336, `buyer_nuts__nuts3` 300, `cpv_additional__cpv3` 210).
1024 covers these; raise it (and rely on the assertion) if a new extract grows past it.

Current measurement (2026-08-06, 6,353 labeled lots): `selection_criteria_types` 890,
`cpv_additional__cpv4` 632, `place_nuts3__nuts3` 364, `buyer_nuts__nuts3` 334. The
guard passes with 890 of a permitted 1024 — but the headroom is thin, and it is
`cpv_additional` that will spend it first (see below). Regenerate with
`python cpv_depth_receipt.py`.

### List columns are multi-hot, never a combination string (2026-08-16)

The headroom ran out on the production server: on its full archive
`cpv_additional__cpv4` reached **1,767** combinations, the guard refused every
candidate, and the cycle delivered nothing (`doc/EXPERIMENTS.md` §1). The
combinations were the problem, never the values — `selection_criteria_types` is 32
criterion types in 2,102 combinations, `cpv_additional` 1,514 codes in 5,325.

So every **list**-typed column (`cpv_additional`, `selection_criteria_types`,
`exclusion_grounds`, `procurement_additional_types`, `funding_programs`,
`procedure_languages`, `change_reasons`, `quality_flags`) is now encoded
**multi-hot**: one numeric 0/1 column per value — per level for the hierarchical
`cpv_additional` (`cpv_additional__cpv4__has_4521`), flat for the rest
(`selection_criteria_types__has_slc-abil-facil-res`) — plus `…__n_rare` (values
outside the vocabulary) and `…__n` (distinct values, 0 for an empty list, so "no
additional codes" is a value of its own). No categorical column remains for them,
so rule 4 holds structurally, whatever the archive grows to.

The **vocabulary** — values present in ≥ `MULTIHOT_MIN_SUPPORT = 30` distinct lots —
is fitted by `single_bidder.fit_multihot` on the **full tenders frame** (the
`list_frame` every caller already passes), never on the labeled or the open subset
alone; no label is consulted. `loop.learn` stores it in the candidate's `meta.json`
(`multihot`) and `loop.predict_open` rebuilds the open lots' columns from the
champion's stored vocabulary, so a model scores with the columns it was trained on
weeks later, whatever codes have appeared since (they land in `n_rare`).

Measured on the laptop store (2026-08-16, 6,683 labeled lots of 24,023): 312
features, of which 239 multi-hot; largest remaining categorical
`place_nuts3__nuts3` 367 (523 for `cpv_main__cpv8` on all tenders) — scalar codes with
a bounded range. All four trust checks pass; tests in `tests/test_multihot.py`.

### Every categorical column multi-hot — the `multihot` build, and support as a share (2026-08-17)

The section above left the single-valued categoricals one-hot under
`ONE_HOT_MAX_SIZE = 1024`. That is a wall, and the operator's plan is a store
beyond construction: the CPV vocabulary is 9,456 codes — 47 divisions, 319
groups, **1,323 classes at cpv4**, and `cpv_main` is expanded to cpv8 — so an
all-trades store breaches the cap on `cpv_main__cpv4` alone and the guard
would refuse every candidate again. The cap is not on tenders, it is on
distinct values; more tenders of the same kinds never hit it, more kinds do.

**`feature_build='multihot'`** (`single_bidder.FEATURE_BUILDS`): every
categorical column — list or single-valued; `categorical`, `hierarchical`
(`cpv_main` at cpv3/4/6/8) and `bool` — is encoded as the list columns already
are: `<col>__[level__]has_<value>` for every value with support, `…__n_rare`,
`…__n`. **No CatBoost categorical feature is left**, so `assert_pure_one_hot`
has nothing to refuse and there is no cardinality wall anywhere. An open lot
with a main code the vocabulary never saw lands in `cpv_main__cpv8__n_rare`
and scores. The build is a knob (`single_bidder.FEATURE_BUILD`, on the queue,
grid `default` / `multihot`); the replay measures it under the lever and an
EXPERIMENTS.md arm confirms it forward before it becomes the default.

**Support is a share, with a floor.** `MULTIHOT_MIN_SUPPORT = 30` was an
absolute count — 0.15 % of today's ~20,000 lots, 0.0015 % of two million: it
would have decayed into a formality as the store grew (operator: "today is
good enough is a time bomb"). Now
`support = max(MULTIHOT_MIN_SUPPORT, ceil(MULTIHOT_MIN_SHARE × n_lots))`,
`MULTIHOT_MIN_SHARE = 0.0015` — the same 30 today, 3,000 at two million lots
— and the floor is the statistical minimum below which a column is noise at
any size; it stops mattering past ~20,000 lots. `fit_multihot` records
`min_support`, `min_share` and `n_lots` in the vocabulary (`meta.json`), so
every model states the number it actually used. The share is on the queue
(grid 0.0005 … 0.005), the floor is FROZEN.

## CPV depth (decision 2026-08-06)

`cpv_main` is expanded to its **full 8 digits**; `cpv_additional` stays at 4. The two
codes are treated differently on purpose.

The main code is a single string: at full depth it has **362** distinct values on the
labeled frame, comfortably under the 1024 cap. `cpv_additional` is a *list* column,
encoded as one joined combination string per level, so its cardinality grows with
depth — **632** categories at cpv4 but **1048** at cpv5, already over the cap, where
CatBoost silently switches the column to CTR target statistics. That is exactly what
leakage rule 4 forbids, so deepening the additional codes needs a different encoding
(per-code indicators, or a count), not a bigger cap.

What the old `cpv2/cpv3/cpv4` build cost:

- `cpv_main__cpv2` had **1** category under a CPV-45 scope — a constant column. The
  A/B arm that simply drops it reproduces the old model to ±0.0000.
- Digits 5-8 were discarded on **75.7%** of stored lots, and they name the actual
  trade: `45312310` is Blitzschutz, not merely `4531` electrical installation.
- Inside one cpv4 bucket the model could express exactly one rate, but the cpv6
  sub-buckets diverge: weighted-mean spread **13.2pt** against a 10.3% base rate, vs
  a permutation null (cpv6 shuffled within cpv4) of 6.8pt mean / 8.5pt p95 — 1.93x
  the noise floor, p ~ 0.000. `4523` alone spans 5.4%–31.6% under one 22.0% price.

A/B retrain, same features otherwise, temporal holdout, 3 seeds:

| arm | `cpv_main` levels | val PR-AUC | Δ |
|---|---|---|---|
| old | cpv2,3,4 | 0.2766 (sd .0032) | — |
| — | cpv3,4,5,6 | 0.2934 | +0.0169 |
| **shipped** | **cpv3,4,6,8** | **0.3071** (sd .0028) | **+0.0305** |
| — | cpv3,4 | 0.2766 | +0.0000 |

Robust across split dates (quantiles 0.70/0.75/0.80/0.85): +0.0269 / +0.0290 /
+0.0292 / +0.0217 — positive 4/4, mean **+0.0267 (+9.6% relative)**. Not a leak: CPV
is contract-notice metadata, the shuffled-label tripwire still collapses (0.1688 vs
base rate 0.1519) and ROC-AUC 0.654 stays under the too-good limit. `cpv_main__cpv6`
and `cpv_main__cpv8` come out **#1 and #2 of 83** features by importance.

Read the gain as **ranking**, not accuracy at the operating point: precision at
threshold 0.5 moves 0.285 → 0.289 and recall 0.220 → 0.211, i.e. flat. Delivery hands
each customer the top ≤`max_picks` flagged lots in their slice
([`SUBSCRIPTIONS.md`](SUBSCRIPTIONS.md)), so better ordering is what the product
consumes — but +9.6% PR-AUC is not a promise that a flagged lot is more often right.

Scope caveat: one store snapshot, CPV 45 / Germany. When the scope widens, `cpv2` and
`cpv3` stop being near-constant and every cardinality above must be re-checked
against `one_hot_max_size`.

### A feature change is a flag day, and the loop now says so

Changing the feature columns invalidates the champion: it cannot score the new build
at all (`predict_open` compares the column list against `model.feature_names_`).
Promotion used to require match-or-beating the champion's validation PR-AUC, which is
undefined across a schema change — and a kept-but-unusable champion would have
crashed the next cycle at the predict step. `learn()` therefore now detects a feature
schema differing from the champion's and **promotes the candidate unconditionally**
(trust checks still binding), naming the added and dropped columns in the gate.
`predict_open` no longer asserts: on a mismatch it prints the two column sets and
scores nothing this cycle, which the report and every subscription already express as
an outcome. Reachable only when a trust check blocked the candidate across a change.

For this decision that meant one forced promotion: champion `m2026-08-04-093924` (82
features) → +`cpv_main__cpv6`, +`cpv_main__cpv8`, −`cpv_main__cpv2` (83 features).

Rehearsed against a scratch copy of the registry before shipping (the real one was
not opened for writing): `learn()` recorded the schema change, promoted, and the new
champion could score the open lots. On the loop's OWN 8-week validation window — a
different, smaller, later window than the holdout table above, so the two sets of
numbers are not comparable — val PR-AUC went **0.6471 → 0.6690**. It would therefore
have promoted on merit as well; the unconditional rule was not load-bearing this time,
which is precisely why it should be in place before a change where it is.

## Evaluation

- **Temporal split**, not random: train on earlier `publication_date`, test on later.
  Random splits leak corrigenda siblings and buyer history across the boundary and
  match nothing about production use.
- Metrics: **PR-AUC** (primary), ROC-AUC (secondary), calibration curve. Accuracy is
  meaningless at 12% positives.
- Baselines to beat before claiming signal: (a) constant base rate, (b)
  single-feature `cpv4` rate.
- Report feature importance + per-group rates for sanity against the EDA.

## Leakage protocol

Enforceable rules, in order of application:

1. **Source firewall.** Features from `tenders.parquet` only. Immediately after the
   join, drop every awards column except `n_tenders` (label) and the join keys — a
   leak must then be re-added deliberately, not forgotten. Awards `quality_flags` may
   drop rows (`winner_but_zero_tenders`), never feed features.
2. **Mechanical selection.** Feature list is derived from role metadata (allowed:
   `categorical`, `hierarchical` as prefixes, `numeric`, `bool`, spans from `date`).
   `key`/`plumbing`/`text`/`nested`/`label` are excluded in code; unknown roles
   default to excluded.
3. **Every revision is a training row; the split is group-aware.** "Latest revision"
   is itself future information — whether more corrigenda will come is unknowable at
   prediction time, so training only on final states samples a population production
   cannot identify. Instead: every revision (original + each corrigendum) is one row,
   features as known at that publication (`n_corrections_so_far` is point-in-time by
   construction; run the extractor WITHOUT `--deduplicate`); all revisions of a lot
   share the lot's eventual label. Correction features (`n_corrections_so_far`,
   `is_corrigendum`, `change_reasons`) are legitimate — corrections so far plausibly
   signal bidder interest. Near-duplicate leakage is handled by the SPLIT: a lot is
   assigned wholly to train or test by its first `publication_date` (temporal,
   ~80/20); assert no `(procedure_id, lot_id)` straddles the boundary. Weight each
   row `1/k` (k = the lot's revision count), in training and evaluation, so a
   5-revision lot does not out-vote single-revision lots. Production matches exactly:
   whatever revision you hold, score it; re-score on each corrigendum.
4. **No target statistics in v1 — one-hot only.** Two ways to feed a category to a
   model: one-hot ("is this lot electrical? 1/0" — contains only the category,
   harmless) and target statistics (replace the category with its average outcome,
   e.g. "electrical: 13% single-bid" — the feature is made out of the answers, and
   computed carelessly a row's own label leaks into its own feature; a buyer with 2
   lots, 1 single-bid, gets "50%" written onto both rows). v1 uses one-hot only.
   CatBoost one-hots a column ONLY while its cardinality is ≤ `one_hot_max_size`;
   above that it silently switches the column to CTR target statistics — exactly
   what this rule forbids. The engineered columns are not small (combos reach 650
   categories; see the Model block), so `one_hot_max_size` must be set above the
   largest categorical cardinality (1024 currently) AND every training run must
   assert `max categorical cardinality <= one_hot_max_size` so the silent CTR
   fallback can never happen. `buyer_name` (1,561
   buyers, most with a single lot) cannot be one-hot — 1,561 columns would just
   memorize labels — so it is EXCLUDED from v1 features.
5. **Outcome-availability (governs v2's buyer history).** "This buyer's past
   single-bid rate" is the strongest known predictor and v2 should add it — but
   "past" must mean *outcome was public*, not *tender happened earlier*. A March
   tender may not use a January lot whose award was only published in May: in March
   nobody could know how January turned out. Aggregate only over lots whose AWARD
   `publication_date` precedes this row's `publication_date`. Tender-to-award gaps
   here are months, so this changes the numbers materially. No library default does
   this; it must be a hand-built expanding-window encoder — which is why it is v2,
   not half-done in v1.
6. **Tripwires — automatic tests for the cheating discipline missed.**
   - Shuffled-label run: scramble the labels and retrain; with nothing to learn, the
     score MUST collapse to the base rate. If it doesn't, the pipeline itself feeds
     answers in — hunt the bug.
   - Too-good alarm: literature tops out around ROC-AUC ~0.7 for call-time
     competition prediction. A near-perfect score means a leak until a specific
     feature is exonerated.
   - Single-feature audit: train on each feature alone; one feature scoring near the
     full model is suspicious — real signal here is many weak features.
   - Production dry-run: score today's still-open tenders (no award exists). Any
     feature that cannot be computed for them depended on the future.

## Known caveats

- ~700 labeled lots when this was written; 6,353 as of 2026-08-06 and growing —
  retrain as awards arrive (the loop does, every cycle).
- Corpus is homogeneous (German open-procedure construction works) — `procedure_type`
  and top-level CPV carry no signal; do not conclude they never would. Measured, and
  acted on: `cpv_main__cpv2` was literally constant and is gone (CPV depth above).
  *Deep* CPV is the opposite case — it is the single strongest feature group.
- `accelerated` is still a `"true"/"false"` string (open fix).
