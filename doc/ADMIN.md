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
| Search by **trade or name**, over the awards store | „blitzschutz" → every winner whose *own trade* is that; „Jebsen GmbH" → that firm. Trades are words, never CPV (house rule) |
| **The trade is the gate's**, not this page's (2026-08-18) | a firm's trade is `evidence.core_keywords` over its newest wins — the identical derivation `relevance.build_profile` hands the delivery gate. The operator therefore reads the firm the way the product will serve it, and there is no second definition to drift (§4) |

## 1. The concrete case

The operator opens `https://app.murara.eu/admin`, the browser asks for the
password once.

1. Types **blitzschutz** in the one search field. The page says which trade
   that word is (`blitzschutz`) and lists every firm in the awards store
   whose *own* trade recurs on it — exact winner spelling, wins, single-bid
   wins, last win, **the firm's trade** (`blitzschutz · fangstang · erder`,
   with the evidence on hover: „blitzschutz: 4 von 6 Referenzen"), and the
   **status** column (§3). Strongest first: a firm with 6 of 6 lightning
   lots stands above one with 2 of 6, and a general contractor who once
   built a lightning system is not in the list at all. Jebsen GmbH is
   *Kunde · aktiv · Tag 11 von 28*; twelve others are *nicht eingeladen*.
2. Types **Jens Dunkel Glas- und Bauelemente GmbH** — the list shows that
   one firm: *Link erzeugt · linkedin · 17.08.* with the one filled button
   **Nachricht anzeigen** and, as plain links, *E-Mail eintragen · Stoppen*
   (§3a).
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
5. A customer phones: no more mails. On its row presses **Stoppen** → one
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
| `/admin/sent` | — | `sub_id` → `invite_sent` event with the channel copied from the invitation; the row becomes *angeschrieben*. Posted from the button **Als verschickt markieren** on `/admin/message` |
| `/admin/message` | the two texts to paste (ONBOARDING.md 9.2a): connection note ≤300 chars, and the message with picks, own win and the live invitation link; below them **Als verschickt markieren** (until the row is *angeschrieben*) and **Neuen Link erzeugen** (`/admin/reissue`) | — |
| `/admin/stop` | confirmation page for `sub_id`, two buttons | `wahl` = berichte / alles → the same writes as `post_stop` (soft/hard, tokens revoked on hard); event `stop_soft` / `stop_hard` with `detail=admin` |
| `/admin/experiments` | the A/B overview (doc/EXPERIMENTS.md §9): open experiments with verdict line and per-arm tables, closed ones, the constants; read-only, links back to the list | — |

Errors from the modules (`InviteError`, `SubscriptionError`) render as one
red line above the list, never a stack trace. All routes `noindex`, CSP as
the rest of the app; `/admin` is excluded from the token rate limit and
included in the access log like every path.

## 3. Status vocabulary (one word per row, computed, never stored)

| status | derived from |
| --- | --- |
| nicht eingeladen | no customer row |
| Link erzeugt · `<channel>` · `<date>` | customer row, no `consent_at`; from the `invited` event — a link exists, **nobody has been written to** |
| angeschrieben · `<channel>` · `<date>` | the operator pressed „verschickt" (`invite_sent` event). Minting is not contacting: without this word a silent firm that was never written to looks like one that ignored us |
| angemeldet · Tag n von 28 / zurückgestellt | `consent_at` set; active version → `subscriptions.trial_status`; `signup_held` and no active version → zurückgestellt |
| Kunde · bezahlt | `plan: paid` |
| gefragt | `ask` event, still trial |
| gestoppt (Berichte) / gestoppt (alles) | `contact_state` soft / hard |
| Widerspruch | hard-stopped by `objection` |

The e-mail is shown masked (`m…@firma.de`), full on the row's own edit
form only.

### 3a. Row actions — one next step, the rest quiet (2026-08-18)

Until then a row without an address carried five controls in three
markups: *Nachricht*, *verschickt*, *URL neu*, *E-Mail eintragen*, *Abmelden*
— two filled, three outlined, some `<a><button>`, some forms, one label a
noun, one a participle, one no phrase at all. Now `admin.row_actions` decides
from the status alone, and the rule is:

- **At most one filled button per row**: the next step of the funnel
  (ONBOARDING §9). *Link erzeugt* → **Nachricht anzeigen**; *angeschrieben*
  → **E-Mail eintragen**; a served row → none.
- **Everything else is a plain link** in the same cell: *E-Mail eintragen* /
  *E-Mail ändern*, *Nachricht anzeigen*, *Stoppen*. Stopped rows offer
  nothing.
- **Every label is an infinitive**, the operator's action, never a status
  word. *Abmelden* became **Stoppen** (the operator stops delivery for a
  firm; *abmelden* is what a customer does to itself, and matches the status
  *gestoppt*).
- **Marking as sent and re-minting the link live on the message page**, next
  to the text that was (or could not be) sent: **Als verschickt markieren**
  (shown until `invite_sent`) and **Neuen Link erzeugen**. They are
  consequences of using the message, not row concerns; the missing-link
  warning on that page carries the re-mint button itself.

| status | filled | links |
| --- | --- | --- |
| nicht eingeladen | `[channel] Einladen` | — |
| Link erzeugt | Nachricht anzeigen | E-Mail eintragen · Stoppen |
| angeschrieben | E-Mail eintragen | Nachricht anzeigen · Stoppen |
| aktiv · ohne Adresse | E-Mail eintragen | Stoppen |
| angemeldet / zurückgestellt / gefragt / Kunde | — | E-Mail ändern · Stoppen |
| gestoppt / Widerspruch | — | — |

## 4. Search — the trade is the one the reports use

Revised 2026-08-18. Until then this page answered a different question from
the product, in the same words.

**What it did.** `outreach.winner_rows` joined to every won-lot title, folded
into one string per firm, and `q in haystack`. One occurrence anywhere, raw
substring, no vocabulary, no recurrence, no distinction between a title and a
description. That is precisely the *context* the delivery gate throws away:
a general contractor with one electrical lot in forty is an electrician by
that rule, and its report would never contain electrical work.

**What it does.** The same derivation the gate uses, per firm:

1. **The references** — the firm's newest `outreach.MAX_PROFILE_REFS` (6)
   wins, deduped onto their contract notices, read through
   `evidence.leistung_text` (`admin._refs_of`). The window
   `relevance.build_profile` builds a customer's profile from.
2. **The trade** — `evidence.core_keywords` over those references: roots from
   the person-reviewed `cpv_trade_roots.txt`, kept when they recur in at
   least `CORE_SHARE` of the root-bearing references, with the single-
   reference title rule, the family pass and the title fallback that
   doc/RELEVANCE.md phases 8o–9f argue for, plus any root in the firm's own
   name. Nothing new is decided here; if the rule changes there, it changes
   here in the same commit.
3. **The query** — `admin.query_roots` puts the typed word through
   `evidence.roots_in`: „Elektroinstallation" → `elektro`. A firm answers when
   one of those roots is in its core. A word the vocabulary does not know
   returns nothing and the page says so, rather than silently searching a
   substring.
4. **The order** — `admin.trade_strength`: the largest share of the firm's
   references carrying a matched root (a root off the firm's own name counts
   1.0, because it is on every reference by definition), then how many
   references that was, then wins. Customers still sort first.

Name search is unchanged and deliberately still a substring: the operator
pastes a company name off a LinkedIn profile, and no root vocabulary helps
there. A query matches on **either** half.

**The consequences, measured over the 2026-08-10 store** (5,476 winner
spellings, `q=Elektroinstallation`):

| | firms |
|---|---|
| before — substring in name + every won title | 100 |
| after — firms whose own trade recurs on `elektro` | 428 |
| in both | 98 |
| dropped: matched the letters, not their trade | 2 |
| added: their trade, but never those 19 letters in a title | 330 |

The two dropped are Gebrüder Peters Gebäudetechnik SE (13 wins, core
`beton · mauer · lueftung · trockenbau`) and ABRAX Sicherheitstechnik (core
`brandmelde`) — both firms the gate would never send an electrical lot to.
The 330 added are firms that write „Elektroarbeiten" or „Elektrische Anlagen"
where the operator typed „Elektroinstallation"; the root is the same word for
all of them, which is the whole point of a vocabulary.

**Firms with no trade at all** (200 of 5,476) appear under no trade word and
are marked *ohne Gewerk* on the row. Almost all are blind framework slices
whose titles name no work; that they cannot be found by trade is correct and
consistent with the gate, which cannot build them a profile either.

**Cost, and the rule it forced (2026-08-18, same day).** Deriving the trades
reads lot descriptions: 34 s over the laptop's 5,476 winners, **158 s over
the server's 15,508**. The first cut ran that inside the first request that
needed it, and the first `/admin` after a deploy took 158 s — to list two
customers, because `search()` built the whole index before looking at the
query. So:

- **A request never derives.** The index is a file, `data/admin_index.json`,
  holding exactly what a row prints (name, numbers, core roots with their
  counts — no texts). Its only writer is `admin.build_index`, run by the
  cycle (`cycle.py`, after the store moved) and by every deploy
  (`docker/deploy.sh build_site`, with the image just proved), or by hand:
  `python admin.py --build`. A request reads it once per process (~0.05 s)
  and again when its mtime moves.
- **The empty query does not open the index at all.** The customers' numbers
  come from the awards store for those few names (0.1 s); the index is
  consulted only if the process already holds it.
- **Missing file → the page still opens instantly** — customers and name
  search — and says the trade search is not ready and how to build it.
  **Stale file** (built for another store, or under other rules or another
  `cpv_trade_roots.txt`) → served, marked *Index von einem älteren Stand*;
  the next cycle or deploy replaces it. A slightly old list beats none.

Result capped at 100 rows with a „mehr eingrenzen" note.

## 5. Protection

Caddyfile, app block:

```
@admin path /admin /admin/*
basic_auth @admin {
    {$TM_ADMIN_USER} {$TM_ADMIN_HASH}
}
reverse_proxy app:8000 { header_up X-Murara-Admin "1" }   # on @admin only
```

`TM_ADMIN_HASH` is a bcrypt hash, and **never written by hand** —
[`docker/admin-password.sh`](../docker/admin-password.sh), built 2026-08-17:

```
TM_SERVER=<host> bash docker/admin-password.sh status   # which file, which mode, is one set
TM_SERVER=<host> bash docker/admin-password.sh set      # hidden prompt, then the edge restarts
```

`set` opens **exactly one** ssh connection. It is opened first, so a
passphrase-protected key asks for its passphrase there — labelled by ssh —
and the remote half answers `READY` over that same connection before this
script asks for anything; the password then travels down the pipe that is
already open, and the confirmation comes back through it. Two connections
(the earlier shape) meant two passphrase prompts with the password prompt
between them, which is unusable. `ssh-add` once per session removes the
passphrase prompt altogether — ssh always reads a passphrase from the
terminal, never from stdin, so the two can never mix. Then a
headed block names what is being set: the web page's password, not the SSH
key. The prompt shows one `*` per character, takes backspace, and asks a second
time. Too short, or the two entries differing, is a **retry** — three tries
on the one open connection, rather than an exit that throws away the
connection and the passphrase with it — a prompt that shows
nothing at all leaves you guessing whether the keyboard is reaching it
(operator, 2026-08-18). The password (or the password manager's value via
`TM_SECRET_SOURCE`, doc/SECRETS.md) travels over ssh's **stdin only** —
no process list, no history, no temporary file — is hashed **on the server**
by `caddy hash-password`, and only the hash is written, with every `$`
doubled (§5a) and `.env` left at mode 600. The plaintext lives in the
password manager and nowhere else.

Why not `secrets.sh`: that tool moves whole files and deliberately never
parses a value (doc/SECRETS.md §3); this one must, because the password may
not be stored at all — only a hash of it. `TM_ADMIN_ENV_FILE` points it at
`env.d/admin.env` when the layered layout lands; nothing else changes.
*Receipt, live:* password set through the tool, `/admin` 200 with it and 401
without, `.env` 664 → 600, app and site untouched. The app serves
`/admin*` only when `X-Murara-Admin: 1` is present **and** the request came
from the compose network (the header can be set only by the edge — the app
port is loopback-bound, HOSTING.md). Without the header: the neutral
„nicht gefunden" page. On the laptop (`docker compose up app`, no edge) the
page is reachable with `TM_ADMIN_OPEN=1` for development, never set on the
server.

### 5c. The file is the truth — 2026-08-18

The credential is no longer an environment variable. It is one line of Caddy
config in `/etc/murara/caddy.d/admin.caddy`:

```
murara $2a$14$<hash>
```

`docker/Caddyfile` imports it (`import /etc/caddy/secrets/admin*.caddy`
inside the `basic_auth` block); compose mounts `/etc/murara/caddy.d`
read-only into the edge as `/etc/caddy/secrets`. `admin-password.sh set`
writes that file and runs `caddy reload` — no recreate, no deploy, no
restart.

**Why it had to change.** An environment variable is a copy handed to a
process when it is created. With `{$TM_ADMIN_HASH}` the password *in force*
was whatever `admin.env` said the last time the edge container was created,
which is not the same thing as what the file says. On 2026-08-18 a `set`
wrote a new hash at 08:22, the edge kept the old one, the browser kept
working with the old password — and an unrelated deploy at 08:47 recreated
the edge, put the 08:22 password in force, and the operator was asked for a
password whose change he had made half an hour earlier. Every inspection in
between said "the file is correct", and every one of them was right. Two
truths, one password.

With the credential imported from a file: a deploy cannot change what is in
force (it recreates the edge, which re-reads the same file and arrives at the
same answer), and a `set` cannot leave a gap (it reloads, then proves it).

**What `set` now does, in order**: hash the password inside Caddy's own image
· write `<user> <hash>` to a staged file · `caddy validate` the real
Caddyfile against it, so a malformed credential is refused *before* the next
container recreate could pick it up · move it into place · `caddy reload` ·
ask the live edge for `/admin` with that password over TLS on the real name,
and report **IN FORCE** or exit non-zero. The password is never in a process
list, a file, or the shell history — it travels on stdin, both to
`hash-password` and to `curl --config -`.

**Measured, not assumed** (throwaway containers, 2026-08-18): no credential
file at all → the import glob matches nothing, the block is empty, `/admin`
answers **401 to every password and the edge starts normally**; an empty file
and a comment-only file behave the same; a replaced file (new inode) is
picked up by `caddy reload` because the mount is the *directory*; a malformed
file makes `reload` refuse and the edge keeps serving the config it has.

The `$`-doubling is gone with the environment variable — nothing interpolates
a Caddy snippet, so the hash is stored exactly as `caddy hash-password`
printed it.

### 5b. Where the credential lived until §5c — 2026-08-18

Superseded the same day by §5c, and kept because its three lessons are why
the file-imported credential looks the way it does.

`/etc/murara/env.d/admin.env` (doc/SECRETS.md §1), mode 600, read by the
**edge alone** through `env_file: [{path: …, required: false}]`. The cycle
and the app never had the admin hash in their environment, and a laptop
without the file still started.

Three things this cost, all now in the files:

1. **`environment:` overrides `env_file:`.** A placeholder written in the
   compose `environment:` block won over `admin.env` permanently. There is
   no TM_ADMIN_* in `environment:` any more; the closed-door default lives in
   the Caddyfile.
2. **The closed-door default must be a valid bcrypt string.** Caddy
   base64-decodes the hash while *loading* its config, so the friendly
   placeholder `kein-passwort-gesetzt` did not fail closed — it failed to
   parse and took the whole edge down, app and public site with it. The
   default is now a bcrypt hash of 30 random bytes nobody holds: an unarmed
   machine answers 401 and still starts.
3. **Compose interpolates `$` in `env_file` values exactly as in `.env`** —
   measured, not assumed: `X=$2a$14$abc…` in an env_file reaches the
   container as `$2a$14`. Every `$` of the hash is doubled wherever it is
   written, and `admin-password.sh` does it.

Also: recreating the edge needs `--profile edge`, or compose silently does
nothing and the old password keeps working — which reads exactly like a
password that did not take.

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

## 6b. The experiments page moved in — 2026-08-18

`/experiments/<key>` became `/admin/experiments`. It had its own value,
`TM_EXPERIMENTS_KEY`, and was an unlisted URL rather than a door: anybody
who ever saw the link kept it, and rotating it meant editing `.env`,
compose, `SECRETS.md` and the server. It is the same reader — the operator
— behind the same edge, so it is now one more `ADMIN_ROUTES` entry under
the one credential of §5b. `TM_EXPERIMENTS_KEY` is gone from `app.py`,
`docker-compose.yml`, `.env.example` and `SECRETS.md`'s `env.d` layout;
`/admin` carries the link to the page, the page a link back.

## 7. Order of work

1. `app._preflight` → shared `signup.activate(home, sub_id, email, consent)`
   used by both `POST /t/` and `/admin/email`; the search index over the
   store. Tests as for the token routes (WSGI called directly).
2. `/admin` list + status + counts; `/admin/invite`, `/reissue`.
3. `/admin/email`, `/admin/stop`.
4. Caddyfile block, `TM_ADMIN_USER/HASH` in `.env.example`, `X-Murara-Admin`
   check; deploy; verified on the server through the real edge before
   „done".
