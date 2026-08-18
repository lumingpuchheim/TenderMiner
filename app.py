"""TenderMining customer app — doc/APP.md.

The single live web surface of the product: server-rendered HTML, no REST, no
login, no cookies, no JS, no build chain. Seven routes, which is why there is no
framework here — `wsgiref` ships with Python and a route table is nine lines.

    python app.py                      # http://127.0.0.1:8000
    python app.py --port 8080 --data-dir /data

In the image:

    docker compose up -d app           # then http://localhost:8000/

All seven routes are live, GET and POST (doc/APP.md 2-6): signup with the
gate pre-flight, feedback confirm, the two-button stop page, and the recall
box. State changes go through `subscriptions.py` (customer row, versions) and
land as `app_events` ledger rows; e-mail goes through `mailer.py`, whose
contact_state guard is the module, not the caller's discipline.

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
import os
import re
import shutil
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import config
import ledger
import subscriptions
import tokens

CONTACT = 'info@murara.eu'   # must exist before the first letter

# Every response carries these. The robots tag is not advice — it is the only
# thing standing between a capability token and a search index.
BASE_HEADERS = [
    ('X-Robots-Tag', 'noindex, nofollow, noarchive'),
    ('Referrer-Policy', 'no-referrer'),       # tokens must not leak in Referer
    ('X-Content-Type-Options', 'nosniff'),
    ('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'"),
]

import style as style_mod

STYLE = style_mod.CSS


def esc(s):
    return html.escape(str(s))


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _event(data_dir, kind, sub_id, **fields):
    """One app_events ledger row — every state change and every question a
    customer asks leaves exactly one (doc/APP.md 2, LAUNCH.md 3)."""
    ledger.append(data_dir, 'app_events',
                  [{'ts': _now(), 'kind': kind, 'sub_id': sub_id, **fields}])


def _form(environ):
    """The POSTed form fields, decoded. Bounded read: no form in this app has
    a reason to exceed a few KB, and an unbounded read is a memory lever."""
    try:
        n = min(int(environ.get('CONTENT_LENGTH') or 0), 64 * 1024)
        body = environ['wsgi.input'].read(n).decode('utf-8', 'replace')
        return {k: v[0] for k, v in parse_qs(body).items()}
    except Exception:                                          # noqa: BLE001
        return {}


# --------------------------------------------------- rate limit (APP.md 3)
# A lazy brake on token enumeration; 192-bit randomness is the real defense.
# Per-IP sliding window, in memory — restarting the app forgets it, which is
# fine for a brake and free of storage.

RATE_MAX, RATE_WINDOW = 30, 60.0          # token lookups per IP per minute
_hits = defaultdict(deque)


def _limited(ip):
    q = _hits[ip]
    now = time.monotonic()
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_MAX:
        return True
    q.append(now)
    if len(_hits) > 10_000:               # bounded memory under address spray
        _hits.clear()
    return False


def page(title, body, status='200 OK'):
    """Every page in the app comes out of here — one <head>, one footer, one
    place where the legal links are correct."""
    doc = (f'<!doctype html><html lang="de"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width, initial-scale=1">'
           f'<meta name="robots" content="noindex, nofollow">'
           f'<title>{esc(title)} — Murara</title>'
           f'<style>{STYLE}</style></head><body>'
           f'{style_mod.header()}{body}'
           f'<footer><a href="/impressum">Impressum</a> · '
           f'<a href="/datenschutz">Datenschutz</a> · '
           f'<a href="mailto:{esc(CONTACT)}">{esc(CONTACT)}</a></footer>'
           f'</body></html>')
    return status, 'text/html; charset=utf-8', doc


# ----------------------------------------------------------------- the pages

def get_root(ctx):
    return page('Murara', f"""
      <h1>Murara</h1>
      <p>Wir melden Bauausschreibungen, bei denen wenige Mitbewerber zu
         erwarten sind — für ein Gewerk und eine Region, wöchentlich geprüft.</p>
      <p class="muted">Diese Seite richtet sich an bestehende Kontakte. Fragen
         beantworten wir unter <a href="mailto:{esc(CONTACT)}">{esc(CONTACT)}</a>.</p>""")


# The operator's legal identity (§ 5 TMG), in ONE place: the public page at
# `site/impressum/index.html`, which is the copy the law actually requires to
# exist. This page reads that one rather than keeping a second.
#
# It used to read only TM_IMPRESSUM, on the reasoning that the identity should
# never have to be committed to a public repository. That reasoning expired the
# day the site page was filled in — an Impressum is published information by
# definition — and it left the two copies free to disagree, which they promptly
# did: the site carried the real Anbieterkennzeichnung while this page still
# served "[Noch nicht eingetragen]". A customer clicking Impressum in the app
# got the unfilled one. `tests/test_site_files.py` is what caught it.
#
# TM_IMPRESSUM still wins where it is set, for a deployment that would rather
# keep the identity out of the repository: HTML-escaped lines, | separated.
# With neither source, the gap stays VISIBLE rather than silently faked.
SITE_IMPRESSUM = Path(__file__).resolve().parent / 'site' / 'impressum' / 'index.html'


def _impressum_lines():
    import os
    raw = os.environ.get('TM_IMPRESSUM', '')
    if raw.strip():
        return [l.strip() for l in raw.split('|') if l.strip()]
    return _site_impressum_lines()


def _site_impressum_lines():
    """The Diensteanbieter block of the public page, as plain text lines.

    Deliberately narrow: the one block that names the provider, not the whole
    page. Anything unreadable or unrecognised returns nothing at all, so a
    renamed heading shows the visible gap instead of a half-page of markup.
    """
    try:
        doc = SITE_IMPRESSUM.read_text(encoding='utf-8')
    except OSError:
        return []
    block = re.search(r'<h2>\s*Diensteanbieter\s*</h2>\s*<p>(.*?)</p>', doc, re.S)
    if not block:
        return []
    return [html.unescape(re.sub(r'<[^>]+>', '', line)).strip()
            for line in re.split(r'<br\s*/?>', block.group(1))
            if line.strip()]


def get_impressum(ctx):
    lines = _impressum_lines()
    body = ''.join(f'<p>{esc(l)}</p>' for l in lines) if lines else (
        '<p class="muted">[Noch nicht eingetragen — Angaben gemäß § 5 TMG '
        'folgen vor Inbetriebnahme. Betreiberkontakt: '
        f'<a href="mailto:{esc(CONTACT)}">{esc(CONTACT)}</a>]</p>')
    return page('Impressum', f'<h1>Impressum</h1>{body}')


def get_datenschutz(ctx):
    # The Art. 14 long-form notice (doc/LEGAL_BASIS_TARGET_LIST.md): who we
    # are, what we hold, where it came from, why we may, how long, and every
    # right — in sober German, no marketing.
    return page('Datenschutz', f"""
      <h1>Datenschutzerklärung</h1>
      <p>Stand: August 2026. Verantwortlicher: siehe
         <a href="/impressum">Impressum</a>; Kontakt für alle
         Datenschutzanliegen: <a href="mailto:{esc(CONTACT)}">{esc(CONTACT)}</a>.</p>

      <h2>Welche Daten wir verarbeiten, und woher sie stammen</h2>
      <p>Wir verarbeiten <strong>geschäftliche</strong> Kontakt- und
         Auftragsdaten von Bauunternehmen: Firmenname, Anschrift und die in
         öffentlichen Vergabebekanntmachungen genannten Zuschläge. Quelle ist
         der Amtsblatt-Dienst der EU (<em>Tenders Electronic Daily</em>, TED)
         — ein öffentliches Register (Art. 14 Abs. 2 lit. f DSGVO). Eine
         E-Mail-Adresse speichern wir nur, wenn Sie sie selbst angeben.</p>

      <h2>Zweck und Rechtsgrundlage</h2>
      <p>Zweck ist, Unternehmen auf zu ihrem Gewerk passende Ausschreibungen
         mit voraussichtlich geringem Wettbewerb hinzuweisen — einmalig
         postalisch, danach nur auf Ihre Anmeldung hin. Rechtsgrundlage der
         Erstansprache ist unser berechtigtes Interesse an
         Geschäftsanbahnung im B2B-Bereich (Art. 6 Abs. 1 lit. f DSGVO);
         Rechtsgrundlage der Berichte nach Anmeldung ist die Anmeldung selbst
         (Art. 6 Abs. 1 lit. b DSGVO).</p>

      <h2>Speicherdauer</h2>
      <p>Vergabedaten stammen aus einem öffentlichen Register und werden für
         die Produktfunktion vorgehalten. Ihre Kontaktdaten löschen wir auf
         Zuruf; nach einem vollständigen Werbewiderspruch führen wir nur den
         Eintrag „nicht kontaktieren", der den Widerspruch durchsetzt.</p>

      <h2>Empfänger</h2>
      <p>Keine Weitergabe an Dritte. Für den E-Mail-Versand nutzen wir einen
         Auftragsverarbeiter (Resend); Grundlage ist ein
         Auftragsverarbeitungsvertrag nach Art. 28 DSGVO.</p>

      <h2>Ihre Rechte</h2>
      <p>Auskunft (Art. 15), Berichtigung (Art. 16), Löschung (Art. 17),
         Einschränkung (Art. 18), Datenübertragbarkeit (Art. 20) und
         Beschwerde bei einer Aufsichtsbehörde (Art. 77 DSGVO).</p>

      <h2>Widerspruch (Art. 21 DSGVO)</h2>
      <p><strong>Sie können der Verarbeitung Ihrer Daten zum Zweck der
         Direktwerbung jederzeit und ohne Begründung widersprechen.</strong>
         Formlos an <a href="mailto:{esc(CONTACT)}">{esc(CONTACT)}</a>, oder
         über den Abbestellen-Link, den jede unserer E-Mails trägt. Danach
         erhalten Sie nichts mehr.</p>

      <h2>Keine Cookies, kein Tracking</h2>
      <p>Diese Seiten setzen keine Cookies und laden nichts von Dritten.
         Zugriffe werden mit IP-Adresse und Zeitpunkt kurzzeitig
         protokolliert (Betriebssicherheit, Art. 6 Abs. 1 lit. f DSGVO) und
         turnusmäßig gelöscht.</p>""")


# What this endpoint turns red at — doc/OPERATIONS.md 1. Both numbers are read
# from outside by a status-code pinger and from inside by the deploy gate of
# OPERATIONS.md 2, which is the point of putting the semantics here: "healthy"
# must not mean two different things to the two of them.
MAX_CYCLE_AGE_DAYS = 8       # weekly cycle, so 8 tolerates exactly one late Monday
MIN_FREE_BYTES = 2 * 1024**3  # state grows ~90 MB/week — 2 GB is weeks of warning


def get_healthz(ctx):
    """200 when the cycle is recent and `/data` has room, 503 otherwise.
    Deliberately text/plain: this one is read by a restart policy, by a dumb
    status-code pinger and by a person on a phone, not by a customer.

    Unknown reads as red — for the age and for the disk. Two consequences,
    both wanted: a fresh deployment answers 503 until it has run one cycle
    (OPERATIONS.md 4 step 5), and a container that cannot see its state
    directory fails the deploy gate instead of sailing through it, which is
    the single most likely way a new image is broken.
    """
    stamp, age = _freshness(ctx['data_dir'])
    free = _free_bytes(ctx['data_dir'])
    ok = (age is not None and age <= MAX_CYCLE_AGE_DAYS
          and free is not None and free >= MIN_FREE_BYTES)
    body = (f'{"ok" if ok else "degraded"}\n'
            f'cycle_last_success={stamp or "unknown"}\n'
            f'cycle_age_days={age if age is not None else "unknown"}\n'
            f'disk_free_mb={free // 1024**2 if free is not None else "unknown"}\n'
            f'gate_config={_gate_fingerprint(ctx["data_dir"]) or "unknown"}\n')
    return (('200 OK' if ok else '503 Service Unavailable'),
            'text/plain; charset=utf-8', body)


def get_robots(ctx):
    return '200 OK', 'text/plain; charset=utf-8', 'User-agent: *\nDisallow: /\n'


def _gate_fingerprint(data_dir):
    """The gate configuration most recently recorded by a cycle
    (`loop.record_gate_config`), read from the ledger — not recomputed here,
    because the app image does not carry the gate's dependencies and because
    the question is what customers were served under, not what this process
    would resolve to (PARAMETERS.md 4.3). Any problem reads as None."""
    try:
        rows = ledger.read(data_dir, 'gate_configs')
    except Exception:
        return None
    if not rows:
        return None
    return max(rows, key=lambda r: r.get('first_seen') or '').get('fingerprint')


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


def _free_bytes(data_dir):
    """Free space on the state directory, or None when it cannot be read. In a
    container that second case means the mount is not there — which is a health
    failure, not a detail, so it reads as unknown and unknown reads as red."""
    try:
        return shutil.disk_usage(data_dir).free
    except Exception:                                          # noqa: BLE001
        return None


# -------------------------------------------------------- the token routes

def _mask(addr):
    """m…@firma.de — enough to recognise your own address, nothing to harvest."""
    if not addr or '@' not in addr:
        return '—'
    local, _, dom = addr.partition('@')
    return f'{local[:1]}…@{dom}'


def get_signup(ctx, row):
    if row.get('used_at'):
        cust = subscriptions.customer_get(ctx['data_dir'], row['sub_id']) or {}
        return page('Bereits angemeldet', f"""
          <h1>Bereits angemeldet</h1>
          <p>Für diesen Zugang ist schon eine Adresse hinterlegt
             ({esc(_mask(cust.get('contact_email')))}). Soll sie geändert
             werden, schreiben Sie uns:
             <a href="mailto:{esc(CONTACT)}">{esc(CONTACT)}</a>.</p>""")
    cust = subscriptions.customer_get(ctx['data_dir'], row['sub_id']) or {}
    firm = cust.get('name') or row['sub_id']
    return page('Anmeldung', f"""
      <h1>Ausschreibungen für Ihren Betrieb</h1>
      <p>Für <strong>{esc(firm)}</strong>: die Ausschreibungen Ihres
         Gewerks und Ihrer Region, bei denen wir wenig Wettbewerb erwarten —
         sobald es welche gibt. Wir prüfen wöchentlich; gibt es nichts
         Passendes, schreiben wir nicht. Kostenlos zum Kennenlernen,
         monatlich beendbar.</p>
      <form method="post">
        <p><label>E-Mail-Adresse<br>
           <input type="email" name="email" required></label></p>
        <p><button type="submit">Berichte erhalten</button></p>
      </form>
      <p class="muted">Mit der Anmeldung erhalten Sie unsere Berichte, wenn
         es passende Ausschreibungen gibt, und danach gelegentlich
         Ergebnis-Nachrichten und Angebote von uns. Jede E-Mail trägt einen Abbestellen-Link; Details in der
         <a href="/datenschutz">Datenschutzerklärung</a>.</p>""")


def post_signup(ctx, row, form):
    home = ctx['data_dir']
    if row.get('used_at'):
        return get_signup(ctx, row)                    # doc/APP.md 4: no overwrite
    email = (form.get('email') or '').strip()
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
        return page('Anmeldung', f"""
          <h1>Anmeldung</h1>
          <p>Die Adresse sieht unvollständig aus — bitte prüfen Sie sie:
             <strong>{esc(email or '(leer)')}</strong>.</p>
          <p><a href="">Zurück zum Formular</a></p>""")
    # The consent record (LAUNCH.md 3): the submission IS the consent, its
    # text on the page above names what will come.
    ok, _detail = activate(home, row['sub_id'], email)
    tokens.mark_used(home, row['token'])
    return page('Angemeldet', f"""
      <h1>Das war alles</h1>
      <p>{'Ihr erster Bericht kommt, sobald es passende Ausschreibungen '
          'gibt — wir prüfen wöchentlich; gibt es nichts, schreiben wir nicht.'
          if ok else
          'Wir richten Ihr Profil ein und melden uns, bevor der erste '
          'Bericht kommt.'}</p>
      <p class="muted">Bestätigung an {esc(_mask(email))}. Abbestellen geht
         in jeder E-Mail mit einem Klick.</p>""")


def activate(home, sub_id, email, consent_note=None):
    """Address + consent + pre-flight + confirmation mail, in one place —
    doc/ADMIN.md 7 step 1. Both ways in use it: the customer typing the
    address on `/t/<token>` (the submit IS the consent) and the operator
    typing it on `/admin/email` (where `consent_note` says where the consent
    came from — a phone call, a reply). -> (activated?, detail).

    The mail is best-effort by design: a signup that is recorded but whose
    confirmation could not go out is a customer we know about; a signup that
    failed because the mail failed is a customer we lost."""
    fields = {'contact_email': email, 'consent_at': _now(),
              'contact_state': 'active'}
    if consent_note:
        fields['contact_note'] = f'Einwilligung: {consent_note}'
    subscriptions.customer_update(home, sub_id, **fields)
    ok, detail = _preflight(home, sub_id)
    if consent_note:
        detail = f'admin: {consent_note} — {detail}'
    _event(home, 'signup' if ok else 'signup_held', sub_id, detail=detail)
    try:
        import mailer
        footer_html, headers = mailer.footer(home, sub_id)
        mailer.send(home, 'confirm', sub_id, 'Ihre Anmeldung bei Murara',
                    f'<p>Ihre Anmeldung ist eingegangen. '
                    f'{"Der erste Bericht kommt, sobald es passende "
                       "Ausschreibungen gibt — wir prüfen wöchentlich."
                       if ok else
                       "Wir richten Ihr Profil ein und melden uns."}</p>'
                    + footer_html, headers=headers)
    except Exception as e:                                     # noqa: BLE001
        print(f'[app] confirm mail not sent ({e}); signup itself is recorded')
    return ok, detail


def stop_customer(home, sub_id, hard, *, source='customer'):
    """The two stop states (LAUNCH.md 3), from the customer's page or the
    operator's. A hard stop revokes every live token as well: "stop sending"
    and "the links in their inbox stop working" are the same promise."""
    subscriptions.customer_update(
        home, sub_id, contact_state='hard_stopped' if hard else 'soft_stopped')
    _event(home, 'stop_hard' if hard else 'stop_soft', sub_id,
           detail=source if source != 'customer' else None)
    if hard:
        tokens.revoke_all(home, sub_id)


def _preflight(home, sub_id):
    """ONBOARDING.md 5.3: before any firm's first report, replay its own wins
    against its gate — a subscriber whose gate rejects their own business
    churns in a month and should never be switched on. -> (activate?, detail).

    Conservative on every failure: no draft, no store, a crashing gate — all
    land in `held`, because the review queue costs a person minutes and a bad
    first report costs the customer."""
    try:
        today = _now()[:10]
        rows = [r for r in subscriptions.read_all(home)
                if r.get('sub_id') == sub_id]
        if not rows:
            return False, 'no draft subscription on file'
        draft = max(rows, key=lambda r: int(r.get('version') or 1))
        import pandas as pd
        import feedback
        import relevance as rel
        store = Path(home) / 'store'
        tenders = pd.read_parquet(store / 'tenders.parquet')
        awards = pd.read_parquet(store / 'awards.parquet')
        wins = feedback.wins_of(awards, tenders, feedback.award_names(draft))
        if not wins:
            return True, 'no resolvable wins to check against'
        gate = rel.Gate(str(home), as_of=today)
        verdicts = [feedback._verdict(rel, gate, draft, pid, lid)
                    for pid, lid, _ in wins.values()]
        n_in = sum(v in ('in', 'ungated') for v in verdicts)
        if n_in == 0:
            return False, f'gate rejects all {len(verdicts)} own wins'
        # activate: append the activating version through subscriptions.py
        subscriptions.append_version(home, {
            **{k: draft[k] for k in draft
               if k in subscriptions.KNOWN and draft[k] is not None},
            'version': int(draft.get('version') or 1) + 1,
            'effective_from': today, 'active': True})
        return True, f'{n_in}/{len(verdicts)} own wins pass'
    except Exception as e:                                     # noqa: BLE001
        return False, f'pre-flight failed: {e!r}'


def get_feedback(ctx, row):
    lot = _lots_by_key(ctx['data_dir']).get((row['procedure_id'], row['lot_id']))
    ident = (f'{esc(lot.title or "")} — {esc(lot.buyer_name or "")}'
             if lot is not None else
             f"{esc(row['procedure_id'])} · {esc(row['lot_id'])}")
    return page('Rückmeldung', f"""
      <h1>Rückmeldung zu einer Ausschreibung</h1>
      <dl><dt>Ausschreibung</dt><dd>{ident}</dd>
          <dt>Ihre Angabe</dt><dd>{esc(row['verdict'])}</dd></dl>
      <form method="post"><p>
        <button type="submit">Bestätigen: {esc(row['verdict'])}</button>
      </p></form>
      <p class="muted">Erst der Klick speichert etwas.</p>""")


def post_feedback(ctx, row, form):
    home = ctx['data_dir']
    _event(home, 'feedback', row['sub_id'],
           procedure_id=row['procedure_id'], lot_id=row['lot_id'],
           verdict=row['verdict'])
    tokens.mark_used(home, row['token'])
    return page('Danke', """
      <h1>Danke</h1>
      <p>Ihre Rückmeldung ist gespeichert und fließt in die nächsten
         Berichte ein.</p>""")


def get_stop(ctx, row):
    return page('Abbestellen', """
      <h1>Abbestellen</h1>
      <form method="post">
        <p><button name="wahl" value="berichte" type="submit">
           Keine Berichte mehr</button></p>
        <p><button name="wahl" value="alles" type="submit" class="secondary">
           Keine E-Mails mehr</button></p>
      </form>
      <p class="muted">„Keine Berichte mehr" lässt gelegentliche
         Ergebnis-Nachrichten zu; „Keine E-Mails mehr" beendet alles,
         dauerhaft.</p>""")


def post_stop(ctx, row, form):
    home = ctx['data_dir']
    # A mail client's own unsubscribe button POSTs `List-Unsubscribe=One-Click`
    # (RFC 8058) to the List-Unsubscribe URL — no person chose between the two
    # buttons, so it is the ambiguous signal LAUNCH.md 3 maps to HARD.
    hard = form.get('wahl') == 'alles' or 'List-Unsubscribe' in form
    tokens.mark_used(home, row['token'])
    stop_customer(home, row['sub_id'], hard)
    if hard:
        return page('Abbestellt', """
          <h1>Abbestellt</h1>
          <p>Sie erhalten keine E-Mails mehr von uns — keine Berichte, keine
             Ergebnis-Nachrichten, nichts. Dauerhaft.</p>""")
    return page('Berichte abbestellt', """
      <h1>Berichte abbestellt</h1>
      <p>Die Berichte sind aus. Gelegentliche
         Ergebnis-Nachrichten können noch kommen.</p>
      <form method="post"><p>
        <button name="wahl" value="alles" type="submit" class="secondary">
          Auch das nicht — keine E-Mails mehr</button></p></form>""")


# One cached view of the lot store for the recall box: pub number -> identity.
# Reloaded when the parquet's mtime moves (the cycle rebuilds it weekly).
_store_cache = {'mtime': None, 'by_pub': {}, 'by_key': {}}


def _lots_by_pub(home):
    p = Path(home) / 'store' / 'tenders.parquet'
    if not p.exists():
        return {}
    mtime = p.stat().st_mtime
    if _store_cache['mtime'] != mtime:
        import pandas as pd
        df = pd.read_parquet(p, columns=['procedure_id', 'lot_id', 'title',
                                         'buyer_name', 'deadline_date',
                                         'publication_number', 'cpv_main',
                                         'place_nuts3'])
        by, by_key = {}, {}
        for r in df.itertuples(index=False):
            if r.publication_number == r.publication_number and r.publication_number:
                by[str(r.publication_number)] = r
            by_key.setdefault((r.procedure_id, r.lot_id), r)
        _store_cache.update(mtime=mtime, by_pub=by, by_key=by_key)
    return _store_cache['by_pub']


def _lots_by_key(home):
    """(procedure_id, lot_id) -> lot identity, for the feedback page: the
    token names the lot by key, the customer must see its title."""
    _lots_by_pub(home)
    return _store_cache['by_key']


def get_recall(ctx, row):
    return page('Haben wir eine Ausschreibung übersehen?', """
      <h1>Haben wir eine Ausschreibung übersehen?</h1>
      <p>Sie haben eine Ausschreibung gefunden, die nicht in Ihrem Bericht
         stand? Tragen Sie hier ihre Nummer oder den Link ein — wir sagen
         Ihnen, wie wir sie für Ihren Betrieb einschätzen, und warum sie
         nicht in Ihrer Liste stand.</p>
      <form method="post">
        <p><input type="text" name="ref" required
                  placeholder="Nummer oder Link hier"></p>
        <p><button type="submit">Prüfen</button></p>
      </form>""")


def post_recall(ctx, row, form):
    """doc/APP.md 6: a submission is a question, never a fact. The echo of the
    lot's identity is the error check; learning happens only when the lot
    fits the customer's own market."""
    home = ctx['data_dir']
    ref = (form.get('ref') or '').strip()
    m = re.search(r'(\d{1,8}-\d{4})', ref)
    lot = _lots_by_pub(home).get(m.group(1)) if m else None
    if lot is None:
        _event(home, 'recall', row['sub_id'], detail=f'unresolved: {ref[:80]}')
        return page('Nicht gefunden', f"""
          <h1>Nicht gefunden</h1>
          <p>Zu „{esc(ref[:80])}" kennen wir keine Ausschreibung. Prüfen Sie
             die Nummer (Form <em>00123456-2026</em>), oder schreiben Sie uns:
             <a href="mailto:{esc(CONTACT)}">{esc(CONTACT)}</a>.</p>""")

    today = _now()[:10]
    sub = subscriptions.one(home, today, row['sub_id'])
    # "fits the customer's profile (trade, plausible region)" — the market
    # filter IS that check, and it decides learn vs review-queue below.
    fits = bool(sub) and subscriptions.in_market(sub, {
        'cpv_main': lot.cpv_main, 'place_nuts3': lot.place_nuts3})
    verdict = 'wird geprüft'
    try:
        import feedback
        import relevance as rel
        gate = rel.Gate(str(home), as_of=today)
        v = feedback._verdict(rel, gate, sub, lot.procedure_id, lot.lot_id) \
            if sub else 'unknown'
        verdict = {'in': 'passend — würden wir empfehlen',
                   'out': 'Ihr Geschäft kann es sein, aber wir erwarten viele '
                          'Bieter — darum nicht empfohlen',
                   'ungated': 'in Ihrem Marktfilter',
                   'unknown': 'noch nicht bewertet'}.get(v, v)
    except Exception as e:                                     # noqa: BLE001
        print(f'[app] recall verdict unavailable: {e!r}')
    if fits:
        # Learning (APP.md 6): fits trade+region -> learned_ref. Doesn't fit
        # -> the event below is the review-queue entry and nothing more. A
        # wrong number can waste a click; it must never poison a profile.
        import feedback as fb
        fb.append_learned(home, [{
            'ts': _now(), 'sub_id': row['sub_id'], 'pub': m.group(1),
            'procedure_id': lot.procedure_id, 'lot_id': lot.lot_id,
            'source': 'recall'}])
    _event(home, 'recall', row['sub_id'], procedure_id=lot.procedure_id,
           lot_id=lot.lot_id, detail=f'{m.group(1)} fits={fits}')
    return page('Unsere Einschätzung', f"""
      <h1>Unsere Einschätzung</h1>
      <dl><dt>Ausschreibung</dt><dd>{esc(lot.title or m.group(1))}</dd>
          <dt>Vergabestelle</dt><dd>{esc(lot.buyer_name or '—')}</dd>
          <dt>Frist</dt><dd>{esc(str(lot.deadline_date or '—')[:10])}</dd>
          <dt>Einschätzung</dt><dd>{esc(verdict)}</dd></dl>
      <p class="muted">Stimmt hier etwas nicht, antworten Sie einfach auf
         Ihren Wochenbericht.</p>""")


# ------------------------------------------------------- the yes-link (/y/)

PRICE_ENV = 'TM_PRICE_LINE'      # e.g. "179 € im Monat, monatlich kündbar";
                                 # unset until the price is decided (LAUNCH.md 6)
STRIPE_ENV = 'TM_STRIPE_URL'     # the payment link; unset = "wir melden uns"


def _paid(home, sub_id, today):
    sub = subscriptions.one(home, today, sub_id)
    return bool(sub) and sub.get('plan') == 'paid'


def get_subscribe(ctx, row):
    home = ctx['data_dir']
    cust = subscriptions.customer_get(home, row['sub_id']) or {}
    firm = cust.get('name') or row['sub_id']
    if _paid(home, row['sub_id'], _now()[:10]):
        return page('Sie sind dabei', f"""
          <h1>Sie sind dabei</h1>
          <p>Für <strong>{esc(firm)}</strong> laufen die Berichte weiter.
             Fragen: <a href="mailto:{esc(CONTACT)}">{esc(CONTACT)}</a>.</p>""")
    price = os.environ.get(PRICE_ENV, '').strip()
    price_line = (f'<p>Preis: <strong>{esc(price)}</strong>.</p>' if price else
                  '<p>Den Preis nennen wir Ihnen persönlich, bevor etwas '
                  'berechnet wird — mit dem Klick entsteht noch keine '
                  'Zahlungspflicht.</p>')
    return page('Weiter mit Murara', f"""
      <h1>Weiter mit Murara</h1>
      <p>Für <strong>{esc(firm)}</strong>: die Berichte laufen weiter — sobald
         es passende Ausschreibungen gibt, geprüft jede Woche. Monatlich
         beendbar, jederzeit, mit einem Klick in jeder E-Mail.</p>
      {price_line}
      <form method="post">
        <p><button type="submit">Ja, weiter mit Murara</button></p>
      </form>""")


def post_subscribe(ctx, row, form):
    """The yes (LAUNCH.md 3, ONBOARDING.md 9.5): one event, one new version
    with `plan: paid`, then the payment link if there is one — or the
    promise that a person follows up. Idempotent: a second yes changes
    nothing and says so."""
    home = ctx['data_dir']
    today = _now()[:10]
    if _paid(home, row['sub_id'], today):
        return get_subscribe(ctx, row)
    rows = [r for r in subscriptions.read_all(home)
            if r.get('sub_id') == row['sub_id']]
    speaking = subscriptions.resolve(rows, today)
    base = speaking[0] if speaking else (max(rows, key=lambda r: int(r.get('version') or 1)) if rows else None)
    if base is None:
        return get_invalid(ctx)
    subscriptions.append_version(home, {
        **{k: base[k] for k in base
           if k in subscriptions.KNOWN and base[k] is not None},
        'version': max(int(r.get('version') or 1) for r in rows) + 1,
        'effective_from': today, 'active': True, 'plan': 'paid'})
    tokens.mark_used(home, row['token'])
    _event(home, 'subscribe_yes', row['sub_id'])
    stripe = os.environ.get(STRIPE_ENV, '').strip()
    if not stripe:
        try:
            import mailer
            cust = subscriptions.customer_get(home, row['sub_id']) or {}
            mailer.send(home, 'operator', 'operator',
                        f"[Murara] Ja von {cust.get('name') or row['sub_id']}",
                        f"<p>{esc(row['sub_id'])} hat auf 'weiter' geklickt "
                        f"({esc(cust.get('contact_email') or '—')}). "
                        f"Kein Stripe-Link gesetzt — bitte melden.</p>",
                        to=CONTACT)
        except Exception as e:                                 # noqa: BLE001
            print(f'[app] operator mail not sent ({e}); the yes is recorded')
        return page('Danke', """
          <h1>Danke — Sie sind dabei</h1>
          <p>Die Berichte laufen weiter. Wir melden uns bei Ihnen wegen der
             Rechnung; bis dahin ändert sich nichts.</p>""")
    return page('Danke', f"""
      <h1>Danke — Sie sind dabei</h1>
      <p>Die Berichte laufen weiter. Der letzte Schritt ist die Zahlung:</p>
      <p><a href="{esc(stripe)}"
            style="display:inline-block;padding:6px 14px;border:1px solid #2a6;
                   border-radius:4px">Zur Zahlung</a></p>
      <p class="muted">Die Zahlungsseite betreibt unser Zahlungsdienstleister;
         sie ist von dieser Seite getrennt.</p>""")


# ------------------------------------------------------------- the admin page

ADMIN_OPEN_VAR = 'TM_ADMIN_OPEN'      # laptop only; never set on the server


def _admin_allowed(environ):
    """doc/ADMIN.md 5: the page is served only for a request the TLS edge
    marked after basic auth. The app port is loopback-bound, so nothing but
    the edge can set the header; a mis-configured edge therefore hides the
    page rather than exposing it. TM_ADMIN_OPEN=1 opens it for development."""
    if os.environ.get(ADMIN_OPEN_VAR, '').strip() == '1':
        return True
    return (environ or {}).get('HTTP_X_MURARA_ADMIN') == '1'


def _query(environ, key, default=''):
    return parse_qs((environ or {}).get('QUERY_STRING', '')).get(
        key, [default])[0]


def admin_page(ctx, q, **kw):
    import admin
    home = ctx['data_dir']
    return page('Firmen', admin.list_html(home, q, admin.state_of(home), **kw))


def get_admin(ctx, environ):
    return admin_page(ctx, _query(environ, 'q'))


def post_admin_invite(ctx, form):
    import invite
    q = (form.get('company') or '').strip()
    try:
        _sub, url = invite.add(ctx['data_dir'], q,
                               channel=(form.get('channel') or 'linkedin'))
        return admin_page(ctx, q, url=url, url_firm=q)
    except invite.InviteError as e:
        return admin_page(ctx, q, error=str(e))


def post_admin_reissue(ctx, form):
    import invite
    sub_id = (form.get('sub_id') or '').strip()
    state_name = (subscriptions.customer_get(ctx['data_dir'], sub_id)
                  or {}).get('name') or sub_id
    try:
        _sub, url = invite.reissue(ctx['data_dir'], sub_id)
        return admin_page(ctx, state_name, url=url, url_firm=state_name)
    except invite.InviteError as e:
        return admin_page(ctx, state_name, error=str(e))


def post_admin_sent(ctx, form):
    """„verschickt": the operator has actually written to this firm
    (doc/ADMIN.md 3). Minting a link is not contact — this event is what the
    funnel counts, and what tells a silent firm apart from an unwritten one.
    The channel is copied from the invitation, so the count stays per
    channel."""
    home = ctx['data_dir']
    sub_id = (form.get('sub_id') or '').strip()
    cust, firm = _admin_firm(home, sub_id)
    if not cust:
        return not_found(ctx)
    channel = ''
    for e in ledger.read(home, 'app_events'):
        if e['sub_id'] == sub_id and e['kind'] in ('invited', 'reissued'):
            channel = next((p.split('=', 1)[1]
                            for p in (e.get('detail') or '').split()
                            if p.startswith('channel=')), channel)
    _event(home, 'invite_sent', sub_id,
           detail=f'channel={channel or "-"} (operator)')
    return admin_page(ctx, firm)


def get_admin_message(ctx, environ):
    """The text to paste into LinkedIn (doc/ONBOARDING.md 9.2a): live picks
    for this firm, its own win, the link. Two versions — the 300-character
    connection note, and the message after the contact is accepted."""
    import mailer
    import pitch
    home = ctx['data_dir']
    sub_id = _query(environ, 'sub_id')
    cust, firm = _admin_firm(home, sub_id)
    if not cust:
        return not_found(ctx)
    value = tokens.live_value(home, 't', sub_id)
    url = (f'{mailer.app_url()}/t/{value}' if value else None)
    m = pitch.message(home, sub_id, url or f'{mailer.app_url()}/t/…',
                      company=firm)
    n = len(m['picks'])
    hidden = f'<input type="hidden" name="sub_id" value="{esc(sub_id)}">'
    reissue = (f'<form method="post" action="/admin/reissue" '
               f'style="display:inline">{hidden}'
               '<button type="submit" class="secondary">Neuen Link erzeugen'
               '</button></form>')
    warn = ('' if url else
            '<p style="background:#fde8e8;border-left:3px solid #c44;'
            'padding:10px 12px">Für diese Firma gibt es keinen offenen '
            'Einladungslink mehr — sonst zeigt die Nachricht nur Punkte. '
            f'{reissue}</p>')
    # marking as sent belongs next to the text that was sent (doc/ADMIN.md
    # 3a); offered until the row is `angeschrieben`, then no longer
    already = any(e['kind'] == 'invite_sent'
                  for e in ledger.read(home, 'app_events')
                  if e['sub_id'] == sub_id)
    sent = ('' if already else
            f'<form method="post" action="/admin/sent" style="display:inline">'
            f'{hidden}<button type="submit">Als verschickt markieren</button>'
            '</form> ')
    return page('Nachricht', f"""
      <h1>Nachricht für {esc(firm)}</h1>
      {warn}
      <p class="muted">{n} passende offene Ausschreibung{'' if n == 1 else 'en'}
         gefunden{'' if n else '; die Nachricht führt dann mit dem eigenen '
                               'Auftrag der Firma'}.</p>
      <h2>Kontaktanfrage (max. 300 Zeichen)</h2>
      <textarea rows="5" style="width:100%" onclick="this.select()"
         readonly>{esc(m['short'])}</textarea>
      <p class="muted">{len(m['short'])} Zeichen.</p>
      <h2>Nachricht nach dem Kontakt</h2>
      <textarea rows="18" style="width:100%" onclick="this.select()"
         readonly>{esc(m['long'])}</textarea>
      <p>{sent}{reissue if url else ''}</p>
      <p><a href="/admin?q={esc(firm)}">zurück zur Liste</a></p>""")


def _admin_firm(home, sub_id):
    cust = subscriptions.customer_get(home, sub_id) or {}
    return cust, (cust.get('name') or sub_id)


def get_admin_email(ctx, environ):
    """The form: one address, one sentence saying where the consent came
    from. Both required — an address entered by us without a consent record
    is exactly what the notice in every mail promises we do not have."""
    home = ctx['data_dir']
    sub_id = _query(environ, 'sub_id')
    cust, firm = _admin_firm(home, sub_id)
    if not cust:
        return not_found(ctx)
    current = (f'<p class="muted">Bisher: {esc(cust["contact_email"])}, '
               f'Einwilligung {esc(str(cust.get("consent_at") or "")[:10])}</p>'
               if cust.get('contact_email') else
               '<p class="muted">Bisher keine Adresse hinterlegt.</p>')
    return page('E-Mail eintragen', f"""
      <h1>E-Mail eintragen</h1>
      <p><strong>{esc(firm)}</strong></p>
      {current}
      <form method="post" action="/admin/email">
        <input type="hidden" name="sub_id" value="{esc(sub_id)}">
        <p><label>E-Mail-Adresse<br>
           <input type="email" name="email" required
                  value="{esc(cust.get('contact_email') or '')}"></label></p>
        <p><label>Einwilligung — woher?<br>
           <input type="text" name="consent" required
                  placeholder="Telefonat 17.08., Herr Dunkel, möchte die Berichte">
           </label></p>
        <p><button type="submit">Eintragen und aktivieren</button>
           <a href="/admin?q={esc(firm)}">abbrechen</a></p>
      </form>
      <p class="muted">Damit beginnt die Testphase: Vorprüfung gegen die
         eigenen Aufträge der Firma, danach Bestätigungsmail und Berichte,
         sobald es passende Ausschreibungen gibt.</p>""")


def post_admin_email(ctx, form):
    home = ctx['data_dir']
    sub_id = (form.get('sub_id') or '').strip()
    email = (form.get('email') or '').strip()
    consent = (form.get('consent') or '').strip()
    _cust, firm = _admin_firm(home, sub_id)
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
        return admin_page(ctx, firm, error=f'Keine gültige Adresse: {email!r}')
    if not consent:
        return admin_page(ctx, firm,
                          error='Ohne Einwilligungsnachweis wird keine '
                                'Adresse eingetragen.')
    ok, detail = activate(home, sub_id, email, consent_note=consent)
    return admin_page(ctx, firm, note=(
        f'{firm}: {email} eingetragen — '
        + ('aktiv, die Testphase läuft.' if ok else
           f'zurückgestellt ({detail}). Kein Bericht, bis das Profil '
           f'stimmt.')))


def get_admin_stop(ctx, environ):
    home = ctx['data_dir']
    sub_id = _query(environ, 'sub_id')
    cust, firm = _admin_firm(home, sub_id)
    if not cust:
        return not_found(ctx)
    return page('Stoppen', f"""
      <h1>Stoppen</h1>
      <p><strong>{esc(firm)}</strong> — was hat die Firma gesagt?</p>
      <form method="post" action="/admin/stop">
        <input type="hidden" name="sub_id" value="{esc(sub_id)}">
        <p><button name="wahl" value="berichte" type="submit">
           Keine Berichte mehr</button></p>
        <p><button name="wahl" value="alles" type="submit" class="secondary">
           Keine E-Mails mehr — dauerhaft</button></p>
        <p><a href="/admin?q={esc(firm)}">abbrechen</a></p>
      </form>
      <p class="muted">„Keine Berichte" lässt gelegentliche
         Ergebnis-Nachrichten zu. „Keine E-Mails" ist der Widerspruch nach
         Art. 21 DSGVO: dauerhaft, und alle offenen Links der Firma werden
         ungültig.</p>""")


def post_admin_stop(ctx, form):
    home = ctx['data_dir']
    sub_id = (form.get('sub_id') or '').strip()
    _cust, firm = _admin_firm(home, sub_id)
    hard = form.get('wahl') == 'alles'
    stop_customer(home, sub_id, hard, source='admin')
    return admin_page(ctx, firm, note=(
        f'{firm}: ' + ('keine E-Mails mehr, dauerhaft; alle Links ungültig.'
                       if hard else
                       'keine Berichte mehr; Ergebnis-Nachrichten weiter '
                       'möglich.')))


def get_experiments(ctx, environ):
    """The operator's A/B overview (doc/EXPERIMENTS.md §9): open experiments
    with their verdict line and per-arm tables, closed ones, the constants.
    Behind the same door as the rest of /admin — the edge's basic auth, one
    credential for the operator's whole page. Read-only, GET only."""
    import experiments
    from datetime import date
    stamp, _age = _freshness(ctx['data_dir'])
    body = experiments.render_html(ctx['data_dir'], config.models_root(),
                                   date.today().isoformat(), last_cycle=stamp)
    return page('Experimente',
                body + '<p><a href="/admin">zurück zur Liste</a></p>')


ADMIN_ROUTES = {
    'invite': (None, post_admin_invite),
    'reissue': (None, post_admin_reissue),
    'email': (get_admin_email, post_admin_email),
    'message': (get_admin_message, None),
    'sent': (None, post_admin_sent),
    'stop': (get_admin_stop, post_admin_stop),
    'experiments': (get_experiments, None),
}


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

# prefix -> (token purpose, GET handler, POST handler). The purpose is what
# makes a feedback link useless as a stop link (tokens.py).
TOKEN_ROUTES = {
    't': ('t', get_signup, post_signup),
    'f': ('f', get_feedback, post_feedback),
    's': ('s', get_stop, post_stop),
    'c': ('c', get_recall, post_recall),
    'y': ('y', get_subscribe, post_subscribe),
}


def rate_limited(ctx):
    return page('Zu viele Anfragen', """
      <h1>Zu viele Anfragen</h1>
      <p class="muted">Bitte versuchen Sie es in einer Minute erneut.</p>""",
                status='429 Too Many Requests')


def route(ctx, method, path, environ=None):
    """-> (status, content_type, body). The only place a request becomes a
    page, and the only place the GET/POST rule is enforced."""
    if path in STATIC:
        if method != 'GET':
            return not_yet(ctx)
        return STATIC[path](ctx)

    parts = [p for p in path.split('/') if p]
    if parts and parts[0] == 'admin':
        # doc/ADMIN.md: the operator's page. Unmarked requests get the same
        # 404 as any unknown path — the page never announces itself.
        if not _admin_allowed(environ):
            return not_found(ctx)
        if len(parts) == 1:
            return (get_admin(ctx, environ) if method == 'GET'
                    else not_yet(ctx))
        pair = ADMIN_ROUTES.get(parts[1]) if len(parts) == 2 else None
        if pair is None:
            return not_found(ctx)
        on_get, on_post = pair
        if method == 'GET':
            return on_get(ctx, environ) if on_get else not_yet(ctx)
        if method == 'POST':
            return on_post(ctx, _form(environ or {}))
        return not_yet(ctx)

    if len(parts) == 2 and parts[0] in TOKEN_ROUTES:
        # the brake sits before resolve(), so enumeration attempts pay it too
        ip = (environ or {}).get('REMOTE_ADDR', '?')
        if _limited(ip):
            return rate_limited(ctx)
        purpose, on_get, on_post = TOKEN_ROUTES[parts[0]]
        row = tokens.resolve(ctx['data_dir'], purpose, parts[1])
        if row is None:
            return get_invalid(ctx)
        if method == 'GET':
            return on_get(ctx, row)
        if method == 'POST':
            return on_post(ctx, row, _form(environ or {}))
        return not_yet(ctx)

    return not_found(ctx)


def application(environ, start_response):
    ctx = {'data_dir': environ.get('tm.data_dir') or config.data_root()}
    status, ctype, body = route(ctx, environ.get('REQUEST_METHOD', 'GET'),
                                environ.get('PATH_INFO', '/'), environ)
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


# A token in a URL is a capability, and wsgiref's default access log prints
# the full request line — straight into `docker logs`, in violation of
# APP.md 3 (first 8 characters only, tokens.short). This handler is the fix:
# every log line has its token truncated BEFORE it is written anywhere.
_TOKEN_IN_PATH = re.compile(r'(/[tfsc]/[A-Za-z0-9_-]{8})[A-Za-z0-9_-]+')


class ScrubbingHandler(WSGIRequestHandler):
    def log_message(self, format, *args):          # noqa: A002 (stdlib name)
        scrubbed = tuple(_TOKEN_IN_PATH.sub(r'\1…', str(a)) for a in args)
        super().log_message(format, *scrubbed)


class ThreadingServer(ThreadingMixIn, WSGIServer):
    """One slow request must not queue every other customer behind it —
    stdlib's default server is single-threaded."""
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', default=config.data_root())
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8000)
    args = ap.parse_args()
    print(f'[app] data root: {config.describe(args.data_dir)}')
    print(f'[app] listening on http://{args.host}:{args.port}')
    with make_server(args.host, args.port, make_app(args.data_dir),
                     server_class=ThreadingServer,
                     handler_class=ScrubbingHandler) as httpd:
        httpd.serve_forever()


if __name__ == '__main__':
    main()
