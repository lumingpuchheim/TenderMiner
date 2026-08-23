"""invite.py — the front of the funnel: a target-list firm becomes an invitation.

doc/ONBOARDING.md 9.2. Console tool; prints, writes no report files. Storage
only through subscriptions.py, tokens.py and ledger.py (CLAUDE.md).

    python invite.py add "Jens Dunkel Glas- und Bauelemente GmbH" [--channel linkedin]
    python invite.py reissue jens-dunkel-glas-und-bauelemente-gmbh
    python invite.py objection "Jens Dunkel Glas- und Bauelemente GmbH"

`add` computes the firm's row from the store itself — `outreach.firm`: the
awards and tenders parquet, the embedding sidecar for the contract-notice
refs, the firm's own award notices for its contact details. No
file to prepare, nothing to copy to the server. It writes the customer row (name, the exact winner spelling as
award_names, the postal contact as contact_note), appends subscription
version 1 with `active: false` — the DRAFT the app's signup handler
pre-flights and activates — mints one `t` token and prints the QR URL. The
URL is printed exactly once: a token is never read back out of storage, only
minted (`reissue` revokes and mints anew).

`objection` is the Art. 21 flag: contact_state = hard_stopped, every token
revoked, one `objection` event. It works for a firm that was never invited —
the row is created hard-stopped, so a later `batch` cannot pick it up.

Draft knobs follow the live customers rather than the target row's CPV3
codes: `cpv_prefixes ['45']` (the whole construction range) and the relevance
gate at 0.7 do the narrowing, because buyers enter CPV wrongly and the gate
reads the lot text; `nuts_prefixes` come from the firm's won regions.
"""

import argparse
import re
import sys
import unicodedata
from datetime import datetime, timezone

import config
import ledger
import subscriptions
import tokens
from mailer import APP_URL_ENV, DEFAULT_APP_URL, app_url

MIN_REFS = 2

# The draft version's knobs, copied from the live customers (2026-08-17).
DRAFT_KNOBS = {'cpv_prefixes': ['45'], 'min_deadline_days': 0,
               'max_picks': 5, 'min_relevance': 0.7}


class InviteError(ValueError):
    """An invitation that must not be written."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _event(data_dir, kind, sub_id, **fields):
    ledger.append(data_dir, 'app_events',
                  [{'ts': _now(), 'kind': kind, 'sub_id': sub_id, **fields}])


def slug(name):
    """'Jens Dunkel Glas- und Bauelemente GmbH' -> 'jens-dunkel-glas-und-bauelemente-gmbh'."""
    s = unicodedata.normalize('NFKD', name)
    s = s.replace('ß', 'ss')
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')
    if not s:
        raise InviteError(f'no slug can be made from {name!r}')
    return s


# ------------------------------------------------------------- the target row

def target_row(data_dir, company):
    """outreach.firm, with its refusal re-raised as ours. Imported lazily:
    the store libraries are heavy and `objection` never needs them."""
    import outreach
    try:
        return outreach.firm(data_dir, company)
    except outreach.OutreachError as e:
        raise InviteError(str(e)) from None


# ------------------------------------------------------------ what exists now

def _known_names(data_dir):
    """Every winner spelling already claimed by a customer, from the draft
    and later versions' award_names and name. An invitation to a firm that is
    already one — under any spelling — is a double letter, and refused."""
    claimed = {}
    for r in subscriptions.read_all(data_dir):
        for n in (r.get('award_names') or []) + [r.get('name')]:
            if n:
                claimed[n] = r['sub_id']
    return claimed


def _versions_of(data_dir, sub_id):
    return [r for r in subscriptions.read_all(data_dir)
            if r.get('sub_id') == sub_id]


def _resolve(data_dir, key):
    """A sub_id or a company spelling -> sub_id (customer or target)."""
    if subscriptions.customer_get(data_dir, key) or _versions_of(data_dir, key):
        return key
    claimed = _known_names(data_dir)
    if key in claimed:
        return claimed[key]
    return slug(key)


# ------------------------------------------------------------------- commands

CHANNELS = ('linkedin', 'linkedin-ads', 'xing', 'phone', 'other')


def add(data_dir, company, *, sub_id=None, also_names=(), batch=None,
        channel='linkedin', base_url=None, now=None, mint=True, owner=None):
    """-> (sub_id, url). Raises InviteError rather than writing a half
    invitation; every check runs before the first write.

    `mint=False` writes the customer row and the draft subscription but no
    token, and returns `(sub_id, None)`: that is **Vormerken**
    (doc/SALES.md 3) — the firm is on a salesman's watch list, nobody has
    been written to, and no link exists to leak. The link is minted later,
    when the message page is first opened. `owner` is the watching
    salesman's address, and is what the "Heute schreiben" mail is keyed by.
    """
    if channel not in CHANNELS:
        raise InviteError(f'channel {channel!r} is not one of {CHANNELS}')
    row = target_row(data_dir, company)
    name = row['company']
    sub_id = sub_id or slug(name)
    # Every spelling TED published for this company, plus anything the
    # salesman added by hand. Before firms.py these had to be typed in as
    # --also-name or the customer's own wins under the other spelling were
    # never learned (feedback.wins_of).
    names = [name] + [n for n in list(row.get('spellings') or [])
                      + list(also_names) if n and n != name]
    names = list(dict.fromkeys(names))

    refs = list(row.get('profile_refs') or [])
    if len(refs) < MIN_REFS:
        raise InviteError(f'{name!r} has {len(refs)} usable profile ref(s); '
                          f'{MIN_REFS} are the minimum for a profile '
                          f'(ONBOARDING.md 1.1)')
    claimed = _known_names(data_dir)
    for n in names:
        if n in claimed:
            raise InviteError(f'{n!r} already belongs to customer '
                              f'{claimed[n]!r} — no second invitation')
    if _versions_of(data_dir, sub_id):
        raise InviteError(f'sub_id {sub_id!r} already has subscription '
                          f'versions; pass --sub-id for a different key')
    cust = subscriptions.customer_get(data_dir, sub_id)
    if cust and cust.get('contact_state') == 'hard_stopped':
        raise InviteError(f'{sub_id!r} is hard_stopped (objection on file) — '
                          f'not invited, ever')

    contact = ', '.join(x for x in (row.get('postal_zone'), row.get('city'))
                        if x)
    note = f'target list {row.get("last_win") or ""}: {contact}'.strip(': ')
    subscriptions.customer_update(data_dir, sub_id, name=name,
                                  award_names=names, contact_note=note,
                                  **({'owner': owner} if owner else {}))
    subscriptions.append_version(data_dir, {
        'sub_id': sub_id, 'version': 1, 'active': False,
        'name': name, 'award_names': names,
        'nuts_prefixes': list(row.get('regions') or []) or None,
        'profile_refs': refs,
        **DRAFT_KNOBS})
    if not mint:
        _event(data_dir, 'vormerkt', sub_id,
               detail=f'owner={owner or "-"} batch={batch or "-"}')
        return sub_id, None
    value = tokens.mint(data_dir, 't', sub_id, now=now)
    _event(data_dir, 'invited', sub_id,
           detail=f'channel={channel} batch={batch or "-"} '
                  f'token={tokens.short(value)}')
    return sub_id, f'{app_url(base_url)}/t/{value}'


def reissue(data_dir, key, *, base_url=None, now=None):
    """A fresh QR URL for a firm invited but not yet signed up. Every earlier
    token is revoked first — the old letter, if it turns up, is dead."""
    sub_id = _resolve(data_dir, key)
    if not _versions_of(data_dir, sub_id):
        raise InviteError(f'{key!r} was never invited — use add')
    cust = subscriptions.customer_get(data_dir, sub_id) or {}
    if cust.get('consent_at'):
        raise InviteError(f'{sub_id!r} has already signed up '
                          f'({cust["consent_at"][:10]}); nothing to reissue')
    if cust.get('contact_state') == 'hard_stopped':
        raise InviteError(f'{sub_id!r} is hard_stopped — no reissue')
    n = tokens.revoke_all(data_dir, sub_id, now=now)
    value = tokens.mint(data_dir, 't', sub_id, now=now)
    _event(data_dir, 'reissued', sub_id,
           detail=f'revoked={n} token={tokens.short(value)}')
    return sub_id, f'{app_url(base_url)}/t/{value}'


def objection(data_dir, key, *, note=None, now=None):
    """Art. 21: honoured immediately, forever, no reasons asked. -> sub_id."""
    sub_id = _resolve(data_dir, key)
    fields = {'contact_state': 'hard_stopped'}
    if not subscriptions.customer_get(data_dir, sub_id):
        # never invited: create the row so a batch can never pick the firm up
        fields.update(name=key, award_names=[key])
    if note:
        fields['contact_note'] = note
    subscriptions.customer_update(data_dir, sub_id, **fields)
    n = tokens.revoke_all(data_dir, sub_id, now=now)
    _event(data_dir, 'objection', sub_id,
           detail=f'revoked={n}' + (f' {note}' if note else ''))
    return sub_id


# ----------------------------------------------------------------- console

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--data-dir', default=None)
    ap.add_argument('--url', default=None,
                    help=f'app base URL (default ${APP_URL_ENV} or '
                         f'{DEFAULT_APP_URL})')
    sub = ap.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('add', help='invite one target-list firm')
    a.add_argument('company')
    a.add_argument('--sub-id')
    a.add_argument('--also-name', action='append', default=[],
                   help='another winner spelling that is this firm')
    a.add_argument('--batch', help='batch label, e.g. 2026-08-24-452')
    a.add_argument('--channel', default='linkedin', choices=CHANNELS,
                   help='how the URL travels (GO_TO_MARKET.md, channel '
                        'decision revised 2026-08-17); default linkedin')
    r = sub.add_parser('reissue', help='revoke and mint a new QR URL')
    r.add_argument('key', help='sub_id or company')
    o = sub.add_parser('objection', help='Art. 21: hard stop, forever')
    o.add_argument('key', help='sub_id or company')
    o.add_argument('--note')
    args = ap.parse_args(argv)
    data_dir = str(config.data_root(args.data_dir))
    try:
        if args.cmd == 'add':
            sub_id, url = add(data_dir, args.company, sub_id=args.sub_id,
                              also_names=args.also_name, batch=args.batch,
                              channel=args.channel, base_url=args.url)
            print(f'{sub_id}\n{url}')
        elif args.cmd == 'reissue':
            sub_id, url = reissue(data_dir, args.key, base_url=args.url)
            print(f'{sub_id}\n{url}')
        else:
            sub_id = objection(data_dir, args.key, note=args.note)
            print(f'{sub_id}: hard_stopped, tokens revoked')
    except InviteError as e:
        print(f'[invite] {e}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
