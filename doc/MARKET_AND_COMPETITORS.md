# MARKET_AND_COMPETITORS — the market, what others sell into it, what we build

Written 2026-08-10. Companion to [`BUSINESS_CASE.md`](BUSINESS_CASE.md) (why the
product is worth money), [`GO_TO_MARKET.md`](GO_TO_MARKET.md) (the play and the
price decision) and [`RELEVANCE.md`](RELEVANCE.md) (the gate whose receipts are
quoted here).

**Rules of evidence for this file.** Every number carries its source, and
sources are of three unequal kinds:

- **ours** — computed from `data/store/*.parquet` through `market.py`'s loaders,
  or from a backtest report in `data/reports/`. Reproducible; the command is
  named.
- **literature** — cited in `BUSINESS_CASE.md` / `METHODS.md`.
- **vendor** — a competitor's own marketing copy. It says what they *sell*, not
  what their product *does*, and a feature absent from a landing page may still
  exist. Nothing in §2 and §3 has been verified by using the products — with one
  exception: the signup table in §2.3 was read off the live forms in a browser
  on 2026-08-10, so its field lists are observed rather than claimed.

---

## 1. The market, from our own store

Store as of 2026-08-10: **24,023 lots, CPV division 45 only**, published
2024-01-19 to 2026-08-10. The store is not a continuous archive — 12 publication
months clear the coverage floor, and only **4 of those are mature** enough (award
resolution ≥ 35%) to carry a bidder rate. Every rate below is over those months
and prints its denominator. Reproduce with `python market.py rank`.

### 1.1 Size and contestedness by trade

Trades are defined by words in [`trades.txt`](../trades.txt), matched against the
notice title (scope `core` — the lot *is* this work, biddable as a main
contractor). 44 of 47 trades clear 30 lots in covered months.

| trade | lots/mo | median award | ≈ €/year | 0/1 bids (mature) | n | no award at all |
| --- | --- | --- | --- | --- | --- | --- |
| Maurerarbeiten | 70.1 | €964 k | 811 M | 4% | 174 | 6% |
| Straßenbau | 22.7 | €2.17 M | 591 M | **23%** | 60 | 8% |
| Elektroinstallation | 76.3 | €565 k | 518 M | 9% | 192 | 6% |
| Wärmedämmung und Fassade | 78.8 | €379 k | 359 M | 8% | 201 | 12% |
| Lüftung, Klima und Kälte | 71.2 | €403 k | 345 M | 6% | 202 | 6% |
| Heizungstechnik | 74.3 | €381 k | 340 M | 5% | 201 | 6% |
| Fenster, Türen und Tore | 81.2 | €269 k | 262 M | 6% | 211 | 7% |
| Trockenbau | 84.7 | €230 k | 234 M | 3% | 207 | 5% |
| Stahl- und Metallbau | 99.4 | €193 k | 230 M | 4% | 270 | 6% |
| Tischler- und Schreinerarbeiten | 94.9 | €199 k | 227 M | 5% | 231 | 5% |
| Sanitärinstallation | 62.2 | €290 k | 216 M | 6% | 168 | 5% |
| Gleisbau und Fahrleitung | 11.1 | €1.20 M | 160 M | **49%** | 39 | 28% |
| Gebäudeautomation und Datennetz | 32.8 | €392 k | 154 M | **16%** | 89 | 6% |
| Aufzüge und Fördertechnik | 31.9 | €65 k | 25 M | **23%** | 95 | 10% |
| Blitzschutz und Erdung | 12.2 | €34 k | 4.9 M | **12%** | 32 | 17% |

"no award at all" = `result_code == 'clos-nw'`, a procedure that closed without
finding anybody — the strongest thin-competition signal in the data.

Store-wide over the same months: **9% of 4,932 awarded lots ended with 0 or 1
bidder**, median 5 bidders. Literature for context: single-bid share EU-wide rose
23.5% → 41.8% between 2011 and 2021 (ECA SR 28/2023, via `BUSINESS_CASE.md`).

### 1.2 Two structural facts about finding the work

Both are counting exercises over the store, independent of anyone's search
behaviour:

- **CPV scatter.** A trade's most common `cpv_main` carries between **15% and
  67%** of its biddable lots (Tischler 15%, Straßenbau 23%, Blitzschutz 63%,
  Gerüstbau 68%). Reaching 90% of a trade takes 6–43 distinct codes. The
  mechanism is written up in [`cpv_trade_roots.txt`](../cpv_trade_roots.txt):
  CPV-45 mixes "what is built" with "what work is done", so the code is a filing
  decision by the buyer's clerk, not a description of the work.
- **Title versus body.** For most trades the majority of lots mentioning the
  trade mention it only in the body — a line item inside a bigger package rather
  than a lot you bid as a main contractor. Straßenbau: 274 by title, 1,631 body
  only. Blitzschutz: 148 / 484. Stahl- und Metallbau: 1,207 / 1,088.

The consequence is a signal-to-noise problem, not an invisibility problem: a
full-text search for "straßenbau" returns ~1,905 lots a year of which **14% are
biddable as a main contractor**; a CPV filter on the obvious code cuts the noise
and loses 77% of the real ones.

**What we do not know.** Nothing in this repo measures how a contractor actually
searches today — which portal, whether their saved profile is CPV codes,
keywords, or a region. Any claim of the form "firms only search by X" is
currently unsupported and must not appear in customer-facing material. One
question on the outreach call answers it (`GO_TO_MARKET.md` phase 3).

---

## 2. What the competitors build

Source for this section: vendor marketing pages, read 2026-08-10. See the
evidence rule at the top.

### 2.1 Three generations in one market

**Incumbent coverage services.** Sell breadth plus human curation, and sell it
through a sales conversation.

- **ibau** — 450,000+ notices a year, "145 researchers with AI data processing",
  plus construction *project* information ahead of the tender. Its distinctive
  product is **LV-Suche**: search inside the Leistungsverzeichnis for trade
  terms, brand names, competitor products, quantities. Their copy claims
  "significantly higher hit rates than other providers" and that it multiplies
  result volume. This is the direct commercial answer to §1.2's title-versus-body
  fact, and it predates us.
- **DTAD** — "over 1 million Auftragsdaten" a year, 450,000+ public tenders,
  100+ portals, CPV search *and* Volltextsuche, saved Suchprofile with daily
  mail, verified contact data. Positions as volume **plus** "KI-Technologie und
  manueller Datenveredlung". Claims **"mehr als 5.500 Unternehmen"** as
  customers — the only competitor customer count found, and a useful scale
  marker for the whole German market.
- **Vergabe24**, **Deutsches Ausschreibungsblatt**, **evergabe.de** — the same
  category at lower price or with e-submission attached; DAB now ships a KI chat
  over the Vergabeunterlagen.

**AI-native matching and drafting.** Sell noise reduction and document work,
self-serve, monthly.

- **Vergabepilot.ai** — explicitly targets Handwerk. 300+ portals, semantic
  search ("Inhaltliche Suche statt Stichworte — KI versteht den Kontext"), AI
  summaries of the Vergabeunterlagen, an assistant chat, team boards. Names the
  customer's problem as "Zu viele irrelevante Treffer" and quotes a customer
  testimonial of **"75 Prozent weniger irrelevante Ergebnisse"**.
- **Patterno** — "KI-native Relevanzbewertung" across 4,500+ European portals,
  document analysis, offer drafting.
- **Tenderzen**, **Leto**, **Brainial**, **Altura**, **Tendery.ai**, **BidFix** —
  requirement extraction, compliance checking, bid qualification, drafting.
  Tenderzen claims "semantic reference matching" (auto-linking a firm's existing
  projects to new requirements) and asserts no competitor has it.

**Market intelligence.** **Hermix** — pan-European analytics: who buys and sells
what, competitor identification, supplier tracking. Enterprise framing, no
public price.

### 2.2 What nobody appears to sell

**A per-lot forecast of how contested a tender will be.** The nearest claim found
is DTAD's *"Prognosen zum Vergabeverhalten und möglichen Bietern"* — from 16+
years of award history, which firms are likely to be in a market. That is
competitor *identification*, not "this lot will close with 0 or 1 bids". Hermix
does market-level competitor analytics. Every comparison table found lists
semantic matching, requirement extraction, compliance and drafting; none lists a
bidder-count prediction.

Treat this as "not advertised", not as "does not exist". The cheap check is a
free tier (Vergabepilot has one) and a demo.

### 2.3 How you become their customer

Read off the live signup forms on 2026-08-10, not off the marketing copy.

| vendor | how a new customer starts | gate |
| --- | --- | --- |
| Deutsches Ausschreibungsblatt | search free, **no registration at all**; full texts are the paid Komfort tier | none |
| Vergabepilot.ai | self-serve: e-mail, password, confirm, first name, last name, company — 6 fields, **no credit card, cancel anytime**. A 45-minute demo (Mo–Mi 11:30–12:15) exists as an *option* | account |
| Vergabe24 | free trial, minimum contract term | account + term |
| Patterno | self-serve monthly/annual per its own comparison page (pricing page 404s — unverified) | unclear |
| DTAD | "Unverbindlicher Testzugang" is a form — **Firmenname, Rechtsform, Firmen-Website, Vorname, Nachname, geschäftliche E-Mail, geschäftliche Telefonnummer**, all mandatory, plus two marketing-consent checkboxes — and the trial comes "inklusive persönlichem Onboarding" | **a salesperson calls** |
| ibau | no self-serve and no published price: "Infos anfordern", "Kostenlose Präsentation anfordern", 0800 number | **appointment or nothing** |

The split runs along the same line as the price bands in §3: the incumbents we
anchored €179 against will not let a Handwerksbetrieb see the product without a
phone number and a sales conversation; the cheaper AI-native entrants removed
that gate years ago.

**What none of them removed is the portal.** Even the zero-friction end of that
table gives the customer an account, a password, a search profile to configure
and a login to remember. That is the gap §4.4 aims at.

### 2.4 Where each of our differentiators actually stands

| claim | status |
| --- | --- |
| "they only search by title and CPV" | **false.** DTAD documents Volltextsuche alongside CPV; TED's own API takes full-text queries. |
| "we surface work hidden in the body" | **taken.** ibau's LV-Suche sells exactly this, deeper (LV text, not just the notice body). |
| "we send you fewer, more relevant tenders" | **contested.** Vergabepilot's landing page and testimonial make the same claim to the same segment at a third of the price. |
| "your profile is built from contracts you won" | **ours.** Competitors start from something the customer expresses; we start from award history joined to the firm. Not seen elsewhere. |
| "the relevance number is published and auditable" | **ours, narrowly.** 1.5% wrong-trade leakage at 60.0% recall on hand-labelled lots, versus a testimonial with no denominator. Only matters to a buyer who asks how it was measured. |
| "we tell you how contested a lot will be" | **ours** (see §2.2). |
| "no account, no password, no login" | **ours** (see §2.3, §4.4). One competitor lets you search without registering; none delivers a personalised weekly market without an account. |

---

## 3. Price models

| vendor | price | model | contract |
| --- | --- | --- | --- |
| Vergabe24 | €19–90 / month | regional → nationwide tariff | minimum term |
| Vergabepilot.ai | €0 / €60 / €125 / custom | credits + AI model tier, seats on the upper tier | monthly or annual (2 months free) |
| Patterno | €99–2,499 / month | Starter / Scale / Enterprise | monthly or annual (−10%) |
| Tenderzen | €269–1,499 / month + pay-per-use credits | per-use credits on top of the tier | monthly |
| DTAD | ~€200 / month upward (~€2,400 / year, third-party report) | negotiated packages | annual, sales-led |
| ibau | not published | negotiated, LV-Suche as an add-on | sales-led |
| Deutsches Ausschreibungsblatt | free search, paid comfort tier unlisted | freemium | — |
| **TenderMining (decided)** | **€179 / month** | flat, one price for everyone, first report free | **cancel monthly** |

Three axes matter more than the absolute numbers:

1. **Self-serve monthly versus sales-led annual.** The incumbents sell a
   negotiated annual contract; the AI-native entrants sell a card payment and a
   cancel-anytime month. We chose the entrant model.
2. **What scales the price.** Region (Vergabe24), credits and AI model tier
   (Vergabepilot, Tenderzen), seats (Patterno, Vergabepilot Ultimate). Ours
   scales on nothing — one flat price, defended in `GO_TO_MARKET.md` as ~1% of a
   median deal per year and fair across a 5× deal-size spread.
3. **Where €179 lands.** `GO_TO_MARKET.md` anchored it against "lead platforms
   €170–330/month", which is the **incumbent** band (DTAD ~€200). Against the
   segment that shares our customer — Vergabepilot at €60, Patterno at €99 — we
   are 1.8–3× the price. That is defensible only if the forecast carries it; the
   finding half of our product is at feature parity with a €60 tool.

---

## 4. What we build

The weekly cycle (Mondays 08:15): download → store → embeddings → grade →
retrain → predict → operator report → customer HTML → winner simulation →
dashboard. Three pieces face the customer.

### 4.1 The relevance gate — "is this lot your business?"

Mechanism: the subscription carries `profile_refs` — publication numbers of
tenders the firm **actually won** (4–8 per pilot). New lots are scored by
embedding similarity against that profile, with CPV demoted to a coarse recall
guard and a trusted-code channel beside the text channel
([`RELEVANCE.md`](RELEVANCE.md)). Customer replies to the near-miss list become
`learned_refs` and re-shape the profile.

Receipts (configuration H+K, `RELEVANCE.md`): **recall 60.0%, wrong-trade leakage
1.5%, admitted volume 5.1%**, and 19/19 on the operator-labelled benchmark. A
configuration that misjudges any benchmark case is rejected regardless of its
aggregate numbers.

### 4.2 The competition flag — "how contested will it be?"

A CatBoost classifier over notice-time features predicts whether a lot ends with
0 or 1 bidder ([`TRAINING.md`](TRAINING.md), leakage rules enforced by
assertion). Live grading is still thin (18 graded outcomes; an award publishes a
median 84 days after the call), so the quotable numbers come from the forward
backtest, which replays history as the loop would have run it
(`data/reports/backtest_2026-08-10.md`):

| | value |
| --- | --- |
| tenders examined while open | 11,709 (3,465 with a published result) |
| base rate ("chance") | 9% ended 0/1 bids |
| alarms raised | 1,139 (391 checkable) |
| **precision** | **16%** (95% CI 13–20%) — **1.84× chance** |
| recall | 21% (95% CI 17–26%) |

Per CPV group, and this is the part that decides which trades we may quote a lift
to:

| code | trade group | chance | alarms right | lift |
| --- | --- | --- | --- | --- |
| 452 | Hochbau (Maurer, Straßenbau, Stahlbau, Dachdeckung, Abdichtung…) | 10% | 24% (45/187) | **2.30×** |
| 454 | Baufertigstellung (Tischler, Fenster, Boden, Putz…) | 7% | 8% (4/48) | 1.28× |
| 453 | Bauinstallation (Elektro, Heizung, Lüftung, Sanitär, GA, Blitzschutz…) | 8% | 9% (12/137) | 1.04× |
| 450 | Bauarbeiten allgemein | 10% | 7% (1/15) | 0.67× |
| 451 | Baureifmachung | 3% | 0% (0/4, thin) | — |

**There is no measured lift in CPV 453 or 450.** Those trades get the market
facts and the relevance gate; a lift number must not appear in their letter.
Awkwardly, 453 is where the highest uncontested rates sit (Aufzüge 23%,
Gebäudeautomation 16%, Blitzschutz 12%).

Benchmark from the literature: precision ≈ 0.61 at recall ≈ 0.34 on 334k TED
notices at a 21% base rate (Acikalin et al. 2023, `METHODS.md` §4.5). Our 16% at
a 9% base rate is well under that; the gap is a to-do, not a selling point.

### 4.3 What lands in the customer's inbox

The weekly HTML (German) contains: every open lot in their market with deadline,
buyer, a verdict ("wenige Bieter erwartet" / "durchschnittliche Chancen" / "viele
Bieter erwartet") and the features behind it; the **near-miss list** of lots that
fell just under the profile threshold, with an invitation to reply with a TED
number so the profile learns; and the deadline-sorted ordering. Around it:
`market.py` for trade-level market facts, `playback.py` for the per-firm receipt
("would we have recommended your actual win, knowing only the past"),
`outreach.py` for the contact list.

### 4.4 How a customer starts with us — the no-login product

Against §2.3, our onboarding is the smallest one available, because there is no
portal to onboard into:

> **Firm name, e-mail, and the TED number of one tender you won.** No account,
> no password, no seat, no login. The report is an e-mail.

The third field is the whole trick: it is the `profile_refs` input (§4.1), it is
**public** data rather than personal data, and it replaces the twenty minutes
every competitor spends making the customer describe their own trade. "Paste one
contract you won" is a smaller ask than "book 45 minutes", and it produces a
better profile than anything the customer could type.

What that does **not** remove, and the landing page must not pretend otherwise:

- **GDPR applies to us.** We store a firm name, a contact e-mail and their won-
  tender references, and we send them mail. Impressum and Datenschutzerklärung
  are required (`GO_TO_MARKET.md` phase 5 lists both).
- **Consent for the e-mail is not optional** — §7 Abs. 2 UWG, the same rule that
  makes postal letters the default outreach channel. The intake form *is* the
  consent record and has to say so in its own text.
- **A cookie banner is needed only if we set non-essential cookies.** The
  decision to have no analytics tool ("this ledger *is* the funnel
  instrumentation", `GO_TO_MARKET.md` phase 4) is what makes a banner-free page
  possible. It stops being true the day anyone bolts on Google Analytics. The
  Stripe payment link is a separate page and does not drag consent onto ours.
- **Onboarding is still manual on our side.** Appending the subscription version
  by hand is correct under 50 customers, but it means the honest promise is
  "your first report on Monday", not instant access. Say that.

---

## 5. Where we honestly stand

**The relevance gate does not yet find a customer's own business.** Across all 7
pilot firms in the 2026-08-10 backtest, their 52 actual wins decompose:

| stage | wins surviving |
| --- | --- |
| the firm's actual wins | 52 |
| call notice present in the store | 52 |
| published inside the replay window | 46 |
| passes CPV + NUTS + deadline filters | 46 |
| **entered their market** | **14** |

Nothing is lost to data coverage or the coarse filters — 32 of 46 were rejected
by the relevance gate, and two firms (VLE, Gökser) had **every** win rejected.
Mitigation: the replay rebuilds each profile as-of the cutoff, so early weeks
judge with one or two references — the failure mode the single-reference-profile
work of 2026-08 addressed. So 14/46 is a floor and 60.0% is the labelled-set
benchmark; the live figure is somewhere between. It is not a "we find
everything" story either way, and two pilots need their profiles looked at before
any receipt is generated for them.

**The live track record is still months out.** 18 graded outcomes; 0 of the
simulated picks graded. Quotable around November (`GO_TO_MARKET.md`).

**The price sits above the segment.** See §3. Decide before the first letter
batch whether €179 is carried by the forecast alone.

**One behavioural assumption is untested.** How firms search today (§1.2). It is
one question on the first fifty calls.

## 6. What the pitch may claim today

Ordered by how well each is backed:

1. **Market facts for their trade** — volume, median award, €/year, the 0/1-bid
   share, the "closed without an award" share. Ours, reproducible, and no
   competitor publishes them.
2. **A profile built from contracts they won**, named in the letter by
   publication number.
3. **The competition forecast** — for CPV 452 trades, "1 in 10 of your trade's
   tenders ends uncontested; among the ones we flag, 1 in 4 does", said as a
   replay result against outcomes already published, not as a live track record.
4. **A published relevance number**, if the buyer asks how it was measured.
5. **No account, no login, one form field of setup** (§4.4) — true against every
   competitor checked, and the easiest of these to verify in the reader's own
   browser.

Not claimable: that competitors only search by title or CPV; that we surface work
others cannot (ibau's LV-Suche); that "fewer, more relevant" is unique
(Vergabepilot); a lift number in CPV 453 or 450; any live track record before the
grades arrive.
