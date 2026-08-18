"""The operator's page — doc/ADMIN.md. Own temp directory with a miniature
store; the app is driven through its WSGI callable, so the guard, the routing
and the HTML are all exercised as a real request would."""
import os
import base64
import threading
import shutil
import subprocess
import sys
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import admin                                                    # noqa: E402
import app                                                      # noqa: E402
import invite                                                   # noqa: E402
import subscriptions                                            # noqa: E402
from tests.test_invite import DUNKEL, write_store               # noqa: E402


NEWLINE = chr(10)
LF = b'\n'
CR = b'\r'
ESC = bytes([27])
DEL = bytes([127])

def request(data_dir, path, method='GET', form=None, admin_header=True,
            query=''):
    """A request with the header the TLS edge sets after basic auth."""
    import io
    from urllib.parse import urlencode
    captured = {}

    def start_response(status, headers):
        captured['status'] = status
        captured['headers'] = dict(headers)

    body_in = urlencode(form or {}).encode('utf-8')
    environ = {'REQUEST_METHOD': method, 'PATH_INFO': path,
               'REMOTE_ADDR': '127.0.0.1', 'QUERY_STRING': query,
               'CONTENT_LENGTH': str(len(body_in)),
               'wsgi.input': io.BytesIO(body_in)}
    if admin_header:
        environ['HTTP_X_MURARA_ADMIN'] = '1'
    body = app.make_app(data_dir)(environ, start_response)
    return captured['status'], captured['headers'], b''.join(body).decode('utf-8')


def add_firm(d, company, titles, size='small'):
    """Give `company` one won lot per title in the miniature store, each with
    its own procedure and contract notice, so `admin.index` reads them as the
    references the gate would build a profile from, and rebuilds the index
    file — a request never does that itself."""
    import pandas as pd
    store = d / 'store'
    aw, tn = (pd.read_parquet(store / 'awards.parquet'),
              pd.read_parquet(store / 'tenders.parquet'))
    slug = ''.join(ch for ch in company.casefold() if ch.isalnum())[:10]
    a, t = [], []
    for i, title in enumerate(titles):
        pid = f'{slug}{i}'
        a.append({'procedure_id': pid, 'lot_id': 'LOT-0001',
                  'publication_number': f'0099{i}000-2026',
                  'publication_date': f'2026-04-0{i + 1}', 'buyer_nuts': 'DE3',
                  'n_tenders': 3.0, 'winner_names': [company],
                  'winner_size': size, 'source_file': 'missing.xml'})
        t.append({'procedure_id': pid, 'lot_id': 'LOT-0001',
                  'place_nuts3': None, 'title': title,
                  'buyer_name': 'Stadt Musterhausen'})
    pd.concat([aw, pd.DataFrame(a)], ignore_index=True).to_parquet(
        store / 'awards.parquet')
    pd.concat([tn, pd.DataFrame(t)], ignore_index=True).to_parquet(
        store / 'tenders.parquet')
    admin.build_index(d)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        import gc
        self.addCleanup(gc.collect)
        write_store(self.dir)
        admin.build_index(self.dir)     # what the cycle and the deploy do
        admin._cache.update(mtime=None, firms=None)   # per-process index
        app._hits.clear()


class Guard(Base):
    def test_without_the_edge_header_the_page_does_not_exist(self):
        status, _, body = request(self.dir, '/admin', admin_header=False)
        self.assertEqual(status, '404 Not Found')
        self.assertNotIn('Firmen', body)
        # ... unless development mode is on
        with mock.patch.dict(os.environ, {'TM_ADMIN_OPEN': '1'}):
            status, _, body = request(self.dir, '/admin', admin_header=False)
        self.assertEqual(status, '200 OK')

    def test_get_never_mutates_and_unknown_admin_paths_are_404(self):
        status, _, _ = request(self.dir, '/admin/invite')
        self.assertEqual(status, '405 Method Not Allowed')
        status, _, _ = request(self.dir, '/admin/nope', 'POST')
        self.assertEqual(status, '404 Not Found')
        status, _, _ = request(self.dir, '/admin', 'POST')
        self.assertEqual(status, '405 Method Not Allowed')


class Search(Base):
    def test_by_trade_word_from_the_lot_titles(self):
        # the miniature store's lots are titled "Dacharbeiten …" for p1
        idx = admin.index(self.dir)
        self.assertIn(DUNKEL, idx)
        state = admin.state_of(self.dir)
        rows, total = admin.search(self.dir, 'dachsanierung', state)
        self.assertEqual([r['company'] for r in rows], [DUNKEL])
        rows, _ = admin.search(self.dir, 'nichts davon', state)
        self.assertEqual(rows, [])

    def test_by_exact_name_and_by_fragment(self):
        state = admin.state_of(self.dir)
        rows, _ = admin.search(self.dir, 'Jens Dunkel Glas- und '
                                         'Bauelemente GmbH', state)
        self.assertEqual([r['company'] for r in rows], [DUNKEL])
        rows, _ = admin.search(self.dir, 'beispiel', state)
        self.assertEqual(len(rows), 2)          # GmbH and GmbH & Co. KG

    def test_the_page_shows_the_firm_and_its_numbers(self):
        _, _, body = request(self.dir, '/admin', query='q=dunkel')
        self.assertIn(DUNKEL, body)
        self.assertIn('nicht eingeladen', body)
        self.assertIn('3 Aufträge', body)
        self.assertIn('Einladen', body)


class TheTradeIsTheOneTheReportsUse(Base):
    """doc/ADMIN.md §4. The page asks the same question the delivery gate
    asks — `evidence.core_keywords` over the firm's newest wins — instead of
    a substring over every title it ever won. Three firms, one word:

      Zange & Söhne      three electrical lots, nothing else   — an electrician
      Generalbau Meier   three concrete lots and ONE electrical — not one
      Halb & Halb        two electrical, two painting          — half one

    The old search returned all three, in that order (Meier first: it had the
    most wins). Every assertion below is a way that could come back."""

    def setUp(self):
        super().setUp()
        # the name never says the trade — this is the TEXT deciding, and it
        # says "Elektroarbeiten" where the operator types "Elektroinstallation"
        add_firm(self.dir, 'Zange & Söhne GmbH',
                 ['Elektroarbeiten Neubau Turnhalle',
                  'Elektroarbeiten Sanierung Kita',
                  'Elektroarbeiten Verwaltungsgebäude'])
        add_firm(self.dir, 'Generalbau Meier GmbH',
                 ['Betonarbeiten Turnhalle', 'Betonarbeiten Parkhaus',
                  'Betonarbeiten Brücke', 'Elektroarbeiten Turnhalle'])
        add_firm(self.dir, 'Halb & Halb GmbH',
                 ['Elektroarbeiten Rathaus', 'Elektroarbeiten Schule',
                  'Malerarbeiten Rathaus', 'Malerarbeiten Schule'])
        self.state = admin.state_of(self.dir)

    def found(self, q):
        rows, _ = admin.search(self.dir, q, self.state)
        return [r['company'] for r in rows]

    def test_the_trade_is_derived_not_read_off_one_title(self):
        idx = admin.index(self.dir)
        self.assertIn('elektro', idx['Zange & Söhne GmbH']['core'])
        self.assertNotIn('elektro', idx['Generalbau Meier GmbH']['core'])
        self.assertIn('beton', idx['Generalbau Meier GmbH']['core'])
        self.assertIn('elektro', idx['Halb & Halb GmbH']['core'])

    def test_a_word_the_firm_never_wrote_still_finds_its_trade(self):
        # "Elektroinstallation" appears in no title of Zange & Söhne; the root
        # `elektro` is what both words are
        self.assertEqual(admin.query_roots('Elektroinstallation'), ['elektro'])
        self.assertIn('Zange & Söhne GmbH', self.found('Elektroinstallation'))

    def test_the_general_contractor_is_not_an_electrician(self):
        # not even under the exact word its own lot carries — one lot of four
        # is context, and the gate would never deliver electrical work to it
        for q in ('Elektroinstallation', 'Elektroarbeiten', 'elektro'):
            self.assertNotIn('Generalbau Meier GmbH', self.found(q),
                             f'still matched on {q!r}')
        self.assertIn('Generalbau Meier GmbH', self.found('Betonarbeiten'))

    def test_the_firm_whose_trade_this_most_is_first(self):
        found = self.found('Elektroinstallation')
        self.assertLess(found.index('Zange & Söhne GmbH'),   # 3 of 3
                        found.index('Halb & Halb GmbH'))     # 2 of 4
        self.assertEqual(
            admin.trade_strength(admin.index(self.dir)['Zange & Söhne GmbH'],
                                 ['elektro']), (1.0, 3))
        self.assertEqual(
            admin.trade_strength(admin.index(self.dir)['Halb & Halb GmbH'],
                                 ['elektro']), (0.5, 2))

    def test_a_name_search_is_still_a_plain_substring(self):
        # not every search is a trade: the operator pastes a company name off
        # a LinkedIn profile, and no root vocabulary can help with that
        self.assertEqual(admin.query_roots('Generalbau Meier'), [])
        self.assertEqual(self.found('generalbau meier'),
                         ['Generalbau Meier GmbH'])

    def test_the_row_prints_the_trade_and_the_page_says_what_it_searched(self):
        _, _, body = request(self.dir, '/admin', query='q=Elektroinstallation')
        self.assertIn('Zange &amp; Söhne GmbH', body)
        self.assertIn('<b>elektro</b>', body)             # matched root, bold
        self.assertIn('Gewerk', body)
        self.assertIn('3 von 3 Referenzen', body)         # the evidence, hover
        self.assertNotIn('Generalbau Meier', body)

    def test_a_word_that_names_no_trade_says_so_instead_of_guessing(self):
        _, _, body = request(self.dir, '/admin', query='q=xyzquatsch')
        self.assertIn('kein Gewerkswort', body)
        self.assertIn('Keine Firma gefunden', body)


class ARequestNeverBuildsTheIndex(Base):
    """2026-08-18: the first /admin after a deploy took 158 s on the server
    — to list two customers — because `search()` derived every winner's
    trade before looking at the query. Now the index is a FILE written only
    by `build_index` (the cycle, the deploy, `python admin.py --build`); a
    request reads it or, when it is missing, does without it. Every
    assertion below is a way the 158 s could come back."""

    def test_the_empty_query_does_not_open_the_index_at_all(self):
        state = admin.state_of(self.dir)
        with mock.patch.object(admin, 'index',
                               side_effect=AssertionError('index opened')):
            rows, _ = admin.search(self.dir, '', state)
        self.assertEqual(rows, [])                       # no customers yet
        invite.add(self.dir, DUNKEL)
        state = admin.state_of(self.dir)
        with mock.patch.object(admin, 'index', return_value={}):
            rows, _ = admin.search(self.dir, '', state)
        # the numbers still come — from the awards store, for that one name
        self.assertEqual([(r['company'], r['wins']) for r in rows],
                         [(DUNKEL, 3)])

    def test_a_request_reads_the_file_and_never_derives(self):
        with mock.patch.object(admin, 'build_index',
                               side_effect=AssertionError('derived')),              mock.patch.object(admin, '_refs_of',
                               side_effect=AssertionError('derived')):
            admin._cache.update(mtime=None, firms=None)
            idx = admin.index(self.dir)
            self.assertIn(DUNKEL, idx)
            self.assertIn('dach', idx[DUNKEL]['core'])
            _, _, body = request(self.dir, '/admin', query='q=dachsanierung')
            self.assertIn(DUNKEL, body)

    def test_without_the_file_the_page_opens_and_says_so(self):
        (self.dir / admin.INDEX_FILE).unlink()
        admin._cache.update(mtime=None, firms=None)
        with mock.patch.object(admin, 'build_index',
                               side_effect=AssertionError('derived')):
            self.assertEqual(admin.index_state(self.dir), 'missing')
            status, _, body = request(self.dir, '/admin')
            self.assertEqual(status, '200 OK')
            status, _, body = request(self.dir, '/admin', query='q=dach')
            self.assertEqual(status, '200 OK')
            self.assertIn('noch nicht bereit', body)
            self.assertIn('admin.py --build', body)
            # and the customers list still works, with numbers
            invite.add(self.dir, DUNKEL)
            _, _, body = request(self.dir, '/admin')
            self.assertIn(DUNKEL, body)
            self.assertIn('3 Aufträge', body)

    def test_the_file_is_what_the_cycle_and_the_deploy_write(self):
        import json
        path = self.dir / admin.INDEX_FILE
        held = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(held['rules'], admin._rules_stamp())
        self.assertEqual(held['stamp'], list(admin._mtimes(self.dir)))
        names = {f['company'] for f in held['firms']}
        self.assertIn(DUNKEL, names)
        # the deploy calls the module: same file, same content
        import subprocess
        out = subprocess.run([sys.executable, 'admin.py', '--build',
                              '--data-dir', str(self.dir)],
                             cwd=Path(__file__).resolve().parents[1],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('index built', out.stdout)
        again = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(again['firms'], held['firms'])

    def test_a_file_from_another_store_or_other_rules_is_served_as_stale(self):
        import evidence
        self.assertEqual(admin.index_state(self.dir), 'current')
        with mock.patch.object(evidence, 'CORE_SHARE', 0.9):
            self.assertEqual(admin.index_state(self.dir), 'stale')
        edited = self.dir / 'roots.txt'
        edited.write_bytes(evidence.ROOTS_FILE.read_bytes() + b'\nreetdach\n')
        with mock.patch.object(evidence, 'ROOTS_FILE', edited):
            self.assertEqual(admin.index_state(self.dir), 'stale')
        # stale is still SERVED (a slightly old list beats none), and marked
        with mock.patch.object(evidence, 'CORE_SHARE', 0.9):
            _, _, body = request(self.dir, '/admin', query='q=dachsanierung')
        self.assertIn(DUNKEL, body)
        self.assertIn('älteren Stand', body)

    def test_a_rewritten_file_is_picked_up_without_a_restart(self):
        import time
        idx = admin.index(self.dir)
        self.assertNotIn('Neu Elektro GmbH', idx)
        time.sleep(0.05)
        add_firm(self.dir, 'Neu Elektro GmbH', ['Elektroarbeiten Schule'])
        # a new mtime, no cache reset: the process notices on its own
        self.assertIn('Neu Elektro GmbH', admin.index(self.dir))


class Status(Base):
    def test_the_words_follow_the_record(self):
        # `today` is pinned: the trial day is counted from the version's
        # effective_from, so a fixture written on one day started failing on
        # the next ("Tag 2 von 28") until this argument existed.
        def label(today='2026-08-17'):
            return admin.status_of(admin.state_of(self.dir, today=today),
                                   DUNKEL)['label']

        self.assertEqual(label(), 'nicht eingeladen')
        sub_id, url = invite.add(self.dir, DUNKEL, channel='xing')
        self.assertTrue(label().startswith('Link erzeugt · xing · '))
        subscriptions.customer_update(self.dir, sub_id,
                                      contact_email='a@b.de',
                                      consent_at='2026-08-17T09:00:00+00:00')
        self.assertEqual(label(), 'zurückgestellt')     # no active version yet
        subscriptions.append_version(self.dir, {
            'sub_id': sub_id, 'version': 2, 'active': True,
            'effective_from': '2026-08-17', 'cpv_prefixes': ['45']})
        self.assertEqual(label(), 'angemeldet · Tag 1 von 28')
        subscriptions.append_version(self.dir, {
            'sub_id': sub_id, 'version': 3, 'active': True,
            'effective_from': '2026-08-17', 'cpv_prefixes': ['45'],
            'plan': 'paid'})
        self.assertEqual(label(), 'Kunde · bezahlt')
        subscriptions.customer_update(self.dir, sub_id,
                                      contact_state='soft_stopped')
        self.assertEqual(label(), 'gestoppt (Berichte)')
        invite.objection(self.dir, DUNKEL)
        self.assertEqual(label(), 'Widerspruch')

    def test_counts_line(self):
        invite.add(self.dir, DUNKEL)
        c = admin.counts(admin.state_of(self.dir))
        self.assertEqual(c['Link erzeugt'], 1)
        self.assertEqual(c['angemeldet'], 0)


class Invite(Base):
    def test_invite_button_mints_the_url_once_and_moves_the_status(self):
        _, _, body = request(self.dir, '/admin/invite', 'POST',
                             {'company': DUNKEL, 'channel': 'linkedin'})
        self.assertIn('Einladungslink', body)
        self.assertIn('https://app.murara.eu/t/', body)
        self.assertIn('Link erzeugt · linkedin', body)
        # a second invitation is refused, visibly, and nothing is minted
        _, _, body = request(self.dir, '/admin/invite', 'POST',
                             {'company': DUNKEL, 'channel': 'linkedin'})
        self.assertIn('already belongs', body)
        self.assertNotIn('Einladungslink', body)

    def test_reissue_button_gives_a_fresh_url(self):
        _, _, first = request(self.dir, '/admin/invite', 'POST',
                              {'company': DUNKEL, 'channel': 'linkedin'})
        sub_id = 'jens-dunkel-glas-und-bauelemente-gmbh'
        _, _, second = request(self.dir, '/admin/reissue', 'POST',
                               {'sub_id': sub_id})
        self.assertIn('Einladungslink', second)
        import re
        u1 = re.findall(r'/t/([A-Za-z0-9_-]{32})', first)[0]
        u2 = re.findall(r'/t/([A-Za-z0-9_-]{32})', second)[0]
        self.assertNotEqual(u1, u2)

    def test_a_firm_that_is_not_in_the_store_is_refused_not_crashed(self):
        _, _, body = request(self.dir, '/admin/invite', 'POST',
                             {'company': 'Gibt Es Nicht GmbH'})
        self.assertIn('nicht', body.lower())
        self.assertNotIn('Traceback', body)




class Email(Base):
    def setUp(self):
        super().setUp()
        self.sub_id, _ = invite.add(self.dir, DUNKEL)
        import mailer
        self.sent = []
        self.patch = mock.patch.object(
            mailer, '_resend', lambda p: self.sent.append(p) or 'm1')
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_form_shows_the_firm_and_demands_both_fields(self):
        _, _, body = request(self.dir, '/admin/email',
                             query=f'sub_id={self.sub_id}')
        self.assertIn(DUNKEL, body)
        self.assertIn('Einwilligung', body)
        self.assertIn('Bisher keine Adresse', body)
        # no consent text -> nothing is written
        _, _, body = request(self.dir, '/admin/email', 'POST',
                             {'sub_id': self.sub_id, 'email': 'a@b.de',
                              'consent': ''})
        self.assertIn('Ohne Einwilligungsnachweis', body)
        self.assertIsNone(subscriptions.customer_get(
            self.dir, self.sub_id)['contact_email'])
        # a broken address likewise
        _, _, body = request(self.dir, '/admin/email', 'POST',
                             {'sub_id': self.sub_id, 'email': 'nope',
                              'consent': 'Telefonat'})
        self.assertIn('Keine gültige Adresse', body)

    def test_entering_an_address_activates_and_records_the_consent(self):
        _, _, body = request(self.dir, '/admin/email', 'POST',
                             {'sub_id': self.sub_id, 'email': 'chef@dunkel.biz',
                              'consent': 'Telefonat 17.08., Herr Dunkel'})
        cust = subscriptions.customer_get(self.dir, self.sub_id)
        self.assertEqual(cust['contact_email'], 'chef@dunkel.biz')
        self.assertTrue(cust['consent_at'])
        self.assertIn('Telefonat 17.08.', cust['contact_note'])
        evs = [e for e in __import__('ledger').read(self.dir, 'app_events')
               if e['kind'].startswith('signup')]
        self.assertEqual(len(evs), 1)
        self.assertIn('admin: Telefonat 17.08.', evs[0]['detail'])
        # the confirmation mail went to that address, with the footer
        self.assertEqual(self.sent[0]['to'], ['chef@dunkel.biz'])
        self.assertIn('abbestellen', self.sent[0]['html'])
        # and the list now says so
        self.assertIn('eingetragen', body)


class Stop(Base):
    def setUp(self):
        super().setUp()
        self.sub_id, self.url = invite.add(self.dir, DUNKEL)

    def test_soft_stop_from_the_admin_page(self):
        _, _, body = request(self.dir, '/admin/stop', 'POST',
                             {'sub_id': self.sub_id, 'wahl': 'berichte'})
        self.assertIn('keine Berichte mehr', body)
        cust = subscriptions.customer_get(self.dir, self.sub_id)
        self.assertEqual(cust['contact_state'], 'soft_stopped')
        evs = [e for e in __import__('ledger').read(self.dir, 'app_events')
               if e['kind'] == 'stop_soft']
        self.assertEqual(evs[0]['detail'], 'admin')

    def test_hard_stop_kills_the_links_too(self):
        _, _, body = request(self.dir, '/admin/stop', 'POST',
                             {'sub_id': self.sub_id, 'wahl': 'alles'})
        self.assertIn('dauerhaft', body)
        self.assertEqual(subscriptions.customer_get(
            self.dir, self.sub_id)['contact_state'], 'hard_stopped')
        import tokens
        self.assertIsNone(tokens.resolve(self.dir, 't',
                                         self.url.rsplit('/', 1)[1]))
        # the row shows it, and offers no further stop
        _, _, body = request(self.dir, '/admin', query='q=dunkel')
        self.assertIn('gestoppt (alles)', body)




class Message(Base):
    """doc/ONBOARDING.md 9.2a — the text the operator pastes into LinkedIn."""

    def setUp(self):
        super().setUp()
        self.sub_id, self.url = invite.add(self.dir, DUNKEL)

    def verdict(self, overall=None, **lv):
        """The last site build's file, synthesised: the fixture firm sits on
        the page „Blitzschutz und Erdung" (its wins say so). `overall` is
        the all-trades record under trade_pages.ALL."""
        import json
        import trade_pages
        doc = {'Blitzschutz und Erdung': {
            'slug': 'blitzschutz-und-erdung', 'generated': '2026-08-18',
            'figures': {'per_month': 12.0, 'median_award': 84000.0,
                        'year_scope': 12.1e6, 'low_bid': 0.12,
                        'median_bidders': 3.0, 'months': 25},
            **lv}}
        if overall:
            doc[trade_pages.ALL] = overall
        (self.dir / trade_pages.FORECAST_FILE).write_text(
            json.dumps(doc), encoding='utf-8')

    def test_the_message_says_who_we_are_and_carries_the_links(self):
        """Drafted with the operator 2026-08-18: 'wir', no person's name,
        no own-win paragraph, TED link under every pick, a signature."""
        import pitch
        m = pitch.message(self.dir, self.sub_id, 'https://a/t/x', today='2026-08-17')
        self.assertIn('Wir sind Murara', m['long'])
        self.assertNotIn('Dachsanierung', m['long'])   # the own win is gone
        self.assertNotIn(' ich ', m['long'].lower())
        self.assertNotIn('Herr', m['long'])
        self.assertIn('https://a/t/x', m['long'])
        self.assertIn('Datenschutz', m['long'])
        self.assertIn(pitch.SIGNATURE, m['long'])
        for p in m['picks']:
            if p.get('publication_number'):
                self.assertIn(pitch.TED_URL.format(pn=p['publication_number']),
                              m['long'])
        self.assertLessEqual(len(m['short']), pitch.SHORT_LIMIT)
        self.assertNotIn('http', m['short'])           # no link in the note
        self.assertTrue(m['short'].startswith('Guten Tag, wir suchen'))

    def test_the_trade_figures_and_the_edge_come_from_the_site_build(self):
        """The message quotes the trade page's numbers and — only when the
        forecast beats guessing there — its edge. No file: no figures, no
        claim, nothing false."""
        import pitch
        m = pitch.message(self.dir, self.sub_id, 'https://a/t/x', today='2026-08-17')
        self.assertIsNone(m['trade'])
        self.assertNotIn('Zahlen zu Ihrem Markt', m['long'])
        self.verdict(state='beats', checked=43, hits=7, precision=0.163,
                     base=0.10, recall=0.3, factor=1.63)
        m = pitch.message(self.dir, self.sub_id, 'https://a/t/x', today='2026-08-17')
        self.assertEqual(m['trade'], 'Blitzschutz und Erdung')
        self.assertIn('für das Gewerk Blitzschutz und Erdung', m['short'])
        self.assertIn('Betriebe im Gewerk Blitzschutz und Erdung', m['long'])
        self.assertNotIn('…', m['short'])           # nothing cut off mid-word
        self.assertIn('12 öffentliche Lose pro Monat', m['long'])
        self.assertIn('84.000 € wert', m['long'])
        self.assertIn('12 % der Lose bekommen höchstens ein Angebot', m['long'])
        self.assertIn('Im Gewerk Blitzschutz und Erdung: von 43 geprüften '
                      'Hinweisen 16 % statt 10 %, das 1,6-Fache', m['long'])
        self.assertNotIn('Über alle Gewerke', m['long'])   # no overall given
        self.assertIn('murara.eu/gewerke/blitzschutz-und-erdung/', m['long'])
        # not better than guessing: the figures stay, the claim goes
        self.verdict(state='no_better', checked=43, hits=3, precision=0.07,
                     base=0.10, recall=0.1, factor=0.7)
        m = pitch.message(self.dir, self.sub_id, 'https://a/t/x', today='2026-08-17')
        self.assertIn('12 öffentliche Lose pro Monat', m['long'])
        self.assertNotIn('geprüften Hinweisen', m['long'])
        self.assertNotIn('-Fache', m['long'])

    def test_the_overall_record_leads_and_the_trade_follows(self):
        """Operator, 2026-08-18: 'show what we are doing, what we are good
        at'. The all-trades record is the one with a real sample; it comes
        first, the trade's own line after it — each only when it beats
        guessing."""
        import pitch
        all_ = {'state': 'beats', 'checked': 1042, 'hits': 183,
                'precision': 0.176, 'base': 0.094, 'recall': 0.32,
                'factor': 1.86, 'generated': '2026-08-18'}
        # trade thin, overall strong: the overall line alone
        self.verdict(overall=all_, state='thin', checked=14, hits=2,
                     precision=0.14, base=0.10, recall=0.1)
        m = pitch.message(self.dir, self.sub_id, 'https://a/t/x', today='2026-08-17')
        self.assertIn('Über alle Gewerke endeten von 1042 geprüften Hinweisen '
                      '18 % mit höchstens einem Angebot; ohne Auswahl sind es '
                      '9 % – also das 1,9-Fache.', m['long'])
        self.assertNotIn('Im Gewerk Blitzschutz', m['long'])
        self.assertEqual(m['overall']['checked'], 1042)
        # both: overall first, trade second, in one paragraph
        self.verdict(overall=all_, state='beats', checked=43, hits=7,
                     precision=0.163, base=0.10, recall=0.3, factor=1.63)
        m = pitch.message(self.dir, self.sub_id, 'https://a/t/x', today='2026-08-17')
        i = m['long'].index('Über alle Gewerke')
        j = m['long'].index('Im Gewerk Blitzschutz')
        self.assertLess(i, j)
        self.assertNotIn('\n', m['long'][i:j])

    def test_the_message_page_and_the_row_show_the_edge_verdict(self):
        """The operator writes only where there is an edge (2026-08-18), so
        the verdict stands on the row and above the texts."""
        _, _, body = request(self.dir, '/admin/message',
                             query=f'sub_id={self.sub_id}')
        self.assertIn('Kein Vorsprung nachweisbar', body)
        self.verdict(state='beats', checked=43, hits=7, precision=0.163,
                     base=0.10, recall=0.3, factor=1.63)
        _, _, body = request(self.dir, '/admin/message',
                             query=f'sub_id={self.sub_id}')
        self.assertIn('Vorsprung im Gewerk', body)
        self.assertIn('1,6-fach', body)
        _, _, body = request(self.dir, '/admin', query='q=dunkel')
        self.assertIn('edge-yes', body)
        self.assertIn('1,6-fach — 16 % statt 10 %, 43 geprüft', body)
        self.verdict(state='thin', checked=9, hits=1, precision=0.11,
                     base=0.10, recall=0.1)
        _, _, body = request(self.dir, '/admin', query='q=dunkel')
        self.assertIn('erst 9 geprüft, Quote ab 30', body)

    def test_a_lot_without_a_deadline_is_never_offered(self):
        import ledger
        import pitch
        ledger.append(self.dir, 'predictions', [{
            'ts': '2026-08-17T08:00:00+00:00', 'model': 'm1',
            'procedure_id': 'p9', 'lot_id': 'L1', 'score': 0.99,
            'title': 'Ohne Frist', 'buyer_name': 'Stadt X',
            'deadline_date': None, 'flag': True}])
        rows = pitch.open_lots(self.dir, '2026-08-17')
        self.assertEqual([r for r in rows if r['procedure_id'] == 'p9'], [])

    def test_the_page_shows_both_texts_and_the_live_link(self):
        _, _, body = request(self.dir, '/admin/message',
                             query=f'sub_id={self.sub_id}')
        self.assertIn('Kontaktanfrage', body)
        self.assertIn('Nachricht nach dem Kontakt', body)
        self.assertIn(self.url.rsplit('/', 1)[1], body)   # the real token
        # after a hard stop the link is revoked -> the page says so
        import tokens
        tokens.revoke_all(self.dir, self.sub_id)
        _, _, body = request(self.dir, '/admin/message',
                             query=f'sub_id={self.sub_id}')
        self.assertIn('keinen offenen', body)




class Sent(Base):
    """Minting a link is not contacting anybody — doc/ADMIN.md 3."""

    def setUp(self):
        super().setUp()
        self.sub_id, _ = invite.add(self.dir, DUNKEL, channel='xing')

    def label(self):
        return admin.status_of(admin.state_of(self.dir), DUNKEL)['label']

    def test_the_two_words_and_the_counts(self):
        self.assertTrue(self.label().startswith('Link erzeugt · xing · '))
        c = admin.counts(admin.state_of(self.dir))
        self.assertEqual((c['Link erzeugt'], c['angeschrieben']), (1, 0))

        _, _, body = request(self.dir, '/admin/sent', 'POST',
                             {'sub_id': self.sub_id})
        self.assertTrue(self.label().startswith('angeschrieben · xing · '))
        self.assertIn('angeschrieben', body)
        c = admin.counts(admin.state_of(self.dir))
        self.assertEqual((c['Link erzeugt'], c['angeschrieben']), (1, 1))

    def test_the_button_is_on_the_message_page_once_and_the_event_is_ledgered(self):
        """doc/ADMIN.md 3a: „Als verschickt markieren" sits under the text
        that was sent, not on the list row; it disappears once pressed."""
        import ledger
        q = f'sub_id={self.sub_id}'
        _, _, body = request(self.dir, '/admin', query='q=dunkel')
        self.assertNotIn('verschickt', body)         # not a row action
        _, _, body = request(self.dir, '/admin/message', query=q)
        self.assertIn('Als verschickt markieren', body)
        self.assertIn('Neuen Link erzeugen', body)
        request(self.dir, '/admin/sent', 'POST', {'sub_id': self.sub_id})
        _, _, body = request(self.dir, '/admin/message', query=q)
        self.assertNotIn('verschickt markieren', body)   # already marked
        evs = [e for e in ledger.read(self.dir, 'app_events')
               if e['kind'] == 'invite_sent']
        self.assertEqual(len(evs), 1)
        self.assertIn('channel=xing', evs[0]['detail'])

    def test_the_row_offers_one_next_step_and_quiet_links(self):
        """doc/ADMIN.md 3a: one filled button per row — the next funnel
        step — the rest plain links, every label an infinitive."""
        def acts(status_prefix):
            st = admin.status_of(admin.state_of(self.dir), DUNKEL)
            self.assertTrue(st['label'].startswith(status_prefix), st['label'])
            return admin.row_actions(st)
        a = acts('Link erzeugt')
        self.assertEqual([(l, p) for l, _, p in a],
                         [('Nachricht anzeigen', True),
                          ('E-Mail eintragen', False), ('Stoppen', False)])
        request(self.dir, '/admin/sent', 'POST', {'sub_id': self.sub_id})
        a = acts('angeschrieben')
        self.assertEqual([(l, p) for l, _, p in a],
                         [('E-Mail eintragen', True),
                          ('Nachricht anzeigen', False), ('Stoppen', False)])
        self.assertEqual(sum(p for _, _, p in a), 1)
        _, _, body = request(self.dir, '/admin', query='q=dunkel')
        self.assertNotIn('<a><button', body.replace('\n', ''))
        self.assertNotIn('URL neu', body)
        self.assertNotIn('Abmelden', body)
        self.assertIn('class="btn"', body)

    def test_an_unknown_firm_is_not_marked(self):
        status, _, _ = request(self.dir, '/admin/sent', 'POST',
                               {'sub_id': 'niemand'})
        self.assertEqual(status, '404 Not Found')


class TheCredentialIsAFileNotAnEnvironmentVariable(unittest.TestCase):
    """doc/ADMIN.md 5c. The edge imports the operator's credential from
    /etc/murara/caddy.d/admin.caddy and re-reads it on `caddy reload`, so the
    file is the only truth about the password.

    It used to be TM_ADMIN_HASH in the edge's environment — a copy taken when
    the container was created. On 2026-08-18 that produced two truths at once:
    a hash written to the file at 08:22 and a different one in force until a
    deploy recreated the edge at 08:47, with the operator locked out in
    between and every check of the file saying "correct". These assertions are
    the shape of the fix; each one is a way that bug could come back."""

    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent
        self.caddyfile = (self.root / 'docker' / 'Caddyfile').read_text(encoding='utf-8')
        self.compose = (self.root / 'docker-compose.yml').read_text(encoding='utf-8')
        self.script = (self.root / 'docker' / 'admin-password.sh').read_text(encoding='utf-8')

    def test_the_caddyfile_imports_the_credential_and_reads_no_variable(self):
        self.assertIn('import /etc/caddy/secrets/admin*.caddy', self.caddyfile)
        # the placeholder form, not the name: the comment above the block
        # tells the story of why it is gone, and should keep telling it
        self.assertNotIn('{$TM_ADMIN_HASH:', self.caddyfile)
        self.assertNotIn('{$TM_ADMIN_USER:', self.caddyfile)

    def test_compose_mounts_the_directory_read_only_and_passes_no_credential(self):
        # The DIRECTORY: the writer replaces the file with a rename, and a
        # single-file bind mount would keep pointing at the old inode.
        self.assertIn('/etc/caddy/secrets:ro', self.compose)
        self.assertIn('caddy.d', self.compose)
        self.assertNotIn('admin.env', self.compose)
        self.assertNotIn('TM_ADMIN_HASH', self.compose)

    def test_setting_a_password_reloads_and_proves_it_is_in_force(self):
        self.assertIn('caddy reload', self.script)
        self.assertIn('caddy validate', self.script)      # before it is in place
        self.assertIn('IN FORCE', self.script)
        self.assertIn('--config -', self.script)          # never -u user:pass
        # A recreate is what made the gap possible; nothing here does one.
        self.assertNotIn('--force-recreate', self.script)

    def test_no_dollar_doubling_survives_anywhere(self):
        # Only compose interpolation ever required it. A Caddy snippet is read
        # by Caddy itself, so a doubled hash would now be the WRONG hash.
        self.assertNotIn("sed 's/[$]/$$/g'", self.script)
        self.assertNotIn('$$2a', self.script)


class ThePasswordPromptSurvivesAPaste(unittest.TestCase):
    """The operator's password comes out of a password manager, so it is
    PASTED. A terminal in bracketed-paste mode wraps pasted text in
    ESC[200~ ... ESC[201~, and the masked prompt in docker/admin-password.sh
    reads one byte at a time — it used to fold those markers into the
    password. Measured 2026-08-18: 17 characters pasted, 29 captured, and the
    "Repeat" entry was mangled identically so the confirmation matched. The
    hash was then of a string no browser can ever send, and /admin refused the
    right password for a morning.

    The real function is lifted out of the shipped script and run under bash,
    so this tests the code that runs and not a copy of it."""

    PW = 'Mein-Passwort-123'

    def setUp(self):
        self.bash = shutil.which('bash')
        if not self.bash:
            self.skipTest('no bash on this machine')
        script = (Path(__file__).resolve().parent.parent
                  / 'docker' / 'admin-password.sh').read_text(encoding='utf-8')
        start = script.index('read_masked() {')
        end = script.index(NEWLINE + '}' + NEWLINE, start) + 3
        self.fn = script[start:end]

    def capture(self, keystrokes):
        """-> exactly what read_masked would hand to `caddy hash-password`."""
        prog = self.fn + NEWLINE + 'read_masked "" ' + NEWLINE
        out = subprocess.run([self.bash, '-c', prog], input=keystrokes,
                             env={**os.environ, 'TM_PASSWORD_STDIN': '1'},
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return out.stdout.decode('utf-8')

    def test_typed_by_hand(self):
        self.assertEqual(self.capture(self.PW.encode() + LF), self.PW)

    def test_pasted_in_a_bracketed_paste_terminal(self):
        keys = ESC + b'[200~' + self.PW.encode() + ESC + b'[201~' + LF
        self.assertEqual(self.capture(keys), self.PW)

    def test_pasted_with_a_carriage_return(self):
        self.assertEqual(self.capture(self.PW.encode() + CR + LF), self.PW)

    def test_backspace_still_erases(self):
        self.assertEqual(self.capture(b'Mein-Passwort-1234' + DEL + LF), self.PW)

    def test_an_umlaut_survives_byte_by_byte(self):
        pw = 'Gruess-Passwort-\u00fc9'
        self.assertEqual(self.capture(pw.encode('utf-8') + LF), pw)


class TheInForceCheckSurvivesTheOperatorsPassword(unittest.TestCase):
    """`admin-password.sh set` ends by asking the live edge for /admin with the
    password it just hashed, through `curl --config -` so the password never
    reaches a process list. curl's config parser treats a backslash and a
    double quote inside a quoted value as escapes: a password containing
    either was sent mangled, or not sent at all, curl got 401, and the run
    announced NOT IN FORCE against a door that opened perfectly — measured
    2026-08-18, after it had told the operator exactly that.

    The escaping function is lifted out of the shipped script, run under bash,
    and the line it produces is handed to a real curl aimed at a listener that
    records the Authorization header. What curl SENDS is what is asserted."""

    PASSWORDS = ('simple-pass-1234',
                 'has spaces in it 12',
                 'has' + chr(34) + 'a-double-quote1',
                 'has' + chr(92) + 'a-backslash-12',
                 'both' + chr(34) + 'and' + chr(92) + 'together',
                 'has#a-hash-mark-12',
                 'hat-Umlaute-uoa-12')

    def setUp(self):
        self.bash = shutil.which('bash')
        self.curl = shutil.which('curl')
        if not self.bash or not self.curl:
            self.skipTest('needs bash and curl')
        script = (Path(__file__).resolve().parent.parent
                  / 'docker' / 'admin-password.sh').read_text(encoding='utf-8')
        start = script.index('curl_config_line() {')
        end = script.index(NEWLINE + '}' + NEWLINE, start) + 3
        self.fn = script[start:end]

        seen = []
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get('Authorization', ''))
                self.send_response(200)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'ok')

            def log_message(self, *a):
                pass

        self.seen = seen
        self.server = HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)

    def line_for(self, user, password):
        # Through the ENVIRONMENT, not argv: on Windows the argument vector is
        # rebuilt into a command line and re-parsed by MSYS bash, which eats
        # exactly the backslashes and quotes this test is about.
        prog = self.fn + NEWLINE + 'curl_config_line "$TM_U" "$TM_P"' + NEWLINE
        out = subprocess.run([self.bash, '-s'], input=prog.encode(),
                             env={**os.environ, 'TM_U': user, 'TM_P': password},
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return out.stdout.decode('utf-8')

    def sent_credentials(self, user, password):
        """-> the user:password curl actually put on the wire, or None."""
        del self.seen[:]
        line = self.line_for(user, password)
        port = self.server.server_address[1]
        subprocess.run([self.curl, '--config', '-', '-s', '-o', os.devnull,
                        '--max-time', '5',
                        'http://127.0.0.1:' + str(port) + '/admin'],
                       input=line.encode(), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        if not self.seen or not self.seen[0].startswith('Basic '):
            return None
        raw = base64.b64decode(self.seen[0].split(' ', 1)[1])
        return raw.decode('utf-8')

    def test_every_password_reaches_the_edge_unchanged(self):
        for pw in self.PASSWORDS:
            with self.subTest(password=pw):
                self.assertEqual(self.sent_credentials('murara', pw),
                                 'murara:' + pw)

    def test_the_line_escapes_a_backslash_and_a_double_quote(self):
        pw = 'has' + chr(92) + 'and' + chr(34) + 'in-it-12'
        line = self.line_for('murara', pw)
        self.assertIn(chr(92) + chr(92), line)      # backslash doubled
        self.assertIn(chr(92) + chr(34), line)      # quote escaped
        self.assertTrue(line.startswith('user = "murara:'), line)


if __name__ == '__main__':
    unittest.main()
