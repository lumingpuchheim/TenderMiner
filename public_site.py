"""TenderMining public site — the page a stranger lands on.

    python public_site.py                  # -> data/public/
    python public_site.py --out /tmp/site --base-url https://www.tendermining.de

One page that says what the service is, plus the legal pages the app already
carries, a sitemap and a robots.txt. That is the whole site.

**It deliberately does not segment by trade** (operator, 2026-08-11). An
electrician has no use for a page about Maler, and a site organised as "browse
the trades" spends his time to save his time. The copy therefore never names a
trade: it describes the service, and the one thing it asks for is a line of
e-mail. LAUNCH.md 4.1's per-trade and trade×Land pages, and the market figures
that went with them, were built and then removed — `git log public_site.py` has
them if the SEO argument is ever reopened.

**It carries no figures yet, on purpose.** A number on a cold page needs its
denominator, its date and its method beside it or it is just a claim; the
operator's call is that the page earns attention with what the service *is*
first. That is also why nothing here reads the store — this renderer has no
data dependency at all, which is the simplest thing it could possibly be.

The one page's job is the funnel's cold entrance (ONBOARDING.md §0). The warm
entrance is the letter's QR code, which lands on `app.<domain>/t/<token>` and
already knows the firm — a different page, in `app.py`, doing a different job.
"""

import argparse
import html
import shutil
import sys
from datetime import date
from pathlib import Path

import config
import style as style_mod

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

CONTACT = 'kontakt@tendermining.de'


def esc(s):
    return html.escape(str(s))


def document(title, description, body, canonical, base_url):
    """Every public page. Unlike the app's pages these are INDEXABLE — that is
    the point of having them — so they carry a description and a canonical URL
    and no robots restriction."""
    can = f'{base_url.rstrip("/")}{canonical}' if base_url else canonical
    return (
        f'<!doctype html><html lang="de"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{esc(title)}</title>'
        f'<meta name="description" content="{esc(description)}">'
        f'<link rel="canonical" href="{esc(can)}">'
        f'<style>{style_mod.CSS}</style></head><body>'
        f'{style_mod.header(home="/")}{body}'
        f'<footer><a href="/">Startseite</a> · '
        f'<a href="/impressum/">Impressum</a> · '
        f'<a href="/datenschutz/">Datenschutz</a> · '
        f'<a href="mailto:{esc(CONTACT)}">{esc(CONTACT)}</a></footer>'
        f'</body></html>')


def landing_page(base_url):
    """The introduction, approved 2026-08-11.

    The order is the argument: what it is, why it can work at all, why we need
    nothing from him, what arrives, what we refuse to promise, and only then
    the ask. The ask is last because it is the only thing on the page that
    costs him anything.

    The CTA is doing the real work. "Kontaktieren Sie uns" asks a stranger to
    take a risk for nothing; *"send your firm name and we will show you what
    you won and what matched this week"* is a free, concrete, personal thing
    only this product can produce — and it is exactly the funnel: he writes,
    we find him in the awards store, the trial starts.
    """
    body = (
        '<h1>Öffentliche Bauaufträge, bei denen kaum jemand mitbietet.</h1>'

        '<p class="lede">Wir lesen jede Woche alle öffentlichen '
        'Bauausschreibungen in Deutschland und schicken Ihnen die wenigen, die '
        'zu Ihrem Betrieb passen — mit dem Hinweis, wo wenige Mitbieter zu '
        'erwarten sind.</p>'

        '<h2>Der Gedanke dahinter</h2>'
        '<p>Nicht jede Ausschreibung ist ein Preiskampf. Ein Teil der '
        'öffentlichen Bauaufträge wird mit einem einzigen Angebot vergeben, '
        'manche ganz ohne. Vergeben werden sie trotzdem — nur eben an den '
        'Betrieb, der sie rechtzeitig gesehen hat.</p>'

        '<h2>Ohne Fragebogen</h2>'
        '<p>Sie müssen uns nicht erklären, was Sie machen. In den amtlichen '
        'Vergabeergebnissen steht, welche Aufträge Sie gewonnen haben. Daraus '
        'bauen wir Ihr Profil — und daran messen wir jede neue '
        'Ausschreibung.</p>'

        '<h2>Was Sie bekommen</h2>'
        '<p>Montags eine E-Mail. Eine kurze Liste statt eines Suchergebnisses: '
        'Vergabestelle, Frist, und warum das Los auf Ihrer Liste steht. Passt '
        'etwas nicht, antworten Sie mit einer Zeile — das Profil ändert sich '
        'zur nächsten Woche.</p>'

        '<h2>Was wir nicht versprechen</h2>'
        '<p>Wer am Ende mitbietet, weiß vorher niemand. Wir rechnen mit einer '
        'erhöhten Chance auf wenig Wettbewerb — nicht mit einer Zusage.</p>'

        '<div class="note">'
        '<h2 style="margin-top:0">Schreiben Sie uns</h2>'
        '<p>Eine Zeile mit Ihrem Firmennamen genügt. Wir sehen nach, was Sie '
        'in den letzten Jahren gewonnen haben, und sagen Ihnen, welche '
        'Ausschreibungen diese Woche dazu gepasst hätten. Kostenlos und '
        'unverbindlich.</p>'
        f'<p><a href="mailto:{esc(CONTACT)}"><strong>{esc(CONTACT)}</strong>'
        f'</a></p></div>')
    return document(
        'Öffentliche Bauaufträge mit wenig Wettbewerb | TenderMining',
        'Wir lesen wöchentlich alle öffentlichen Bauausschreibungen in '
        'Deutschland und melden Ihnen die passenden, bei denen wenige '
        'Mitbieter zu erwarten sind. Ohne Fragebogen — Ihr Profil entsteht '
        'aus den Aufträgen, die Sie gewonnen haben.',
        body, '/', base_url)


def legal_pages(base_url):
    """The same texts the app serves. Imported from `app.py` rather than
    copied: two Datenschutzerklärungen that can drift apart is the failure
    mode here, and the one on the public site is the one a stranger reads."""
    import app
    _, _, imp = app.get_impressum({})
    _, _, dat = app.get_datenschutz({})

    def inner(doc):
        return doc.split('</header>', 1)[1].split('<footer>', 1)[0]

    return (document('Impressum | TenderMining',
                     'Impressum und Anbieterkennzeichnung.',
                     inner(imp), '/impressum/', base_url),
            document('Datenschutz | TenderMining',
                     'Datenschutzerklärung: welche Daten wir verarbeiten, '
                     'woher sie stammen und wie Sie widersprechen.',
                     inner(dat), '/datenschutz/', base_url))


def sitemap(urls, base_url):
    base = base_url.rstrip('/') if base_url else ''
    today = date.today().isoformat()
    entries = ''.join(f'<url><loc>{esc(base)}{esc(u)}</loc>'
                      f'<lastmod>{today}</lastmod></url>' for u in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f'{entries}</urlset>')


def write(out, rel, text):
    p = Path(out) / rel.lstrip('/')
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8')
    return rel


def render(out, base_url=''):
    """The whole site into `out`. Returns the URLs written."""
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)              # a stale page is a wrong page
    imp, dat = legal_pages(base_url)
    urls = ['/', '/impressum/', '/datenschutz/']
    write(out, '/index.html', landing_page(base_url))
    write(out, '/impressum/index.html', imp)
    write(out, '/datenschutz/index.html', dat)
    write(out, '/sitemap.xml', sitemap(urls, base_url))
    # The opposite of the app's robots.txt: this is the one surface that is
    # meant to be found.
    write(out, '/robots.txt', 'User-agent: *\nAllow: /\n' + (
        f'Sitemap: {base_url.rstrip("/")}/sitemap.xml\n' if base_url else ''))
    return urls


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', default=config.data_root())
    ap.add_argument('--out', default=None,
                    help='output directory (default: <data-dir>/public)')
    ap.add_argument('--base-url', default='',
                    help='e.g. https://www.tendermining.de — canonical URLs '
                         'and the sitemap')
    args = ap.parse_args()
    out = args.out or (Path(args.data_dir) / 'public')
    urls = render(out, args.base_url)
    print(f'[public] {len(urls)} pages -> {out}')


if __name__ == '__main__':
    main()
