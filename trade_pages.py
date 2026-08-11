"""Market pages per trade, for search — doc/TRADE_PAGES.md.

    python trade_pages.py                     # -> site/gewerke/
    python trade_pages.py --dry-run           # who qualifies, writes nothing

One page per trade at `/gewerke/<slug>`, carrying that trade's market figures
and nothing else, so a contractor searching for his own trade can find it and
learn something true when he arrives. The pages never link to each other: a
Maler is shown nothing about Elektro, which is the rule the whole design
exists to protect.

**Generated, unlike the rest of `site/`.** The operator's split (2026-08-11)
is hand-write what has no data in it, generate what does: `site/index.html`
is a file you edit, these are 28 pages of figures that move every week and
nobody maintains by hand. Output is committed, so "upload the site/ folder"
stays true and a moving number shows up in a diff.

**Every figure comes from `market.py`** — its loader, its coverage rule, its
`SMALL_SAMPLE` line. The public figure and the operator's figure must not be
able to drift apart, which they would the moment this file computed anything
itself.
"""

import argparse
import html
import re
import shutil
import sys
from datetime import date
from pathlib import Path

import market

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

SITE = Path(__file__).resolve().parent / 'site'
CONTACT = 'kontakt@tendermining.de'
BASE_URL = 'https://www.tendermining.de'

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
    }


def tile(value, label):
    return (f'<div class="tile"><div class="tile-value">{esc(value)}</div>'
            f'<div class="tile-label">{esc(label)}</div></div>')


def page(name, slug, f):
    """One trade page. Short on purpose: a page of four true numbers ranks
    safely, a padded one is what gets a domain demoted (TRADE_PAGES.md 5)."""
    today = date.today()
    stand = f'{MONTHS_DE[today.month - 1]} {today.year}'
    tiles = ''.join([
        tile(f'{f["per_month"]:.0f}', 'Lose pro Monat'),
        tile(f'{money_de(f["median_award"])} €', 'Median-Auftragswert'),
        tile(f'{100 * f["low_bid"]:.0f} %', 'mit höchstens einem Angebot'),
        tile(f'{money_de(f["year_scope"])} €', 'Volumen pro Jahr, überschlägig'),
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
        f'TenderMining</title>\n'
        f'<meta name="description" content="Wie viel öffentliche Arbeit im '
        f'Gewerk {esc(name)} ausgeschrieben wird: {f["per_month"]:.0f} Lose '
        f'pro Monat, Median-Auftragswert '
        f'{money_de(f["median_award"])} €, '
        f'{100 * f["low_bid"]:.0f} % mit höchstens einem Angebot.">\n'
        f'<link rel="canonical" href="{BASE_URL}/gewerke/{slug}/">\n'
        f'<link rel="stylesheet" href="../../style.css">\n'
        f'</head>\n<body>\n\n'
        f'<header class="bar">\n'
        f'  <a class="brand" href="../../index.html">TenderMining</a>\n'
        f'  <span class="tag">Ausschreibungen mit wenig Wettbewerb</span>\n'
        f'</header>\n\n'
        f'<h1>Öffentliche Ausschreibungen für {esc(name)}</h1>\n\n'
        f'<p class="lede">Wie groß der öffentliche Markt in diesem Gewerk '
        f'ist, was ein Los im Mittel wert ist und wie oft nur ein oder gar '
        f'kein Angebot eingeht — berechnet aus dem amtlichen '
        f'EU-Vergaberegister.</p>\n\n'
        f'<div class="tiles">{tiles}</div>\n\n'
        f'<h2>Wie oft kaum jemand mitbietet</h2>\n'
        f'<p>Von {f["n_awarded"]} ausgewerteten Losen dieses Gewerks gingen '
        f'bei {f["zero"]} gar kein und bei {f["one"]} genau ein Angebot ein '
        f'— zusammen {100 * f["low_bid"]:.0f} %. Im Mittel bewerben sich '
        f'{f["median_bidders"]:.0f} Bieter auf ein Los.</p>\n'
        f'{closed}\n\n'
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
        f'<title>Marktzahlen nach Gewerk | TenderMining</title>\n'
        f'<meta name="description" content="Öffentliche Bauvergaben nach '
        f'Gewerk: Lose pro Monat, Auftragswerte und wie oft kaum jemand '
        f'mitbietet.">\n'
        f'<link rel="canonical" href="{BASE_URL}/gewerke/">\n'
        f'<link rel="stylesheet" href="../style.css">\n'
        f'</head>\n<body>\n\n'
        f'<header class="bar">\n'
        f'  <a class="brand" href="../index.html">TenderMining</a>\n'
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


def build(data_dir, site=SITE, dry_run=False):
    """-> (built, skipped). `built` is [(name, slug)], `skipped` is
    [(name, n_awarded)] — the trades that cannot yet carry a page."""
    lots = market.add_text(market.load_lots(data_dir))
    trades = market.load_trades()
    covered, mature, _ = market.coverage(lots)

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
        pages[slug] = page(name, slug, f)

    if dry_run:
        return built, skipped

    out = Path(site) / 'gewerke'
    if out.exists():
        shutil.rmtree(out)          # a trade that fell below the floor must go
    out.mkdir(parents=True)
    for slug, text in pages.items():
        (out / slug).mkdir()
        (out / slug / 'index.html').write_text(text, encoding='utf-8')
    (out / 'index.html').write_text(index_page(built), encoding='utf-8')
    (Path(site) / 'sitemap.xml').write_text(sitemap(built), encoding='utf-8')
    return built, skipped


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    import config
    ap.add_argument('--data-dir', default=config.data_root())
    ap.add_argument('--site', default=SITE)
    ap.add_argument('--dry-run', action='store_true',
                    help='report who qualifies, write nothing')
    args = ap.parse_args()
    built, skipped = build(args.data_dir, args.site, args.dry_run)
    print(f'[trades] {len(built)} pages'
          + (' (dry run, nothing written)' if args.dry_run else
             f' -> {Path(args.site) / "gewerke"}'))
    if skipped:
        print(f'[trades] {len(skipped)} below the floor of {MIN_AWARDED} '
              f'awarded lots, no page:')
        for name, n in sorted(skipped, key=lambda x: -x[1]):
            print(f'           {name:44s} {n:3d}')


if __name__ == '__main__':
    main()
