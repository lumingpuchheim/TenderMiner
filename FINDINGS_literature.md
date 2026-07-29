# Literature findings — features for the two estimation tasks

What the published, peer-reviewed literature says about the two core tasks and the
features they require. Sources verified against publisher pages on 2026-07-29; full
citations also in [`METHODS.md`](METHODS.md) §5a.5 (cost) and §4.5 (competition).

## Task 1 — Estimating total construction cost

Published error levels: **MAPE 19.3 %** with regression on 286 UK projects
(Lowe et al. 2006), **MAPE 16.6 %** with an ANN on ~300 projects (Emsley et al.
2002); systematic reviews report 3–18 % depending on model class — vs **~25 %**
for traditional manual estimating.

| Feature | Role in the estimate | Source |
| --- | --- | --- |
| Gross (internal) floor area | The dominant cost driver — appeared in all six of Lowe et al.'s regression models; confirmed as one of the two most important parameters across later ANN studies | [Lowe, Emsley & Harding 2006](https://ascelibrary.org/doi/abs/10.1061/(ASCE)0733-9364(2006)132:7(750)); [Ahn et al. 2023](https://www.tandfonline.com/doi/full/10.1080/13467581.2023.2294883) |
| Function / building type (school, office, residential…) | Sets the cost class per m²; a key linear driver in all six models | [Lowe et al. 2006](https://ascelibrary.org/doi/abs/10.1061/(ASCE)0733-9364(2006)132:7(750)) |
| Number of storeys | Second most important parameter after floor area in early-stage ANN models | [Ahn et al. 2023](https://www.tandfonline.com/doi/full/10.1080/13467581.2023.2294883) |
| Location / region | Included as a categorical variable; regional factor noted repeatedly as an important cost factor across the reviewed studies | [Tayefeh Hashemi et al. 2020 review](https://link.springer.com/article/10.1007/s42452-020-03497-1) |
| Project duration | Key driver in all six Lowe et al. models | [Lowe et al. 2006](https://ascelibrary.org/doi/abs/10.1061/(ASCE)0733-9364(2006)132:7(750)) |
| Mechanical installations (extent of M&E work) | One of the five recurring drivers | [Lowe et al. 2006](https://ascelibrary.org/doi/abs/10.1061/(ASCE)0733-9364(2006)132:7(750)) |
| Piling / foundation conditions | One of the five recurring drivers | [Lowe et al. 2006](https://ascelibrary.org/doi/abs/10.1061/(ASCE)0733-9364(2006)132:7(750)) |
| Structural material & construction type | Input to the ANN model reaching MAPE 0.6 % on structural elements | [ANN building-elements study](https://link.springer.com/chapter/10.1007/978-981-97-5910-1_34) |
| Building height, occupancy type | Inputs to high-accuracy ANN models | [ANN building-elements study](https://link.springer.com/chapter/10.1007/978-981-97-5910-1_34) |
| Year / price indices (inflation, cost index) | Economic normalisation over time; "ML models considering economic variables and indexes" | [Tayefeh Hashemi et al. 2020 review](https://link.springer.com/article/10.1007/s42452-020-03497-1) |

## Task 2 — Estimating whether a tender will have (almost) no competition

Headline results (corrected 2026-07-29 after reading the primary papers — an
earlier version of this section conflated ex-post cartel screening with call-time
forecasting):

- **The boundary every prediction paper uses is 1 bid vs ≥2 bids** ("single
  bidding") — no study validates any other cut (e.g. "3 vs 8"). It is also the
  EU's official competition indicator: the Single Market Scoreboard's
  triple-weighted single-bidder share, red-flagged above **20 %**
  ([Scoreboard](https://single-market-scoreboard.ec.europa.eu/business-framework-conditions/public-procurement_en)).
- The **exact** bidder count is barely predictable — best model reaches **R²
  0.168** (XGBoost, 913 Singapore projects, Oo et al. 2025); count regression on
  TED text lands at **MAE ≈ 1.6 bids** (Acikalin et al. 2023).
- **Call-time (ex-ante) single-bid prediction — the numbers that transfer to us:**
  on **333,832 TED notices** (2011–2018, 21 % single-offer), predicting
  single-offer from the call description reaches **precision ≈ 0.61 at recall
  ≈ 0.34** ([Acikalin, Gorgun, Kutlu & Tas 2023, *Nat. Lang. Eng.*](https://doi.org/10.1017/S135132492300030X)).
  On 15,800 Croatian tenders (26 % single-bid; target verbatim "one bid = true,
  more than one = false"), tender-text models reach **accuracy 0.59–0.85 by CPV
  sector** with often-poor single-bid recall
  ([Rabuzin & Modrušan 2019](https://www.scitepress.org/Papers/2019/83536/83536.pdf)).
  Useful (≈3–5× enrichment over the base rate), but far from solved.
- The oft-quoted **70–84 % accuracy** (Fazekas et al. 2026) is **ex-post cartel
  detection** — classifying contracts as cartel vs non-cartel from *award-side*
  bidding/pricing patterns (73 confirmed cartels). It is not a call-time
  competition forecast and must not be used as a benchmark for one. Likewise
  Goryunova et al. 2021 ([arXiv:2102.05523](https://arxiv.org/abs/2102.05523))
  classify *already-known* single-bidder auctions into "probably fair" vs
  "suspicious".

So "is this tender a good one (no competition)?" is supported as a probability
flag at the 1-vs-≥2 boundary, with realistic ex-ante performance around precision
0.6 — not as a bidder count, and not at 70–84 % accuracy.

| Feature | Effect on number of bidders | Source |
| --- | --- | --- |
| Procedure type (negotiated vs open) | Negotiated procedures attract 2–5 more bids | [Tátrai, Vörösmarty & Juhász 2023](https://link.springer.com/article/10.1007/s11115-023-00742-0) |
| Award criterion (lowest price vs MEAT) | Lowest-price awards generate significantly more bids | [Tátrai et al. 2023](https://link.springer.com/article/10.1007/s11115-023-00742-0) |
| Lot division | Fewer lots → more bidders; over-fragmentation reduces participation | [Tátrai et al. 2023](https://link.springer.com/article/10.1007/s11115-023-00742-0) |
| Contract duration | Longer contracts attract substantially more bidders | [Tátrai et al. 2023](https://link.springer.com/article/10.1007/s11115-023-00742-0) |
| Funding source (local vs EU-funded) | Locally financed contracts receive more offers | [Tátrai et al. 2023](https://link.springer.com/article/10.1007/s11115-023-00742-0) |
| Contract type (works vs supplies) | Construction works attract more bidders than goods | [Tátrai et al. 2023](https://link.springer.com/article/10.1007/s11115-023-00742-0) |
| Authority type (local vs central) | Regional/local authorities receive more bids than ministries | [Tátrai et al. 2023](https://link.springer.com/article/10.1007/s11115-023-00742-0) |
| Project size / work nature / workhead (trade) type | Project-characteristic inputs to the bidder-count models | [Oo, Nguyen, Ahn & Lim 2025](https://www.emerald.com/insight/content/doi/10.1108/ci-10-2024-0325/full/html) |
| Tender price index, construction demand | "Economic-related factors play a vital role in this prediction problem" | [Oo et al. 2025](https://www.emerald.com/insight/content/doi/10.1108/ci-10-2024-0325/full/html) |
| Bidding-pattern & pricing screens (bid spread, repeated co-bidding, price irregularities) | Distinguish collusive/no-competition behaviour from healthy markets at 70–84 % accuracy — **ex-post screens on award-side data; not usable as call-time features** | [Fazekas, Tóth, Wachs & Abdou 2026](https://www.sciencedirect.com/science/article/pii/S0167718725000943) |

## The asymmetry between the tasks

Cost estimation needs **physical** project features (floor area, storeys,
materials) that usually require extraction from project documents. Competition
prediction runs mostly on **administrative** features (procedure, criterion, lots,
buyer type) plus market indicators — data that is structured and complete in
procurement notices from the start. This is why competition prediction is the more
reliably learnable target on notice data alone, while literature-level cost
accuracy depends on a feature-extraction step (METHODS.md §5a).
