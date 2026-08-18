"""The operator's page — doc/ADMIN.md.

Search the awards store for a firm (by a trade word in its won lots, or by
name), see where it stands, invite it, enter an e-mail it gave us by phone,
stop it. This module owns the *search index*, the *status vocabulary* and the
*HTML*; `app.py` owns the routes and the guard. Nothing here writes storage
directly — `invite.py`, `subscriptions.py`, `tokens.py`, `ledger.py` do.

The index is the awards store joined to its lots' titles, cached per process
and rebuilt when the parquet's mtime moves — the pattern the recall box
already uses. Trades match on **title words**, never on CPV (house rule:
buyers file CPV wrongly).
"""

from datetime import date
from html import escape as esc
from pathlib import Path

import ledger
import subscriptions

LIMIT = 100                    # rows per search; beyond it: "mehr eingrenzen"
CHANNELS = ('linkedin', 'linkedin-ads', 'xing', 'phone', 'other')

_cache = {'mtime': None, 'firms': None}


# ------------------------------------------------------------------ the index

def _mtimes(data_dir):
    out = []
    for name in ('awards.parquet', 'tenders.parquet'):
        p = Path(data_dir) / 'store' / name
        out.append(p.stat().st_mtime if p.exists() else None)
    return tuple(out)


def index(data_dir):
    """{winner name: row} — one entry per exact winner spelling, its won
    lots' titles folded into a searchable haystack. Empty when the store is
    not there (a fresh deployment); never raises."""
    stamp = _mtimes(data_dir)
    if _cache['mtime'] == stamp and _cache['firms'] is not None:
        return _cache['firms']
    firms = {}
    try:
        import pandas as pd
        import outreach
        store = Path(data_dir) / 'store'
        ex = outreach.winner_rows(store)
        # a store without titles (an old rebuild, a test fixture) still
        # searches by name — the trade words are simply missing, and that is
        # visible in the results rather than fatal
        import pyarrow.parquet as pq
        have = set(pq.read_schema(store / 'tenders.parquet').names)
        cols = [c for c in ('procedure_id', 'lot_id', 'title') if c in have]
        by_lot = {}
        if 'title' in cols:
            titles = pd.read_parquet(store / 'tenders.parquet', columns=cols)
            by_lot = {(r.procedure_id, r.lot_id): (r.title or '')
                      for r in titles.itertuples(index=False)}
        for name, g in ex.groupby('company'):
            words = ' '.join(by_lot.get((p, l), '')
                             for p, l in zip(g['procedure_id'], g['lot_id']))
            sizes = g['winner_size'].dropna()
            firms[name] = {
                'company': name,
                'size': sizes.mode().iloc[0] if len(sizes) else 'unknown',
                'wins': int(len(g)),
                'single_bid_wins': int((g['n_tenders'] <= 1).sum()),
                'last_win': str(g['publication_date'].max())[:10],
                'haystack': f'{name} {words}'.casefold(),
            }
    except Exception as e:                                     # noqa: BLE001
        print(f'[admin] search index unavailable ({e})')
        firms = {}
    _cache.update(mtime=stamp, firms=firms)
    return firms


def search(data_dir, q, state, limit=LIMIT):
    """-> (rows, total). Firms whose name or won-lot titles contain `q`,
    customers first. An empty query lists the customers we already have, so
    the page is useful before anything is typed."""
    firms = index(data_dir)
    q = (q or '').strip().casefold()
    if not q:
        rows = [firms.get(name, {'company': name, 'size': '—', 'wins': 0,
                                 'single_bid_wins': 0, 'last_win': '—'})
                for name in sorted(set(state['name_of'].values()))]
    else:
        rows = [f for f in firms.values() if q in f['haystack']]
    total = len(rows)
    rows.sort(key=lambda f: (0 if state['sub_of'].get(f['company']) else 1,
                             -f.get('wins', 0), f['company']))
    return rows[:limit], total


# ------------------------------------------------------------- what we know

def state_of(home, today=None):
    """Everything the status column needs, read once per request: customers,
    their subscription versions, their events."""
    today = today or date.today().isoformat()
    customers, sub_of, name_of = {}, {}, {}
    try:
        import db
        con = db.connect(home, create=False)
        if con is not None:
            import json
            for r in con.execute('SELECT * FROM customer').fetchall():
                d = dict(r)
                d['contact_state'] = d.get('contact_state') or 'active'
                if isinstance(d.get('award_names'), str):
                    try:
                        d['award_names'] = json.loads(d['award_names'])
                    except ValueError:
                        d['award_names'] = []
                customers[d['customer_id']] = d
            con.close()
    except Exception as e:                                     # noqa: BLE001
        print(f'[admin] customers unavailable ({e})')
    versions = {}
    try:
        for r in subscriptions.read_all(home):
            versions.setdefault(r['sub_id'], []).append(r)
    except Exception as e:                                     # noqa: BLE001
        print(f'[admin] subscriptions unavailable ({e})')
    events = {}
    try:
        for e in ledger.read(home, 'app_events'):
            events.setdefault(e['sub_id'], []).append(e)
    except Exception as e:                                     # noqa: BLE001
        print(f'[admin] events unavailable ({e})')
    for sub_id, cust in customers.items():
        for spelling in list(cust.get('award_names') or []) + [cust.get('name')]:
            if spelling:
                sub_of[spelling] = sub_id
        name_of[sub_id] = cust.get('name') or sub_id
    return {'customers': customers, 'versions': versions, 'events': events,
            'sub_of': sub_of, 'name_of': name_of, 'today': today}


def status_of(state, company):
    """One firm's standing -> {sub_id, label, cls, email} (doc/ADMIN.md 3).
    Computed from the record on every request, never stored."""
    sub_id = state['sub_of'].get(company)
    if sub_id is None:
        return {'sub_id': None, 'label': 'nicht eingeladen', 'cls': 'st-none',
                'email': None}
    cust = state['customers'].get(sub_id, {})
    evs = state['events'].get(sub_id, [])
    kinds = {e['kind'] for e in evs}
    out = {'sub_id': sub_id, 'email': cust.get('contact_email')}

    def done(label, cls):
        return {**out, 'label': label, 'cls': cls}

    if cust.get('contact_state') == 'hard_stopped':
        return done('Widerspruch' if 'objection' in kinds else
                    'gestoppt (alles)', 'st-stop')
    if cust.get('contact_state') == 'soft_stopped':
        return done('gestoppt (Berichte)', 'st-stop')
    if not cust.get('consent_at'):
        invites = [e for e in evs if e['kind'] in ('invited', 'reissued')]
        sent = [e for e in evs if e['kind'] == 'invite_sent']
        if sent or invites:
            # Minting a link is not contacting anybody. Two words, because
            # the difference decides whether a silent firm was never written
            # to or wrote us off (operator, 2026-08-17).
            last = (sent or invites)[-1]
            chan = next((p.split('=', 1)[1]
                         for p in (last.get('detail') or '').split()
                         if p.startswith('channel=')), '')
            word = 'angeschrieben' if sent else 'Link erzeugt'
            cls = 'st-sent' if sent else 'st-inv'
            return done(f"{word}{' · ' + chan if chan else ''} · "
                        f"{str(last['ts'])[:10]}", cls)
        # No consent and no invitation, but a live subscription: the pilot
        # customers, whose reports are written to disk and read by us. Saying
        # "angelegt" would hide that they are being served.
        if any(r.get('active') for r in state['versions'].get(sub_id, [])):
            return done('aktiv · ohne Adresse', 'st-on')
        return done('angelegt', 'st-inv')
    st = subscriptions.trial_status(state['versions'].get(sub_id, []),
                                    state['today'])
    if st['plan'] == 'paid':
        return done('Kunde · bezahlt', 'st-paid')
    if not st['started']:
        return done('zurückgestellt', 'st-held')
    if 'ask' in kinds:
        return done('gefragt', 'st-ask')
    return done(f"angemeldet · Tag {st['day']} von {subscriptions.TRIAL_DAYS}",
                'st-on')


def counts(state):
    """The read-off line (ONBOARDING.md 6): one number per funnel stage."""
    out = {'Link erzeugt': 0, 'angeschrieben': 0, 'angemeldet': 0,
           'zurückgestellt': 0, 'gefragt': 0, 'ja': 0, 'gestoppt': 0}
    for sub_id, cust in state['customers'].items():
        evs = {e['kind'] for e in state['events'].get(sub_id, [])}
        versions = state['versions'].get(sub_id, [])
        if 'invited' in evs:
            out['Link erzeugt'] += 1
        if 'invite_sent' in evs:
            out['angeschrieben'] += 1
        if cust.get('consent_at'):
            out['angemeldet'] += 1
        if 'signup_held' in evs and not any(r.get('active') for r in versions):
            out['zurückgestellt'] += 1
        if 'ask' in evs:
            out['gefragt'] += 1
        if 'subscribe_yes' in evs:
            out['ja'] += 1
        if cust.get('contact_state') in ('soft_stopped', 'hard_stopped'):
            out['gestoppt'] += 1
    return out


# -------------------------------------------------------------------- the page

STYLE = """
<style>
  .adm td, .adm th { font-size: .93rem; vertical-align: top }
  .adm .firm { font-weight: 600 }
  .adm .st { white-space: nowrap; font-size: .85rem; padding: 2px 8px;
             border-radius: 10px; display: inline-block }
  .st-none { background: #eee; color: #555 }
  .st-inv  { background: #e6eff8; color: #24578c }
  .st-sent { background: #dbe9fb; color: #1a3f6b; font-weight: 600 }
  .st-on   { background: #e3f3e8; color: #1d6b39 }
  .st-paid { background: #d8efe0; color: #14532d; font-weight: 600 }
  .st-ask  { background: #fdf0d5; color: #8a5a00 }
  .st-held { background: #fde8e8; color: #8c2424 }
  .st-stop { background: #eee; color: #777; text-decoration: line-through }
  .adm form { display: inline; margin: 0 }
  .adm .act button { font-size: .85rem; padding: 3px 9px; margin: 1px 0 }
  .urlbox { background: #f4f7fa; border-left: 3px solid #6b93c0;
            padding: 10px 12px; margin: 1rem 0 }
  .urlbox input { width: 100%; font-family: monospace; font-size: .9rem;
                  padding: 6px }
  .err { background: #fde8e8; border-left: 3px solid #c44; padding: 10px 12px;
         margin: 1rem 0 }
  .ok  { background: #e6f4ea; border-left: 3px solid #2a7; padding: 10px 12px;
         margin: 1rem 0 }
  .counts { color: #555; font-size: .93rem }
</style>
"""


def _row_html(f, st, url_for):
    """One table row: the firm, what we know, what can be done to it."""
    sub_id, label = st['sub_id'], st['label']
    mail = st['email']
    masked = ('—' if not mail else
              f"{esc(mail.split('@')[0][:1])}…@{esc(mail.split('@')[-1])}")
    acts = []
    if sub_id is None:
        opts = ''.join(f'<option value="{c}">{c}</option>' for c in CHANNELS)
        acts.append(
            '<form method="post" action="/admin/invite">'
            f'<input type="hidden" name="company" value="{esc(f["company"])}">'
            f'<select name="channel">{opts}</select> '
            '<button type="submit">Einladen</button></form>')
    else:
        if not st['email']:
            acts.append(f'<a href="/admin/message?sub_id={esc(sub_id)}">'
                        '<button type="button">Nachricht</button></a>')
            if not st['label'].startswith('angeschrieben'):
                acts.append(
                    '<form method="post" action="/admin/sent">'
                    f'<input type="hidden" name="sub_id" value="{esc(sub_id)}">'
                    '<button type="submit" class="secondary">verschickt'
                    '</button></form>')
            acts.append('<form method="post" action="/admin/reissue">'
                        f'<input type="hidden" name="sub_id" value="{esc(sub_id)}">'
                        '<button type="submit" class="secondary">URL neu</button>'
                        '</form>')
        acts.append(f'<a href="/admin/email?sub_id={esc(sub_id)}">'
                    f'<button type="button">E-Mail eintragen</button></a>')
        if label.startswith('gestoppt') or label == 'Widerspruch':
            pass
        else:
            acts.append(f'<a href="/admin/stop?sub_id={esc(sub_id)}">'
                        '<button type="button" class="secondary">Abmelden'
                        '</button></a>')
    return (f'<tr><td class="firm">{esc(f["company"])}'
            f'<br><span class="muted">{esc(str(f.get("size") or ""))} · '
            f'{f.get("wins", 0)} Aufträge · {f.get("single_bid_wins", 0)} mit '
            f'einem Bieter · zuletzt {esc(str(f.get("last_win") or "—"))}'
            f'</span></td>'
            f'<td><span class="st {st["cls"]}">{esc(label)}</span></td>'
            f'<td>{masked}</td>'
            f'<td class="act">{" ".join(acts)}</td></tr>')


def list_html(data_dir, q, state, *, url=None, url_firm=None, error=None,
              note=None):
    """The whole page body: search field, counts, the table."""
    rows, total = search(data_dir, q, state)
    c = counts(state)
    parts = [STYLE, '<h1>Firmen</h1>',
             '<form method="get" action="/admin">'
             f'<input type="search" name="q" value="{esc(q or "")}" '
             'placeholder="Gewerk (z. B. blitzschutz) oder Firmenname" '
             'style="min-width:22em"> '
             '<button type="submit">Suchen</button></form>',
             '<p class="counts">' + ' · '.join(
                 f'{k}: <b>{v}</b>' for k, v in c.items()) + '</p>',
             '<p class="muted"><a href="/admin/experiments">Experimente</a>'
             ' — die laufenden A/B-Tests</p>']
    if error:
        parts.append(f'<div class="err">{esc(error)}</div>')
    if note:
        parts.append(f'<div class="ok">{esc(note)}</div>')
    if url:
        parts.append(
            '<div class="urlbox"><p style="margin:0 0 .4em"><b>Einladungslink '
            f'für {esc(url_firm or "")}</b> — steht auch in der '
            'fertigen Nachricht:</p>'
            f'<input type="text" readonly value="{esc(url)}" '
            'onclick="this.select()"></div>')
    if not rows:
        parts.append('<p class="muted">Keine Firma gefunden. Der Suchbegriff '
                     'muss im Firmennamen oder im Titel eines gewonnenen '
                     'Auftrags vorkommen.</p>')
        return '\n'.join(parts)
    body = ''.join(_row_html(f, status_of(state, f['company']), None)
                   for f in rows)
    parts.append('<table class="adm"><thead><tr><th>Firma</th><th>Status</th>'
                 '<th>E-Mail</th><th></th></tr></thead>'
                 f'<tbody>{body}</tbody></table>')
    if total > len(rows):
        parts.append(f'<p class="muted">{total} Treffer, {len(rows)} '
                     'angezeigt — bitte weiter eingrenzen.</p>')
    return '\n'.join(parts)
