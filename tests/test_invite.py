"""invite.py — doc/ONBOARDING.md 9.2. Own temp directory, own target list,
no real data; the app is driven through its WSGI callable to prove the URL
`add` prints is a working signup page."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import invite                                                   # noqa: E402
import ledger                                                   # noqa: E402
import subscriptions                                            # noqa: E402
import tokens                                                   # noqa: E402
from tests.test_app import request                              # noqa: E402

HEADER = ('company,size,wins,single_bid_wins,trades,regions,last_win,'
          'profile_refs,profile_refs_n,email,phone,city,postal_zone,website,'
          'sim_picks,trade_read,trade_read3,trade_match\n')
ROWS = [
    'Jens Dunkel Glas- und Bauelemente GmbH,small,4,1,454;452,DE7;DEA,'
    '2026-05-04,00134047-2026;00022597-2026,2,info@dunkel.biz,,Burg,39288,'
    ',9,,454,True\n',
    'Müller Elektro GmbH,micro,2,0,453,DE1,2026-01-01,00000001-2026,1,'
    ',,Ulm,89073,,3,,453,True\n',
    'Beispiel Bau GmbH,small,3,0,452,DE2,2026-02-02,'
    '00000002-2026;00000003-2026;00000004-2026,3,,,Rosenheim,83022,,4,,452,'
    'True\n',
    'Beispiel Bau GmbH & Co. KG,small,2,0,452,DE2,2026-03-03,'
    '00000005-2026;00000006-2026,2,,,Rosenheim,83022,,4,,452,True\n',
]
DUNKEL = 'Jens Dunkel Glas- und Bauelemente GmbH'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        import gc
        self.addCleanup(gc.collect)
        (self.dir / 'outreach').mkdir()
        (self.dir / 'outreach' / 'targets.csv').write_text(
            HEADER + ''.join(ROWS), encoding='utf-8-sig')

    def events(self, kind=None):
        rows = ledger.read(self.dir, 'app_events')
        return [r for r in rows if kind is None or r['kind'] == kind]


class Slug(unittest.TestCase):
    def test_umlauts_and_punctuation(self):
        self.assertEqual(invite.slug(DUNKEL),
                         'jens-dunkel-glas-und-bauelemente-gmbh')
        self.assertEqual(invite.slug('Müller & Söhne GmbH & Co. KG'),
                         'muller-sohne-gmbh-co-kg')
        self.assertEqual(invite.slug('Straßenbau Groß'), 'strassenbau-gross')


class Add(Base):
    def test_writes_customer_draft_token_and_event(self):
        sub_id, url = invite.add(self.dir, DUNKEL, batch='b1',
                                 base_url='https://app.example')
        self.assertEqual(sub_id, 'jens-dunkel-glas-und-bauelemente-gmbh')
        self.assertTrue(url.startswith('https://app.example/t/'))
        cust = subscriptions.customer_get(self.dir, sub_id)
        self.assertEqual(cust['name'], DUNKEL)
        self.assertIn('39288, Burg', cust['contact_note'])
        self.assertIsNone(cust.get('consent_at'))
        versions = [r for r in subscriptions.read_all(self.dir)
                    if r['sub_id'] == sub_id]
        self.assertEqual(len(versions), 1)
        draft = versions[0]
        self.assertIs(draft['active'], False)
        self.assertEqual(draft['award_names'], [DUNKEL])
        self.assertEqual(draft['profile_refs'],
                         ['00134047-2026', '00022597-2026'])
        self.assertEqual(draft['nuts_prefixes'], ['DE7', 'DEA'])
        self.assertEqual(draft['cpv_prefixes'], ['45'])
        self.assertEqual(draft['min_relevance'], 0.7)
        # a draft is not a live subscription: the cycle must not deliver to it
        self.assertEqual(subscriptions.load(self.dir, '2026-08-17'), [])
        ev = self.events('invited')
        self.assertEqual(len(ev), 1)
        self.assertIn('batch=b1', ev[0]['detail'])
        # the token is logged short, never in full
        value = url.rsplit('/', 1)[1]
        self.assertNotIn(value, ev[0]['detail'])
        self.assertIn(tokens.short(value), ev[0]['detail'])

    def test_the_url_is_a_working_signup_page(self):
        _, url = invite.add(self.dir, DUNKEL, base_url='https://app.example')
        path = url[len('https://app.example'):]
        status, _, body = request(self.dir, path)
        self.assertEqual(status, '200 OK')
        self.assertIn('Anmeldung', body)
        self.assertIn('name="email"', body)

    def test_case_insensitive_match_and_also_name(self):
        sub_id, _ = invite.add(self.dir, DUNKEL.lower(),
                               also_names=['Dunkel Bauelemente'])
        cust = subscriptions.customer_get(self.dir, sub_id)
        self.assertEqual(cust['name'], DUNKEL)
        draft = subscriptions.read_all(self.dir)[0]
        self.assertEqual(draft['award_names'], [DUNKEL, 'Dunkel Bauelemente'])

    def test_refuses_too_few_refs(self):
        with self.assertRaises(invite.InviteError) as cm:
            invite.add(self.dir, 'Müller Elektro GmbH')
        self.assertIn('1 usable profile ref', str(cm.exception))
        self.assertEqual(subscriptions.read_all(self.dir), [])

    def test_refuses_a_second_invitation_under_any_spelling(self):
        invite.add(self.dir, DUNKEL, also_names=['Dunkel Bauelemente'])
        with self.assertRaises(invite.InviteError):
            invite.add(self.dir, DUNKEL)
        with self.assertRaises(invite.InviteError) as cm:
            invite.add(self.dir, 'Beispiel Bau GmbH & Co. KG',
                       also_names=['Dunkel Bauelemente'])
        self.assertIn('already belongs', str(cm.exception))
        self.assertEqual(len(self.events('invited')), 1)

    def test_ambiguous_name_names_the_candidates(self):
        with self.assertRaises(invite.InviteError) as cm:
            invite.add(self.dir, 'Beispiel Bau')
        self.assertIn('2 row(s) contain it', str(cm.exception))
        with self.assertRaises(invite.InviteError):
            invite.add(self.dir, 'Nicht Da GmbH')

    def test_refuses_a_hard_stopped_firm(self):
        invite.objection(self.dir, DUNKEL)
        with self.assertRaises(invite.InviteError) as cm:
            invite.add(self.dir, DUNKEL)
        self.assertIn('hard_stopped', str(cm.exception))


class Reissue(Base):
    def test_old_link_dies_new_link_works(self):
        sub_id, url1 = invite.add(self.dir, DUNKEL, base_url='https://a')
        _, url2 = invite.reissue(self.dir, sub_id, base_url='https://a')
        self.assertNotEqual(url1, url2)
        self.assertIsNone(tokens.resolve(self.dir, 't', url1.rsplit('/', 1)[1]))
        self.assertIsNotNone(tokens.resolve(self.dir, 't',
                                            url2.rsplit('/', 1)[1]))
        self.assertEqual(len(self.events('reissued')), 1)
        # by company spelling, too
        _, url3 = invite.reissue(self.dir, DUNKEL, base_url='https://a')
        self.assertNotEqual(url2, url3)

    def test_refuses_after_signup_and_for_unknown(self):
        sub_id, url = invite.add(self.dir, DUNKEL, base_url='https://a')
        request(self.dir, url[len('https://a'):], 'POST',
                {'email': 'chef@dunkel.biz'})
        with self.assertRaises(invite.InviteError) as cm:
            invite.reissue(self.dir, sub_id)
        self.assertIn('already signed up', str(cm.exception))
        with self.assertRaises(invite.InviteError):
            invite.reissue(self.dir, 'niemand')


class Objection(Base):
    def test_invited_firm_is_stopped_and_its_qr_dies(self):
        sub_id, url = invite.add(self.dir, DUNKEL, base_url='https://a')
        self.assertEqual(invite.objection(self.dir, DUNKEL, note='Brief'),
                         sub_id)
        cust = subscriptions.customer_get(self.dir, sub_id)
        self.assertEqual(cust['contact_state'], 'hard_stopped')
        status, _, body = request(self.dir, url[len('https://a'):])
        self.assertIn('nicht mehr gültig', body)
        ev = self.events('objection')
        self.assertEqual(len(ev), 1)
        self.assertIn('revoked=1', ev[0]['detail'])

    def test_never_invited_firm_gets_a_hard_stopped_row(self):
        sub_id = invite.objection(self.dir, 'Beispiel Bau GmbH')
        cust = subscriptions.customer_get(self.dir, sub_id)
        self.assertEqual(cust['contact_state'], 'hard_stopped')
        self.assertEqual(cust['name'], 'Beispiel Bau GmbH')


class Console(Base):
    def test_add_prints_sub_id_and_url_and_errors_exit_2(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        out = io.StringIO()
        with redirect_stdout(out):
            rc = invite.main(['--data-dir', str(self.dir), '--url',
                              'https://a', 'add', DUNKEL, '--batch', 'x'])
        self.assertEqual(rc, 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], 'jens-dunkel-glas-und-bauelemente-gmbh')
        self.assertTrue(lines[1].startswith('https://a/t/'))
        err = io.StringIO()
        with redirect_stderr(err):
            rc = invite.main(['--data-dir', str(self.dir), 'add', DUNKEL])
        self.assertEqual(rc, 2)
        self.assertIn('already belongs', err.getvalue())


if __name__ == '__main__':
    unittest.main()
