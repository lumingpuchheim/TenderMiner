"""TenderMining capability tokens — doc/APP.md 3.

The app has no login, no accounts and no sessions. A token in a URL path *is*
the authorisation, and this module is the only thing that mints or judges one.
Same shape as the other storage modules: every function takes the **data
directory**, never a path into storage (CLAUDE.md), and the `token` table
belongs to this module alone.

Four purposes, and a handler accepts exactly its own:

    t  signup      one per target-list firm, printed as a QR code on a letter
    f  feedback    one lot x one verdict x one customer
    s  stop        standing, per customer
    c  recall      standing, per customer
    y  subscribe   standing, per customer — the yes-link that never expires

**Purpose-binding is the security property**, not a tidiness rule. Without it a
feedback link — which is printed in every report and travels through mail
scanners, forwarding rules and helpdesk inboxes — would be a working stop link
and a working signup link for the same customer.

`resolve` answers `None` for every kind of failure: never existed, wrong
purpose, revoked. A caller therefore cannot tell those apart, and neither can
anyone probing the app (doc/APP.md 2: "identical for 'never existed' and
'revoked' (no oracle)").

Values are 32 URL-safe characters from `secrets.token_urlsafe(24)` — 192 bits,
comfortably past the 128 the spec asks for. That randomness is the real defence
against enumeration; the rate limit in front of it is a lazy brake.
"""

import secrets
from datetime import datetime, timezone

import db

PURPOSES = {'t': 'signup', 'f': 'feedback', 's': 'stop', 'c': 'recall',
            'y': 'subscribe'}

# Bytes of entropy per token. 24 bytes -> 192 bits -> 32 characters.
ENTROPY_BYTES = 24

# How much of a token may ever be written down (doc/APP.md 3: "tokens never
# appear in full in any log — first 8 characters only").
LOG_CHARS = 8


class TokenError(ValueError):
    """A token request that cannot mean what it says."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def short(value):
    """The only form of a token that may be logged, printed in an error, or
    put in a traceback. Use it everywhere, including in your own debugging —
    a token pasted into a log once is a token that lives in the log forever."""
    if not value:
        return '<none>'
    return f'{str(value)[:LOG_CHARS]}…'


def mint(data_dir, purpose, sub_id, *, procedure_id=None, lot_id=None,
         verdict=None, now=None):
    """A new token of `purpose` for `sub_id`. Returns the value — the only
    time this module ever hands out a full token."""
    if purpose not in PURPOSES:
        raise TokenError(f'unknown purpose {purpose!r}; '
                         f'known: {", ".join(sorted(PURPOSES))}')
    if not sub_id:
        raise TokenError('a token without a subject authorises nothing')
    if purpose == 'f' and not (procedure_id and lot_id and verdict):
        # Refused rather than defaulted: an `f` token whose verdict silently
        # became None would record a customer's click as an opinion nobody
        # holds, and the click is not repeatable to correct it.
        raise TokenError('an f token needs procedure_id, lot_id and verdict — '
                         'the lot and the verdict live in the row, never in '
                         'the URL')
    if purpose != 'f' and (procedure_id or lot_id or verdict):
        raise TokenError(f'{purpose!r} tokens carry no lot or verdict')
    value = secrets.token_urlsafe(ENTROPY_BYTES)
    con = db.connect(data_dir)
    with con:
        con.execute(
            'INSERT INTO token (token, purpose, sub_id, procedure_id, lot_id,'
            ' verdict, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (value, purpose, sub_id, procedure_id, lot_id, verdict,
             now or _now()))
    con.close()
    return value


def standing(data_dir, purpose, sub_id, now=None):
    """The customer's live `s` or `c` token, minted on first ask and reused
    after. These two are standing links (doc/APP.md 3) — they appear in the
    footer of every report, so a fresh one per report would leave a trail of
    equally valid stop links that can never be revoked as a set."""
    if purpose not in ('s', 'c', 'y'):
        raise TokenError(f'only s, c and y tokens are standing, not {purpose!r}')
    con = db.connect(data_dir)
    row = con.execute(
        'SELECT token FROM token WHERE sub_id = ? AND purpose = ?'
        ' AND revoked_at IS NULL ORDER BY created_at LIMIT 1',
        (sub_id, purpose)).fetchone()
    con.close()
    if row:
        return row['token']
    return mint(data_dir, purpose, sub_id, now=now)


def resolve(data_dir, purpose, value):
    """The token's row if it is live and of exactly this purpose, else None.

    One `None` for four different failures, on purpose. The caller renders the
    same neutral page either way, so the app never confirms that a token once
    existed."""
    if not value or purpose not in PURPOSES:
        return None
    con = db.connect(data_dir, create=False)
    if con is None:          # no database yet: nothing can be authorised
        return None
    row = con.execute('SELECT * FROM token WHERE token = ?',
                      (value,)).fetchone()
    con.close()
    if row is None or row['purpose'] != purpose or row['revoked_at']:
        return None
    return dict(row)


def mark_used(data_dir, value, now=None):
    """Stamp first use. Not a consumption: `used_at` is a record, and which
    purposes may be used twice is the handler's call (doc/APP.md 3 — a click on
    a superseded report is harmless and allowed). Only the first use is kept,
    so `used_at` always answers "when did this leave the desk"."""
    con = db.connect(data_dir)
    with con:
        con.execute('UPDATE token SET used_at = ? WHERE token = ?'
                    ' AND used_at IS NULL', (now or _now(), value))
    con.close()


def revoke(data_dir, value, now=None):
    """Effective immediately, by definition (doc/APP.md 3). Idempotent: a
    second revocation keeps the first timestamp, because when a link stopped
    working is a fact worth not overwriting."""
    con = db.connect(data_dir)
    with con:
        con.execute('UPDATE token SET revoked_at = ? WHERE token = ?'
                    ' AND revoked_at IS NULL', (now or _now(), value))
    con.close()


def revoke_all(data_dir, sub_id, now=None):
    """Every live token a customer holds. This is what a hard stop needs: not
    just "stop sending", but "the links already in their inbox stop working"."""
    con = db.connect(data_dir)
    with con:
        cur = con.execute('UPDATE token SET revoked_at = ? WHERE sub_id = ?'
                          ' AND revoked_at IS NULL', (now or _now(), sub_id))
        n = cur.rowcount
    con.close()
    return n
