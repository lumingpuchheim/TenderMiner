"""The operator's page — doc/ADMIN.md.

Search the awards store for a firm (by its trade, or by name), see where it
stands, invite it, enter an e-mail it gave us by phone, stop it. This module
owns the *search index*, the *status vocabulary* and the *HTML*; `app.py`
owns the routes and the guard. Nothing here writes storage directly —
`invite.py`, `subscriptions.py`, `tokens.py`, `ledger.py` do.

The index is the awards store joined to its lots' texts, cached per process
and rebuilt when the parquet's mtime moves — the pattern the recall box
already uses. Trades match on **words**, never on CPV (house rule: buyers
file CPV wrongly).

A firm's trade is not asked here. It is `evidence.core_keywords` over the
firm's newest wins — the very lexicon `relevance.build_profile` gives the
gate, so the trade the operator reads off this page is the trade the reports
will treat as the firm's business. See doc/ADMIN.md §4 for why the earlier
raw substring over every won title was a different question with the same
spelling.
"""

import hashlib
import json
import os
from datetime import date
from html import escape as esc
from pathlib import Path

import ledger
import subscriptions

LIMIT = 100                    # rows per search; beyond it: "mehr eingrenzen"
CHANNELS = ('linkedin', 'linkedin-ads', 'xing', 'phone', 'other')
SHOW_ROOTS = 5                 # core roots printed on a row; the rest on hover
CORES_CACHE = 'admin_cores.json'

_cache = {'mtime': None, 'firms': None}


# ------------------------------------------------------------------ the index

def _mtimes(data_dir):
    out = []
    for name in ('awards.parquet', 'tenders.parquet'):
        p = Path(data_dir) / 'store' / name
        out.append(p.stat().st_mtime if p.exists() else None)
    return tuple(out)


def _refs_of(g, by_lot, max_refs):
    """One firm's newest wins as the gate reads them — (texts, titles, dates),
    newest first, one entry per contract notice, capped at `max_refs`.

    The same window `outreach.refs_for` hands `relevance.build_profile`:
    several won lots of one procedure were announced by one notice and count
    once, and a win whose notice is not in the tender store yields nothing.
    `evidence.leistung_text` is what makes the body half the Leistung section
    rather than the project prose — the gate's own reading of a lot."""
    import evidence as evd
    gs = g.sort_values('publication_date', ascending=False)
    texts, titles, dates, seen = [], [], [], set()
    for p, l, d in zip(gs['procedure_id'], gs['lot_id'], gs['publication_date']):
        row = by_lot.get((p, l))
        if row is None:
            continue
        key = row[2] or (p, l)         # a store without publication numbers
        if key in seen:                # (an old rebuild) dedupes per lot
            continue
        seen.add(key)
        texts.append((evd.leistung_text(row[0], row[1]), None))
        titles.append(row[0])
        dates.append(str(d)[:10])
        if len(texts) >= max_refs:
            break
    return texts, titles, dates


def _cores_held(data_dir):
    """The cached trades, or None when there are none for this store under
    these rules. Read before the lot texts are, because a hit means they never
    have to be loaded at all — that is the difference between a 3-second page
    and a 0.3-second one."""
    try:
        held = json.loads((Path(data_dir) / CORES_CACHE)
                          .read_text(encoding='utf-8'))
        if (held.get('stamp') == list(_mtimes(data_dir))
                and held.get('rules') == _rules_stamp()):
            return held['firms']
    except Exception:                                          # noqa: BLE001
        pass
    return None


def _cores(data_dir, ex, by_lot):
    """{winner name: {core, counts, name_roots, refs}} — every firm's trade,
    derived exactly as the delivery gate derives its customers'.

    Cached to `<data>/admin_cores.json` under the store's mtimes, because it
    is the one expensive thing on this page: ~30 s over 5.5k winners, against
    0.1 s for everything else. The cycle warms it (`loop.py`), so the operator
    normally reads a file. A stale or unreadable cache is recomputed, never
    served: the trade is what the page is for."""
    import evidence as evd
    import outreach
    cache = Path(data_dir) / CORES_CACHE
    stamp, rules = list(_mtimes(data_dir)), _rules_stamp()
    firms = {}
    for name, g in ex.groupby('company'):
        texts, titles, dates = _refs_of(g, by_lot, outreach.MAX_PROFILE_REFS)
        core = evd.core_keywords(texts, firm=name, titles=titles, dates=dates)
        counts = evd.root_share(texts)
        firms[name] = {
            'core': core,
            # how many of those references carry the root — the numbers behind
            # CORE_SHARE, shown so a 1-of-6 trade cannot pass for a 6-of-6 one
            'counts': {r: int(counts.get(r, 0)) for r in core},
            # a root off the firm's OWN NAME is core unconditionally
            # (evidence.name_keywords) and recurs by definition, so it is not
            # a count and must not be printed as one
            'name_roots': evd.name_keywords(name),
            'refs': len(texts),
        }
    try:
        tmp = cache.with_suffix('.json.tmp')
        tmp.write_text(json.dumps({'stamp': stamp, 'rules': rules,
                                   'firms': firms}), encoding='utf-8')
        os.replace(tmp, cache)          # the app and the cycle both write it
    except Exception as e:                                     # noqa: BLE001
        print(f'[admin] core-root cache not written ({e})')
    return firms


def _rules_stamp():
    """What the cached trades were derived under: the gate's rule fingerprint
    plus the root vocabulary itself. The fingerprint covers every CORE_* knob
    (`evidence.RULES`) but not `cpv_trade_roots.txt`, and that file is edited
    by hand — a released change to it has to invalidate the cache on the day
    it ships, not on the next store rebuild."""
    import evidence as evd
    return (evd.rules_fingerprint() + ':'
            + hashlib.sha256(evd.ROOTS_FILE.read_bytes()).hexdigest()[:10])


def index(data_dir):
    """{winner name: row} — one entry per exact winner spelling, with the
    trade its wins recur on. Empty when the store is not there (a fresh
    deployment); never raises."""
    stamp = _mtimes(data_dir)
    if _cache['mtime'] == stamp and _cache['firms'] is not None:
        return _cache['firms']
    firms = {}
    try:
        import pandas as pd
        import outreach
        store = Path(data_dir) / 'store'
        ex = outreach.winner_rows(store)
        cores = _cores_held(data_dir)
        if cores is None:
            # a store without lot texts (an old rebuild, a test fixture) still
            # searches by name — the trade is simply missing, and that is
            # visible in the results rather than fatal
            import pyarrow.parquet as pq
            have = set(pq.read_schema(store / 'tenders.parquet').names)
            cols = [c for c in ('procedure_id', 'lot_id', 'title',
                                'description', 'publication_number')
                    if c in have]
            by_lot = {}
            if 'title' in cols:
                lots = pd.read_parquet(store / 'tenders.parquet', columns=cols)
                d, p = 'description' in cols, 'publication_number' in cols
                by_lot = {(r.procedure_id, r.lot_id):
                          (r.title or '', (r.description or '') if d else '',
                           r.publication_number if p else None)
                          for r in lots.itertuples(index=False)}
                del lots
            cores = _cores(data_dir, ex, by_lot) if by_lot else {}
        for name, g in ex.groupby('company'):
            sizes = g['winner_size'].dropna()
            firms[name] = {
                'company': name,
                'size': sizes.mode().iloc[0] if len(sizes) else 'unknown',
                'wins': int(len(g)),
                'single_bid_wins': int((g['n_tenders'] <= 1).sum()),
                'last_win': str(g['publication_date'].max())[:10],
                'name_cf': name.casefold(),
                **cores.get(name, {'core': [], 'counts': {},
                                   'name_roots': [], 'refs': 0}),
            }
    except Exception as e:                                     # noqa: BLE001
        print(f'[admin] search index unavailable ({e})')
        firms = {}
    _cache.update(mtime=stamp, firms=firms)
    return firms


def query_roots(q):
    """The trade roots a search term names, in the gate's own vocabulary:
    "Elektroinstallation" -> ['elektro']. A word the reviewed root list does
    not know returns nothing, and the search then only reads names."""
    try:
        import evidence as evd
        return sorted({r for w in evd.tokens(str(q or '').casefold())
                       for r in evd.roots_in(w)})
    except Exception as e:                                     # noqa: BLE001
        print(f'[admin] trade vocabulary unavailable ({e})')
        return []


def trade_strength(f, roots):
    """How firmly a firm answers a trade query: the largest share of its
    references carrying one of `roots`, and how many that was.

    A root off the firm's own name scores 1.0 — it is on every reference the
    firm has ever had, which is exactly why `core_keywords` admits it without
    counting. -> (share, count) or None when the firm's trade is not this."""
    best = None
    for r in roots:
        if r not in f.get('core', ()):
            continue
        n = f.get('refs') or 0
        if r in f.get('name_roots', ()):
            hit = (1.0, n)
        else:
            c = f.get('counts', {}).get(r, 0)
            hit = (c / n if n else 0.0, c)
        best = hit if best is None else max(best, hit)
    return best


def search(data_dir, q, state, limit=LIMIT):
    """-> (rows, total). Firms whose **trade** is what `q` names, or whose
    name contains it, customers first. An empty query lists the customers we
    already have, so the page is useful before anything is typed.

    The trade half is not a substring search over won-lot titles any more
    (doc/ADMIN.md §4): one lot of forty carrying the word made a general
    contractor an electrician, which is precisely the reading the delivery
    gate throws away. `q` becomes trade roots and the firms whose OWN trade
    recurs on one of them answer — strongest share first, so a 6-of-6
    electrician stands above a 1-of-6 one and the general contractor, whose
    trade this is not, does not appear at all."""
    firms = index(data_dir)
    q = (q or '').strip().casefold()
    if not q:
        rows = [firms.get(name, {'company': name, 'size': '—', 'wins': 0,
                                 'single_bid_wins': 0, 'last_win': '—'})
                for name in sorted(set(state['name_of'].values()))]
        strength = {}
    else:
        roots = query_roots(q)
        rows, strength = [], {}
        for f in firms.values():
            hit = trade_strength(f, roots) if roots else None
            if hit is None and q not in f['name_cf']:
                continue
            rows.append(f)
            strength[f['company']] = hit or (0.0, 0)
    total = len(rows)
    rows.sort(key=lambda f: (0 if state['sub_of'].get(f['company']) else 1,
                             -strength.get(f['company'], (0.0, 0))[0],
                             -strength.get(f['company'], (0.0, 0))[1],
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
  .adm .trade { font-size: .85rem; color: #24578c; cursor: help }
  .adm .trade.none { color: #999; font-style: italic }
  .why { color: #555; font-size: .9rem; margin: .3rem 0 1rem }
</style>
"""


def _trade_html(f, roots=()):
    """The firm's trade, as the reports will read it: its core roots, the
    matched one first. The hover text carries the evidence — how many of the
    references the gate builds a profile from carry each root."""
    core = list(f.get('core') or ())
    if not core:
        return ('<span class="trade none" title="Keine wiederkehrenden '
                'Gewerkswörter in den letzten Aufträgen — diese Firma '
                'erscheint unter keinem Gewerk">ohne Gewerk</span>')
    core.sort(key=lambda r: r not in (roots or ()))
    n = f.get('refs') or 0
    why = ', '.join(
        f'{r}: Firmenname' if r in (f.get('name_roots') or ())
        else f'{r}: {f.get("counts", {}).get(r, 0)} von {n} Referenzen'
        for r in core)
    shown = ' · '.join(
        f'<b>{esc(r)}</b>' if r in (roots or ()) else esc(r)
        for r in core[:SHOW_ROOTS])
    if len(core) > SHOW_ROOTS:
        shown += f' +{len(core) - SHOW_ROOTS}'
    return f'<span class="trade" title="{esc(why)}">{shown}</span>'


def _row_html(f, st, url_for, roots=()):
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
            f'</span><br>{_trade_html(f, roots)}</td>'
            f'<td><span class="st {st["cls"]}">{esc(label)}</span></td>'
            f'<td>{masked}</td>'
            f'<td class="act">{" ".join(acts)}</td></tr>')


def list_html(data_dir, q, state, *, url=None, url_firm=None, error=None,
              note=None):
    """The whole page body: search field, counts, the table."""
    rows, total = search(data_dir, q, state)
    roots = query_roots(q)
    c = counts(state)
    parts = [STYLE, '<h1>Firmen</h1>',
             '<form method="get" action="/admin">'
             f'<input type="search" name="q" value="{esc(q or "")}" '
             'placeholder="Gewerk (z. B. blitzschutz) oder Firmenname" '
             'style="min-width:22em"> '
             '<button type="submit">Suchen</button></form>']
    if (q or '').strip():
        # what was actually searched for. The operator types a word; the page
        # says which trade that word is, and that the answer is the firm's
        # recurring trade rather than any lot it once happened to win
        parts.append('<p class="why">' + (
            f'Gewerk <b>{esc(" · ".join(roots))}</b> — Firmen, deren '
            'wiederkehrendes Gewerk das ist (dieselbe Ableitung wie in den '
            'Berichten), stärkste zuerst; dazu Namenstreffer.'
            if roots else
            f'„{esc(q)}" ist kein Gewerkswort — gesucht wird nur im '
            'Firmennamen.') + '</p>')
    parts += ['<p class="counts">' + ' · '.join(
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
                     'muss ein Gewerk nennen, das die Aufträge einer Firma '
                     'wiederholt tragen, oder im Firmennamen vorkommen.</p>')
        return '\n'.join(parts)
    body = ''.join(_row_html(f, status_of(state, f['company']), None, roots)
                   for f in rows)
    parts.append('<table class="adm"><thead><tr><th>Firma / Gewerk</th>'
                 '<th>Status</th><th>E-Mail</th><th></th></tr></thead>'
                 f'<tbody>{body}</tbody></table>')
    if total > len(rows):
        parts.append(f'<p class="muted">{total} Treffer, {len(rows)} '
                     'angezeigt — bitte weiter eingrenzen.</p>')
    return '\n'.join(parts)
