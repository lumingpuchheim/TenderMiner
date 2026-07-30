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
- `entity` (`buyer_name`) → pass as a raw categorical to CatBoost (its ordered target
  statistics do leakage-safe target encoding); later upgrade: out-of-fold historical
  single-bid rate per buyer.
- `key`, `label`, `plumbing`, `text` → never features. `text`/`nested` columns feed
  future engineered features (LLM pass), not the baseline.
- Missingness is often informative (`bid_bond_required` null ≠ False) — keep nulls as
  their own category, do not impute.

## Model

**CatBoostClassifier** (gradient-boosted trees; standard for tabular data at this
size). Why: raw string categoricals via `cat_features` (no one-hot at any
cardinality), built-in leakage-safe target encoding, native nulls,
`auto_class_weights` for the 12% positive rate, usable probabilities.

```python
model = CatBoostClassifier(
    cat_features=CATEGORICAL_COLS,       # roles categorical + hierarchical + entity
    auto_class_weights='Balanced',
    eval_metric='PRAUC',
    verbose=False,
)
```

## Evaluation

- **Temporal split**, not random: train on earlier `publication_date`, test on later.
  Random splits leak corrigenda siblings and buyer history across the boundary and
  match nothing about production use.
- Metrics: **PR-AUC** (primary), ROC-AUC (secondary), calibration curve. Accuracy is
  meaningless at 12% positives.
- Baselines to beat before claiming signal: (a) constant base rate, (b)
  single-feature `cpv4` rate.
- Report feature importance + per-group rates for sanity against the EDA.

## Known caveats

- ~700 labeled lots today and growing; retrain as awards arrive.
- Corpus is homogeneous (German open-procedure construction works) — `procedure_type`
  and top-level CPV carry no signal; do not conclude they never would.
- `accelerated` is still a `"true"/"false"` string (open fix).
