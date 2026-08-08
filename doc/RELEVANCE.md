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
4 feedback stays the backstop throughout. **Phase 8 (the evidence gate,
specified 2026-08-06) supersedes the guess channels wholesale when its
receipt wins**: embeddings nominate, lexical trade evidence convicts —
the operator's own reading procedure, adopted as the gate's; the soft
fingerprint and the phase-5/7 guards are deleted with it.
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

## Reading the trade from the text — projection corroboration (phase 5, implemented 2026-08-05; superseded by phase 8 when configuration L ships)

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

## The trade-talk contradiction — hierarchy-aware hard-pass corroboration (phase 7, implemented 2026-08-06; superseded by phase 8 when configuration L ships)

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

## The evidence gate (phase 8, configuration L — specified 2026-08-06, supersedes the guess channels)

Origin: the operator rejected the Kreishaus Schwachstrom recommendation by
a procedure better than the gate's — read the **Leistung** section, scan
for trade evidence, reject on absence. The gate could not do this because
cosine similarity cannot represent absence: it compresses project frame,
lexical fields and trade content into one "feels alike" number. The soft
fingerprint guessed trades from reference texts, phase 5 guarded that
guess, phase 7 guarded the hard channel with embedding margins — three
layers of compensation for a judgment the similarity score had discarded.
This phase replaces the compensations with the judgment.

**Design principle: embeddings nominate, evidence convicts.** The burden
of proof flips: today any firing channel passes a lot; under L a
recommendation requires positive trade evidence, and ambiguity lands in
the borderline band — visibly undecided, never silently passed.

**Component 1 — the section splitter (structure-aware text).** German
notices routinely separate project description from the procured work,
often with literal headings ("Kurzbeschreibung des Projekts:" /
"Kurzbeschreibung der hier ausgeschriebenen Leistung:" — the Kreishaus
notice labels both). Split on these markers (plus conservative
heuristics); the evidence test reads title + Leistung part. No detectable
structure → the whole text is the Leistung (fail-open). This is the
structural, near-free version of what phase 6 tried statistically.

**Component 2 — the per-profile trade lexicon, derived, never
hand-written.** Three sources, merged and stemmed: (a) the distinctive
tokens of the reference texts — frequent in the customer's wins, rare in
the store (document-frequency ratio; on the pilot this alone surfaces
Blitzschutz, Erdung, Fangeinrichtung, Ableiter, Fundamenterder,
Blitzschutzklasse); (b) the tokens of the profile's hard-code CPV labels;
(c) `profile_texts` tokens — the cold-start path. Matching is
substring-on-stems, which German compounding makes strong
("blitzschutz" hits Blitzschutzanlage, Gebäudeblitzschutz,
Blitzschutzklasse). The lexicon is a derived artifact of the profile,
rebuilt when the profile changes, stamped on delivery rows.

**Component 3 — the simplified ladder** (judge shrinks from six rules to
four):

    1. not in sidecar -> pass ungated            (fail-open, unchanged)
    2. contract-type mismatch -> borderline      (cheap fact, unchanged)
    3. NOMINATE: text similarity >= nomination bar (same-buyer: abstains,
       unchanged), OR a hard trusted-code match. Nomination is the recall
       funnel and convicts nothing.
    4. CONVICT: trade evidence present in title+Leistung -> market;
       absent -> borderline. No channel passes a lot without evidence —
       not a 1.000 code match (the Trafostation dies here: zero
       trade-family terms), not a high cosine (the Kreishaus lot dies
       here identically).

**Superseded but NOT deleted (decision 2026-08-06, operator).** This is
a large change, and the insurance is the same as for an embedding-model
flip: **both gates stay implemented, a single committed switch
(`GATE_MODE = 'evidence' | 'embedding'`) chooses, and rollback is
flipping the constant back** — no retraining, no rebuild, the sidecars
and the champion model stay on disk untouched (the competition model is
independent of the gate by design and is never affected). The state
before the experiment is tagged `gate-v1-embedding`. Under
`'embedding'` the phase-5/7 ladder runs exactly as shipped; under
`'evidence'` the soft fingerprint, phase-5 corroboration and phase-7
margins are simply not consulted. Deleting the dormant machinery is a
separate, later decision made after live experience, not by this spec.
**Kept in both modes**: the coarse filter, trusted-code cohesion and the
hard channel (as nominator), the same-buyer guard, the contract-type
rule, the borderline band, one bar, all stamps. The report's "warum Ihr
Geschäft" gets *better* under the evidence gate: it can quote the
evidence ("nennt Fangeinrichtungen, Ableiter") instead of describing a
score.

**Evidence matching is three-tiered (operator's design, 2026-08-06)** —
document-level similarity is gone, but embeddings survive at the one
granularity where they cannot lie. Per keyword, cheapest tier first,
every match quotable: (1) **exact** — case-folded substring search;
German compounding makes stems powerful (blitzschutz hits
Blitzschutzanlage, Gebäudeblitzschutz, Blitzschutzklasse); (2) **typo**
— bounded edit distance, deterministic, reported as "gefunden:
'Blitzshutzanlage', vermutlich Blitzschutz"; (3) **synonym** — the
keyword's embedding against embeddings of the description's *words*
(the label sidecar already proves word-level embedding works in this
space): "Überspannungsschutz (ähnlich Blitzschutz, 0.8x)". A single
word carries no project frame — the failure mode that killed
document similarity is structurally impossible at word granularity.
Word vectors are cached by vocabulary (embed each unique word once);
tier 3 runs only on words tiers 1–2 did not settle, only in slices —
no backfills on the weekly critical path. One calibrated threshold
(tier 3), one edit-distance bound (tier 2), both in configuration L's
receipt.

**Calibration (configuration L).** Parameters: the nomination text bar
(re-searched, and expected to come DOWN from 0.700 — similarity no
longer convicts, so it can afford recall); the lexicon distinctiveness
cutoff; evidence scope (title+Leistung vs whole text, decided
empirically). Objective as in G. Two receipts gate the ship decision:
the standard corpus (recall/leakage/volume vs H+K's 60.0% / 1.5% / 5.1%)
**and the operator benchmark** — the 16 hand-labeled lots of 2026-08
(6 confirmed-wrong, 10 confirmed-right), committed as a regression file;
a configuration that misjudges any benchmark case is rejected regardless
of its aggregate numbers.

**Known risks, named**: (a) genuinely relevant lots with term-free thin
text and no honest code land in borderline — the measured recall price,
absorbed by the band and phase-4 feedback; (b) sub-scope mentions
("inkl. Blitzschutz" inside a Generalunternehmer Leistung) satisfy the
evidence test — whether that needs a density/title condition is decided
by the benchmark, not by taste; (c) evidence-only lots (terms present,
no nomination) are reported as a diagnostic before anyone decides
whether evidence may also nominate.

**Build order**: (1) benchmark file + section splitter + TF-IDF lexicon
derivation + the three-tier matcher, diagnostic against the 16 cases
and the 2,473-win leave-one-out *before* any gate change; (2)
configuration L in `calibrate.py`; (3) the evidence ladder in
`relevance.py` behind `GATE_MODE`, embedding gate untouched; (4) the
"warum" upgrade in the report. Flip `GATE_MODE` to `'evidence'` only on
a winning receipt; the tag `gate-v1-embedding` and the constant are the
two-step rollback.

**Status (2026-08-06, built — NOT flipped).** Everything is implemented
behind `GATE_MODE` (default `'embedding'`; env var for per-run tryouts).
Receipts so far, honestly mixed:

- **Operator benchmark: 19/19.** Two derivation lessons were learned on
  the way and are now rules: keywords must come from **more than one
  buyer's** wins (the same-buyer lesson applied to the lexicon — the SBH
  template had smuggled "hansestadt/landesbetrieb/wirtschaftlich" in),
  and **buyer-name words are excluded** (they say who, not what —
  "hamburg" is geography). The pilot lexicon after both rules:
  `blitzschutz, erdungsanlag` — the whole trade, auditable at a glance.
  Every benchmark verdict quotes its evidence or its absence.
- **Store-wide leave-one-out (tiers 1–2, conviction-only): recall
  51.7%** vs the embedding gate's 60.0% — the vocabulary-gap fear is
  real at scale. The miss autopsy: 13.7% of holdout profiles derive an
  empty lexicon, 40% under three keywords (multi-trade firms with few
  wins per trade), and the rich-lexicon misses are dominated by
  trade-name synonymy inside a firm's own wins (Holzbau↔Holzarbeit,
  Schlosser↔Stahlbau, Parkett↔Bodenbelag) — precisely tier 3's job;
  its receipt is the open measurement. Conviction-only leakage 5.5% /
  volume 7.4% are upper bounds (no nomination step applied) and include
  the genuine "names the trade as sub-scope" class.
- Verified end-to-end under `GATE_MODE=evidence`: the pilot's render
  yields the same single correct pick, the "warum Ihr Geschäft" column
  quotes the found words ("nennt blitzschutz, erdungsanlag"), and
  `explain.py` prints lexicon + evidence per lot alongside the
  embedding path.

**Receipt trajectory (2026-08-06, all tiers-1–2 unless noted; embedding
gate reference: 60.0% recall).** Two operator design changes closed most
of the gap, each measured against both gatekeepers:

| configuration | recall | conviction-only leakage | benchmark |
| --- | --- | --- | --- |
| two-witness rule, exact+typo | 51.7% | 5.5% | 19/19 |
| + synonym tier (tier 3) | 55.9% | 7.3% | 19/19 |
| single-witness rule (operator) | 54.8% | 6.6% | 19/19 |
| **single-witness + synonym tier** | **59.0%** | 8.4% | **19/19** |

The single-witness decision (MIN_WITNESSES = 1) fixed the diagnosed
starvation: the two-witness rule was unsatisfiable for 2-wins-per-trade
profiles (the Ahle carpentry autopsy — 'zimmer', rare and present, died
for lacking a second witness that the leave-one-out itself had hidden).
The buyer-diversity rule still guards multi-buyer profiles against
template words; the pilot lexicon is unchanged. Leave-one-out
understates BOTH gates' live recall equally (hiding one of two
same-trade wins starves the test profile, not the live one).

**The whole run through the REAL judge() (2026-08-06, `evidence.py
--judge`: ~130k judgments per mode, same lots, same seed — the
calibration numbers above are arithmetic replicas; this executed the
shipped code):**

| real judge() | benchmark | recall (LOO) | leakage | volume |
| --- | --- | --- | --- | --- |
| embedding gate (live) | 18/19 | 44.5% | 1.9% | 4.6% |
| evidence gate | 18/19 | 23.8% | **0.4%** | 0.9% |

Three findings. (1) **The replicas flattered both gates**: the live
gate's true recall is 44.5%, not 60.0% — the replica omitted the
same-buyer guard (LOO holdouts often share a buyer with the remaining
refs, so the text channel abstains), and real leakage is 1.9%, not
1.5%. (2) **The evidence gate's 23.8% is not its conviction quality
(59.0%) but its NOMINATION bar**: the ladder reused the embedding
gate's 0.700 conviction bar as a nomination bar, which the spec
explicitly predicted must come down ("similarity no longer convicts,
so it can afford recall") and which was never re-searched. The shared
benchmark failure proves it: the Kölln-Reisiek reference (coded
45000000, findable only by text) has the evidence — 'blitzschutz' in
its title — but no nomination path under LOO. (3) **Apples-to-apples
leakage at last: 0.4% vs 1.9%** — the full evidence ladder leaks
five times less than the live gate. **Next measured step (open)**:
re-search the nomination bar downward (the old 90%-recall levels
~0.45–0.50 are the natural grid; or let evidence itself nominate) —
expected to lift full-ladder recall toward its 59% conviction ceiling
while keeping the leakage advantage; then the flip decision has
honest numbers on both sides. `GATE_MODE` remains 'embedding'.

**The nomination bar, re-searched (2026-08-06, `evidence.py --sweep`).**
The bar became an explicit constant (`NOMINATION_BAR`, no longer the
embedding gate's `min_relevance`), the decision itself a pure shared
function (`relevance._evidence_verdict`) so the sweep executes the
shipped decision code on components collected in ONE real-code pass
(same loops, same seed as `--judge`: 2,473 positives, 25,600 clean
negatives, 102,400 volume lots). The 0.70 anchor row reproduces the
committed `--judge` receipt to the digit — the refactor changed no
behaviour. The grid:

| real judge() components | benchmark | recall (LOO) | leakage | volume |
| --- | --- | --- | --- | --- |
| embedding gate (live) | 18/19 | 44.5% | 1.9% | 4.6% |
| evidence, bar 0.40 | 19/19 | 42.1% | 6.0% | 7.2% |
| evidence, bar 0.45 | 19/19 | 41.1% | 4.5% | 5.5% |
| evidence, bar 0.50 | 19/19 | 39.3% | 3.0% | 3.8% |
| **evidence, bar 0.55 (chosen)** | **19/19** | **35.9%** | **1.6%** | **2.4%** |
| evidence, bar 0.60 | 18/19 | 31.8% | 0.8% | 1.5% |
| evidence, bar 0.65 | 18/19 | 27.3% | 0.4% | 1.1% |
| evidence, bar 0.70 (old, anchor) | 18/19 | 23.8% | 0.4% | 0.9% |
| evidence-nominates (diagnostic) | 19/19 | 57.9% | 8.7% | 10.3% |

**0.55 is the only grid point satisfying both ship constraints** —
benchmark 19/19 (the Kölln-Reisiek in→out failure flips to correct;
its LOO nomination similarity sits just under 0.60) and leakage below
the live gate's 1.9% (1.6% on 25,600 shared negatives is ~4σ — real,
if modest). Below it, leakage runs away (3.0% at 0.50); above it, the
benchmark breaks. The evidence-nominates variant (evidence alone may
nominate, no similarity/code needed) measures the gate's true
conviction ceiling — 57.9% recall — but at 8.7% leakage: conviction
without nomination is not precise enough to ship, so
`EVIDENCE_NOMINATES = False`. The recall gap to the ceiling
(35.9% vs 57.9%) is now mostly the same-buyer abstention, which LOO
overstates (a holdout often shares its buyer with the remaining refs,
killing the text nomination path; live candidates rarely do).

**Standing after the re-search**: the evidence gate beats the live
gate on the benchmark (19/19 vs 18/19) and on leakage (1.6% vs 1.9%),
and trails it on LOO recall (35.9% vs 44.5%). It does NOT yet beat
the live gate on both axes. Confirmed end-to-end with the committed
constant: a full `evidence.py --judge` (both modes, the shipped
`_judge_evidence`, no component shortcut) reproduces the chosen row
to the digit — evidence 19/19 / 35.9% / 1.6% / 2.4%, embedding 18/19
/ 44.5% / 1.9% / 4.6%. `GATE_MODE` remains 'embedding'; the flip is
the operator's call on these numbers.

**Phase 8b — the witness rule (operator design, 2026-08-06; measured,
kept OFF).** "One coincidence is coincidence, multiple coincidences
are a conviction": evidence alone may NOMINATE a lot — no similarity,
no code, deliberately buyer-independent (it targets the same-buyer LOO
miss class, e.g. the Prokot lot: similarity 1.000 to its sibling wins,
16 lexicon words in the text, rejected because text abstains and the
code is silent) — when at least `EVIDENCE_NOMINATION_MIN` DISTINCT
keywords are found by the exact/typo tiers. Synonym hits do not count
toward nomination (measured: they add nothing — K>=2 all-tiers 2.5%
leakage vs 2.4% exact/typo, same recall). Conviction, the bar, and the
same-buyer guard for text are unchanged; K=1/any-tier reproduces the
rejected evidence-nominates variant. Receipt (one `--sweep` pass, real
judge() components, same corpus as above; `hard19` = the original
hand-labeled set, `bench` = the grown 103-case scorecard):

| bar 0.55 + witness rule | hard19 | bench | recall | leakage | volume |
| --- | --- | --- | --- | --- | --- |
| embedding gate (live) | 18/19 | 47/103 | 44.5% | 1.9% | 4.6% |
| K off (committed) | 19/19 | 39/103 | 35.9% | 1.6% | 2.4% |
| K>=1 any tier (anchor) | 19/19 | 55/103 | 57.9% | 8.7% | 10.3% |
| K>=2 exact/typo | 19/19 | 47/103 | 48.3% | 2.4% | 3.4% |
| K>=3 exact/typo | 19/19 | 42/103 | 43.2% | 1.8% | 2.7% |

**Decision (pre-stated rule: max recall s.t. leakage CLEARLY below the
live 1.9% and hard19 at 19/19): the rule stays off.** K>=2 leaks 2.4%
— above the live gate, disqualified. K>=3 leaks 1.8% — below, but by
~25 lots on 25,600 shared negatives (<1σ), not clearly. The residual
K>=2 leak class is honest multi-word sub-scope: an EMSR/GU lot whose
LV genuinely lists Blitzschutz AND Erdung as positions produces two
true witnesses for the wrong trade; no count threshold separates it
from the real thing (the spec's risk (b), still open — density/title
conditions are the next candidate lever). A bar×K cross-grid is
excluded by the hard set: Kölln-Reisiek carries exactly ONE witness
('blitzschutz'; the title's typo "Erdnungsanlagen" is 3 chars off the
'erdungsanlag' stem, beyond the typo tier's edit-1 window), so any
bar >= 0.60 fails hard19 regardless of K. **Standing operator option**:
K>=2 is the first configuration that beats the live gate on recall
(48.3% vs 44.5%) while keeping hard19 at 19/19, tying the 103-case
scorecard and running at 26% less volume — its price is 2.4% vs 1.9%
leakage. One constant (`EVIDENCE_NOMINATION_MIN = 2`) turns it on.

## Phase 8c — lexicon coverage (operator decision 2026-08-06: recall first)

The evidence gate's recall was capped by conviction coverage (57.9%
ceiling), and the autopsy found a structural bias: **the store-rarity
sieve deletes the trade's own name for exactly the biggest trades**
(malerarbeiten 2.0%, sanitär 2.6%, heizung 2.6%, lüftung 2.1%, abbruch
4.8% of store lots — all above the 2% cutoff; a trade's name is common
in proportion to its market share). Three changes, each behind its own
constant in `evidence.py`:

1. **Definitional waiver** — words from the profile's own trusted-code
   labels enter regardless of store frequency when they name few trades
   (label-space rarity, `LABEL_DF_MAX`): 'estricharbeiten' names one
   trade (definitional), 'installation' names dozens (still filtered).
2. **Trade dictionaries** (`trade_dictionaries`, cached
   `data/trade_dicts.json`) — each trusted trade's vocabulary derived
   from ALL store lots carrying the code: frequent inside the trade
   (>= 10% of its lots), rare outside (>= 8x ratio). The two-sided test
   distinguishes 'malerarbeiten' (common only inside painting) from
   'neubau' (common everywhere), which the store-wide cutoff cannot. A
   3-win firm inherits the vocabulary of hundreds of lots: the pilot
   painter's lexicon went from `anstricharbeit` alone to `malerarbeit,
   lackierarbeit, anstrich, beschichtung, ...`; Jebsen's is the trade
   itself (`blitzschutz, erdungsanlag, ableitung, fangstang, ringerder,
   fundamenterder`).
3. **Title-or-two conviction** (`evidence.convicts`) — the benchmark
   forced this during implementation: the richer lexicon's 'ableitung'
   convicted the Kreishaus-Starkstrom hard case via "rauchableitung" in
   the LV fine print (the sub-scope risk (b) the phase-8 spec left
   open). The operator's witness principle, extended: one coincidence
   in the fine print is a coincidence (borderline, visible); the TITLE
   naming the trade (exact tier), or >= 2 distinct keywords, is a
   conviction. This alone returns hard19 to 19/19 at every K (the
   `any-ev convicts` diagnostic row shows 18/19 without it).

**Receipt (full `--sweep`, real judge() components, 2,473 pos / 25,600
neg / 102,400 vol; `hard19` = original hand-labeled set, `bench` = the
grown 103-case scorecard):**

| configuration | hard19 | bench | recall | leakage | volume |
| --- | --- | --- | --- | --- | --- |
| embedding gate (live) | 18/19 | 47/103 | 44.5% | 1.9% | 4.6% |
| evidence 0.55, K off | 19/19 | 52/103 | 36.5% | 1.1% | 2.1% |
| **evidence 0.55 + K>=2 (committed)** | **19/19** | **61/103** | **51.5%** | 2.7% | 4.4% |
| evidence 0.55 + K>=3 | 19/19 | 56/103 | 45.0% | 1.6% | 2.9% |
| evidence 0.55 + K>=1 | 19/19 | 65/103 | 55.3% | 3.7% | 5.6% |

Honesty rows (same pass): **visible recall** (pass or borderline — what
the report shows) 74.2% for the evidence gate vs 57.5% embedding;
live-proxy recall (no same-buyer muting) 47.5% at K off. **Decision
(operator priority: recall first, ~2-3% leakage acceptable):
`EVIDENCE_NOMINATION_MIN = 2`.** K>=2 beats the live gate on recall
(+7.0pt), hard19, the scorecard (+14 cases) and volume, at 2.7% vs
1.9% leakage. K>=3 strictly dominates the live gate on every axis and
is the fallback if leakage ever binds; K>=1 is the max-recall option.
`GATE_MODE` remains 'embedding' — the flip is the operator's decision,
now with the evidence gate ahead on recall for the first time.

## Phase 8d — the borderline band: guessing vs reading (2026-08-06)

The operator's objection to the band was structural: *"a customer is only
interested in if it is his business, yes or no"*. The band is not a third
verdict — it is a flagged **no**, the cases where the gate's signals
conflict. The question this phase answers: **what does it buy to guess on
them, and what would it buy to read them?**

**Band composition (from the 8c receipt).** Of the evidence gate's
judgments at K>=2, the band holds **22.7pt of true wins** (visible recall
74.2% − pass 51.5%) and **15.6pt of wrong-trade lots** (visible leakage
18.3% − 2.7%). Admitting the band with probability *p* therefore moves in
a straight line, with no free lunch:

    recall(p)  = 51.5% + p * 22.7
    leakage(p) = 2.7%  + p * 15.6
    volume(p)  = 4.4%  + p * 15.7

**Every recall point costs ~0.69 leakage points.** An uninformed policy
can only slide along this line; it cannot bend it. Random guessing is
strictly dominated by the measured rules — K>=1 delivers 55.3% recall at
3.7% leakage, while a coin tuned to the same recall (p≈0.17) leaks 5.3%.

**Chosen operating point: `BORDERLINE_ADMIT_P = 0.375`** — the smallest
*p* clearing the operator's 60% recall bar. The draw is a deterministic
hash of the lot identity (`relevance._band_draw`), not a live random
number: the same lot always gets the same verdict, so reports and
receipts are reproducible and a lot never flips between runs. Measured
against the prediction (full `--sweep`, same corpus as 8c):

| configuration | hard19 | bench | recall | leakage | volume |
| --- | --- | --- | --- | --- | --- |
| predicted at p=0.375 | — | — | 60.0% | 8.5% | 10.2% |
| **measured (committed)** | **15/19** | 63/103 | **60.4%** | **8.6%** | **10.3%** |
| K>=2, no band admit (8c) | 19/19 | 61/103 | 51.5% | 2.7% | 4.4% |

The linear model predicted the outcome to a tenth of a point — the band
arithmetic is sound.

**The cost, stated plainly: the coin overturns four of the operator's own
hand-labeled rejections.** The hard set drops 19/19 → 15/19, and the four
casualties are not incidental — they are the cases that *founded* this
phase:

- `Gebäudewirtschaft / SZ-Nord` — the Trafostation wearing the customer's
  Blitzschutz code (phase 7's origin case)
- `Versorgungsbetriebe Bordesholm` — the Batteriespeicher, hard code 1.0
- `Schwachstrom - Kreishaus` — **the lot whose operator rejection created
  the evidence gate in the first place**
- `Starkstrom - Kreishaus` — its sibling

No value of *p* repairs this. These lots sit in the band precisely
because the mechanical signals conflict, and a blind draw admits its
share of them regardless of the bar. **This configuration violates the
standing ship rule** ("a configuration that misjudges any benchmark case
is rejected regardless of its aggregate numbers"). It is committed as a
measured data point and an interim placeholder, not as a recommendation;
`GATE_MODE` remains `'embedding'`, so nothing reaches a customer. Setting
`BORDERLINE_ADMIT_P = 0` restores 19/19 and the 8c numbers exactly.

**Documented intent: the LLM judge replaces this coin.** The band is
where word-matching runs out of information, and no *uninformed* policy
improves the frontier — only new information does. Reading the lot's
title + Leistung is that information, and it is the same act the operator
performs by hand. A reader with accuracy *a* on the band gives
`recall = 51.5 + a*22.7` and `leakage = 2.7 + (1-a)*15.6`:

| policy on the band | recall | leakage |
| --- | --- | --- |
| reject all (8c) | 51.5% | 2.7% |
| coin at p=0.375 (committed) | 60.4% | 8.6% |
| reader at 90% accuracy | ~71.9% | ~4.3% |
| perfect reader (ceiling) | 74.2% | 2.7% |

A reader beats the coin by ~12 recall points **and** ~4 leakage points
simultaneously — the only move measured so far that improves both axes at
once. Validation needs no new labeling: ~560 band lots are firms' own
wins (ground truth "in") and ~4,000 are off-class trusted lots (ground
truth "out"), so the judge's accuracy is measurable against the existing
corpus before it ever decides a live lot. The one genuine label gap is
same-CPV-class wrong trades (Blitzschutz vs Starkstrom), which the code
proxy cannot see — a few hundred hand-read, operator-audited labels close
it. Cost is not the constraint: at ~50 band lots per customer per week
and ~1,650 tokens per judgment, a weekly run is cents per customer, and
the one-time validation pass is tens of dollars.

## Phase 8e — the flip: `GATE_MODE = 'evidence'` (operator decision 2026-08-06)

The evidence gate is now the **live** gate. `GATE_MODE`'s committed
default is `'evidence'`, so the scheduled `loop.py` run picks it up with
**no change to how cron calls it** — that was the operator's constraint.

**Shipped configuration**: nomination bar 0.55, witness rule **K>=2**,
band guess **off** (`BORDERLINE_ADMIT_P = 0`).

| | evidence gate (live) | embedding gate (replaced) |
| --- | --- | --- |
| recall (LOO) | **51.5%** | 44.5% |
| leakage | 2.7% | **1.9%** |
| volume | 4.4% | 4.6% |
| hand-labeled hard set | **19/19** | 18/19 |
| grown benchmark | **61/103** | 47/103 |

### The K>=2 vs K>=3 call

The aggregates alone don't settle it — K>=3 leaks less (1.6%) and K>=2
recalls more (51.5% vs 45.0%). **What settles it is which cases separate
them.** Measured per case on the 103-case benchmark:

- The two configurations are **identical on every wrong-trade case**:
  28/32 each. K>=3 buys **no** additional precision against operator
  judgment.
- **All 5 differing cases are true wins**, and K>=2 catches all 5 that
  K>=3 rejects (in-cases 33/71 vs 28/71).
- All 5 are **same-buyer template wins** (similarity 0.86–1.00) carrying
  **exactly 2 witnesses** — AVS's Verkehrsführung lot, Achatz's
  Gleis-/Tiefbau, B+H's Eisenbahnüberführung, Bahnbau Weidlich's
  TK-Strecke, Robert Seidel's Schlosser lot. That is precisely the
  starvation class the witness rule was invented to rescue (the Ahle
  carpentry autopsy, phase 8b); K>=3 re-starves it.

So K>=3's lower leakage is visible only against **synthetic** off-class
CPV negatives, never against a lot a human actually read. K>=2 ships.
K>=3 remains the documented fallback if leakage binds in production.

### What "volume" is for (and why it has no target)

Volume is **not a quality metric and has no good value in itself**. It is
the report-size gauge: the share of the whole market an average profile
admits, i.e. how long the weekly list is *before* the competition model
filters it for lonely lots. Read it as a two-sided sanity band:

- **too low** → weeks with an empty report; the customer asks what they
  are paying for
- **too high** → the report hands the filtering work back to the customer,
  which is the thing the product exists to do for them

Its job is to be read **alongside leakage**: leakage says whether what is
in the report belongs there, volume says whether there is enough of it. A
gate can look excellent on leakage while admitting almost nothing.

For this flip volume is the *reassuring* number: **4.4% against the
outgoing 4.6%** — report size is essentially unchanged, so customers see
no shift in how much they receive, only in what. That is also the second
argument against K>=3, whose 2.9% would have cut reports by about a third
at a moment when they are already short (the pilot currently matches 0–1
lots per cycle; verified under both gates with `tryout.py` — the evidence
gate matches 1 where the embedding gate matches 0).

### The band guess is off

Phase 8d measured admitting the borderline band with probability 0.375:
it hit the 60% recall target (60.4%) but tripled leakage to 8.6%, doubled
volume to 10.3%, and **overturned four hand-labeled operator rejections**
(hard set 15/19), including the Kreishaus Schwachstrom lot whose
rejection founded the evidence gate. For a product whose value is a short
trustworthy list, that trade runs backwards — a missed lot is invisible
to the customer, a wrong-trade recommendation is not. The band stays a
flagged **no**, and the recall it holds is left for the LLM judge, which
the receipts show beats the coin on *both* axes (see phase 8d).

### Rollback

One constant: `GATE_MODE = 'embedding'` restores the previous gate
exactly (tag `gate-v1-embedding`) — no retraining, no rebuild, sidecars
and champion model untouched. `GATE_MODE=embedding python loop.py run`
does it for a single run without a commit.

## Phase 9 — learned references: the customer's own wins (implemented 2026-08-06)

**The label nobody has to judge.** Every earlier phase argued about
whether a lot is a customer's business. An award settles it: they bid on
it and they won it. Revealed preference outranks any signal the gate
computes, so a win becomes a profile reference — **including the wins the
gate rejected** (operator decision 2026-08-06: *"if a customer wins a
tender that we believe it is not his business, we should add it"*). Those
are the false negatives: the recall gap made visible, one lot at a time,
and they are recorded with `gate_verdict: "out"` and named in the cycle
output.

**Derived data, not operator intent.** Learned references live in
`data/ledger/learned_refs.jsonl` and never in `subscriptions.jsonl`. That
keeps the versioning contract clean: a subscription version continues to
mean *the operator decided something*, not *another week passed*. The
ledger is append-only with a `learned_at` stamp — so "which profile
produced this pick" stays exactly answerable, the same guarantee the
delivery ledger already relies on — and it is disposable:
`python feedback.py --rebuild` regenerates it from `awards.parquet`.

**Time isolation is fail-safe, by construction.** `relevance.Gate(dir)`
with no `as_of` loads **no** learned references at all. Every pre-existing
caller — `playback.py`, `backtest.py`, `replay.py`, `explain.py`, the
receipt harnesses — therefore keeps precisely the behaviour it had, and a
replay path that forgets its cutoff *understates* the profile. It cannot
pull a future win into a historical world, which is the one direction
that would silently flatter every backtest ("we predicted the win" using
knowledge of that win). Only `Gate(dir, as_of='YYYY-MM-DD')` unions
references with `learned_at <= as_of`; `loop.py` passes today.

**Error handling is deliberately asymmetric.** An operator-written
`profile_ref` that does not resolve in the sidecar still raises — a typo
in a customer's line must never be a silent skip. A *learned* ref that
does not resolve (store rebuilt, sidecar lagging) is skipped instead: a
feedback record must never break a customer's delivery.

**Verified** (no writes to `data/`): the awards join recovers all 6 pilot
wins and proposes 0 new ones (all already referenced); a held-out
reference is rediscovered; `as_of=None` → 0 learned refs; a ref stamped
one day *after* the cutoff is excluded; an unresolvable learned ref is
skipped without a crash; and `--judge-benchmark` is unchanged at 47/103
(embedding) / 61/103 (evidence).

**Wired into the cycle** as step 4c, after `report()` and before
`deliver()`, so the same run that learns a win already delivers against
the widened profile. It never fails a cycle. Standalone:

```
python feedback.py --list      # the ledger    --as-of D  reconstructs it
python feedback.py --dry-run   # what would be learned, writes nothing
python feedback.py --learn     # discover + append
python feedback.py --rebuild   # regenerate from awards.parquet
```

**Open, deliberately.** (1) `award_names` is read from the subscription
but no line carries it yet, so matching falls back to the display `name`
— exact-string against `winner_names`, which holds entries like
`KG Robert Seidel GmbH &amp; Co.`; a silent mismatch means a customer
never learns, so `--dry-run` should be checked per customer before
relying on it. (2) Profiles grow without bound; capping is a union-time
decision (keep newest N), never a record-time one, and stays unbounded
until a receipt shows growth moving leakage. (3) False negatives are
printed but not yet surfaced in the weekly operator report.

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
8. **The evidence gate** — section splitter, per-profile trade lexicon,
   the committed 16-case operator benchmark, configuration L, then the
   simplified four-rule ladder with the guess channels (soft
   fingerprint, phases 5 and 7 machinery) deleted. *Embeddings
   nominate, evidence convicts; the gate gets simpler and every verdict
   becomes quotable.*

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

## Phase 8i — similarity no longer nominates (operator decision 2026-08-07)

Route 2 of the nomination ladder — "this lot's text resembles the
customer's past tenders" — is off (`SIMILARITY_NOMINATES = False`).
Nomination is now the CPV hard-code match or the trade-evidence witnesses;
no part of the evidence ladder reads the buyer name.

**The decisive argument is that the test never exercised it.** The positive
cases are the firm's OWN past wins, which come from buyers already in its
profile, so `same_buyer` is true and route 2 is muted for most of them.
Production is the mirror image: open lots come overwhelmingly from buyers
the firm has never won from, so route 2 is on. We measured a gate with this
route disabled and shipped one with it enabled — including when
`NOMINATION_BAR` itself was chosen, which is the knob that controls it.

Three supporting reasons. (a) Its safety catch is a string comparison:
`same_buyer` is buyer-NAME equality, so "SBH | Schulbau Hamburg" and "GMH |
Gebäudemanagement Hamburg GmbH" are different buyers and a shared municipal
template passes — the Norderschulweg heating lot (GMH) reached a FLOORING
firm whose six references are all SBH. It guards against one buyer
repeating itself, not against a family of authorities sharing a template,
which is the common German case. (b) It contradicts phase 8's founding
decision: similarity was demoted because it cannot be trusted to CONVICT,
yet it was still trusted to decide who gets considered — and the evidence
test behind the door is satisfiable by coincidence ('dehnfug' in a
water-reservoir spec). (c) The bar sweep moved leakage 0.4% → 1.6% → 6.0%
as the bar dropped 0.70 → 0.55 → 0.40; that range is similarity admitting
lots.

**Receipt (`lexicon_receipt.py --config both`, 122 hand-labeled cases):**

| route 2 | IN (should pass) | OUT (should reject) | total |
| --- | --- | --- | --- |
| on (rollback) | 29/74 | 45/52 | 74/126 |
| **off (shipped)** | 26/74 | 45/52 | 71/126 |

Read this honestly: removal costs 3 recall cases and buys **no measured
precision**. That is not evidence the route was harmless — it is the same
blind spot the decision is about. Route 2's harm lands on the ~3,000 open
lots scored per production cycle, where it is on for nearly all of them;
the benchmark's 52 rejection cases cannot see it. Its harm is also now
partly masked by phase 8g: a wrongly nominated lot still needs convicting
trade evidence, and the cleaned vocabulary usually denies it — which is why
the polat picks that shipped in August no longer pass either way. The
decision is made on the logic, not on this table.

Known cost, unrepaired: route 2 uniquely rescued the lot whose TITLE names
the trade with a single keyword, since the witness rule demands
`EVIDENCE_NOMINATION_MIN` of them. The direct repair — let a title witness
nominate, as conviction already treats it as sufficient — is measured
separately, deliberately not folded into this decision. The 3 lost cases
are the expected class.

Rollback: `SIMILARITY_NOMINATES=1`.

## Phase 8j — the store-rarity sieve is not a recall lever (measured 2026-08-07, NOTHING SHIPPED)

A negative result, recorded so it is not re-run.

**Hypothesis.** `MAX_DOC_FREQ` (2%) deletes a trade's own name for exactly
the biggest trades — the constants block records malerarbeiten 2.0%,
lüftung 2.1%, sanitär 2.6%, heizung 2.6%, abbruch 4.8% — because a trade's
name is common in proportion to its market share. Firms in those trades
should therefore be starved of their core word, and with phase 8g's
vocabulary now vetting words, the rarity sieve should be waivable for
approved ones.

**Result (`lexicon_receipt.py --config both`, 122 hand-labeled cases):**

| arm | IN (should pass) | OUT (should reject) | total |
| --- | --- | --- | --- |
| **cut 2% (shipped)** | **26/74** | **45/52** | **71/126** |
| cut 5% | 26/74 | 41/52 | 67/126 |
| cut 10% | 26/74 | 41/52 | 67/126 |
| cut 50% | 26/74 | 41/52 | 67/126 |
| waived for `names_trade()` words | 26/74 | 41/52 | 67/126 |

**Recall does not move at all** — identical in every arm, including with the
sieve effectively removed. Only precision moves, and only downward. It
saturates already at 5%, so this is not a matter of degree.

**Why the hypothesis was false.** Phase 8c had already solved it, twice.
(1) The definitional waiver admits a word from the profile's own trusted
CPV labels regardless of store frequency. (2) The trade dictionaries derive
each trade's vocabulary from all lots carrying the code via a two-sided
in/out ratio (`DICT_MIN_IN` / `DICT_MIN_RATIO`) which has **no corpus-rarity
cap at all** — so `heizung` already reaches a heating firm's lexicon through
its trade dictionary, and `abbruch` a demolition firm's. The firm-side
sieve was never what blocked them. The phase-8c comment *describing* the
problem was mistaken for the problem still being open.

**Standing.** `MAX_DOC_FREQ` stays at 0.02. It is a precision instrument,
not a recall one, and loosening it costs 4 rejection cases for nothing.

**`MIN_STEM_LEN` stays too**, for a separate reason that no vocabulary can
cover: it guards the MATCHING step, where a lexicon word is substring-
matched against tender text. A four-letter `glas` would fire on "Glasgow",
`dach` on "Obdachlosenunterkunft". The roots file's exception lines filter
lexicon membership, not text matches, and word boundaries are not available
as a fix because German compounds require substring matching in the first
place (`Flachdach` carries the root mid-word).

**Where recall actually has to come from**, after this: vocabulary coverage
for the trades the list does not yet reach, then title-witness nomination
(phase 8i's known unrepaired cost), then `EVIDENCE_NOMINATION_MIN` 2 -> 1
once a clean vocabulary has changed what a witness means.

## Phase 8k — what convicts may also nominate (shipped 2026-08-07)

The gate already held that a trade keyword in the TITLE convicts on its own
(`evidence.convicts`, the title-or-two rule). Nothing let that same fact
open the door, so a lot could be convictable and never considered. This
closes the gap: conviction-strength evidence nominates itself.

**The case that showed it** — `00367721-2025`, *"Estricharbeiten"*
(Thüringer Landesamt für Bau), against the screed firm N3Bau. Before:

    verdict ok=False borderline=True  text=0.588 | hard 0.135 | evidence: estrich(t1)

Route 1 fails because the buyer filed it under CPV 45216111 *Bau von
Polizeirevieren*, so `hard` is 0.135 against a bar of 0.825. Route 3 fails
because the description is an address — *"Neubau Polizeiinspektion
Saale-Orla, Hofer Str. 54, Schleiz"* — leaving `estrich(t1)` as the single
witness where the rule wants `EVIDENCE_NOMINATION_MIN`. Route 2 is gone
since phase 8i. So a tender whose title IS the firm's trade was rejected —
and it is exactly the case the evidence gate exists for, since the CPV code
names the building and only the text names the work. After: `ok=True`.

**Receipt (`lexicon_receipt.py --config both`):**

| configuration | IN (should pass) | OUT (should reject) | total |
| --- | --- | --- | --- |
| phase 8i (route 2 removed) | 26/74 | 45/52 | 71/126 |
| **+ conviction nominates** | **34/74** | **45/52** | **79/126** |

Eight recall cases at **no measured precision cost** — the only free move in
this sequence. Why it does not reopen what phase 8i closed: route 2 failed
because whole-document similarity is dominated by a buyer's standard
paragraphs, so it fired hardest on sibling authorities sharing a template.
The title carries no boilerplate; it is the one field that names the actual
work. The mechanisms are opposites, not variants.

Residual risk: a large package whose title lists several trades
(*"Neubau Schule: Estrich, Maler, Trockenbau"*) now reaches all of them —
arguably correct, since each of those firms can bid the relevant lot.

**Standing after phases 8f-8k**, against the gate as shipped in `50eaca2`:

| | IN | OUT | total |
| --- | --- | --- | --- |
| base (phase 8e) | 36/74 | 30/52 (58%) | 66/126 |
| **now** | 34/74 | **45/52 (87%)** | **79/126** |

Near-parity on recall, wrong-trade rejection from 58% to 87%. Rollback:
`CONVICTION_NOMINATES=0`.

## Phase 8n/8o — the trade recurs, the context varies (shipped 2026-08-07)

**First, a structural finding.** Since phase 8k, `convicting` implies
`nominated`, so `nominated and convicting` reduces to `convicting` alone.
**Nomination no longer decides anything** — not the CPV code match, not the
witness rule, not similarity. Only conviction moves the receipt. Phase 8n
(a wide root lexicon feeding nomination) was built and measured before this
was understood: it changes no verdict at all, and is kept only because a
lot with wide witnesses and no narrow evidence lands in the visible
borderline band instead of being rejected outright.

**Phase 8o, at conviction.** A firm's won tenders describe the work AND its
context — what it sits on, connects to, replaces — so a guardrail firm's
texts carry `beton` (the posts are set in it), `bohr` (the holes) and
`rueckbau` (the old barrier). Those are trades MENTIONED, not the trade the
firm IS. A lexicon of every root mentioned matches far too much: measured
alone it gave IN 60/74 but OUT 27/52.

Recurrence separates them without judging what the words mean: **the trade
appears in every win, the context only in some.** `core_keywords()` keeps
the roots present in at least `CORE_SHARE` of a firm's references, and a
core root in the TITLE convicts. The title is where the work is named —
boilerplate lives in the description — so it is the safe place to admit
evidence the firm's narrow lexicon happens to lack.

| CORE_SHARE | IN (should pass) | OUT (should reject) | total |
| --- | --- | --- | --- |
| off | 34/74 | 45/52 | 79/126 |
| **0.5 (shipped)** | **44/74** | **45/52** | **89/126** |
| 0.75 | 42/74 | 45/52 | 87/126 |
| 1.0 | 42/74 | 45/52 | 87/126 |

Ten recall cases at **no precision cost**, and precision is identical at
every threshold — a firm's core root appearing in a lot's title really does
mean the lot is their work.

**Standing against the gate shipped in `50eaca2`:**

| | IN | OUT | total |
| --- | --- | --- | --- |
| base | 40/74 | 30/52 (58%) | 70/126 |
| **now** | **44/74** | **45/52 (87%)** | **89/126** |

Better on both axes for the first time in this sequence: recall above base,
wrong-trade rejection 58% -> 87%. Rollback: `CORE_TITLE_CONVICTS=0`,
`WIDE_NOMINATION=0`.

## Dead ends — measured, do not repeat (register, 2026-08-07)

Everything below was built, measured through the real `judge()` on
`benchmark_relevance.jsonl`, and then reverted or kept only for form. The
detail sits in the phase sections above and in the commit messages; the
one-line verdict sits here, because the expensive part of phases 8f-8o was
not measuring these — it was re-deriving them.

**Read the two directions separately.** `lexicon_receipt.py` reports IN
(should pass) and OUT (should reject) apart for a reason: the committed
cases are recall-heavy, so a change that stops wrong-trade picks looks like
a regression in the total while doing exactly what it was built for. Every
row below is quoted as `IN/74, OUT/52`, never as a total.

1. **Relaxing `MAX_DOC_FREQ`** (2% -> 5%, 10%, 50%, or waived for
   `names_trade()` words). Recall is **identical in every arm**, including
   with the sieve effectively removed; only precision moves, and only
   downward (45/52 -> 41/52). It saturates at 5%, so it is not a matter of
   degree. Phase 8c had already solved the problem twice — the definitional
   waiver and the trade dictionaries, neither of which has a corpus-rarity
   cap. Full receipt: phase 8j.

2. **Buyer-diversity proxies in place of reading the word.** The
   single-buyer rule (8f (B)) was withdrawn in 8h: every word it removed
   fails `names_trade()` anyway, and it cost 5 recall cases by stripping
   one-buyer profiles of real trade words. The dictionary buyer-share test
   (8f (A)) is measured **redundant** — the `roots only` arm ties `both`
   to the digit (29/74, 45/52 each at the time) — and survives only because
   it costs nothing. The principle: judge the word, not a statistic
   standing in for the word.

3. **Similarity nomination** (route 2). Removed in 8i, and not because of
   its table (removal cost 3 IN cases and bought no measured OUT): the
   benchmark **cannot see** it, since the positives are the firm's own wins
   from buyers already in the profile, where `same_buyer` mutes it.
   Re-enabling it re-opens a route whose only safety catch is buyer-NAME
   string equality.

4. **The embedder cannot supply the vocabulary.** Measured against a noise
   floor of 0.133, trade synonyms score 0.29-0.89 and only 2 of 12 clear
   the 0.80 bar; the misses are exactly the regional names that matter
   (anstreicher/maler 0.289, spengler/klempner 0.395, schreiner/tischler
   0.525). Material-to-trade is at noise (linoleum/bodenbelag 0.108) and
   nearest-neighbour lookup returns schreiner -> Faulbehälter. Receipt in
   `50eaca2`. `cpv_trade_roots.txt` is written by hand and stays that way.

5. **Adding truncated roots to compensate for stemming.** When
   `names_trade()` ran after `stem()` it asked whether `ortbeto` contains
   `beton`. The tempting fix — 42 truncated roots (`beto`, `flies`,
   `zarg`) — was rejected: they are not words and they misfire, `flies`
   matching Fließestrich and handing a screed lot to a tiling firm. The
   real fix was the **order** of the check (vet the raw word inside
   `trade_dictionaries`), which needs no new roots. Phase 8l.

6. **More vocabulary, past ~500 roots.** Two independent expansions
   (~235 roots between them) moved **one** firm on `--coverage` and
   **nothing** on the benchmark. The list is worth completing for its own
   sake; it is not a recall lever any more, and "add roots" is no longer an
   answer to a recall gap without a receipt naming the firms it unblocks.

7. **Anything that only touches NOMINATION.** Since phase 8k `convicting`
   implies `nominated`, so `nominated and convicting` reduces to
   `convicting` alone — the CPV hard-code match, the witness rule,
   similarity and `keywords_wide` are all inert. Phase 8n was built before
   this was understood and changes no verdict at all. Verify it in
   `relevance._evidence_verdict` before spending effort anywhere but
   conviction.

8. **`CORE_SHARE` above 0.5.** 0.5, 0.75 and 1.0 all give OUT 45/52, and
   0.5 is strictly best on IN (44 vs 42). Raising it buys nothing.

9. **Letting a word into a lexicon on one mention** (no recurrence
   requirement at all — `WORD_MIN_REF_SHARE=0`, which is also what removing
   the old word-level buyer rule leaves behind if nothing replaces it):
   IN 49/74 but OUT **38/52**, five recall cases bought with seven
   wrong-trade rejections. A firm's tender text mentions many trades and
   names only one; something must separate them. See phase 8q — the
   separator is recurrence over references, and it must not be spelled as
   a buyer rule.

   Corollary for the register's own sake: **"buyer diversity is a proxy
   the vocabulary replaces" (dead end 2) is true of the profile-level rule
   and false as a generalisation.** 8f (B) — a one-buyer firm contributes
   no words — was genuinely subsumed. The word-level rule was not
   redundant; it was carrying real precision, just under the wrong name.
   Withdrawing it without a replacement costs 9 rejection cases.

## Phase 8p — the empty lexicon is a TRUST gap, not a vocabulary gap (read 2026-08-07)

`--coverage` reported 91 of the 512 firms with >= 3 wins left with no
convicting lexicon once the vocabulary is on, ~46 of them having had words
before it. Two causes were on the table and no number could separate them:
(a) their trade is missing from `cpv_trade_roots.txt`, or (b) their texts
are genuinely boilerplate carrying no trade vocabulary, in which case empty
is CORRECT and convicting on `derzeit` would be worse. So the words were
dumped and read — `lexicon_receipt.py --empty`, which prints each firm's
dropped words beside its core and wide root lists.

**The reading found a third cause that subsumes both, and it separates
perfectly:**

| | firms | of which empty |
| --- | --- | --- |
| has >= 1 **trusted** CPV code | 261 | **0** |
| has none | 251 | **91** (36%) |

Not a correlation — a partition. A firm with a trusted code inherits that
trade's dictionary (derived from *all* store lots carrying the code) and
its definitional label words, and **not one such firm ends up empty**. A
firm with no trusted code has exactly one source for its narrow lexicon:
its own handful of reference texts, through a sieve demanding >= 6
characters, two buyers, <= 2% store frequency *and* a trade root. The
vocabulary is only ever the last of four filters to reject a word, and it
can only starve a firm that has no dictionary to fall back on. **The
question "which trades are missing from the roots file" was the wrong
question.**

**What the dropped words actually are.** Of the ~46 firms that lost words,
about half lost nothing but

- *procurement geography* — `regionalbereich unterregion hamburg berlin
  münchen nürnberg südost leipzig dresden`: seven firms' entire
  pre-vocabulary lexicon was the Deutsche Bahn tender template's place
  names (Possehl Spezialbau's fourteen words are geography, end to end);
- *contract language* — `ausführungsbeginn einzelfrist ausführungsende
  bauzeit vorhanden ausgeschrieben baubegleitend`;
- *building types and rooms* — `klassenräume gemeinschaftsschule fachräume
  mittelschule hochschul institut`;
- *project names* — Elektro Böhe's five words are `institut lörrach
  hochschul markgräflerland rheintal`.

For those, empty is the correct answer and the vocabulary did its job.
Three of them (Hohenloher Schuleinrichtungen, Laborbau Systeme Hemling,
Siemens Healthineers) are not construction trades at all — school
furniture, lab furniture, medical imaging — and have no Gewerk to name.

**The genuine vocabulary gaps, in full, so nobody re-derives them:**
`lichtsignalanlage signalgeber` (traffic-signal engineering, YUNEX — the
root `ampel` is present but the trade's own word is not), `taktil
leitsystem` (tactile guidance, HTI), `landschaftsbau` and bare `pflanz`
with the nursery vocabulary `sträucher hochstämme heister mulchen` (Rudolf
Schrader, August Fichter, ML Gartenplus, Ringbeck — `landschaftsgaertner`
and `pflanzarbeit` are roots, neither matches `landschaftsbauarbeiten`),
`modulbau` (KLEUSBERG), `kläranlage reinigungsstufe` (Heberger), `erdbau`
(Schleith — `erdarbeit` is a root, `erdbau` is not), `rohrrahmen`
(MB Lauterbach), `trennvorhang` (Metallbau Politz), plus two mechanical
misses of the class `044fdc5` set out to fix and did not: `trennwaend`,
the umlaut plural of the existing root `trennwand` (Kemmlit, twice), and
`aussentuer` (ASGE), since bare `tuer` is refused for colliding with
*natürlich*.

**None of it is worth adding on today's evidence.** 59 of the 91 already
carry core roots, so a core root in a lot's title convicts them regardless
(phase 8o); of the firms with a genuine gap, all but about seven are in
that group — ASGE already convicts on `tueren fassade metallbau innentuer`,
Schleith on `beton leitung erdarbeit`, TK Aufzüge on `aufzug`. And dead end
6 stands: two expansions totalling ~235 roots moved one firm on coverage
and nothing on the benchmark.

**Where the lever actually is, if this matters:** the trust side, not the
roots file. 251 of 512 repeat winners carry no trusted deep code, and that
single fact — not the vocabulary — is what decides whether a firm gets a
lexicon at all. `trust_min_lots` (10) and `trust_cohesion_min` are the
parameters; both are recomputed per `model_tag` and both were set for
labelling clean negatives, never for feeding lexicons. That is a receipt
nobody has run.

### Then why THOSE 91? The word-level buyer rule, measured

The trusted code explains why a firm has nothing to fall back on. It does
not explain why its own texts yield nothing. `--empty` therefore attributes
every rejected word to the first filter that killed it — `derive_keywords`
takes an optional `reasons` dict, so the attribution is of the shipped
sieve rather than a replica. Over the 45 firms that were empty *before* the
vocabulary existed:

| filter | words rejected | firms it bit |
| --- | --- | --- |
| **one buyer** (`MIN_BUYERS`) | **3,262** | 45/45 |
| too short (`MIN_STEM_LEN`) | 1,308 | 45/45 |
| store-common (`MAX_DOC_FREQ`) | 72 | — |
| buyer name | 49 | — |

Seventy per cent, one rule — and not the rarity sieve or the vocabulary,
the two suspects this project had spent phases on. That rule is withdrawn
in phase 8q below.

### Correction: the committed `--coverage` table was contaminated

`data/trade_dicts.json` keyed its cache on the store size, the cache
version and `BUYER_DIVERSITY` — but not on `TRADE_ROOTS`. Both arms of a
vocabulary A/B therefore reused whichever arm's dictionaries happened to be
on disk, and measured the same lexicons twice. Found because `--coverage`
stopped reproducing while the empty counts (45/91) reproduced exactly:
those firms have no trusted code, hence no dictionary, hence nothing for
the contamination to reach. The key now carries `tr`. The `off` row was
right; the `on` row had been published as median 4 / mean 7.0 / 207 under
3, and is honestly median 3 / mean 5.5 / 224 under 3. The live gate was
never affected — its defaults are the ones the on-disk cache was built
under. Only the A/B was wrong, and only by understating what the vocabulary
removes.

## Phase 8q — recurrence, not buyers (operator decision 2026-08-07)

The word-level buyer rule is **withdrawn**, on the rule's own terms rather
than on a receipt: *"the rule itself makes no sense"* (operator). It
required each word to appear under two of the firm's buyers, and

- it **silently overrode `MIN_WITNESSES = 1`**. Phase 8b lowered the
  witness count to 1 to rescue the starved 2-wins-per-trade profiles (the
  Ahle carpentry autopsy). For any firm with two or more buyers the buyer
  rule reimposed a two-reference minimum, so that decision was never in
  force where it was aimed;
- it was the **largest single cause of empty lexicons** (the table above);
- and what it actually measured was **recurrence** — a word appearing under
  two buyers is a word that recurs across projects — which is phase 8o's
  distinction (*the trade recurs, the context varies*) wearing a buyer's
  name. Buyers were a proxy for the number of projects, and the number of
  projects is available directly.

So recurrence is now stated directly: a word must appear in
`WORD_MIN_REF_SHARE` of the firm's references.

| rule | IN (should pass) | OUT (should reject) | total |
| --- | --- | --- | --- |
| word-level buyer rule (withdrawn) | 44/74 | 45/52 (87%) | 89/126 |
| no rule at all (`share 0`) | **49/74** | 38/52 (73%) | 87/126 |
| **share 0.34 (shipped)** | **44/74** | **47/52 (90%)** | **91/126** |
| share 0.5 | 44/74 | 47/52 (90%) | 91/126 |
| share 0.75 | 42/74 | 47/52 (90%) | 89/126 |

**Recall unchanged, two more wrong-trade rejections** — the coherent rule
is also the better one, which is not something this sequence has been able
to say often. 0.34 and 0.5 tie because they differ only above four
references (both demand 2 of 3 and 2 of 4, and most firms have three or
four wins); 0.34 ships as the more permissive of the two.

**Disclosed cost.** Coverage gets *worse*: 91 firms with no narrow lexicon
becomes 103, and 40 rather than 45 were already empty before the
vocabulary. That is the right trade and phase 8p is why — lexicon size is
not the objective, 60 of the 103 still convict through a core root in a
title, and the gate is better on both benchmark axes. It also breaks the
clean partition of 8p by exactly one firm: 1 of 261 coded firms is now
empty, against 102 of 251 uncoded.

**Standing against the gate shipped in `50eaca2`:**

| | IN | OUT | total |
| --- | --- | --- | --- |
| base | 40/74 | 30/52 (58%) | 70/126 |
| phase 8o | 44/74 | 45/52 (87%) | 89/126 |
| **now** | **44/74** | **47/52 (90%)** | **91/126** |

Rollback: `WORD_MIN_REF_SHARE=0` removes the recurrence rule, but note that
this does **not** restore the buyer rule — that code is gone, and the
receipt for taking it out is the table above.

### Open: the mute profile is a different failure from the thin one

`--empty` counts 103 firms with no narrow lexicon, and **43 of them are
fully mute** — no narrow lexicon, no core root, no wide root, not one trade
word anywhere in their reference texts. For those the gate cannot return
yes for any lot, ever. That is categorically different from a thin profile
that scores low: a thin lexicon is a quiet gate, a mute profile is a broken
one, and nothing downstream tells them apart. A customer with a mute
profile receives an empty report every week and no signal anywhere says
why.

Two of the 43 have been read. **YUNEX** was mute because its trade was
missing from the vocabulary — its texts carry `lichtsignalanlag`,
`signalgeber`, `steuergerät`, `knotenpunkt`, every one of which survives
the whole statistical sieve, and `names_trade()` refused them all because
the roots file held `ampel`, the colloquial word, and not
*Lichtsignalanlage*, the word tenders are actually written in. Adding
`lichtsignal` and `signalgeber` gives YUNEX the lexicon
`['lichtsignalanlag', 'signalgeber']` and leaves the benchmark at 44/74,
47/52 — the standard dead end 6 demands before a root goes in: a named
firm, its words read, and a reason they were refused that is not "the list
is incomplete in general". **Braun GmbH** is the opposite and needs
nothing: its 25 surviving words are procurement procedure end to end
(`angebotsfrist`, `nachunternehmer`, `werktag`, `angebotserstellung`) and
refusing them is correct — it convicts through its core roots `pump daemm
leitung` regardless.

The other 41 are unread, and the counts cannot say which kind they are.
Same answer as phase 8p: read them. The neighbours already visible in the
dump suggest railway civil engineering is the next real hole —
`eisenbahnüberführung` and `durchlässe` (Adolf Lupp), `stellwerk` (Grötz)
— but those name structures rather than trades, so whether they belong is
an operator call under the file's own rule, alongside `spundwand`,
`fundament` and `laermschutz`, which sit on the same line.

A mute profile should probably be *detectable* rather than silent — the
profile builder knows at build time that a subscription has no convicting
route at all. That is a report-side change, not a gate change, and it is
not scoped here.

## Phase 8r — the firm says what it is on its letterhead (operator decision 2026-08-07)

Every source the lexicon had read the same thing: text *about projects*.
The firm's own name was never read, and in German construction it states
the trade — Tischlerei Fischer, Metallbau Politz, Elektro Böhe, Reutlinger
Abbruch, KSG Kabel-Signal-Gleisbau. It is the one statement of what the
firm IS rather than what one project happened to contain: self-declared,
identical on every reference, and free of the buyer's house style that
makes tender prose so hard to read. The BUYER's name has been read since
phase 8 and discarded as geography; the winner's never was.

`name_keywords()` takes the trade roots in the name and adds them to the
narrow lexicon and to the core roots — to core because recurrence is a way
of asking *is this the trade or the context*, and a name recurs by
definition.

**Ungated, deliberately** (operator: *"a broad business is better than no
business mentioned at all"*). Where the name says nothing — Braun GmbH,
YUNEX GmbH, PIK AG — it contributes nothing and costs nothing, so there is
nothing to filter for; where it says something broad, broad is what the
firm published about itself.

| | before | after |
| --- | --- | --- |
| IN (should pass) | 44/74 | **45/74** |
| OUT (should reject) | 47/52 | 47/52 |
| firms with no narrow lexicon | 103 | **77** |
| fully mute (no narrow, no core, no wide) | 42 | **34** |
| empty before the vocabulary | 40 | 29 |

Twenty-six firms gain a convicting lexicon and eight profiles that could
never have said yes to any lot now can, for one more recall case and no
precision cost. A trade root appears in 146 of the 512 firms with >= 3
wins.

**Not isolated, disclosed.** The run also carries `aufzueg` and
`trennwaend`, two umlaut-plural entries; they can only reach firms whose
text or name carries *Aufzüge* or *Trennwände* (TK Aufzüge, Kemmlit twice),
so the name source accounts for nearly all of the movement, but the split
was not measured.

**Those two entries are a patch, and the pattern behind them is a dead
end.** `fold()` writes ü as `ue`, so `Aufzüge` becomes `aufzuege`, which no
longer contains the root `aufzug` — a root cannot match its own plural, and
every umlauting German noun has the same hole. Enumerating them one at a
time is dead end 6 with a worse ratio. If inflection ever proves to matter
the fix is one normalisation at match time (strip the umlaut instead of
expanding it), and it needs a store sweep for the collisions it creates
first — `Stück` would fold onto the `stuck` root of Stuckateur, and *Stück*
is in every Leistungsverzeichnis. Not attempted here.

Rollback: `NAME_KEYWORDS=0`.

## Phases 8r-8u — the lexicon stops measuring surface forms (2026-08-07)

Four changes to how a lexicon is built, each from reading firms' actual word
lists rather than moving a threshold. Three ship; the fourth ships dormant.

**8r — the firm's own name.** Every source read text *about projects*; the
name was never read, and in German construction it states the trade.
`Tischlerei Fischer` -> `tischler`, `Metallbau Politz` -> `metallbau`,
`Elektro Böhe` -> `elektro`, `KSG Kabel-Signal-Gleisbau` -> `kabel gleis`.
Ungated by operator decision (*"a broad business is better than no business
mentioned at all"*): where the name says nothing it contributes nothing.

**8s — count ROOTS, not surface forms.** Braun GmbH's four references say
Wärmepumpenanlage, Wärmepumpen and Wärmepumpe — one trade word, three
surface forms. Counted as tokens that is three words with one mention each
and the recurrence rule refused all three; counted as roots it is `pump` in
three references of four. The proof was already in the code: Braun's core
list (root-counted) read `pump daemm leitung` while its narrow lexicon
(token-counted) was empty. Every other list was already canonical;
`derive_keywords` was the last one measuring the surface.

**8u — the pool votes (operator idea).** A dictionary is only as good as the
lots filed under its code. Read for 45261420 *Abdichtungsarbeiten gegen
Wasser*: 17 lots arrive via `cpv_main`, all genuinely waterproofing, and 88
via `cpv_additional` — 84% of the pool — titled "Rohbauarbeiten 3.
Bauabschnitt", "Spraypark", "Baumeisterarbeiten". A procurement lists every
trade it contains as an additional code on each of its lots. `DICT_MIN_IN`
and `DICT_MIN_RATIO` are built for noise and this is not noise.

The fix: wrongly-filed lots are wrong in every direction at once while
correctly-filed ones all say the same thing, so the right trade outvotes the
wrong ones. Each lot votes with the trade roots in its text; the roots
carrying the vote are the code's signature; a lot holding none of them
leaves the pool before any word is counted.

It makes dictionaries **richer**, not smaller — dropping miscoded lots
shrinks the denominator, so a genuine word's in-trade share rises above the
10% bar it had been failing. Landschaftsbau went from `erdarbeit aushub
pflaster` to `rasenflaeche pflanzarbeit fallschutz dachbegruenung sitzstufen
mauern`, derived from 421 lots rather than hand-written.

**Two generic heads removed from the vocabulary.** `leitung` matches
Bauleitung, Projektleitung, Objektleitung — site *management* — and sat in a
large share of all lexicons; `pump` covers Wärmepumpe (HLK),
Abwasserpumpwerk (Tiefbau) and Betonpumpe (equipment) alike. Both violated
the file's own "no generic heads" rule. Replaced by compounds naming an
actual thing. **A third is still open: `schal` matches Schaltschrank,
Schalter and Schallschutz.**

**Receipt.**

| | master | shipped here |
| --- | --- | --- |
| firms with no lexicon | 77 | **33** |
| under 3 words | 240 | **133** |
| IN (should pass) | 45/74 | **50/74** |
| OUT (should reject) | **47/52 (90%)** | 45/52 (87%) |

Not free: five recall cases and half the starved firms, for two rejection
cases. Read the lexicons for what it bought — Braun `daemm waermepump`,
YUNEX `signalgeber lichtsignal`, Hohenloher `einbaumöbel schreinerarbeit
tischler` (school furniture, previously the roof and concrete of the
building it furnishes).

### 8t — dictionaries beyond `trusted`: right, and SHIPPED DORMANT

A dictionary was built only for a code marked *trusted*, which means the
code's lot texts sit close together in embedding space — a test invented to
certify clean negatives during calibration, reused as a gate on vocabulary,
about which it says nothing. Landschaftsbau scores low because its projects
are parks, schools and roadsides; its vocabulary is perfectly sharp.
Measured: 212 deep codes, 56 trusted, 47 dictionaries built; 135 codes carry
>= 20 lots but only 33 of those are trusted. The 102 excluded are the major
trades — Metallbau 702 lots, HLK 579, Rohbau 513, Elektroinstallation 499,
Fassade 443, Landschaftsbau 421, Bahnbau 347, Aufzüge 245.

Opening it takes empty lexicons from 33 to 3. It also costs **nine**
rejection cases (OUT 45/52 -> 36/52, voting held on in both arms), because
a firm's code set has the same defect the pool had. Heberger — three won
tenders in wastewater plants — inherits seven dictionaries spanning fire
alarms, building automation, lightning protection and structural concrete:
24 words, and it convicts on all of them. The procurement's `cpv_additional`
lists every trade it contains, so as the pool absorbed lots that were not
its trade, the firm absorbs codes that are not its trade.

So `DICT_TRUSTED_ONLY` ships **on** and the work sits behind it. Turn it off
once a firm's trade is taken from the `cpv_main` of the lots it actually
won.

**Isolation grid** (`lexicon_receipt.py --config both`), the reason each
change can be judged separately:

| root lexicon | trust gate open | voting | IN | OUT |
| --- | --- | --- | --- | --- |
| — | — | — | 44/74 | 47/52 |
| ✓ | — | — | 50/74 | 45/52 |
| **✓** | — | **✓** | **50/74** | **45/52** |
| ✓ | ✓ | — | 54/74 | 37/52 |
| ✓ | ✓ | ✓ | 54/74 | 36/52 |

Note what this says about voting: it buys **no** precision anywhere and
costs one case where the gate is open, even though it visibly improves the
word lists. Pool contamination was not what drove the loss — firm-level code
inheritance is. The two are different problems and were conflated once.

**A trap that caught this work three times.** `trade_dicts.json` keyed its
cache on some but not all of the switches the dictionaries are derived
under. Adding a field is not enough on its own: `bool(d.get('to'))` is False
for a cache written before the field existed, which is exactly what the new
default carries, so a stale cache matches the key and is silently reused.
Every change to what the dictionaries contain must bump `DICT_CACHE_V`.

**New tools.** `evidence.py --keywords FIRM` tags each word with its SOURCE
(own text / CPV label / dictionary <code> / firm name) — the merged list hid
the one thing worth reading. `evidence.py --dict CODE` reads a dictionary's
pool split by `cpv_main` vs `cpv_additional`. `evidence.py --pairs` measures
what the embedder scores on INFLECTION (0.88-0.96, passes) against SYNONYMS
(0.29, fails) and against a max-over-document noise floor — the first
receipt `SYN_THRESHOLD = 0.80` has ever had, since it was picked in the
original phase-8 commit and never calibrated. `lexicon_receipt.py --show
FIRMS` prints source-tagged lexicons; `--empty` dumps starved firms with the
filter that killed each word.

Rollbacks: `ROOT_LEXICON=0`, `NAME_KEYWORDS=0`, `DICT_VOTE=0`,
`DICT_TRUSTED_ONLY=0` (opens the dormant work).

## Phase 8w — a code must recur too; the dictionary gate opens (2026-08-07)

Phase 8t was right and unshippable: opening the trade dictionaries to every
code with enough lots took empty lexicons from 33 to 3 and cost **eleven**
rejection cases. The diagnosis was Heberger Hoch-, Tief- und Ingenieurbau —
three won tenders, all in wastewater plants, and seven dictionaries:

```
45252100  maschinentechnik belebungsbecken rohrleitung armatur
45262310  perimeterdämmung ortbeton mauerarbeiten rohbau betonarbeiten ...
45311000  brandmeldeanlage elektro
45311100  unterverteiler steckdose beleuchtung verkabelung
45314310  kabeltrasse
45317000  gebäudeautomation
45317300  starkstrom unterverteilung blitzschutz leuchte
```

**Every one of those 24 words is correct for its trade.** Nothing is wrong
with them as words, which is why no threshold on word quality could remove
them — a sweep of `DICT_MIN_IN` from 10% to 100% either changed nothing or
emptied every dictionary in the store (at 50% and above, not one word
survives in all of a code's lots; German lot texts are too short and varied,
and many lots are a bare address or "Los 12"). The error is that six of
those seven trades are not Heberger's. They arrive through `cpv_additional`,
which names every trade in a procurement on each of its lots.

**So the missing rule is the same one, at the third level.** A word must
recur across the firm's references (8q); a lot must agree with its code's
majority (8u); and now a **code** must recur across the firm's wins.
Heberger's wastewater code is on 3 of 3; its fire-alarm code on 1 of 3. One
is its trade, the other is the context of somebody else's procurement.
`firm_codes()` keeps codes present on at least `DICT_CODE_SHARE` of a firm's
won lots (always at least two).

**The frontier**, trust gate open in every row:

| dictionaries reach a firm when its code is on… | empty | <3 | IN | OUT |
| --- | --- | --- | --- | --- |
| any win (phase 8t as measured) | 3 | 11 | 54/74 | 36/52 |
| **>= 1/3 of wins (shipped)** | **14** | **72** | **49/74** | **43/52** |
| >= 3/4 of wins | 23 | 126 | 48/74 | 46/52 |
| every win | 25 | 135 | 48/74 | 46/52 |
| *master before this (gate closed)* | *33* | *133* | *50/74* | *45/52* |

Empty lexicons 33 -> 14 and firms under three words 133 -> 72, for two
rejection cases and one recall case. The 3/4 row is the precision-first
alternative — better than master on OUT — and stays documented for the day
leakage binds.

**What this settles about two other proposals.** (1) The operator's
intersection rule — a word must appear in ALL of a trade's surviving lots —
is implementable and measured, and it empties every dictionary in the store:
`45233280` (30 lots), `45112700` (558), `45262320` (408), `45311200` (796)
all derive `[]`. Its apparent precision gain (OUT 47/52) is the gain of
having no dictionaries at all. Rejected on the reading, not on the number.
(2) Raising `DICT_MIN_IN` cannot address code inheritance, since the words
were never the defect. The usable range of that knob is 0-25%; above it the
dictionaries are empty.

Rollbacks: `DICT_CODE_SHARE=0` (every code counts again),
`DICT_TRUSTED_ONLY=1` (closes the gate, phase-8t rollback),
`DICT_MIN_IN` env-overridable for the share sweep.

## Phase 8x — `schal`, and why ambiguous roots cannot be found by machine (2026-08-07)

`schal` entered the vocabulary for *Schalung* (formwork) and, matching being
substring, also sat inside **Schalt**schrank, **Schalt**er and
**Schall**schutz. Yunex — an electrical firm — carried it, taken from
*Schaltanlage*: it would have convicted on any tender naming a Schaltschrank
and the receipt would have read `schal`, meaning formwork. Third instance of
the defect behind `pump` and `leitung`, and the same hand fix: replace the
bare root with compounds that name one thing (`schalung`, `betonschal`,
`ausschal`, `einschal`; `schaltschrank` added on the electrical side).

**It moves no number.** IN 49/74, OUT 43/52, 14 empty, 72 under three words
— identical to master. The benchmark holds no lot where `schal` decided
anything, and coverage counts list sizes, so Yunex dropping one word of six
is invisible. The harm sits in production across the ~3,000 open lots scored
per cycle, which is where the benchmark is blind — the argument phase 8i
shipped on, and stronger here because this costs nothing.

### Dead end: detecting ambiguous roots from CPV codes

Two scores were built and measured. Neither works.

1. **Concentration of a root's lots in one code.** Surfaces `daemm`, `holz`,
   `dach`, `stein` — sound roots whose matched words are one family
   (`dämmung wärmedämmung perimeterdämmung`). It measures *ubiquity*: a real
   trade word appears in many kinds of project.
2. **Whether a root's own words agree on a trade.** Surfaces `stein`,
   `putz`, `verkleid`, `pfahl`, `schweiss` — splits that are real but are
   *project* differences, not meaning differences. Plaster is plaster
   whether the lot is filed under painting, structural or insulation.

Both ask the CPV code what a word means, and phase 8u established that the
code often does not know: 84% of one dictionary's pool arrived through
`cpv_additional`, and a tender titled *Estricharbeiten* sits under "Bau von
Polizeirevieren". A noisy label cannot be ground truth for meaning.

All three ambiguous roots so far were found the same way — a person reading
one firm's lexicon and noticing a word that did not belong. `evidence.py
--roots` ships as the store-wide version of that reading (every root with
the words it actually matches, grouped by trade), explicitly **not** as a
detector; its ordering is not a signal.

## Phase 8y — the configuration is the cache filename (2026-08-07)

`data/trade_dicts.json` was one file guarded by a hand-written comparison of
key fields (`n`, `v`, `bd`, `tr`, `to`, `vo`, `mi`). That design failed three
times in one day, always identically: adding a switch means adding a field
**and** bumping the version, and a field missing from an older file reads as
`False` — exactly what a new default carries — so a stale entry matched the
key and was silently reused. The A/B arms of phases 8t and 8u both measured
the wrong dictionaries before this was noticed, and it was noticed only
because `--coverage` stopped reproducing a committed table.

`evidence.dict_cache_path()` now hashes every input into the name:
`trade_dicts_<12 hex>.json`. Three consequences:

- a stale cache **cannot be found**, rather than being wrongly matched;
- adding a switch needs no bookkeeping at all — it enters the hash;
- A/B arms stop evicting each other, so a sweep rebuilds once per arm and
  then reuses. Today's sweeps rebuilt on every switch flip.

**`cpv_trade_roots.txt` is hashed too, and was never in the old key** even
though the dictionaries are derived through `names_trade()` and the phase-8u
vote signature. Editing a root left the dictionaries stale with nothing to
say so — including the `schal` edit in phase 8x.

Verified unchanged: IN 49/74, OUT 43/52, 14 empty lexicons, 72 under three
words. The old `data/trade_dicts.json` is orphaned and safe to delete.

This is complementary to `STORAGE.md`, which independently decided these
caches stay files: they are pure functions of the store, rewritten
wholesale, never read by key. The problem was never where they live — it was
that their identity was hand-maintained.

## Phase 8z — the mute profile is now audible (2026-08-08)

Under the evidence gate a lot passes on one of two things: a word from the
profile's lexicon found in the tender, or a core trade root found in its
title. **A profile holding neither cannot pass any lot in the market** — not
a low score, an impossibility. The customer receives an empty report every
week and nothing anywhere says why; the operator sees silence and reasonably
concludes the market was quiet.

43 of the 512 firms with >= 3 wins were in that state when it was counted on
2026-08-07 (fewer since the dictionaries opened, but the failure mode is
structural, not historical). The gate has always known at build time — it
just never said so. Same principle as the borderline band: a gate that goes
wrong silently is the one thing the design does not allow.

`relevance.mute_reason(profile)` returns `None` when a profile can convict,
otherwise the reason. It is consulted in two places:

```
python relevance.py --check-profiles      # on demand, per live subscription
python loop.py run                        # every cycle, at delivery
```

The check prints each subscription's lexicon and core roots, and a
`** MUTE **` line for any that cannot recommend anything; the cycle prints
`[deliver] <sub_id>: ** MUTE PROFILE ** ...` at the moment the profile is
built, before any lot is judged. Reading `--check-profiles` is also the
cheapest way to see what the eight live customers' lexicons actually contain
— the operator's test for a lexicon has always been reading it.

Nothing about a verdict changes; this only makes a silent failure loud. The
entry point at the bottom of `relevance.py` moved below `class Gate` so it
can load one — it previously sat mid-file, which was fine while it only
printed constants.

## Phase 9a — the reading checks become commands (2026-08-08)

Three ambiguous roots were found this session — `pump`, `leitung`, `schal` —
every one by a person reading a firm's lexicon and noticing a word that did
not belong. The checks behind that existed only as habits.

**`evidence.py --collide ROOT`** lists every store word a candidate root would
match, with lot counts. The roots file always described this check ("the store
was checked" — how `stei` and `gla` were refused); it was never a command. It
immediately refused four of today's candidates:

| candidate | why refused |
| --- | --- |
| `labor` | 180 words, 2.7% of the store. `labortische`/`laboreinrichtung` are the trade, but `laborgebäude`, `laborneubau`, `zentrallabor` are BUILDING TYPES and `kollaborationsflächen` is unrelated |
| `leitsystem` | two trades in 19 words: `blindenleitsystem`/`wegeleitsystem` (guidance) vs `prozessleitsystem`/`gebäudeleitsystem` (automation) — the `pump` shape |
| `ktb` | 24 true against `projektbeschreibung` 79, `objektbetreuung` 9 |
| `lst` | 41 true against `edelstahl` 658, `vollständig` 364 — 18.1% of the store |

`taktil` (6 words, all inflections) and `stellwerk` (13, all railway) pass.

**`lexicon_receipt.py --rootless`** lists firms whose won tenders contain no
trade word at all, with titles, so the two causes can be separated by reading.
Both exist, and the second is commoner than assumed:

- *named but unreadable* — `Labor-Ausstattung Chemie` (Laborbau Hemling),
  `Turnhalle - Trennvorhang` (Metallbau Politz), `KiTa als Modulbau`
  (KLEUSBERG), `6 Signalausleger` (KÖNIGBAU)
- *railway acronyms* — `ESTW Mühlacker - KTB`, `LST ESTW MOF`, `BÜ km 62,181`
  across Grötz, Duensing, Weidlich, Josef Hell, Willke. The work is named
  precisely, in a dialect the vocabulary cannot read.
- *genuinely silent* — `Los 01_RV OG Offenburg` (RIWAtec),
  `Generalunternehmerleistungen` (Köster)

This matters beyond the words: a count of "lots with no trade" cannot be used
as a denominator, because most of it is our blindness rather than the
document's silence.

### Dead end: `CORE_SHARE` below 0.5

Listed as promising since phase 8o, run 2026-08-08:

| `CORE_SHARE` | IN | OUT |
| --- | --- | --- |
| **0.5 (shipped)** | 49/74 | **43/52** |
| 0.34 | 49/74 | **43/52** |
| 0.25 | 49/74 | 42/52 |
| 0.2 | 49/74 | 42/52 |

**Recall is identical across the whole range** and precision falls below 0.34.
Loosening buys nothing and costs a case.

Why recall is flat: `need = max(2, ceil(n × share))`, and the floor of 2 binds
for every small firm — a 3-win firm needs 2 references at 0.5 and still 2 at
0.2. The share only bites from about five wins upward, moving DB Bahnbau from
32 references to 13 and Ed. Züblin from 8 to 4, which is where the lost
rejection case comes from: a generalist adopting a context root as its trade.


## Phase 9b — `--benchmark` was dead, and what it says now (2026-08-08)

`evidence.py --benchmark` had been raising `NameError: is_deep` since before
`bc77f7d`: `run_benchmark` builds `firm_tc` with `is_deep(c)` but imported only
`lot_codes` from `calibrate`. Every neighbouring function imports it locally;
this one did not, so the whole path failed before doing any work. Nobody
noticed because the gate-level harnesses (`--judge-benchmark`, `--sweep`) were
the ones being read.

Its score line was also wrong. `fails` counts **once per selected lot**, but
the line printed `len(cases) - fails` over `len(cases)`. A case's
`(pub, title_contains)` can select more than one lot, so the numerator
subtracted a per-lot count from a per-case total. It printed 88/122 where the
run had judged 126 lots and got 34 wrong. Now 92/126 — which is what the
`IN`/`OUT` tables in this document have always counted (49/74 + 43/52 = 92).

**Read `--benchmark` as a lexicon harness, not as product accuracy.** Its
verdict is `'in' if ev else 'out'` — any evidence at all. The shipped gate does
not do that; it uses `evidence.convicts`, the title-or-two rule of phase 8c(3)
above. Scoring the same 126 lots through `convicts` gives 94/126. The divergence
is by design (this harness isolates the keyword layer, `--judge-benchmark`
exercises the real `judge()`), but the two numbers are not comparable and
`--benchmark`'s must not be quoted as the system's accuracy.

### The 126 lots, split by failure

| | count | |
| --- | --- | --- |
| correct | 92 | |
| false positive (`out` -> `in`) | 22 | a trade word appears in a package that is not that trade |
| false negative (`in` -> `out`) | 12 | **no keyword matched at all** |

The twelve misses are exactly the twelve lots that produce *zero* evidence —
under an any-evidence rule those coincide by construction. That sets a ceiling:
any rule that decides **from** the evidence must call a zero-evidence lot `out`,
so **114/126 is the best any filtering change can reach.** The whole remaining
gap is false positives.

Seven of the twelve are the railway dialect phase 9a already named — `TK
Immenstadt - Oberstdorf`, `Möttingen, Baut. Anp. BÜ km 62,181`, `Bf Sünching -
Bahnsteiganpassung`, `GE, WE, RbLs in Bf. Rheinkamp`, `Erneuerung EÜ Strothe`,
`Umbau Vst Leuna Werke Süd`, `Errichtung Bedienstandort Bremen`. Not short
text: only 8 of the 42 no-evidence lots are under 200 characters, and the
`in`-labelled ones run a median of 435. The documents say what the work is; the
vocabulary cannot read it.

### Dead end: lot trade-span and the coverage ratio

The 22 false positives share one shape — `estrich` matching a waterworks,
`beton` matching `Weidenstieg 29 - Elektro`. Matching asks whether a word is
*present*, which cannot separate "this lot **is** screed work" from "this lot
**mentions** screed among fifty trades". The proposed denominator: run every
trade dictionary over the lot text, count the trades that fire, and require
coverage `|firm ∩ lot| / |lot|` above a cutoff.

The signal is real — 239 dictionaries, a trade counted present at >= 2 of its
words:

| lots labelled | median span | max |
| --- | --- | --- |
| `in` | 1 | 29 |
| `out` | 6 | 50 |

It does not survive being tuned. Best cutoff over an 18-point grid reaches
100/126, but split by **firm** (splitting by lot leaks the lexicon across
halves) and tuned on one half, scored on the other:

| | scored | vs `convicts` |
| --- | --- | --- |
| tune A, score B | 36/54 | −1 |
| tune B, score A | 60/72 | +3 |

Net +2, one half negative. The grid says the same thing if read rather than
maximised: `span>3, cov<0.10` scores 100 while both its neighbours score 97 —
isolated spikes, not a plateau. 126 lots clustered into ~40 firms cannot tune
two parameters.

And it is absent where the category is clearest: the three
`GU-Leistung - De- und Remontagen KG300 und 400` lots — a general-contractor
scope named as such in the title — score span=4. The package is short; the text
never mentions the fifty trades it contains. A signal read off the prose misses
the lots whose breadth is *declared* rather than described.

**Not shipped.** The span count may still be worth surfacing as a readable
property of a lot ("this lot spans 14 trades"), but no threshold rule on it is
justified by this evidence.
