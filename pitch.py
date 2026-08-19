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


def _buyer_head(name):
    """'Landeshauptstadt Hannover - Fachbereich Gebäudemanagement' ->
    'Landeshauptstadt Hannover': the department after the dash is what a
    300-character note cannot afford, and the reader knows the city."""
    s = ' '.join(str(name or '').split())
    for sep in (' - ', ' – ', ', '):
        if sep in s and len(s.split(sep, 1)[0]) >= 8:
            return s.split(sep, 1)[0]
    return s


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
    """The firm's trade page and its verdict, and the overall record —
    (trade name, verdict, overall) from the operator index
    (`admin.build_index` stores the firm's trade pages) and the last site
    build (`trade_pages.forecasts`; the overall under `trade_pages.ALL`).
    (None, {}, {}) when nothing has been built yet: the message then carries
    no market figures and no forecast claim, and says nothing false.
    """
    try:
        import admin
        import trade_pages
        firm = admin.index(home).get(company) or {}
        verdicts = trade_pages.forecasts(home)
        overall = verdicts.get(trade_pages.ALL) or {}
        for t in firm.get('trades') or ():
            if t in verdicts:
                return t, verdicts[t], overall
        # The MAIN trade comes from the index, and stands on its own: a
        # trade with no page yet (too few awarded lots, or no site build)
        # still decides which lots may be offered and what the note calls
        # the reader's trade. Without this the whole sales trigger went
        # silent on a fresh checkout — no verdict, no trade, no candidates.
        first = next(iter(firm.get('trades') or ()), None)
        return first, {}, overall
    except Exception as e:                                     # noqa: BLE001
        print(f'[pitch] trade facts unavailable ({e})')
    return None, {}, {}


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


def edge_block(trade, v, overall=None):
    """What we are good at, in numbers: the overall record first (all
    trades, the one figure with a real sample), then the trade's own line —
    each ONLY when it beats guessing on enough checked alarms
    (`trade_pages.level` state 'beats'). A trade without an edge prints
    nothing of its own; the operator does not write to those firms
    (2026-08-18), and a message never carries a number the page would not.
    """
    from trade_pages import factor_de, pct_de

    def beats(lv):
        return lv and lv.get('state') == 'beats' and lv.get('factor')

    if not beats(overall) and not beats(v):
        return []

    def n_de(n):
        return f'{n:,}'.replace(',', '.')          # 1042 -> 1.042

    # Every number is defined in the sentence that uses it (operator,
    # 2026-08-18: "the numbers make no sense unless you explain them"):
    # what a checked hint is, what "ended with at most one bid" means, what
    # the comparison is (any tender, picked blind), and what the factor is.
    out = ['Wie gut das klappt, prüfen wir laufend: Für jeden Hinweis '
           'sehen wir später im veröffentlichten Ergebnis nach, wie viele '
           'Angebote wirklich eingegangen sind.']
    if beats(overall):
        out.append(f'Bisher haben wir {n_de(overall["checked"])} Hinweise so '
                   f'geprüft, über alle Gewerke: {pct_de(overall["precision"])} '
                   f'davon bekamen am Ende höchstens ein Angebot. Nimmt man '
                   f'stattdessen irgendeine Ausschreibung, sind es '
                   f'{pct_de(overall["base"])}. Unsere Auswahl trifft also '
                   f'{factor_de(overall["factor"])}-mal so oft.')
    if beats(v):
        out.append(f'Im Gewerk {trade} allein: {n_de(v["checked"])} Hinweise '
                   f'geprüft, {pct_de(v["precision"])} mit höchstens einem '
                   f'Angebot gegenüber {pct_de(v["base"])} ohne Auswahl – '
                   f'{factor_de(v["factor"])}-mal so oft; diese Zahl wächst '
                   f'mit jeder geprüften Woche.')
    return [' '.join(out)]


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
    win = own_win(home, company)
    trade, v, overall = trade_of(home, company)
    # The lots the note promises and the message delivers are ONE list
    # (doc/SALES.md 6): the candidates of the trigger — flagged, in the
    # firm's MAIN trade, with enough deadline left that a reply in three
    # days still finds them open. Without candidates there is nothing to
    # promise, and the page says so instead of offering a note.
    picks = []
    if sub:
        try:
            import sales
            picks = sales.candidates(home, sub, trade, today)
        except Exception as e:                                 # noqa: BLE001
            print(f'[pitch] candidates unavailable ({e})')
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
    edge = edge_block(trade, v, overall)
    if edge:
        lines += edge
    if facts or edge:
        where = (f'{SITE_URL}/gewerke/{v["slug"]}/' if v.get('slug')
                 else f'{SITE_URL}/gewerke/')
        lines += [f'Alle Zahlen und wie sie entstehen: {where}', '']
    if picks:
        # The list is the product working, not inventory (operator,
        # 2026-08-18: "just three tenders? why should they care?"): it says
        # these are the lots we expect few bidders on, picked for this firm,
        # and each carries the reasons the forecast gives (`why_lonely`).
        n = len(picks)
        word = {1: 'Eine Ausschreibung, die', 2: 'Zwei Ausschreibungen, die'
                }.get(n, 'Drei Ausschreibungen, die')
        lines += [f'So sieht das konkret aus. {word} gerade offen '
                  f'{"ist" if n == 1 else "sind"} und bei '
                  f'{"der" if n == 1 else "denen"} wir wenige Bieter erwarten '
                  f'– ausgewählt nach dem, was Sie bisher gewonnen haben:', '']
        for i, p in enumerate(picks, 1):
            lines.append(f'{i}. {_short(p.get("title"), 80)} – '
                         f'{_short(p.get("buyer_name"), 60)}, '
                         f'Frist {_de(p.get("deadline_date"))}')
            why = [str(w) for w in (p.get('why_lonely') or ()) if w][:3]
            if why:
                lines.append(f'   Für wenig Wettbewerb {"spricht" if len(why) == 1 else "sprechen"}: '
                             + ', '.join(why) + '.')
            pn = p.get('publication_number')
            if pn:
                lines.append(f'   {TED_URL.format(pn=pn)}')
        lines += ['', 'Bei solchen Losen lohnt sich ein Angebot besonders: '
                  'wenige Mitbewerber, weniger Preisdruck – und Sie erfahren '
                  'davon, bevor die Frist knapp wird.', '']
    else:
        lines += ['Diese Woche ist in Ihrem Gewerk nichts Passendes offen – '
                  'auch das sagen wir, statt etwas aufzufüllen. Der erste '
                  'Bericht bringt, was neu dazukommt.', '']
    lines += [f'Wenn Sie so eine Auswahl jeden Montag per E-Mail bekommen '
              f'möchten: {url} – E-Mail-Adresse eintragen, fertig. Vier '
              f'Wochen kostenlos, kein Konto, jederzeit abbestellbar.', '',
              'Woher wir Ihre Firmendaten haben und wie Sie widersprechen, '
              'steht dort unter „Datenschutz".', '',
              SIGNATURE]
    long = '\n'.join(lines)

    # The note: lead, one live example, the ask — and never a cut-off word.
    # The example gives way first: its title and buyer shrink to what is
    # left after lead and ask, and below a readable minimum it is dropped
    # (a trade name like „Lüftung, Klima und Kälte" costs 25 characters).
    # The note (doc/SALES.md 6): what we have for HIM, now. No candidates,
    # no note — a first contact without a tender behind it is the message
    # the operator called bullshit on 2026-08-18.
    short = ''
    if picks:
        n = len(picks)
        what = ('eine öffentliche Ausschreibung' if n == 1 else
                f'{"zwei" if n == 2 else n} öffentliche Ausschreibungen')
        lead = (f'Guten Tag, für {trade or "Ihr Gewerk"} sehen wir gerade '
                f'{what}, bei {"der" if n == 1 else "denen"} wir nur ein bis '
                f'zwei Bieter erwarten. ')
        ask = (f'Dürfen wir Ihnen '
               f'{"sie" if n == 1 else "die beiden" if n == 2 else "diese"} '
               f'schicken? Kostenlos, ohne Konto.')
        p = picks[0]
        when = _de(p.get('deadline_date'))[:5]              # 08.09
        fixed = len(f'Z. B.: , , Frist {when}. ')
        room = SHORT_LIMIT - len(lead) - len(ask) - fixed
        example = ''
        if room >= 40:
            title = _short(p.get('title'), max(20, room * 3 // 5))
            buyer = _short(_buyer_head(p.get('buyer_name')),
                           max(16, room - len(title)))
            example = f'Z. B.: {title}, {buyer}, Frist {when}. '
        short = lead + example + ask
        if len(short) > SHORT_LIMIT:                         # belt and braces
            short = short[:SHORT_LIMIT - 1].rstrip() + '…'
    return {'short': short, 'long': long, 'picks': picks, 'win': win,
            'trade': trade, 'edge': v, 'overall': overall}
