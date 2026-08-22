"""TenderMining Stripe transport — doc/PAYMENT.md.

The payment rule (operator, 2026-08-20): **a subscription exists only when
the customer has paid, or the operator activated the firm from the admin
page.** A "Ja" click alone changes nothing — it opens Stripe Checkout, and
only the signed webhook flips the plan. No SDK: Stripe's API is form-encoded
HTTPS and the webhook signature is an HMAC, both stdlib work, same as the
mailer's transport.

Three jobs, nothing else:

  * `checkout_url(...)`  — one Checkout Session for one firm, 79 EUR/month
    (the price object lives at Stripe; `TM_STRIPE_PRICE_ID` names it).
  * `cancel(...)`        — end a subscription NOW ("no more payments, no
    more emails" — the running month is not refunded, it is simply the last).
  * `verify_signature()` — the webhook's proof that Stripe sent the event.

Every function takes an optional `transport` for tests; the network is never
touched outside `_call`. Keys: `STRIPE_SECRET_KEY` and
`STRIPE_WEBHOOK_SECRET` from `payments.env` (doc/SECRETS.md 1);
`TM_STRIPE_PRICE_ID` from `site.env` — an id, not a secret.
"""

import hmac
import json
import os
import time
import urllib.parse
import urllib.request

API_BASE = 'https://api.stripe.com'
KEY_ENV = 'STRIPE_SECRET_KEY'
WEBHOOK_ENV = 'STRIPE_WEBHOOK_SECRET'
PRICE_ENV = 'TM_STRIPE_PRICE_ID'
TOLERANCE = 300                      # seconds a webhook timestamp may be old


class StripeError(RuntimeError):
    """A call that could not happen or an answer that made no sense."""


def configured():
    """Payment is live only when the key AND the price are set. The webhook
    secret is checked where the webhook arrives — a missing one must fail
    the event, not silently disable checkout."""
    return bool(os.environ.get(KEY_ENV, '').strip()
                and os.environ.get(PRICE_ENV, '').strip())


def _call(method, path, fields=None, transport=None):
    """One authenticated form-encoded call -> parsed JSON. Bodies carry no
    secret of ours beyond the key in the header; Stripe errors surface with
    Stripe's own message, which names the fix better than we could."""
    key = os.environ.get(KEY_ENV, '').strip()
    if not key:
        raise StripeError(f'{KEY_ENV} is not set — cannot talk to Stripe')
    if transport is not None:
        return transport(method, path, fields)
    data = urllib.parse.urlencode(fields or {}).encode('ascii') \
        if fields else None
    req = urllib.request.Request(
        API_BASE + path, data=data, method=method,
        headers={'Authorization': f'Bearer {key}',
                 'Content-Type': 'application/x-www-form-urlencoded',
                 # Cloudflare rejects urllib's bare signature (mailer.py,
                 # measured 2026-08-20); name the product everywhere
                 'User-Agent': 'murara-mailer/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode('utf-8'))
            msg = detail.get('error', {}).get('message', str(e))
        except Exception:                                      # noqa: BLE001
            msg = str(e)
        raise StripeError(f'Stripe {e.code}: {msg}') from e


def checkout_url(sub_id, email, success_url, cancel_url, transport=None):
    """A Checkout Session for this firm -> the URL to send the browser to.

    `client_reference_id` carries the sub_id out and back: it is the ONLY
    join between a Stripe event and our customer row at checkout time, so it
    goes on the session AND into the subscription's metadata (the session is
    gone after completion; the subscription lives as long as the payments)."""
    fields = {
        'mode': 'subscription',
        'line_items[0][price]': os.environ.get(PRICE_ENV, '').strip(),
        'line_items[0][quantity]': '1',
        'client_reference_id': sub_id,
        'subscription_data[metadata][sub_id]': sub_id,
        'success_url': success_url,
        'cancel_url': cancel_url,
        'locale': 'de',
    }
    if email:
        fields['customer_email'] = email
    out = _call('POST', '/v1/checkout/sessions', fields, transport=transport)
    url = (out or {}).get('url')
    if not url:
        raise StripeError(f'checkout session came back without a url: '
                          f'{sorted((out or {}))}')
    return url


def cancel(stripe_subscription_id, transport=None):
    """End the subscription immediately. DELETE is Stripe's 'now', as opposed
    to cancel_at_period_end — the operator's rule is that a stop means no
    further payment, and the already-paid month simply runs out unmailed."""
    if not stripe_subscription_id:
        raise StripeError('no stripe_subscription_id to cancel')
    return _call('DELETE',
                 f'/v1/subscriptions/{stripe_subscription_id}',
                 transport=transport)


def verify_signature(payload, header, *, secret=None, now=None):
    """True only for a fresh, correctly signed webhook body.

    Stripe-Signature is `t=<unix>,v1=<hexdigest>[,v1=…]`; the signed string
    is `<t>.<raw body>`. Comparison is constant-time; the timestamp bounds a
    replay at TOLERANCE seconds. A missing secret fails closed."""
    secret = (secret if secret is not None
              else os.environ.get(WEBHOOK_ENV, '')).strip()
    if not secret or not header:
        return False
    parts = dict()
    v1s = []
    for piece in header.split(','):
        k, _, v = piece.strip().partition('=')
        if k == 'v1':
            v1s.append(v)
        else:
            parts[k] = v
    try:
        ts = int(parts.get('t', ''))
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts) > TOLERANCE:
        return False
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    want = hmac.new(secret.encode('ascii'),
                    f'{ts}.'.encode('ascii') + payload,
                    'sha256').hexdigest()
    return any(hmac.compare_digest(want, v) for v in v1s)
