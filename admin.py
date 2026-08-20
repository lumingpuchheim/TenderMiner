"""The operator's page — doc/ADMIN.md.

Search the awards store for a firm (by its trade, or by name), see where it
stands, invite it, enter an e-mail it gave us by phone, stop it. This module
owns the *search index*, the *status vocabulary* and the *HTML*; `app.py`
owns the routes and the guard. Nothing here writes storage directly —
`invite.py`, `subscriptions.py`, `tokens.py`, `ledger.py` do.

A firm's trade is not asked here. It is `evidence.core_keywords` over the
firm's newest wins — the very lexicon `relevance.build_profile` gives the
gate, so the trade the operator reads off this page is the trade the reports
will treat as the firm's business (doc/ADMIN.md §4).

**A request never derives anything.** Deriving 15,000 trades takes minutes
on the server, and on 2026-08-18 the first `/admin` after a deploy paid it
in full — to list two customers. So the index is a FILE, `admin_index.json`,
written only by `build_index()` — from the cycle (`cycle.py`) and from every
deploy (`docker/deploy.sh`) — and a request only ever reads it: once per
process, ~15k small rows, well under a second. Missing file: the page still
opens instantly with the customers and name search, and says the trade
search is not ready yet. Trades match on **words**, never on CPV (house
rule: buyers file CPV wrongly).
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
INDEX_FILE = 'admin_index.json'

_cache = {'mtime': None, 'firms': None}


# ------------------------------------------------------------------ the index

def _mtimes(data_dir):
    out = []
    for name in ('awards.parquet', 'tenders.parquet'):
        p = Path(data_dir) / 'store' / name
        out.append(p.stat().st_mtime if p.exists() else None)
    return tuple(out)


def _rules_stamp():
    """What the trades were derived under: the gate's rule fingerprint plus
    the root vocabulary itself. The fingerprint covers every CORE_* knob
    (`evidence.RULES`) but not `cpv_trade_roots.txt`, and that file is edited
    by hand — a released change to it has to show on the page the day it
    ships, which the deploy-time build guarantees only if the stamp moves."""
    import evidence as evd
    return (evd.rules_fingerprint() + ':'
            + hashlib.sha256(evd.ROOTS_FILE.read_bytes()).hexdigest()[:10])


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


def _firm_rows(ex):
    """{winner name: the numbers the row prints} — from the awards store
    alone, no texts. Cheap (a groupby over the exploded awards) and the only
    thing an EMPTY query ever needs, so it is what `customers_only` reads
    when there is no index file yet."""
    firms = {}
    for name, g in ex.groupby('company'):
        sizes = g['winner_size'].dropna()
        firms[name] = {
            'company': name,
            'size': str(sizes.mode().iloc[0]) if len(sizes) else 'unknown',
            'wins': int(len(g)),
            'single_bid_wins': int((g['n_tenders'] <= 1).sum()),
            'last_win': str(g['publication_date'].max())[:10],
        }
    return firms


def build_index(data_dir, out=None):
    """Derive every winner's trade the way the gate derives a customer's
    and write the page's index file. THE ONLY WRITER. Called by the cycle
    and by the deploy; never by a request. -> (path, n_firms).

    The file holds exactly what a row prints — name, numbers, core roots
    with their evidence — and nothing a request would have to compute:
    no texts, no groupby. Written under the store's mtimes and the rules
    stamp, so a reader can tell a stale file from a current one; replaced
    by rename, because the app may be reading the old one at that moment.
    Runs in minutes over the server store, which is exactly why it lives
    here and not in `index()`."""
    import pandas as pd
    import pyarrow.parquet as pq
    import evidence as evd
    import outreach
    store = Path(data_dir) / 'store'
    out = Path(out) if out else Path(data_dir) / INDEX_FILE
    ex = outreach.winner_rows(store)
    firms = _firm_rows(ex)
    have = set(pq.read_schema(store / 'tenders.parquet').names)
    cols = [c for c in ('procedure_id', 'lot_id', 'title', 'description',
                        'publication_number') if c in have]
    by_lot = {}
    if 'title' in cols:
        lots = pd.read_parquet(store / 'tenders.parquet', columns=cols)
        d, p = 'description' in cols, 'publication_number' in cols
        by_lot = {(r.procedure_id, r.lot_id):
                  (r.title or '', (r.description or '') if d else '',
                   r.publication_number if p else None)
                  for r in lots.itertuples(index=False)}
        del lots
    import trade_pages
    trade_words = trade_pages.market.load_trades()
    for name, g in ex.groupby('company'):
        texts, titles, dates = _refs_of(g, by_lot, outreach.MAX_PROFILE_REFS)
        core = evd.core_keywords(texts, firm=name, titles=titles, dates=dates)
        counts = evd.root_share(texts)
        firms[name].update({
            'core': core,
            # the trade PAGES this firm belongs to (trades.txt names, by the
            # same title match that builds a page) — what the forecast
            # verdict is looked up by (doc/ADMIN.md 3b)
            'trades': [t for t, _ in
                       trade_pages.trades_of_titles(titles, trade_words)],
            # how many of those references carry the root — the numbers behind
            # CORE_SHARE, shown so a 1-of-6 trade cannot pass for a 6-of-6 one
            'counts': {r: int(counts.get(r, 0)) for r in core},
            # a root off the firm's OWN NAME is core unconditionally
            # (evidence.name_keywords) and recurs by definition, so it is not
            # a count and must not be printed as one
            'name_roots': evd.name_keywords(name),
            'refs': len(texts),
        })
    doc = {'stamp': list(_mtimes(data_dir)), 'rules': _rules_stamp(),
           'firms': list(firms.values())}
    tmp = out.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(doc, ensure_ascii=False, separators=(',', ':')),
                   encoding='utf-8')
    os.replace(tmp, out)
    return out, len(firms)


def index(data_dir):
    """{winner name: row} — the index FILE, read once per process and again
    when it changes on disk. Never derives: a missing or unreadable file is
    an empty index (the page then says the trade search is not ready), and a
    file written for another store or under other rules is served but
    reported as such through `index_state`, because a slightly old trade
    list beats none while the deploy that follows a store move rebuilds it.
    Never raises."""
    p = Path(data_dir) / INDEX_FILE
    try:
        mtime = p.stat().st_mtime
    except OSError:
        _cache.update(mtime=None, firms=None)
        return {}
    if _cache['mtime'] == mtime and _cache['firms'] is not None:
        return _cache['firms']
    firms = {}
    try:
        doc = json.loads(p.read_text(encoding='utf-8'))
        for f in doc['firms']:
            f['name_cf'] = f['company'].casefold()
            f.setdefault('core', [])
            f.setdefault('counts', {})
            f.setdefault('name_roots', [])
            f.setdefault('refs', 0)
            f.setdefault('trades', [])
            firms[f['company']] = f
        _cache.update(mtime=mtime, firms=firms,
                      stamp=doc.get('stamp'), rules=doc.get('rules'))
    except Exception as e:                                     # noqa: BLE001
        print(f'[admin] index file unreadable ({e})')
        _cache.update(mtime=None, firms=None)
    return firms


def index_state(data_dir):
    """One of 'missing', 'stale', 'current' — for the line under the search
    field. 'stale': the file exists but was built for another store or under
    other rules; served, and the next cycle or deploy replaces it."""
    if not index(data_dir):
        return 'missing'
    try:
        current = (_cache.get('stamp') == list(_mtimes(data_dir))
                   and _cache.get('rules') == _rules_stamp())
    except Exception:                                          # noqa: BLE001
        current = False
    return 'current' if current else 'stale'


def customers_only(data_dir, state):
    """The rows for an EMPTY query — the customers we already have, with
    their numbers. Straight from the awards store for those few names — a
    0.1 s read — and the index is used only if this process already holds
    it (then the rows carry the trade too). Never opens the index: the empty
    page is the one the operator lands on, and it can never be slow again."""
    names = sorted(set(state['name_of'].values()))
    firms = dict(_cache['firms'] or {})
    if not all(n in firms for n in names):
        try:
            import outreach
            ex = outreach.winner_rows(Path(data_dir) / 'store')
            firms.update(_firm_rows(ex[ex['company'].isin(names)]))
        except Exception as e:                                 # noqa: BLE001
            print(f'[admin] awards store unavailable ({e})')
    return [firms.get(name, {'company': name, 'size': '—', 'wins': 0,
                             'single_bid_wins': 0, 'last_win': '—'})
            for name in names]


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
    already have, so the page is useful before anything is typed — and does
    not open the index at all.

    The trade half is not a substring search over won-lot titles
    (doc/ADMIN.md §4): one lot of forty carrying the word made a general
    contractor an electrician, which is precisely the reading the delivery
    gate throws away. `q` becomes trade roots and the firms whose OWN trade
    recurs on one of them answer — strongest share first, so a 6-of-6
    electrician stands above a 1-of-6 one and the general contractor, whose
    trade this is not, does not appear at all."""
    q = (q or '').strip().casefold()
    if not q:
        rows, strength = customers_only(data_dir, state), {}
    else:
        firms = index(data_dir)
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
                    'gestoppt', 'st-stop')
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
        # Vormerken (doc/SALES.md 3): a watch-list row — profile written, no
        # link minted, nobody written to. The owner is part of the word
        # because the "Heute schreiben" mail is keyed by it.
        if 'vormerkt' in kinds:
            who = (cust.get('owner') or '').split('@')[0]
            return done(f'vorgemerkt{" · " + who if who else ""}', 'st-mark')
        return done('angelegt', 'st-inv')
    # the trial counts mails with recommendations (2026-08-20), so the row
    # shows how many of the free ones have gone out
    sent = sum(1 for e in state['events'].get(sub_id, [])
               if e['kind'] == 'send'
               and str(e.get('detail', '')).startswith('report:'))
    st = subscriptions.trial_status(state['versions'].get(sub_id, []),
                                    state['today'], sent_reports=sent)
    if st['plan'] == 'paid':
        return done('Kunde · bezahlt', 'st-paid')
    if not st['started']:
        return done('zurückgestellt', 'st-held')
    if 'ask' in kinds:
        return done('gefragt', 'st-ask')
    return done(f"angemeldet · Empfehlung {st['sent']} von "
                f'{subscriptions.FREE_REPORTS} kostenlos', 'st-on')


def counts(state):
    """The read-off line (ONBOARDING.md 6): one number per funnel stage."""
    out = {'vorgemerkt': 0, 'Link erzeugt': 0, 'angeschrieben': 0,
           'angemeldet': 0, 'zurückgestellt': 0, 'gefragt': 0, 'ja': 0,
           'gestoppt': 0}
    for sub_id, cust in state['customers'].items():
        evs = {e['kind'] for e in state['events'].get(sub_id, [])}
        versions = state['versions'].get(sub_id, [])
        if 'vormerkt' in evs and 'invited' not in evs:
            out['vorgemerkt'] += 1
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
        if cust.get('contact_state') == 'hard_stopped':
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
  .st-mark { background: #f1eaf8; color: #5b3a86 }
  .st-sent { background: #dbe9fb; color: #1a3f6b; font-weight: 600 }
  .st-on   { background: #e3f3e8; color: #1d6b39 }
  .st-paid { background: #d8efe0; color: #14532d; font-weight: 600 }
  .st-ask  { background: #fdf0d5; color: #8a5a00 }
  .st-held { background: #fde8e8; color: #8c2424 }
  .st-stop { background: #eee; color: #777; text-decoration: line-through }
  .adm form { display: inline; margin: 0 }
  .adm .act { white-space: nowrap }
  .adm .act button, .adm .act a.btn {
    font-size: .85rem; padding: 3px 9px; margin: 1px 0; line-height: 1.4;
    display: inline-block; background: var(--blue); color: #fff;
    border: 0; border-radius: 8px; text-decoration: none; font-weight: 550 }
  .adm .act a:not(.btn) { font-size: .85rem }
  .adm .act a.btn { margin-right: .7em }
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
  .adm .edge { font-size: .85rem; cursor: help }
  .edge-yes  { color: #1d6b39; font-weight: 600 }
  .edge-no   { color: #8c2424 }
  .edge-thin { color: #777 }
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


def edge_of(f, verdicts):
    """The forecast's edge for this firm — the verdict of the strongest trade
    page it belongs to (doc/ADMIN.md 3b). The operator writes only to firms
    whose trade shows an advantage over guessing (2026-08-18), so this is
    read before anything else on the row.

    -> {'state', 'trade', 'text', 'cls', ...level fields} — state as
    trade_pages.level: 'beats' · 'no_better' · 'thin' · 'none' — or
    'no_page' when none of the firm's trades has a page."""
    import trade_pages
    for trade in f.get('trades') or ():
        lv = verdicts.get(trade)
        if not lv:
            continue
        st = lv.get('state', 'none')
        if st == 'beats':
            text = (f'{trade_pages.factor_de(lv["factor"])}-fach — '
                    f'{trade_pages.pct_de(lv["precision"])} statt '
                    f'{trade_pages.pct_de(lv["base"])}, '
                    f'{lv["checked"]} geprüft')
            cls = 'edge-yes'
        elif st == 'no_better':
            text = (f'kein Vorsprung — {trade_pages.pct_de(lv["precision"])} '
                    f'statt {trade_pages.pct_de(lv["base"])}, '
                    f'{lv["checked"]} geprüft')
            cls = 'edge-no'
        elif st == 'thin':
            text = f'erst {lv["checked"]} geprüft, Quote ab 30'
            cls = 'edge-thin'
        else:
            text = 'kein Rücktest'
            cls = 'edge-thin'
        return {**lv, 'state': st, 'trade': trade, 'text': text, 'cls': cls}
    return {'state': 'no_page', 'trade': None, 'text': 'kein Gewerk mit Seite',
            'cls': 'edge-thin'}


def _edge_html(edge):
    t = edge.get('trade')
    label = f'{esc(t)}: ' if t else ''
    return (f'<span class="edge {edge["cls"]}" title="Rücktest-Vorsprung des '
            f'Gewerks (www.murara.eu/gewerke/{esc(edge.get("slug") or "")})">'
            f'{label}{esc(edge["text"])}</span>')


def _action(label, href, *, primary=False):
    """One row action, a GET link. `primary` draws it as the row's one filled
    button — the next funnel step; the rest are plain links (doc/ADMIN.md 3a)."""
    cls = ' class="btn"' if primary else ''
    return f'<a{cls} href="{href}">{esc(label)}</a>'


# doc/SALES.md 0: the salesman writes to small firms only. The register's
# own size band, as the index stores it per firm; anything else (medium,
# large, unknown) cannot be vormerkt and says so on the row.
SMALL = ('micro', 'small')


def is_small(f):
    return str((f or {}).get('size') or '').strip().lower() in SMALL


def row_actions(st):
    """Which actions a row offers, from its status alone: at most one primary
    (the next step of ONBOARDING §9's funnel), then quiet links. Everything
    the row does not offer — marking the message as sent, a new link — lives
    on the firm's message page, where the message is (doc/ADMIN.md 3a).
    Returns [(label, href, primary)]."""
    sub_id, label = st['sub_id'], st['label']
    if sub_id is None:
        return []
    q = f'sub_id={esc(sub_id)}'
    message = ('Nachricht anzeigen', f'/admin/message?{q}')
    stop = ('Stoppen', f'/admin/stop?{q}', False)
    if label.startswith('gestoppt') or label == 'Widerspruch':
        return []
    if label.startswith('vorgemerkt'):
        # on the watch list; the next step is the note, which the message
        # page builds (and mints the link for) when there is a lot to
        # promise — doc/SALES.md 6
        return [(*message, True), ('E-Mail eintragen', f'/admin/email?{q}',
                                   False), stop]
    if label.startswith('Link erzeugt'):
        return [(*message, True), ('E-Mail eintragen', f'/admin/email?{q}',
                                   False), stop]
    if label.startswith('angeschrieben'):
        return [('E-Mail eintragen', f'/admin/email?{q}', True),
                (*message, False), stop]
    if not st['email']:
        # served, but no address on file yet (the pilot rows)
        return [('E-Mail eintragen', f'/admin/email?{q}', True), stop]
    return [('E-Mail ändern', f'/admin/email?{q}', False), stop]


def _row_html(f, st, url_for, roots=(), verdicts=None):
    """One table row: the firm, what we know, what can be done to it."""
    edge = edge_of(f, verdicts or {})
    sub_id, label = st['sub_id'], st['label']
    mail = st['email']
    masked = ('—' if not mail else
              f"{esc(mail.split('@')[0][:1])}…@{esc(mail.split('@')[-1])}")
    if sub_id is None:
        opts = ''.join(f'<option value="{c}">{c}</option>' for c in CHANNELS)
        # Vormerken is the primary step for a small firm (doc/SALES.md 3):
        # profile now, link and note when a lot in its trade is flagged.
        # Einladen stays for the exception — and for a firm that is not
        # small, it is the only way in.
        if is_small(f):
            acts = ['<form method="post" action="/admin/vormerken">'
                    f'<input type="hidden" name="company" '
                    f'value="{esc(f["company"])}">'
                    '<button type="submit">Vormerken</button></form>']
        else:
            acts = ['<span class="muted" title="Die Vormerkliste ist für '
                    'kleine Betriebe (micro/small) — doc/SALES.md 0">'
                    'nicht klein</span>']
        acts.append(
            '<form method="post" action="/admin/invite">'
            f'<input type="hidden" name="company" value="{esc(f["company"])}">'
            f'<select name="channel">{opts}</select> '
            '<button type="submit" class="secondary">Einladen</button></form>')
    else:
        offered = row_actions(st)
        acts = [_action(l, h, primary=True) for l, h, p in offered if p]
        links = [_action(l, h) for l, h, p in offered if not p]
        if links:
            acts.append(' · '.join(links))
    return (f'<tr><td class="firm">{esc(f["company"])}'
            f'<br><span class="muted">{esc(str(f.get("size") or ""))} · '
            f'{f.get("wins", 0)} Aufträge · {f.get("single_bid_wins", 0)} mit '
            f'einem Bieter · zuletzt {esc(str(f.get("last_win") or "—"))}'
            f'</span><br>{_trade_html(f, roots)} · {_edge_html(edge)}</td>'
            f'<td><span class="st {st["cls"]}">{esc(label)}</span></td>'
            f'<td>{masked}</td>'
            f'<td class="act">{" ".join(acts)}</td></tr>')


def list_html(data_dir, q, state, *, url=None, url_firm=None, error=None,
              note=None, viewer=None, all_due=False):
    """The whole page body: search field, counts, the table. `viewer` is
    the salesman's address (from the basic-auth user); the „Heute
    schreiben" list is theirs alone unless `all_due`."""
    rows, total = search(data_dir, q, state)
    roots = query_roots(q)
    c = counts(state)
    parts = [STYLE, '<h1>Firmen</h1>',
             '<form method="get" action="/admin">'
             f'<input type="search" name="q" value="{esc(q or "")}" '
             'placeholder="Gewerk (z. B. blitzschutz) oder Firmenname" '
             'style="min-width:22em"> '
             '<button type="submit">Suchen</button></form>']
    ready = index_state(data_dir) if (q or '').strip() else None
    if ready == 'missing':
        # a request never builds the index (module docstring); until the
        # cycle or a deploy has written it, say so instead of searching
        # nothing silently
        parts.append('<p class="why">Die Suche über alle Firmen ist noch '
                     'nicht bereit — der Index wird vom nächsten Lauf oder '
                     'Deploy geschrieben (<code>python admin.py --build</code>'
                     '). Bis dahin findet die Suche nichts; ohne Suchbegriff '
                     'stehen die Kunden hier.</p>')
    elif (q or '').strip():
        # what was actually searched for. The operator types a word; the page
        # says which trade that word is, and that the answer is the firm's
        # recurring trade rather than any lot it once happened to win
        parts.append('<p class="why">' + (
            f'Gewerk <b>{esc(" · ".join(roots))}</b> — Firmen, deren '
            'wiederkehrendes Gewerk das ist (dieselbe Ableitung wie in den '
            'Berichten), stärkste zuerst; dazu Namenstreffer.'
            if roots else
            f'„{esc(q)}" ist kein Gewerkswort — gesucht wird nur im '
            'Firmennamen.')
            + (' <span class="muted">(Index von einem älteren Stand; der '
               'nächste Lauf erneuert ihn.)</span>' if ready == 'stale' else '')
            + '</p>')
    parts += ['<p class="counts">' + ' · '.join(
                 f'{k}: <b>{v}</b>' for k, v in c.items()) + '</p>',
             '<p class="muted"><a href="/admin/experiments">Experimente</a>'
             ' — die laufenden A/B-Tests</p>']
    # „Heute schreiben" (doc/SALES.md 5): the same list the salesman's mail
    # carries, so the mail is one way in and not the only one. Computed per
    # request — it is a handful of watched firms against this week's picks,
    # and a stored copy would be wrong the moment a deadline passes.
    try:
        import sales
        rows_due = sales.due(data_dir, state['today'])
        mine = ([r for r in rows_due if r['owner'] == viewer]
                if viewer and not all_due else rows_due)
        others = len(rows_due) - len(mine)
        if mine or others:
            tail = ''
            if others and not all_due:
                tail = (f'<p class="muted"><a href="/admin?alle=1">'
                        f'{others} weitere bei anderen anzeigen</a></p>')
            whose = (f' — Liste von {esc(viewer)}' if viewer and not all_due
                     else ' — alle Listen')
            parts.append(
                f'<h2 style="margin-bottom:.2rem">Heute schreiben{whose}</h2>'
                '<p class="muted" style="margin-top:0">Vorgemerkte Firmen, '
                'für die gerade eine Ausschreibung mit wenig Wettbewerb im '
                'eigenen Gewerk offen ist.</p><ul class="plain">'
                + ''.join(sales.line_html(r, '') for r in mine) + '</ul>'
                + tail)
    except Exception as e:                                     # noqa: BLE001
        print(f'[admin] due list unavailable ({e})')
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
    import trade_pages
    verdicts = trade_pages.forecasts(data_dir)
    body = ''.join(_row_html(f, status_of(state, f['company']), None, roots,
                             verdicts)
                   for f in rows)
    parts.append('<table class="adm"><thead><tr><th>Firma / Gewerk</th>'
                 '<th>Status</th><th>E-Mail</th><th></th></tr></thead>'
                 f'<tbody>{body}</tbody></table>')
    if total > len(rows):
        parts.append(f'<p class="muted">{total} Treffer, {len(rows)} '
                     'angezeigt — bitte weiter eingrenzen.</p>')
    return '\n'.join(parts)


# ------------------------------------------------------------- the builder

def main():
    """`python admin.py --build` — write the index file. What the deploy runs
    (docker/deploy.sh build_site) and what the cycle runs at its end; also
    the thing to run by hand when the page says the index is missing."""
    import argparse
    import time
    import config
    ap = argparse.ArgumentParser(description="the operator page's index")
    ap.add_argument('--data-dir', default=config.data_root())
    ap.add_argument('--build', action='store_true',
                    help="derive every winner's trade and write "
                         f'<data-dir>/{INDEX_FILE}')
    args = ap.parse_args()
    if not args.build:
        ap.error('nothing to do without --build')
    t0 = time.time()
    path, n = build_index(args.data_dir)
    print(f'[admin] index built: {n} firms -> {path} in {time.time() - t0:.0f}s')


if __name__ == '__main__':
    main()
