# SALES — when to write to a firm, and who is told

Specified 2026-08-18 with the operator, after reading the first contact
messages as a recipient. Companions: [`ADMIN.md`](ADMIN.md) (the operator
page this extends), [`ONBOARDING.md`](ONBOARDING.md) §9 (the funnel),
`pitch.py` (the two texts), `delivering.py` (the customer's mail, which this
document does **not** touch).

## 0. Decisions

| decision | consequence |
| --- | --- |
| **A firm is written to only when we have a concrete tender for it today** — an open lot in its main trade that the forecast flags as low-contest, with enough deadline left to survive a slow reply | the first contact is always followed by a real recommendation; no "random list" notes |
| **The program tells the salesman; the salesman writes** | nothing is ever sent to a prospect by the program — one e-mail to the salesman says *whom* to write to; the texts are ready on the firm's page; the salesman pastes them into LinkedIn/Xing |
| **Two e-mails, two audiences, never confused** | (1) to a subscribed customer: the weekly report, sent only when there is something to say — exists, unchanged; (2) to the salesman: "Heute schreiben", the list of firms that are due — new |
| **Which firms interest us is told, not guessed**: the salesman marks firms (**Vormerken**) from the search list | the program watches only those; no rules language; a second salesperson is a second watch list |
| **Each vormerkt firm has an owner** (an e-mail) | the "Heute schreiben" mail goes to the owner; staff split by trade or by buyer falls out of whose list a firm is on |
| **Small firms only** (operator): micro + small by the award register's size | the list and the mail skip medium/large; the size is on every line so the cut is visible |
| **Threshold: one** flagged lot in the main trade, ≥ 10 days to deadline (operator: "start with one") | more firms eligible per week; the count is shown, raise by eye |
| **The note names the firm's main trade and one lot from it** — never a side root | the Blitzschutz-to-an-Elektro-firm note was the reason this document exists |

## 1. The concrete case

Monday. The cycle has written this week's predictions.

1. The salesman (the operator, `luming.sjtu@gmail.com`) has, over the last
   weeks, searched `/admin` for `elektro`, `lueftung`, `heizung` and pressed
   **Vormerken** on twelve small firms. Each row became *vorgemerkt ·
   Inhaber: luming*.
2. 08:30, after the cycle: one e-mail, subject **„Heute schreiben: 3
   Firmen"**. Body, one line each:
   `Elektro Beckhoff GmbH · klein · Elektroinstallation (Vorsprung 1,2-fach)
   · 2 Lose mit wenigen Bietern erwartet, nächste Frist 07.09. → Nachricht`
   where *Nachricht* links to `/admin/message?sub_id=elektro-beckhoff-gmbh`.
   Nine vormerkt firms are not in the mail: nothing flagged in their trade
   this week.
3. He clicks the first. The page opens with the verdict box, then the
   **Kontaktanfrage** (300 chars): *„Guten Tag, für Elektroinstallation
   sehen wir gerade 2 öffentliche Ausschreibungen, bei denen wir nur ein
   bis zwei Bieter erwarten – z. B. Elektroinstallation Neubau
   Gewerbeschulstraße 109, Stadt Wuppertal, Frist 07.09. Dürfen wir Ihnen
   die beiden schicken? Kostenlos, ohne Konto."* He pastes it into the
   LinkedIn connection request, presses **Als verschickt markieren**. Row:
   *angeschrieben · linkedin · 18.08.*
4. Wednesday the contact is accepted. He opens the same page: the
   **Nachricht nach dem Kontakt** — who we are, the trade's figures, the
   record, **the two lots the note promised** with reasons and TED links,
   the invitation link, signature. Pastes it. The firm signs up through
   the link or not; from here on ONBOARDING §9 applies, and any mail the
   firm receives is e-mail type 1.
5. Variation: the contact is accepted the following Tuesday, the two lots
   have passed their deadline, and this week's cycle flagged nothing in
   Elektroinstallation. The page says **„Nichts Passendes offen — noch
   nicht schicken"** above an empty list; the firm stays *angeschrieben*
   and reappears in a "Heute schreiben" mail the week its trade has a
   flagged lot again. The salesman never sends a list that does not back
   the note.
6. Variation: no reaction within three days. He does nothing; the row shows
   the age. LinkedIn will not take a second connection note for ~3 weeks
   anyway, so the firm is not listed as due again for **21 days**; after
   that, a flagged lot in its trade puts it back into the mail.

## 2. The two e-mails

| | to whom | when | content | built by |
| --- | --- | --- | --- | --- |
| **customer report** | a subscribed customer | Monday 08:30, only when there is something to report (`delivering.py`: „nothing to report — not sent") | the picks of the week, the record | exists — **not changed by this document** |
| **Heute schreiben** | the owner of vormerkt firms | Monday, right after the cycle, **only when at least one firm is due** | the due firms, one line each, linking to their message page | new: `sales.py`, called at the end of `cycle.py` |

Nothing else mails anybody. A prospect is reached only by the salesman's
own hand, on LinkedIn/Xing.

## 3. The watch list — Vormerken, owner, size

**Vormerken** is a row action on `/admin` for a firm with status *nicht
eingeladen* and size *micro* or *small*: it does what `Einladen` does —
`invite.add(mint=False)` writes the customer row and the draft subscription
from the firm's wins — **without minting a link**. The link is minted by
*Neuen Link erzeugen* on the message page, which is one press before the
first paste: a GET may not mutate (ADMIN.md 0), so opening the page cannot
mint it. New status word:
**vorgemerkt · Inhaber: \<owner\>**. Rows of medium/large firms offer no
Vormerken (grey hint „nicht klein"); `Einladen` stays for the odd exception.

**Owner**: a new column on `customer` — `owner`, an e-mail — filed from
`TM_SALES_OWNER` (one address) or `TM_SALES_OWNERS` (`user=mail,…`, the
edge's basic-auth users; one today). The address is configuration, not
secret, and **not in `.env`**: it lives in `/etc/murara/env.d/site.env` and
reaches the containers as the first env.d layer (SECRETS.md 2a), set with
`bash docker/secrets.sh push site.env`. Shown on the row. Everything the
mail does is keyed by it.

### 3a. Several salespeople — built 2026-08-18

Verified before building: with two addresses in `TM_SALES_OWNERS` and no
user name reaching the app, every Vormerken filed the firm unwatched —
`default_owner` refuses to guess between two. What makes a second list
work:

- **The edge says who.** `docker/Caddyfile` forwards basic auth's user id
  as `X-Murara-User` on `/admin*` (and strips it on every other path, like
  `X-Murara-Admin`). Each salesperson is one `admin*.caddy` credential
  (`docker/admin-password.sh`) and one `user=mail` entry in
  `TM_SALES_OWNERS` (`site.env`, SECRETS.md 2a).
- **Vormerken files under the presser** — `sales.owner_for(user)`. With
  several owners configured and a user the map does not know (a mis-wired
  edge, a credential without an entry) the press is **refused** with the
  user name shown, never filed on somebody's list. With one owner nothing
  has to be attributed; that path is unchanged.
- **Reassign on the firm's page** (`/admin/email`): an *Inhaber* select of
  the configured addresses, shown only when there is more than one; POST
  `/admin/owner`, event `owner_set`. Free-text addresses are refused — the
  env is the one list of who exists.
- **„Heute schreiben" on `/admin` is the viewer's list** („Liste von
  \<address\>"), with „n weitere bei anderen anzeigen" → `?alle=1`. The
  mail was per owner from the start.

Not built: a per-person size or trade preference, and any rule that files
a firm automatically — the list is still made by hand, one press per firm.

**Size**: the register's `winner_size` as the index already stores it per
firm (`admin_index.json` → `size`). `SMALL = {'micro', 'small'}` is a
constant in `sales.py`; a per-owner preference is not built until a second
owner needs one.

## 4. Due — the trigger

After the cycle's predictions are written, for every customer row with
`owner` set and no `consent_at` (a prospect, not a customer):

1. **main trade** = the firm's strongest trade page
   (`admin_index.json` → `trades[0]`, `trade_pages.trades_of_titles`);
   its root set = the words of that trade (trades.txt).
2. **main trade, without a page.** The trade comes from the index, not
   from the site build: a trade too small for a page (or a checkout with no
   build yet) still decides which lots may be offered and what the note
   calls the reader's trade. Only the market FIGURES and the edge wait for
   a build.
3. **candidate lots** = this week's flagged predictions
   (`ledger.prediction_latest_per_lot`, `flag`), deadline ≥ today + 10
   days, title matching the main trade's words (same match as the page),
   gated by the firm's draft profile exactly as `pitch.picks_for` does —
   so the lots the mail counts are the lots the message will show.
4. **due** ⇔ candidates ≥ 1 **and** the firm was not written to in the
   last 21 days (no `invite_sent` event newer than that) **and** the firm
   is not stopped.
5. The count, the nearest deadline and the main trade's edge verdict
   (`trade_pages.forecasts`) go into the mail line and onto the row
   (*„2 Lose, Frist 07.09."*, green).

`sales.due(data_dir, today)` → `[{sub_id, company, size, trade, edge,
n_lots, next_deadline, owner}]`, computed, never stored; the mail is the
record that it was sent (`app_events`: `sales_mail`, detail = the sub_ids).

## 5. The salesman's mail

One mail per owner per cycle, only when their list is non-empty. Through
`mailer.send` (Resend), sender as the customer mails, subject **„Heute
schreiben: n Firmen"**, body: the lines of §1 step 2, each a link to
`/admin/message?sub_id=…`; a footer line with the count of vormerkt firms
that were *not* due this week. Plain, short; it is a pointer. The same list
sits at the top of `/admin` under **Heute schreiben**, so the mail is one
way in, not the only one.

## 6. The two messages, from the trigger

`pitch.message` gains the main trade and the candidate lots of §4 and uses
them instead of the firm's top-scored picks:

- **Kontaktanfrage** — a teaser, reworded 2026-08-18 after the first
  version named title and buyer and *then* asked permission to send them
  (operator: "nonsense — he can google it, there is no reason to accept"),
  and again 2026-08-20 (operator: "too many sudden facts, too few
  emotion"). Variant B — the pain first, then the one lot almost nobody
  will bid on:
  *„Guten Tag, die meisten Angebote auf Ausschreibungen sind umsonst
  kalkuliert – zu viele Bieter. Wir suchen die Lose, bei denen fast
  niemand bietet. In \<Land\> ist gerade so eines offen, Frist \<dd.mm.\>,
  passend zu Ihren bisherigen Aufträgen. Nehmen Sie die Anfrage an,
  schicken wir die Bekanntmachung."*
  No trade name — the match clause carries the relevance; the Land is the
  lot's NUTS (his region — the draft's `nuts_prefixes` are where he has
  won). No title, no buyer, no link. **No terms, no price, no
  „kostenlos"** — the note is about one tender and one promise; on
  LinkedIn accepting *is* the answer, so that is the ask. One lot is
  promise enough; further ones are the surprise in the message. ≤ 300
  characters, never a cut word: the match clause shortens, then the Land
  becomes „Ihrer Region", then the clause goes. Variant A (recognition
  first) sits in `pitch.note`'s docstring for a future A/B test. With
  zero candidates the page shows **no note** — there is nothing to
  promise.
- **The terms stand in the message after contact, complete and in one
  breath**, where the decision is made: *„Vier Wochen bekommen Sie sie ohne
  Kosten; danach kostet sie \<TM_PRICE_LINE\>, kündbar jederzeit mit einem
  Klick. Es gibt kein Konto und kein Passwort – nur Ihre E-Mail-Adresse."*
  A free period with its end and its price visible is a trial; without them
  it is bait. Until the price is decided the sentence says „gegen eine
  monatliche Gebühr, die wir Ihnen vorher nennen" — true, and the reason to
  decide it.
- **Nachricht nach dem Kontakt**: as today (who we are, the trade's
  figures, the record, the lots with reasons and TED links, the invitation
  link, signature) — with the lots being the candidates, the ones the note
  promised. With zero candidates: the **„Nichts Passendes offen — noch
  nicht schicken"** box and no list; the invitation paragraph and the
  signature stay so the text is still complete if the salesman decides
  otherwise.

## 7. Routes, storage, status

| route | GET | POST |
| --- | --- | --- |
| `/admin` | + **Heute schreiben** section (the due list of §4 for this owner); rows carry *vorgemerkt*, owner, due count | — |
| `/admin/vormerken` | — | `company` → `invite.add(..., mint=False)`, `owner` from the auth user; row → *vorgemerkt* |
| `/admin/message` | as today + the postpone box; note only when candidates ≥ 1 | — |

Storage: `customer.owner` (`subscriptions.CUSTOMER_FIELDS` += `owner`, same
commit); `invite.add(mint=False)`; event kinds `vormerkt`, `sales_mail`.
Nothing new is written to the ledgers' files; `db.py` migration adds the
column.

Status vocabulary (ADMIN.md §3) gains **vorgemerkt · Inhaber: x** (customer
row, no link, no consent) — between *nicht eingeladen* and *Link erzeugt*;
the counts line gains *vorgemerkt* and *heute schreiben*.

## 8. Not built, on purpose

- No automatic sending to prospects, on any channel.
- No rule language for watch lists ("every small firm in Gleisbau"): the
  salesman vormerkt by hand; a *Vormerken alle* button for a search result
  can come when the clicking becomes the bottleneck.
- No per-owner size or trade preferences: one constant, one owner.
- No change to the customer report mail.
- No mid-week runs: the trigger is the Monday cycle. If a week is too
  coarse, the cycle can be run mid-week (`cycle.py run`, no delivery) and
  the due-check runs with it.

## 9. Build order and receipts

Steps 1–4 are built (2026-08-18); step 5 is wired into `cycle.py` after the
admin index, non-fatal like the index itself. The texts of §6 were read on
the fixture end to end: a two-lot note of 266 characters naming
„Blitzschutz und Erdung", and the message delivering those same two lots,
nearest deadline first.


1. `customer.owner` + `CUSTOMER_FIELDS`; `invite.add(mint=False)`; status
   *vorgemerkt*; `/admin/vormerken` with the size guard. Tests: a micro
   firm can be vormerkt, a large one cannot; the row word; the owner.
2. `sales.due` over the test store: a vormerkt firm with a flagged lot in
   its main trade and deadline +12 d is due; the same lot at +5 d is not;
   a firm written to 10 days ago is not; a stopped firm is not.
3. `sales.mail`: one mail per owner, none when empty; the event row; the
   "Heute schreiben" section on `/admin`.
4. `pitch.message` on candidates; the note's main-trade example; the
   postpone box. Tests: note absent with zero candidates; the example's
   trade is the main trade; ≤ 300, no cut word.
5. `cycle.py` calls `sales.run` after `admin.build_index` (needs the index
   and the predictions); `docker/cycle.sh` unchanged.
6. Receipt on the server: vormerk one small firm by hand, run
   `python sales.py --dry-run` in the app container, read the line it would
   mail.
