"""The invitation message — doc/ONBOARDING.md 9.2a.

One firm, one message to paste into LinkedIn (or Xing, or read out on the
phone). Redrafted with the operator 2026-08-18; reordered 2026-08-20 ("a new
potential customer wont be interested in the theory if he doesnt know what
is it about"): who we are in two sentences; the trade's market figures from
its public page (`trade_pages.forecasts`); why low-competition lots pay,
then the picks, each with its TED link; the invitation link; the signature.
Our own hit rate is ONE line inside the market figures — when the trade's
forecast beats guessing, „von den Losen, die wir empfehlen, bekommen X %
höchstens ein Angebot" stands right under the market's rate; any other
verdict state adds nothing. No proof paragraph, no Datenschutz pointer
(operator, 2026-08-20: a reader who has not subscribed is not interested in
how we work — the method and the Datenschutzerklärung stay on the trade
page). The verdict still decides WHOM the operator writes to (admin page).
"Wir" throughout, no person's name (we do not know it), no "ich".

The picks come from the same machinery a customer's Monday report uses: the
cycle's latest scored lots, the firm's draft subscription (written by
`invite.add` from its own won contracts), the relevance gate, and
`selection.for_sub`. A prospect therefore sees exactly what it would receive
as a customer — if the pick list is thin, that is the honest signal, and the
message says nothing rather than inventing something.
"""

import re
import threading
from datetime import date
from pathlib import Path

import ledger
import subscriptions
import util

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


# --------------------------------------------------------- the request cache
#
# The operator's pages rebuilt the relevance gate (2.7 s), re-read the open
# market (1.2 s) and re-read the awards store (1.0 s) on EVERY request — a
# 10 s message page, and „Heute schreiben" pays it once per watched firm
# (operator, 2026-08-20). Each of these is a pure function of what it reads
# on disk and the caller's day, so each is cached on exactly that — and on
# ONLY that: the stamp names the tables and files the piece actually reads
# (`ledger.versions` per table, mtime+size per file), so marking a message
# as sent (an app_events write) does not retire a gate that never reads
# app_events. A write to a named source moves its marker and the next
# request rebuilds — nothing is ever served past what it was built from.
#
# One slot per thing, not a dict: the app serves one operator and one
# current world, and a receipt harness replaying historical dates must not
# pin a sidecar matrix per date it visits. The lock is held through the
# build on purpose — the second thread waits for the first build instead of
# duplicating it.

_cache_lock = threading.Lock()
_gate_slot = {}
_lots_slot = {}
_win_slot = {}


def _file_stamp(*paths):
    out = []
    for p in paths:
        try:
            st = p.stat()
            out.append((p.name, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((p.name, None, None))
    return tuple(out)


def _store_stamp(home):
    store = Path(home) / 'store'
    return _file_stamp(store / 'awards.parquet', store / 'tenders.parquet')


def _sidecar_stamp(home):
    import embed
    side = embed.sidecar_dir(home)
    return _file_stamp(side / 'lots.npy', side / 'lots_index.jsonl',
                       side / 'cpv_labels.npy')


def data_stamp(home):
    """Everything the picks read, as one tuple — the key sales.candidates
    memoises under. Deliberately WITHOUT app_events: the operator marking a
    message as sent must not cost the next click a rebuild."""
    return (_sidecar_stamp(home), _store_stamp(home),
            ledger.versions(home, 'predictions', 'learned_refs'))


def _slot(slot, key, build):
    with _cache_lock:
        if slot.get('key') == key:
            return slot['value']
        value = build()
        slot['key'], slot['value'] = key, value
        return value


def gate_for(home, today):
    """The day's relevance gate, shared across requests — relevance.Gate is
    'loaded once per cycle; profiles built per sub' by design, and a request
    is no different. Keyed on what a Gate reads: the embedding sidecars,
    the store (cpv+buyer per row) and the learned references."""
    import relevance as rel
    key = (str(home), str(today), _sidecar_stamp(home), _store_stamp(home),
           ledger.versions(home, 'learned_refs'))
    return _slot(_gate_slot, key, lambda: rel.Gate(str(home), as_of=today))


def open_lots(home, today):
    """The cycle's current view of the open market: the last prediction per
    lot, deadline not yet passed. Awarded lots are dropped by the deadline
    filter in all but rare cases, and a teaser is not a report."""
    key = (str(home), str(today), ledger.versions(home, 'predictions'))
    # a fresh list per caller — the rows are shared, the list is theirs
    return list(_slot(_lots_slot, key, lambda: _open_lots(home, today)))


def _open_lots(home, today):
    rows = []
    for row in ledger.prediction_latest_per_lot(home).values():
        # A REAL date, not merely a truthy value: a missing deadline arrives
        # as NaN, whose str() is 'nan' — and 'nan' > '2026-08-17' is True in
        # a string comparison, which put "Frist None" into the first message
        # this function ever produced. A lot we cannot date is a lot we
        # cannot invite anyone to. The date is the ACTIONABLE one — offer
        # deadline, or a two-stage lot's participation deadline (util.frist).
        deadline, _ = util.frist(row)
        try:
            date.fromisoformat(deadline or '')
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
            gate = gate_for(home, today)
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
    cannot say. Cached like the gate: the store is re-read only when its
    files move."""
    return _slot(_win_slot, (str(home), company, _store_stamp(home)),
                 lambda: _own_win(home, company))


def _own_win(home, company):
    try:
        import pandas as pd
        import outreach
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
        # the bidder line runs over the page's bidder window (the last
        # `bidder_months` months with complete results, market.RECENT_MONTHS),
        # not the volume lines' full history — and says so, like the page
        # (2026-08-20: one window per claim, named where it is used)
        window = (f' (letzte {f["bidder_months"]} Monate mit vollständigen '
                  f'Ergebnissen)' if f.get('bidder_months') else '')
        lines.append(f'– auf ein Los bewerben sich im Mittel '
                     f'{f.get("median_bidders", 0):.0f} Firmen – aber '
                     f'{pct_de(f["low_bid"])} der Lose bekommen höchstens ein '
                     f'Angebot{window}')
    # our one number, right under the market's, when the trade's forecast
    # beats guessing (operator, 2026-08-20: only the 24 %, in the figures —
    # a reader who has not subscribed is not interested in how we work)
    if v.get('state') == 'beats' and v.get('precision'):
        lines.append(f'– von den Losen, die wir empfehlen, bekommen '
                     f'{pct_de(v["precision"])} höchstens ein Angebot')
    return lines


def message(home, sub_id, url, company=None, today=None):
    """-> {'short', 'long', 'picks', 'win', 'trade', 'edge'} — the two texts
    to paste (drafted with the operator 2026-08-18).

    `short` fits LinkedIn's 300-character connection note: who we work for,
    the problem (few bidders = high chance), one live tender, the ask. No
    link (a note with a URL reads as spam and the link is useless before the
    contact is accepted). `long` is the message after the contact: who we
    are and why we wrote, the trade's market figures from its public page
    (our hit rate as one line among them, when the forecast beats guessing),
    the picks each with its TED link, the invitation link, a signature.
    "Wir" throughout; no person's name — we do not know it.
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
    # Reader-first order (operator, 2026-08-20: "a new potential customer
    # wont be interested in the theory if he doesnt know what is it about"):
    # who writes, the trade's numbers (our hit rate one line among them),
    # why low-competition lots pay, the lots. No track-record paragraph and
    # no proof sentence — the method stays on the trade page.
    lines = ['Guten Tag,', '',
             f'danke für die Kontaktannahme. Wir sind Murara – wir lesen jede '
             f'Woche alle deutschen Ausschreibungen. Ihre Firma haben wir '
             f'über Ihre Aufträge im EU-Vergaberegister gefunden.', '']
    facts = facts_block(trade, v) if trade else []
    if facts:
        lines += facts + ['']
    if picks:
        # The list is the product working, not inventory (operator,
        # 2026-08-18: "just three tenders? why should they care?"): the why
        # comes BEFORE the lots, each lot carries the forecast's reasons
        # (`why_lonely`).
        n = len(picks)
        count = {1: 'Eine solche ist', 2: 'Zwei solche sind',
                 3: 'Drei solche sind'}.get(n, f'{n} solche sind')
        lines += [f'Bei Losen mit wenig Wettbewerb lohnt sich ein Angebot '
                  f'besonders: kaum Mitbewerber, kaum Preisdruck – und keine '
                  f'Angebote, die umsonst kalkuliert waren. {count} in Ihrem '
                  f'Gewerk gerade offen – ausgewählt nach dem, was Sie bisher '
                  f'gewonnen haben:', '']
        for i, p in enumerate(picks, 1):
            fd, fp = util.frist(p)
            frist_txt = ('Teilnahmeantrag bis' if fp else 'Frist') + f' {_de(fd)}'
            lines.append(f'{i}. {_short(p.get("title"), 80)} – '
                         f'{_short(p.get("buyer_name"), 60)}, {frist_txt}')
            why = [str(w) for w in (p.get('why_lonely') or ()) if w][:3]
            if why:
                lines.append(f'   Für wenig Wettbewerb {"spricht" if len(why) == 1 else "sprechen"}: '
                             + ', '.join(why) + '.')
            pn = p.get('publication_number')
            if pn:
                lines.append(f'   {TED_URL.format(pn=pn)}')
        lines += ['']
    else:
        lines += ['Diese Woche ist in Ihrem Gewerk nichts Passendes offen – '
                  'auch das sagen wir, statt etwas aufzufüllen. Sobald so '
                  'ein Los auftaucht, bekommen Sie es.', '']
    # The terms, complete and in one breath, where the decision is made
    # (operator, 2026-08-18: "kostenlos" with its end and its price visible
    # is a trial; without them it is bait). The price is `mailer.price_line`
    # — always a number, never "to be announced" (2026-08-20).
    import mailer
    after = f'danach {mailer.price_line()}'
    # No weekly promise (operator, 2026-08-20): a mail comes when there is a
    # recommendation in it, and only then — delivering.deliver enforces
    # exactly that, so the sentence describes the behaviour, not a rhythm.
    # After the terms comes the signature, nothing else: the proof paragraph
    # and the Datenschutz pointer are gone (operator, 2026-08-20 — a reader
    # who has not subscribed is not interested in how we work; our hit rate
    # is ONE line in the figures above, the full method and the
    # Datenschutzerklärung stay on the trade page).
    lines += [f'Wenn Sie solche Empfehlungen per E-Mail bekommen möchten: '
              f'{url} – E-Mail-Adresse eintragen, fertig. Wir schreiben nur, '
              f'wenn es so ein Los für Sie gibt; gibt es keines, kommt keine '
              f'E-Mail. Die ersten vier E-Mails mit Empfehlungen sind '
              f'kostenlos; {after}, kündbar jederzeit mit einem Klick. Es '
              f'gibt kein Konto und kein Passwort – nur Ihre '
              f'E-Mail-Adresse.', '',
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
    """The connection note (doc/SALES.md 6; variant B, chosen by the
    operator 2026-08-20 — "too many sudden facts, too few emotion"): the
    pain first (most bids are calculated for nothing), then the one lot
    almost nobody will bid on, then the ask. The trade name does not appear;
    „passend zu Ihren bisherigen Aufträgen" carries the relevance. Still
    withheld: title, buyer, link — accepting the request keeps its value —
    and no terms, no price, no "kostenlos". Accepting IS the answer on
    LinkedIn, so that is the ask; one lot is promise enough, the further
    ones are the surprise in the message after the contact. Never a cut-off
    word: the parts give way in order — the match clause shortens, the Land
    becomes „Ihrer Region", the clause goes — before anything is truncated.

    Variant A, kept for a future A/B test (recognition first, chosen
    against on 2026-08-20 but explicitly kept as the challenger):

        Guten Tag, Ihre gewonnenen Aufträge im EU-Vergaberegister zeigen:
        Sie können {trade}. Gerade ist in {Land} ein Los offen, auf das
        nach unserer Prüfung kaum jemand bieten wird – kaum Preisdruck,
        {Frist_wort} {when}. Wenn Sie die Anfrage annehmen, schicken wir
        Ihnen die Bekanntmachung."""
    p = picks[0]
    d, part = util.frist(p)
    when = _de(d)[:6]                                   # 07.09.
    frist_word = 'Teilnahmefrist' if part else 'Frist'
    land = land_of(p.get('place_nuts3'))

    def build(match, with_land):
        region = f'In {land}' if with_land and land else 'In Ihrer Region'
        return (f'Guten Tag, die meisten Angebote auf Ausschreibungen sind '
                f'umsonst kalkuliert – zu viele Bieter. Wir suchen die Lose, '
                f'bei denen fast niemand bietet. {region} ist gerade so '
                f'eines offen, {frist_word} {when}{match}. Nehmen Sie die '
                f'Anfrage an, schicken wir die Bekanntmachung.')

    for match, with_land in (
            (', passend zu Ihren bisherigen Aufträgen', True),
            (', passend zu Ihren Aufträgen', True),
            (', passend zu Ihren Aufträgen', False),
            ('', False)):
        text = build(match, with_land)
        if len(text) <= SHORT_LIMIT:
            return text
    return text[:SHORT_LIMIT - 1].rstrip() + '…'            # belt and braces
