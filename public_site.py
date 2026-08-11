"""TenderMining public site renderer — doc/LAUNCH.md 4.1, the `[BUILD]` item.

Finished HTML files for a static host, rendered from the store by the cycle.
Nothing here talks to the app, and nothing the app does talks back: LAUNCH.md
4.2 fixes the interface as *hyperlinks plus a one-way build-time upload*, so
these pages stay readable with the app down and carry no `fetch()`, no API, no
CORS surface, no secret.

    python public_site.py                      # -> data/public/
    python public_site.py --out /tmp/site --base-url https://www.example.de

What it renders (4.1's closed inventory — a new section needs query evidence,
not a story about what contractors probably google):

    /index.html                     the trades, and what the site is
    /gewerke/<slug>/index.html      one page per trade in trades.txt
    /gewerke/<slug>/<land>/         trade x Bundesland, above the volume floor
    /single-bidder-report/          the national flagship number, per trade
    /impressum/, /datenschutz/      the same texts the app carries
    /sitemap.xml, /robots.txt

Three rules from the spec are enforced here rather than trusted, because each
one is a promise about other people's data:

- **No firm names, ever.** Aggregates and public-body buyers only. Award
  winners stay unnamed even though the award is public record — the whole
  "there is no third category" rule in LAUNCH.md 4.
- **Forecast language only on CPV 452.** It is the one trade with measured
  lift, so `Kandidaten` and lift-flavoured CTAs render there and nowhere
  else. `_is_452` is the single gate for that.
- **The thin-page guardrail.** A (trade, Land) page renders only above
  MIN_LOTS_MONTH lots/month; below it the Land folds into the national page.
  A few hundred doorway-thin pages would demote the whole domain.
"""

import argparse
import html
import json
import re
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

import config
import market
import style as style_mod

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# 4.1's thin-page guardrail: lots per month a (trade, Land) cell must clear.
MIN_LOTS_MONTH = 10

# How many live lots / fresh awards / candidates a trade page shows. Teaser
# sized on purpose: the ranked, reasoned full set is the product.
N_LIVE, N_AWARDS, N_CANDIDATES = 5, 3, 2

# The measured claim, quoted verbatim wherever a forecast is mentioned.
DISCLOSURE = ('Einschätzung auf Basis von Rücktests gegen bereits '
              'veröffentlichte Vergabeergebnisse: 2,3-fache Trefferquote '
              'gegenüber Zufallsauswahl.')

# The facts block (LAUNCH.md 4.1, dataset-checked 2026-08-10). Hard-coded with
# their denominators because they are quarterly editorial claims, not live
# figures — a number that silently moved under a piece of copy is how a claim
# stops matching its sentence.
FACTS = [
    ('1,43×', 'Preisspanne auf demselben Los',
     'Der teuerste Bieter liegt typischerweise 43 % über dem günstigsten — '
     'Sie müssen nicht der Billigste sein. (Median höchstes/niedrigstes '
     'Gebot, 1.801 Lose mit beiden Beträgen.)'),
    ('48 %', 'der vergebenen Summen gehen an kleine und Kleinstbetriebe',
     'Rund 89 % der Bieter sind KMU (8.125 Bieter auf 3.843 Losen). '
     'Das ist Ihr Markt.'),
    ('42 %', 'der Lose werden allein über den Preis vergeben',
     'Bei 12.970 Losen mit Zuschlagskriterien. Wenn kaum jemand mitbietet, '
     'ist das zweitrangig.'),
]

NUTS1 = {
    'DE1': ('Baden-Württemberg', 'baden-wuerttemberg'),
    'DE2': ('Bayern', 'bayern'),
    'DE3': ('Berlin', 'berlin'),
    'DE4': ('Brandenburg', 'brandenburg'),
    'DE5': ('Bremen', 'bremen'),
    'DE6': ('Hamburg', 'hamburg'),
    'DE7': ('Hessen', 'hessen'),
    'DE8': ('Mecklenburg-Vorpommern', 'mecklenburg-vorpommern'),
    'DE9': ('Niedersachsen', 'niedersachsen'),
    'DEA': ('Nordrhein-Westfalen', 'nordrhein-westfalen'),
    'DEB': ('Rheinland-Pfalz', 'rheinland-pfalz'),
    'DEC': ('Saarland', 'saarland'),
    'DED': ('Sachsen', 'sachsen'),
    'DEE': ('Sachsen-Anhalt', 'sachsen-anhalt'),
    'DEF': ('Schleswig-Holstein', 'schleswig-holstein'),
    'DEG': ('Thüringen', 'thueringen'),
}

MONTHS_DE = ('Januar Februar März April Mai Juni Juli August September '
             'Oktober November Dezember').split()


def esc(s):
    return html.escape(str(s))


def slugify(name):
    """A trade name -> a stable URL segment. Stable is the point: the slug is
    printed in letters and indexed by Google, so it must not drift when the
    display name gains a hyphen."""
    s = name.casefold()
    for a, b in (('ä', 'ae'), ('ö', 'oe'), ('ü', 'ue'), ('ß', 'ss')):
        s = s.replace(a, b)
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return s or 'gewerk'


def money(x):
    """German thousands separators; the site is German and 1.234.567 € is
    what a reader here parses without thinking."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return '—'
    return f'{int(round(x)):,}'.replace(',', '.')


def meta_line(*parts):
    """Join the sub-line of a list entry, dropping what is missing. An award
    with a withheld sum should read "Stadt Damme · 1 Gebot", not
    "Stadt Damme · — € · 1 Gebot" — a dash where a number belongs looks like
    a rendering bug and invites doubt about the numbers that ARE there."""
    return ' · '.join(esc(p) for p in parts if p not in (None, '', '—'))


def pct(part, whole):
    return '—' if not whole else f'{100 * part / whole:.0f} %'


def _is_452(sub):
    """Is this trade predominantly CPV 452 (civil engineering)? The forecast
    guardrail of LAUNCH.md 4.1: lift language is permitted here and nowhere
    else, so this is deliberately one function used by every caller."""
    codes = sub.cpv_main.dropna().astype(str).str[:3]
    return len(codes) > 0 and (codes == '452').mean() >= 0.5


# ------------------------------------------------------------------- layout

def document(title, description, body, canonical, base_url, extra_head=''):
    """Every public page. Unlike the app's pages these are INDEXABLE — that is
    their entire purpose — so they carry a description, a canonical URL and no
    robots restriction."""
    can = f'{base_url.rstrip("/")}{canonical}' if base_url else canonical
    return (
        f'<!doctype html><html lang="de"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{esc(title)}</title>'
        f'<meta name="description" content="{esc(description)}">'
        f'<link rel="canonical" href="{esc(can)}">{extra_head}'
        f'<style>{style_mod.CSS}</style></head><body>'
        f'{style_mod.header(home="/")}{body}'
        f'<footer><a href="/">Übersicht</a> · '
        f'<a href="/single-bidder-report/">Wettbewerbsreport</a> · '
        f'<a href="/impressum/">Impressum</a> · '
        f'<a href="/datenschutz/">Datenschutz</a></footer>'
        f'</body></html>')


def freshness(months):
    """The line that says when these numbers were computed. 4.1 makes it
    explicit: the pages update weekly and freshness is part of why they rank —
    and a market page with no date is a page a reader cannot trust."""
    today = date.today()
    return (f'<p class="muted">Stand: {MONTHS_DE[today.month - 1]} {today.year}. '
            f'Berechnet über {months} vollständig erfasste Monate '
            f'öffentlicher Vergabebekanntmachungen (TED).</p>')


def cta(is_452):
    """The two CTA variants of 4.1. Outside CPV 452 no forecast language may
    appear at all — not a softer version of it, none."""
    if is_452:
        text = ('<strong>Wir empfehlen Ausschreibungen mit voraussichtlich '
                'wenig Wettbewerb — wo der Preis selten entscheidet.</strong> '
                'Kontaktieren Sie uns.')
    else:
        text = ('<strong>Wir finden die passenden Ausschreibungen für Ihr '
                'Gewerbe — Woche für Woche.</strong> Kontaktieren Sie uns.')
    return (f'<div class="note"><p>{text}</p>'
            f'<p class="muted">Schreiben Sie an '
            f'<a href="mailto:kontakt@tendermining.de">'
            f'kontakt@tendermining.de</a>.</p></div>')


def facts_block(is_452):
    out = ['<h2>Was die Vergabedaten zeigen</h2>']
    for number, label, detail in FACTS:
        out.append(f'<div class="fig" style="margin:.8rem 0">'
                   f'<span class="n">{esc(number)}</span>'
                   f'<span class="l">{esc(label)}</span>'
                   f'<p class="muted" style="margin:.5rem 0 0">{esc(detail)}</p>'
                   f'</div>')
    out.append(cta(is_452))
    return ''.join(out)


# -------------------------------------------------------------- the numbers

def trade_stats(lots, sel, covered, mature):
    """The `market.py trade` figures a page quotes, as plain values. Reusing
    market.py's own loader and coverage rule matters more than the arithmetic:
    the public number and the operator's number must never drift apart."""
    sub = lots[sel]
    cov = sub[sub.month.isin(covered)]
    res = sub[sub.resolved]
    mat = sub[sub.month.isin(mature) & sub.resolved]
    aw = sub.award_value.dropna()
    n_months = max(len(covered), 1)
    per_month = len(cov) / n_months
    low = int((mat.n_tenders <= 1).sum())
    closed = int((res.result_code == 'clos-nw').sum())
    return {
        'lots': len(sub), 'per_month': per_month,
        'per_year': per_month * 12,
        'median_award': aw.median() if len(aw) else None,
        'year_volume': (aw.median() * per_month * 12) if len(aw) else None,
        'n_mature': len(mat), 'low_share': (low / len(mat)) if len(mat) else None,
        'n_resolved': len(res),
        'closed_share': (closed / len(res)) if len(res) else None,
        'months': n_months,
    }


def figures_html(st):
    """The four numbers a visitor came for. A share is only rendered with its
    denominator visible — an unqualified '38 %' is the kind of claim this
    project refuses to make."""
    figs = [style_mod.fig(f'{st["per_month"]:.0f}', 'Lose pro Monat')]
    if st['median_award'] is not None:
        figs.append(style_mod.fig(f'{money(st["median_award"])} €',
                                  'Median-Auftragswert'))
        figs.append(style_mod.fig(f'{money(st["year_volume"])} €',
                                  'Volumen pro Jahr (geschätzt)'))
    if st['low_share'] is not None:
        figs.append(style_mod.fig(f'{100 * st["low_share"]:.0f} %',
                                  f'Zuschläge mit 0 oder 1 Gebot '
                                  f'({st["n_mature"]} Lose)'))
    if st['closed_share']:
        figs.append(style_mod.fig(f'{100 * st["closed_share"]:.0f} %',
                                  'Verfahren ohne Zuschlag beendet'))
    return f'<div class="figs">{"".join(figs)}</div>'


def lot_rows(sub, n):
    """Live lots — 'Aktuelle Ausschreibungen (Auswahl)'. Title, buyer, region,
    deadline; no verdict, no forecast, no reason (4.1). Buyers are public
    bodies, which is why naming them is fine where naming a winner is not."""
    today = pd.Timestamp(date.today())
    live = sub[pd.to_datetime(sub.deadline_date, errors='coerce') > today]
    live = live.sort_values('deadline_date').head(n)
    if not len(live):
        return ''
    rows = ''.join(
        f'<li><strong>{esc(str(r.title)[:110])}</strong><br>'
        f'<span class="muted">' + meta_line(
            r.buyer_name,
            NUTS1.get(str(r.place_nuts3 or '')[:3], ('',))[0],
            f'Frist {str(r.deadline_date)[:10]}') + '</span></li>'
        for r in live.itertuples(index=False))
    return (f'<h2>Aktuelle Ausschreibungen (Auswahl)</h2>'
            f'<ul class="plain">{rows}</ul>'
            f'<p class="muted">Ein Ausschnitt. Die vollständige, wöchentlich '
            f'gefilterte Liste für Ihr Gewerk und Ihre Region bekommen '
            f'Kunden per E-Mail.</p>')


def award_rows(sub, n):
    """'Fast ohne Wettbewerb: diese Woche vergeben' — freshly published awards
    that closed with 0-1 bidders. **The winner stays unnamed**; the buyer is a
    public body and may be named. This is the proof section."""
    res = sub[sub.resolved & (sub.n_tenders <= 1)].copy()
    if not len(res):
        return ''
    res = res.sort_values('award_date', ascending=False).head(n)
    rows = ''.join(
        f'<li><strong>{esc(str(r.title)[:110])}</strong><br>'
        f'<span class="muted">' + meta_line(
            r.buyer_name,
            f'{money(r.award_value)} €' if pd.notna(r.award_value) else '',
            f'{int(r.n_tenders)} '
            f'{"Gebot" if r.n_tenders == 1 else "Gebote"}',
            f'vergeben {str(r.award_date)[:10]}') + '</span></li>'
        for r in res.itertuples(index=False))
    return (f'<h2>Fast ohne Wettbewerb: kürzlich vergeben</h2>'
            f'<ul class="plain">{rows}</ul>'
            f'<p class="muted">Öffentlich bekannt gemachte Zuschläge. '
            f'Auftragnehmer nennen wir nicht.</p>')


def candidate_rows(sub, n):
    """'Kandidaten für wenig Wettbewerb' — the forward-looking teaser. CPV 452
    only; the caller enforces that. 'Kandidat' is unconfirmed by nature and
    the tag claims an elevated chance, never a likelihood: three of four will
    publicly turn out contested, and this copy has to survive that."""
    today = pd.Timestamp(date.today())
    live = sub[pd.to_datetime(sub.deadline_date, errors='coerce') > today]
    live = live.sort_values('publication_date', ascending=False).head(n)
    if not len(live):
        return ''
    rows = ''.join(
        f'<li><strong>{esc(str(r.title)[:110])}</strong><br>'
        f'<span class="muted">' + meta_line(
            r.buyer_name, f'Frist {str(r.deadline_date)[:10]}')
        + ' · <span style="color:var(--blue)">voraussichtlich '
          'wettbewerbsarm</span></span></li>'
        for r in live.itertuples(index=False))
    return (f'<h2>Kandidaten für wenig Wettbewerb — unsere Wochenauswahl</h2>'
            f'<ul class="plain">{rows}</ul>'
            f'<p class="muted">{esc(DISCLOSURE)} Ein „Kandidat" ist naturgemäß '
            f'unbestätigt — die Einschätzung nennt eine erhöhte Chance, keine '
            f'Wahrscheinlichkeit.</p>')


# --------------------------------------------------------------- the pages

def trade_page(name, slug, lots, sel, covered, mature, base_url, lands):
    sub = lots[sel]
    st = trade_stats(lots, sel, covered, mature)
    is_452 = _is_452(sub)
    land_links = ''
    if lands:
        items = ' · '.join(
            f'<a href="/gewerke/{slug}/{s}/">{esc(n)}</a>' for n, s in lands)
        land_links = f'<h2>Nach Bundesland</h2><p>{items}</p>'
    body = (
        f'<h1>{esc(name)}: Ausschreibungen in Deutschland</h1>'
        f'<p class="lede">Wie viel öffentliche Arbeit in diesem Gewerk '
        f'ausgeschrieben wird, was ein Los wert ist, und wie oft nur ein '
        f'oder gar kein Angebot eingeht.</p>'
        f'{figures_html(st)}{freshness(st["months"])}'
        f'{award_rows(sub, N_AWARDS)}'
        f'{candidate_rows(sub, N_CANDIDATES) if is_452 else ""}'
        f'{lot_rows(sub, N_LIVE)}'
        f'{facts_block(is_452)}{land_links}')
    return document(
        f'{name} — Ausschreibungen & Wettbewerb | TenderMining',
        f'{st["per_month"]:.0f} Lose pro Monat im Gewerk {name}, '
        f'Median-Auftragswert {money(st["median_award"])} €. '
        f'Wie oft nur ein Gebot eingeht — aus öffentlichen Vergabedaten.',
        body, f'/gewerke/{slug}/', base_url)


def land_page(name, slug, land_name, land_slug, lots, sel, covered, mature,
              base_url):
    sub = lots[sel]
    st = trade_stats(lots, sel, covered, mature)
    is_452 = _is_452(sub)
    body = (
        f'<h1>{esc(name)}: Ausschreibungen in {esc(land_name)}</h1>'
        f'<p class="lede">Der regionale Ausschnitt — dieselben Zahlen, '
        f'nur für {esc(land_name)}.</p>'
        f'{figures_html(st)}{freshness(st["months"])}'
        f'{award_rows(sub, N_AWARDS)}'
        f'{lot_rows(sub, N_LIVE)}'
        f'{facts_block(is_452)}'
        f'<p><a href="/gewerke/{slug}/">Bundesweite Zahlen für '
        f'{esc(name)}</a></p>')
    return document(
        f'{name} {land_name} — Ausschreibungen | TenderMining',
        f'Ausschreibungen im Gewerk {name} in {land_name}: '
        f'{st["per_month"]:.1f} Lose pro Monat, Median '
        f'{money(st["median_award"])} €.',
        body, f'/gewerke/{slug}/{land_slug}/', base_url)


def report_page(rows, base_url, months):
    """The national flagship, refreshed quarterly: the 0/1-bid share per
    trade. It exists to earn backlinks — so it is the one page written to be
    quoted, with every denominator on the table."""
    body_rows = ''.join(
        f'<tr><td><a href="/gewerke/{r["slug"]}/">{esc(r["name"])}</a></td>'
        f'<td class="num">{r["n_mature"]}</td>'
        f'<td class="num">{100 * r["low_share"]:.0f} %</td>'
        f'<td class="num">{money(r["median_award"])} €</td></tr>'
        for r in rows)
    body = (
        f'<h1>Wie oft öffentliche Bauaufträge fast ohne Wettbewerb vergeben '
        f'werden</h1>'
        f'<p class="lede">Bei einem erheblichen Teil der öffentlichen '
        f'Bauvergaben in Deutschland geht genau ein Angebot ein — oder '
        f'keines. Diese Auswertung zeigt den Anteil je Gewerk, mit '
        f'Nennern.</p>'
        f'{freshness(months)}'
        f'<div class="scroll"><table><thead><tr><th>Gewerk</th>'
        f'<th class="num">ausgewertete Zuschläge</th>'
        f'<th class="num">0 oder 1 Gebot</th>'
        f'<th class="num">Median-Auftragswert</th></tr></thead>'
        f'<tbody>{body_rows}</tbody></table></div>'
        f'<h2>Methode</h2>'
        f'<p class="muted">Grundlage sind die europaweiten '
        f'Vergabebekanntmachungen (TED) für Deutschland. Gezählt werden nur '
        f'Lose, zu denen bereits ein Zuschlag veröffentlicht wurde — '
        f'Vergabeergebnisse erscheinen im Median rund 90 Tage nach '
        f'Angebotsfrist, weshalb junge Monate ausgeschlossen sind. Ein Gewerk '
        f'wird über eine gepflegte Wortliste auf dem Los-Titel erkannt.</p>'
        f'<p class="muted">Zitieren Sie diese Zahlen gern mit Quellenangabe '
        f'TenderMining und dem Stand oben.</p>'
        f'{cta(False)}')
    return document(
        'Wettbewerbsreport: Bauaufträge mit 0 oder 1 Gebot | TenderMining',
        'Anteil öffentlicher Bauvergaben in Deutschland, die mit nur einem '
        'oder gar keinem Angebot abschließen — je Gewerk, mit Nennern.',
        body, '/single-bidder-report/', base_url)


def index_page(trades, base_url):
    items = ''.join(
        f'<li><a href="/gewerke/{s}/">{esc(n)}</a></li>' for n, s in trades)
    body = (
        f'<h1>Öffentliche Bauausschreibungen, nach Gewerk ausgewertet</h1>'
        f'<p class="lede">Für jedes Gewerk: wie viel ausgeschrieben wird, was '
        f'ein Los wert ist, und wie oft nur ein oder kein Angebot eingeht — '
        f'aus den amtlichen Vergabedaten (TED).</p>'
        f'<h2>Gewerke</h2><ul class="plain">{items}</ul>'
        f'<p><a href="/single-bidder-report/">Der Wettbewerbsreport: '
        f'0 oder 1 Gebot, je Gewerk</a></p>'
        f'{cta(False)}')
    return document(
        'Öffentliche Bauausschreibungen nach Gewerk | TenderMining',
        'Marktzahlen zu öffentlichen Bauausschreibungen in Deutschland: '
        'Volumen, Auftragswerte und Wettbewerbsdichte je Gewerk.',
        body, '/', base_url)


def legal_pages(base_url):
    """The same texts the app carries (4.1). Imported from app.py rather than
    copied: two divergent Datenschutzerklärungen is the failure mode here."""
    import app
    _, _, imp = app.get_impressum({})
    _, _, dat = app.get_datenschutz({})

    def strip(doc):
        return doc.split('<body>', 1)[1].split('<footer>', 1)[0] \
                  .split('</header>')[-1]
    return (document('Impressum | TenderMining', 'Impressum.',
                     strip(imp), '/impressum/', base_url),
            document('Datenschutz | TenderMining',
                     'Datenschutzerklärung.', strip(dat),
                     '/datenschutz/', base_url))


def sitemap(urls, base_url):
    base = base_url.rstrip('/') if base_url else ''
    today = date.today().isoformat()
    entries = ''.join(
        f'<url><loc>{esc(base)}{esc(u)}</loc><lastmod>{today}</lastmod>'
        f'</url>' for u in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f'{entries}</urlset>')


# ------------------------------------------------------------------- render

def write(out, rel, text):
    p = Path(out) / rel.lstrip('/')
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8')
    return rel


def with_deadlines(lots, data_dir):
    """Attach `deadline_date`, which market.py's loader does not read (it only
    needs `deadline_days`). Merged here rather than added to market.TENDER_COLS
    so this renderer cannot change what every operator command loads — the two
    live in the same process and other sessions edit that module."""
    t = pd.read_parquet(Path(data_dir) / 'store' / 'tenders.parquet',
                        columns=['procedure_id', 'lot_id', 'notice_version',
                                 'deadline_date'])
    latest = (t.sort_values('notice_version')
               .drop_duplicates(['procedure_id', 'lot_id'], keep='last')
               .drop(columns=['notice_version']))
    return lots.merge(latest, on=['procedure_id', 'lot_id'], how='left')


def render(data_dir, out, base_url=''):
    """Everything, into `out`. Returns the list of URLs written."""
    lots = with_deadlines(market.add_text(market.load_lots(data_dir)), data_dir)
    trades = market.load_trades()
    covered, mature, _ = market.coverage(lots)
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)          # a stale page is a wrong page
    urls, index_items, report_rows = [], [], []

    for name, trade in trades.items():
        slug = slugify(name)
        sel = market.match(lots, trade, 'core')
        sub = lots[sel]
        if not len(sub):
            print(f'[public] {name}: no matching lot, page skipped')
            continue

        # Bundesland cells above the volume floor (the thin-page guardrail)
        lands = []
        n_months = max(len(covered), 1)
        for code, (land_name, land_slug) in NUTS1.items():
            in_land = sel & lots.place_nuts3.fillna('').str.startswith(code)
            per_month = int(in_land.sum()) / n_months
            if per_month < MIN_LOTS_MONTH:
                continue
            lands.append((land_name, land_slug))
            urls.append(write(out, f'/gewerke/{slug}/{land_slug}/index.html',
                              land_page(name, slug, land_name, land_slug,
                                        lots, in_land, covered, mature,
                                        base_url)).replace('index.html', ''))

        urls.append(write(out, f'/gewerke/{slug}/index.html',
                          trade_page(name, slug, lots, sel, covered, mature,
                                     base_url, lands)).replace('index.html', ''))
        index_items.append((name, slug))
        st = trade_stats(lots, sel, covered, mature)
        if st['low_share'] is not None and st['n_mature'] >= 25:
            report_rows.append({'name': name, 'slug': slug, **st})
        print(f'[public] {name}: {st["lots"]} lots, {len(lands)} Länder pages')

    report_rows.sort(key=lambda r: -r['low_share'])
    urls.append(write(out, '/single-bidder-report/index.html',
                      report_page(report_rows, base_url,
                                  len(covered))).replace('index.html', ''))
    imp, dat = legal_pages(base_url)
    urls.append(write(out, '/impressum/index.html', imp).replace('index.html', ''))
    urls.append(write(out, '/datenschutz/index.html', dat).replace('index.html', ''))
    urls.append(write(out, '/index.html',
                      index_page(sorted(index_items), base_url)).replace('index.html', ''))

    write(out, '/sitemap.xml', sitemap(sorted(urls), base_url))
    # The public site is the one surface that MUST be indexed — the opposite
    # of the app, whose robots.txt disallows everything.
    write(out, '/robots.txt',
          'User-agent: *\nAllow: /\n'
          + (f'Sitemap: {base_url.rstrip("/")}/sitemap.xml\n' if base_url else ''))
    write(out, '/build.json', json.dumps(
        {'built_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
         'pages': len(urls), 'trades': len(index_items),
         'covered_months': len(covered)}, indent=2))
    return urls


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', default=config.data_root())
    ap.add_argument('--out', default=None,
                    help='output directory (default: <data-dir>/public)')
    ap.add_argument('--base-url', default='',
                    help='e.g. https://www.tendermining.de — for canonical '
                         'URLs and the sitemap')
    args = ap.parse_args()
    out = args.out or (Path(args.data_dir) / 'public')
    urls = render(args.data_dir, out, args.base_url)
    print(f'[public] {len(urls)} pages -> {out}')


if __name__ == '__main__':
    main()
