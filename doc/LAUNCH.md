# LAUNCH — what to build now, independent of channel

Written 2026-08-10, from a working session on [`ONBOARDING.md`](ONBOARDING.md).
That document designs the letter funnel; this one extracts the part that has to
exist **whichever way a customer arrives** — letter, Google, trade press, or a
phone call. The letter-specific builds (mail-merge, `trade` column, alias
merging, the print-API integration) are deliberately *not* on this list; they
wait until the channel question is settled and the legal sign-off
([`LEGAL_BASIS_TARGET_LIST.md`](LEGAL_BASIS_TARGET_LIST.md), ONBOARDING.md §7
decision #1) is in.

Decisions settled in that session, recorded here so the open-decisions table in
`ONBOARDING.md` §7 can be read against it:

- **Free period: four weeks** (decision #3, the doc's own recommendation).
- **Trade market page: public** (decision #7). It only works as an inbound
  channel if Google can crawl it.
- **Signup writes the subscription automatically** — with the safeguards in
  §2 below, which is what makes that safe.
- **No TED-number field on the signup form.** Asking a customer for a
  publication number is technically correct and practically a signup killer:
  they don't know it, mistype it, and give up. Finding a firm's wins in the
  awards store is our job. The form asks for what the customer knows without
  looking anything up.
- **Price (decision #2): still open — and no longer blocking.** With four free
  weeks, no price is printed on the signup page; the ask is "four weeks free,
  cancel anytime". The real deadline moves to **before the first trial ends**,
  which buys four weeks of live conversion data to decide with.

---

## 1. The signup page

Two fields and a dropdown; no account, no password, no login (per
`MARKET_AND_COMPETITORS.md` §4.4):

- **firm name** — free text, what the customer calls themselves;
- **e-mail** — where the weekly report goes; submitting the form is the
  consent record (`consent_at`, ONBOARDING.md §5.2);
- **source** — "Wie haben Sie von uns erfahren?" (letter / search /
  recommendation / press / other). Pre-filled and hidden when the visitor
  arrives via a tagged link.

Arrivals from a letter's QR code carry the firm identity in the link — the
target list is in the database, so for them even the firm-name field is
pre-filled and the form is a confirmation, not a task.

**[BUILD]** the page, plus Impressum and Datenschutzerklärung (legally
required for any German commercial site; the Datenschutzerklärung also carries
the long-form Art. 14 information the legal doc calls for). No cookie banner:
analytics stay cookieless, and payment lives on a separate Stripe page
precisely so its consent machinery never touches ours (ONBOARDING.md §5.4).

## 2. From form submission to running subscription

The automatic path, in order:

1. **Look the firm up in the awards store by name** — fuzzy, against
   `winner_names`, same alias caution as `outreach.py` (similar names are
   *candidates*, never silently merged).
2. **Found, with usable `profile_refs`** → build the profile from their wins,
   run the **gate pre-flight check** (ONBOARDING.md §5.3 **[BUILD]**): replay
   the firm's own won lots against the proposed gate. In the 2026-08-10
   backtest 32 of 46 pilot firms had wins rejected by their own gate, two
   firms lost *every* win. A firm that fails pre-flight is **not activated**;
   it is queued for a person to fix the profile first. Activating a
   subscription whose gate rejects the customer's own business converts
   acquisition money into guaranteed one-month churn.
3. **Not found, or found without usable refs** (new firm, subcontractor going
   direct, name mismatch) → the subscription row is created but **held for
   manual review**. Under 50 customers this queue is minutes per week, and it
   is also where alias problems surface before they become wrong reports.
4. Either way, the customer gets a confirmation e-mail saying what happens
   next; the activated ones get the §5.1 moment — "we found these contracts
   you won, confirm they are yours" — as the first weekly report's opening.

All writes go through `subscriptions.py` (project rule); activation is a
subscription version with `effective_from`, review-queue state is **[CLARIFY]**
a field vs. a separate small queue — proposal: a `status` note on the customer
row, since the queue dies at ~50 customers anyway.

## 3. The trade market pages

One public page per trade: lots per month, median award, €/year in scope, the
0/1-bid share, the closed-without-award share — `market.py trade` output,
re-rendered weekly by the cycle. No personal data on the page, so **not
blocked** by the legal sign-off. It is simultaneously:

- the landing page every other channel points at (QR code, article by-line,
  search result);
- the only content of its kind on the German market
  (`MARKET_AND_COMPETITORS.md` §6), which is what makes it rank;
- the proof the product is real, one click before the signup form.

**[BUILD]**: static pages from `market.py`, one per trade in
[`trades.txt`](../trades.txt), plus the signup form. Static output keeps
hosting at zero and nothing to patch.

What **not** to build, re-affirmed: no page listing firms and their wins. It
would rank and flatter, and it is republishing personal data for a new purpose
— the hard version of the question the lawyer has not answered yet
(ONBOARDING.md §7 decision #8's risk, squared).

## 4. Seeing conversion

The outreach ledger (ONBOARDING.md §6, **[BUILD]**) grows one field: `source`.
Every signup writes a row whether or not we ever mailed the firm — letter
arrivals attribute via their QR link, everyone else via the form's source
field. Conversion is then a `group by source`, and the funnel questions in §6's
table gain one column ("which *channel* converts") at the cost of one field.

Known leak, accepted: a letter recipient who types the domain instead of
scanning looks organic. Under 50 customers the names are recognisable; the
follow-up call catches the rest.

A weekly glance needs no dashboard: a `report` subcommand printing
signups / activations / held-for-review / trials-ending-this-week by source,
to the console (house rule: tools print, they do not write report files).

## 5. Payment — last, on purpose

Stripe payment link on its own page (ONBOARDING.md §5.4). Blocked by the price
decision, and *only* by it; nothing else on this list depends on it. The
four free weeks mean the first customer's payment moment is ≥4 weeks after
launch — the price must be decided by then, informed by which sources convert
and against the €60–€99 entrant band vs. €179 anchor analysis in
`MARKET_AND_COMPETITORS.md`.

---

## Order of work

| # | build | unblocks | blocked by |
| --- | --- | --- | --- |
| 1 | signup page + Impressum/Datenschutz | everything customer-facing | — |
| 2 | awards-store lookup + pre-flight check + review queue | safe automatic activation | — |
| 3 | trade market pages (public, static) | inbound channel, QR landing target | — |
| 4 | outreach ledger with `source` + console report | conversion by channel | — |
| 5 | Stripe page | first paid conversion | price decision, due before first trial ends |

Letter-specific work (mail-merge, `trade` column, alias merge pass,
print-API integration, the letters themselves) resumes when decision #1's
lawyer sign-off arrives — none of it gates rows 1–4.
