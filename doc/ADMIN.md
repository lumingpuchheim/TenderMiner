# ADMIN — the operator's page: find a firm, invite it, enter its e-mail, stop it

Specified 2026-08-17 on the operator's request. This reopens one line of
[`APP.md`](APP.md) §0 („no admin UI"): the console is not how the operator
wants to work, so the back office gets one page in the same app, behind
basic authentication at the TLS edge. Everything the page does already
exists as a function — [`invite.py`](../invite.py), the signup handler's
pre-flight, the stop handler — the page is the interface over them, not new
mechanics. Companions: [`ONBOARDING.md`](ONBOARDING.md) §9 (the funnel the
page drives), [`GO_TO_MARKET.md`](GO_TO_MARKET.md) „Channel decision, revised"
(the URL travels by LinkedIn/Xing message; this page hands it out).

## 0. Decisions

| decision | consequence |
| --- | --- |
| **One page**, `/admin`, server-rendered, no JavaScript needed | same style, same `page()`, same rules as the customer pages (GET never mutates; every action is a POST from a button) |
| **Basic auth at the edge** (operator, 2026-08-17) | Caddy asks for user + password on `/admin*`; the app itself stays login-free; the app additionally refuses `/admin` unless the request carries the header Caddy sets after auth, so a mis-configured edge cannot expose it |
| **No subscription management** (operator) | no profile, knob or version editing on the page; the profile is built automatically from the firm's won lots, as `invite.py add` does |
| **The page writes only through the modules** | `invite.py`, `subscriptions.py`, `tokens.py`, `ledger.py`; nothing new touches storage |
| Search by **trade word or exact name**, over the awards store | „blitzschutz" → every winner whose won-lot titles carry the word; „Jebsen GmbH" → that firm. Trades are title words, never CPV (house rule) |

## 1. The concrete case

The operator opens `https://app.murara.eu/admin`, the browser asks for the
password once.

1. Types **blitzschutz** in the one search field. The list shows every firm
   in the awards store whose won lots carry that word in the title —
   exact winner spelling, city, wins, single-bid wins, last win, and the
   **status** column (§3). Jebsen GmbH is *Kunde · aktiv · Tag 11 von 28*;
   twelve others are *nicht eingeladen*.
2. Types **Jens Dunkel Glas- und Bauelemente GmbH** — the list shows that
   one firm: *eingeladen · linkedin · 17.08.* with a button **URL zeigen**.
3. On a *nicht eingeladen* row presses **Einladen** (channel select:
   linkedin / linkedin-ads / xing / phone / other). The row becomes
   *eingeladen*, and the invitation URL is shown once in a copy box —
   `https://app.murara.eu/t/…` — to paste into the LinkedIn message.
4. A firm answered by phone with an address. On its row presses **E-Mail
   eintragen**: a small form — e-mail, and a required **Einwilligung** text
   („Telefonat 17.08., Herr Dunkel, möchte die Berichte"). Submit writes the
   address and the consent, runs the pre-flight, activates (or holds), and
   the row becomes *Kunde · aktiv · Tag 1 von 28* (or *zurückgestellt*).
   The trial clock starts. Confirmation mail goes out as after a self-signup.
5. A customer phones: no more mails. On its row presses **Abmelden** → one
   confirmation page with the two buttons the customer page has („keine
   Berichte mehr" / „keine E-Mails mehr"); the operator picks the one the
   customer said. Row becomes *gestoppt (Berichte)* or *gestoppt (alles)*.
6. Above the list, one line of counts: eingeladen / angemeldet /
   zurückgestellt / gefragt / ja / gestoppt — the read-off of ONBOARDING §6.

## 2. Routes

| route | GET | POST |
| --- | --- | --- |
| `/admin` | search field, counts line, the list for `?q=` (empty `q` = customers only) | — |
| `/admin/invite` | — | `company`, `channel` → `invite.add`; re-renders the list with the URL box on that row (URL shown once) |
| `/admin/reissue` | — | `sub_id` → `invite.reissue`; URL box |
| `/admin/email` | form for `sub_id` (`?sub_id=`): e-mail, Einwilligung | writes `contact_email`, `consent_at`, `contact_note` += „Einwilligung: …"; runs the same pre-flight/activation as `POST /t/` (`app._preflight`, moved to a shared function); event `signup` / `signup_held` with `detail=admin: <consent text>`; confirmation mail |
| `/admin/stop` | confirmation page for `sub_id`, two buttons | `wahl` = berichte / alles → the same writes as `post_stop` (soft/hard, tokens revoked on hard); event `stop_soft` / `stop_hard` with `detail=admin` |

Errors from the modules (`InviteError`, `SubscriptionError`) render as one
red line above the list, never a stack trace. All routes `noindex`, CSP as
the rest of the app; `/admin` is excluded from the token rate limit and
included in the access log like every path.

## 3. Status vocabulary (one word per row, computed, never stored)

| status | derived from |
| --- | --- |
| nicht eingeladen | no customer row |
| eingeladen · `<channel>` · `<date>` | customer row, no `consent_at`, a live `t` token; from the `invited` event |
| angemeldet · Tag n von 28 / zurückgestellt | `consent_at` set; active version → `subscriptions.trial_status`; `signup_held` and no active version → zurückgestellt |
| Kunde · bezahlt | `plan: paid` |
| gefragt | `ask` event, still trial |
| gestoppt (Berichte) / gestoppt (alles) | `contact_state` soft / hard |
| Widerspruch | hard-stopped by `objection` |

The e-mail is shown masked (`m…@firma.de`), full on the row's own edit
form only.

## 4. Search

`outreach.winner_rows` (awards store) joined to won-lot titles from the
tender store; cached per process and refreshed when the parquet's mtime
moves (the same cache pattern as the recall box). Match: case-insensitive
substring on the winner name **or** on any won-lot title. Result capped at
100 rows with a „mehr eingrenzen" note; sorted by status (customers first),
then wins.

## 5. Protection

Caddyfile, app block:

```
@admin path /admin /admin/*
basic_auth @admin {
    {$TM_ADMIN_USER} {$TM_ADMIN_HASH}
}
reverse_proxy app:8000 { header_up X-Murara-Admin "1" }   # on @admin only
```

`TM_ADMIN_HASH` is a bcrypt hash (`caddy hash-password`), in `.env` like the
other secrets; the plaintext lives in the password manager. The app serves
`/admin*` only when `X-Murara-Admin: 1` is present **and** the request came
from the compose network (the header can be set only by the edge — the app
port is loopback-bound, HOSTING.md). Without the header: the neutral
„nicht gefunden" page. On the laptop (`docker compose up app`, no edge) the
page is reachable with `TM_ADMIN_OPEN=1` for development, never set on the
server.

### 5a. What the edge taught us — 2026-08-17

Two traps, both found by taking the site down for four minutes and both now
closed in the files rather than in someone's memory:

1. **An empty `TM_ADMIN_HASH` is a Caddyfile parse error**, and a config that
   does not parse takes the *whole edge* with it — app and public site
   included, not just `/admin`. Compose therefore passes a non-empty
   placeholder (`kein-passwort-gesetzt`) when the variable is unset: the
   config stays valid, no password matches, `/admin` answers 401.
2. **Compose expands `$` inside `.env` values.** A bcrypt hash pasted raw
   (`$2a$14$…`) arrives at Caddy mangled and no password ever works, with a
   `variable is not set` warning as the only clue. Every `$` must be doubled
   in `.env`; `.env.example` carries the one-line recipe.

Verified through the real edge afterwards: `/admin` 401 without credentials,
200 with them, the search answering from the live store; a request carrying
`X-Murara-Admin: 1` from outside still 401 — the header is set by the edge
after auth and stripped everywhere else.

## 6. Out of scope

Subscription and profile editing (operator); a review queue beyond the
„zurückgestellt" status; batch invitations; letters (dropped); anything for
customers — this page is the operator's, the tokened pages stay the
customer's.

## 6a. Built — 2026-08-17

All of §1–§5 except the deploy-side receipt: [`admin.py`](../admin.py) (index,
status vocabulary, HTML), the routes and the guard in [`app.py`](../app.py),
`app.activate` / `app.stop_customer` shared by the token pages and the
operator's, the `@admin` block in [`docker/Caddyfile`](../docker/Caddyfile),
`TM_ADMIN_USER` / `TM_ADMIN_HASH` in `.env.example` and compose,
`tests/test_admin.py` (14 tests: the guard, search, every status word, invite,
reissue, e-mail with and without a consent note, both stops).

Two things the building settled:

- **`aktiv · ohne Adresse`** joined the vocabulary of §3. The pilot customers
  have live subscriptions and no `consent_at`; „angelegt" would have hidden
  that reports are being written for them every week.
- **The index costs ~4 s on the first request** (22k procedures joined to
  their titles), then nothing until the store's mtime moves. Acceptable for
  a page one person opens; if it ever is not, the index belongs in the cycle.

Receipt against a copy of the live store: `q=blitzschutz` → 39 firms,
customers first; `q=Jebsen GmbH` → 1; no query → the 9 customers; the e-mail
and stop forms render for a named firm.

## 7. Order of work

1. `app._preflight` → shared `signup.activate(home, sub_id, email, consent)`
   used by both `POST /t/` and `/admin/email`; the search index over the
   store. Tests as for the token routes (WSGI called directly).
2. `/admin` list + status + counts; `/admin/invite`, `/reissue`.
3. `/admin/email`, `/admin/stop`.
4. Caddyfile block, `TM_ADMIN_USER/HASH` in `.env.example`, `X-Murara-Admin`
   check; deploy; verified on the server through the real edge before
   „done".
