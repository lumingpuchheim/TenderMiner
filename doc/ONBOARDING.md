# ONBOARDING — from a name in the awards store to a paying subscription

Design, written 2026-08-10; §8–9 (status and the remaining build, specified) added
2026-08-17. Companion to [`GO_TO_MARKET.md`](GO_TO_MARKET.md)
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

One append-only ledger — **built 2026-08-11 as the `app_event` table**
(`db.py`), not a JSONL file — one row per (firm, event): `company`, `batch`, `trade`, `channel`, `sent_at`, `replied_at`,
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

**Status of the table, 2026-08-17:** #3 decided (four weeks, `LAUNCH.md`);
#7 decided (public site is one page without figures, `LAUNCH.md` §4.1); #8
decided (the QR page shows, never asks, `LAUNCH.md`); #2 moved to "before the
first trial ends" (`LAUNCH.md` §6). #1, #4, #5, #6, #9 open as written. The
pre-flight check and the outreach ledger (`app_event`) exist — see §8.

---

## 8. Where it stands — 2026-08-17

The middle of the funnel is built and live; the two ends are not.

```
target list → invitation → letter + QR → /t/<token> → signup → pre-flight → report e-mail → ask → paid
   built        NOT          NOT           built        built      built       NOT (files)    NOT   NOT
```

**Built and running** (`app.murara.eu`, `/healthz` green; details in
`APP.md` §10b–10c, `HOSTING.md`, `OPERATIONS.md`):

- [`outreach.py`](../outreach.py) → `data/outreach/targets.csv`, 472 firms with
  wins, refs, contacts, `sim_picks`; `market.py pitch <firm>` prints one firm's
  letter numbers.
- [`tokens.py`](../tokens.py): `t`/`f`/`s`/`c` tokens, purpose-bound,
  revocable, rate-limited, never logged in full.
- [`app.py`](../app.py): `GET /t/<token>` (the QR page), `POST /t/<token>`
  (e-mail + `consent_at`, pre-flight against the firm's own wins, activating
  version through `subscriptions.append_version`, confirmation mail), the
  stop page (`contact_state`), feedback confirm, recall box, legal pages,
  `app_event` ledger for every state change and send.
- [`mailer.py`](../mailer.py): the guard around Resend; refuses hard-stopped
  customers inside the module.
- Deploy on push, TLS edge, backups, secrets, hardening.

**Not built**, and the reason it matters:

| gap | consequence today |
| --- | --- |
| nothing creates an invitation (customer row + draft subscription + `t` token); `tokens.mint` has no caller outside `app.py` and the tests | no real firm can reach the QR page |
| no QR image, no letter template | nothing to post |
| the weekly report is written to `data/reports/…/report_<date>.html` and **not sent**; it carries no `f`/`s`/`c` links, no Abbestellen, no criterion line | a signed-up firm gets a confirmation and then nothing |
| the confirmation mail has no footer (no `/s/` link, no `List-Unsubscribe`) | first e-mail already breaks `APP.md` §8 |
| no trial clock, no ask, no yes-link, no results notes, no Stripe page | no path from trial to money |
| no console report over `app_event` | conversion is a hand query |

Non-code blockers, unchanged: lawyer sign-off on the LIA (decision #1);
Resend sending domain verified and `info@murara.eu` receiving; the price
before the first trial ends.

---

## 9. The rest, specified

Sized to one firm from the target list, then each piece named where it goes.
Nothing here reopens a decision in `APP.md` §0 or `LAUNCH.md`.

### 9.1 The concrete case, end to end

*Jens Dunkel Glas- und Bauelemente GmbH*, Burg (39288) — first row of
`targets.csv`: small, 4 wins, one of them with a single bid, trades 454/452,
regions DE7 DEA DEC DED, 4 usable `profile_refs`, `sim_picks` 9.

1. **Invite.** `python invite.py add "Jens Dunkel Glas- und Bauelemente GmbH"`
   reads the row, writes the customer row (`name`, `award_names=[the exact
   winner string]`, `contact_note` = postal address from the XML), appends
   subscription version 1 with `active: false` (the *draft*: `cpv_prefixes`
   from `trades`, `nuts_prefixes` from `regions`, `profile_refs`, the standard
   gate knobs), mints one `t` token and prints
   `https://app.murara.eu/t/<token>` plus the `invited` event. `sub_id` is a
   slug of the name (`jens-dunkel-glas-und-bauelemente-gmbh`).
2. **Letter.** `python invite.py letter <sub_id>` renders one HTML page (the
   §2.2 template) with the QR as an inline SVG, plus a second page holding the
   Art. 14 notice; a person prints it. `python invite.py batch --trade 452
   --n 50` does 1–2 for a batch and writes `letters/<batch>/<sub_id>.html`.
3. **Scan.** The page already built: firm name, market figures, one field.
   Submitting writes `contact_email`, `consent_at`, runs the pre-flight; for
   this firm the gate passes ≥1 of 4 wins → version 2, `active: true`,
   `effective_from` today. The confirmation mail now carries the standard
   footer (9.4).
4. **Monday.** The cycle writes the report as today and then **sends it**: the
   same HTML, every lot with two `f` links, footer with `/s/` and `/c/` links
   and the criterion line, through `mailer.send(kind='report')`. Event `send`.
5. **Week 4.** The Monday cycle after `effective_from + 28 d` sends the last
   trial report with the ask on top: one paragraph and one link,
   `/y/<token>` (9.5). No further reports unless yes.
6. **Yes.** `/y/<token>` is one page, one button; the POST records
   `subscribe_yes`, appends a version with `plan: paid`, and forwards to the
   Stripe payment link. Reports resume next Monday. Silence → results notes
   only, each carrying the same `/y/` link (9.6).
7. **Read-off.** `python invite.py report` prints per batch: invited, scanned
   (`t` used), signed up, held, asks sent, yes, soft/hard stops.

### 9.2 `invite.py` — the front of the funnel [BUILD]

Console tool, prints, writes no report files (house rule). Storage only
through `subscriptions.py`, `tokens.py`, `ledger.py`.

- `add <company> [--sub-id …]` — one firm from `targets.csv`. Refuses if a
  customer with the same `award_names` entry exists (no double invitations)
  or if the row has `< 2 profile_refs`. Prints the URL once; the token is not
  retrievable later except by minting a new one (`reissue`, which revokes the
  old).
- `batch --trade <cpv3> --n 50 [--dry-run]` — the §1.2 ordering, skips firms
  already invited or flagged `do_not_contact`, stamps `batch` (`YYYY-MM-DD-<trade>`)
  on the `invited` event. `--dry-run` prints the list and touches nothing.
- `letter <sub_id>` / part of `batch` — the template of §2.2, in German,
  no forecast claim outside 452 (the not-claimable list from §3 sits in the
  template as a comment). QR: SVG, generated in-process (`segno`, one new
  pure-Python dependency, or a stdlib encoder — the operator's call, default
  `segno`), error level M, the URL and nothing else in it. Page 2: the Art. 14
  notice from `LEGAL_BASIS_TARGET_LIST.md` §"What this project must actually
  do", the Art. 21 objection stated separately, `info@murara.eu` and the
  postal address as objection channels.
- `objection <sub_id|company>` — sets `contact_state = hard_stopped`,
  revokes all tokens, event `objection`. Honoured before the next `batch`
  runs (`batch` refuses to include a hard-stopped firm).
- `report [--batch …]` — the §6 read-off over `app_event`, one line per
  batch, plus trials ending this week.

New `app_event` kinds: `invited`, `objection`, `ask`, `subscribe_yes`. New
customer field: none — `batch` lives on the event, `do_not_contact` is
`contact_state = hard_stopped`.

Alias merging (decision #5) stays manual: `add --also-name "<spelling>"`
appends to `award_names`; nothing merges by similarity.

**Built 2026-08-17: `add`, `reissue`, `objection`** ([`invite.py`](../invite.py),
`tests/test_invite.py`). **Nothing manual, nothing to copy**: `add` does not
read `targets.csv` — `outreach.firm(data_dir, company)` computes the row from
the awards and tenders store, the sidecar index for the contract-notice refs,
and the firm's own award notices (`source_file`) for the contact; two seconds
per firm on the server's `/data`. The row equals the CSV's row for the same
firm (checked: regions, six refs, city). Two things settled in the building: the draft's
knobs copy the live customers (`cpv_prefixes ['45']`, gate at 0.7,
`max_picks 5`) rather than the row's CPV3 codes — buyers enter CPV wrongly and
the gate reads the text, so the market filter stays wide and the gate narrows;
and `add` refuses an exact-spelling miss instead of guessing (the message
names the rows that contain the typed text). Receipt against a copy of the
live database: `add "Jens Dunkel Glas- und Bauelemente GmbH"` → sub_id,
`https://app.murara.eu/t/…`; the URL answers 200 with the signup form; a
second `add` refuses with the owning customer; `invited` event carries the
batch and the 8-character token stub. `batch`, `letter`, `report` are the
next rows.

### 9.2a The invitation message — built 2026-08-17

The channel decision (`GO_TO_MARKET.md`) leaves the sending to a person: we
mint the URL, the operator writes to the firm. What the program contributes
is the part only it can: **the message leads with live tenders picked for
that firm**, and with the firm's own win — the product working, not a
description of it (operator: „just do it and let customer decide if it is
valuable"; no „wir könnten", no service prose).

[`pitch.py`](../pitch.py), reachable as the **Nachricht** button on the
operator's page (`/admin/message?sub_id=…`, doc/ADMIN.md). Two texts:

- **Kontaktanfrage**, ≤300 characters (LinkedIn's note), one concrete tender,
  **no link** — a note with a URL reads as spam and the link is useless
  before the contact is accepted;
- **Nachricht nach dem Kontakt**: up to three open lots with buyer and
  deadline, then „Ihren Auftrag „…" haben Sie gewonnen — bei 1 Bieter. Genau
  solche Lose suchen wir für Sie", then the invitation URL, then one line
  pointing at the Datenschutzerklärung (the Art. 14 pointer travels with the
  approach).

The picks come from the customer machinery: this cycle's scored lots
(`ledger.prediction_latest_per_lot`), the firm's draft subscription written
by `invite.add` from its own contracts, the relevance gate, and
`selection.for_sub`. A prospect therefore sees exactly what it would receive
as a customer; when nothing matches, the message opens with the win instead
and invents nothing.

`tokens.live_value` hands the operator's page the firm's current invitation
link (behind basic auth, and a link is useless to anyone but the firm it
names) — an invitation written days after minting still needs it.

*Found by running it:* the first message said „Frist None". Lots without a
deadline passed the filter because `str(NaN)` is `'nan'` and `'nan' >
'2026-08-17'` is true in a string comparison. A lot we cannot date is now
never offered.

### 9.3 The report goes out by e-mail [BUILD]

In [`delivering.py`](../delivering.py), after the report file is written and
the delivery rows appended: `mailer.send(home, 'report', sub_id, subject,
html)`. Rules:

- **Links.** [`render.py`](../render.py) gets the app base URL
  (`TM_APP_URL`, default `https://app.murara.eu`) and mints per lot two `f`
  tokens (`ist unser Geschäft` / `nicht unser Geschäft`) — for picks and
  near-misses alike — plus the standing `s` and `c` tokens for the footer.
  The file on disk and the mail are the same HTML; tokens in
  `data/reports/` are on the private volume and that is acceptable.
- **Footer** (`APP.md` §8): Abbestellen → `/s/<token>`; „Ausschreibung
  übersehen?" → `/c/<token>`; the Art. 21 line, visually separate;
  `List-Unsubscribe` header pointing at `/s/<token>` (the app maps a
  header-driven visit to **hard**, `LAUNCH.md` §3). Criterion line per pick.
- **Kind and state.** `report` needs `active` — the mailer already refuses
  otherwise; a refusal is logged by the cycle as one line, never a failed
  cycle. `send`/`send_refused` events as today.
- **The cycle container needs the secret.** `RESEND_API_KEY` and
  `TM_MAIL_FROM` reach `loop.py`'s environment the way `SECRETS.md` already
  describes; nothing new to design, one line in the compose file.
- **No e-mail when there is nothing to report** — same rule as no file.
- The confirmation mail in `app.py` uses the same footer function.
  `mailer.send` grows a `headers` argument for `List-Unsubscribe`.

### 9.4 One footer for every mail [BUILD]

`mailer.footer(home, sub_id)` — returns the HTML block above and the header
value; both `app.py` and `render.py` call it, so no mail can be assembled
without it. Tested: a rendered report contains exactly one `/s/` link, its
token resolves as `s` for that customer.

**Built 2026-08-17 (9.3 + 9.4)** — `mailer.footer` (+ `headers` on
`mailer.send`, `send_failed` ledgered when the transport fails), `render.py`
(two `f` links per pick, the „Zuschlag" criterion column, the footer, brand
„Murara-Bericht"), `delivering.py` (`mail_links` mints only when an address
is on record; `send_report` never raises; the criterion joined from the tender
store), `app.py` (confirmation mail carries the footer; a `List-Unsubscribe`
one-click POST is the hard stop; the feedback page names the lot by title),
compose + `weekly.sh` (the mail secrets and `TM_APP_URL` reach app AND cycle —
they reached neither before). `loop.py run --no-mail`, `preview_report.py` and
`rewind_report.py` mail nobody. Receipt on a copy of the live state, customer
`beck` given an address: 3 picks → 6 `f` links, 1 `s` link, criterion
„100 % Preis / Preis 60 / Qualität 40 / 100 % Preis", footer present, no
„TenderMining"; with no key: `send_failed: RESEND_API_KEY is not set` in the
ledger and one printed line, the file written regardless. No live customer
has an address today, so the next Monday sends nothing until one signs up.
Not in this row: near-misses in the report (the report shows picks; the
annex stays the operator's).

### 9.5 The trial clock, the ask, the yes-link [BUILD]

**Recounted 2026-08-20 (operator): the trial is `FREE_REPORTS` (4) MAILS
WITH RECOMMENDATIONS, not 28 days.** Sending became event-driven the same
day — a report without picks is written for the operator and never mailed —
so a four-week clock could mean four mails or none. Now:
`trial_status(rows, as_of, sent_reports)` takes the count of `send` events
with detail `report:` (delivering.trial_state reads the ledger; since the
no-picks-no-mail rule, every such mail carried a recommendation);
`ask_due` from `sent >= FREE_REPORTS - 1`, so the ask rides ON the fourth
free mail ("Das ist Ihre vierte kostenlose Empfehlung"); after the ask,
`trial` customers get the file only, as before. `TRIAL_DAYS` is retired in
place. The /admin row shows „Empfehlung N von 4 kostenlos". The paragraphs
below describe the original 28-day build; the mechanics they name (ask
once, `y` token, paid versions) are unchanged.

- **No new date field.** Trial start = `effective_from` of the first
  `active: true` version; the trial ends after 28 days. `plan` joins `KNOWN`
  (`trial` | `paid`; absent reads `trial`) — in the same commit that first
  writes it.
- **The ask** goes on top of the last trial report (the first Monday on or
  after day 28), once: what they got, one question, one link. Event `ask`.
  After that Monday, `report` is not sent to a `trial` customer (the mailer
  guard is state-based; this rule is the cycle's, in `delivering.py`, and it
  is a filter — the report file is still written for the operator).
- **`y` token, `/y/<token>`** — fifth purpose, standing per customer, in
  `tokens.PURPOSES` and `app.py`. GET: the price and one button. POST:
  `subscribe_yes` event, new version `plan: paid`, redirect to the Stripe
  payment link (`TM_STRIPE_URL`; until it is set the page says
  „wir melden uns" and the operator gets a mail). GET never mutates.
- Reports resume the next Monday for `plan: paid`. Cancel = the stop page's
  existing soft button plus a `plan: trial`-less deactivating version, by
  hand, under 50 customers.

**Built 2026-08-17 (9.5)** — `plan` in `subscriptions.KNOWN` (`trial` |
`paid`; absent = trial), `subscriptions.trial_status(rows, as_of)` (started =
first active version's `effective_from`, 28 days, `ask_due`), token purpose
`y` (standing), `render.ask_html`, `delivering.trial_state / ask_for` (the ask
rides on top of the first report on or after day 28 that has content, event
`ask` only when the mail actually went; after it, `trial` customers get the
file only), `app.py /y/<token>` (GET: firm, „monatlich beendbar", price from
`TM_PRICE_LINE` or „keine Zahlungspflicht"; POST: `subscribe_yes`, new version
`plan: paid`, then the `TM_STRIPE_URL` button or „wir melden uns" + an
operator mail to `info@murara.eu`; idempotent). Receipt on a copy of the live
state (`TRIAL_DAYS` forced to 7 in-process so `beck` was due): report mailed
with the ask block and a `/y/` link, `ask` event written; the next run:
„trial ended, ask sent … file only", nothing mailed. The pre-existing demo
customers are all `trial` by this definition and past day 28 — harmless
today (no address, nothing mailed); give them `plan: paid` versions if they
should ever receive mail.

### 9.6 Results notes [BUILD, blocked by data]

Sent by the cycle when ≥3 of a customer's own trial picks have graded
outcomes (`grades` ledger) and no note went out in the last 30 days: the
lots, what they closed at, how many bids; the `/y/` link; kind `results`
(allowed for `active` and `soft_stopped`). Awards lag ~3 months, so the first
one is ~November for an August trial. Event `results`.

### 9.7 Order of work

| # | build | unblocks | size |
| --- | --- | --- | --- |
| 1 | `invite.py add`, `reissue`, `objection`; the `invited` event | a real firm on the live QR page | small |
| 2 | footer + report by e-mail (9.3–9.4), compose env line | the launch gate: nothing may be sent before Abbestellen works | medium |
| 3 | ~~`invite.py letter`, `batch`, QR~~ **void — no letters (operator, 2026-08-17; GO_TO_MARKET.md „Channel decision, revised")** | — | — |
| 4 | trial clock, ask, `/y/` (9.5) | trial → paid | medium |
| 5 | `invite.py report` | reading conversion | small |
| 6 | Stripe URL | first payment; waits on the price | tiny |
| 7 | results notes (9.6) | win-back; waits on award publications | small |

Row 1 first because it makes the built middle testable end to end on the
server today, with a real letter-shaped URL and no letters. Rows 1–2 are done
in one worktree each; row 3 is the only one that waits on a person outside
the repository.
