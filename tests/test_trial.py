"""The trial clock, the ask, the yes-link — doc/ONBOARDING.md 9.5,
LAUNCH.md 3. Own temp directory, fake transport, no network."""
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import delivering                                              # noqa: E402
import ledger                                                  # noqa: E402
import subscriptions                                           # noqa: E402
import tokens                                                  # noqa: E402
from tests.test_app import request                             # noqa: E402

V1 = {'sub_id': 'firm', 'version': 1, 'active': False, 'name': 'Firm GmbH',
      'cpv_prefixes': ['45'], 'profile_refs': ['00000001-2026']}
V2 = {**V1, 'version': 2, 'active': True, 'effective_from': '2026-08-01'}


class Status(unittest.TestCase):
    def test_never_active(self):
        s = subscriptions.trial_status([V1], '2026-08-17')
        self.assertEqual((s['plan'], s['started'], s['ask_due']),
                         ('trial', None, False))

    def test_trial_days_and_ask(self):
        s = subscriptions.trial_status([V1, V2], '2026-08-17')
        self.assertEqual((s['started'], s['ends'], s['day'], s['ask_due']),
                         ('2026-08-01', '2026-08-29', 17, False))
        s = subscriptions.trial_status([V1, V2], '2026-08-29')
        self.assertEqual((s['day'], s['ask_due']), (29, True))
        s = subscriptions.trial_status([V1, V2], '2026-09-14')
        self.assertTrue(s['ask_due'])

    def test_paid_stops_the_clock(self):
        v3 = {**V2, 'version': 3, 'effective_from': '2026-09-01', 'plan': 'paid'}
        s = subscriptions.trial_status([V1, V2, v3], '2026-09-14')
        self.assertEqual((s['plan'], s['ask_due']), ('paid', False))
        # before v3 speaks, still trial
        s = subscriptions.trial_status([V1, V2, v3], '2026-08-30')
        self.assertEqual((s['plan'], s['ask_due']), ('trial', True))

    def test_plan_is_validated(self):
        with self.assertRaises(subscriptions.SubscriptionError):
            subscriptions.validate({**V2, 'plan': 'gold'})
        self.assertEqual(subscriptions.validate({**V2, 'plan': 'paid'})['plan'],
                         'paid')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        import gc
        self.addCleanup(gc.collect)
        subscriptions.customer_update(self.dir, 'firm', name='Firm GmbH',
                                      contact_email='f@firm.de')
        subscriptions.append_version(self.dir, V1)
        subscriptions.append_version(self.dir, V2)

    def events(self, kind):
        return [r for r in ledger.read(self.dir, 'app_events')
                if r['kind'] == kind]


class Ask(Base):
    def test_ask_block_only_when_due_and_only_once(self):
        allv = subscriptions.read_all(self.dir)
        st, asked = delivering.trial_state(self.dir, 'firm',
                                           date(2026, 8, 17), allv)
        self.assertFalse(st['ask_due'])
        self.assertFalse(asked)
        st, asked = delivering.trial_state(self.dir, 'firm',
                                           date(2026, 9, 7), allv)
        self.assertTrue(st['ask_due'])
        html = delivering.ask_for(self.dir, 'firm')
        self.assertIn('Ja, weiter mit Murara', html)
        self.assertIn('https://app.murara.eu/y/', html)
        # the ask goes out -> event; from then on `asked` is True
        ledger.append(self.dir, 'app_events', [{
            'ts': '2026-09-07T08:00:00+00:00', 'kind': 'ask',
            'sub_id': 'firm', 'detail': 'test'}])
        _, asked = delivering.trial_state(self.dir, 'firm',
                                          date(2026, 9, 14), allv)
        self.assertTrue(asked)
        # the y link is standing: same token next time
        self.assertEqual(html, delivering.ask_for(self.dir, 'firm'))


class Yes(Base):
    def _y(self):
        return '/y/' + tokens.standing(self.dir, 'y', 'firm')

    def test_get_shows_the_firm_and_no_price_until_set(self):
        status, _, body = request(self.dir, self._y())
        self.assertEqual(status, '200 OK')
        self.assertIn('Firm GmbH', body)
        self.assertIn('keine Zahlungspflicht', body)
        with mock.patch.dict(os.environ, {'TM_PRICE_LINE': '179 € im Monat'}):
            _, _, body = request(self.dir, self._y())
        self.assertIn('179 € im Monat', body)

    def test_post_records_yes_and_writes_a_paid_version(self):
        sent = []
        import mailer
        with mock.patch.object(mailer, '_resend',
                               lambda payload: sent.append(payload) or 'op-1'):
            status, _, body = request(self.dir, self._y(), 'POST')
        self.assertEqual(status, '200 OK')
        self.assertIn('wir melden uns', body.lower())
        self.assertEqual(len(self.events('subscribe_yes')), 1)
        today = date.today().isoformat()
        sub = subscriptions.one(self.dir, today, 'firm')
        self.assertEqual((sub['plan'], sub['version'], sub['active']),
                         ('paid', 3, True))
        # the operator was told, because no Stripe link exists
        self.assertEqual(len(sent), 1)
        self.assertIn('Firm GmbH', sent[0]['subject'])
        # a second yes changes nothing
        request(self.dir, self._y(), 'POST')
        self.assertEqual(len(self.events('subscribe_yes')), 1)
        self.assertEqual(subscriptions.one(self.dir, today, 'firm')['version'], 3)
        _, _, body = request(self.dir, self._y())
        self.assertIn('Sie sind dabei', body)
        # the clock is off
        s = subscriptions.trial_status(subscriptions.read_all(self.dir), today)
        self.assertEqual((s['plan'], s['ask_due']), ('paid', False))

    def test_post_with_stripe_link_shows_the_payment_button(self):
        with mock.patch.dict(os.environ, {'TM_STRIPE_URL': 'https://pay.example/x'}):
            _, _, body = request(self.dir, self._y(), 'POST')
        self.assertIn('https://pay.example/x', body)
        self.assertIn('Zur Zahlung', body)

    def test_y_token_is_only_a_y_token(self):
        y = self._y().rsplit('/', 1)[1]
        for prefix in ('t', 'f', 's', 'c'):
            _, _, body = request(self.dir, f'/{prefix}/{y}')
            self.assertIn('nicht mehr gültig', body)


if __name__ == '__main__':
    unittest.main()
