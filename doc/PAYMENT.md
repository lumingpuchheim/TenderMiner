# PAYMENT — Stripe, and the one rule

Written 2026-08-22 from the operator's decisions of 2026-08-20/22. Replaces
the payment-link design of LAUNCH.md §6 (a static `TM_STRIPE_URL`, plan
flipped on trust at the yes click) — that design never carried a real
payment and is gone from the code.

## 0. The one rule

> "subscription only when customer pays or i activate them in backdoor for
> testing" (operator, 2026-08-20)

`plan: paid` is written by exactly one function, `app.activate_paid`, and it
has exactly two callers: the **signed Stripe webhook** after a completed
checkout, and the **admin Aktivieren button**. Nothing else — not the yes
click, not a script — makes a customer. Every activation ledgers a
`paid_started` event naming which door it came through (`stripe: sub_…` or
`admin (backdoor)`).

There is deliberately **no intermediate state to see**: a customer who
clicked yes but did not pay looks exactly like one who never clicked, both
on the admin page and in the counts line. The raw `subscribe_yes` events
stay in the ledger for a support question ("I clicked and paid but nothing
happened"), but no surface is built on them.

## 1. The customer's path

1. The subscribe box's button opens `/y/<token>` — unchanged.
2. POST (the "Ja, Murara abonnieren" click) creates a **Stripe Checkout
   Session** (`stripe_pay.checkout_url`) and answers **303 → Stripe**:
   mode `subscription`, the price named by `TM_STRIPE_PRICE_ID`, the firm's
   e-mail prefilled, `client_reference_id = sub_id` (also copied into the
   subscription's metadata — the session dies after checkout, the
   subscription lives as long as the payments).
3. Stripe hosts the payment page. Kleinunternehmer §19 UStG: the price is
   79 € flat, **no VAT anywhere** — the price object carries no tax, and
   the §19 clause stands in the dashboard's invoice footer (§6 step 4).
4. After payment Stripe redirects to `/danke` (static — it promises nothing
   the webhook has not yet made true) and POSTs
   `checkout.session.completed` to `/stripe/webhook`.
5. The webhook (HMAC-verified, `STRIPE_WEBHOOK_SECRET`) stores
   `stripe_customer_id` + `stripe_subscription_id` on the customer row and
   calls `activate_paid` → the trial clock stops, the reports keep coming.

**Stripe not configured / down** (no keys, or the session call fails): the
click ledgers `subscribe_yes`, the customer reads "wir melden uns", the
operator gets a mail — and NO plan changes. The operator arranges payment
and uses the backdoor.

## 2. Unsubscribe ends the payments too

`app.stop_customer` (the one stop path: customer page, admin page, RFC 8058
one-click) additionally cancels the Stripe subscription **immediately**
(`DELETE /v1/subscriptions/…` — not `cancel_at_period_end`; the operator's
words are "no more payments, no more emails", and the already-paid month
simply runs out unmailed, no refund). The stop itself **never waits on
Stripe**: an API outage cannot block an Art. 21 objection — the failure is
ledgered (`stripe_cancel_failed`) and the operator mailed to cancel by
hand. Success ledgers `stripe_cancelled`.

A subscription that ends **at Stripe** (card failure, dashboard cancel)
arrives as `customer.subscription.deleted`: if we stopped the customer
ourselves nothing happens; otherwise the operator is mailed and
`stripe_sub_ended` ledgered — the plan does NOT change automatically,
because §0 allows only two doors and "Stripe stopped paying us" is not one
of them. The operator decides: backdoor, or stop.

## 3. The admin page's three doors

| action | page | what it does |
| --- | --- | --- |
| **Aktivieren** | `/admin/activate?sub_id=…` | the backdoor: `plan: paid` without Stripe, `paid_started` detail `admin (backdoor)`. For tests and payments arranged outside Stripe. Offered until the row is a customer. |
| **Reaktivieren** | `/admin/unstop?sub_id=…` | the way back from `hard_stopped` — with a REQUIRED note (why/when the firm asked to return, or that this is a test), ledgered as `unstop` detail `admin: <note>`. The stop page's "dauerhaft" stays honest: only the firm's own request (or the operator's test) reopens the door. Revoked tokens stay revoked; standing links re-mint on the next mail. |
| **Löschen** | `/admin/delete?sub_id=…` | full erasure (`subscriptions.erase`): customer row, every subscription version, every token, every app event — one transaction. Two buttons, see §3a: **Löschen + Sperrliste** (default) keeps the firm's name as a do-not-contact marker; **Restlos löschen** keeps nothing (test data only — a firm can be invited again as if never seen). Refused while a live Stripe subscription exists (deleting our records cannot stop a payment — stop first, which cancels). Refused for firms the frozen pre-migration JSONL files mention (`ledger.frozen_mentions`): deleting their DB rows would trip the stale-file guard on every later read. |

Erasure is the one legal exception to the append-only triggers on
`subscription_version` and `app_event`; `subscriptions.erase` drops the two
delete-triggers inside its transaction and recreates them before commit.

### 3a. "Löscht alles und schreibt uns nie wieder" — the suppression entry

The two demands collide: never-contact requires remembering the firm,
erasure requires forgetting it. Art. 17 Abs. 3 DSGVO resolves it — data
needed to comply with a legal obligation (here: honouring the Art. 21
objection, durably) is exempt from erasure. So the DEFAULT delete keeps a
**suppression entry**: after `erase`, the customer row is re-created with
nothing but the firm's name(s) and `hard_stopped`, plus one `objection`
event (`Sperrvermerk nach Löschung`). Address, notes, history, versions,
tokens: gone. The row shows as `Widerspruch` with no data behind it.

The marker is not pedantry: the prospect list is rebuilt from **public**
procurement data every cycle, so a fully forgotten firm resurfaces as a
fresh lead and would be written to again — the marker is what makes the
erasure compatible with the promise. The reply to the firm, in one
sentence: „Wir haben alle Ihre Daten gelöscht. Nur Ihren Firmennamen
behalten wir als Sperrvermerk — das ist nötig, um Ihren Wunsch, nie wieder
kontaktiert zu werden, dauerhaft zu erfüllen (Art. 17 Abs. 3 DSGVO)."

There is no restlos button for a real firm: the total forget exists only
for test twins (§3b), decided by the `test-` id and not by a click a hurry
could get wrong.

### 3b. Test twins — `testfirm.py` (operator's manual: doc/TESTFIRM.md)

The operator tests with real firms, and every admin state is then a false
statement about a real firm ("Jebsen objected" — they didn't; "never
contact Jebsen" — they're a real prospect). The twin resolves it: `add`
builds a customer from the REAL firm's public award history (that is what
makes the mails real — same profile door as a real invitation) under the
identity `test-<slug>` / "TEST <name>", pointed at the operator's inbox.

* A twin is a full customer to delivery (operator, 2026-08-22: "i want to
  receive real monday mails for test companies") — the Monday cron mails
  it, the trial clock runs: four free mails, the ask, silence. `remove` +
  `add` restarts the trial; `add --paid` makes the Mondays endless.
* `send` mails the current report NOW through the same door — it counts as
  one of the trial mails, because it is one.
* Twins are excluded from the admin counts line; the real firm's row stays
  untouched and invitable throughout.
* `remove` (and the admin Löschen page for a `test-` id) erases restlos —
  a twin never objected, so no Sperrvermerk — and the `test-` prefix guard
  means the tool cannot delete a real firm. A test-mode Stripe
  subscription is cancelled first; if that fails the erase proceeds and
  the id is printed for the dashboard.

```
python testfirm.py add "Jebsen" [--email you@…] [--paid]
python testfirm.py send "Jebsen"
python testfirm.py remove "Jebsen"
python testfirm.py list
```

The operator's test loop ("I will frequently add and remove Jebsen") does
NOT run through these doors — it runs on twins (§3b):
`testfirm.py add "Jebsen"` → real Monday mails / `send` / a test-mode
checkout → `testfirm.py remove "Jebsen"` → add again. The real firm's row
never carries a test state.

## 4. The transport (`stripe_pay.py`)

No SDK. Stripe's API is form-encoded HTTPS (two calls: create checkout
session, cancel subscription) and the webhook signature is
`HMAC-SHA256(secret, "t.body")` — all stdlib, tests inject a transport,
same shape as the mailer. The webhook route is POST-only, never
rate-limited (Stripe retries in bursts; the signature is the
authentication), 400 on a bad signature so a misconfigured secret is loud
in Stripe's dashboard, 200 for every event kind we do not handle.

## 5. Configuration

| name | file | what |
| --- | --- | --- |
| `STRIPE_SECRET_KEY` | `payments.env` | `sk_test_…` = test mode, `sk_live_…` = real money; same code |
| `STRIPE_WEBHOOK_SECRET` | `payments.env` | the endpoint's signing secret (`whsec_…`) |
| `TM_STRIPE_PRICE_ID` | `site.env` | `price_…` — an id, not a secret, but per machine (test vs live) |

`payments.env` is wired to the **app service only** (compose `env_file`) —
checkout, webhook and the stop's cancel all live in the app; the scheduler
never sees the key. Push with `bash docker/secrets.sh push payments.env
site.env`. With nothing set, `stripe_pay.configured()` is False and the
whole feature degrades to "wir melden uns".

## 6. Stripe dashboard setup — once per mode (test, then live)

1. **Product + price**: Products → Add product: "Murara", recurring,
   79,00 €, monthly, **no tax** (Kleinunternehmer). Copy the `price_…` id →
   `site.env` `TM_STRIPE_PRICE_ID`.
2. **API key**: Developers → API keys → secret key (`sk_test_…` first) →
   `payments.env` `STRIPE_SECRET_KEY`.
3. **Webhook**: Developers → Webhooks → Add endpoint,
   `https://app.murara.eu/stripe/webhook`, events
   `checkout.session.completed` + `customer.subscription.deleted`. Copy the
   signing secret (`whsec_…`) → `payments.env` `STRIPE_WEBHOOK_SECRET`.
4. **§19 UStG**: Settings → Invoice template → footer: "Gemäß § 19 UStG
   wird keine Umsatzsteuer berechnet." (applies to receipts/invoices Stripe
   sends).
5. `bash docker/secrets.sh push payments.env site.env` — recreates the app;
   `secrets.sh list` shows the three keys set.
6. Test-mode receipt: invite a test firm to the operator's own inbox →
   yes-click → Stripe's test card `4242 4242 4242 4242` → the row shows
   "Kunde · bezahlt" and `paid_started` says `stripe: sub_…`; then the stop
   button → Stripe dashboard shows the subscription canceled. Then repeat
   §6 steps 1–5 in live mode with real keys.
