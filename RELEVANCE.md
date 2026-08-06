# RELEVANCE — recommend only the customer's business

Status: phases 1 (embedding sidecar — `embed.py`, wired into the loop),
2 (calibration + trusted codes — `calibrate.py`, receipts committed per
`model_tag`) and 3 (gate in the renderer — `relevance.py`, wired into
`deliver`; pilot subscription live) implemented; phase 4 specification.
Default embedding model: `jina-v2-base-de` (flipped 2026-08-05 after a
full-store A/B; shipping gate is configuration E — plain references +
code channel, expansion off). Known open weaknesses, both diagnosed on
the pilot 2026-08-05 (the Rettungswache replay): (a) the **soft
fingerprint can acquire object labels** — a building label entered via
one reference text and passed two Generalunternehmer lots label-to-label
(the Polderwand mechanism, second occurrence); (b) **project vocabulary
outweighs trade vocabulary** in tender prose, inflating cross-buyer text
similarity and polluting projections. Weakness (a) is closed: phase 5
(projection corroboration, below) shipped 2026-08-05 as configuration H
(receipt: leakage 2.1% → 1.5%, recall 58.4% → 60.3%; delivery-row
`trade_read` stamps pending a loop.py touch). Weakness (b) was attacked by
phase 6 (sentence-level template stripping): built and measured
2026-08-06, mechanism validated on the raw text channel, but its receipt
lost to configuration H at the shipping operating point (1.9% vs 1.5%
leakage) — not shipped, re-measured at the next model flip. Weakness (c),
found by the operator 2026-08-06 in the first replay demo: **a wrong code
pointing toward the customer's trade** (the Trafostation case) passes the
hard channel unopposed and is invisible to the leakage metric by
construction; closed by phase 7 (trade-talk contradiction, below), shipped
2026-08-06 as configuration K: margin 0.225, recall 60.3% → 60.0%, 5.9%
of hard-code admissions contested, both known wrong picks demoted. Phase
4 feedback stays the backstop throughout.
Builds on the running loop
([`ONLINE_LEARNING.md`](ONLINE_LEARNING.md)) and the subscription layer
([`SUBSCRIPTIONS.md`](SUBSCRIPTIONS.md)); uses their vocabulary
(**component** = a box that always runs; **phase** = build order in time,
never a scope cut). Nothing here changes the loop's spine — download, grade,
learn, predict stay exactly as they are; this document adds the layer that
decides **whether a lot is the customer's business at all**, before the
competition model decides whether it is worth bidding on.

## The problem, in one paragraph

A subscription today is a CPV-prefix filter, and CPV codes are filled in by
whoever publishes the notice. The hierarchy is sound; the usage is not: a
lightning-protection lot appears as `45312310` (Blitzschutz) when a
professional Vergabestelle codes it, as `45210000` (Hochbau) when the clerk
codes the *project*, and as `45000000` (Bauarbeiten) when nobody cares.
Measured on the current store, roughly two thirds of live
lightning-protection tenders are findable only through their text, not their
code. A deep CPV prefix therefore has both failure modes at once: it
**misses** the customer's business (miscoded relevant lots) and it **pollutes**
the report (correctly-prefixed lots that are not the customer's trade). The
fix is to stop asking the taxonomy what the customer does.

## Design principle: a customer is defined by tenders, not by codes

The customer's business is expressed as a set of **reference tenders** — in
the normal case, tenders they have already won, which the awards store
already contains — plus optionally a line of free text from onboarding. A
candidate lot is relevant if its text is close to any reference. No trade
names, no CPV round-trip, no dictionary: tender-to-tender comparison, same
document type, same vocabulary, same clerk-written German on both sides.

CPV is demoted to the job it does reliably: a **coarse recall guard**
(division level, `45`), where miscoding is rare. The two stages compose:

```
subscription filter (cheap, coarse, high recall)   CPV division × NUTS × deadline
        │
        ▼
relevance gate (this document, high precision)     embedding similarity vs. profile
        │
        ▼
competition ranking (unchanged)                    the single-bidder model
```

Relevance decides *eligibility* ("is this my business?"); the existing model
decides *ranking* ("is it lonely?"). The axes are orthogonal and stay
separate — a relevance score must never leak into the competition model's
features, and vice versa.

## Worked example (used throughout)

> **Jebsen GmbH** (Hamburg, lightning protection & earthing) has six wins in
> the awards store: three SBH Schulbau Hamburg lots, HafenCity, Sprinkenhof,
> and one Gemeinde Kölln-Reisiek lot — the last coded `45000000`, findable
> only by its title. Their profile is those six notices. A new notice
> *"Blitzschutz- und Erdungsanlagen — 3. Bauabschnitt Neubau Grundschule"*
> (Gemeinde Schönkirchen) scores high against the Kölln-Reisiek reference
> regardless of how it was coded; a fire-alarm lot correctly coded `453121`
> scores low against all six and is dropped, even though a `4531` prefix
> would have passed it.

## The embedding sidecar

One vector per stored lot, computed once, cached forever — the store grows
append-only, so embedding is an incremental step of the extract phase, not a
per-run cost.

- **Text**: `title + "\n" + description`, truncated to the model's window
  (~512 tokens). The trade-defining vocabulary ("Ringerder,
  Fangeinrichtungen, Ableitungen") appears early; truncation is safe. Both
  fields are 100% populated in the current store (median 48 / 401 chars).
- **Model**: a small multilingual sentence-embedding model
  (`multilingual-e5-small` class, 384 dims) via ONNX/quantized runtime. No
  GPU. Measured scale: backfilling the full store (~28k lots) is a
  ~15–30 minute one-time job on the operator laptop (i5-8250U); the daily
  increment is under a minute. Query-time scoring is numpy dot products —
  milliseconds per subscriber per cycle.
- **Storage**: `data/embeddings/<model_tag>/lots.npy` + a parallel
  `lots_index.jsonl` (procedure_id, lot_id, notice_id, text_hash). The
  `model_tag` names the embedding model and version; switching models means
  a new directory and a full re-embed, never an in-place mutation — the same
  append-only discipline as every ledger.
- **Encoding check (verified 2026-08-04)**: the store is clean UTF-8
  end-to-end — zero replacement characters across all 28,148
  titles/descriptions; console mojibake was a display artifact. One
  description (notice 00444708-2026) carries mojibake *from the official
  source itself* (the raw XML contains a literal `Ã¼ber` — the buyer pasted
  corrupted text; verified against `data/raw/xml/444708-2026.xml`). Rule:
  the store preserves the source byte-for-byte; embedding text prep runs
  `ftfy.fix_text()` to repair upstream mojibake before vectorizing. The
  archive stays the official record; only the derived vectors see the
  cleaned text.

## The profile — extending the subscription object

New optional fields on the existing versioned subscription line
(`data/subscriptions.jsonl`; format rules from SUBSCRIPTIONS.md carry over —
versioned, never edited, filters AND, lists OR):

```jsonl
{"sub_id": "jebsen-blitzschutz", "version": 2, "effective_from": "2026-08-15",
 "name": "Jebsen GmbH",
 "cpv_prefixes": ["45"], "nuts_prefixes": ["DE6", "DEF"],
 "profile_refs": ["00123456-2025", "00234567-2025", "..."],
 "profile_texts": ["Blitzschutz- und Erdungsanlagen, Fangeinrichtungen, Ableitungen"],
 "min_relevance": 0.62,
 "min_deadline_days": 14, "max_picks": 5, "avoid_n": 5, "active": true}
```

- **`profile_refs`** — publication numbers of reference tenders (normally
  the customer's wins, straight from the awards store). The renderer resolves
  them to embeddings via the sidecar index. A ref that cannot be resolved is
  a rendering error, not a silent skip.
- **`profile_texts`** — free-text lines, embedded with the same model into
  the same space. This is the cold-start path for a customer with no award
  history ("describe your business in one sentence"), and the escape hatch
  for a business line the customer has not yet won publicly.
- **`min_relevance`** — the gate threshold for this subscription (see
  calibration below). Omitted = gate disabled; the subscription behaves
  exactly as today. This makes the feature adoptable per customer with zero
  migration.
- **Note the CPV prefix widened** from `453123` to `45`: with a relevance
  gate, the prefix's job flips from "select the trade" to "keep recall".
  Widening is a new subscription version like any other filter change.

## The relevance score

For candidate lot *c* with embedding **e**(c) and profile references
r₁…rₙ:

```
relevance(c) = max_i  cosine( e(c), e(rᵢ) )
```

**Max, not centroid** (decision): repeat winners measurably span 2–3 CPV
classes — a firm doing Blitzschutz *and* fire alarms has two clusters, and
their average is a midpoint matching neither. Max against each reference
keeps every business line sharp; adding a reference can only widen what the
customer sees, never dilute it. n is small (single digits for years), so max
costs nothing.

The gate: a lot enters the customer's slice iff
`relevance(c) ≥ min_relevance` **or** it auto-passes on a trusted code
(next section). Below the gate a lot is invisible to picks, warnings, *and*
the annex — with one exception, the borderline band (below).

## Trusted codes — CPV earns its way back in (decision 2026-08-04)

Deep CPV codes are sometimes precise and sometimes nonsense, and no rule of
thumb separates them (building-type vs. trade branches was tested and
refuted — both average the same). So no code is *assumed* meaningful; every
code **proves itself on data**:

- **Cohesion**: for every deep code (digits 5–8 carry information) with
  ≥ `trust_min_lots` lots in the store, measure the mean pairwise cosine of
  its lots' embeddings. A code filled honestly names one trade, and its
  tenders read alike (measured range in the current store: 0.32–0.73 against
  a 0.404 random baseline — some deep codes are *less* coherent than random).
- **Trusted** = cohesion ≥ `trust_cohesion_min`. The trusted-code list is a
  committed artifact next to the calibration receipt, recomputed per
  `model_tag` and as the store grows. An untrusted deep code is treated
  exactly like `45000000`: ignored, the lot is judged by its text alone.

Trusted codes are used in three places, always under one asymmetry — **a
code can add evidence, but no code can ever veto the text**:

1. **Auto-pass**: a candidate deep-coded with a trusted code that also
   appears (trusted) among the profile references passes the gate outright,
   regardless of embedding score.
2. **Profile expansion**: other lots under a trusted code from the
   customer's wins become **pseudo-references**, widening a handful of wins
   into the trade's full textual variety. Guard at the lot level: a lot
   whose mean similarity to its code siblings falls clearly below the
   code's cohesion — past the halfway point toward the random baseline —
   is an outlier (a Speyer hiding inside a good code) and is not used as a
   pseudo-reference. **Off by default (decision 2026-08-05):** under
   `jina-v2-base-de` expansion measurably hurts (receipt: 41.3% leakage
   with vs 26.5% without) — in a sharp embedding space pseudo-references
   widen a profile more than they help. The mechanism stays implemented
   and measured per calibration (configurations C/D vs E); it returns only
   if a future model's receipt favors it.
3. **Calibration negatives** (next section): only trusted codes may label a
   lot "definitely another trade" — an untrusted label is no label.

## The code-label channel and the trade fingerprint (decision 2026-08-04)

CPV codes are not only match keys — the official codelist gives every code a
**name** ("45312310 = Blitzschutzarbeiten"), and those names live in the same
language as the tenders. This adds a second, independently-failing channel
next to the win-text channel: tender text is rich but fuzzy (it drags in
project context — the school-newbuild problem), a code label is poor but
precise (it says the trade and nothing else). A candidate that clears both
channels is a far safer pass than either alone.

- **The dictionary** is the official EU CPV 2008 codelist, committed as
  `cpv_2008_de.csv` (code, German label; provenance noted in the file
  header) — a static reference, not downloaded at run time.
- **Label embeddings**: every label is embedded with the same `model_tag`
  and stored beside the lot sidecar (`cpv_labels.npy` + index). Same space,
  same model, rebuilt on model change like everything else.
- **The trade fingerprint** of a profile is **two-tiered by origin
  (decision 2026-08-05, configuration F)**. **Hard** codes are facts:
  trusted codes on the customer's actual won lots — full match authority.
  **Soft** labels are our own guesses: CPV entries ranked by label-embedding
  similarity to the reference texts, kept only above a floor with a
  reference-consensus requirement, and held to their own calibrated
  threshold. A soft match may pass a lot into the market but **never makes
  it pick-confident** — a guess that something matches exactly is still a
  guess (the Polderwand case: a coastal-protection label entered one
  profile via a 0.585 single-reference association and exact-matched a
  candidate's additional code at 1.0). The calibration pareto showed strict
  membership (floor .50 / consensus 2) costs ~4 points of leakage — a
  multi-trade firm's minority trade is often backed by a single win — so
  the searched optimum keeps loose membership, raises the hard bar
  instead, and the no-confidence rule contains what loose membership lets
  through. The
  fingerprint is the profile *named in official vocabulary* — it feeds
  (a) the code channel below, (b) the customer report's profile line
  ("Ihr Profil: Blitzschutzarbeiten, Elektroinstallation"), (c) the mapping
  target for free-text onboarding, and (d) the count of trade areas the
  pricing model needs.
- **The code channel** reads **all** of a lot's codes — `cpv_main` and
  `cpv_additional` — and scores the best label-to-label cosine between any
  of them and the fingerprint's codes (decision 2026-08-05: buyers often
  put the real trade in the additional codes; the Lübeck Gleisbau case
  carried `71521000 Baustellenüberwachung` there). Reference lots
  contribute their additional trusted codes to the fingerprint the same
  way. This grades across sibling codes (45312311 Blitzableiterbau scores
  near 45312310 Blitzschutzarbeiten) where the exact-match auto-pass is
  binary. The gate becomes an OR: pass if the text channel clears
  `min_relevance` **or** the code channel clears `min_code_relevance` —
  both fitted jointly in calibration to the same recall promise
  (configuration D; whether the channel should require *trusted* candidate
  codes is decided there empirically, not by taste).
- **The asymmetry survives**: the code channel adds evidence, never
  vetoes. A nonsense candidate code points at no fingerprint entry and
  contributes nothing — the text channel decides, which is yesterday's
  behaviour. The residual risk is a wrong code pointing *toward* the
  customer's own trade (a false pass into "read the notice" territory),
  bounded by the feedback loop like every other gate error.

## The same-buyer guard (decision 2026-08-05)

Serial buyers copy-paste one text template across all lots of all their
projects (measured: 311 of 1,166 multi-trade buyers do it heavily — 14% of
stored lots, including the largest public builders). Between two documents
written by the same office, text similarity is self-plagiarism, not
evidence. The same measurement shows these buyers code *better* than the
market (78% deep-coded): industrialized procurement templates the prose but
disciplines the dropdown.

The rule: **for a candidate sharing a buyer with any profile reference, the
text channel abstains and the code-label channel decides alone.** A
same-buyer lot whose code cannot speak (shallow or outside the dictionary)
goes to the borderline band — visibly undecided, never silently passed on a
meaningless signal. The asymmetry is untouched for independent buyers: there
text still decides and a code still cannot veto. Cross-buyer template reuse
is the residual weakness this guard cannot see; its fix is sentence-level
template stripping, specified as phase 6 below.

## The contract-type rule (decision 2026-08-05)

`contract_type` ("Art des Auftrags": works / services / supplies) is stored
and hard information, so the gate uses it — conservatively: a candidate
whose type is known and matches **none** of the profile references' types
goes to the **borderline band**, never a silent pass or drop. Borderline
and not out, because adjacent types can be real business (a works-profile
firm may bid a services-coded maintenance tender); visible-undecided is the
correct verdict for a signal this coarse. Unknown types on either side
disable the rule (a promise needs data on both sides).

## Reading the trade from the text — projection corroboration (phase 5, implemented 2026-08-05)

The Rettungswache diagnostic (2026-08-05, replayed on the pilot profile
with the deadline promise off) pinned the week's leakage to one door.
Not the text channel — at the pilot's 0.68 bar no wrong-trade lot passed
on raw text — but the **soft fingerprint**: the Sprinkenhof reference's
text ("Erdungs- und Blitzschutzanlagen", written inside a
laboratory-building project) reads as the label *45216120 Bauarbeiten an
Gebäuden für Not- und Rettungsdienste* at 0.618, just above the loose
membership floor (.60/k1, kept deliberately by the pareto), so the
profile acquired a **building label**; two Generalunternehmer-
Rettungswache lots then matched it label-to-label at 0.902 ≥ 0.750 —
soft-only passes (their text scores were 0.540/0.559, their hard score
0.438). The Polderwand case, second occurrence, now with a receipt.

The fix reads the label sidecar in the other direction. Score a
**candidate's text** against all label embeddings and ask *what trade
does this text read as*. Measured on the week's lots, this projection
separates exactly where the fingerprint failed: "Los 12
Blitzschutzanlagen" reads as *Blitzschutzarbeiten* top-1 (0.568); the
Rettungswache lots read as *Bau von Rettungsdienststationen* (0.62–0.63)
with the profile's trade label nowhere in reach. But the same
measurement forbids using the projection as a classifier or a general
veto: it is project-contaminated too. The Schönkirchen lot — genuinely
Blitzschutz, the title says so — projects onto school-building labels
(0.579) with *Blitzschutzarbeiten* below its top-3, and one of the
pilot's own six references (HafenCity, the biggest-project text) shows
no trade label in its top-3 at all.

The rule that fits both findings, under the standing asymmetry (a
derived signal may demote a guess, never override a fact):

> **A lot whose only pass is a soft code match must be corroborated by
> its own text: the candidate's projection onto the profile's hard trade
> labels must clear the corroboration bar, else the lot goes to the
> borderline band** — visibly undecided, never silently dropped.

Text passes and hard-code passes are untouched — the text the bidder
actually reads and a trusted code on the lot both outrank our guesses.
Profiles without hard labels (cold start, no wins yet) keep today's
behaviour: the rule needs a fact to corroborate against. Effect on the
worked week: both Rettungswache lots demote to borderline (also the
honest verdict — a Generalunternehmer newbuild genuinely contains
Blitzschutz as a sub-scope, so "undecided, read the notice" is correct);
Schönkirchen, Bordesholm and Los 12 (hard 1.0 each) are untouched.

**Calibration (configuration H).** The corroboration bar is fitted like
every threshold in this system, never hand-picked. Two forms are
measured and the receipt decides: **H1**, an absolute floor on
sim(candidate text, best hard label); **H2**, a contrast form — the
profile's hard label must rank within the candidate's top-k projections
(or within δ of its best projection anywhere), which self-adjusts for
text-poor lots. Objective as in G: minimise leakage under the
admitted-volume floor; recall reported, not promised. The run also
re-reports the soft-membership pareto with H active — if corroboration
contains what loose membership lets through, floor/consensus may stay
loose or relax, which is the direction the multi-trade-firm evidence
wants.

**Plumbing.** No new sidecar, no new model, no per-notice cost — the
label matrix already exists; the projection is one matrix-vector product
at judge time. Delivery rows gain `trade_read` and the matched label id
when the rule fired. Report copy is unchanged (scores stay internal); a
demoted lot renders in the borderline band, whose annex copy widens from
"knapp unter der Ähnlichkeitsschwelle" to also cover "nicht eindeutig
Ihrem Gewerk zuzuordnen".

**Receipt (2026-08-05, shipped).** The joint search chose **H2 @ 0.000**
— the strictest contrast form: a soft-only pass stands only when the
profile's hard trade label is the candidate text's *top* read in the
whole dictionary. Against configuration G: leakage 2.1% → **1.5%**,
recall 58.4% → **60.3%**, volume 5.0% → 5.1% — better on all three
axes, which is the signature of a rule that removes noise rather than
trading it. The soft-membership pareto re-ran with H active and relaxed
exactly as predicted: floor 0.60/consensus 1 → **0.45/consensus 2**,
soft threshold 0.750 → 0.725, text bar 0.680 → 0.700 (`relevance.py`
constants updated in the same commit; pilot subscription bumped to v6).
Verified on the worked week via `explain.py`/`tryout.py`: both
Rettungswache lots demote to the borderline band ("SOFT pass 1.000, NOT
corroborated — reads as Bau von Rettungsdienststationen"), Los 12 and
Bordesholm keep their hard passes, the pick list is unchanged.
Implementation note: the rule and its constants live entirely in
`relevance.py::judge` (rule 5 in the ladder); the delivery-row
`trade_read` stamp and the widened annex copy are the one remaining
loop.py touch, deferred while loop.py carries unrelated in-flight work
(SIMULATION.md).

## The trade-talk contradiction — hierarchy-aware hard-pass corroboration (phase 7, implemented 2026-08-06)

Phase 5 guards soft passes; hard passes stayed exempt under the asymmetry
("a trusted code on the lot is a fact"). The Trafostation case
(2026-08-06, found by the operator in the first replay demo) shows the
fact can lie *toward* the customer: Stadt Norderstedt coded a
transformer-station lot with 45312310 Blitzschutzarbeiten (plausibly for
an earthing sub-scope), the hard channel matched it 1.000 against the
pilot's wins, and the lot became a pick — while its own text scored 0.46
on the text channel and 0.168 on the trade-read. **This class is invisible
to the leakage metric by construction**: clean negatives are labeled by
trusted codes of a *different* class, so a wrong lot wearing the
customer's own code can never enter the negative set. The 1.5% receipt
number is honest for the contradicting-code class and silent about this
one; the two wrong picks the operator has rejected to date (Trafostation,
Bordesholm) both walked through this door.

The rule (operator's design, 2026-08-06): **leniency for project talk,
scrutiny for trade talk.** A tender's text describes two different things
— *where the work happens* (the project: Schulzentrum, Sanierung,
Erweiterung) and *what the work is* (the Gewerk). Only the second can
contradict a code:

1. **Split the label space by CPV branch.** Trade labels = groups 453
   (Bauinstallation) and 454 (Ausbau); object/project labels = 450–452.
   Verified against the operator's counter-worry: all 19 "Oberbauarbeiten"
   labels are 452 — surface works on objects, project vocabulary, never a
   contradiction source.
2. **Compatibility is the CPV hierarchy itself, ancestor-based.** A
   candidate's trade reading is *compatible* with the profile when its
   code and a profile hard code stand in an ancestor/descendant relation
   (zero-trimmed prefix of one another): a text reading as
   *Elektroinstallationsarbeiten* (45310000) can never contradict a
   Blitzschutz profile (45312310) — that is what the taxonomy's tree
   means. Sibling categories (45316 Signalanlagen, 45315 Stromversorgung)
   are different trades. Measured necessity: at class level (4 digits) the
   Trafostation *escapes* (its top trade reading shares class 4531);
   ancestor-vs-sibling at category depth catches it.
3. **The contradiction is a margin, not a floor**: the lot's best
   *foreign* trade reading must exceed its best *profile* trade reading
   by `trade_talk_margin`. Self-normalizing — a text-poor lot murmuring
   about everything confidently about nothing stays lenient. Measured on
   the worked cases: Trafostation +0.28, Bordesholm +0.29 foreign margin;
   Schönkirchen and all six pilot references ≤ ~0 (their top trade
   reading is 45312310 itself, every one).
4. **Effect**: a hard pass whose text carries a confident foreign-trade
   contradiction demotes to the borderline band — visibly undecided,
   never a silent drop, same landing zone as every other demotion.

The asymmetry survives in amended form: a code still cannot veto text,
and our *guesses* still cannot veto facts — but a fact contradicted by
the lot's own confident testimony about a different trade is no longer
treated as unopposed.

**Calibration (configuration K).** Parameters: `trade_talk_margin`
(worked-case gap suggests ~0.15–0.25; the receipt decides), the branch
split (453/454 default; the VOB/C ATV catalog as the upgrade path if CPV
branches prove too coarse), and ancestor depth. Objective as in G/H —
but with an honest twist the receipt must state: **the standard clean-
negative leakage metric cannot see this rule's benefit** (agreeing-wrong-
code lots are excluded from negatives by construction). The receipt
therefore adds a new diagnostic: the *contested hard-pass rate* (share of
hard-code admissions carrying a foreign-trade margin above the bar),
reported per configuration, with a hand-read sample of contested cases as
ground truth — measurement and mechanism must not share the same trust in
the same code twice. The recall price shows up normally in the positives
(a real win demoted by a foreign margin is a counted miss).

Diagnostic receipts so far (2026-08-06, pilot cases only — store-wide
numbers are configuration K's job): the two known wrong hard-pass picks
are separated from all eight known-right lots with ~0.28 of margin to
spare; sample size two-and-eight, which is why K exists.

**Receipt (2026-08-06, shipped).** The K sweep at H's operating point:
margin **0.225** = the largest catch whose recall price stays under half
a point — recall 60.3% → 60.0%, with **5.9% of all hard-code admissions
flagged as contested** (17 of 2,473 positives; both known wrong picks
carried +0.28/+0.29 and are caught with room to spare). The curve is
steep and informative: margin 0.10 would contest 28.7% of hard passes at
2.5 recall points — the store is full of generously coded lots — while
0.30 catches almost nothing. As predicted, the clean-negative leakage
number does not move (1.5%, the metric is blind here); the contested
rate is the benefit proxy, pending hand-read ground truth from the
borderline band. Verified on every worked case via `explain.py`:
Trafostation and Bordesholm demote to borderline, Schönkirchen (−0.029)
and Los 12 (−0.089) pass untouched; the pilot's current market shrinks
to the two lots that are actually his trade, pick unchanged.
Implementation: `trade_talk_contradicted()` + rule 6 in
`relevance.py::judge`, `foreign_trade_rows` on the profile, the K sweep
in `calibrate.py`, the trade-talk verdict line in `explain.py`.

## Sentence-level template stripping (phase 6, measured 2026-08-06 — not shipped)

The remaining text pathologies are one pathology: tender prose describes
the *project around the trade*, and whole-document embeddings average
the two. The measured symptoms, all from the same diagnostic: the
borderline cluster at 0.66–0.68 (Klassenhaus, Heizung, Technische
Außenanlagen — other trades of serial school builders pressing against
the bar from below, cross-buyer, so the same-buyer guard cannot see
them); the Schönkirchen projection above; and the HafenCity reference's
noisy projection. Phase 5 contains the damage at the gate; this phase
attacks the cause in the vectors.

- **Segmentation**: split `title + description` into sentences (simple
  splitter, German abbreviation guard). Title always survives.
- **The boilerplate ledger**: normalise each sentence (whitespace,
  case), hash it, count distinct procedures it appears in across the
  store. A sentence spanning ≥ `strip_min_procs` procedures (proposal:
  5) is a boilerplate *candidate*. Frequency alone cannot be the
  verdict: standard trade phrases ("Blitzschutzanlage nach DIN EN
  62305") are frequent too, and they ARE the signal. The guard reuses
  the cohesion idea at sentence level — a frequent sentence is stripped
  only if its host lots are **heterogeneous** (spread across many cpv3
  trades); a frequent sentence whose hosts are homogeneous is trade
  vocabulary and stays. The ledger is derived data, rebuilt with the
  store, never committed.
- **Distinctive text** = what remains after stripping, on **both sides
  symmetrically** — references and candidates get the same treatment. A
  lot whose text is entirely boilerplate is honestly text-poor: its
  distinctive text is the title, and where that is thin too, the score
  is honestly low and the code channel decides — today's ladder,
  unchanged.
- **Storage**: a new sidecar directory `<model_tag>-strip`, full
  re-embed, exactly the append-only discipline of a model flip — old
  vectors untouched, rollback is flipping back. Cost: hashing is
  negligible; the re-embed is one checkpointed backfill (hours under
  jina); disk doubles per tag.
- **Calibration**: configuration **I** (stripped text channel replacing
  the plain one) and **J** (H + I together), same objective as G. The
  flip ships only on a winning receipt, like every model change.
- **Per-sentence max scoring** (scoring each surviving sentence against
  the profile and taking the max) is measured as a *diagnostic only*:
  it converts "contains my trade as a sub-scope" into a strong signal,
  which for Generalunternehmer lots is precisely the ambiguity whose
  honest verdict is borderline, not pass. If it ever ships, it feeds
  the borderline band, never a full pass.

**Receipt (2026-08-06, not shipped).** Built and measured end-to-end:
`strip.py` + the `jina-v2-base-de-strip` sidecar (23,354 lots re-embedded;
boilerplate = 1.0% of distinct sentences carrying 10.3% of store text).
The mechanism is validated where it acts — the raw text channel's leakage
at the 90%-recall promise drops (A 32.1% → 28.6%, B 37.1% → 34.8%), and
ten more deep codes reach trust (56 → 66; sharper vectors, sharper
cohesion). But at the shipping operating point it loses: configuration I
(stripping alone) lands at **1.9% leakage / 61.4% recall**, and in
configuration J the search turned the corroboration off — stripping and
phase-5 corroboration attack the *same* template noise, so they are
substitutes, not complements, and neither combination beats the
unstripped gate's **1.5% / 60.3%**. Per the standing rule (flip only on
a winning receipt, as with USE_EXPANSION) the stripped sidecar stays
unshipped; receipts are committed, the sidecar and frozen ledger stay on
disk (rebuildable), and the configuration re-runs at the next model_tag
flip. Comparability caveat, disclosed: trust lists are computed per tag,
so the two receipts' clean-negative pools differ slightly.

## One bar (decision 2026-08-05, supersedes the pick margin)

The earlier design had two thresholds on the relevance score: a loose one
for the market view and a stricter "pick confidence" one (threshold +
hand-picked margin) for recommendations. With the market view demoted from
product to appendix, the loose bar guarded an audience that does not exist,
and the margin was the one number in the system never backed by a
calibration receipt — it cost a documented false rejection (a €131k solo
win at rank 5). Superseded by a **single calibrated bar**: a lot that
passes the gate is recommendable, full stop; the pick list is the flagged
gated lots, capped at `max_picks`.

The bar's calibration objective changes with it (configuration G). The
product principle — a short list where the customer finds their business
and it is likely lonely — contains no recall promise, so none is imposed:
the search **maximises list precision (minimises wrong-trade leakage) and
is stopped only by the constraint that a typical customer's week still has
candidates** (admitted market volume above a floor derived from the pilot
replays). The recall that falls out is *reported* in the receipt, not
promised.

**The relevance "why" stays**: every pick carries a plain-language reason —
"ähnelt Ihrem Auftrag „…‟" or "CPV-Code passt: <label>" — next to the
competition "why". Scores stay internal (decision 2026-08-04). The
borderline band below the single bar also stays, as a display-and-feedback
surface only.

Report copy consequence (unchanged): the report never cites how many lots
were checked or matched — the product is the list, and the size of our
haystack is our business, not the customer's.

## Calibrating `min_relevance` — from the data, not from taste

The awards store already contains everything needed, no customers required:

1. For every repeat winner (≥3 wins in the store — 528 firms today), hold
   out each win and score it against the firm's remaining wins: the
   **positive** distribution. The default threshold is set from this
   distribution alone — the 10th percentile, i.e. the recall promise "90% of
   a firm's own wins pass their own gate." No negative labels required.
2. **Wrong-trade leakage** is measured against *clean* negatives only: lots
   carrying a **trusted** deep code (previous section) in a *different*
   class than all of the firm's wins. Untrusted deep codes may not label
   negatives — measured against naively deep-coded negatives the leakage
   number mixes model errors with label errors (an electrical lot deep-coded
   as hospital construction counts as "leakage" when the model correctly
   recognises electrical work). Random lots are NOT negatives either: a
   random lot won by another firm of the same trade is a competitor's win —
   a tender the gate *must* pass, since the customer should have bid on it.
   A threshold tuned to reject those would systematically hide competitor
   territory, the most valuable part of the feed (both flaws found
   2026-08-04, before phase 2 was built).
3. The pass-rate over random same-division lots is still computed, but
   reported as **admitted market volume** ("this profile lets through X% of
   the division") — a sizing number, never an error rate.

The run (`calibrate.py`) reports the gate in three configurations so every
design choice carries its own number: text-only against naive negatives
(the historical baseline), text-only against trusted negatives (label noise
removed), and the full hybrid (profile expansion + auto-pass). It is
repeated once per embedding `model_tag`, with the resulting curves committed
to the repo as the threshold's receipt. Per-subscription overrides of `min_relevance` are a
format field, not new architecture — a customer in a text-poor trade may
need a looser gate, and the borderline band catches the cost of guessing
wrong.

## Feedback — the gate learns from being wrong

Two mistake types, two cheap signals, one append-only file
(`data/ledger/relevance_feedback.jsonl`):

- **False positive** ("not my business"): every delivered pick row renders
  with a one-click marker. The mark is recorded with the lot, subscription,
  profile version, and score. Effect: operator review; typically raises
  `min_relevance` (new subscription version) or removes a stale
  `profile_ref`.
- **False negative** ("you missed this one"): the customer names a tender
  they found themselves. Recorded the same way, with the score it *would*
  have had. Effect: usually a new `profile_ref` (the named tender itself —
  after they bid on it, it is a legitimate reference by definition).
- **Passive positive**: a win by the customer arriving in the awards store
  is a candidate `profile_ref` proposed automatically at the next
  subscription review — the profile grows from the public record without
  anyone typing.

**The borderline band** keeps misses visible without flooding: lots scoring
within a small margin below the gate (default 0.05) render in the annex
under *"knapp aussortiert"* — one line each, so a miscalibrated gate is
discovered by reading, not by silence. Full-annex rules from SUBSCRIPTIONS.md
apply (not delivery-ledger rows; disputes trace through the prediction
ledger and the dated annex file).

## Honesty plumbing (stamps, as always)

Delivery rows gain, at write time: `relevance_score`, `profile_version`
(the subscription version whose profile produced it), `embed_model_tag`.
The per-slice track record (grades ⋈ deliveries) is thereby computable
*conditional on the gate that was in force* — "what did Jebsen see and how
did it end" never depends on reconstructing an old profile. Rows written
before these stamps predate the feature and carry no relevance columns;
they are graded as ungated, which is what they were.

The customer never sees a relevance score, tier, or threshold — same
boundary as SUBSCRIPTIONS.md (decision 2026-08-04): scores are our mental
load. The customer-visible effect is purely that everything in their report
*is their trade*, and the report header may say, in words, how their view
is defined: *"Ihr Profil: 6 gewonnene Ausschreibungen + 1 Beschreibung."*

## What can go wrong, and the designed answer

- **A profile reference is itself miscoded garbage** (wrong tender attached
  to the firm in the awards data) → max-similarity means one bad reference
  admits junk near *it* but never blocks good lots; the false-positive
  marker plus operator review removes it. Profile edits are new versions —
  attributable, reversible.
- **Customer's business drifts** (new line of work) → false-negative signal
  or a new win adds the reference; nothing needs re-architecting.
- **Embedding model upgrade** → new `model_tag` directory, full re-embed,
  re-run calibration, new default threshold; old delivery rows keep their
  stamped scores and tags, so track records stay attributable to the model
  that produced them.
- **A trade where titles are uninformative** ("Los 3 — Elektro") → the
  description carries the signal (p90 ~1.6k chars); where both are thin, the
  score is honestly low, the borderline band surfaces the near-misses, and
  the coarse CPV filter still bounds what can be lost.
- **The gate goes wrong silently** → it cannot: gated-out lots near the
  threshold are printed (borderline band), gated-in mistakes have a
  one-click marker, and the calibration curves are committed artifacts.

## Build order (phases in time — no component is cut)

1. **Embedding sidecar + backfill** — UTF-8 verification, model choice
   pinned as `model_tag`, embed the full store, wire the daily increment
   into the extract phase. *Everything else reads this; ship first.*
2. **Calibration notebook** — positive/negative distributions from repeat
   winners, default `min_relevance`, curves committed. *One Colab or laptop
   run; repeats only on model change.*
3. **Profile fields + gate in the renderer** — subscription format v2
   fields, gate applied between slice filter and ranking, delivery-row
   stamps, borderline band in the annex. *First customer-visible effect;
   Jebsen (version 2, CPV widened to `45`) is the pilot subscription.*
4. **Feedback file + review loop** — markers, missed-tender intake, the
   automatic profile-ref proposal from new wins. *The gate starts learning.*
5. **Projection corroboration** — configuration H in `calibrate.py`, the
   soft-only corroboration rule + `trade_read` stamps in `relevance.py`,
   widened borderline copy. *No new infrastructure; kills the
   soft-fingerprint leak (Rettungswache class) at the gate.* **Shipped
   2026-08-05** (H2 @ 0.000; stamps + annex copy pending, see receipt
   note above).
6. **Template stripping** — boilerplate ledger, `<model_tag>-strip`
   sidecar backfill, configurations I/J; flip only on a winning receipt.
   *Attacks the project-vocabulary cause in the vectors themselves.*
   **Measured 2026-08-06: receipt lost to configuration H — not shipped**
   (see receipt note above).
7. **Trade-talk contradiction** — configuration K in `calibrate.py`
   (margin, branch split, ancestor depth, plus the contested-hard-pass
   diagnostic with hand-read ground truth), then the hierarchy-aware
   demotion rule in `relevance.py::judge`. *Closes the agreeing-wrong-
   code door the Trafostation walked through.*

Each phase leaves a running system; a subscription without `min_relevance`
is untouched at every phase, so nothing ships as a flag-day.

## Open decisions (defaults proposed, none blocking Phase 1)

- **Embedding model** — `multilingual-e5-small` proposed (384 dims, strong
  German, ONNX-friendly); decide at Phase 1 and pin as `model_tag`.
- **Borderline margin** — 0.05 below `min_relevance` proposed; revisit after
  the calibration curves exist.
- **Trust parameters** — `trust_min_lots` (10 proposed: below that, cohesion
  is too noisy to certify a code) and `trust_cohesion_min` (proposed at the
  measured random baseline + 0.15; the calibration receipt shows the
  sensitivity). Both recomputed per `model_tag`, both parameters like every
  window in this system.
- **Annex volume under a coarse prefix** — widening `cpv_prefixes` to `45`
  multiplies annex candidates; the gate shrinks them again, but if annexes
  balloon for text-poor profiles, cap the annex at the top-N by relevance
  with an explicit "N weitere unter der Schwelle" line — a rendering rule,
  not architecture.
- **`profile_texts` weight** — references from real wins and references
  from free text currently score identically; if free text proves noisier,
  a per-reference weight is a format field away.
- **Corroboration form (phase 5)** — *decided 2026-08-05 by the receipt*:
  H2 @ 0.000 (contrast form, strictest setting) beat every H1 floor; the
  soft membership relaxed to floor 0.45/consensus 2 in the same search.
  Reopens only on a model_tag flip, like every threshold.
- **Stripping parameters (phase 6)** — `strip_min_procs` (5 proposed)
  and the host-heterogeneity cut for the trade-phrase guard; both
  reported with sensitivity in the receipt, like the trust parameters.
- **Competitor detection (idea, not scoped)** — profile similarity between
  firms is competitor identification for free: the same sidecar over award
  data answers "who competes with whom" per niche. Possible future
  analytics product; noted here so it is not re-derived from scratch.
