# LAUNCH — what to build now, independent of channel

Written 2026-08-10, revised the same day after a design session on the customer
lifecycle. Companion to [`ONBOARDING.md`](ONBOARDING.md) (the letter funnel);
this document extracts what has to exist **whichever way a customer arrives**
and now also fixes the trial-to-paid lifecycle that session settled. The
letter-specific builds (mail-merge, `trade` column, alias merging, print-API
integration) stay off this list; they wait on the channel decision and the
legal sign-off ([`LEGAL_BASIS_TARGET_LIST.md`](LEGAL_BASIS_TARGET_LIST.md),
ONBOARDING.md §7 decision #1).

Decisions settled here, to be read against the open-decisions table in
`ONBOARDING.md` §7:

- **Free period: four weeks** (decision #3, the doc's own recommendation).
- **Trade market page: public when it comes — deferred until the inbound
  channel opens** (decision #7, refined). With QR-only acquisition there is
  no stranger to show it to; it returns as the SEO play (§4).
- **Signup is QR-personalised; no login, no manual input, no confirmation
  step.** Every reachable customer is already known (decision #8's default,
  sharpened: we do not ask a customer to confirm what we already verified).
- **No TED-number field, ever.** Customers don't know publication numbers;
  finding a firm's wins is our job.
- **Playback receipts stay internal.** Shown to a customer they read as
  cheatable ("you made this up afterwards"); they are our quality check, not
  customer-facing evidence. The customer-facing evidence is the results note
  (§3), which grades picks the customer *watched arrive* — nothing
  retrodicted.
- **Price (decision #2): still open, and no longer blocking.** No price
  appears at signup; the ask is "four weeks free". The deadline moves to
  before the first trial ends.
- **An open form for unknown visitors is deferred** until a channel exists
  that produces them (Google, press). When it comes, its arrivals are held for
  manual review; nothing about the QR path changes.

---

## 1. The signup page

One page per invited firm, reached through its QR link. The token identifies
the firm — the target list is already in the database — so the page *shows*
rather than asks (careful copy per decision #8: their trade, their market's
numbers; never anything that reads as surveillance):

- their firm name and trade;
- their trade's market figures (rendered from `market.py`, inline);
- **one input: the e-mail address** the weekly report should go to.
  Submitting it is the consent record (`consent_at`, ONBOARDING.md §5.2). The
  consent text names what will come: the weekly reports, and — after the
  trial — occasional result updates. That sentence is what later makes the
  win-back channel (§3) lawful, so it is part of the design, not boilerplate.

No account, no password, no login (per `MARKET_AND_COMPETITORS.md` §4.4), and
no confirmation step: identity was resolved before the letter was printed
(alias merge, decision #5), and the pre-flight check (§2) verifies the profile
against the firm's own history without asking anyone. If our data is ever
wrong, the first weekly report — read by the actual businessperson — is a
better error detector than a yes-button clicked by whoever opened the mail.

A visitor without a valid token sees the root page: one sentence about the
product, a contact address, Impressum and Datenschutzerklärung — not a form.

**[BUILD]** the page, plus Impressum and Datenschutzerklärung (legally
required; the Datenschutzerklärung also carries the long-form Art. 14
information the legal doc calls for). No cookie banner: analytics stay
cookieless, and payment lives on a separate Stripe page precisely so its
consent machinery never touches ours (ONBOARDING.md §5.4).

## 2. From e-mail entry to running subscription — automatic

1. The firm is known from the token; the profile is already built from its
   won lots' `profile_refs`.
2. **Gate pre-flight check** (ONBOARDING.md §5.3 **[BUILD]**): replay the
   firm's own won lots against its proposed gate. In the 2026-08-10 backtest,
   32 of 46 pilot firms had wins rejected by their own gate; two firms lost
   every win. Pass → activate: subscription version written through
   `subscriptions.py`, first weekly report next Monday. Fail → **not
   activated**, queued for a person to fix the profile first; activating a
   gate that rejects the customer's own business converts acquisition cost
   into guaranteed one-month churn.
3. The customer gets one confirmation e-mail saying what will arrive and
   when. It carries the same footer links as everything else (§3).

## 3. The lifecycle: trial → ask → results → win-back

The paths and states, then the rules:

```
signup ── 4 free weekly reports ── the ask (once) ──┬── yes → paying
                                                    │
                                                    └── silence → picks stop
                                                          │
                                             results notes, as outcomes
                                             publish (each carries the ask)
                                                          │
                                            yes, any time — even months later
```

- **The ask happens exactly once**, at the end of the four weeks, on what the
  customer already knows: were the picks relevant. It does not wait for
  graded outcomes — award notices lag ~3 months and some never publish, so
  "when all results are in" is a moment that never arrives.
- **After the trial: no more picks.** The only further e-mails are **results
  notes**: when enough of the customer's own trial picks have published
  outcomes ("of the 12 lots we picked for you, 3 closed with a single bid —
  here is what they went for"), a note goes out, dynamic per lot, each
  carrying "do you want to subscribe now?". To a paying customer the same
  note is retention proof. This is the entire post-trial conversion channel,
  which is why the grading machinery ranks high in the order of work (§7).
- **The yes-link never expires.** A customer who needs approval, or winter,
  can subscribe months later; activation is just a new subscription version
  with its own `effective_from`. Nothing to rebuild, nothing to re-ask.

### Feedback in every report

Every lot the report shows — picks and near-misses alike — carries the same
two tokened links: **"ist unser Geschäft"** and **"nicht unser Geschäft"**.
No login, no free text, no typed TED numbers anywhere (a customer will not
hunt down a publication number; the token already names the lot). One
question only, because one question is all a customer can answer for us:
**relevance**. Whether a relevant lot gets *recommended* is our pick policy
(the competition forecast may say "your business, but contested — skip"),
stated in the email as verdict + reason; feedback does not override it.

- *nicht unser Geschäft* → rejection event; on picks it also measures the
  wrong-trade leakage claim live, per customer.
- *ist unser Geschäft* on a near-miss → the boundary signal the reply loop
  was designed to catch: a `learned_ref` and a new subscription version, one
  click instead of a typed reply. On a pick, a confirmation that gives the
  profile's anchors weight.

**Lots we never showed — the recall channel.** One box, somewhere stable
(report footer, the customer's page): "Wir haben eine Ausschreibung
übersehen? Nummer oder Link hier." The customer pastes a number or the
tender's URL — the one moment a contractor has either at hand is when the
tender documents are in front of them, and paste beats typing. The
submission is treated as a **question, never as a fact**:

1. We resolve it and answer with the lot's full identity plus our verdict —
   "Los 2, Dachsanierung Grundschule, Stadt Erfurt, Frist 12.09. — ja, Ihr
   Geschäft. Nicht empfohlen, weil wir viele Bieter erwarten." The echo *is*
   the error check: a mistyped number resolves to a lot the customer
   instantly recognises as wrong, an unresolvable one fails loudly, and no
   confirmation is ever asked. It is also the only place a contested verdict
   reaches a customer — on request, which is the reason to show it.
2. **A submitted ref never becomes a `learned_ref` automatically.** Resolves
   and fits the existing profile (trade, plausible region) → learn; the
   worst wrong number that still fits looks like their business anyway.
   Resolves but does not fit → answered normally, ref to the manual review
   queue. Wrong numbers can waste a click; they cannot poison a profile.

Feedback clicks get the same scanner protection as the stop link (§ below): a
click lands on a page with the lot's title and one button, otherwise a mail
scanner "reads" the report and poisons the profile with fake rejections.
Feedback events go to the ledger; how they reweight the profile is the
relevance layer's decision, but the events must be recorded from day one —
the trial weeks are precisely when the profile has the most to learn.

### Stopping: one link, one page, two buttons

Every e-mail carries one "Abbestellen" link → one page (button, not
auto-action — corporate mail scanners follow links, a true one-click URL gets
"clicked" by virus scanners) → two explicit choices:

1. **"Keine wöchentlichen Berichte mehr"** — soft stop. Weekly reports end;
   occasional result updates and offers may still come. This is "you may
   contact me later."
2. **"Keine E-Mails mehr"** — hard stop. Everything ends, forever. This is
   the do-not-contact flag ONBOARDING.md §1.4 already honours eternally, and
   the Art. 21 objection the legal doc requires be effective immediately.

**Every ambiguous signal defaults to the hard stop**: the mail client's
built-in unsubscribe (List-Unsubscribe header), a reply saying stop, anything
unclear. The soft state exists only where the customer explicitly chose it —
the only kind of win-back that works anyway. The button copy defines the
consent scope: option 1's text must say updates *and offers* may come,
because that sentence is the legal basis for every win-back mail sent under
it.

**Cancelling is not unsubscribing.** A paying customer who cancels is ending
a purchase, not objecting to e-mail: billing and weekly reports stop, but
they land in the soft state — results notes continue. A lapsed customer is
the warmest future lead, and the results note arriving with evidence is the
win-back letter. The hard stop is one more click away on the same page, and
a cancel e-mail says so plainly.

### The three contact states — the database must see the difference

**[BUILD]** on the customer record, one field: `contact_state` ∈

| state | weekly reports | results notes / win-back offers | may return via |
| --- | --- | --- | --- |
| `active` | yes (trial or paid) | yes | — |
| `soft_stopped` | no | **yes — further marketing may come** | standing link in any note |
| `hard_stopped` | no | no, nothing, ever | only by contacting us themselves |

Plus timestamps for each transition (state changes are events in the outreach
ledger, §5, so "how many soft-stops came back" is a query, not a study). Every
mailer checks `contact_state` before sending — not the calling code's
discipline, the mailer's own guard, so a future bug cannot mail a
`hard_stopped` customer. The win-back set is exactly `soft_stopped`; it must
be selectable in one query, because it *is* a marketing audience and will be
mailed as one.

## 4. Hosting, and the public/personal rule

**One live surface: the app**, running in Docker next to the database, on its
own subdomain (`app.…`). It serves everything that exists in the current
scope — the QR pages, the e-mail submit, feedback confirms, the stop page,
the recall box, and the minimal root page (product sentence, contact,
Impressum, Datenschutzerklärung). It reads the database per request, so a new
customer, a changed prediction or a fresh profile needs **no deployment
anywhere** — the next request simply sees the new row. Code deploys happen
only when code changes. Consequence: **the Docker box must be reachable from
the internet around the clock** — a small VPS, or a tunnel to wherever the
box lives. **[CLARIFY]** which; it is the one real hosting decision, and it
must be settled before the first QR code is printed, because the app's
domain is baked into every letter.

What is public vs. personal is decided by a rule, not case by case:

- **Public = aggregates only** — content with no customer and no person in
  it. This product has exactly one such asset: the per-trade market
  statistics from `market.py`. Nobody else in Germany publishes them; they
  are the future advertising. Specified in §4.1; *opening* the channel (i.e.
  actually deploying them) remains a decision, but the spec and the
  interface (§4.2) are fixed now so the app is built against them from the
  start.
- **Personal = everything with a firm in it**, and it is not merely
  non-public but **actively unfindable**: tokened URLs, `noindex`, tokens
  long enough that guessing is hopeless. A customer page reachable through
  Google would be decision #8's surveillance nightmare realised.
- **There is no third category.** Anything with names in it — "top winning
  firms", "recent awards near you" — is out; re-affirmed, that page ranks,
  flatters, and republishes personal data for a new purpose (the hard
  version of the unanswered lawyer question).

The administrator has no web surface at all: the console `report` subcommand
and the review queue are the whole back office, on the machine the data
lives on.

### 4.1 The public site, specified

Static HTML on a static host — Vercel is the working assumption, but the
spec is host-agnostic: *any host that accepts finished files*. Chosen for
exactly one reason: **Google reads server-delivered HTML better than
client-assembled JavaScript**, and these pages exist to rank.

- **One page per trade** in [`trades.txt`](../trades.txt), at a stable slug
  (`/gewerke/strassenbau`), German-language: lots per month, median award,
  €/year in scope, the 0/1-bid share, the closed-without-award share — the
  `market.py trade` numbers, plus the month they were computed. Nothing
  else: no firm names, no lot lists, no login, no JavaScript required to
  read the content.
- **Trade × Bundesland pages** (`/gewerke/strassenbau/bayern`) — the actual
  SEO play, decided 2026-08-10: contractors search locally ("Ausschreibungen
  Elektro Bayern"), head terms belong to the incumbents, and the long tail
  is where a new domain with unique data ranks. Same aggregates, one more
  `group by`. **Thin-page guardrail**: a (trade, Land) page is rendered only
  when the cell has real volume (≥10 lots/month proposed); below that the
  Land folds into the national trade page — a few hundred doorway-thin
  pages would get the whole domain demoted.
- **The single-bidder report** — one national flagship page, refreshed
  quarterly: the share of lots closing with 0–1 bids, per trade. The
  citable, newsworthy number only this data produces; it exists to earn
  the backlinks that make everything else rank. It is also the public
  version of the letter's opening argument.
- **„Fast ohne Wettbewerb: diese Woche vergeben"** on each trade page —
  two or three of the week's freshly *published awards* in that trade that
  closed with 0–1 bidders: lot title, buyer (public organs, not persons),
  award value, bid count; **the winner stays unnamed** (the rule). Decided
  2026-08-10 over a plain new-lot count, which googles but is useless to
  the visitor: this shows money that went uncontested in their own trade —
  the letter's argument, made public. Backward-looking: award facts are
  published record. Real titles and cities double as the pages' indexable
  long-tail text.
- **„Kandidaten für wenig Wettbewerb — unsere Wochenauswahl"** (decided
  2026-08-10): a deliberate, small public slice of the *forward-looking*
  forecast — one or two live tenders per week tagged **„voraussichtlich
  wettbewerbsarm"** (title, buyer, deadline). The wording carries the
  honesty: „Kandidat" is by nature unconfirmed, and the tag claims an
  elevated chance, never a likelihood — the 452 backtest is 24% against a
  10% base, so three of four candidates will publicly turn out contested,
  and the copy must survive that. Fixed disclosure line under the list, in
  the replay-result framing the claim rules require: „Einschätzung auf
  Basis von Rücktests gegen bereits veröffentlichte Vergabeergebnisse:
  2,3-fache Trefferquote gegenüber Zufallsauswahl." Guardrails: **CPV 452
  only** (the one trade with measured lift — same rule as the letter;
  elsewhere the section does not render), a teaser-sized count so the
  ranked, reasoned full set stays paid, and the entries stay dated and
  checkable — which is the point: with an elevated-chance claim, the aging
  page becomes the public track record.
- **„Aktuelle Ausschreibungen (Auswahl)"** — three to five *live* lots per
  trade page: title, buyer, region, deadline. No verdicts, no forecast, no
  reasons. Added 2026-08-10 after filtering the inventory by the criterion
  *"can a real customer begin with this?"* — without it, a visitor outside
  CPV 452 finds orientation and proof but no first step. It gives nothing
  away: notices are public on every portal; the product is completeness +
  the personalized gate + the forecast + the learning loop, none of which
  the sample contains.

The same filtering, recorded so the sections keep their honest jobs:
the aggregates are **orientation** ("lohnt sich das?"), the sample and the
452 Kandidaten are the **first step**, the fresh 0/1 awards are **proof**,
and the national report is **PR for backlinks** — the last two are not
customer-start data and must not crowd the first two on the page.

- **An index page** linking the trades, and the same Impressum /
  Datenschutzerklärung the app carries.
- **A sitemap.xml**, rendered with the pages — the pages update weekly,
  and freshness is part of why they rank.
- Every page links to the app for anything personal or interactive; the
  public site itself has **zero forms and zero backend** on the static
  host. When the open-signup form for strangers eventually comes, it lives
  on the app (`app.…/anmelden`) and is merely *linked* from here.

**[BUILD]** (whenever the channel opens — nothing else waits for it): a
renderer beside the report renderer, driven by the cycle; output directory
is gitignored build artifact.

### 4.2 The interface between the static host and the app

The whole interface is two things, and deliberately nothing more:

1. **Hyperlinks at stable URLs.** The static pages know the app only as
   `app.<domain>` plus a handful of paths; the app knows the public site
   only as `www.<domain>`. The URL contract, fixed now:

   | URL | serves | side |
   | --- | --- | --- |
   | `www.<domain>/gewerke/<slug>` | trade page | static |
   | `app.<domain>/t/<token>` | QR / signup page | app |
   | `app.<domain>/f/<token>` | feedback confirm (lot + verdict in token) | app |
   | `app.<domain>/s/<token>` | stop page, two buttons | app |
   | `app.<domain>/c/<token>` | recall box | app |

   Tokens are single-use-purpose, unguessable, and carry their meaning —
   the URL never exposes ids, e-mails or lot numbers in the clear. Either
   side may be redeployed freely; the contract is only that these URLs keep
   working.
2. **A one-way deploy pipeline, build-time only.** The cycle renders the
   public pages from the database and uploads finished files
   (`vercel deploy --prebuilt` or equivalent). **At runtime there is no
   data flow at all**: static pages contain no `fetch()` to the app, no
   embedded API calls, nothing — which also means no CORS surface, and the
   public site stays fully readable when the app is down. Interaction
   happens only when a person clicks a link and lands on the app.
   Upload failures are non-fatal to the cycle (a week-stale public page is
   acceptable; an undelivered weekly report is not) — logged, retried next
   cycle.

What the interface is **not**: the app never proxies the static site, the
static site never embeds app content, and no secret is shared between them
— the static host holds nothing worth stealing.

### 4.3 Authentication: capability tokens, and nothing else

There is no login, no password, no session, no cookie. **The token is the
authentication** — the capability-URL model every magic link, unsubscribe
link and password-reset mail uses. Its properties, which are the actual
security spec (details in [`APP.md`](APP.md)):

- **Purpose-bound**: a feedback token records one verdict on one lot; a
  stop token can only stop; the QR token only shows the signup page. A
  leaked token leaks one small power, never "the account".
- **Unguessable and revocable**: long random values, rows in the database,
  deletable one by one.
- **GET never mutates.** Every state change is a POST from a page with a
  button — one rule that simultaneously defeats mail-scanner clicks and
  link-prefetching everywhere, instead of per-page defenses.
- **Proportionate by data, not by negligence**: everything any token can
  reveal derives from public procurement data; the confidentiality ceiling
  is inherently low (Art. 32 GDPR asks for measures appropriate to the
  risk — this is that, argued, not assumed). Worst realistic incidents are
  integrity nuisances — fake feedback clicks, a stranger starting a firm's
  trial — all visible in the ledger, all reversible in the review queue.
  Hard authentication exists exactly where money moves: on Stripe's side.

The day a single URL would unlock something whose leak genuinely hurts — a
full pick-history dashboard, payment data — is the day a verified-login
upgrade is designed. Not before.

## 5. Seeing conversion

The outreach ledger (ONBOARDING.md §6, **[BUILD]**) records every lifecycle
event with a `source`: letter arrivals attribute via their QR token,
everything else via its own tag when other channels exist. Conversion is a
`group by`; the ask-to-yes gap (how long a customer needs to decide) is a
column to read off after the first batches, not a number to guess now.

A weekly glance needs no dashboard: a `report` subcommand printing
signups / activations / held-for-review / asks-sent / yeses / soft-stops /
hard-stops / trials-ending-this-week, to the console (house rule: tools
print, they do not write report files).

## 6. Payment — last, on purpose

Stripe payment link on its own page (ONBOARDING.md §5.4). Blocked by the
price decision and only by it. Four free weeks mean the first payment moment
is ≥4 weeks after launch; the price must be decided by then, informed by live
conversion against the €60–€99 entrant band vs. €179 anchor
(`MARKET_AND_COMPETITORS.md`).

---

## 7. Order of work

| # | build | unblocks | blocked by |
| --- | --- | --- | --- |
| 1 | the app: QR pages, e-mail submit, root page (Impressum/Datenschutz) | everything customer-facing | — |
| 1a | app hosting: VPS or tunnel, domain | letters can be printed (domain is on them) | the [CLARIFY] in §4 |
| 2 | pre-flight check + automatic activation | safe signups | — |
| 3 | `contact_state` + stop page + guarded mailer | lawful sending at all | — |
| 4 | pick grading + results notes | the post-trial conversion channel | award publications (data, not code) |
| 4a | feedback links + confirm pages + recall box + ledger events | the learning loop at trial time | — |
| 5 | public site renderer + upload step (spec: §4.1–4.2) | the inbound channel | the decision to open it |
| 6 | outreach ledger events + console report | conversion by channel, ask-to-yes gap | — |
| 7 | Stripe page | first paid conversion | price decision, due before first trial ends |

Rows 1–3 are the launch gate: nothing may be sent to anyone before the stop
mechanics exist, because the first e-mail already needs a working Abbestellen
link. Letter-specific work resumes when decision #1's lawyer sign-off
arrives; none of it gates rows 1–6.
