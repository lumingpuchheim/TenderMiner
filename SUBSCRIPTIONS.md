# SUBSCRIPTIONS — one run, many customer views

Status: implemented — phases 1 (slicing-key + publication-number stamping),
3 (subscriptions, renderer, delivery ledger) and 4 (per-slice track record
with the fallback ladder) in [`loop.py`](loop.py); phase 5 (slice matrix +
drift panel) in [`render_dashboard.py`](render_dashboard.py). Phase 2 (wider
backfill) is an operational step, deferred with the beyond-construction
scope. Grade rows have carried the slicing keys from before the first real
grade was ever written, so the keyless-row fallback join is dead code
territory — it was never needed and is not implemented. Builds on the
running loop
([`ONLINE_LEARNING.md`](ONLINE_LEARNING.md)) and uses its vocabulary
(**component** = a box that always runs; **phase** = build order in time, never
a scope cut). Nothing here changes the loop's spine — download, grade, learn,
predict stay exactly as they are; this document adds the layer between the
ledger and a paying customer.

## What this is, in one paragraph

Today the loop produces one global report: the top of one ranking over one
market (CPV 45, Germany). Customers, however, buy *their* market: an industry,
maybe a region. The design principle is **one run, many views** — the loop
scores the whole market once per cycle, and a customer is a **saved filter**
(a *subscription*) over that one result. Delivery is rendering: filter the
freshly scored lots by the subscription, rank within the slice, write the
customer's report. Quality is the same filter applied to the grades: every
customer's track record is computed from the same append-only files an auditor
would see, just sliced to their market. There is never a run per customer.

## Worked example (used throughout)

> **Weber Tiefbau GmbH** subscribes to civil engineering (CPV 452) in Bayern
> (NUTS DE2). Each cycle they receive their top picks — say 12 open lots,
> tiered HIGH/MEDIUM/LOW **within their slice**. Three months later their
> report opens with: *"In your market over the trailing 12 weeks, 61 of our
> predictions got their outcome. Of the top 20% we ranked for you, 41 in 100
> ended with 0–1 bids, vs 16 in 100 by chance — lift 2.6x."*

Every design decision below exists to make that paragraph computable and
honest.

## The subscription object

Append-only file, one line per subscription **version** (private, next to the
ledger — the repo is public):

```jsonl
# data/subscriptions.jsonl
{"sub_id": "weber-tiefbau", "version": 1, "effective_from": "2026-09-01",
 "name": "Weber Tiefbau GmbH",
 "cpv_prefixes": ["452"], "nuts_prefixes": ["DE2"],
 "min_deadline_days": 14, "max_picks": 5, "avoid_n": 5, "active": true}
```

- **Filters compose by AND; each list composes by OR** (a lot matches if its
  CPV starts with any listed prefix AND its NUTS starts with any listed
  prefix). Omitted filter = no constraint. `nuts_prefixes` is optional by
  design — region is a refinement, not a requirement.
- **Unknown deadlines are excluded whenever `min_deadline_days` is set.** The
  filter is a customer promise ("at least 14 days left to bid"), and a promise
  cannot be honored for a lot whose deadline the notice does not state. With
  `min_deadline_days: 0` (or omitted) such lots pass through.
- **Versioned, never edited.** Widening Weber to all of CPV 45 in March is a
  new line with `version: 2, effective_from: …`. The question "what did Weber
  see on date D" is answered by the version in force on D. Deactivation is a
  new version with `active: false`.
- The filter vocabulary is deliberately small (CPV prefix, NUTS prefix,
  minimum days to deadline, max picks). Value bands, buyer types, keyword
  filters are future *versions of this format*, not future architecture.
- **`max_picks` (default 5, decision 2026-08-04) caps the list; the flag is
  the floor.** A pick must be *flagged* (score at or above the model's
  cut-off) — being top of a weak week is not enough. At most `max_picks`
  flagged lots are delivered; when none qualify, the report says **"no pick
  this week"** outright. A short list a customer can actually act on beats a
  long one that becomes homework; an empty list that says "keep your money"
  is the product's founding promise, not a failure state. (`top_n` is the
  retired name of this field and is ignored.)

## Ledger changes: stamp the slicing keys at write time

The prediction ledger row gains the columns a slice needs, written at the
moment of scoring:

```
cpv3                first 3 digits of cpv_main   (industry)
place_nuts3         NUTS-3 of the place of performance
publication_number  the scored notice's TED publication number (audit link)
buyer_name          for rendering, never for features
est_value_lot       for rendering, never for features
title               for rendering, never for features
```

Grade rows already carry `cpv3`; they gain `place_nuts3` the same way, plus
`award_publication_number` — the TED publication number of the award notice
that supplied the outcome. The publication numbers make every ledger row one
click from the EU's public record (`ted.europa.eu/en/notice/-/detail/<nr>`):
a customer disputing a graded outcome is pointed at the official journal, not
at our files. Stamped at write time for the same reason as the slicing keys —
a dispute must never depend on a join against a rebuilt store.

Why at write time and not by joining the store later: the ledger is the
frozen record. A join against a rebuilt store can drift; a stamped row cannot.
Rows written before this ships stay keyless forever (append-only, never
edited) — for those, and only those, slicing falls back to a store join at
read time, clearly a best-effort reconstruction. From the first stamped row
onward, no customer number depends on a join.

## The delivery ledger — what did this customer actually see?

The prediction ledger records what the *model* said. To grade what a
*customer* saw, that is not enough: tiers are slice-relative (below), so
Weber's HIGH is not the global HIGH. The answer is the same trick a third
time — an append-only **delivery ledger**:

```jsonl
# data/ledger/deliveries.jsonl
{"ts": "2026-09-04T06:12:00Z", "sub_id": "weber-tiefbau", "sub_version": 1,
 "procedure_id": "…", "lot_id": "LOT-0001", "model": "m2026-09-04-…",
 "score": 0.71, "slice_rank": 3, "slice_size": 118, "slice_tier": "HIGH"}
```

One row per (subscription, cycle, delivered lot), written when the customer's
report is rendered, never edited. A cycle is a calendar day: re-running the
loop on the same day re-renders the report but appends no duplicate delivery
rows — the same idempotence rule the prediction ledger already follows.

**The negative list.** The product's founding problem is customers bidding
into crowds; the report therefore also delivers the **`avoid_n` most
contested-looking lots** of the slice — the *bottom* of the same ranking —
as an explicit "don't go there" list. Warnings are delivery rows like any
other, marked `kind: "avoid"` (`slice_tier: "AVOID"`, `slice_rank` counted
from the crowded end; rows without a `kind` predate the field and are
picks). They are graded by the same join and reviewed in their own receipts
block — a warning is **right when the lot ends contested**, and a warning
that ended with 0–1 bids is shown as the miss it is. The list only renders
when the slice is large enough that picks and warnings cannot overlap
(`slice_size > top_n + avoid_n`). Grading a customer's view is then a pure
join: grades (by lot) ⋈ deliveries (by lot + sub). No reconstruction, no
"what would the filter have matched back then" — the row *is* what they saw.

## Tiers within the slice

The stamped global tier in the prediction ledger stays (it feeds the global
report and its per-tier track record, unchanged). For a subscription, tiers
are recomputed **within the slice at render time** from the frozen raw
scores: HIGH = top `tier_high` share of *this slice's* ranking this cycle,
MEDIUM = next `tier_medium` share, LOW = the rest — same shares, same
rank-quantile logic, same "no probabilities" rule as the global tiers
(decision 2026-08-01 in ONLINE_LEARNING.md carries over). A slice tier's
real-world meaning is verified the same way the global one is: by the graded
outcomes of delivered rows carrying that `slice_tier`.

Why slice-relative and not global: a customer in a low-scoring trade would
otherwise receive an empty HIGH bucket forever. "Top 10% of your market this
week" is the product promise; the delivery ledger is what keeps it honest.

**Every pick says why, in plain words (decision 2026-08-04).** Each scored
lot carries a short "why" — the model's own per-prediction feature
attributions (SHAP), passed through a fixed phrase book that translates
feature groups into customer language ("the location — fewer firms bid
there", "the qualification requirements", "the specialised type of work").
Deliberately lossy: technical features (notice subtype, currency codes,
CPV digit positions) map to nothing and never surface. The picks table
shows the top reasons the lot looks lonely; the warnings table the top
reasons it looks crowded. The full attribution stays in the model; the
phrase book is the customer boundary.

**Tiers and scores are internal (decision 2026-08-04).** They are recorded
on every delivery row and verified through the graded joins, but the
customer report never shows them: no score column, no tier column, no
legend. A customer sees tenders — deadline, buyer, title linked to the
official TED notice page (the clear next action: read the notice, get the
documents) — and verified outcomes in plain words. Our vocabulary is our
mental load, not the customer's.

## The per-slice track record, and the fallback ladder

**Receipts before rates.** Humans reason story-first and are bad at
statistics; the report does not fight that. The track-record section leads
with the **itemized review**: every delivered pick whose outcome has arrived,
one line each — what we said (slice tier), what happened (the actual bid
count), and the award notice's TED link as the receipt — misses shown exactly
like hits, newest first. The **statistical record** (the Weber sentence
below) follows immediately as the honest summary: it is what protects the
receipts from the "you only show your hits" objection, because the rate is
computed over *all* graded picks and compared to chance.

To make receipt lines renderable without joins: delivery rows additionally
stamp `title`, `buyer_name` and `publication_number` at write time
(rendering, never features), and grade rows stamp `n_tenders` — the outcome's
concrete number ("1 bid" beats "label 1"). Rows written before these stamps
fall back to a read-time join against the append-only prediction ledger; that
file is itself frozen, so the rebuilt-store drift concern does not apply.

Each customer report then quotes the verified numbers, computed from
grades ⋈ deliveries over the track-record window: graded count, base rate in
the slice, top-share hit rate and lift, per-`slice_tier` outcomes — the
Weber sentence above.

Thin slices do not get fake precision. If a slice has fewer than
`min_slice_grades` graded outcomes (a parameter, like every window and
threshold in this system), the report **says so and climbs a ladder**, always
labeling the rung it stands on:

1. the subscription's slice (CPV × region),
2. the industry Germany-wide (CPV prefixes, region dropped),
3. the whole graded market.

> *"Your slice has 9 graded outcomes so far — too few to quote honestly.
> Across civil engineering in all of Germany (214 outcomes): top 20% hit 38
> in 100, chance 17."*

Silence is not an option (the current per-trade table simply omits thin
trades; the ladder replaces omission), and an unlabeled broader number is not
an option either.

## Widening the scope — the credibility clock

More industries means a wider `--cpv` download and one global model retrained
on the wider corpus (features are role-driven; the loop is CPV-agnostic by
construction — this was always the plan). The binding constraint is not
compute, it is **history**:

- a track record needs months of graded outcomes,
- the base-rate drift band needs months of monthly rates,
- the validation window needs enough labeled lots in the new branches.

So a new industry's credibility clock starts at its **backfill**, not at its
first live cycle. The playbook for adding a branch: backfill its raw history
first (months of notices + awards, the same `--last Nm` mechanism), let the
loop label and grade it, and only sell the slice when its ladder rung 2
number exists. Selling ahead of the backfill means selling rung 3 (whole
market) and saying so.

Per-branch base rates differ (civil engineering is not finishing trades), so
the operator matrix below shows base rate per slice; the global drift
monitors stay global, and a per-branch base-rate drift view joins the matrix
rather than multiplying alarm channels.

## The annex — ship all the answers (decision 2026-08-04)

The curated report cannot intercept a bad bid on a tender *we* didn't
list — and the founding pain lives in the tenders the customer finds
themselves. An inquiry service is unnecessary because every possible answer
is precomputed weekly; so we ship them all: alongside each report, an
**annex file** (`annex_<date>.md`) listing **every** open tender matching
the subscription's CPV/NUTS filters (the deadline filter is deliberately
ignored — a candidate with 10 days left still deserves its verdict), one
line each with a plain-word verdict:

- **few bidders likely** — flagged by the model (same floor as picks),
- **expect a crowd** — bottom fifth of the slice ranking,
- **average odds** — everything else,

plus the top plain-language reasons for the non-average rows, deadline,
buyer, TED link. The report instructs: *before any bid/no-bid decision,
find your tender in the annex.* Annex rows are **not** delivery-ledger
rows (hundreds per customer per cycle would drown the ledger); a disputed
annex verdict traces through the prediction ledger, which froze the same
score with the same model id, and the dated annex file itself. The curated
report stays the product's face; the annex is the reference behind it.

## Rendering and delivery

Per cycle, after the global report:

```
data/reports/subscriptions/<sub_id>/report_<date>.md
```

**Customer artifacts are HTML (decision 2026-08-05).** Markdown was the
prototyping format; the shipped customer files are `report_<date>.html` and
`annex_<date>.html` — self-contained, inline-styled, e-mail-body-ready:
true traffic-light verdict colors (green / yellow / red cells, not emoji),
tender titles as links, tables that survive long buyer names. One format
per audience: customers get HTML only (no per-customer markdown — every
copy change would otherwise be made twice); the operator keeps the global
markdown report and the dashboard; auditors keep the JSONL ledgers. The
annex column order is tender · deadline · buyer · verdict · why. The
renderer is one loop over active subscriptions doing filter → rank → write
report → append delivery rows; a hundred subscriptions is milliseconds,
which is the entire point of one-run-many-views.

## The operator view

The dashboard gains two panels:

1. **Slice matrix** — one row per industry × region with graded outcomes:
   graded / base rate / top-share hit / lift, thin cells greyed with their
   count. This is where "which slices carry their weight" is answered, and
   where a new branch's clock is visibly still running.
2. **Drift panel** — the four drift monitors' current status (already in the
   report footer, not yet on the dashboard; this closes that known gap).

## What can go wrong, and the designed answer

- **A subscription filter matches nothing this cycle** → the report says so
  explicitly ("0 open lots matched your filters this week") — an empty page
  is a statement, not a failure.
- **A customer's slice definition was wrong for weeks** → versioning: fix it
  as a new version; the delivery ledger shows exactly what the old version
  delivered, so the track record stays attributable to what was actually sent.
- **Old keyless ledger rows** → best-effort store join at read time, labeled
  as reconstruction; all new rows are stamped.
- **The store is rebuilt with different extraction rules** → stamped rows and
  delivery rows are immune; only the keyless-row fallback can shift, and it
  is labeled.

## Build order (phases in time — no component is cut)

1. **Stamp the slicing keys** — cpv3, place_nuts3 (+ rendering columns) into
   new prediction rows; place_nuts3 into new grade rows. *Every later phase
   reads these; ship first so the append-only files start accumulating keys.*
2. **Backfill the wider CPV scope** — widen the download, backfill history
   for the target branches, let base-rate bands and validation windows fill.
   *Longest lead time; start early, runs unattended.*
3. **Subscriptions + delivery** — subscription file, renderer (filter → rank
   → slice tiers → per-customer report), delivery ledger. *First customer-
   shaped output.*
4. **Per-slice track record** — grades ⋈ deliveries, fallback ladder in the
   customer report header. *The Weber sentence becomes real.*
5. **Operator matrix + drift panel** — the dashboard learns slices and shows
   the monitors. *Operating the thing becomes one page.*

Each phase leaves a running system; the delivery-ledger format carries
`slice_tier` and `sub_version` from day one, so nothing written early needs
rewriting later.

## Open decisions (defaults proposed, none blocking Phase 1)

- **`min_slice_grades`** — ladder threshold; 25 proposed (mirrors
  `min_trade_grades`).
- **Tier shares per slice** — same defaults as global (10% / 20%) proposed;
  per-subscription overrides are a subscription-format field away if a
  customer's market argues for it.
- **NUTS granularity** — prefixes make DE / DE2 / DE21x all expressible;
  whether to *offer* NUTS-3 to customers is a product call (thin slices grade
  slowly — the ladder will be doing the talking for a long time).
- **Delivery channel** — markdown files first; e-mail once a real customer
  wants it (rendering, not architecture).
