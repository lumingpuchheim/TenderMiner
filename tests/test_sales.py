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


def predict(home, rows):
    """Prediction rows as the cycle writes them; `rows` is
    [(procedure_id, title, buyer, deadline, flag)].

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
        'cpv_main': '45312310', 'cpv3': '453', 'place_nuts3': 'DE712',
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


class Owners(unittest.TestCase):
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
