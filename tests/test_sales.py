"""Who is due today, and the mail that says so — doc/SALES.md 4-5.

The trigger is the point of the whole document: a firm is written to only
when we have a concrete tender for it, so the first contact can be followed
by a real recommendation. These tests pin the four ways a firm can fail to
qualify — no lot, deadline too close, wrong trade, written to recently —
because each of them, if it slipped, would produce exactly the note the
operator called bullshit on 2026-08-18.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import admin                                                    # noqa: E402
import invite                                                   # noqa: E402
import ledger                                                   # noqa: E402
import sales                                                    # noqa: E402
import subscriptions                                            # noqa: E402
from tests.test_invite import DUNKEL, write_store               # noqa: E402

TODAY = '2026-08-18'
OWNER = 'luming@murara.eu'


def predict(home, rows, nuts='DE712'):
    """Prediction rows as the cycle writes them; `rows` is
    [(procedure_id, title, buyer, deadline, flag)]; `nuts` the lot's region
    (DE712 is Hessen, where the DUNKEL fixture firm has won).

    CPV 45 and a NUTS the fixture firm won in are not decoration: the draft
    subscription `invite.add` writes carries `cpv_prefixes: ['45']` and the
    firm's own regions, and a row that cannot prove it is inside them does
    not match (SUBSCRIPTIONS.md keyless-row rule). A lot the market filter
    drops can never be a candidate, which is correct — and would make these
    tests vacuous if the fixture forgot the fields."""
    ledger.append(home, 'predictions', [{
        'ts': f'{TODAY}T07:00:00+00:00', 'model': 'm1',
        'procedure_id': p, 'lot_id': 'L1', 'score': 0.9,
        'title': t, 'buyer_name': b, 'deadline_date': d, 'flag': f,
        'cpv_main': '45312310', 'cpv3': '453', 'place_nuts3': nuts,
        'publication_number': f'009{i:05d}-2026'}
        for i, (p, t, b, d, f) in enumerate(rows)])


class Due(unittest.TestCase):
    """The fixture firm's own wins are Dachsanierung and Blitzschutzanlage,
    so its main trade page is „Blitzschutz und Erdung" — which is what the
    candidate lots must match."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        import gc
        self.addCleanup(gc.collect)
        write_store(self.dir)
        admin.build_index(self.dir)
        admin._cache.update(mtime=None, firms=None)
        env = mock.patch.dict(os.environ, {'TM_SALES_OWNER': OWNER})
        env.start()
        self.addCleanup(env.stop)
        self.sub_id, _ = invite.add(self.dir, DUNKEL, mint=False, owner=OWNER)

    def test_the_main_trade_and_a_far_enough_deadline_make_a_firm_due(self):
        predict(self.dir, [
            ('p10', 'Blitzschutzanlage Feuerwache', 'Stadt Nord',
             '2026-09-30', True)])
        rows = sales.due(self.dir, TODAY)
        self.assertEqual([r['company'] for r in rows], [DUNKEL])
        r = rows[0]
        self.assertEqual((r['n_lots'], r['next_deadline'], r['owner']),
                         (1, '2026-09-30', OWNER))
        self.assertEqual(r['trade'], 'Blitzschutz und Erdung')
        self.assertEqual(r['size'], 'small')
        self.assertIn('state', r['edge'])

    def test_a_deadline_inside_the_window_is_not_worth_writing_for(self):
        """A reply comes in two or three days; a lot that closes in five
        cannot be delivered by the second message."""
        predict(self.dir, [
            ('p11', 'Blitzschutzanlage Turnhalle', 'Stadt Süd',
             '2026-08-23', True)])
        self.assertEqual(sales.due(self.dir, TODAY), [])

    def test_a_lot_outside_the_main_trade_does_not_count(self):
        """The Elektro-firm-gets-Blitzschutz complaint, mirrored: a lot in
        another trade may score well and still says nothing to this reader."""
        predict(self.dir, [
            ('p12', 'Malerarbeiten Grundschule', 'Stadt West',
             '2026-09-30', True)])
        self.assertEqual(sales.due(self.dir, TODAY), [])

    def test_an_unflagged_lot_does_not_count(self):
        predict(self.dir, [
            ('p13', 'Blitzschutzanlage Rathaus II', 'Stadt Ost',
             '2026-09-30', False)])
        self.assertEqual(sales.due(self.dir, TODAY), [])

    def test_a_firm_written_to_recently_rests(self):
        predict(self.dir, [
            ('p14', 'Blitzschutzanlage Feuerwache', 'Stadt Nord',
             '2026-09-30', True)])
        self.assertEqual(len(sales.due(self.dir, TODAY)), 1)
        ledger.append(self.dir, 'app_events', [{
            'ts': '2026-08-11T09:00:00+00:00', 'kind': 'invite_sent',
            'sub_id': self.sub_id, 'detail': 'channel=linkedin'}])
        self.assertEqual(sales.due(self.dir, TODAY), [])
        # ... and is due again once the rest window has passed
        self.assertEqual(len(sales.due(self.dir, '2026-09-02')), 1)

    def test_only_watched_prospects_appear(self):
        predict(self.dir, [
            ('p15', 'Blitzschutzanlage Feuerwache', 'Stadt Nord',
             '2026-09-30', True)])
        # not on anybody's list -> not due, however good the lot is
        subscriptions.customer_update(self.dir, self.sub_id, owner=None)
        self.assertEqual(sales.due(self.dir, TODAY), [])
        subscriptions.customer_update(self.dir, self.sub_id, owner=OWNER)
        self.assertEqual(len(sales.due(self.dir, TODAY)), 1)
        # another salesperson's list is not mine
        self.assertEqual(sales.due(self.dir, TODAY, owner='other@x.de'), [])
        # a signed-up customer is served by the report mail, not by sales
        subscriptions.customer_update(self.dir, self.sub_id,
                                      consent_at=f'{TODAY}T10:00:00+00:00')
        self.assertEqual(sales.due(self.dir, TODAY), [])

    def test_a_stopped_firm_is_never_due(self):
        predict(self.dir, [
            ('p16', 'Blitzschutzanlage Feuerwache', 'Stadt Nord',
             '2026-09-30', True)])
        subscriptions.customer_update(self.dir, self.sub_id,
                                      contact_state='hard_stopped')
        self.assertEqual(sales.due(self.dir, TODAY), [])

    def test_the_strongest_signal_is_first(self):
        predict(self.dir, [
            ('p17', 'Blitzschutzanlage Feuerwache', 'Stadt Nord',
             '2026-09-30', True),
            ('p18', 'Blitzschutz Erdungsanlage Klinik', 'Kreis Mitte',
             '2026-09-15', True)])
        rows = sales.due(self.dir, TODAY)
        self.assertEqual(rows[0]['n_lots'], 2)
        self.assertEqual(rows[0]['next_deadline'], '2026-09-15')


class Mail(Due):
    """doc/SALES.md 5: one mail per owner, only when their list is
    non-empty — the second kind of e-mail, the one that goes to us."""

    def send(self, today=TODAY):
        sent = []
        rows = sales.run(self.dir, today,
                         transport=lambda p: sent.append(p) or 'm1')
        return sent, rows

    def test_a_due_firm_produces_one_mail_to_its_owner(self):
        predict(self.dir, [
            ('p20', 'Blitzschutzanlage Feuerwache', 'Stadt Nord',
             '2026-09-30', True)])
        sent, rows = self.send()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['to'], [OWNER])
        self.assertEqual(sent[0]['subject'], 'Heute schreiben: 1 Firma')
        body = sent[0]['html']
        self.assertIn(DUNKEL, body)
        self.assertIn('Blitzschutz und Erdung', body)
        self.assertIn('1 Los mit wenigen Bietern erwartet', body)
        self.assertIn('Frist 30.09.', body)
        self.assertIn(f'/admin/message?sub_id={self.sub_id}', body)
        self.assertEqual(len(rows), 1)
        evs = [e for e in ledger.read(self.dir, 'app_events')
               if e['kind'] == 'sales_mail']
        self.assertEqual(len(evs), 1)
        self.assertIn(self.sub_id, evs[0]['detail'])

    def test_nobody_due_is_no_mail_at_all(self):
        sent, rows = self.send()
        self.assertEqual((sent, rows), ([], []))
        self.assertEqual([e for e in ledger.read(self.dir, 'app_events')
                          if e['kind'] == 'sales_mail'], [])

    def test_the_footer_counts_the_watched_firms_that_are_not_due(self):
        """A short list must not read as a broken list."""
        invite.add(self.dir, 'Beispiel Bau GmbH', mint=False, owner=OWNER)
        predict(self.dir, [
            ('p21', 'Blitzschutzanlage Feuerwache', 'Stadt Nord',
             '2026-09-30', True)])
        sent, _ = self.send()
        self.assertIn('1 weitere vorgemerkte Firmen haben diese Woche nichts',
                      sent[0]['html'])

    def test_a_broken_mail_never_costs_the_cycle(self):
        predict(self.dir, [
            ('p22', 'Blitzschutzanlage Feuerwache', 'Stadt Nord',
             '2026-09-30', True)])

        def boom(_p):
            raise RuntimeError('resend down')

        rows = sales.run(self.dir, TODAY, transport=boom)   # must not raise
        self.assertEqual(len(rows), 1)

    def test_the_page_shows_the_same_list(self):
        from tests.test_admin import request
        predict(self.dir, [
            ('p23', 'Blitzschutzanlage Feuerwache', 'Stadt Nord',
             '2026-09-30', True)])
        _, _, body = request(self.dir, '/admin')
        self.assertIn('Heute schreiben', body)
        self.assertIn(DUNKEL, body)
        self.assertIn(f'/admin/message?sub_id={self.sub_id}', body)

    def test_each_salesperson_sees_and_is_mailed_their_own_list(self):
        from tests.test_admin import request
        predict(self.dir, [
            ('p24', 'Blitzschutzanlage Feuerwache', 'Stadt Nord',
             '2026-09-30', True)])
        predict(self.dir, [
            ('p25', 'Straßenbau Ortsumgehung', 'Kreis West',
             '2026-09-30', True)], nuts='DE212')   # Bayern: Beispiel Bau's
        # a second list: Beispiel Bau (Straßenbau) belongs to anna
        other, _ = invite.add(self.dir, 'Beispiel Bau GmbH', mint=False,
                              owner='anna@murara.eu')
        with mock.patch.dict(os.environ, {
                'TM_SALES_OWNERS': f'luming={OWNER},anna=anna@murara.eu'},
                clear=False):
            os.environ.pop('TM_SALES_OWNER', None)
            sent = []
            rows = sales.run(self.dir, TODAY,
                             transport=lambda p: sent.append(p) or 'm1')
            self.assertEqual(sorted(r['owner'] for r in rows),
                             sorted([OWNER, 'anna@murara.eu']))
            self.assertEqual(sorted(m['to'][0] for m in sent),
                             sorted([OWNER, 'anna@murara.eu']))
            mine = next(m for m in sent if m['to'] == [OWNER])
            self.assertIn(DUNKEL, mine['html'])
            self.assertNotIn('Beispiel Bau', mine['html'])
            # the page: luming sees luming's, with a pointer to the rest
            _, _, body = request(self.dir, '/admin', user='luming')
            self.assertIn(f'Liste von {OWNER}', body)
            self.assertIn(DUNKEL, body)
            self.assertNotIn('Beispiel Bau GmbH</b>', body)
            self.assertIn('1 weitere bei anderen anzeigen', body)
            _, _, body = request(self.dir, '/admin', query='alle=1',
                                 user='luming')
            self.assertIn('alle Listen', body)
            self.assertIn('Beispiel Bau GmbH</b>', body)


class TheTwoMessages(Due):
    """doc/SALES.md 6: the note promises exactly the lots the message
    delivers, and both come from the trigger's candidates. No candidates,
    no note — that is the whole point of the document."""

    def test_the_note_leads_with_the_pain_and_withholds_the_lot(self):
        import pitch
        predict(self.dir, [
            ('p40', 'Blitzschutzanlage Feuerwache', 'Stadt Nord',
             '2026-09-30', True),
            ('p41', 'Blitzschutz Erdungsanlage Klinik', 'Kreis Mitte',
             '2026-09-15', True)])
        m = pitch.message(self.dir, self.sub_id, 'https://a/t/x', today=TODAY)
        # variant B (operator, 2026-08-20): the pain first, then the one lot
        # almost nobody bids on — and NOT the title, buyer or link, so that
        # accepting the request has a value. Accepting is the answer. One
        # lot is promise enough; the second is the surprise in the message.
        self.assertTrue(m['short'].startswith(
            'Guten Tag, die meisten Angebote auf Ausschreibungen sind '
            'umsonst kalkuliert – zu viele Bieter. '), m['short'])
        self.assertIn('In Hessen ist gerade so eines offen, Frist 15.09., '
                      'passend zu Ihren bisherigen Aufträgen.', m['short'])
        self.assertTrue(m['short'].endswith(
            'Nehmen Sie die Anfrage an, schicken wir die Bekanntmachung.'),
            m['short'])
        self.assertNotIn('Kreis Mitte', m['short'])          # the buyer stays back
        self.assertNotIn('Blitzschutz Erdungsanlage Klinikum', m['short'])
        self.assertLessEqual(len(m['short']), pitch.SHORT_LIMIT)
        self.assertNotIn('…', m['short'])
        # and the long message delivers those same two lots
        self.assertEqual([p['procedure_id'] for p in m['picks']],
                         ['p41', 'p40'])
        self.assertIn('Blitzschutz Erdungsanlage Klinik', m['long'])

    def test_without_a_candidate_there_is_no_note_and_the_page_says_wait(self):
        import pitch
        m = pitch.message(self.dir, self.sub_id, 'https://a/t/x', today=TODAY)
        self.assertEqual(m['short'], '')
        self.assertEqual(m['picks'], [])
        from tests.test_admin import request
        _, _, body = request(self.dir, '/admin/message',
                             query=f'sub_id={self.sub_id}')
        self.assertIn('Nichts Passendes offen — noch nicht schicken', body)
        self.assertIn('nichts zu versprechen', body)

    def test_a_lot_in_another_trade_never_reaches_the_texts(self):
        """The complaint that started this: an Elektro firm offered a
        Blitzschutz lot. Mirrored — this firm is Blitzschutz, and a Maler
        lot must not appear however well it scores."""
        import pitch
        predict(self.dir, [
            ('p42', 'Malerarbeiten Grundschule', 'Stadt West',
             '2026-09-30', True)])
        m = pitch.message(self.dir, self.sub_id, 'https://a/t/x', today=TODAY)
        self.assertEqual(m['short'], '')
        self.assertNotIn('Malerarbeiten', m['long'])


class TheTeaser(unittest.TestCase):
    """`pitch.note` and its helpers, on their own: what is shown, what is
    held back, and that no word is ever cut."""

    def test_findable_parts_come_out_of_the_title(self):
        import pitch
        w = pitch.work_of
        self.assertEqual(w('Gewerbeschulstr 109 Neubau Installation '
                           'Elektroinstallation', 'Elektroinstallation'),
                         'Neubau')
        self.assertEqual(w('Neubau Kita Sonnenweg 4, Los 3 - Elektroinstallation',
                           'Elektroinstallation'), 'Neubau Kita')
        self.assertEqual(w('Sanierung Rathaus (BV 2026-17) – Elektroarbeiten',
                           'Elektroinstallation'), 'Sanierung Rathaus')
        self.assertEqual(w('Erneuerung Beleuchtung Sporthalle Am Hang 12a'),
                         'Erneuerung Beleuchtung Sporthalle')
        # nothing left but the trade word: no work line, the note falls back
        self.assertIsNone(w('Elektroinstallation', 'Elektroinstallation'))
        self.assertIsNone(w('', 'Elektroinstallation'))

    def test_the_value_is_a_band_and_never_a_guess(self):
        import pitch
        vb = pitch.value_band
        self.assertEqual(vb({'est_value_lot': 612345}), 'rund 600.000 €')
        self.assertEqual(vb({'est_value_lot': 84000}), 'rund 80.000 €')
        self.assertEqual(vb({'est_value_lot': 2_340_000}), 'rund 2,3 Mio. €')
        self.assertEqual(vb({'est_value_lot': 1_000_000}), 'rund 1 Mio. €')
        self.assertIsNone(vb({'est_value_lot': None}))
        self.assertIsNone(vb({'est_value_lot': float('nan')}))
        self.assertIsNone(vb({}))

    def test_the_land_is_the_lots_own(self):
        import pitch
        self.assertEqual(pitch.land_of('DEA1A'), 'Nordrhein-Westfalen')
        self.assertEqual(pitch.land_of('DE712'), 'Hessen')
        self.assertIsNone(pitch.land_of(None))
        self.assertIsNone(pitch.land_of('FR101'))

    def test_the_optional_parts_give_way_before_a_word_is_cut(self):
        import pitch
        # the worst legitimate load: the longest Land name and the longer
        # deadline word — the match clause shortens, then the Land becomes
        # „Ihrer Region", and no word is ever cut
        n = pitch.note([{'title': 'x', 'place_nuts3': 'DE803',
                         'participation_deadline_date': '2026-09-07'}],
                       'Elektroinstallation')
        self.assertLessEqual(len(n), pitch.SHORT_LIMIT)
        self.assertNotIn('…', n)
        self.assertIn('Teilnahmefrist 07.09.', n)
        self.assertIn('In Ihrer Region', n)          # Mecklenburg gave way
        self.assertIn('passend zu Ihren Aufträgen', n)
        # the full version, when it fits, carries the Land and the full
        # match clause
        n = pitch.note([{'title': 'x', 'deadline_date': '2026-09-07',
                         'place_nuts3': 'DE712'}], 'Elektroinstallation')
        self.assertIn('In Hessen ist gerade so eines offen, Frist 07.09., '
                      'passend zu Ihren bisherigen Aufträgen.', n)
        self.assertTrue(n.endswith('schicken wir die Bekanntmachung.'))
        # no region known: "In Ihrer Region" — true, the lot is inside the
        # draft's nuts_prefixes, which are where the firm has won
        n = pitch.note([{'title': 'x', 'deadline_date': '2026-09-07',
                         'place_nuts3': None}], 'Elektroinstallation')
        self.assertIn('In Ihrer Region', n)


class Owners(unittest.TestCase):
    def test_the_press_is_attributed_to_the_basic_auth_user(self):
        with mock.patch.dict(os.environ,
                             {'TM_SALES_OWNERS': 'a=a@b.de,c=c@d.de'},
                             clear=True):
            self.assertEqual(sales.owner_for('a'), 'a@b.de')
            self.assertEqual(sales.owner_for('c'), 'c@d.de')
            self.assertIsNone(sales.owner_for('nobody'))   # two: no guessing
            self.assertIsNone(sales.owner_for(''))
        with mock.patch.dict(os.environ, {'TM_SALES_OWNERS': 'a=a@b.de'},
                             clear=True):
            self.assertEqual(sales.owner_for('nobody'), 'a@b.de')  # one: it
        with mock.patch.dict(os.environ, {'TM_SALES_OWNER': 'solo@x.de'},
                             clear=True):
            self.assertEqual(sales.owner_for('anyone'), 'solo@x.de')

    def test_the_address_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {'TM_SALES_OWNER': 'a@b.de'}):
            self.assertEqual(sales.default_owner(), 'a@b.de')
        with mock.patch.dict(os.environ, {'TM_SALES_OWNERS': 'luming=a@b.de'},
                             clear=True):
            self.assertEqual(sales.owners(), {'luming': 'a@b.de'})
            self.assertEqual(sales.default_owner(), 'a@b.de')
        # two configured and none named: no guessing whose list it is
        with mock.patch.dict(os.environ,
                             {'TM_SALES_OWNERS': 'a=a@b.de,c=c@d.de'},
                             clear=True):
            self.assertIsNone(sales.default_owner())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(sales.owners(), {})
            self.assertIsNone(sales.default_owner())


if __name__ == '__main__':
    unittest.main()
