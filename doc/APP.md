# APP — the customer-facing app, build specification

Written 2026-08-10. This is the self-contained spec for building the app
described in [`LAUNCH.md`](LAUNCH.md) §4: the **single live web surface** of
the product, running in Docker next to the database. Read `LAUNCH.md` first
for the why; this document is the what. Companions:
[`ONBOARDING.md`](ONBOARDING.md) (funnel and subscription mechanics),
[`LEGAL_BASIS_TARGET_LIST.md`](LEGAL_BASIS_TARGET_LIST.md) (what the notices
must say), `CLAUDE.md` (storage access rules — binding).

## 0. Decisions already made — do not reopen

| decision | consequence for this build |
| --- | --- |
| server-rendered HTML, no REST | routes return finished pages; no JSON endpoints, no CORS, no JS framework, no build chain. JSON appears the day a second consumer exists, beside the HTML, not instead |
| no login | no accounts, no passwords, no sessions, no cookies (also: no cookie banner) |
| capability tokens are the auth | see §3; nothing else authenticates anything |
| no confirmation steps | the customer is never asked to confirm what we can verify ourselves |
| German-language customer text | every customer-facing string; sober tone, no marketing fluff on functional pages |
| no admin UI | the back office is the console `report` subcommand and the review queue; the app serves customers only |

## 1. Shape

One small WSGI/ASGI app (stdlib-near; no heavy framework needed for seven
routes), one Docker container, sharing `data/` with the cycle. All storage
access through the project modules — `subscriptions.py` for anything
subscription-shaped, `ledger.py` for append-only event records; **never a
path into storage files, never raw SQL against another module's tables**
(house rule in `CLAUDE.md`). New subscription fields (e.g. `contact_state`,
`consent_at` if missing) go into `KNOWN` in `subscriptions.py` **in the same
commit** that first writes them.

SQLite is shared with the cycle: open with WAL mode, keep write transactions
short (single-row writes only — every handler writes at most one event and
one state change), and treat `database is locked` as a retry, not an error
page.

## 2. Routes

All under `app.<domain>`, HTTPS only. `<token>` is always a path segment,
never a query parameter (§3). **GET never mutates — every state change is a
POST from a page with a button** (this one rule is the defense against mail
scanners and prefetching; there are no exceptions).

| route | GET shows | POST does |
| --- | --- | --- |
| `/` | root page: one sentence, contact address, links to `/impressum`, `/datenschutz` | — |
| `/impressum`, `/datenschutz` | the legal pages; Datenschutzerklärung carries the long-form Art. 14 notice | — |
| `/t/<token>` | signup page: firm name, trade, market figures (from `market.py`), one e-mail field | records e-mail + `consent_at`, runs pre-flight (§4), shows "what happens next" page |
| `/f/<token>` | feedback confirm: lot title + one button naming the verdict the token carries | records the feedback event, thanks page |
| `/s/<token>` | stop page: two buttons (§5) | applies chosen stop, confirmation page stating exactly what is now off |
| `/c/<token>` | recall box: one input, "Nummer oder Link hier" | resolves the ref, answers with lot identity + verdict, records event (§6) |
| `/healthz` | 200 + cycle-data freshness timestamp | — |

Unknown or revoked token → one neutral page ("Dieser Link ist nicht mehr
gültig") with the contact address; identical for "never existed" and
"revoked" (no oracle). 404 for everything else.

Robots: `X-Robots-Tag: noindex` on every response; `robots.txt` disallows
all. The app must be unfindable, not merely unlisted.

## 3. Tokens

**[BUILD]** `tokens.py`, same pattern as the other modules: takes the data
directory, owns its own table, no other module touches it.

- **Value**: ≥128 bits from `secrets`, URL-safe alphabet. Stored server-side
  with: purpose, subject (customer id; plus lot id and verdict for `f`
  tokens), `created_at`, `revoked_at`, `used_at`.
- **Purpose-bound**: `t` (signup, per target-list firm, printed as QR),
  `f` (one lot × one verdict × one customer — a report with 10 lots carries
  20 `f` tokens), `s` (stop, per customer), `c` (recall box, per customer).
  A handler accepts exactly its own purpose. Verdict and lot live in the
  token row, never in a readable URL.
- **Lifetime**: `t` tokens live until used or revoked (letters sit on desks
  for months — the standing-link principle). `f` tokens are per-report;
  accepting a click on a superseded report is harmless and allowed. `s` and
  `c` are standing. Revocation is a timestamp, effective immediately.
- **Logs**: tokens never appear in full in any log — first 8 characters
  only. Access logs keep IP + path-prefix + timestamp, rotated; that is the
  whole analytics stack (cookieless by construction).
- **Rate limit**: token lookups per IP capped (a lazy brake on enumeration;
  128-bit randomness is the real defense).

## 4. Signup handler (`POST /t/<token>`)

1. Validate the e-mail syntactically; store it with `consent_at = now` on
   the customer row. The consent text on the page names what will come —
   weekly reports and, after the trial, occasional result updates and
   offers — because that sentence is the legal basis of the win-back
   channel (`LAUNCH.md` §3).
2. Run the **gate pre-flight check** (ONBOARDING.md §5.3): replay the
   firm's won lots against its proposed gate. Pass → write the activating
   subscription version through `subscriptions.py`; first report next
   cycle. Fail → subscription created **held**, review-queue state set; no
   report until a person fixes the profile.
3. Either way send one confirmation e-mail (through the guarded mailer, §7)
   saying what will arrive and when, carrying the standard footer (§8).
4. Duplicate submit (token already used): show the "already registered"
   page with the stored e-mail partially masked (`m…@firma.de`) and the
   contact address — no silent overwrite, no error.

## 5. Stop handler (`/s/<token>`)

Two buttons, two outcomes, written through `subscriptions.py` as state
changes plus a ledger event each (`LAUNCH.md` §3 has the full semantics):

- **"Keine wöchentlichen Berichte mehr"** → `contact_state = soft_stopped`.
- **"Keine E-Mails mehr"** → `contact_state = hard_stopped`, permanent.

Confirmation page states what is off and, for soft stops, that result
updates may still come — with the hard-stop button right there. A paying
customer's cancel flow lands on this same page pre-scoped: cancel sets
billing off + `soft_stopped`. Any ambiguous stop signal arriving elsewhere
(List-Unsubscribe, a reply) maps to **hard**; when in doubt, hard.

## 6. Recall handler (`/c/<token>`)

Input: a tender number or URL, pasted. **A submission is a question, never
a fact.**

1. Resolve against the lot store. Unresolvable → "nicht gefunden" page,
   nothing recorded but the attempt.
2. Resolved → answer with the lot's full identity (title, buyer, deadline)
   plus our verdict for *this* customer's profile — including, on request
   like this, the contested verdict ("Ihr Geschäft, aber wir erwarten viele
   Bieter — darum nicht empfohlen"). The echo is the error check; no
   confirmation is asked.
3. Learning: fits the customer's profile (trade, plausible region) → write
   the `learned_ref` + new subscription version. Doesn't fit → record to
   the review queue only. A wrong number can waste a click; it must never
   be able to poison a profile.

## 7. The guarded mailer

One module sends every e-mail the product ever sends. **The
`contact_state` check lives inside the mailer**, not in calling code — a
future bug must be unable to mail a `hard_stopped` customer. Weekly reports
require `active`; results notes and win-back require `active` or
`soft_stopped`; `hard_stopped` sends nothing, ever, and the attempt is
logged as a defect. Every send is a ledger event.

## 8. Every outgoing e-mail carries

- the Art. 21 objection notice, clearly separated from content (legal doc
  §5; DSK OH 5.2 recommends it on every mailing);
- the `/s/<token>` link ("Abbestellen") and a `List-Unsubscribe` header
  mapping to **hard** stop;
- the `/c/<token>` recall link in the report footer;
- the standing subscribe link, post-trial (`LAUNCH.md` §3);
- plain-HTML body readable without images or JS; `f` links per lot.

## 9. Deployment and ops

- One container beside the cycle, same volume for `data/`. The app reads
  the database per request: **no deployment on data change**, code deploys
  only on code change.
- **[CLARIFY]** (the one open hosting decision, blocks printing letters):
  small VPS running both cycle and app, or a tunnel to the current box.
  Either way the app must answer around the clock — a customer clicks at
  21:00 on a Sunday.
- TLS terminated in front (Caddy or the host's proxy), HSTS on.
- App down = forms fail visibly; picks, grading and report generation are
  unaffected (they live in the cycle). Restart policy `always`; `/healthz`
  is the check.
- Backups are the database's problem (already handled by the cycle's
  environment), not the app's; the app holds no state outside `data/`.

## 10b. Build status — 2026-08-10

The **serving core is built and answers from the image**:
[`app.py`](../app.py) (stdlib `wsgiref`, no new dependency — seven routes did
not need a framework and `requirements.txt` is unchanged) and
[`tokens.py`](../tokens.py), the §3 `[BUILD]` module, with its own `token`
table. That table lands through `db.connect`'s additive self-heal, the same way
`simulation_gate` did, so no migration runs — and it is deliberately **not** in
`LEDGER_TABLES`: revocation that could not take effect immediately would not be
revocation.

*Receipt* — against a container holding a copy of the real database:

    GET /            -> 200, text/html, X-Robots-Tag: noindex, nofollow, noarchive
    GET /t/<token>   -> 200, the firm's signup page, token minted in the container
    GET /healthz     -> 200, cycle_last_success=20260810, cycle_age_days=0

Done: `/`, `/impressum`, `/datenschutz`, `/healthz`, `robots.txt`, the neutral
invalid-token page, and the **GET** side of all four token routes. Purpose
binding, revocation and the no-oracle rule are enforced and tested — a feedback
token is not a stop token, and a revoked token renders byte-identically to one
that never existed. 24 tests in `tests/test_app.py`, no port bound and no real
data: the WSGI callable is called directly, which is what a request reduces to.

Two headers beyond the spec, both because tokens live in the URL path:
`Referrer-Policy: no-referrer` (a token must not travel in a `Referer` to any
link the page names) and a `Content-Security-Policy` of `default-src 'none'`,
which costs nothing on pages that already load nothing.

**Not built, and why:** the POST handlers of §4-§6 and the mailer of §7. They
need `contact_state`, `email` and `consent_at`, none of which are in
`subscriptions.KNOWN` — and CLAUDE.md requires the `KNOWN` half to land *in the
same commit* as the first write, so those fields belong to the commit that
writes them, not to this one. Until then a POST to a token route answers **405**
with a page saying so, rather than a form that accepts what a customer typed and
drops it. The §9 `[CLARIFY]` (VPS or tunnel) is untouched and still blocks
printing letters, as is the real Impressum text, which is left visibly absent
rather than filled with a plausible placeholder.

## 10. Explicitly out of scope

No REST API, no JS framework, no login, no admin pages, no dashboard, no
analytics beyond access logs, no cookie, no CAPTCHA, no e-mail
verification loop, no rate-limited signup funnel — each of these is either
deferred with a named trigger (`LAUNCH.md` §4.3) or rejected with reasons
(`LAUNCH.md` throughout). When in doubt: the app stays a document server
with seven routes.
