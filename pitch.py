"""The invitation message — doc/ONBOARDING.md 9.2a.

One firm, one message to paste into LinkedIn (or Xing, or read out on the
phone). Redrafted with the operator 2026-08-18, after reading the first one
as a recipient: it said neither who "wir" was nor what problem it solved, the
tenders were dead text, and the firm's own win proved nothing. Now: who we
are and the problem in two sentences; the trade's market figures from its
public page (`trade_pages.forecasts`); the forecast's edge in that trade,
**only where it beats guessing** — the operator writes to no other firms;
the live picks, each with its TED link; the invitation link; a signature.
"Wir" throughout, no person's name (we do not know it), no "ich".

The picks come from the same machinery a customer's Monday report uses: the
cycle's latest scored lots, the firm's draft subscription (written by
`invite.add` from its own won contracts), the relevance gate, and
`selection.for_sub`. A prospect therefore sees exactly what it would receive
as a customer — if the pick list is thin, that is the honest signal, and the
message says nothing rather than inventing something.
"""

from datetime import date

import ledger
import subscriptions

MAX_PICKS = 3
SHORT_LIMIT = 300          # LinkedIn's note on a connection request
TITLE_CHARS = 60


def _de(iso):
    """2026-09-12 -> 12.09.2026; anything unparseable stays as it is."""
    s = str(iso or '')[:10]
    try:
        d = date.fromisoformat(s)
    except ValueError:
        return s
    return f'{d.day:02d}.{d.month:02d}.{d.year}'


def _short(text, n=TITLE_CHARS):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= n else text[:n - 1].rstrip() + '…'


def draft_of(home, sub_id):
    """The newest subscription version of this firm, active or not — a
    prospect's draft never reaches `subscriptions.load`, which serves only
    live ones."""
    rows = [r for r in subscriptions.read_all(home) if r.get('sub_id') == sub_id]
    if not rows:
        return None
    return max(rows, key=lambda r: int(r.get('version') or 1))


def open_lots(home, today):
    """The cycle's current view of the open market: the last prediction per
    lot, deadline not yet passed. Awarded lots are dropped by the deadline
    filter in all but rare cases, and a teaser is not a report."""
    rows = []
    for row in ledger.prediction_latest_per_lot(home).values():
        # A REAL date, not merely a truthy value: a missing deadline arrives
        # as NaN, whose str() is 'nan' — and 'nan' > '2026-08-17' is True in
        # a string comparison, which put "Frist None" into the first message
        # this function ever produced. A lot we cannot date is a lot we
        # cannot invite anyone to.
        deadline = str(row.get('deadline_date') or '')[:10]
        try:
            date.fromisoformat(deadline)
        except ValueError:
            continue
        if deadline >= today:
            rows.append(row)
    return rows


def picks_for(home, sub, today, n=MAX_PICKS):
    """What this firm would be recommended today. Any failure of the gate
    means no picks — never a made-up list."""
    import selection
    profile, gate = None, None
    try:
        import relevance as rel
        if rel.wants_gate(sub):
            gate = rel.Gate(str(home), as_of=today)
            profile = rel.build_profile(gate, sub)
    except Exception as e:                                     # noqa: BLE001
        print(f'[pitch] gate unavailable ({e}) — market filter only')
        gate, profile = None, None
    sel = selection.for_sub(sub, open_lots(home, today), date.fromisoformat(today),
                            gate=gate, profile=profile)
    return sel.picks[:n] or sel.ranked[:n]


def own_win(home, company):
    """The firm's most quotable win: fewest bidders first, then most recent —
    with the lot's title, so the message can name it. None when the store
    cannot say."""
    try:
        import pandas as pd
        import outreach
        from pathlib import Path
        store = Path(home) / 'store'
        g = outreach.winner_rows(store)
        g = g[g['company'] == company]
        if not len(g):
            return None
        import pyarrow.parquet as pq
        have = set(pq.read_schema(store / 'tenders.parquet').names)
        cols = [c for c in ('procedure_id', 'lot_id', 'title', 'buyer_name')
                if c in have]
        titles = pd.read_parquet(store / 'tenders.parquet', columns=cols)
        by_lot = {(r.procedure_id, r.lot_id):
                  (getattr(r, 'title', None), getattr(r, 'buyer_name', None))
                  for r in titles.itertuples(index=False)}
        best = None
        for r in g.itertuples(index=False):
            bidders = (None if r.n_tenders != r.n_tenders else int(r.n_tenders))
            title, buyer = by_lot.get((r.procedure_id, r.lot_id), (None, None))
            if not title:
                continue
            key = (bidders if bidders is not None else 99,
                   -int(str(r.publication_date)[:10].replace('-', '')))
            if best is None or key < best[0]:
                best = (key, {'title': title, 'buyer': buyer,
                              'date': str(r.publication_date)[:10],
                              'bidders': bidders})
        return best[1] if best else None
    except Exception as e:                                     # noqa: BLE001
        print(f'[pitch] own win unavailable ({e})')
        return None


TED_URL = 'https://ted.europa.eu/de/notice/-/detail/{pn}'
SITE_URL = 'https://www.murara.eu'
SIGNATURE = 'Freundliche Grüße\nMurara · murara.eu'


def trade_of(home, company):
    """The firm's trade page and its verdict — (trade name, verdict) from
    the operator index (`admin.build_index` stores the firm's trade pages)
    and the last site build (`trade_pages.forecasts`). (None, {}) when the
    firm sits on no page or nothing has been built yet: the message then
    carries no market figures and no forecast claim, and says nothing false.
    """
    try:
        import admin
        import trade_pages
        firm = admin.index(home).get(company) or {}
        verdicts = trade_pages.forecasts(home)
        for t in firm.get('trades') or ():
            if t in verdicts:
                return t, verdicts[t]
    except Exception as e:                                     # noqa: BLE001
        print(f'[pitch] trade facts unavailable ({e})')
    return None, {}


def facts_block(trade, v):
    """The market figures of the firm's trade page, as three lines — the
    same numbers the page shows, quoted, with the page as the source."""
    f = v.get('figures') or {}
    if not f.get('per_month'):
        return []
    from trade_pages import money_de, pct_de
    lines = [f'Ein paar Zahlen zu Ihrem Markt, {trade} (Quelle: '
             f'EU-Vergaberegister TED, {f.get("months", "?")} Monate):',
             f'– {f["per_month"]:.0f} öffentliche Lose pro Monat'
             + (f', zusammen rund {money_de(f["year_scope"])} € im Jahr'
                if f.get('year_scope') else '')]
    if f.get('median_award'):
        lines.append(f'– ein Los ist im Mittel {money_de(f["median_award"])} '
                     f'€ wert')
    if f.get('low_bid') is not None:
        lines.append(f'– auf ein Los bewerben sich im Mittel '
                     f'{f.get("median_bidders", 0):.0f} Firmen – aber '
                     f'{pct_de(f["low_bid"])} der Lose bekommen höchstens ein '
                     f'Angebot')
    return lines


def edge_block(trade, v):
    """The forecast's edge in this trade — ONLY when it beats guessing on
    enough checked alarms (`trade_pages.level` state 'beats'). Any other
    state prints nothing: the operator does not write to those firms
    (2026-08-18), and a message must never carry a number the page would
    not."""
    if v.get('state') != 'beats' or not v.get('factor'):
        return []
    from trade_pages import factor_de, pct_de
    return [f'Diese Lose suchen wir – und wir prüfen, wie gut das klappt: '
            f'Jeder Hinweis wird gegen das später veröffentlichte Ergebnis '
            f'geprüft. Im Gewerk {trade} endeten von {v["checked"]} geprüften '
            f'Hinweisen {pct_de(v["precision"])} mit höchstens einem Angebot; '
            f'ohne Auswahl sind es {pct_de(v["base"])}. Also das '
            f'{factor_de(v["factor"])}-Fache.']


def message(home, sub_id, url, company=None, today=None):
    """-> {'short', 'long', 'picks', 'win', 'trade', 'edge'} — the two texts
    to paste (drafted with the operator 2026-08-18).

    `short` fits LinkedIn's 300-character connection note: who we work for,
    the problem (few bidders = high chance), one live tender, the ask. No
    link (a note with a URL reads as spam and the link is useless before the
    contact is accepted). `long` is the message after the contact: who we
    are and why we wrote, the trade's market figures from its public page,
    the forecast's edge when it has one, the picks each with its TED link,
    the invitation link, the legal line, a signature. "Wir" throughout; no
    person's name — we do not know it.
    """
    today = today or date.today().isoformat()
    sub = draft_of(home, sub_id)
    company = company or (sub or {}).get('name') or sub_id
    picks = picks_for(home, sub, today) if sub else []
    win = own_win(home, company)
    trade, v = trade_of(home, company)
    who = (f'Betriebe im Gewerk {trade}' if trade
           else 'Handwerks- und Baubetriebe')

    lines = ['Guten Tag,', '',
             f'danke für die Kontaktannahme. Worum es geht: Wir sind Murara. '
             f'Wir lesen jede Woche alle deutschen Ausschreibungen und suchen '
             f'für {who} die Lose heraus, bei denen voraussichtlich nur ein '
             f'oder zwei Firmen anbieten. Dort ist die Zuschlagschance am '
             f'höchsten – und man spart sich Angebote gegen zehn Wettbewerber. '
             f'Ihre Firma haben wir über Ihre Aufträge im EU-Vergaberegister '
             f'gefunden.', '']
    facts = facts_block(trade, v) if trade else []
    if facts:
        lines += facts + ['']
    edge = edge_block(trade, v) if trade else []
    if edge:
        lines += edge
    if facts or edge:
        lines += [f'Alle Zahlen und wie sie entstehen: '
                  f'{SITE_URL}/gewerke/{v.get("slug")}/', '']
    if picks:
        n = len(picks)
        word = {1: 'Ein Los, das', 2: 'Zwei Lose, die'}.get(n, 'Drei Lose, die')
        lines += [f'{word} gerade offen {"ist" if n == 1 else "sind"}:', '']
        for i, p in enumerate(picks, 1):
            lines.append(f'{i}. {_short(p.get("title"), 80)} – '
                         f'{_short(p.get("buyer_name"), 60)}, '
                         f'Frist {_de(p.get("deadline_date"))}')
            pn = p.get('publication_number')
            if pn:
                lines.append(f'   {TED_URL.format(pn=pn)}')
        lines.append('')
    lines += [f'Wenn Sie so eine Auswahl jeden Montag per E-Mail bekommen '
              f'möchten: {url} – E-Mail-Adresse eintragen, fertig. Vier '
              f'Wochen kostenlos, kein Konto, jederzeit abbestellbar.', '',
              'Woher wir Ihre Firmendaten haben und wie Sie widersprechen, '
              'steht dort unter „Datenschutz".', '',
              SIGNATURE]
    long = '\n'.join(lines)

    lead = (f'Guten Tag, wir suchen für {who} die öffentlichen Aufträge, bei '
            f'denen kaum jemand mitbietet – also hohe Zuschlagschance. ')
    ask = ('Dürfen wir Ihnen einmal pro Woche drei solche Ausschreibungen '
           'schicken? Kostenlos, ohne Konto.')
    example = ''
    if picks:
        p = picks[0]
        fixed = len('Aktuell z. B.: , , Frist 00.00. ')
        room = SHORT_LIMIT - len(lead) - len(ask) - fixed
        title = _short(p.get('title'), max(20, room * 2 // 3))
        buyer = _short(p.get('buyer_name'), max(12, room - len(title)))
        example = (f'Aktuell z. B.: {title}, {buyer}, '
                   f'Frist {_de(p.get("deadline_date"))[:6]}. ')
    short = lead + example + ask
    if len(short) > SHORT_LIMIT:
        short = short[:SHORT_LIMIT - 1].rstrip() + '…'
    return {'short': short, 'long': long, 'picks': picks, 'win': win,
            'trade': trade, 'edge': v}
