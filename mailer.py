"""TenderMining guarded mailer — doc/APP.md 7.

One module sends every e-mail the product ever sends, and **the contact_state
check lives here**, not in calling code: a future bug must be unable to mail a
`hard_stopped` customer. The guard is the module; the transport is a detail.

Which kinds may reach which states (LAUNCH.md 3):

    report        weekly report            -> active only
    confirm       signup confirmation      -> active only
    results       results notes            -> active only
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

import html as _html
import json
import os
import urllib.request
from datetime import datetime, timezone

import ledger
import subscriptions
import tokens

API_URL = 'https://api.resend.com/emails'
KEY_ENV = 'RESEND_API_KEY'
FROM_ENV = 'TM_MAIL_FROM'      # e.g. "Murara <post@murara.eu>" — the sending
                               # domain must be verified at Resend first, or
                               # every send is refused by the API

# Where the token links point (doc/APP.md 2). One place for every module
# that writes a customer-facing URL — the report renderer, the footer,
# invite.py — so a domain change is one environment variable.
APP_URL_ENV = 'TM_APP_URL'
DEFAULT_APP_URL = 'https://app.murara.eu'


def app_url(base=None):
    return (base or os.environ.get(APP_URL_ENV) or DEFAULT_APP_URL).rstrip('/')


PRICE_ENV = 'TM_PRICE_LINE'
DEFAULT_PRICE_LINE = '79 € im Monat'   # operator, 2026-08-20 — provisional
                                       # while the pricing study runs


def price_line():
    """The subscription price as one German phrase — '79 € im Monat'.

    Asking someone to subscribe without naming the price is bait (operator,
    2026-08-20: "i assume i must show the price clearly"), so every ask —
    the report box, the invitation, the /y/ page — reads THIS function and
    there is deliberately no 'price to be announced' state anywhere. The
    default is provisional; `TM_PRICE_LINE` overrides it. Trade-dependent
    and per-customer offers are the operator's next decision and will plug
    in here, not in the texts."""
    return (os.environ.get(PRICE_ENV) or '').strip() or DEFAULT_PRICE_LINE


# kind -> contact states it may reach. THE table of this module.
ALLOWED = {
    'report':   ('active',),
    'confirm':  ('active',),
    'results':  ('active',),   # the soft state fell 2026-08-20; one stop, total
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
                 'Content-Type': 'application/json',
                 # Cloudflare in front of api.resend.com rejects urllib's
                 # default Python-urllib/3.x signature with 403 (error 1010,
                 # measured from the VPS 2026-08-20); any explicit product
                 # agent passes
                 'User-Agent': 'murara-mailer/1.0'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode('utf-8')).get('id')


def footer(home, sub_id, base_url=None):
    """What every customer e-mail ends with (doc/APP.md 8) -> (html, headers).

    The standing `s` and `c` tokens of this customer (minted on first ask,
    reused after), as the Abbestellen link, the recall link, and the Art. 21
    objection notice — visually separated from the content above. `headers`
    carries `List-Unsubscribe` (+ the one-click POST form of RFC 8058) so a
    mail client's own unsubscribe button lands on the same `/s/` page, where
    the app maps a header-driven visit to the HARD stop (LAUNCH.md 3: every
    ambiguous stop signal is hard). One function, called by everything that
    assembles a mail, so no mail can be built without it."""
    base = app_url(base_url)
    stop = f'{base}/s/{tokens.standing(home, "s", sub_id)}'
    recall = f'{base}/c/{tokens.standing(home, "c", sub_id)}'
    e = _html.escape
    # Two blocks, never one line (operator, 2026-08-17): the recall box is a
    # SERVICE and belongs to the content; Abbestellen is the objection and
    # belongs to the legal footer, "clearly separated from content"
    # (doc/APP.md 8). Side by side they read as one row of small print.
    html = (
        '<div style="margin-top:2em;padding:12px 14px;background:#f4f7fa;'
        'border-left:3px solid #6b93c0">'
        '<p style="margin:0;font-size:95%">'
        '<b>Haben wir eine Ausschreibung übersehen?</b><br>'
        'Wenn Sie eine Ausschreibung gefunden haben, die nicht in dieser '
        'Liste stand: '
        f'<a href="{e(recall)}">Nummer oder Link hier prüfen lassen</a> — '
        'wir sagen Ihnen, wie wir sie einschätzen.</p></div>'
        '<hr style="margin-top:2.5em;border:0;border-top:1px solid #ccc">'
        # a button, not a gray text line: the operator checked the
        # 2026-08-20 test mail and "Berichte abbestellen is still not clear
        # enough" — an unsubscribe a reader has to hunt for reads as one we
        # hid. Neutral gray, so it is findable without competing with the
        # subscribe ask.
        '<p style="font-size:85%;margin-bottom:.4em">'
        f'<a href="{e(stop)}" style="display:inline-block;padding:5px 12px;'
        'background:#667;border-radius:4px;color:#fff;text-decoration:none">'
        'E-Mails abbestellen</a></p>'
        '<p style="font-size:85%;color:#555;margin-top:0">Sie können der '
        'Verarbeitung Ihrer Daten für diese Berichte jederzeit widersprechen '
        '(Art. 21 DSGVO) — über den Link „E-Mails abbestellen" oder formlos '
        'per Antwort auf diese E-Mail. Der Widerspruch wirkt sofort und '
        'dauerhaft.</p>')
    headers = {'List-Unsubscribe': f'<{stop}>',
               'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click'}
    return html, headers


def send(home, kind, sub_id, subject, html, to=None, transport=None,
         headers=None):
    """Send one e-mail to one customer, if their contact_state allows `kind`.

    Returns the transport's message id. Raises MailerError on refusal or
    transport failure — callers show a page or log, they do not retry blind.
    `to` defaults to the customer's stored contact_email. `headers`: extra
    message headers (the footer's List-Unsubscribe pair)."""
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

    payload = {'from': os.environ.get(FROM_ENV, ''),
               'to': [address], 'subject': subject, 'html': html}
    if headers:
        payload['headers'] = dict(headers)
    try:
        mid = (transport or _resend)(payload)
    except Exception as e:                                     # noqa: BLE001
        # the transport, not the guard: no key, API down, domain unverified.
        # Ledgered so a Monday with nothing delivered is visible in the record,
        # not only in a log line.
        ledger.append(home, 'app_events', [{
            'ts': _now(), 'kind': 'send_failed', 'sub_id': sub_id,
            'detail': f'{kind}: {e}'}])
        raise MailerError(f'{kind!r} to {sub_id} failed: {e}') from e
    ledger.append(home, 'app_events', [{
        'ts': _now(), 'kind': 'send', 'sub_id': sub_id,
        'detail': f'{kind}: {subject!r}'}])
    return mid
