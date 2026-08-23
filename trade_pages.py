"""Murara's public site — doc/TRADE_PAGES.md.

    python trade_pages.py                     # -> <data-dir>/public/current/
    python trade_pages.py --dry-run           # who qualifies, writes nothing
    python trade_pages.py --replay run.json   # ... with the forecast section

Builds the whole site: the hand-written pages copied out of `site/`, plus one
generated market page per trade at `/gewerke/<slug>` and the sitemap that
knows both halves. A contractor searching for his own trade can find that page
and learn something true when he arrives. The pages never link to each other:
a Maler is shown nothing about Elektro, which is the rule the whole design
exists to protect.

**Source and output are separate directories** (operator, 2026-08-11).
`site/` is source — committed, hand-edited, and inside the code checkout,
which in the container is the read-only image. The build therefore goes to
`<data-dir>/public/`, on the mounted volume: nothing generated is committed,
and nothing is written where the container would discard it.

**The served site is `<data-dir>/public/current/`**, a symlink to the one
complete build beside it (`release`, below). The edge serves that path, so a
rebuild is either not yet visible or entirely visible — never a half-written
directory, never an empty one — and the previous build is deleted the moment
the link has moved (operator, 2026-08-15: nothing is kept). `deploy.sh` runs
this after every switch and the weekly cycle runs it every Monday.

Murara is the customer-facing brand; TenderMining stays the internal system
name, so nothing a visitor reads says TenderMining.

**Every figure comes from `market.py`** — its loader, its coverage rule, its
`SMALL_SAMPLE` line. The public figure and the operator's figure must not be
able to drift apart, which they would the moment this file computed anything
itself.
"""

import argparse
import html
import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

import market

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

SITE = Path(__file__).resolve().parent / 'site'
CONTACT = 'info@murara.eu'
BASE_URL = 'https://www.murara.eu'

# doc/TRADE_PAGES.md 3: the floor is market.py's own line for "below this, a
# share is indicative, not a rate". A page whose headline figure cannot
# honestly be quoted should not exist.
MIN_AWARDED = market.SMALL_SAMPLE

MONTHS_DE = ('Januar', 'Februar', 'März', 'April', 'Mai', 'Juni', 'Juli',
             'August', 'September', 'Oktober', 'November', 'Dezember')


def esc(s):
    return html.escape(str(s))


def money_de(x):
    """German money, for a customer. `market.money` is the operator console's
    terse form ('204 k', '34.08 M') — right for a terminal, wrong on a page a
    contractor reads: German uses '.' for thousands and ',' for decimals."""
    def de(v, decimals=0):
        """1234.5 -> '1.234,5' — swap the separators, both at once, because
        doing it in two passes turns the thousands dot into a comma."""
        s = f'{v:,.{decimals}f}'
        return s.translate(str.maketrans({',': '.', '.': ','}))

    if x is None:
        return '—'
    if x >= 1_000_000_000:
        return f'{de(x / 1_000_000_000, 2)} Mrd.'
    if x >= 1_000_000:
        return f'{de(x / 1_000_000, 1)} Mio.'
    return de(round(x))


def pct_de(x):
    """A share as German typography writes it: a space before the sign.
    Python's `:.0%` produces '10%', which is the English convention and looks
    wrong beside the '12 %' the figure cards already print."""
    return f'{100 * x:.0f} %'


def factor_de(x):
    """'2,2' — a decimal comma, like every other number on the page."""
    return f'{x:.1f}'.replace('.', ',')


def slugify(name):
    """'Maler- und Lackierarbeiten' -> 'maler-und-lackierarbeiten'."""
    s = name.lower()
    for a, b in (('ä', 'ae'), ('ö', 'oe'), ('ü', 'ue'), ('ß', 'ss')):
        s = s.replace(a, b)
    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-', s)).strip('-')


def figures(lots, sel, covered, mature):
    """The numbers on a page, all of them market.py's own.

    Volume figures (Lose pro Monat, Median-Auftragswert, Jahresvolumen) run
    over the covered months — how big the market is. Every BIDDER figure —
    the tile, the spread table, the median bidders, and therefore the base
    every forecast claim is measured against — runs over the newest
    `market.RECENT_MONTHS` mature months (`market.recent_mature`), and the
    page says so: the archive reaches into 2023, whose much lonelier market
    would otherwise sit inside a rate quoted to a reader bidding today
    (operator, 2026-08-20).

    Returns None when the trade cannot honestly carry a page — fewer than
    MIN_AWARDED awarded lots in the bidder window (doc/TRADE_PAGES.md 3)."""
    sub = lots[sel]
    n_months = max(len(covered), 1)
    cov = sub[sub.month.isin(covered)]
    recent = market.recent_mature(mature)
    mat = sub[sub.month.isin(recent) & sub.resolved]
    if len(mat) < MIN_AWARDED:
        return None
    aw = sub.award_value.dropna()
    per_month = len(cov) / n_months
    med = float(aw.median()) if len(aw) else None
    zero = int((mat.n_tenders == 0).sum())
    one = int((mat.n_tenders == 1).sum())
    low_bid, _ = market.low_bid_rate(lots, recent, sel)
    # "Closed without award" (`clos-nw`) is deliberately not on the page: to
    # a reader it means the same as "kein Angebot", and the difference (bids
    # came in, buyer still awarded nobody) needs a paragraph to explain and
    # is a handful of lots. It stays in `market.py trade` for the operator.
    # How competition is spread, in buckets a contractor thinks in. This is
    # the page's real content: "9 % have at most one bidder" is a headline,
    # but the shape underneath is what tells him whether his market is a
    # price war with a tail or genuinely thin.
    buckets = [('kein Angebot', (mat.n_tenders == 0)),
               ('1 Angebot', (mat.n_tenders == 1)),
               ('2–3', mat.n_tenders.between(2, 3)),
               ('4–6', mat.n_tenders.between(4, 6)),
               ('7 und mehr', (mat.n_tenders >= 7))]
    dist = [(label, int(m.sum()), float(m.mean())) for label, m in buckets]
    return {
        'per_month': per_month,
        'per_year': per_month * 12,
        'median_award': med,
        'year_scope': med * per_month * 12 if med else None,
        'n_awarded': len(mat),
        # the tile's figure AND the forecast's denominator — one rate per
        # trade, `market.low_bid_rate`, so the claim below the tile can be
        # checked against the tile
        'low_bid': low_bid,
        'zero': zero, 'one': one,
        'median_bidders': float(mat.n_tenders.median()),
        'months': n_months,
        'bidder_months': len(recent),
        'dist': dist,
    }


# --------------------------------------------- the forecast, per trade
#
# doc/METHODS.md 0: this is the FORECAST precision/recall — did a lot we
# flagged really end with 0-1 bids — not the gate's. It can only come from the
# as-of replay (`rewind_all.py`), because a live award publishes a median 84
# days after its tender, so the live grade ledger holds 18 rows.
#
# The replay is sliced HERE, by the same title word-match that defines a trade
# everywhere else, rather than by CPV3 as the backtest's own prose table does:
# buyers enter CPV wrongly, which is why `market.py` never consults it.

# A precision over a handful of alarms is noise: flag one lot a year, get it
# right, claim 100 %. Same line the pages already use for a market share.
MIN_CHECKED = market.SMALL_SAMPLE


# The one conventional place, on the state volume: the deploy and the cycle
# build the site without flags, and the operator's replay is meant to reach
# every visitor (2026-08-18: "the pages don't mention the lift — add it").
# `python rewind_all.py --out <data>/replay/latest.json` puts it there; the
# next deploy or cycle prints the forecast section on every page. Nothing
# writes this file but the operator's own replay run.
REPLAY_FILE = Path('replay') / 'latest.json'


def replay_path(data_dir, given=None):
    """`--replay PATH` wins; otherwise `<data>/replay/latest.json` if it
    exists; otherwise None (no claim, and the console says so)."""
    if given:
        return given
    p = Path(data_dir) / REPLAY_FILE
    return p if p.exists() else None


def load_replay(path):
    """A replay document from `rewind_all.py`, or None when there is none.

    The replay is produced by `python rewind_all.py --out somewhere.json` and
    this program is told where it is — by `--replay`, or by the conventional
    `<data>/replay/latest.json` (`replay_path`). No document means no
    forecast claim, which is the state on a fresh checkout and the correct
    one — the pages then say so in words.

    A **bad** document is also None, because an optional section must never
    cost the market pages — but it is announced on the console first. Silence
    would make a typo'd `--replay` path indistinguishable from a deliberate
    omission: same site, no forecast anywhere, no way to tell which. That is
    the failure this branch exists to prevent, so it prints and carries on.
    """
    if not path:
        return None
    # lazy, like the flag_stats import below: reading a document must not make
    # the site builder pull the ML stack when no --replay was given
    from rewind_all import BadDocument, read_payload
    try:
        return read_payload(str(path))
    except BadDocument as e:
        print(f'[trades] --replay ignored: {e}', file=sys.stderr)
        print('[trades] the pages will make no forecast claim', file=sys.stderr)
        return None


def rank_stats(receipt, base, lots=None, sel=None, months=None):
    """-> (stats, generated) — the RANKING measured on the replay, against
    the MARKET's rate, or None when the document cannot carry the claim.

    The product is a ranked shortlist, so the page describes the ranking:
    of the graded lots (award published), how often did the top TOP_SHARE by
    score end with 0-1 bids (`grading.score_stats` — the same function the
    weekly report's rank view uses), plus the sorting graded without a cut
    (AUC). The flag at 0.5 is not quoted here any more: it is one arbitrary
    operating point, its volume swung 15-45 % of the market across cutoffs,
    and it is not what a customer receives (operator, 2026-08-20).

    Two honesty rules, both learned the hard way (2026-08-19):
    * `base` is the trade's own 0/1 rate — the tile, `market.low_bid_rate` —
      never the replay pool's own rate, which is what the top slice's `hit`
      is compared against for the verdict and the factor. The pool's rate
      stays in the dict (`base` of score_stats -> `pool_base`).
    * only lots from MATURE months are graded (`months`): among recently
      published tenders the outcomes already known are mostly the lonely
      ones (awards within 60 days: 33 % lonely; later: 7 %), so grading
      fresh flags flatters the forecast in exactly the way a reader cannot
      see. With `lots`/`sel`/`months` omitted (the overall record), the
      caller passes the store-wide mature-month base and the slice is every
      graded lot of the replay.

    A schema-2 document has no scores: -> None, and the page says "noch
    nicht genug" rather than quoting anything."""
    if not receipt:
        return None
    keys = None
    if sel is not None:
        keys = set(zip(lots.loc[sel, 'procedure_id'], lots.loc[sel, 'lot_id']))
    mature_keys = None
    if months is not None:
        m = lots[lots.month.isin(months)]
        mature_keys = set(zip(m.procedure_id, m.lot_id))
    rows = [{'score': r['score'], 'label': int(r['n_tenders'] <= 1)}
            for r in receipt['lots']
            if r['n_tenders'] is not None and r.get('score') is not None
            and (keys is None or (r['procedure_id'], r['lot_id']) in keys)
            and (mature_keys is None
                 or (r['procedure_id'], r['lot_id']) in mature_keys)]
    if not rows:
        return None
    from grading import score_stats
    st = score_stats(rows)
    if st is None:
        return None
    positives = sum(r['label'] for r in rows)
    return {**st, 'pool_base': st['base'], 'base': base,
            # of the lonely lots we could check, the share our top fifth held
            'recall': (st['hits'] / positives) if positives else None,
            'beats_base': (base is not None and st['hit'] > base),
            }, receipt.get('generated', '?')


ALL = '_all'        # the key of the overall record in FORECAST_FILE


# The smallest lift the page will call an advantage. `factor_de` prints one
# decimal, so anything under 1.05 would show as „1,0-fach — so oft trifft
# unser Hinweis, verglichen mit Zufall": an advantage tile announcing no
# advantage. Measured against the trade's own rate (2026-08-19) Heizung is
# exactly that case — 10,3 % against 9,9 % — and it must read "nicht besser",
# not carry a tile. This is a display floor, not a significance test: the
# count is printed beside every rate so a reader can weigh it.
MIN_FACTOR = 1.05


def level(fc):
    """The forecast's standing in one trade, as a small dict the page tile,
    the operator page and the invitation message all read (2026-08-18: the
    operator writes only to firms whose trade shows an advantage over
    guessing, so the same verdict must be visible on all three).

    Since 2026-08-20 the standing is the RANKING's: `checked` is the top
    fifth of the trade's graded lots (`hits` of them ended 0-1 bids,
    `precision` their share), `base` the trade's own rate — the tile —
    `auc` the sorting check. Key names kept from the flag era so the
    operator page and the message read one shape across the change.

    state: 'none' (no replay/scores) · 'thin' (a top fifth smaller than
    MIN_CHECKED) · 'beats' (at least MIN_FACTOR times the trade's rate) ·
    'no_better'."""
    if fc is None:
        return {'state': 'none'}
    st, generated = fc
    out = {'checked': st['k'], 'hits': st['hits'],
           'precision': st['hit'], 'base': st['base'],
           'pool_base': st.get('pool_base'), 'auc': st.get('auc'),
           'graded': st['n'],
           'recall': st['recall'], 'generated': generated}
    if st['k'] < MIN_CHECKED:
        return {**out, 'state': 'thin'}
    out['factor'] = (st['hit'] / st['base']) if st['base'] else None
    beats = st['beats_base'] and (out['factor'] or 0) >= MIN_FACTOR
    return {**out, 'state': 'beats' if beats else 'no_better'}


def level_tile(lv):
    """The fifth figure — only when there is an advantage to show. A trade
    without one keeps four tiles and the section below says why."""
    if lv.get('state') != 'beats' or not lv.get('factor'):
        return ''
    return fig(f'{factor_de(lv["factor"])}-fach',
               'so oft trifft unsere Auswahl, verglichen mit Zufall')


# Where the site build leaves the per-trade verdicts for the operator page and
# the invitation message: one small file beside the data, rewritten by every
# build. Read with `forecasts(data_dir)`; never computed at request time (the
# replay slice needs the whole lot table).
FORECAST_FILE = 'trade_forecast.json'


def forecasts(data_dir):
    """{trade name: level dict + 'slug'} from the last site build, or {}."""
    p = Path(data_dir) / FORECAST_FILE
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except ValueError:
        return {}


def trades_of_titles(titles, trades=None):
    """Which trade pages a firm belongs to, from the titles of its reference
    wins: a trade counts when its words hit at least half of the titles —
    the same title word-match (`market.match`, scope 'core') that puts a lot
    on a page, applied to the firm's own lots. Strongest first.
    -> [(trade name, hits)]"""
    trades = trades if trades is not None else market.load_trades()
    folded = [market.ev.fold(str(t or '').casefold()) for t in titles or ()]
    folded = [t for t in folded if t]
    if not folded:
        return []
    need = max(1, (len(folded) + 1) // 2)
    out = []
    for name, t in trades.items():
        hits = sum(1 for s in folded
                   if any(w in s for w in t['terms'])
                   and not any(x in s for x in t['exclude']))
        if hits >= need:
            out.append((name, hits))
    out.sort(key=lambda x: (-x[1], x[0]))
    return out


def overall_html(lv):
    """One sentence on the whole record, all trades together — printed in
    every state of the section, because a trade with too few checked
    alarms still belongs to a product with a measured record. Nothing when
    the overall itself is thin or absent."""
    if not lv or lv.get('state') != 'beats':
        return ''
    n = f'{lv["graded"]:,}'.replace(',', '.')
    k = f'{lv["checked"]:,}'.replace(',', '.')
    return (f'<p>Über alle Gewerke zusammen: {n} Lose konnten bisher gegen '
            f'das veröffentlichte Ergebnis geprüft werden. Vom obersten '
            f'Fünftel unserer Reihenfolge ({k} Lose) endeten '
            f'{pct_de(lv["precision"])} mit höchstens einem Angebot; über '
            f'alle ausgewerteten Lose im Register sind es '
            f'{pct_de(lv["base"])}. Unsere Auswahl trifft also '
            f'{factor_de(lv["factor"])}-mal so oft.</p>')


def forecast_section(fc, all_fc=None):
    """Three states, and the page says which one it is in; under each, the
    overall record (`overall_html`).

    The unflattering one is printed rather than dropped (operator's call,
    2026-08-11): a page that shows the market and then admits the forecast is
    not beating the base rate here is auditable, and silent omission is not.
    It also says something true — in that trade the value is the coverage,
    not the forecast."""
    overall = overall_html(level(all_fc))
    if fc is None:
        return ('<h2>Wie gut trifft unsere Einschätzung?</h2>'
                '<p>Für dieses Gewerk liegen noch nicht genug ausgewertete '
                'Hinweise vor, um das zu belegen. Solange das so ist, '
                'behaupten wir dazu nichts.</p>' + overall)
    st, generated = fc
    checked, hits = st['k'], st['hits']
    if checked < MIN_CHECKED:
        return ('<h2>Wie gut trifft unsere Einschätzung?</h2>'
                f'<p>Bisher konnten erst {st["n"]} Lose dieses Gewerks gegen '
                f'ein veröffentlichtes Ergebnis geprüft werden — zu wenige '
                f'für eine belastbare Quote. Wir nennen sie erst, wenn das '
                f'oberste Fünftel unserer Reihenfolge mindestens '
                f'{MIN_CHECKED} Lose umfasst.</p>' + overall)
    prec, base = st['hit'], st['base']
    # the same rate as the tile at the top, by construction (rank_stats):
    # the sentence says so, and a reader can check it
    lead = (f'<p>Wir ordnen jede Ausschreibung danach, wie wahrscheinlich '
            f'sie mit höchstens einem Angebot endet. {st["n"]} Lose dieses '
            f'Gewerks konnten bisher gegen das veröffentlichte Ergebnis '
            f'geprüft werden. Vom <strong>obersten Fünftel</strong> dieser '
            f'Reihenfolge ({checked} Lose) endeten <strong>{hits} mit '
            f'höchstens einem Angebot ({pct_de(prec)})</strong>. '
            f'Im Gewerk insgesamt sind es {pct_de(base)} — die Kennzahl '
            f'oben.</p>')
    # one verdict for the sentence, the tile, the operator page and the
    # message: `level`'s
    if level(fc)['state'] != 'beats':
        verdict = ('<p><strong>Damit trifft unsere Auswahl hier nicht '
                   'besser als der Durchschnitt des Gewerks.</strong> Wir '
                   'sagen das, statt es wegzulassen: in diesem Gewerk liegt '
                   'unser Nutzen derzeit in der vollständigen Übersicht, '
                   'nicht in der Vorhersage.</p>')
    else:
        verdict = (f'<p>Das ist das {factor_de(prec / base)}-Fache der Quote '
                   f'des Gewerks.</p>')
    # the sorting check, without choosing a cut: AUC in plain words. Printed
    # in both verdicts — a reader who can divide deserves the whole picture.
    auc_p = ('' if st.get('auc') is None else
             f'<p>Die Sortierung insgesamt: nimmt man ein Los, das mit '
             f'höchstens einem Angebot endete, und eines mit mehreren, dann '
             f'steht das einsame in {100 * st["auc"]:.0f} von 100 Fällen '
             f'weiter oben in unserer Reihenfolge (Zufall wäre 50, perfekt '
             f'100 — der Fachbegriff ist AUC).</p>')
    # recall: of the lonely lots we could check, the share our top fifth
    # held. The sentence names the checked pool, not "all lots of the trade".
    rec = ('' if st['recall'] is None else
           f'<p class="muted">Umgekehrt gilt: wir finden nicht alle. Von '
           f'den geprüften Losen, die mit höchstens einem Angebot endeten, '
           f'stand {pct_de(st["recall"])} in unserem obersten Fünftel.</p>')
    return ('<h2>Wie gut trifft unsere Einschätzung?</h2>' + lead + verdict
            + auc_p + rec + overall +
            f'<p class="muted">Grundlage ist ein Rücktest: die Historie wird '
            f'so nachgespielt, wie das System sie damals gesehen hätte, und '
            f'jede Einschätzung gegen das später veröffentlichte Ergebnis '
            f'geprüft. Gezählt werden nur Lose, deren Frist lange genug '
            f'zurückliegt, dass das Ergebnis vorliegt. Stand des Rücktests: '
            f'{esc(generated)}.</p>')


def fig(value, label):
    """One figure card. The classes are the stylesheet's own — `.figs/.fig`
    with `.n` and `.l`. Getting this wrong is not a cosmetic slip: unknown
    class names render as bare divs, so all four numbers run together into one
    unreadable line, which is exactly how this shipped the first time."""
    return (f'<div class="fig"><span class="n">{esc(value)}</span>'
            f'<span class="l">{esc(label)}</span></div>')


def dist_table(dist, n_awarded):
    """The bidder spread as a bar chart made of table cells — no image, no
    script, and it still reads as a table with a screen reader or with CSS
    off. Bars are scaled to the biggest bucket rather than to 100 %, or a
    thin market's shape flattens into nothing."""
    peak = max((share for _, _, share in dist), default=0) or 1
    rows = ''.join(
        f'<tr><th scope="row">{esc(label)}</th>'
        f'<td class="num">{n}</td>'
        f'<td class="num">{100 * share:.0f} %</td>'
        # an empty bucket gets no bar at all: the stylesheet's min-width
        # would otherwise draw a 2px stub, which reads as "a few" rather
        # than "none"
        f'<td class="barcell">' + (
            f'<span class="bar" style="width:{100 * share / peak:.1f}%">'
            f'</span>' if n else '') + '</td></tr>'
        for label, n, share in dist)
    return (f'<table class="dist"><caption class="muted">Angebote je Los, '
            f'über {n_awarded} ausgewertete Lose</caption>'
            f'<tbody>{rows}</tbody></table>')


def page(name, slug, f, fc=None, all_fc=None):
    """One trade page. Short on purpose: a page of four true numbers ranks
    safely, a padded one is what gets a domain demoted (TRADE_PAGES.md 5)."""
    today = date.today()
    stand = f'{MONTHS_DE[today.month - 1]} {today.year}'
    figs = ''.join([
        fig(f'{f["per_month"]:.0f}', 'Lose pro Monat'),
        fig(f'{money_de(f["median_award"])} €', 'Median-Auftragswert'),
        fig(f'{100 * f["low_bid"]:.0f} %', 'höchstens ein Angebot'),
        fig(f'{money_de(f["year_scope"])} €', 'Volumen pro Jahr, überschlägig'),
        level_tile(level(fc)),
    ])
    return (
        f'<!doctype html>\n<html lang="de">\n<head>\n'
        f'<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>Öffentliche Ausschreibungen {esc(name)} — Marktzahlen | '
        f'Murara</title>\n'
        f'<meta name="description" content="Wie viel öffentliche Arbeit im '
        f'Gewerk {esc(name)} ausgeschrieben wird: {f["per_month"]:.0f} Lose '
        f'pro Monat, Median-Auftragswert '
        f'{money_de(f["median_award"])} €, '
        f'{100 * f["low_bid"]:.0f} % mit höchstens einem Angebot.">\n'
        f'<link rel="canonical" href="{BASE_URL}/gewerke/{slug}/">\n'
        f'<link rel="stylesheet" href="../../style.css">\n'
        f'</head>\n<body>\n\n'
        f'<header class="bar">\n'
        f'  <a class="brand" href="../../index.html">Murara</a>\n'
        f'  <span class="tag">Ausschreibungen mit wenig Wettbewerb</span>\n'
        f'</header>\n\n'
        f'<h1>Öffentliche Ausschreibungen für {esc(name)}</h1>\n\n'
        f'<p class="lede">Wie groß der öffentliche Markt in diesem Gewerk '
        f'ist, was ein Los im Mittel wert ist und wie oft nur ein oder gar '
        f'kein Angebot eingeht — berechnet aus dem amtlichen '
        f'EU-Vergaberegister.</p>\n\n'
        f'<div class="figs">{figs}</div>\n\n'
        f'<h2>Wie viele bieten mit?</h2>\n'
        f'<p>Im Mittel bewerben sich {f["median_bidders"]:.0f} Bieter auf ein '
        f'Los dieses Gewerks. So verteilt sich das:</p>\n'
        f'{dist_table(f["dist"], f["n_awarded"])}\n\n'
        f'{forecast_section(fc, all_fc)}\n\n'
        f'<h2>Woher die Zahlen kommen</h2>\n'
        f'<p>Quelle ist <em>Tenders Electronic Daily</em> (TED), das '
        f'Amtsblatt der EU für öffentliche Vergaben. Ein Los zählt zu diesem '
        f'Gewerk, wenn sein Titel es benennt. In die Bieterzahlen gehen nur '
        f'Lose ein, für die eine Vergabebekanntmachung mit Bieteranzahl '
        f'vorliegt — die erscheint typischerweise rund drei Monate nach '
        f'Angebotsfrist, weshalb die jüngsten Monate ausgenommen sind. '
        f'Alle Bieterzahlen — auch die Kennzahl oben und die Prüfquoten — '
        f'stammen aus den letzten {f["bidder_months"]} Monaten, für die '
        f'Ergebnisse vollständig vorliegen: der Wettbewerb verändert sich '
        f'von Jahr zu Jahr, und eine Quote über die ganze Historie würde '
        f'einen Markt beschreiben, den es so nicht mehr gibt.</p>\n'
        f'<p class="muted">Stand: {stand}, Marktvolumen berechnet über '
        f'{f["months"]} vollständig erfasste Monate.</p>\n\n'
        f'<div class="note">\n'
        f'  <h2 style="margin-top:0">Welche davon passen zu Ihrem Betrieb?</h2>\n'
        f'  <p>Eine Zeile mit Ihrem Firmennamen genügt. Wir sehen nach, was '
        f'Sie in den letzten Jahren gewonnen haben, und sagen Ihnen, welche '
        f'Ausschreibungen diese Woche dazu gepasst hätten. Kostenlos und '
        f'unverbindlich.</p>\n'
        f'  <p><a href="mailto:{CONTACT}"><strong>{CONTACT}</strong></a></p>\n'
        f'</div>\n\n'
        f'<footer>\n'
        f'  <a href="../../index.html">Startseite</a> ·\n'
        f'  <a href="../index.html">Marktzahlen nach Gewerk</a> ·\n'
        f'  <a href="../../impressum/index.html">Impressum</a> ·\n'
        f'  <a href="../../datenschutz/index.html">Datenschutz</a>\n'
        f'</footer>\n\n'
        f'</body>\n</html>\n')


def _by_group(built, groups=None):
    """-> [(group or None, [(name, slug), ...])], the groups in `trades.txt`
    order and the trades alphabetical inside each. One (None, everything)
    block when the file names no groups, which is the old list exactly."""
    groups = groups or {}
    present = {groups.get(name) for name, _ in built}
    order = [g for g in dict.fromkeys(groups.values()) if g in present]
    if None in present:
        order.append(None)          # a trade with no group goes last
    return [(g, [(n, s) for n, s in built if groups.get(n) == g])
            for g in order]


def index_page(built, groups=None):
    """The plain list. Nothing else on it — it exists so the trade pages are
    reachable, not to be read (TRADE_PAGES.md 4).

    `groups` is {trade name: group name} from `trades.txt`, in file order.
    With it the list is broken under headings — 54 trades in one column asks
    a software firm to read past 40 Gewerke to find itself. Without it, or
    for a trade that carries no group, the list is exactly as before: the
    heading is a display device, nothing selects by it."""
    lists = []
    for group, names in _by_group(built, groups):
        items = '\n'.join(
            f'  <li><a href="{slug}/index.html">{esc(name)}</a></li>'
            for name, slug in names)
        head = f'<h2>{esc(group)}</h2>\n' if group else ''
        lists.append(f'{head}<ul class="plain">\n{items}\n</ul>')
    body = '\n\n'.join(lists)
    return (
        f'<!doctype html>\n<html lang="de">\n<head>\n'
        f'<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>Marktzahlen nach Gewerk | Murara</title>\n'
        f'<meta name="description" content="Öffentliche Ausschreibungen nach '
        f'Gewerk — Bau und IT: Lose pro Monat, Auftragswerte und wie oft kaum '
        f'jemand mitbietet.">\n'
        f'<link rel="canonical" href="{BASE_URL}/gewerke/">\n'
        f'<link rel="stylesheet" href="../style.css">\n'
        f'</head>\n<body>\n\n'
        f'<header class="bar">\n'
        f'  <a class="brand" href="../index.html">Murara</a>\n'
        f'  <span class="tag">Ausschreibungen mit wenig Wettbewerb</span>\n'
        f'</header>\n\n'
        f'<h1>Marktzahlen nach Gewerk</h1>\n'
        f'<p class="lede">Öffentliche Ausschreibungen in Deutschland, je '
        f'Gewerk: wie viel ausgeschrieben wird, was ein Los wert ist und wie '
        f'oft kaum jemand mitbietet.</p>\n\n'
        f'{body}\n\n'
        f'<footer>\n'
        f'  <a href="../index.html">Startseite</a> ·\n'
        f'  <a href="../impressum/index.html">Impressum</a> ·\n'
        f'  <a href="../datenschutz/index.html">Datenschutz</a>\n'
        f'</footer>\n\n'
        f'</body>\n</html>\n')


def sitemap(built):
    urls = [f'{BASE_URL}/', f'{BASE_URL}/impressum/',
            f'{BASE_URL}/datenschutz/', f'{BASE_URL}/gewerke/']
    urls += [f'{BASE_URL}/gewerke/{slug}/' for _, slug in built]
    body = '\n'.join(f'  <url><loc>{u}</loc></url>' for u in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f'{body}\n</urlset>\n')


def publish(out, site=SITE):
    """Copy the hand-written part of the site into `out`.

    `site/` is source — files a person edits, committed. `out` is the built
    site, which is what gets uploaded. Keeping them apart is what lets the
    generated pages stay out of git AND out of the image: in the container
    `site/` is `/app`, read-only by design, while `out` lives on the mounted
    volume and survives the container."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for src in Path(site).rglob('*'):
        if src.is_dir() or 'gewerke' in src.parts:
            continue
        dst = out / src.relative_to(site)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


CURRENT = 'current'          # <public>/current -> site-XXXX, what the edge serves
BUILD_PREFIX = 'site-'       # <public>/site-XXXX, one complete site each


def _point(link, target_name):
    """Make `link` a symlink to its sibling `target_name`, atomically replacing
    whatever `link` was before. A relative target, so the link resolves the
    same on the host and inside the edge container that mounts the directory.

    Windows without the symlink privilege gets a directory junction instead
    (the operator's laptop); on the server it is a plain symlink."""
    import os
    tmp = link.with_name(link.name + '.tmp')
    if tmp.is_symlink() or tmp.exists():
        _unlink_link(tmp)
    try:
        tmp.symlink_to(target_name, target_is_directory=True)
    except OSError:
        if sys.platform != 'win32':
            raise
        import _winapi
        _winapi.CreateJunction(str(link.parent / target_name), str(tmp))
    # rename over the old link: one syscall, so a request in flight sees the
    # old site or the new one, never neither. Windows cannot rename over a
    # directory link at all; there (the laptop, no edge) it is unlink + rename.
    try:
        os.replace(tmp, link)
    except OSError:
        if sys.platform != 'win32':
            raise
        if link.is_symlink() or link.exists():
            _unlink_link(link)
        os.rename(tmp, link)


def _unlink_link(p):
    """Remove a symlink or junction, never what it points to."""
    import os
    try:
        os.unlink(p)
    except OSError:
        os.rmdir(p)               # a Windows directory link


def release(public, write, prefix=BUILD_PREFIX):
    """Publish a new site under `public` without ever serving a partial one.

    `write(dir)` fills a fresh, empty directory with the whole site. Then:

      1. `public/current` is pointed at that directory (atomic rename);
      2. the directory it pointed at before is deleted, and so is any other
         `site-*` left by a build that died halfway;
      3. anything else lying in `public/` — the flat layout that was served
         before `current` existed — is swept, but only once a `current` was
         already there when this started, so the edge that still serves the
         flat files keeps them until it has been recreated to serve `current`.

    At rest, `public/` holds `current` and the one directory it points to.
    Nothing accumulates. If `write` raises, the new directory is removed and
    `current` still points at the last complete site (operator, 2026-08-15).

    The edge bind-mounts `public/` itself, never a child of it, and this
    function never deletes or recreates `public/` — a bind mount follows the
    inode, so removing and recreating the mounted directory (the old
    `shutil.rmtree(out)`) leaves the container serving a deleted directory.

    -> the path of the directory now served."""
    import os
    import tempfile
    public = Path(public)
    public.mkdir(parents=True, exist_ok=True)
    link = public / CURRENT
    had_current = link.is_symlink() or link.exists()
    before = os.readlink(link) if link.is_symlink() else None

    new = Path(tempfile.mkdtemp(prefix=prefix, dir=public))
    new.chmod(0o755)                # mkdtemp gives 0700; the edge is another uid
    try:
        write(new)
    except BaseException:
        shutil.rmtree(new, ignore_errors=True)
        raise
    _point(link, new.name)

    for entry in public.iterdir():
        if entry == link or entry == new:
            continue
        if entry.name == CURRENT + '.tmp':
            _unlink_link(entry)
            continue
        stale = entry.name.startswith(prefix) or (
            entry.name == before if before else False)
        if not stale and not had_current:
            continue            # the flat layout, still being served: next time
        if entry.is_symlink() or entry.is_junction():
            _unlink_link(entry)
        elif entry.is_file():
            entry.unlink()
        else:
            shutil.rmtree(entry)
    return new


def build(data_dir, out=None, dry_run=False, site=SITE, replay=None):
    """Build the whole site and release it under `out` (default
    `<data-dir>/public`) — see `release` for how; the served site is always
    `<out>/current/`.

    `replay` is the path to a `rewind_all.py` document; without one the pages
    carry no forecast claim.

    -> (built, skipped). `built` is [(name, slug)], `skipped` is
    [(name, n_awarded)] — the trades that cannot yet carry a page."""
    out = Path(out) if out else Path(data_dir) / 'public'
    lots = market.add_text(market.load_lots(data_dir))
    trades = market.load_trades()
    covered, mature, _ = market.coverage(lots)
    replay = replay_path(data_dir, replay)
    receipt = load_replay(replay)
    if receipt:
        print(f'[trades] forecast section from {replay} '
              f'(replay of {receipt.get("generated", "?")})')

    built, skipped, pages, verdicts = [], [], {}, {}
    # every forecast claim is measured against the market's own 0/1 rate —
    # store-wide here, the trade's tile below — never the replay pool's;
    # and only over the bidder window (the newest RECENT_MONTHS mature
    # months), in the slice and the base alike: one window for every
    # bidder number on a page
    recent = market.recent_mature(mature)
    base_all, _ = market.low_bid_rate(lots, recent)
    all_fc = rank_stats(receipt, base_all, lots=lots, months=recent)
    verdicts[ALL] = level(all_fc)
    for name, trade in sorted(trades.items()):
        sel = market.match(lots, trade, 'core')
        f = figures(lots, sel, covered, mature)
        if f is None:
            mat = lots[sel]
            mat = mat[mat.month.isin(recent) & mat.resolved]
            skipped.append((name, len(mat)))
            continue
        slug = slugify(name)
        built.append((name, slug))
        fc = rank_stats(receipt, f['low_bid'], lots=lots, sel=sel,
                        months=recent)
        pages[slug] = page(name, slug, f, fc, all_fc)
        # the level AND the page's market figures: the invitation message
        # quotes both, and a request must not recompute them
        verdicts[name] = {**level(fc), 'slug': slug,
                          'figures': {k: v for k, v in f.items() if k != 'dist'}}

    if dry_run:
        return built, skipped

    def write(root):
        # a fresh directory every time, so a trade that fell below the floor
        # simply is not written — nothing to delete
        publish(root, site)
        gew = root / 'gewerke'
        gew.mkdir(parents=True)
        for slug, text in pages.items():
            (gew / slug).mkdir()
            (gew / slug / 'index.html').write_text(text, encoding='utf-8')
        (gew / 'index.html').write_text(
            index_page(built, {n: t['group'] for n, t in trades.items()}),
            encoding='utf-8')
        # the sitemap can only be written here: it is the one file that has
        # to know both halves, the hand-written pages and the generated ones
        (root / 'sitemap.xml').write_text(sitemap(built), encoding='utf-8')

    release(out, write)
    # the operator's copy of the verdicts, beside the data (FORECAST_FILE)
    (Path(data_dir) / FORECAST_FILE).write_text(
        json.dumps(verdicts, ensure_ascii=False, indent=1), encoding='utf-8')
    return built, skipped


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    import config
    ap.add_argument('--data-dir', default=config.data_root())
    ap.add_argument('--out', default=None,
                    help='site root; the build lands in <out>/current/ '
                         '(default: <data-dir>/public)')
    ap.add_argument('--dry-run', action='store_true',
                    help='report who qualifies, write nothing')
    ap.add_argument('--replay', default=None, metavar='PATH',
                    help='a `python rewind_all.py --out PATH` document; '
                         'default <data-dir>/replay/latest.json if present, '
                         'else the pages make no forecast claim')
    args = ap.parse_args()
    out = Path(args.out) if args.out else Path(args.data_dir) / 'public'
    built, skipped = build(args.data_dir, out, args.dry_run,
                           replay=args.replay)
    print(f'[trades] {len(built)} trade pages'
          + (' (dry run, nothing written)' if args.dry_run else
             f'; site built -> {out / CURRENT}')
          + ('' if replay_path(args.data_dir, args.replay)
             else f'; no replay document ({REPLAY_FILE}), so no forecast '
                  'section'))
    if skipped:
        print(f'[trades] {len(skipped)} below the floor of {MIN_AWARDED} '
              f'awarded lots, no page:')
        for name, n in sorted(skipped, key=lambda x: -x[1]):
            print(f'           {name:44s} {n:3d}')


if __name__ == '__main__':
    main()
