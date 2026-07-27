# TenderMining — Estimation Methods

Method selection and detailed design for estimating a tender's **value** and
**competition** from its notice (free-text description + structured fields). Companion to
[`MODELING.md`](MODELING.md), which defines the data pipeline (tender/award join,
features, targets, leakage rules).

Running example used throughout: notice `516890-2026` — *"Neubau Kita Kuhweid und MGH
Weinheim, Metallbauarbeiten"*, description-lot listing quantities (48 m railing wall
plates, 43 m² stainless-steel netting, 136 m double handrails, 4 exterior doors), CPV
`45262670`, region `DE128`, open procedure, actual award **€196,069.17**, **4 bids**.

## Decision record

| Method | Decision | Reason |
| --- | --- | --- |
| A — Retrieval ("price by analogy") | **selected** | explainable, no training, robust to new vocabulary |
| B — Embedding + regressor | **selected** | learns global patterns, the classic supervised approach |
| C — LLM as estimator | **rejected** | result not controllable (non-reproducible, hallucination risk) |
| D — Competition classifier | **selected** | most reliably learnable target; no price estimation involved |
| Final estimator | **learnable combination of A + B + D** | stacking meta-model, see §6 |

---

## 1. Shared foundation (all methods build on this)

### 1.1 Data

Matched tender→award pairs joined on `procedure-identifier`: features come from the
tender (`cn-standard`, known at call time), labels from the award (`can-standard`).
See MODELING.md §2–§5, including the leakage rule (award-side fields are never inputs).

### 1.2 Text representation: sentence embeddings

Every description is converted **once** into a fixed-length dense vector (e.g. 768
floats) by a pretrained sentence encoder. Properties that matter here:

- **Fixed width forever.** A steelwork description and a bridge description produce
  vectors of identical shape. There is no growing set of keys/columns — the
  open-vocabulary problem ("every new term is a new unknown feature") does not exist in
  this representation.
- **Meaning, not word identity.** Synonyms (`Heizung`/`Heizungsanlage`) land close
  together; typos (`Gelnder` for `Geländer`) land near the correct word because encoders
  tokenize into subwords; genuinely new terms (`Wasserstoffelektrolyseur`) are still
  representable because the encoder was pretrained on general German.
- **No rarity prior.** Unlike TF-IDF, nothing assumes "rare = important", which was the
  objection that ruled TF-IDF out as the primary representation.

Model candidates (pretrained, downloadable, no fine-tuning required to start):

- [aari1995/German_Semantic_V3](https://huggingface.co/aari1995/German_Semantic_V3) —
  gbert-large backbone, German-specific.
- [T-Systems-onsite/german-roberta-sentence-transformer-v2](https://huggingface.co/T-Systems-onsite/german-roberta-sentence-transformer-v2)
  — German/multilingual sentence transformer.
- A multilingual encoder if non-German countries are added later.

Embedding text = `title-proc + ". " + description-proc + ". " + description-lot`
(the lot description often carries the quantity detail — in the running example it holds
the entire bill-of-quantities summary).

### 1.3 Structured features

CPV group (first 2–4 digits, categorical), NUTS region, procedure-type, contract-nature,
GPA flag, framework flag, buyer country + buyer identity, duration (days, when present),
estimated value (when present; log-scaled), missing-value indicator flags.

### 1.4 Evaluation protocol (applies to every method)

- **Time-based split only:** train on older procedures, test on newer ones. Never a
  random shuffle across time — the real task is "predict at call time, verify later".
- **Value metrics:** MAE on `log(value)` (readable as "typical relative error"), plus
  median absolute percentage error, plus a coarse **band accuracy** (order-of-magnitude
  bucket hit rate: <50k / 50–200k / 200k–1M / 1–5M / >5M €) — this matches the honest
  goal of magnitude estimation.
- **Competition metrics:** for single-bidder: precision/recall and ROC-AUC; for bid
  count: MAE and class accuracy on binned counts (1 / 2–3 / 4–6 / 7+).
- **Baseline to beat:** predict the median value of the tender's CPV division (a
  no-ML lookup table). Any method that cannot beat this is not adding value.

---

## 2. Method A — Retrieval / "price by analogy" (k-NN over embeddings)

### 2.1 Idea

Do not predict — **look up**. A new tender is priced from the most similar *awarded*
tenders in history. This is the algorithmic version of what a human estimator does with
reference projects, and it is the pattern used in the published Italian
public-administration case study (BERT embeddings of contract descriptions to compare
awarded amounts — [ScienceDirect](https://www.sciencedirect.com/science/article/pii/S0267364923000973)).

### 2.2 Pipeline in detail

**Index build (offline, repeatable):**

1. For every historical awarded tender: embed its text (§1.2) → vector `v`.
2. Store `(v, award_value_eur, publication-number, cpv, nuts, buyer, date)` in a vector
   index. At our scale (10⁴–10⁶ rows) exact cosine search in numpy/FAISS is enough;
   no approximate-NN infrastructure needed.

**Query (per new tender):**

3. Embed the new tender's text → `q`.
4. Optional hard pre-filter: same CPV division and/or same country (keeps neighbours
   thematically legal; avoids "a cheap IT contract is textually similar to an expensive
   one" cross-domain accidents).
5. Retrieve top-k by cosine similarity (k ≈ 5–20; tune on the validation window).
6. **Estimate** = similarity-weighted median of the neighbours' values. Median, not
   mean — awards are heavy-tailed and a single outlier neighbour must not drag the
   estimate.
7. Report the neighbours themselves: publication numbers, titles, values, similarities.

**Running example:** the Kita-Metallbau query would retrieve past *Metallbau/Geländer/
Treppen* awards; ten neighbours in the €120k–€310k range with high similarity produce
an estimate near €200k **plus the list of those ten projects** as justification.
(Actual award: €196k.)

### 2.3 Confidence signal (a first-class output)

- `A_similarity` = mean cosine of the k neighbours. High (≈0.7+) ⇒ dense comparable
  history. Low ⇒ "no comparable projects known" — the honest answer in sparse niches,
  and a signal the ensemble (§6) uses to down-weight A.
- `A_spread` = interquartile range of neighbour values relative to the median. Tight
  spread ⇒ the niche has a going rate; wide spread ⇒ text similarity does not pin down
  price here (e.g. "Sanierung Schule" can be €80k or €8M depending on scope).

### 2.4 Properties

| | |
| --- | --- |
| Training | none — the "model" is the index; it improves by ingesting more history |
| Explainability | maximal: the estimate *is* a list of reference projects |
| New vocabulary | graceful — nearest neighbours always exist; low similarity exposes weak coverage |
| Failure modes | sparse niches; scope not expressed in text (identical text, different size); temporal drift (old neighbours at outdated price levels — mitigate: recency weighting or an inflation index on neighbour values) |
| Cost | embedding once per notice; queries are milliseconds on CPU |

---

## 3. Method B — Embedding + regressor (trained value model)

### 3.1 Idea

Classic supervised learning: fit a function from `(text embedding ⊕ structured
features)` to `log(award value)`. Precedent: the published award-price estimator on
Spanish open data 2012–2018 using Random-Forest regression
([IntechOpen](https://www.intechopen.com/chapters/1193009)).

### 3.2 Pipeline in detail

**Feature matrix (one row per matched pair):**

```
[ 768 embedding dims | cpv_division one-hot | nuts1 one-hot | procedure one-hot |
  nature one-hot | gpa | framework | duration_days | duration_missing |
  est_value_log | est_value_missing | buyer_country ]        →  y = log10(total_value_eur)
```

- **Log target, mandatory.** Values span €10⁴–€2.5·10⁸ in our own samples. On the raw
  scale a model minimises error on the giants and ignores everything below €1M; on the
  log scale errors are relative ("off by a factor of 1.3"), which is the meaningful notion
  of accuracy here.
- **Currency normalisation** to EUR before the log (PLN etc. via ECB reference rates at
  publication date).
- **Outlier policy:** winsorise the target at the 1st/99th percentile of the training
  window; framework-ceiling artefacts (€36bn) otherwise poison the fit.
- **Missingness flags** instead of silent imputation: `duration_missing`,
  `est_value_missing` are themselves informative (small below-threshold-style notices
  omit more).

**Model:** gradient-boosted trees (LightGBM/XGBoost) as the default — strong on mixed
dense-embedding + categorical data, trains in minutes on CPU at 10⁴–10⁵ rows, no GPU.
Random Forest as the simpler cross-check.

**Uncertainty:** train three quantile models (q10, q50, q90) → output is an interval,
"likely €150k–€400k", not a fake-precise point. This matches the magnitude-not-precision
reality established in MODELING.md §9.

**Hyperparameters:** tuned on a validation window that is *later* than the training
window (never random CV across time).

### 3.3 What B learns that A cannot

Global, additive patterns that no single neighbourhood shows: regional price levels
("NUTS DE2 +x%"), procedure effects ("negotiated trends higher for equal scope"), buyer
effects, CPV base rates, and interactions between text topic and region. B interpolates
smoothly where A's neighbour set is thin.

### 3.4 Properties

| | |
| --- | --- |
| Training | yes — light (minutes, CPU); retrain on a schedule (e.g. monthly window roll) |
| Explainability | limited: global feature importances / SHAP, but no reference projects |
| Failure modes | silent degradation out-of-distribution (a project type never seen); over-reliance on `est_value` when present (it is almost the answer — report performance with and without it) |
| Output | `B_estimate` (q50) + `B_interval` (q10–q90) |

---

## 4. Method D — Competition classifier (no price involved)

### 4.1 Idea

Predict the *competition* a tender will attract. Two targets from the award side:

- `single_bidder` — binary: exactly one bid received?
- `n_bids` — the count, treated as ordinal classes `1 / 2–3 / 4–6 / 7+` (regression on
  small counts is noisier than binned classification).

Published support: competition-driver analysis on TED 2017–2020
([Springer](https://link.springer.com/article/10.1007/s11115-023-00742-0)) and
single-bidder risk modelling ([arXiv](https://arxiv.org/pdf/2102.05523)). Our own
sample showed 27% single-bidder awards — a strong, learnable base rate.

### 4.2 Pipeline in detail

Same feature matrix as B (§3.2), classification heads instead of regression:

- `single_bidder`: gradient-boosted classifier, class-weighted (≈1:3 imbalance),
  calibrated probabilities (isotonic/Platt on the validation window) so
  `p_single_bidder = 0.4` is trustworthy as a probability.
- `n_bids` bins: one multiclass model, or ordinal-aware (cumulative binary) if bin
  confusion matters.

The drivers live mostly in the **structured** features (procedure type, CPV niche,
region, GPA, buyer). The text embedding is included but expected to contribute less
than for value — a useful diagnostic in itself.

### 4.3 Why D is in the final system although it does not estimate price

1. **Standalone value:** "expected ≤2 bidders" is directly actionable for a bidder
   (weak competition = opportunity, and for buyers a procurement-quality warning).
2. **Reliability:** the most consistently learnable procurement target in the
   literature — D will work even where value estimation stays weak.
3. **Price coupling:** competition correlates with price outcomes (fewer bidders → less
   price pressure). D's outputs are therefore *inputs* to the final combination (§6).

### 4.4 Properties

| | |
| --- | --- |
| Training | yes — light, CPU |
| Explainability | good: SHAP on mostly-categorical drivers reads naturally ("negotiated procedure + niche CPV + short deadline ⇒ high single-bid risk") |
| Failure modes | class imbalance (handled by weighting + calibration); drift in procurement law changing base rates |
| Output | `D_p_single_bidder` (calibrated), `D_expected_bids` (class probabilities → expectation) |

---

## 5. Rejected: Method C — LLM as estimator

Prompting a large language model with the description to output a value (benchmarked in
[MDPI, LLM conceptual cost estimation](https://www.mdpi.com/2075-5309/16/2/396)).
**Rejected because the result is not controllable:** non-reproducible across runs,
hallucination risk on thin descriptions, per-call cost at scale, and no way to improve
it on our own data short of fine-tuning. A possible future *offline* role — parsing
descriptions into structured quantity tables (48 m, 43 m², 4 doors → features for A/B)
— is noted but out of scope.

---

## 6. Combining the models: a learnable ensemble (stacking)

The final value estimate is **not** a hand-picked winner among A and B but a **learned
combination** of everything the three selected models emit. Standard name: *stacking* —
a small meta-model learns how much to trust which base model in which situation.

```
                        ┌──────────────┐
  description ─ embed ─►│ A: retrieval  │─ A_estimate, A_similarity, A_spread ─┐
                        └──────────────┘                                       │
                        ┌──────────────┐                                       ▼
  embedding ⊕ struct ──►│ B: regressor  │─ B_estimate, B_interval ────►  meta-model ──► final value
                        └──────────────┘                                       ▲          (+ interval)
                        ┌──────────────┐                                       │
  embedding ⊕ struct ──►│ D: classifier │─ p_single_bidder, E[bids] ──────────┘
                        └──────────────┘
```

### 6.1 Meta-model design

- **Inputs (~8–12 numbers, no raw text):** `A_estimate`, `A_similarity`, `A_spread`,
  `B_estimate`, `B_interval_width`, `D_p_single_bidder`, `D_expected_bids`, plus minimal
  context (CPV division, `est_value_missing`).
- **Model:** deliberately small — ridge regression or a depth-≤3 tree. With ~10 inputs a
  big meta-model would only overfit. Interpretable by design: one can read off rules like
  *"high A_similarity ⇒ weight A ~70%; sparse niches ⇒ lean on B; high single-bid risk ⇒
  shift the estimate up"*.
- **D enters as an input, not a target:** expected competition is information *about*
  price, so the meta-model may use it to adjust the value estimate.

### 6.2 Leakage-safe training windows

Three disjoint time windows, strictly ordered:

1. **W1 (oldest):** train base models A-index, B, D.
2. **W2:** run the W1-trained bases on W2 tenders → their *out-of-sample* outputs become
   the meta-model's training rows (meta target = W2 actual award values).
3. **W3 (newest):** final evaluation of the full stack.

Base and meta are never fit on the same rows — otherwise the meta-model learns the
bases' training error and collapses onto whichever base memorised best.

### 6.3 System output per tender

- final value estimate + interval (from the meta-model),
- expected bidders + single-bid probability (from D, passed through),
- A's reference projects as the human-readable justification,
- a confidence note driven by `A_similarity` ("dense comparable history" vs "novel
  territory — wide uncertainty").

---

## 7. Expectations (honest)

- **Value:** magnitude/band accuracy, not surveyor precision — the notice text bounds
  what is knowable (scope details live in tender documents, not the notice; see the
  scope-vs-metadata discussion in MODELING.md §9). Success = beating the CPV-median
  baseline clearly and hitting the right value band most of the time.
- **Competition (D):** the strongest published track record; treat it as the flagship
  deliverable.
- **Ensemble:** typically a modest but consistent win over the best single model; its
  real value is robustness (A and B fail in different situations) plus keeping A's
  reference projects attached to every estimate.

## 8. Build order

1. **Foundation** — matched tender→award pairs + embedding pipeline (shared by all).
2. **A** (no training) — a working, explainable estimator immediately; also validates
   the embedding quality by eyeballing neighbours.
3. **D** — the reliable learnable target; validates the feature matrix and evaluation
   harness.
4. **B** — the supervised value model; compare against A and the CPV-median baseline on
   the time split.
5. **Stacking meta-model** over A+B+D outputs (§6), evaluated on the newest window.
