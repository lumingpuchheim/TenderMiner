# ONBOARDING — from a name in the awards store to a paying subscription

Design, written 2026-08-10. Companion to [`GO_TO_MARKET.md`](GO_TO_MARKET.md)
(the play and the channel law), [`MARKET_AND_COMPETITORS.md`](MARKET_AND_COMPETITORS.md)
(what may and may not be claimed), [`SUBSCRIPTIONS.md`](SUBSCRIPTIONS.md) (the
customer-layer mechanics this design drives) and [`RELEVANCE.md`](RELEVANCE.md)
(the gate that decides what a customer sees).

Two markers are used throughout and collected in §7:

- **[CLARIFY]** — a decision a person has to make; the design proposes a default
  but does not get to choose.
- **[BUILD]** — a component that does not exist yet, named where it belongs.

---

## 0. The funnel

```
awards store  →  target list  →  letter  →  landing page  →  free weeks  →  paid  →  learning loop
   5,229          ~471            50 per      [CLARIFY]       [CLARIFY]     €179/mo    learned_refs
 winner names   contactable      batch       conversion      length         cancel     per reply
                small/micro                  unknown                        monthly
```

Every arrow writes a row to the outreach ledger (§6). No stage is estimated from
a benchmark someone read on the internet: where we do not have the number, the
funnel says so.

---

## 1. Finding the customer in our own data

The premise that makes this cheap: **every firm in the awards store is a proven
tender bidder.** They have already decided that public work is their market, so
the letter never has to sell that idea — only a better way to pick which lots to
bid.

### 1.1 The selection, as filters

`outreach.py` builds the list. Current rules, in the order they cut:

| filter | why | roughly |
| --- | --- | --- |
| appears as `winner_names` on an award | proven bidder, and we hold the evidence | 5,229 names |
| `winner_size` ∈ {small, micro} | the segment the price is sized for; large firms need a different pitch | 2,566 |
| ≥ 2 won lots | one win can be luck; two is a habit, and it gives the profile two anchors | 471 |
| ≥ 2 usable `profile_refs` | the **contract-notice** publication numbers behind their wins, not the award notices — without these there is no profile and no receipt | printed by `outreach.py` |
| contact details present in the award XML | postal address for the letter (Organizations block) | printed by `outreach.py` |
| their trade gets ≥ 1 pick most weeks | a subscriber who gets "no pick this week" for a month churns; `sim_picks` from the simulation ledger measures it | `sim_picks` column |

### 1.2 The ordering — who gets the first batch

Sort by, in order:

1. **Trade group.** CPV 452 first. It is the only group where the competition
   flag has measured lift (24% vs a 10% base rate, 2.30× — backtest 2026-08-10),
   so it is the only group whose letter may carry the forecast claim. 453 and 450
   have no measured lift and wait for a better model, not for a better letter.
2. **`sim_picks`** — how much product their market actually gets today.
3. **`single_bid_wins`** — a firm that has already won something with one bid
   recognises the phenomenon we are selling. Their own contract number is the
   opening line.
4. **`wins`**.

### 1.3 Two known seams

- **Identity is the exact winner-name string** (`SIMULATION.md`). "Müller GmbH"
  and "Mueller GmbH & Co. KG" are two rows. `outreach.py` flags likely duplicates
  and never merges them. **[CLARIFY]** who merges alias groups before a batch
  goes out, and whether a merged firm keeps both names' wins as profile refs.
- **The target list speaks CPV, the pitch speaks trades.** `outreach.py` reports
  `trades` (cpv3) and `trade_read` (what the won texts read as against CPV
  labels); the market facts in the letter come from `market.py`, which works in
  [`trades.txt`](../trades.txt) names. **[BUILD]** a `trade` column on the target
  list, assigned by the same title matching `market.py` uses, so the letter's
  "in Straßenbau…" and the firm's row are the same object.

### 1.4 Exclusions

Current customers; consortium/ARGE names that are not a firm; firms whose only
wins are framework call-offs **[CLARIFY]** — a framework winner may have no
reason to watch open calls at all; and anything a person marks as "do not
contact" in the ledger, which is honoured forever.

---

## 2. Reaching out

### 2.1 Channel, and why

Promotional e-mail to a German business without prior express consent is
unlawful (§7 Abs. 2 UWG; B2B is **not** exempt), and these addresses were
published for procurement contact, not advertising. The order stands as decided
in `GO_TO_MARKET.md`:

1. **Postal letter with a QR code** — lawful, and Bau/Handwerk firms read letters.
   ~€1 each; a 50-letter batch costs ~€50, the whole pool ~€500.
2. **Phone** — defensible under mutmaßliche Einwilligung (we offer tenders to
   firms that demonstrably bid on tenders). **[CLARIFY]** whether we use it at
   all; it is a judgement about risk appetite, not about law we can settle here.
3. **Cold e-mail** — not used.

**[CLARIFY]** the legal basis for holding the target list itself. The contact
details are personal data taken from published procurement notices, processed
before any consent exists. A documented legitimate-interest assessment (Art. 6(1)(f))
plus an information notice on the landing page is the normal answer, but this is
a lawyer question, not a design question, and it blocks the first batch.

### 2.2 What the letter is

One page, generated per firm — never written by hand, so 471 letters cost the
same as one:

1. **Their own win.** "Los 3 Blitzschutz, Stadt X, awarded 2026-03-14 — one
   bidder. You were it." With the TED link so they can check it in a minute.
   Generated by `rewind_win.py`, which replays what we would have shown them
   knowing only what was public at the time.
2. **Their trade's market.** Lots per month, median award, €/year in scope, the
   0/1-bid share, the "closed without an award" share. From `market.py trade`.
3. **The forecast** — CPV 452 batches only (§1.2).
4. **Two or three live picks** in their trade and region from the current cycle,
   with deadlines, so the letter is useful even if they bin it.
5. **Price, and the QR code** to the landing page.

**[BUILD]** the mail-merge itself: one template, fed by the target row plus the
playback receipt plus the week's picks.

### 2.3 Batches and follow-up

Batches of **50, stratified by trade**, so per-trade conversion falls out of a
join rather than needing an experiment. One follow-up **[CLARIFY]**: a phone call
about ten days after the letter, or nothing at all. No second letter — if the
first one with their own contract number in it did not land, a reminder will not.

---

## 3. What we pitch

The ranked, defensible claims (derivation in `MARKET_AND_COMPETITORS.md` §6):

1. **Their trade's market facts** — ours, reproducible, published by nobody else.
2. **A profile built from contracts they won**, named by publication number.
3. **The competition forecast** — 452 only, and said as a replay result against
   outcomes already published, never as a live track record until the grades
   arrive (~November).
4. **A published relevance number** — 1.5% wrong-trade leakage at 60.0% recall
   against hand-labelled lots — for the buyer who asks how it was measured.
5. **No account, no login, one field of setup.**

Not claimable, and this list belongs in the letter template as a comment so
nobody re-adds them: that competitors search only by title or CPV (false); that
we surface work others cannot (ibau's LV-Suche does it deeper); that "fewer, more
relevant tenders" is ours (Vergabepilot's own line); any lift number outside 452;
any live track record.

---

## 4. What is free

Three things, in increasing order of what they cost us:

- **The receipt, before they answer.** The playback line in the letter is already
  a free personalised analysis. It costs ~10 minutes of compute per firm and it
  is the only asset in this funnel a competitor cannot produce.
- **The trade market page.** `market.py trade` output, per trade, as a public
  page. Costs nothing per visitor, ranks for the trade name, and is the landing
  page for the QR code. **[BUILD]**, **[CLARIFY]** whether it is public or
  behind the intake form — public is proposed: it is market data, not product.
- **The free weeks.** `GO_TO_MARKET.md` says "first report free". **[CLARIFY]**
  whether that is one report or four weeks. Recommendation: **four weeks**. One
  report shows the market once; the product is the *recurring* read, and four
  weeks lets the near-miss replies teach the profile before the customer judges
  it. The marginal cost is zero — the loop runs anyway.

---

## 5. How the subscription works

### 5.1 Sign-up

Three fields on the landing page: **firm name, e-mail, and the TED number of one
tender they won.** No account, no password, no login (§4.4 of
`MARKET_AND_COMPETITORS.md`).

For a firm from our own target list we can do better: **the profile is already
built.** The page can say "we found these six contracts you won — confirm they
are yours", and the customer's job shrinks to a yes. **[BUILD]** that lookup;
**[CLARIFY]** whether showing a firm its own award history before it is a
customer is something we are comfortable doing (it is public data, but it reads
as surveillance if the copy is wrong).

### 5.2 What gets created

Manual, by hand, and correct to keep manual under 50 customers:

- a `customer` row — name, `award_names` (the exact winner strings that are this
  firm), `contact_email`, `consent_at` (**the intake form is the consent
  record**; the field exists for this reason), billing note;
- a `subscription_version` — `cpv_prefixes`, `nuts_prefixes` from their won lots'
  regions, `min_deadline_days`, `max_picks`, `profile_refs`, and the gate knobs
  (`min_relevance`, `min_code_hard`, `min_code_soft`).

Everything goes through `subscriptions.py` — never by opening storage directly
(the project rule in `CLAUDE.md`), and every later change is a **new version**
with its own `effective_from`, never an edit.

### 5.3 The weekly loop, from the customer's side

Monday: an HTML e-mail. Their market, deadline-sorted, each lot with buyer,
deadline, a verdict and the reason behind it; then the near-miss list — lots that
fell just under their profile threshold — with the invitation to reply with a TED
number. A reply becomes a `learned_ref` and a new subscription version. That
reply loop is the onboarding that never ends, and it is the only mechanism we
have for fixing a profile that is wrong.

**Before any firm's first report goes out**, check its gate against its own
history: in the 2026-08-10 backtest, 32 of 46 of the pilot firms' actual wins
were rejected by the relevance gate, and two firms (VLE, Gökser) had *every* win
rejected. A subscriber whose gate rejects their own business will churn in a
month and should never have been switched on. **[BUILD]** this as a pre-flight
check, not as something someone remembers to run.

### 5.4 Money

€179/month, cancel monthly, first period free (§4). Stripe payment link — a
separate page, so it drags no consent banner onto ours. No VAT by default
(Kleinunternehmer) **[CLARIFY]**, the open Steuerberater question from
`GO_TO_MARKET.md`: whether combined revenue across products stays under the
limit. Cancelling is a reply to the weekly e-mail; we write a deactivating
version. No retention flow, no "are you sure" — a monthly product that has to
trap people is not working.

**[CLARIFY] the price itself.** €179 was anchored against the incumbent band
(DTAD ~€200/month, sales-led). The segment that shares our customer prices at
€60 (Vergabepilot) to €99 (Patterno), and the *finding* half of our product is at
feature parity with them. The forecast has to carry the difference, and today it
carries it only in CPV 452. Decide before the first batch, because the price is
printed on the letter.

---

## 6. Instrumentation

One append-only ledger, `data/outreach/outreach.jsonl` **[BUILD]**, one row per
(firm, event): `company`, `batch`, `trade`, `channel`, `sent_at`, `replied_at`,
`trial_started_at`, `paid_at`, `churned_at`, `note`. Batches stratified by trade
means per-trade conversion is a `group by`, not a study.

What it decides, and when:

| question | answered by | available |
| --- | --- | --- |
| does a small contractor respond to a letter at all | first 50 letters | ~2 weeks after send |
| which trade converts | ledger × trade | ~4 weeks |
| is €179 right | conversion at price vs the €60–99 band | after batch 2 |
| do they stay | deactivating versions, cohorts by start month | ~3 months |
| were our picks right, live | `simulation.py check` + grades | ~November (award lag) |

The riskiest assumption in the whole design is still the first row: **whether a
small contractor responds to cold outreach at all.** Fifty letters answer it for
about €50, and no amount of further design substitutes for sending them.

---

## 7. Open decisions, collected

| # | decision | proposed default | blocks |
| --- | --- | --- | --- |
| 1 | Legal basis for holding the target list (Art. 6(1)(f) assessment + information notice) | lawyer question, not ours | **the first batch** |
| 2 | Price: €179, or nearer the €60–99 entrant band | decide before letters print | **the first batch** |
| 3 | Free period: one report or four weeks | four weeks | the landing page |
| 4 | Phone follow-up: used or not | one call at ~10 days | batch design |
| 5 | Who merges alias groups, and when | a person, before each batch | list quality |
| 6 | Framework-only winners: contact or exclude | exclude for batch 1 | list size |
| 7 | Trade market page public, or behind the form | public | the landing page |
| 8 | Pre-filled profile shown before sign-up | yes, with careful copy | intake page |
| 9 | Kleinunternehmer/VAT across products | Steuerberater | invoicing |

And the components that do not exist yet: the `trade` column on the target list
(§1.3), the letter mail-merge (§2.2), the public trade page (§4), the
own-history lookup on the intake page (§5.1), the gate pre-flight check (§5.3),
and the outreach ledger (§6).
