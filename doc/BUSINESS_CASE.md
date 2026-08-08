# TenderMining — Business case for competition prediction

Who profits from predicting tender competition, and by how much. Every number below
is sourced; sources were checked on 2026-07-30. Companion to [`METHODS.md`](METHODS.md)
(how to build the estimator) and [`FINDINGS_literature.md`](FINDINGS_literature.md)
(what accuracy is achievable — note the corrected benchmark: precision ≈ 0.6 on the
single-bid flag, **not** 70–84 %).

## The claim to be backed

A mid-size construction firm loses most of the tenders it bids on, each losing bid
costs real money, and margins are thin — so a tool that improves *which* tenders it
bids on is worth six figures a year to a single firm. The four legs of evidence:

## 1. How often do bidders lose?

| Evidence | Number | Source |
| --- | --- | --- |
| German above-threshold construction lots (our own XML-parsed data, 223 lot results, July 2026) | median **5** bids/lot, mean **6.0** → naive win chance ≈ **1 in 5–6** | [`FINDINGS_ted.md`](FINDINGS_ted.md) §2 (corrected competition figures) |
| EU-wide, all sectors, 2011→2021 | average bidders per procedure fell **5.7 → 3.2** | [ECA Special Report 28/2023](https://www.eca.europa.eu/en/publications/sr-2023-28) ([PDF](https://www.eca.europa.eu/ECAPublications/SR-2023-28/SR-2023-28_EN.pdf)) |
| Single-bid contracts, EU-wide 2011→2021 | share nearly doubled **23.5 % → 41.8 %** | ECA SR 28/2023 (as above) |
| Single-bid lots, German construction (our data) | **12 %** of lots got exactly 1 bid | [`FINDINGS_ted.md`](FINDINGS_ted.md) §2 |

Reading: a firm bidding on typical German construction tenders **loses ~80 % of its
bids** — while a large uncontested segment exists where whoever shows up wins.

## 2. What does a (losing) bid cost?

The thinnest leg — no recent German study found. Two independent UK sources, two
decades apart, agree:

| Evidence | Number | Source |
| --- | --- | --- |
| University of Reading survey, 179 firms (Hughes; dated but the classic reference) | average bid cost **0.57 % of project value** across winning and losing bids; winning contractor tender ≈ £60k | [summary at Evolution5](https://evolution5.co.uk/how-much-does-it-cost-to-tender-a-construction-project/) |
| UK trade guidance, Q4 2025 | SME bid preparation **£500–£4,000 per tender**, or **0.3–1.0 % of contract value** for complex submissions | [costestimator.co.uk](https://costestimator.co.uk/tender-and-bid-in-construction-understanding-the-key-differences/) |

Central estimate used below: **0.5 % of bid volume**, uncertainty ±0.2 pp.
**Open to-do:** validate against one or two German contractors' real Kalkulation
effort before quoting this in any customer-facing material.

## 3. What do bidders earn?

German Bauhauptgewerbe, return on sales (Umsatzrendite):

| Evidence | Number | Source |
| --- | --- | --- |
| Sector median | **10.2 % (2020) → ~7.3 % (2023)** | [Bauindustrie, Betriebswirtschaftliche Lage](https://www.bauindustrie.de/zahlen-fakten/publikationen/brancheninfo-bau/betriebswirtschaftliche-lage-im-bauhauptgewerbe) (DSGV calculations) |
| Firms > €10 M revenue (the ones bidding on TED-sized tenders) | **4.4–5.1 % pre-tax (2023)** | [Bauindustrie, Wirtschaftliche Situation](https://www.bauindustrie.de/zahlen-fakten/publikationen/bauwirtschaft-im-zahlenbild/wirtschaftliche-situation-im-bauhauptgewerbe) (annual-accounts statistics) |

The sector median is inflated by tiny owner-run firms (owner's unpaid wage sits in
"profit"); the size-class figure **4.5 %** is the honest number for a mid-size bidder.

## 4. The model, assembled

Reference firm: **€20 M revenue** contractor at the sourced **4.5 %** margin
→ **€900k pre-tax profit/year**.

- To win €20 M of work at the German-construction win rate (~1 in 5.5, leg 1),
  it must bid on roughly **€110 M** of tenders per year.
- At 0.5 % bid cost (leg 2), that is **~€550k/year spent on bidding** — of which
  **~€440k goes into bids that lose** (≈ half of annual profit).

Value of competition prediction, conservatively:

1. **Avoiding crowded tenders.** Lifting the effective win rate from 18 % to just
   22 % (4 points — achievable with an imperfect flag; see the precision ≈ 0.6
   benchmark in `FINDINGS_literature.md`) means the same €20 M of wins needs only
   ~€90 M of bid volume: **~€100k/year less bidding cost**, straight to profit
   (an ~11 % profit lift).
2. **Finding the uncontested segment.** 12 % of German construction lots got exactly
   one bid (41.8 % of contracts EU-wide, ECA). Each such tender routed to the firm is
   a near-certain win: **one extra €2 M lot/year at 4.5 % margin = €90k**.

**Order of magnitude: €100–200k/year of value per mid-size firm**, against current
tender-platform subscriptions (DTAD, ibau) of €2–4k/year that provide leads with no
prediction. The value-vs-price gap is the product opportunity; a €5–20k/year price
point is supportable.

## Sensitivity and caveats

- **Bid cost 0.3 % instead of 0.5 %** → bidding spend €330k, losing share €265k;
  lever 1 shrinks to ~€60k/year. The case weakens but holds.
- **Win probability = 1/n assumes symmetric bidders.** Incumbents beat 1/n,
  newcomers do worse; for a newcomer the *triage* value (don't waste money where an
  incumbent is entrenched) is larger, not smaller.
- **ECA's 3.2-bidder average spans all sectors**; our construction sample shows 5–6.
  The model uses the less favorable 5.5.
- **The flag is not an oracle.** At the published state of the art
  (precision ≈ 0.61 at recall ≈ 0.34 on TED text, Acikalin et al. 2023 — see
  `METHODS.md` §4.5), flagged tenders are single-bid ~60 % of the time vs a 12–21 %
  base rate: a 3–5× enrichment. The worked levers above assume modest improvements
  consistent with that, not perfect foresight.

## Other beneficiaries (not quantified here)

- **Public buyers (Vergabestellen):** must estimate contract value before publishing
  (§ 3 VgV); a bad estimate can force cancelling and re-running the procedure. A
  budget sanity-check against historical awards is the buyer-side product.
- **Authorities / researchers:** collusion screening on bidding patterns (the
  Fazekas et al. 2026 line of work) — high social value, hard sales channel.
- **Tender platforms (DTAD, ibau, Vergabe24):** sell leads today, no predictions;
  licensing target rather than competitor.
