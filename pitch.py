"""The invitation message — doc/ONBOARDING.md 9.2a.

One firm, one message to paste into LinkedIn (or Xing, or read out on the
phone). It leads with **live tenders picked for that firm** and its own win,
because the offer is the product working, not a description of it (operator,
2026-08-17): no "wir könnten", no "unser Service bietet" — three open lots
with deadlines, and the link.

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


def message(home, sub_id, url, company=None, today=None):
    """-> {'short', 'long', 'picks', 'win'} — the two texts to paste.

    `short` fits LinkedIn's 300-character connection note and carries one
    concrete tender, no link (a note with a URL reads as spam and the link is
    useless before the contact is accepted). `long` is the message after the
    contact: the picks, the firm's own win, the link.
    """
    today = today or date.today().isoformat()
    sub = draft_of(home, sub_id)
    company = company or (sub or {}).get('name') or sub_id
    picks = picks_for(home, sub, today) if sub else []
    win = own_win(home, company)

    lines = ['Guten Tag,', '']
    if picks:
        lines.append('offen in Ihrem Gewerk, mit wenigen Bietern zu rechnen:')
        lines.append('')
        for p in picks:
            lines.append(f"• {_short(p.get('title'), 70)} — "
                         f"{_short(p.get('buyer_name'), 40)}, "
                         f"Frist {_de(p.get('deadline_date'))}")
        lines.append('')
    if win:
        bidders = win.get('bidders')
        how = (f' — bei {bidders} Bieter{"n" if bidders != 1 else ""}'
               if bidders else '')
        lines.append(f'Ihren Auftrag „{_short(win["title"])}" '
                     f'({_short(win.get("buyer"), 40)}, {_de(win["date"])}) '
                     f'haben Sie gewonnen{how}. Genau solche Lose suchen wir '
                     f'für Sie — wöchentlich, aus allen deutschen '
                     f'Bekanntmachungen.')
    else:
        lines.append('Wir lesen wöchentlich alle deutschen Bauausschreibungen '
                     'und melden die, die zu Ihrem Betrieb passen und bei '
                     'denen wenig Wettbewerb zu erwarten ist.')
    lines += ['',
              f'Vier Wochen kostenlos, kein Konto, eine Zeile Anmeldung: {url}',
              '',
              'Woher wir Ihre Daten haben und wie Sie widersprechen, steht '
              'dort unter „Datenschutz".']
    long = '\n'.join(lines)

    if picks:
        p = picks[0]
        short = (f"Guten Tag, aktuell offen für Ihr Gewerk: "
                 f"„{_short(p.get('title'), 55)}\" ({_short(p.get('buyer_name'), 30)}, "
                 f"Frist {_de(p.get('deadline_date'))}) — wir erwarten dort "
                 f"wenige Bieter. Solche Lose suchen wir wöchentlich; die "
                 f"Liste ist kostenlos. Darf ich sie Ihnen schicken?")
    elif win:
        short = (f"Guten Tag, Sie haben „{_short(win['title'], 55)}\" "
                 f"gewonnen — wir suchen wöchentlich Ausschreibungen mit "
                 f"wenig Wettbewerb in Ihrem Gewerk. Die Liste ist kostenlos. "
                 f"Darf ich sie Ihnen schicken?")
    else:
        short = ('Guten Tag, wir lesen wöchentlich alle deutschen '
                 'Bauausschreibungen und melden die, die zu Ihrem Betrieb '
                 'passen und bei denen wenig Wettbewerb zu erwarten ist. '
                 'Kostenlos zum Kennenlernen — darf ich Ihnen die Liste '
                 'schicken?')
    if len(short) > SHORT_LIMIT:
        short = short[:SHORT_LIMIT - 1].rstrip() + '…'
    return {'short': short, 'long': long, 'picks': picks, 'win': win}
