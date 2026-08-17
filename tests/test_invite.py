"""invite.py — doc/ONBOARDING.md 9.2. Own temp directory, own target list,
no real data — a miniature store is written per test; the app is driven through its WSGI callable to prove the URL
`add` prints is a working signup page."""
import json
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

DUNKEL = 'Jens Dunkel Glas- und Bauelemente GmbH'

# The store, in miniature: four winner spellings, one of them with too few
# contract-notice refs, two that share a prefix. Same shape as the real
# parquet files and sidecar index; contact details in one award-notice XML.
AWARDS = [
    # company, procedure, lot, award pub, date, buyer_nuts, n_tenders, size
    (DUNKEL, 'p1', 'LOT-0001', '00900001-2026', '2026-05-04', 'DE7', 1.0, 'small'),
    (DUNKEL, 'p2', 'LOT-0001', '00900002-2026', '2026-03-01', 'DEA1', 4.0, 'small'),
    (DUNKEL, 'p3', 'LOT-0002', '00900003-2025', '2025-11-11', 'DEA2', 3.0, 'small'),
    ('Müller Elektro GmbH', 'p4', 'LOT-0001', '00900004-2026', '2026-01-01', 'DE1', 2.0, 'micro'),
    ('Beispiel Bau GmbH', 'p5', 'LOT-0001', '00900005-2026', '2026-02-02', 'DE2', 5.0, 'small'),
    ('Beispiel Bau GmbH', 'p6', 'LOT-0001', '00900006-2026', '2026-02-03', 'DE2', 5.0, 'small'),
    ('Beispiel Bau GmbH & Co. KG', 'p7', 'LOT-0001', '00900007-2026', '2026-03-03', 'DE2', 2.0, 'small'),
    ('Beispiel Bau GmbH & Co. KG', 'p8', 'LOT-0001', '00900008-2026', '2026-03-04', 'DE2', 2.0, 'small'),
]
# contract notices behind the wins (the sidecar index); p3 has none
SIDECAR = {'p1': '00134047-2026', 'p2': '00022597-2026', 'p4': '00000001-2026',
           'p5': '00000002-2026', 'p6': '00000003-2026', 'p7': '00000005-2026',
           'p8': '00000006-2026'}
XML = """<Notice><NoticeResult/><UBLExtensions><UBLExtension><ExtensionContent>
<EformsExtension><Organizations><Organization><Company>
<PartyName><Name>{name}</Name></PartyName>
<PostalAddress><CityName>Burg</CityName><PostalZone>39288</PostalZone></PostalAddress>
<Contact><ElectronicMail>info@dunkel.biz</ElectronicMail></Contact>
</Company></Organization></Organizations></EformsExtension>
</ExtensionContent></UBLExtension></UBLExtensions></Notice>"""


def write_store(d):
    import pandas as pd
    import embed
    (d / 'store').mkdir()
    pd.DataFrame([{
        'procedure_id': p, 'lot_id': l, 'publication_number': pub,
        'publication_date': date, 'buyer_nuts': nuts, 'n_tenders': n,
        'winner_names': [c], 'winner_size': size,
        'source_file': 'a.xml' if c == DUNKEL else 'missing.xml'}
        for c, p, l, pub, date, nuts, n, size in AWARDS]
    ).to_parquet(d / 'store' / 'awards.parquet')
    pd.DataFrame([{'procedure_id': p, 'lot_id': 'LOT-0001',
                   'place_nuts3': 'DEA23' if p == 'p2' else None}
                  for p in SIDECAR]).to_parquet(d / 'store' / 'tenders.parquet')
    sd = embed.sidecar_dir(d)
    sd.mkdir(parents=True)
    with open(sd / 'lots_index.jsonl', 'w', encoding='utf-8') as f:
        for p, pub in SIDECAR.items():
            f.write(json.dumps({'procedure_id': p, 'lot_id': 'LOT-0001',
                                'publication_number': pub}) + '\n')
    (d / 'raw' / 'xml').mkdir(parents=True)
    (d / 'raw' / 'xml' / 'a.xml').write_text(XML.format(name=DUNKEL),
                                             encoding='utf-8')




class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        import gc
        self.addCleanup(gc.collect)
        write_store(self.dir)

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
        # DE7 from the buyer, DEA from the lot's place (p2) and the buyer (p3)
        self.assertEqual(draft['nuts_prefixes'], ['DE7', 'DEA'])
        self.assertEqual(draft['cpv_prefixes'], ['45'])
        self.assertEqual(draft['min_relevance'], 0.7)
        # a draft is not a live subscription: the cycle must not deliver to it
        self.assertEqual(subscriptions.load(self.dir, '2026-08-17'), [])
        ev = self.events('invited')
        self.assertEqual(len(ev), 1)
        self.assertIn('batch=b1', ev[0]['detail'])
        self.assertIn('channel=linkedin', ev[0]['detail'])
        with self.assertRaises(invite.InviteError):
            invite.add(self.dir, 'Beispiel Bau GmbH', channel='fax')
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
        # the firm's name, not the sub_id slug
        self.assertIn(DUNKEL, body)
        self.assertNotIn('jens-dunkel-glas-und-bauelemente-gmbh', body)

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
        self.assertIn('2 name(s) contain it', str(cm.exception))
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
