"""doc/PAYMENT.md — the one rule and its three doors.

A subscription exists only when Stripe confirmed a payment (signed webhook)
or the operator activated the firm from the admin page. The customer's yes
opens Checkout and changes nothing; the stop button also ends the Stripe
subscription; the admin page can un-stop (with a reason) and erase.

No network anywhere: stripe_pay calls are faked at `_call`, webhook events
are signed locally with the same HMAC Stripe uses.
"""

import hmac
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app                                                      # noqa: E402
import ledger                                                   # noqa: E402
import stripe_pay                                               # noqa: E402
import subscriptions                                            # noqa: E402
import tokens                                                   # noqa: E402

SECRET = 'whsec_test_secret'


def sign(body, secret=SECRET, ts=None):
    ts = int(ts if ts is not None else time.time())
    mac = hmac.new(secret.encode(), f'{ts}.'.encode() + body, 'sha256')
    return f't={ts},v1={mac.hexdigest()}'


def request(data_dir, path, method='GET', form=None, body=None, headers=None,
            admin=False):
    """-> (status, headers dict, body). Raw `body` bytes win over `form`."""
    from urllib.parse import urlencode
    captured = {}

    def start_response(status, hdrs):
        captured['status'] = status
        captured['headers'] = dict(hdrs)

    raw = body if body is not None else urlencode(form or {}).encode()
    environ = {'REQUEST_METHOD': method, 'PATH_INFO': path,
               'REMOTE_ADDR': '127.0.0.1', 'CONTENT_LENGTH': str(len(raw)),
               'wsgi.input': io.BytesIO(raw)}
    if admin:
        environ['HTTP_X_MURARA_ADMIN'] = '1'
    for k, v in (headers or {}).items():
        environ['HTTP_' + k.upper().replace('-', '_')] = v
    out = app.make_app(data_dir)(environ, start_response)
    return captured['status'], captured['headers'], b''.join(out).decode()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        import gc
        self.addCleanup(gc.collect)
        app._hits.clear()
        self.env = mock.patch.dict('os.environ', {
            'STRIPE_SECRET_KEY': 'sk_test_x',
            'STRIPE_WEBHOOK_SECRET': SECRET,
            'TM_STRIPE_PRICE_ID': 'price_x'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def sub(self, sub_id='beck', plan=None):
        # customer first: it creates the database, so the version below is
        # appended to the DB and not to a legacy JSONL the guard then flags
        subscriptions.customer_update(self.dir, sub_id, name='Beck GmbH',
                                      contact_email='b@beck.de')
        row = {'sub_id': sub_id, 'version': 1, 'active': True,
               'effective_from': '2026-01-01', 'cpv_prefixes': ['45']}
        if plan:
            row['plan'] = plan
        subscriptions.append_version(self.dir, row)

    def events(self, kind):
        return [e for e in ledger.read(self.dir, 'app_events')
                if e['kind'] == kind]


class Signature(unittest.TestCase):
    def test_round_trip_and_the_ways_it_fails(self):
        body = b'{"x": 1}'
        good = sign(body)
        self.assertTrue(stripe_pay.verify_signature(body, good, secret=SECRET))
        self.assertFalse(stripe_pay.verify_signature(b'{"x": 2}', good,
                                                     secret=SECRET))
        self.assertFalse(stripe_pay.verify_signature(body, good,
                                                     secret='whsec_other'))
        self.assertFalse(stripe_pay.verify_signature(body, good, secret=''))
        self.assertFalse(stripe_pay.verify_signature(body, '', secret=SECRET))
        self.assertFalse(stripe_pay.verify_signature(body, 'v1=deadbeef',
                                                     secret=SECRET))
        stale = sign(body, ts=int(time.time()) - 3600)
        self.assertFalse(stripe_pay.verify_signature(body, stale,
                                                     secret=SECRET))


class Checkout(Base):
    def test_session_carries_the_firm_and_comes_back_as_a_url(self):
        calls = []

        def transport(method, path, fields):
            calls.append((method, path, fields))
            return {'url': 'https://checkout.stripe.com/c/pay/x'}

        url = stripe_pay.checkout_url('beck', 'b@beck.de',
                                      success_url='https://a/danke',
                                      cancel_url='https://a/y/t',
                                      transport=transport)
        self.assertEqual(url, 'https://checkout.stripe.com/c/pay/x')
        method, path, fields = calls[0]
        self.assertEqual((method, path), ('POST', '/v1/checkout/sessions'))
        self.assertEqual(fields['client_reference_id'], 'beck')
        self.assertEqual(fields['subscription_data[metadata][sub_id]'], 'beck')
        self.assertEqual(fields['line_items[0][price]'], 'price_x')
        self.assertEqual(fields['mode'], 'subscription')

    def test_yes_click_redirects_and_flips_no_plan(self):
        self.sub()
        value = tokens.mint(self.dir, 'y', 'beck')
        with mock.patch.object(stripe_pay, '_call',
                               return_value={'url': 'https://stripe/x'}):
            status, headers, _ = request(self.dir, f'/y/{value}',
                                         method='POST')
        self.assertEqual(status, '303 See Other')
        self.assertEqual(headers['Location'], 'https://stripe/x')
        today = '2026-12-31'
        sub = subscriptions.one(self.dir, today, 'beck')
        self.assertNotEqual(sub.get('plan'), 'paid')
        self.assertEqual(len(self.events('subscribe_yes')), 1)
        self.assertEqual(self.events('paid_started'), [])

    def test_yes_without_stripe_keys_promises_a_person_not_a_plan(self):
        self.sub()
        value = tokens.mint(self.dir, 'y', 'beck')
        with mock.patch.dict('os.environ', {'STRIPE_SECRET_KEY': '',
                                            'TM_STRIPE_PRICE_ID': ''}):
            status, _, body = request(self.dir, f'/y/{value}', method='POST')
        self.assertEqual(status, '200 OK')
        self.assertIn('Wir melden uns', body)
        sub = subscriptions.one(self.dir, '2026-12-31', 'beck')
        self.assertNotEqual(sub.get('plan'), 'paid')


class Webhook(Base):
    def hook(self, event, secret=SECRET, ts=None):
        raw = json.dumps(event).encode()
        return request(self.dir, '/stripe/webhook', method='POST', body=raw,
                       headers={'Stripe-Signature': sign(raw, secret, ts=ts)})

    def test_paid_checkout_makes_the_customer(self):
        self.sub()
        status, _, _ = self.hook({
            'type': 'checkout.session.completed',
            'data': {'object': {'client_reference_id': 'beck',
                                'customer': 'cus_1', 'subscription': 'sub_1'}}})
        self.assertEqual(status, '200 OK')
        cust = subscriptions.customer_get(self.dir, 'beck')
        self.assertEqual(cust['stripe_subscription_id'], 'sub_1')
        sub = subscriptions.one(self.dir, '2026-12-31', 'beck')
        self.assertEqual(sub.get('plan'), 'paid')
        ev = self.events('paid_started')
        self.assertEqual(len(ev), 1)
        self.assertIn('stripe: sub_1', ev[0]['detail'])
        # idempotent: Stripe retries deliveries
        self.hook({'type': 'checkout.session.completed',
                   'data': {'object': {'client_reference_id': 'beck',
                                       'customer': 'cus_1',
                                       'subscription': 'sub_1'}}})
        self.assertEqual(len(self.events('paid_started')), 1)

    def test_bad_signature_is_refused_and_changes_nothing(self):
        self.sub()
        status, _, _ = self.hook({
            'type': 'checkout.session.completed',
            'data': {'object': {'client_reference_id': 'beck',
                                'subscription': 'sub_1'}}},
            secret='whsec_wrong')
        self.assertEqual(status, '400 Bad Request')
        self.assertEqual(self.events('paid_started'), [])

    def test_a_subscription_ended_at_stripe_alerts_and_touches_nothing(self):
        self.sub(plan='paid')
        subscriptions.customer_update(self.dir, 'beck',
                                      stripe_subscription_id='sub_1')
        status, _, _ = self.hook({'type': 'customer.subscription.deleted',
                                  'data': {'object': {'id': 'sub_1'}}})
        self.assertEqual(status, '200 OK')
        self.assertEqual(len(self.events('stripe_sub_ended')), 1)
        # the plan did NOT change: only the operator decides what follows
        sub = subscriptions.one(self.dir, '2026-12-31', 'beck')
        self.assertEqual(sub.get('plan'), 'paid')


class Stop(Base):
    def test_stop_also_cancels_the_stripe_subscription(self):
        self.sub(plan='paid')
        subscriptions.customer_update(self.dir, 'beck',
                                      stripe_subscription_id='sub_1')
        value = tokens.mint(self.dir, 's', 'beck')
        with mock.patch.object(stripe_pay, 'cancel') as cancel:
            status, _, _ = request(self.dir, f'/s/{value}', method='POST')
        self.assertEqual(status, '200 OK')
        cancel.assert_called_once_with('sub_1')
        self.assertEqual(len(self.events('stripe_cancelled')), 1)

    def test_stripe_down_never_blocks_the_stop(self):
        self.sub(plan='paid')
        subscriptions.customer_update(self.dir, 'beck',
                                      stripe_subscription_id='sub_1')
        value = tokens.mint(self.dir, 's', 'beck')
        with mock.patch.object(stripe_pay, 'cancel',
                               side_effect=stripe_pay.StripeError('down')):
            status, _, body = request(self.dir, f'/s/{value}', method='POST')
        self.assertEqual(status, '200 OK')
        self.assertIn('Abbestellt', body)
        cust = subscriptions.customer_get(self.dir, 'beck')
        self.assertEqual(cust['contact_state'], 'hard_stopped')
        self.assertEqual(len(self.events('stripe_cancel_failed')), 1)


class AdminDoors(Base):
    def test_unstop_needs_a_reason_and_reactivates(self):
        self.sub()
        app.stop_customer(self.dir, 'beck')
        status, _, body = request(self.dir, '/admin/unstop', method='POST',
                                  form={'sub_id': 'beck'}, admin=True)
        self.assertIn('Begründung fehlt', body)
        self.assertEqual(subscriptions.customer_get(
            self.dir, 'beck')['contact_state'], 'hard_stopped')
        status, _, _ = request(self.dir, '/admin/unstop', method='POST',
                               form={'sub_id': 'beck',
                                     'note': 'Test, 20.08.'}, admin=True)
        self.assertEqual(subscriptions.customer_get(
            self.dir, 'beck')['contact_state'], 'active')
        ev = self.events('unstop')
        self.assertEqual(len(ev), 1)
        self.assertIn('Test, 20.08.', ev[0]['detail'])

    def test_backdoor_activation_is_paid_and_says_who(self):
        self.sub()
        request(self.dir, '/admin/activate', method='POST',
                form={'sub_id': 'beck'}, admin=True)
        sub = subscriptions.one(self.dir, '2026-12-31', 'beck')
        self.assertEqual(sub.get('plan'), 'paid')
        ev = self.events('paid_started')
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]['detail'], 'admin (backdoor)')

    def test_deleting_a_twin_is_restlos_by_identity(self):
        """A test- sub_id gets the total forget WITHOUT any button choice —
        restlos is decided by what the row is, not by what was clicked."""
        self.sub(sub_id='test-beck')
        tokens.mint(self.dir, 'y', 'test-beck')
        request(self.dir, '/admin/delete', method='POST',
                form={'sub_id': 'test-beck'}, admin=True)
        self.assertIsNone(subscriptions.customer_get(self.dir, 'test-beck'))
        self.assertEqual([r for r in subscriptions.read_all(self.dir)
                          if r['sub_id'] == 'test-beck'], [])
        self.assertIsNone(tokens.live_value(self.dir, 'y', 'test-beck'))
        self.assertEqual([e for e in ledger.read(self.dir, 'app_events')
                          if e['sub_id'] == 'test-beck'], [])

    def test_the_default_delete_keeps_a_suppression_entry(self):
        """doc/PAYMENT.md 3a: 'löscht alles und schreibt uns nie wieder' —
        every personal datum goes, the name stays as the block, and a click
        that chose nothing gets THIS, never the total forget."""
        self.sub()
        tokens.mint(self.dir, 'y', 'beck')
        request(self.dir, '/admin/delete', method='POST',
                form={'sub_id': 'beck'}, admin=True)
        cust = subscriptions.customer_get(self.dir, 'beck')
        self.assertEqual(cust['contact_state'], 'hard_stopped')
        self.assertEqual(cust['name'], 'Beck GmbH')
        self.assertIsNone(cust['contact_email'])          # the datum is gone
        self.assertEqual([r for r in subscriptions.read_all(self.dir)
                          if r['sub_id'] == 'beck'], [])
        self.assertIsNone(tokens.live_value(self.dir, 'y', 'beck'))
        kinds = [e['kind'] for e in ledger.read(self.dir, 'app_events')
                 if e['sub_id'] == 'beck']
        self.assertEqual(kinds, ['objection'])            # history is gone
        # and the mailer cannot write to the shell that remains
        import mailer
        with self.assertRaises(mailer.MailerError):
            mailer.send(self.dir, 'report', 'beck', 'x', 'x',
                        transport=lambda p: 'id')

    def test_delete_refuses_while_a_stripe_subscription_lives(self):
        self.sub(plan='paid')
        subscriptions.customer_update(self.dir, 'beck',
                                      stripe_subscription_id='sub_1')
        _, _, body = request(self.dir, '/admin/delete', method='POST',
                             form={'sub_id': 'beck'}, admin=True)
        self.assertIn('nicht gelöscht', body)
        self.assertIsNotNone(subscriptions.customer_get(self.dir, 'beck'))

    def test_erase_refuses_a_firm_the_frozen_files_know(self):
        self.sub()
        legacy = ledger.file_path(self.dir, 'subscriptions')
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({'sub_id': 'beck', 'version': 1}) + '\n',
                          encoding='utf-8')
        with self.assertRaises(subscriptions.SubscriptionError):
            subscriptions.erase(self.dir, 'beck')


if __name__ == '__main__':
    unittest.main()
