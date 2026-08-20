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

import re
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


# NUTS-1 -> Land, for the note: the lot's region in words a reader uses. A
# lot's NUTS is one the firm has already won in (invite.py writes the draft's
# nuts_prefixes from its wins), so naming it is naming HIS region.
LAND = {
    'DE1': 'Baden-Württemberg', 'DE2': 'Bayern', 'DE3': 'Berlin',
    'DE4': 'Brandenburg', 'DE5': 'Bremen', 'DE6': 'Hamburg', 'DE7': 'Hessen',
    'DE8': 'Mecklenburg-Vorpommern', 'DE9': 'Niedersachsen',
    'DEA': 'Nordrhein-Westfalen', 'DEB': 'Rheinland-Pfalz', 'DEC': 'Saarland',
    'DED': 'Sachsen', 'DEE': 'Sachsen-Anhalt', 'DEF': 'Schleswig-Holstein',
    'DEG': 'Thüringen',
}


def land_of(nuts):
    return LAND.get(str(nuts or '')[:3].upper())


# What a title says about the WORK, with the things that make the lot
# findable taken out: a street ("Gewerbeschulstr 109"), a lot number, a
# buyer's project code. The note must be credible without being googleable
# (operator, 2026-08-18: show part of the tender, the notice comes after the
# contact). A heuristic, and a bad strip reads worse than none — so anything
# that comes out too short falls back to the trade word alone.
_FINDABLE = re.compile(
    r'(\b[\wäöüß.-]*(?:str(?:aße|asse)?\.?|weg|platz|allee|gasse|ring|damm|'
    r'ufer|chaussee)\s*\d+[a-z]?\b)'           # a street with a number
    r'|(\b(?:Am|An der|Im|In der|Auf dem|Zum|Zur)\s+[A-ZÄÖÜ][\wäöüß]+'
    r'\s+\d+[a-z]?\b)'                          # "Am Hang 12a"
    r'|(\bLos\s*\d+\b)'                        # a lot number
    r'|(\b\d{2,}[./-]\d+[\w./-]*\b)'          # a project or file code
    r'|(\b[A-Z]{2,}[-/]?\d+\b)'                 # BV-2026-17, VE12
    r'|([(\[][^)\]]*[)\]])',                     # anything in brackets
    re.I)


def work_of(title, trade=None):
    s = ' '.join(str(title or '').split())
    s = _FINDABLE.sub(' ', s)
    s = re.sub(r'\s*[-–—:;,/]+\s*', ' ', s)          # the separators it left
    if trade:
        # the trade is the head of the sentence already; a title word that
        # IS the trade (Elektroinstallation, Blitzschutzarbeiten) would only
        # repeat it
        root = trade.split()[0].casefold()[:6]
        s = ' '.join(w for w in s.split()
                     if not w.casefold().startswith(root))
    # words that say nothing once the trade is named
    s = ' '.join(w for w in s.split()
                 if w.casefold().strip('.,') not in _GENERIC)
    s = ' '.join(s.split()).strip(' .')
    words = s.split()
    if not words or (len(words) == 1 and len(s) < 6):
        return None
    return s


_GENERIC = {'installation', 'installationen', 'arbeiten', 'leistungen',
            'bauleistungen', 'gewerk', 'gewerke', 'los', 'lose', 'und', 'für',
            'der', 'die', 'das', 'des', 'im', 'in', 'am', 'an', 'mit', 'von'}


def value_band(p):
    """'rund 600.000 €' when the notice carries an estimate, else None —
    never guessed. Rounded so it reads as a size, not an exact bid."""
    v = p.get('est_value_lot')
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v != v or v <= 0:                                  # NaN
        return None
    if v >= 1_000_000:
        return f'rund {v / 1_000_000:.1f} Mio. €'.replace('.', ',', 1) \
            .replace(',0 Mio', ' Mio')
    step = 50_000 if v >= 300_000 else 10_000
    return f'rund {int(round(v / step) * step):,} €'.replace(',', '.')


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
    out = ['Wie gut das klappt, prüfen wir laufend: Wir ordnen jede '
           'Ausschreibung nach der erwarteten Zahl der Angebote und sehen '
           'später im veröffentlichten Ergebnis nach, wie viele wirklich '
           'eingegangen sind.']
    if beats(overall):
        out.append(f'Vom obersten Fünftel unserer Reihenfolge — '
                   f'{n_de(overall["checked"])} geprüfte Lose über alle '
                   f'Gewerke — endeten {pct_de(overall["precision"])} mit '
                   f'höchstens einem Angebot. Nimmt man stattdessen '
                   f'irgendeine Ausschreibung, sind es '
                   f'{pct_de(overall["base"])}. Unsere Auswahl trifft also '
                   f'{factor_de(overall["factor"])}-mal so oft.')
    if beats(v):
        out.append(f'Im Gewerk {trade} allein: {n_de(v["checked"])} Lose im '
                   f'obersten Fünftel geprüft, {pct_de(v["precision"])} mit '
                   f'höchstens einem Angebot gegenüber {pct_de(v["base"])} im '
                   f'Gewerk insgesamt – '
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
    # The terms, complete and in one breath, where the decision is made
    # (operator, 2026-08-18: "kostenlos" with its end and its price visible
    # is a trial; without them it is bait). The price line is the env's
    # (app.PRICE_ENV) — until it is decided the sentence says so rather than
    # hiding it.
    import os
    price = (os.environ.get('TM_PRICE_LINE') or '').strip()
    after = (f'danach kostet sie {price}' if price else
             'danach gegen eine monatliche Gebühr, die wir Ihnen vorher nennen')
    lines += [f'Wenn Sie so eine Auswahl jeden Montag per E-Mail bekommen '
              f'möchten: {url} – E-Mail-Adresse eintragen, fertig. Vier Wochen '
              f'bekommen Sie sie ohne Kosten; {after}, kündbar jederzeit mit '
              f'einem Klick. Es gibt kein Konto und kein Passwort – nur Ihre '
              f'E-Mail-Adresse.', '',
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
    short = note(picks, trade) if picks else ''
    return {'short': short, 'long': long, 'picks': picks, 'win': win,
            'trade': trade, 'edge': v, 'overall': overall}


def note(picks, trade):
    """The connection note (doc/SALES.md 6, reworded 2026-08-18 with the
    operator): a teaser with the forecast first.

    Shows what makes the lot credible and relevant — the kind of work, the
    Land, the size when the notice has one, the deadline — and our one thing
    he cannot google: that we expect one or two bidders. Withholds what
    makes it findable (title, buyer, link), so accepting the request has a
    value: the notice. No terms, no price, no "kostenlos" — the note is about
    one tender and one promise; the terms stand, complete, in the message
    after the contact. Accepting IS the answer on LinkedIn, so that is the
    ask. Never a cut-off word: the optional parts give way in order (value,
    then the kind of work) before anything is truncated."""
    p = picks[0]
    n = len(picks)
    trade = trade or 'Ihr Gewerk'
    when = _de(p.get('deadline_date'))[:6]                  # 07.09.
    land = land_of(p.get('place_nuts3'))
    where = f' in {land}' if land else ' in Ihrer Region'
    work = work_of(p.get('title'), trade)
    value = value_band(p)

    def build(with_work, with_value):
        head = (f'{trade}: {work}{where}' if with_work and work
                else f'{trade}{where}')
        size = f', {value}' if with_value and value else ''
        first = (f'Guten Tag, {head}{size}, Frist {when} – nach unserer '
                 f'Einschätzung bieten dort nur ein bis zwei Firmen. ')
        more = ('' if n == 1 else
                'Eine zweite solche haben wir auch. ' if n == 2 else
                f'{n - 1} weitere solche haben wir auch. ')
        what = ('die Bekanntmachung' if n == 1 else
                'beide Bekanntmachungen' if n == 2 else
                'die Bekanntmachungen')
        ask = f'Wenn Sie die Anfrage annehmen, schicken wir Ihnen {what}.'
        return first + more + ask

    for with_work, with_value in ((True, True), (True, False), (False, False)):
        text = build(with_work, with_value)
        if len(text) <= SHORT_LIMIT:
            return text
    return text[:SHORT_LIMIT - 1].rstrip() + '…'            # belt and braces
