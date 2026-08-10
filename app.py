"""TenderMining customer app — doc/APP.md.

The single live web surface of the product: server-rendered HTML, no REST, no
login, no cookies, no JS, no build chain. Seven routes, which is why there is no
framework here — `wsgiref` ships with Python and a route table is nine lines.

    python app.py                      # http://127.0.0.1:8000
    python app.py --port 8080 --data-dir /data

In the image:

    docker compose up -d app           # then http://localhost:8000/

**What is built here** is the serving core: the public pages, `/healthz`,
`robots.txt`, the token layer (`tokens.py`), and the GET side of the four
token routes. The POST handlers of doc/APP.md 4-6 are NOT built — they need
subscription fields that do not exist yet (`contact_state`, `email`,
`consent_at`) and the guarded mailer of 7, and CLAUDE.md requires the `KNOWN`
half of that to land in the same commit as the first write. Until then a POST
answers 405 with a page saying so, rather than a form that silently drops what
a customer typed.

Two rules from the spec are enforced here rather than trusted:

- **GET never mutates.** The dispatcher only ever calls a `get_*` handler for
  GET, and no `get_*` handler in this file writes. This is the whole defence
  against mail scanners and link prefetchers, which fetch every URL in an
  e-mail before a human sees it — a stop link that worked on GET would
  unsubscribe customers who never clicked anything.
- **`X-Robots-Tag: noindex` on every response**, robots.txt disallowing all.
  The app must be unfindable, not merely unlisted: the URLs contain capability
  tokens, and a search engine that indexed one would publish it.
"""

import argparse
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from wsgiref.simple_server import make_server

import config
import tokens

CONTACT = 'kontakt@tendermining.de'   # [CLARIFY] the real address, before print

# Every response carries these. The robots tag is not advice — it is the only
# thing standing between a capability token and a search index.
BASE_HEADERS = [
    ('X-Robots-Tag', 'noindex, nofollow, noarchive'),
    ('Referrer-Policy', 'no-referrer'),       # tokens must not leak in Referer
    ('X-Content-Type-Options', 'nosniff'),
    ('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'"),
]

STYLE = """
  :root { color-scheme: light dark }
  body { font: 16px/1.6 system-ui, -apple-system, Segoe UI, sans-serif;
         max-width: 34rem; margin: 3rem auto; padding: 0 1.2rem; }
  h1 { font-size: 1.35rem; margin-bottom: .2rem }
  h2 { font-size: 1.05rem; margin-top: 2rem }
  .muted { color: #6b7280 }
  dl { display: grid; grid-template-columns: max-content 1fr; gap: .3rem .9rem }
  dt { color: #6b7280 }
  footer { margin-top: 3rem; font-size: .875rem; color: #6b7280 }
  a { color: inherit }
"""


def esc(s):
    return html.escape(str(s))


def page(title, body, status='200 OK'):
    """Every page in the app comes out of here — one <head>, one footer, one
    place where the legal links are correct."""
    doc = (f'<!doctype html><html lang="de"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width, initial-scale=1">'
           f'<meta name="robots" content="noindex, nofollow">'
           f'<title>{esc(title)} — TenderMining</title>'
           f'<style>{STYLE}</style></head><body>{body}'
           f'<footer><a href="/impressum">Impressum</a> · '
           f'<a href="/datenschutz">Datenschutz</a> · '
           f'<a href="mailto:{esc(CONTACT)}">{esc(CONTACT)}</a></footer>'
           f'</body></html>')
    return status, 'text/html; charset=utf-8', doc


# ----------------------------------------------------------------- the pages

def get_root(ctx):
    return page('TenderMining', f"""
      <h1>TenderMining</h1>
      <p>Wir melden Bauausschreibungen, bei denen wenige Mitbewerber zu
         erwarten sind — wöchentlich, für ein Gewerk und eine Region.</p>
      <p class="muted">Diese Seite richtet sich an bestehende Kontakte. Fragen
         beantworten wir unter <a href="mailto:{esc(CONTACT)}">{esc(CONTACT)}</a>.</p>""")


def get_impressum(ctx):
    # [CLARIFY] before the first letter goes out: §5 TMG requires the real
    # name, address and represented person. Deliberately not invented here —
    # a placeholder that looks like an Impressum is worse than an obvious gap.
    return page('Impressum', """
      <h1>Impressum</h1>
      <p>Angaben gemäß § 5 TMG.</p>
      <p class="muted">Diese Angaben werden vor dem Versand der ersten
         Anschreiben eingetragen.</p>""")


def get_datenschutz(ctx):
    return page('Datenschutz', f"""
      <h1>Datenschutzerklärung</h1>
      <p>Wir verarbeiten Firmen-Kontaktdaten aus öffentlichen
         Vergabebekanntmachungen, um Ihnen passende Ausschreibungen zu melden.</p>
      <h2>Widerspruch</h2>
      <p>Sie können der Verarbeitung jederzeit widersprechen — formlos an
         <a href="mailto:{esc(CONTACT)}">{esc(CONTACT)}</a> oder über den
         Abbestellen-Link in jeder E-Mail.</p>
      <p class="muted">Die vollständige Erklärung nach Art. 14 DSGVO wird hier
         eingetragen, bevor die erste Nachricht versendet wird
         (doc/LEGAL_BASIS_TARGET_LIST.md).</p>""")


def get_healthz(ctx):
    """200 plus how fresh the cycle's data is. Deliberately text/plain: this
    one is read by a restart policy and by a person on a phone, not by a
    customer."""
    stamp, age = _freshness(ctx['data_dir'])
    body = (f'ok\ncycle_last_success={stamp or "unknown"}\n'
            f'cycle_age_days={age if age is not None else "unknown"}\n')
    return '200 OK', 'text/plain; charset=utf-8', body


def get_robots(ctx):
    return '200 OK', 'text/plain; charset=utf-8', 'User-agent: *\nDisallow: /\n'


def _freshness(data_dir):
    """(last successful cycle date, days ago) from the loop checkpoint. Any
    problem reads as unknown — a health endpoint that raises is worse than one
    that admits it does not know."""
    try:
        cp = json.loads((Path(data_dir) / 'logs' / 'loop_checkpoint.json')
                        .read_text(encoding='utf-8'))
        stamp = cp.get('last_success_to')
        if not stamp:
            return None, None
        d = datetime.strptime(str(stamp), '%Y%m%d').replace(tzinfo=timezone.utc)
        return stamp, (datetime.now(timezone.utc) - d).days
    except Exception:                                          # noqa: BLE001
        return None, None


# -------------------------------------------------------- the token routes

def get_signup(ctx, row):
    return page('Anmeldung', f"""
      <h1>Wöchentliche Ausschreibungen</h1>
      <p>Für <strong>{esc(row['sub_id'])}</strong>.</p>
      <p class="muted">Das Anmeldeformular wird hier eingesetzt
         (doc/APP.md 4). Der Link ist gültig und bleibt es.</p>""")


def get_feedback(ctx, row):
    return page('Rückmeldung', f"""
      <h1>Rückmeldung zu einer Ausschreibung</h1>
      <dl><dt>Los</dt><dd>{esc(row['procedure_id'])} · {esc(row['lot_id'])}</dd>
          <dt>Ihre Angabe</dt><dd>{esc(row['verdict'])}</dd></dl>
      <p class="muted">Der Bestätigungsknopf wird hier eingesetzt
         (doc/APP.md 2). Erst ein Klick darauf speichert etwas.</p>""")


def get_stop(ctx, row):
    return page('Abbestellen', """
      <h1>Abbestellen</h1>
      <p>Zwei Möglichkeiten: keine wöchentlichen Berichte mehr, oder gar keine
         E-Mails mehr.</p>
      <p class="muted">Die beiden Knöpfe werden hier eingesetzt
         (doc/APP.md 5). Noch ist nichts abbestellt.</p>""")


def get_recall(ctx, row):
    return page('Ausschreibung prüfen', """
      <h1>Ausschreibung prüfen</h1>
      <p>Nummer oder Link einer Ausschreibung — wir sagen Ihnen, wie wir sie
         für Sie einschätzen.</p>
      <p class="muted">Das Eingabefeld wird hier eingesetzt (doc/APP.md 6).</p>""")


def get_invalid(ctx):
    """One page for every token that does not work, whatever the reason
    (doc/APP.md 2). 200, not 404: a status code is an oracle too."""
    return page('Link nicht gültig', f"""
      <h1>Dieser Link ist nicht mehr gültig</h1>
      <p>Bitte melden Sie sich bei
         <a href="mailto:{esc(CONTACT)}">{esc(CONTACT)}</a>, wenn Sie ihn
         gerade erst erhalten haben.</p>""")


def not_found(ctx):
    return page('Nicht gefunden', '<h1>404</h1>', status='404 Not Found')


def not_yet(ctx):
    """A POST to a route whose handler is not built. 405 and an honest
    sentence, never a form that accepts input and drops it."""
    return page('Noch nicht verfügbar', """
      <h1>Noch nicht verfügbar</h1>
      <p class="muted">Dieser Schritt ist noch nicht freigeschaltet.</p>""",
                status='405 Method Not Allowed')


# ------------------------------------------------------------- the dispatch

STATIC = {
    '/': get_root,
    '/impressum': get_impressum,
    '/datenschutz': get_datenschutz,
    '/healthz': get_healthz,
    '/robots.txt': get_robots,
}

# prefix -> (token purpose, GET handler). The purpose is what makes a feedback
# link useless as a stop link (tokens.py).
TOKEN_ROUTES = {
    't': ('t', get_signup),
    'f': ('f', get_feedback),
    's': ('s', get_stop),
    'c': ('c', get_recall),
}


def route(ctx, method, path):
    """-> (status, content_type, body). The only place a request becomes a
    page, and the only place the GET/POST rule is enforced."""
    if path in STATIC:
        if method != 'GET':
            return not_yet(ctx)
        return STATIC[path](ctx)

    parts = [p for p in path.split('/') if p]
    if len(parts) == 2 and parts[0] in TOKEN_ROUTES:
        purpose, handler = TOKEN_ROUTES[parts[0]]
        row = tokens.resolve(ctx['data_dir'], purpose, parts[1])
        if row is None:
            return get_invalid(ctx)
        if method == 'GET':
            return handler(ctx, row)
        return not_yet(ctx)                     # doc/APP.md 4-6, not built

    return not_found(ctx)


def application(environ, start_response):
    ctx = {'data_dir': environ.get('tm.data_dir') or config.data_root()}
    status, ctype, body = route(ctx, environ.get('REQUEST_METHOD', 'GET'),
                                environ.get('PATH_INFO', '/'))
    payload = body.encode('utf-8')
    headers = [('Content-Type', ctype),
               ('Content-Length', str(len(payload)))] + BASE_HEADERS
    start_response(status, headers)
    return [payload]


def make_app(data_dir):
    """The WSGI callable with its data directory bound — what the server and
    the tests both use, so neither depends on a global."""
    def app(environ, start_response):
        environ['tm.data_dir'] = str(data_dir)
        return application(environ, start_response)
    return app


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', default=config.data_root())
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8000)
    args = ap.parse_args()
    print(f'[app] data root: {config.describe(args.data_dir)}')
    print(f'[app] listening on http://{args.host}:{args.port}')
    with make_server(args.host, args.port, make_app(args.data_dir)) as httpd:
        httpd.serve_forever()


if __name__ == '__main__':
    main()
