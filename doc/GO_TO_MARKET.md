# GO_TO_MARKET — from running loop to first paying customer

Status: plan, written 2026-08-06. Companion to
[`SUBSCRIPTIONS.md`](SUBSCRIPTIONS.md) (the customer layer this plan sells),
[`SIMULATION.md`](SIMULATION.md) (the outreach evidence engine) and
[`BUSINESS_CASE.md`](BUSINESS_CASE.md) (why the product is worth money).
Uses their vocabulary. Everything here is sales machinery; nothing changes
the loop's spine.

## The play, in one paragraph

Cold outreach to **small construction firms that demonstrably win tenders**
(they are in our awards store as winners), each contacted with their own
verifiable evidence — "the tender you won with 1 bid: we flagged it on
publication day, here is the TED link" — plus this week's live picks in
their trade and region, at **€75/month, cancel monthly**. Small firms are
the deliberate segment: flexible, fast to decide, and the price is sized to
them (large firms need a different pitch and are out of scope here). A
minimal website takes the subscription; onboarding stays manual (append a
line to `subscriptions.jsonl`). Every step is instrumented from letter one
so channel, branch, price and retention decisions are made from ledgers,
not vibes.

## Where the system stands (inventory, 2026-08-06)

Working and scheduled:

- The weekly cycle (Mondays 08:15): download → store → embeddings → grade →
  retrain → predict → operator report → customer HTML → winner simulation →
  dashboard.
- One real pilot customer (gated, German HTML reports, relevance gate at
  1.5% wrong-trade leakage).
- Winner simulation: 11,577 would-be picks for 3,048 real companies in
  `data/ledger/simulations.jsonl`.
- Awards store: 8,520 lot results, 5,229 distinct winner names; **2,566
  small/micro firms, 471 of them with ≥2 wins** — the target pool.
- Winner e-mail addresses exist in the raw award XML (Organizations block);
  not yet extracted.
- `rewind_win.py`: per-firm "would we have recommended your actual win,
  knowing only the past" — the personalized proof generator (~10 min/firm).

The gap: **live evidence is months behind ambition.** 7 graded outcomes,
0 of the simulated picks graded (~90-day award lag). The live track record
becomes quotable around November; until then, evidence comes from the
backtest (outcomes already published) and playback receipts.

## Channel decision — the UWG constraint

Promotional e-mail to German businesses without prior express consent is
unlawful (§7 Abs. 2 UWG; B2B is not exempt), and addresses harvested from
procurement notices were published for procurement contact, not
advertising — a textbook Abmahnung exposure. Channels, in order of
preference:

1. **Postal letter with QR code to the website** — fully legal, and
   Bau/Handwerk firms read letters. ~€1/letter; 471 letters ≈ €500.
2. **Phone** — legal under mutmaßliche Einwilligung, which plausibly holds
   (we offer tenders to firms that demonstrably bid on tenders).
3. **E-mail** — cheapest and most measurable, but carries the legal risk
   above. If ever used: small batches, no tracking pixels, unsubscribe
   honored instantly.

The outreach *content* is channel-independent; nothing below blocks on this
decision. Default for the first batch: letters.

### Channel decision, revised — operator, 2026-08-17

**No letters.** Too expensive per contact for the operator's taste; the
letter template, QR code and print batches are dropped from the build
(ONBOARDING.md §9.7 row 3 is void). What every remaining channel carries
instead is the same thing the QR carried: the firm's personal invitation URL
`https://app.murara.eu/t/<token>` from `invite.py add`, or — for channels
that reach strangers — an open signup page (`app.murara.eu/anmelden`,
**[BUILD]**, LAUNCH.md's deferred form; arrivals held for a person to build
the profile). Consent is recorded at signup exactly as before; the channel
only decides how the URL travels.

**Options on the table, in the operator's order of interest** (Google Ads
deliberately excluded):

| # | channel | audience | how the URL travels | notes |
| --- | --- | --- | --- | --- |
| 1 | **LinkedIn — Message / Conversation Ads** | members who agreed in their settings to sponsored messages; targetable by Position (Geschäftsführer, Inhaber), Branche, Region | LinkedIn's own inbox ad, priced per send, our text with the `/t/` link to a named firm or the open form | EU targeting supported again since Oct 2024 (LinkedIn Marketing Solutions help a1514689); the recipient's opt-in is LinkedIn's — the cleanest paid inbox route |
| 2 | **LinkedIn — direct messages** (by hand from the operator's profile, Sales Navigator to find the person; or a tool such as Lemlist / Waalaxy / Expandi) | Geschäftsführer/Inhaber of the target-list firms found by company name | personal message with the `/t/` link | the legal position (§7 Abs. 2 UWG, OLG Hamm 18 U 154/22; LinkedIn's terms on automation) was laid out and is the operator's accepted business risk — it is not re-raised |
| 3 | **Xing** — ads or direct messages | the German B2B network, shrinking, still strong among 45+ Handwerk owners | as 1–2 | same opt-in logic as LinkedIn |
| 4 | **Meta ads (Facebook / Instagram)** | small Handwerk firms are more present here than on LinkedIn — company pages, local groups | feed/story ad → open form | needs `/anmelden` |
| 5 | **Trade press and association newsletters** — Deutsches Handwerksblatt, Bauhandwerk, BM, Fachverbände, Handwerkskammer newsletters | our segment, by trade; the publisher holds the double-opt-in | sponsored placement → open form | |
| 6 | **Vergabeportale** (DTVP, evergabe, subreport, ibau) | contractors *while* they look for tenders | partner listing / advertising → open form | the most on-target audience there is |
| 7 | **Handwerkskammern / Innungen** | every target-list firm is a member of one | a mention in a Kammer newsletter or at an Innung meeting | free, trusted; a person's call, not a tool's |
| 8 | **Phone** | firms that demonstrably bid | „ich schicke Ihnen den Link" | the mutmaßliche-Einwilligung tier of §2.1 above |

**Open:** which of 1–3 starts, and therefore whether the first build is
`--channel` on the `invited` event (named firms, `/t/` links) or the open
`/anmelden` form (strangers). Everything else in ONBOARDING.md §9 —
report by e-mail, trial clock, the ask, `/y/`, results notes — is
channel-independent and stands.

## Pricing

**One flat price for everyone, anchored at 1% of a median deal per year**
(decision 2026-08-06; supersedes the earlier €75 idea as too low).

From the awards store, lots actually won by small/micro firms (winning-bid
amounts > €1k):

| Slice | n | median deal | p25 | p75 |
| --- | --- | --- | --- | --- |
| all trades | 2,746 | €205k | €89k | €450k |
| 452 (campaign trade) | 906 | €224k | €99k | €537k |
| 453 | 604 | €267k | €117k | €542k |
| 454 | 749 | €139k | €70k | €293k |

1% of the median 452 deal per year = €2,240 → **€179/month, cancel
monthly, first report free.** Sanity checks: the p25 firm (€99k deals)
pays 2.2% of one deal per year, the p75 firm 0.4% — a 5× spread, narrow
enough that one flat price stays fair; lead platforms with no prediction
charge €170–330/month, so €179 sits at the bottom of the dumb-alternative
range; one won median deal at 4.5% margin ≈ €10k profit ≈ 4.7 years of
subscription. Revisit only with conversion data.
(Kleinunternehmer/VAT stance as elsewhere: no VAT by default; one
Steuerberater question whether combined revenue across products stays under
the limit.)

## Build order (phases in time — no component is cut)

1. **Branch ranking from the backtest** — extend `rewind_all.py` with a
   per-trade (cpv3) table over the global replay: scored lots, graded,
   base rate, flags, hit rate, lift. Outcomes are already published for
   the replayed period, so this answers "which branch first" *now* instead
   of November. The first outreach batch goes to the best-backtesting
   trades; slices with chronic "no pick this week" weeks are churn risks
   and wait.
2. **Target list** — `outreach.py`: winner contact extraction from the raw
   award XML (name, e-mail, phone, city, size), aggregated per company
   (wins, trades, regions), joined with the simulation ledger (how many
   picks their market currently gets), filtered to small/micro with ≥2
   wins. Output: `data/outreach/targets.csv` (private, gitignored — it is
   personal data; the script is committed).
3. **The outreach asset, per firm** — generated, not written: (a) the
   playback receipt for one of their actual wins, with TED link; (b) 2–3
   live picks in their trade+region from the current cycle; (c) price and
   the website address. One template, mail-merged.
4. **Campaign ledger** — `data/outreach/outreach.jsonl`, append-only like
   every ledger in this repo: one row per (firm, contact event) —
   channel, batch, date sent → replied → trial → paid → churned. Batches
   are stratified by trade so per-branch conversion falls out of a join.
   This ledger *is* the funnel instrumentation; no analytics tool.
5. **Website** — one landing page: pitch, one anonymized real report as
   sample, price, intake form (firm name + e-mail + trade/region or "paste
   the TED link of a tender you won"), Stripe Payment Link, Impressum +
   Datenschutz. No self-service onboarding — at <50 customers, appending
   the subscription line by hand is correct.
6. **E-mail delivery of the weekly report** — the only *required* new
   plumbing before taking money: reports are files on disk today. A small
   delivery step at the end of the loop sends the already-e-mail-ready
   HTML to paying subscribers (transactional mail to a customer is not
   UWG-restricted).

## Decisions and their data sources

| Decision | Source | Available |
| --- | --- | --- |
| Which branch to target first | Backtest per-cpv3 table (phase 1) | now |
| Which firms to contact | `targets.csv` (phase 2) | now |
| Is €75 right | Business case + competitor prices | decided above |
| Which branch *converts* | Outreach ledger, batches stratified by trade | ~4 weeks after first sends |
| Retention rate | `subscriptions.jsonl` deactivation versions, cohorts by start month | ~3 months after first customers |
| "For X% of firms our picks were right" (live) | Simulation grades (`simulation.py check`) | ~November (award lag; cannot be accelerated) |

The riskiest assumption is not the model — it is whether a small contractor
responds to outreach at all. Fifty letters into the best-backtesting trade
answer that for about €50.
