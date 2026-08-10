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
- **Trade market page: public** (decision #7).
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
- their trade's market figures (the §4 page content, inline or linked);
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

A visitor without a valid token sees the public trade pages and a contact
address, not a form.

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

## 4. The trade market pages

One public page per trade: lots per month, median award, €/year in scope, the
0/1-bid share, the closed-without-award share — `market.py trade` output,
re-rendered weekly by the cycle. No personal data, so **not blocked** by the
legal sign-off. It is simultaneously the QR landing target for visitors
without tokens, the only content of its kind on the German market
(`MARKET_AND_COMPETITORS.md` §6), and the proof the product is real.

**[BUILD]**: static pages from `market.py`, one per trade in
[`trades.txt`](../trades.txt). Static keeps hosting at zero.

Re-affirmed: **no page listing firms and their wins** — it would rank and
flatter, and it is republishing personal data for a new purpose (the hard
version of the unanswered lawyer question).

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
| 1 | signup page + Impressum/Datenschutz | everything customer-facing | — |
| 2 | pre-flight check + automatic activation | safe signups | — |
| 3 | `contact_state` + stop page + guarded mailer | lawful sending at all | — |
| 4 | pick grading + results notes | the post-trial conversion channel | award publications (data, not code) |
| 5 | trade market pages (public, static) | QR landing target, later inbound | — |
| 6 | outreach ledger events + console report | conversion by channel, ask-to-yes gap | — |
| 7 | Stripe page | first paid conversion | price decision, due before first trial ends |

Rows 1–3 are the launch gate: nothing may be sent to anyone before the stop
mechanics exist, because the first e-mail already needs a working Abbestellen
link. Letter-specific work resumes when decision #1's lawyer sign-off
arrives; none of it gates rows 1–6.
