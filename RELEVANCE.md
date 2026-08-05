# RELEVANCE — recommend only the customer's business

Status: phases 1 (embedding sidecar — `embed.py`, wired into the loop),
2 (calibration + trusted codes — `calibrate.py`, receipts committed per
`model_tag`) and 3 (gate in the renderer — `relevance.py`, wired into
`deliver`; pilot subscription live) implemented; phase 4 specification.
Default embedding model: `jina-v2-base-de` (flipped 2026-08-05 after a
full-store A/B; shipping gate is configuration E — plain references +
code channel, expansion off). Known open weakness: same-buyer template
text still lifts sibling lots of other trades over the gate (weakened
but not solved by the model flip); mitigation is specced only as a
diagnostic for now, with phase 4 feedback as the backstop.
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
- **The trade fingerprint** of a profile: rank all CPV entries by the
  similarity of their label embeddings to the profile's reference texts,
  union the references' own trusted codes, keep the top entries. The
  fingerprint is the profile *named in official vocabulary* — it feeds
  (a) the code channel below, (b) the customer report's profile line
  ("Ihr Profil: Blitzschutzarbeiten, Elektroinstallation"), (c) the mapping
  target for free-text onboarding, and (d) the count of trade areas the
  pricing model needs.
- **The code channel**: for a deep-coded candidate, score = the best
  label-to-label cosine between the candidate's code and the fingerprint's
  codes. This grades across sibling codes (45312311 Blitzableiterbau scores
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
remains the open weakness; sentence-level template stripping is its specced
fix if the calibration diagnostic stays ugly.

## Pick confidence and the relevance "why" (decision 2026-08-05)

A lot that clears the gate by 0.007 and one that clears it by 0.3 must not
look identical in the report. Two rules:

- **Pick margin**: a gated lot may enter the customer's market view at
  `min_relevance`, but a **recommendation** must be confident — text at
  least `min_relevance + pick_margin` (default 0.05, mirroring the
  borderline margin below the gate), or a code-channel pass (codes are
  precise when they speak; no margin needed). Scrape-overs stay in the
  market, never in the picks — "no pick this week" beats a weak pick.
- **The relevance "why"**: every gated pick carries a plain-language reason
  it is in the customer's market — "ähnelt Ihrem Auftrag „…‟" (nearest
  reference) or "CPV-Code passt: <label>" — next to the existing
  competition "why". Scores stay internal (decision 2026-08-04); the words
  let a customer judge a marginal case in one glance.

Report copy consequence: the report no longer cites how many lots were
checked or matched — the product is the list, and the size of our haystack
is our business, not the customer's.

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
- **Competitor detection (idea, not scoped)** — profile similarity between
  firms is competitor identification for free: the same sidecar over award
  data answers "who competes with whom" per niche. Possible future
  analytics product; noted here so it is not re-derived from scratch.
