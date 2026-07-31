# TRAINING — single-bidder classifier

Status: agreed approach, not yet implemented. Complements [`MODELING.md`](MODELING.md)
(older, broader price+bidders sketch); this document is the concrete recipe for the
first model. Field roles: [`FIELDS.md`](FIELDS.md). EDA:
[`eda_single_bidder.ipynb`](eda_single_bidder.ipynb).

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
  `cpv2/cpv3/cpv4`, `nuts1/nuts2/nuts3`, postal-zone first digit. The tree picks its
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

- ~700 labeled lots today and growing; retrain as awards arrive.
- Corpus is homogeneous (German open-procedure construction works) — `procedure_type`
  and top-level CPV carry no signal; do not conclude they never would.
- `accelerated` is still a `"true"/"false"` string (open fix).
