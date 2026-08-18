"""The operator's page — doc/ADMIN.md. Own temp directory with a miniature
store; the app is driven through its WSGI callable, so the guard, the routing
and the HTML are all exercised as a real request would."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import admin                                                    # noqa: E402
import app                                                      # noqa: E402
import invite                                                   # noqa: E402
import subscriptions                                            # noqa: E402
from tests.test_invite import DUNKEL, write_store               # noqa: E402


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


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        import gc
        self.addCleanup(gc.collect)
        write_store(self.dir)
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

    def test_the_message_leads_with_the_firms_own_win(self):
        import pitch
        m = pitch.message(self.dir, self.sub_id, 'https://a/t/x', today='2026-08-17')
        self.assertIn('Dachsanierung', m['long'])      # the fixture's own win
        self.assertIn('https://a/t/x', m['long'])
        self.assertIn('Datenschutz', m['long'])
        self.assertLessEqual(len(m['short']), pitch.SHORT_LIMIT)
        self.assertNotIn('http', m['short'])           # no link in the note

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

    def test_the_button_is_offered_once_and_the_event_is_ledgered(self):
        import ledger
        _, _, body = request(self.dir, '/admin', query='q=dunkel')
        self.assertIn('verschickt', body)
        request(self.dir, '/admin/sent', 'POST', {'sub_id': self.sub_id})
        _, _, body = request(self.dir, '/admin', query='q=dunkel')
        self.assertNotIn('verschickt', body)         # already marked
        evs = [e for e in ledger.read(self.dir, 'app_events')
               if e['kind'] == 'invite_sent']
        self.assertEqual(len(evs), 1)
        self.assertIn('channel=xing', evs[0]['detail'])

    def test_an_unknown_firm_is_not_marked(self):
        status, _, _ = request(self.dir, '/admin/sent', 'POST',
                               {'sub_id': 'niemand'})
        self.assertEqual(status, '404 Not Found')


if __name__ == '__main__':
    unittest.main()
