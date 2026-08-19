"""The salesman's side — doc/SALES.md.

Two things live here, and neither of them writes to a prospect:

1. **The watch list's owner.** A firm the salesman marks with *Vormerken*
   carries their address (`customer.owner`); everything below is keyed by
   it. Who that is comes from the environment, not from a login: the app
   has no accounts, the operator page is behind the edge's basic auth, and
   one address is the whole configuration today.

2. **Who is due today** (§4) and **the mail that says so** (§5) — added in
   the next step of the build order.

The customer's own mail (`delivering.py`) is not touched by anything here.
Two e-mails, two audiences: the report goes to a subscribed customer when
there is something to report; the "Heute schreiben" mail goes to the
salesman when a prospect is worth writing to.
"""

import os
from datetime import date, timedelta

# doc/SALES.md 4, the trigger's two numbers. MIN_DAYS is the deadline a lot
# must still have when the FIRST message goes out, so that a reply two or
# three days later still finds it open — and the second message can deliver
# what the first promised. REST_DAYS is how long a firm that was written to
# is left alone: LinkedIn will not take a second connection note before the
# first is withdrawn, and three days of silence is already a no (operator).
MIN_DAYS = 10
REST_DAYS = 21
MIN_LOTS = 1              # "start with one" (operator, 2026-08-18)

# The owners, as `user=mail,user=mail` — the edge's basic-auth user mapped to
# an address. One entry today; the format exists so a second salesperson is a
# configuration change rather than a code change (doc/SALES.md 3).
OWNERS_ENV = 'TM_SALES_OWNERS'
DEFAULT_OWNER_ENV = 'TM_SALES_OWNER'


def owners():
    """{basic-auth user: address} from TM_SALES_OWNERS, possibly empty."""
    out = {}
    for part in (os.environ.get(OWNERS_ENV) or '').split(','):
        part = part.strip()
        if '=' in part:
            user, mail = part.split('=', 1)
            if user.strip() and mail.strip():
                out[user.strip()] = mail.strip()
    return out


def owner_for(user):
    """The address a press by basic-auth `user` files a firm under
    (doc/SALES.md 3a). The user's own entry in TM_SALES_OWNERS wins; with
    no match, `default_owner` — which is the single configured address, or
    None when there are several and the press cannot be attributed (the
    caller then refuses rather than guessing)."""
    user = (user or '').strip()
    known = owners()
    if user and user in known:
        return known[user]
    return default_owner()


def default_owner():
    """The address a Vormerken is filed under when the request cannot say
    who pressed it — which is every request today: the app sees only the
    edge's "this passed basic auth" header, never the user name. Explicit
    TM_SALES_OWNER wins; otherwise the single configured owner; otherwise
    None, and the row is simply unwatched (no mail, no crash)."""
    one = (os.environ.get(DEFAULT_OWNER_ENV) or '').strip()
    if one:
        return one
    known = list(owners().values())
    return known[0] if len(known) == 1 else None


# --------------------------------------------------------------- who is due

def candidates(home, sub, trade, today, trades=None):
    """The lots that would make a first message worth sending to this firm:
    what it would be recommended today (`pitch.picks_for` — the same gate,
    the same selection the message will show), reduced to

      * the competition flag (a pick already carries it),
      * a deadline at least MIN_DAYS away, and
      * a title that belongs to the firm's MAIN trade.

    The last one is the operator's complaint of 2026-08-18: an Elektro firm
    was offered a Blitzschutz lot because a side root matched. A note has
    one line; that line must be in the trade the reader calls his own.
    Nearest deadline first — that is the one the note names.
    """
    import market
    import pitch
    if not sub or not trade:
        return []
    words = (trades if trades is not None else market.load_trades()).get(trade)
    if not words:
        return []
    cut = (date.fromisoformat(today) + timedelta(days=MIN_DAYS)).isoformat()
    out = []
    for p in pitch.picks_for(home, sub, today, n=50):
        if not p.get('flag'):
            continue
        if str(p.get('deadline_date') or '')[:10] < cut:
            continue
        title = market.ev.fold(str(p.get('title') or '').casefold())
        if not any(w in title for w in words['terms']):
            continue
        if any(x in title for x in words['exclude']):
            continue
        out.append(p)
    return sorted(out, key=lambda p: str(p.get('deadline_date'))[:10])


def written_recently(events, today):
    """True when this firm was written to inside the rest window — silence
    is a no, but a second note is not possible (or polite) before then."""
    edge = (date.fromisoformat(today) - timedelta(days=REST_DAYS)).isoformat()
    return any(e['kind'] == 'invite_sent' and str(e['ts'])[:10] >= edge
               for e in events or ())


def due(home, today=None, owner=None):
    """Whom to write to today (doc/SALES.md 4) -> a list of dicts, computed,
    never stored. A firm qualifies when it is on someone's watch list, is
    still a prospect, was not written to inside the rest window, and has at
    least MIN_LOTS candidate lots in its main trade. Sorted by the strongest
    signal: most lots first, then the nearest deadline."""
    import admin
    import market
    import pitch
    import trade_pages
    today = today or date.today().isoformat()
    state = admin.state_of(home, today)
    index = admin.index(home)
    verdicts = trade_pages.forecasts(home)
    trades = market.load_trades()
    out = []
    for sub_id, cust in state['customers'].items():
        who = (cust.get('owner') or '').strip()
        if not who or (owner and who != owner):
            continue
        if cust.get('consent_at'):                  # a customer, not a prospect
            continue
        if cust.get('contact_state') in ('soft_stopped', 'hard_stopped'):
            continue
        if written_recently(state['events'].get(sub_id), today):
            continue
        firm = index.get(cust.get('name') or '') or {}
        if not admin.is_small(firm):
            continue
        trade = next(iter(firm.get('trades') or ()), None)
        sub = pitch.draft_of(home, sub_id)
        lots = candidates(home, sub, trade, today, trades)
        if len(lots) < MIN_LOTS:
            continue
        out.append({
            'sub_id': sub_id, 'company': cust.get('name') or sub_id,
            'size': firm.get('size') or '—', 'trade': trade,
            'edge': admin.edge_of(firm, verdicts),
            'n_lots': len(lots),
            'next_deadline': str(lots[0].get('deadline_date'))[:10],
            'owner': who,
        })
    return sorted(out, key=lambda r: (-r['n_lots'], r['next_deadline']))


# ------------------------------------------------------- the salesman's mail

def _de(iso):
    s = str(iso or '')[:10]
    return f'{s[8:10]}.{s[5:7]}.' if len(s) == 10 else s


def line_html(r, base):
    """One firm in the mail and in the page's „Heute schreiben" section —
    the same line, because the mail is a pointer to the page, not a second
    view of the data."""
    from html import escape as esc
    edge = r.get('edge') or {}
    verdict = (f' <span style="color:#1d6b39">({esc(edge.get("text", ""))})'
               f'</span>' if edge.get('state') == 'beats' else '')
    lots = f'{r["n_lots"]} Los' + ('' if r['n_lots'] == 1 else 'e')
    return (f'<li><a href="{base}/admin/message?sub_id={esc(r["sub_id"])}">'
            f'<b>{esc(r["company"])}</b></a> · {esc(r["size"])} · '
            f'{esc(r.get("trade") or "ohne Gewerk")}{verdict} · '
            f'{lots} mit wenigen Bietern erwartet, nächste Frist '
            f'{_de(r["next_deadline"])}</li>')


def mail_html(rows, base, watched=0):
    n = len(rows)
    rest = max(0, watched - n)
    return (
        f'<p>Für {n} vorgemerkte Firma{"" if n == 1 else "n"} gibt es gerade '
        f'eine Ausschreibung mit wenig Wettbewerb im eigenen Gewerk — '
        f'{"sie ist" if n == 1 else "sie sind"} den ersten Kontakt wert. '
        f'Ein Klick öffnet die fertigen Texte.</p>'
        f'<ul>{"".join(line_html(r, base) for r in rows)}</ul>'
        + (f'<p style="color:#5b6472;font-size:.9rem">{rest} weitere '
           f'vorgemerkte Firmen haben diese Woche nichts Passendes offen; '
           f'sie erscheinen wieder, sobald sich das ändert.</p>'
           if rest else ''))


def run(home, today=None, transport=None, dry_run=False):
    """One mail per owner whose list is non-empty (doc/SALES.md 5) -> the
    rows that were mailed. Never writes to a prospect: this is the second
    kind of e-mail, the one that goes to the salesman.

    A failure to send is printed and ledgered by the mailer, never raised
    into the cycle: the mail is a convenience, the page has the same list.
    """
    import mailer
    from collections import defaultdict
    today = today or date.today().isoformat()
    rows = due(home, today)
    by_owner = defaultdict(list)
    for r in rows:
        by_owner[r['owner']].append(r)
    watched = _watched_per_owner(home)
    base = mailer.app_url()
    for who, mine in sorted(by_owner.items()):
        n = len(mine)
        subject = f'Heute schreiben: {n} Firma' + ('' if n == 1 else 'n')
        html = mail_html(mine, base, watched.get(who, n))
        if dry_run:
            print(f'[sales] {who}: {subject}')
            for r in mine:
                print(f'          {r["company"]} · {r["trade"]} · '
                      f'{r["n_lots"]} Lose, Frist {r["next_deadline"]}')
            continue
        try:
            mailer.send(home, 'operator', 'sales', subject, html, to=who,
                        transport=transport)
            _ledger(home, 'sales_mail', detail=f'{who}: '
                    + ', '.join(r['sub_id'] for r in mine))
        except Exception as e:                                 # noqa: BLE001
            print(f'[sales] mail to {who} failed ({e}) — the list is on /admin')
    if not rows:
        print('[sales] nobody is due today — no mail')
    return rows


def _watched_per_owner(home):
    """{owner: how many prospects they watch} — the mail's footer counts the
    ones that were not due, so a short list never looks like a broken list."""
    import admin
    out = {}
    for cust in admin.state_of(home)['customers'].values():
        who = (cust.get('owner') or '').strip()
        if who and not cust.get('consent_at'):
            out[who] = out.get(who, 0) + 1
    return out


def _ledger(home, kind, detail=''):
    import ledger
    from datetime import datetime, timezone
    ledger.append(home, 'app_events', [{
        'ts': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'kind': kind, 'sub_id': 'sales', 'detail': detail}])


def main():
    import argparse
    import config
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data-dir', default=config.data_root())
    ap.add_argument('--today', default=None)
    ap.add_argument('--dry-run', action='store_true',
                    help='print the mail that would go out, send nothing')
    a = ap.parse_args()
    rows = run(a.data_dir, a.today, dry_run=a.dry_run)
    print(f'[sales] {len(rows)} firm(s) due')


if __name__ == '__main__':
    main()
