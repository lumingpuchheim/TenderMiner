"""Murara's public site — doc/TRADE_PAGES.md.

    python trade_pages.py                     # -> <data-dir>/public/
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
and nothing is written where the container would discard it. Upload
`<data-dir>/public/`, never `site/`.

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

    Returns None when the trade cannot honestly carry a page — fewer than
    MIN_AWARDED awarded lots in mature months (doc/TRADE_PAGES.md 3)."""
    sub = lots[sel]
    n_months = max(len(covered), 1)
    cov = sub[sub.month.isin(covered)]
    mat = sub[sub.month.isin(mature) & sub.resolved]
    if len(mat) < MIN_AWARDED:
        return None
    aw = sub.award_value.dropna()
    per_month = len(cov) / n_months
    med = float(aw.median()) if len(aw) else None
    zero = int((mat.n_tenders == 0).sum())
    one = int((mat.n_tenders == 1).sum())
    closed = float((mat.result_code == 'selec-n').mean()) \
        if 'result_code' in mat.columns else None
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
        'low_bid': (zero + one) / len(mat),
        'zero': zero, 'one': one,
        'median_bidders': float(mat.n_tenders.median()),
        'closed': closed,
        'months': n_months,
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


def load_replay(path):
    """A replay document from `rewind_all.py`, or None when there is none.

    There is no default path and no conventional filename on purpose: the
    replay is produced by `python rewind_all.py > somewhere.json`, the operator
    names that file, and this program is told where it is. No argument means
    no forecast claim, which is the state on a fresh checkout and the correct
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


def forecast_for(receipt, lots, sel):
    """-> (stats, generated) for this trade's slice of the replay, or None.

    `stats` is `loop.flag_stats`' dict — the SAME function the weekly report
    and the backtest's own table use. That sharing is deliberate: until live
    awards accumulate, the replayed number is the one quoted, and it must be
    the same statistic rather than a second implementation that agrees by
    coincidence.
    """
    if not receipt:
        return None
    keys = set(zip(lots.loc[sel, 'procedure_id'], lots.loc[sel, 'lot_id']))
    # `n_tenders is None` = examined but no award published yet. Those rows
    # carry the "examined" denominator for the operator's report and must not
    # reach a rate here: an uncheckable lot is neither a hit nor a miss.
    rows = [{'flag': r['flag'], 'label': int(r['n_tenders'] <= 1)}
            for r in receipt['lots']
            if r['n_tenders'] is not None
            and (r['procedure_id'], r['lot_id']) in keys]
    if not rows:
        return None
    from loop import flag_stats           # lazy: pulls the ML stack
    return flag_stats(rows), receipt.get('generated', '?')


def forecast_section(fc):
    """Three states, and the page says which one it is in.

    The unflattering one is printed rather than dropped (operator's call,
    2026-08-11): a page that shows the market and then admits the forecast is
    not beating the base rate here is auditable, and silent omission is not.
    It also says something true — in that trade the value is the coverage,
    not the forecast."""
    if fc is None:
        return ('<h2>Wie gut trifft unsere Einschätzung?</h2>'
                '<p>Für dieses Gewerk liegen noch nicht genug ausgewertete '
                'Hinweise vor, um das zu belegen. Solange das so ist, '
                'behaupten wir dazu nichts.</p>')
    st, generated = fc
    checked, hits = st['flagged'], st['tp']
    if checked < MIN_CHECKED or st['precision'] is None:
        return ('<h2>Wie gut trifft unsere Einschätzung?</h2>'
                f'<p>Bisher konnten erst {checked} unserer Hinweise in diesem '
                f'Gewerk gegen ein veröffentlichtes Ergebnis geprüft werden — '
                f'zu wenige für eine belastbare Quote. Wir nennen sie erst ab '
                f'{MIN_CHECKED}.</p>')
    prec, base = st['precision'], st['base']
    lead = (f'<p>Von {checked} Hinweisen, die wir in diesem Gewerk gegeben '
            f'haben und deren Ergebnis inzwischen veröffentlicht ist, endeten '
            f'<strong>{hits} mit höchstens einem Angebot '
            f'({pct_de(prec)})</strong>. '
            f'Ohne jede Einschätzung liegt die Quote im Gewerk bei '
            f'{pct_de(base)}.</p>')
    if not st['beats_base']:
        verdict = ('<p><strong>Damit trifft unsere Einschätzung hier nicht '
                   'besser als der Durchschnitt.</strong> Wir sagen das, '
                   'statt es wegzulassen: in diesem Gewerk liegt unser Nutzen '
                   'derzeit in der vollständigen Übersicht, nicht in der '
                   'Vorhersage.</p>')
    else:
        verdict = (f'<p>Das ist das {factor_de(prec / base)}-Fache der Quote '
                   f'ohne Einschätzung.</p>')
    rec = ('' if st['recall'] is None else
           f'<p class="muted">Umgekehrt gilt: wir finden nicht alle. Von '
           f'allen Losen dieses Gewerks, die mit höchstens einem Angebot '
           f'endeten, hatten wir {pct_de(st["recall"])} vorher genannt.</p>')
    return ('<h2>Wie gut trifft unsere Einschätzung?</h2>' + lead + verdict
            + rec +
            f'<p class="muted">Grundlage ist ein Rücktest: die Historie wird '
            f'so nachgespielt, wie das System sie damals gesehen hätte, und '
            f'jede Einschätzung gegen das später veröffentlichte Ergebnis '
            f'geprüft. Stand des Rücktests: {esc(generated)}.</p>')


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


def page(name, slug, f, fc=None):
    """One trade page. Short on purpose: a page of four true numbers ranks
    safely, a padded one is what gets a domain demoted (TRADE_PAGES.md 5)."""
    today = date.today()
    stand = f'{MONTHS_DE[today.month - 1]} {today.year}'
    figs = ''.join([
        fig(f'{f["per_month"]:.0f}', 'Lose pro Monat'),
        fig(f'{money_de(f["median_award"])} €', 'Median-Auftragswert'),
        fig(f'{100 * f["low_bid"]:.0f} %', 'höchstens ein Angebot'),
        fig(f'{money_de(f["year_scope"])} €', 'Volumen pro Jahr, überschlägig'),
    ])
    closed = ''
    if f['closed'] is not None:
        closed = (f'<p>Ohne Zuschlag beendet: '
                  f'{100 * f["closed"]:.0f} % der ausgewerteten Lose.</p>')
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
        f'{dist_table(f["dist"], f["n_awarded"])}\n'
        f'<p>Auf {f["zero"]} der {f["n_awarded"]} ausgewerteten Lose ging gar '
        f'kein Angebot ein, auf {f["one"]} genau eines — zusammen '
        f'{100 * f["low_bid"]:.0f} %.</p>\n'
        f'{closed}\n\n'
        f'{forecast_section(fc)}\n\n'
        f'<h2>Woher die Zahlen kommen</h2>\n'
        f'<p>Quelle ist <em>Tenders Electronic Daily</em> (TED), das '
        f'Amtsblatt der EU für öffentliche Vergaben. Ein Los zählt zu diesem '
        f'Gewerk, wenn sein Titel es benennt. In die Bieterzahlen gehen nur '
        f'Lose ein, für die eine Vergabebekanntmachung mit Bieteranzahl '
        f'vorliegt — die erscheint typischerweise rund drei Monate nach '
        f'Angebotsfrist, weshalb die jüngsten Monate ausgenommen sind.</p>\n'
        f'<p class="muted">Stand: {stand}, berechnet über {f["months"]} '
        f'vollständig erfasste Monate.</p>\n\n'
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


def index_page(built):
    """The plain list. Nothing else on it — it exists so the trade pages are
    reachable, not to be read (TRADE_PAGES.md 4)."""
    items = '\n'.join(
        f'  <li><a href="{slug}/index.html">{esc(name)}</a></li>'
        for name, slug in built)
    return (
        f'<!doctype html>\n<html lang="de">\n<head>\n'
        f'<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>Marktzahlen nach Gewerk | Murara</title>\n'
        f'<meta name="description" content="Öffentliche Bauvergaben nach '
        f'Gewerk: Lose pro Monat, Auftragswerte und wie oft kaum jemand '
        f'mitbietet.">\n'
        f'<link rel="canonical" href="{BASE_URL}/gewerke/">\n'
        f'<link rel="stylesheet" href="../style.css">\n'
        f'</head>\n<body>\n\n'
        f'<header class="bar">\n'
        f'  <a class="brand" href="../index.html">Murara</a>\n'
        f'  <span class="tag">Ausschreibungen mit wenig Wettbewerb</span>\n'
        f'</header>\n\n'
        f'<h1>Marktzahlen nach Gewerk</h1>\n'
        f'<p class="lede">Öffentliche Bauvergaben in Deutschland, je Gewerk: '
        f'wie viel ausgeschrieben wird, was ein Los wert ist und wie oft kaum '
        f'jemand mitbietet.</p>\n\n'
        f'<ul class="plain">\n{items}\n</ul>\n\n'
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


def build(data_dir, out=None, dry_run=False, site=SITE, replay=None):
    """Build the whole site into `out` (default `<data-dir>/public`).

    `replay` is the path to a `rewind_all.py` document; without one the pages
    carry no forecast claim.

    -> (built, skipped). `built` is [(name, slug)], `skipped` is
    [(name, n_awarded)] — the trades that cannot yet carry a page."""
    out = Path(out) if out else Path(data_dir) / 'public'
    lots = market.add_text(market.load_lots(data_dir))
    trades = market.load_trades()
    covered, mature, _ = market.coverage(lots)
    receipt = load_replay(replay)

    built, skipped, pages = [], [], {}
    for name, trade in sorted(trades.items()):
        sel = market.match(lots, trade, 'core')
        f = figures(lots, sel, covered, mature)
        if f is None:
            mat = lots[sel]
            mat = mat[mat.month.isin(mature) & mat.resolved]
            skipped.append((name, len(mat)))
            continue
        slug = slugify(name)
        built.append((name, slug))
        pages[slug] = page(name, slug, f, forecast_for(receipt, lots, sel))

    if dry_run:
        return built, skipped

    if out.exists():
        shutil.rmtree(out)   # a trade that fell below the floor must vanish
    publish(out, site)
    gew = out / 'gewerke'
    gew.mkdir(parents=True)
    for slug, text in pages.items():
        (gew / slug).mkdir()
        (gew / slug / 'index.html').write_text(text, encoding='utf-8')
    (gew / 'index.html').write_text(index_page(built), encoding='utf-8')
    # the sitemap can only be written here: it is the one file that has to
    # know both halves, the hand-written pages and the generated ones
    (out / 'sitemap.xml').write_text(sitemap(built), encoding='utf-8')
    return built, skipped


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    import config
    ap.add_argument('--data-dir', default=config.data_root())
    ap.add_argument('--out', default=None,
                    help='built site (default: <data-dir>/public)')
    ap.add_argument('--dry-run', action='store_true',
                    help='report who qualifies, write nothing')
    ap.add_argument('--replay', default=None, metavar='PATH',
                    help='a `python rewind_all.py > PATH` document; without it '
                         'the pages make no forecast claim')
    args = ap.parse_args()
    out = Path(args.out) if args.out else Path(args.data_dir) / 'public'
    built, skipped = build(args.data_dir, out, args.dry_run,
                           replay=args.replay)
    print(f'[trades] {len(built)} trade pages'
          + (' (dry run, nothing written)' if args.dry_run else
             f'; site built -> {out}')
          + ('' if args.replay else '; no --replay, so no forecast section'))
    if skipped:
        print(f'[trades] {len(skipped)} below the floor of {MIN_AWARDED} '
              f'awarded lots, no page:')
        for name, n in sorted(skipped, key=lambda x: -x[1]):
            print(f'           {name:44s} {n:3d}')


if __name__ == '__main__':
    main()
