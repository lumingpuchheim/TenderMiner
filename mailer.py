"""TenderMining guarded mailer — doc/APP.md 7.

One module sends every e-mail the product ever sends, and **the contact_state
check lives here**, not in calling code: a future bug must be unable to mail a
`hard_stopped` customer. The guard is the module; the transport is a detail.

Which kinds may reach which states (LAUNCH.md 3):

    report        weekly report            -> active only
    confirm       signup confirmation      -> active only
    results       results notes, win-back  -> active or soft_stopped
    operator      to the OPERATOR, not a customer (defect alerts) -> always

Every send is one `app_events` ledger row (`kind: send`), and every refusal is
one too (`kind: send_refused`) — the refusal of a hard-stopped customer is
logged as a defect, because reaching this module with one already means a
caller's guard is missing.

Transport is **Resend** (operator decision 2026-08-10): one HTTPS POST to
api.resend.com, authenticated by `RESEND_API_KEY` — the project's first
server-side secret. It is read at send time, never stored; without it, sending
fails loudly and the failure is in the ledger. Tests inject a fake transport
and never touch the network.
"""

import json
import os
import urllib.request
from datetime import datetime, timezone

import ledger
import subscriptions

API_URL = 'https://api.resend.com/emails'
KEY_ENV = 'RESEND_API_KEY'
FROM_ENV = 'TM_MAIL_FROM'      # e.g. "TenderMining <post@tendermining.de>"

# kind -> contact states it may reach. THE table of this module.
ALLOWED = {
    'report':   ('active',),
    'confirm':  ('active',),
    'results':  ('active', 'soft_stopped'),
}


class MailerError(RuntimeError):
    """A send that could not happen. The refusal reasons that matter are
    recorded in the ledger before this is raised."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _resend(payload):
    """The real transport: one POST. Kept tiny so the fake in tests is
    honestly equivalent."""
    key = os.environ.get(KEY_ENV)
    if not key:
        raise MailerError(f'{KEY_ENV} is not set — cannot send')
    req = urllib.request.Request(
        API_URL, data=json.dumps(payload).encode('utf-8'),
        headers={'Authorization': f'Bearer {key}',
                 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode('utf-8')).get('id')


def send(home, kind, sub_id, subject, html, to=None, transport=None):
    """Send one e-mail to one customer, if their contact_state allows `kind`.

    Returns the transport's message id. Raises MailerError on refusal or
    transport failure — callers show a page or log, they do not retry blind.
    `to` defaults to the customer's stored contact_email."""
    if kind == 'operator':
        # defect alerts to ourselves: no customer, no state, still ledgered
        mid = (transport or _resend)({'from': os.environ.get(FROM_ENV, ''),
                                      'to': [to], 'subject': subject,
                                      'html': html})
        ledger.append(home, 'app_events', [{
            'ts': _now(), 'kind': 'send', 'sub_id': 'operator',
            'detail': f'{subject!r} -> {to}'}])
        return mid
    if kind not in ALLOWED:
        raise MailerError(f'unknown mail kind {kind!r}; '
                          f'known: {", ".join(sorted(ALLOWED))} and operator')

    cust = subscriptions.customer_get(home, sub_id)
    state = (cust or {}).get('contact_state') or 'active'
    address = to or (cust or {}).get('contact_email')

    if cust is None or state not in ALLOWED[kind] or not address:
        why = ('no customer row' if cust is None else
               f'contact_state={state}' if state not in ALLOWED[kind] else
               'no address on file')
        ledger.append(home, 'app_events', [{
            'ts': _now(), 'kind': 'send_refused', 'sub_id': sub_id,
            'detail': f'{kind}: {why}'}])
        if state == 'hard_stopped':
            # Reaching the mailer with a hard-stopped customer IS the defect
            # this module exists to catch. The ledger row above is the alarm;
            # the exception makes sure the caller cannot mistake it for sent.
            raise MailerError(
                f'DEFECT: attempted {kind!r} to hard_stopped {sub_id} — '
                f'refused and ledgered')
        raise MailerError(f'{kind!r} to {sub_id} refused: {why}')

    mid = (transport or _resend)({
        'from': os.environ.get(FROM_ENV, ''),
        'to': [address], 'subject': subject, 'html': html})
    ledger.append(home, 'app_events', [{
        'ts': _now(), 'kind': 'send', 'sub_id': sub_id,
        'detail': f'{kind}: {subject!r}'}])
    return mid
