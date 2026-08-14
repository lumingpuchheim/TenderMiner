"""The app's routing and the token layer — doc/APP.md 2 and 3.

Own file, like tests/test_housekeeping.py: importing `app` costs only stdlib,
and keeping it out of test_storage.py leaves that suite free of anything that
opens a socket. Nothing here binds a port — the WSGI callable is called
directly, which is what a real request reduces to anyway.

No real data: every test builds its own temporary directory.
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app                                                      # noqa: E402
import tokens                                                   # noqa: E402


def request(data_dir, path, method='GET', form=None, ip='127.0.0.1'):
    """-> (status, headers dict, body). A request without a server."""
    import io
    from urllib.parse import urlencode
    captured = {}

    def start_response(status, headers):
        captured['status'] = status
        captured['headers'] = dict(headers)

    body_in = urlencode(form or {}).encode('utf-8')
    environ = {'REQUEST_METHOD': method, 'PATH_INFO': path,
               'REMOTE_ADDR': ip, 'CONTENT_LENGTH': str(len(body_in)),
               'wsgi.input': io.BytesIO(body_in)}
    body = app.make_app(data_dir)(environ, start_response)
    return captured['status'], captured['headers'], b''.join(body).decode('utf-8')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        # LIFO: collect BEFORE the tmp dir is removed — Windows refuses to
        # delete a database file some unclosed connection still holds.
        import gc
        self.addCleanup(gc.collect)
        app._hits.clear()          # the rate brake is per-process state


class PublicPages(Base):
    def test_root_is_html_and_says_what_this_is(self):
        status, headers, body = request(self.dir, '/')
        self.assertEqual(status, '200 OK')
        self.assertTrue(headers['Content-Type'].startswith('text/html'))
        self.assertIn('<!doctype html>', body)
        # Murara is the customer-facing brand; TenderMining is the repository
        self.assertIn('Murara', body)
        self.assertNotIn('TenderMining', body)

    def test_legal_pages_render(self):
        for path in ('/impressum', '/datenschutz'):
            status, _, body = request(self.dir, path)
            self.assertEqual(status, '200 OK', path)
            self.assertIn('<h1>', body)

    def test_every_response_is_noindex(self):
        """The URLs carry capability tokens. A search engine that indexed one
        would publish it, so this header is load-bearing on EVERY route
        including 404s — not only on the token pages."""
        for path in ('/', '/impressum', '/healthz', '/robots.txt', '/nope',
                     '/t/whatever'):
            _, headers, _ = request(self.dir, path)
            self.assertIn('noindex', headers['X-Robots-Tag'], path)

    def test_referrer_policy_stops_tokens_leaking(self):
        _, headers, _ = request(self.dir, '/')
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')

    def test_robots_txt_disallows_everything(self):
        _, headers, body = request(self.dir, '/robots.txt')
        self.assertTrue(headers['Content-Type'].startswith('text/plain'))
        self.assertIn('Disallow: /', body)

    # /healthz carries the health *semantics*, not just the numbers: the same
    # 200/503 is read by the external pinger and by the deploy gate
    # (doc/OPERATIONS.md 1 and 2). These tests are what stop the two drifting.

    def _checkpoint(self, days_ago):
        """A loop checkpoint whose last success was `days_ago` days ago."""
        logs = self.dir / 'logs'
        logs.mkdir(exist_ok=True)
        stamp = (datetime.now(timezone.utc)
                 - timedelta(days=days_ago)).strftime('%Y%m%d')
        (logs / 'loop_checkpoint.json').write_text(
            json.dumps({'last_success_to': stamp}), encoding='utf-8')
        return stamp

    def _disk(self, free_bytes):
        """Pin the free-space reading. The real one is the test machine's, and
        a health check that goes green or red with whoever's laptop is running
        the suite is not a test."""
        patch = mock.patch.object(app, '_free_bytes', lambda _d: free_bytes)
        patch.start()
        self.addCleanup(patch.stop)

    def test_healthz_is_green_when_the_cycle_is_recent_and_the_disk_has_room(self):
        stamp = self._checkpoint(days_ago=2)
        self._disk(50 * 1024**3)
        status, _, body = request(self.dir, '/healthz')
        self.assertEqual(status, '200 OK')
        self.assertIn(f'cycle_last_success={stamp}', body)
        self.assertIn('cycle_age_days=2', body)

    def test_healthz_tolerates_exactly_one_late_monday(self):
        """8 days is the boundary and it is deliberate: a cycle that slipped a
        day must not page the operator, a cycle that missed a week must."""
        self._checkpoint(days_ago=8)
        self._disk(50 * 1024**3)
        status, _, _ = request(self.dir, '/healthz')
        self.assertEqual(status, '200 OK')

        self._checkpoint(days_ago=9)
        status, _, _ = request(self.dir, '/healthz')
        self.assertEqual(status, '503 Service Unavailable')

    def test_healthz_is_red_without_a_cycle_but_still_answers(self):
        """A fresh deployment has no checkpoint. Unknown reads as red on
        purpose (OPERATIONS.md 1) — but the endpoint must still answer, since
        one that raises when the data is missing is worse than one that admits
        it does not know."""
        self._disk(50 * 1024**3)
        status, _, body = request(self.dir, '/healthz')
        self.assertEqual(status, '503 Service Unavailable')
        self.assertIn('cycle_last_success=unknown', body)
        self.assertIn('cycle_age_days=unknown', body)

    def test_healthz_is_red_when_the_disk_is_nearly_full(self):
        self._checkpoint(days_ago=1)
        self._disk(1 * 1024**3)                      # under the 2 GB floor
        status, _, body = request(self.dir, '/healthz')
        self.assertEqual(status, '503 Service Unavailable')
        self.assertIn('disk_free_mb=1024', body)

    def test_healthz_is_red_when_the_state_directory_cannot_be_read(self):
        """What a missing /data mount looks like from inside the container —
        the failure the deploy gate exists to catch."""
        self._checkpoint(days_ago=1)
        self._disk(None)
        status, _, body = request(self.dir, '/healthz')
        self.assertEqual(status, '503 Service Unavailable')
        self.assertIn('disk_free_mb=unknown', body)

    def test_unknown_path_is_404(self):
        status, _, _ = request(self.dir, '/admin')
        self.assertEqual(status, '404 Not Found')


class TokenRoutes(Base):
    def test_a_valid_token_renders_its_own_page(self):
        value = tokens.mint(self.dir, 't', 'mueller-elektro')
        status, _, body = request(self.dir, f'/t/{value}')
        self.assertEqual(status, '200 OK')
        self.assertIn('mueller-elektro', body)

    def test_a_feedback_token_shows_its_lot_and_verdict(self):
        value = tokens.mint(self.dir, 'f', 'beck', procedure_id='p1',
                            lot_id='LOT-0001', verdict='passend')
        _, _, body = request(self.dir, f'/f/{value}')
        self.assertIn('LOT-0001', body)
        self.assertIn('passend', body)

    def test_unknown_and_revoked_are_the_same_page(self):
        """doc/APP.md 2: no oracle. If a revoked token looked different from
        one that never existed, the app would confirm which customers exist to
        anyone holding an expired link."""
        value = tokens.mint(self.dir, 's', 'beck')
        tokens.revoke(self.dir, value)
        _, _, revoked = request(self.dir, f'/s/{value}')
        _, _, never = request(self.dir, '/s/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')
        self.assertEqual(revoked, never)
        self.assertIn('nicht mehr gültig', revoked)

    def test_a_feedback_token_is_not_a_stop_token(self):
        """The reason purposes exist. A feedback link is printed next to every
        lot in every report and travels through scanners and forwarded mail; if
        it also worked as /s/ it would be an unsubscribe button anyone could
        press on the customer's behalf."""
        value = tokens.mint(self.dir, 'f', 'beck', procedure_id='p1',
                            lot_id='L1', verdict='passend')
        _, _, body = request(self.dir, f'/s/{value}')
        self.assertIn('nicht mehr gültig', body)

    def test_get_never_mutates(self):
        """The rule the whole design leans on. Mail scanners and prefetchers
        fetch every URL in a message before a human sees it."""
        value = tokens.mint(self.dir, 's', 'beck')
        request(self.dir, f'/s/{value}')
        row = tokens.resolve(self.dir, 's', value)
        self.assertIsNone(row['used_at'])
        self.assertIsNone(row['revoked_at'])

    def test_rate_limit_brakes_enumeration(self):
        """APP.md 3: lookups per IP are capped. The 31st probe in a minute
        gets a 429; a different IP is unaffected."""
        for _ in range(app.RATE_MAX):
            request(self.dir, '/t/probe', ip='10.0.0.9')
        status, _, _ = request(self.dir, '/t/probe', ip='10.0.0.9')
        self.assertEqual(status, '429 Too Many Requests')
        status, _, _ = request(self.dir, '/t/probe', ip='10.0.0.10')
        self.assertEqual(status, '200 OK')

    def test_access_log_scrubbing_truncates_tokens(self):
        """APP.md 3: first 8 characters only, in EVERY log line. The regex is
        what stands between a capability URL and `docker logs`."""
        line = 'GET /t/AIZqvIs_oJfgmKj57DsBZRomDCYLXff0 HTTP/1.1'
        out = app._TOKEN_IN_PATH.sub(r'\1…', line)
        self.assertEqual(out, 'GET /t/AIZqvIs_… HTTP/1.1')
        self.assertNotIn('oJfgmKj57DsBZRomDCYLXff0', out)


class Handlers(Base):
    """The POST side — APP.md 4-6. No store parquet in the tmp dir, so the
    pre-flight lands every signup in `held`, which is its conservative
    default and exactly what these tests assert."""

    def _events(self, kind=None):
        import ledger
        rows = ledger.read(self.dir, 'app_events')
        return [r for r in rows if kind is None or r['kind'] == kind]

    def test_signup_records_consent_and_holds_without_a_draft(self):
        value = tokens.mint(self.dir, 't', 'mueller')
        status, _, body = request(self.dir, f'/t/{value}', method='POST',
                                  form={'email': 'chef@mueller.de'})
        self.assertEqual(status, '200 OK')
        self.assertIn('richten Ihr Profil ein', body)
        cust = subs_mod().customer_get(self.dir, 'mueller')
        self.assertEqual(cust['contact_email'], 'chef@mueller.de')
        self.assertIsNotNone(cust['consent_at'])
        self.assertEqual(cust['contact_state'], 'active')
        held = self._events('signup_held')
        self.assertEqual(len(held), 1)
        self.assertIn('no draft subscription', held[0]['detail'])

    def test_signup_rejects_a_broken_address_and_keeps_the_token(self):
        value = tokens.mint(self.dir, 't', 'mueller')
        _, _, body = request(self.dir, f'/t/{value}', method='POST',
                             form={'email': 'not-an-address'})
        self.assertIn('unvollständig', body)
        self.assertIsNone(tokens.resolve(self.dir, 't', value)['used_at'])

    def test_second_signup_shows_masked_email_and_overwrites_nothing(self):
        """APP.md 4: duplicate submit -> already-registered page, stored
        address masked, no silent overwrite."""
        value = tokens.mint(self.dir, 't', 'mueller')
        request(self.dir, f'/t/{value}', method='POST',
                form={'email': 'chef@mueller.de'})
        _, _, body = request(self.dir, f'/t/{value}', method='POST',
                             form={'email': 'other@else.de'})
        self.assertIn('c…@mueller.de', body)
        cust = subs_mod().customer_get(self.dir, 'mueller')
        self.assertEqual(cust['contact_email'], 'chef@mueller.de')

    def test_stop_soft_then_hard(self):
        """LAUNCH.md 3: two buttons, two states, one event each — and the
        soft page carries the hard button."""
        value = tokens.mint(self.dir, 's', 'beck')
        _, _, body = request(self.dir, f'/s/{value}', method='POST',
                             form={'wahl': 'berichte'})
        self.assertIn('Ergebnis-Nachrichten können noch kommen', body)
        self.assertIn('keine E-Mails mehr', body)
        self.assertEqual(subs_mod().customer_get(self.dir, 'beck')['contact_state'],
                         'soft_stopped')
        request(self.dir, f'/s/{value}', method='POST', form={'wahl': 'alles'})
        self.assertEqual(subs_mod().customer_get(self.dir, 'beck')['contact_state'],
                         'hard_stopped')
        self.assertEqual(len(self._events('stop_soft')), 1)
        self.assertEqual(len(self._events('stop_hard')), 1)

    def test_feedback_click_is_one_event_with_the_tokens_verdict(self):
        value = tokens.mint(self.dir, 'f', 'beck', procedure_id='p1',
                            lot_id='L1', verdict='passend')
        _, _, body = request(self.dir, f'/f/{value}', method='POST')
        self.assertIn('Danke', body)
        ev = self._events('feedback')
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]['procedure_id'], ev[0]['verdict']),
                         ('p1', 'passend'))

    def test_recall_unresolvable_records_the_attempt_only(self):
        value = tokens.mint(self.dir, 'c', 'beck')
        _, _, body = request(self.dir, f'/c/{value}', method='POST',
                             form={'ref': 'nonsense-99'})
        self.assertIn('Nicht gefunden', body)
        ev = self._events('recall')
        self.assertEqual(len(ev), 1)
        self.assertIn('unresolved', ev[0]['detail'])


def subs_mod():
    import subscriptions
    return subscriptions


class Mailer(Base):
    """APP.md 7: the contact_state guard IS the module. A fake transport
    records what would have been sent; the network is never touched."""

    def setUp(self):
        super().setUp()
        self.sent = []
        self.transport = lambda payload: self.sent.append(payload) or 'id-1'

    def _mailer(self):
        import mailer
        return mailer

    def test_report_reaches_an_active_customer(self):
        subs_mod().customer_update(self.dir, 'beck',
                                   contact_email='b@beck.de')
        mid = self._mailer().send(self.dir, 'report', 'beck', 'Bericht',
                                  '<p>…</p>', transport=self.transport)
        self.assertEqual(mid, 'id-1')
        self.assertEqual(self.sent[0]['to'], ['b@beck.de'])

    def test_soft_stopped_gets_results_but_never_the_report(self):
        m = self._mailer()
        subs_mod().customer_update(self.dir, 'beck',
                                   contact_email='b@beck.de',
                                   contact_state='soft_stopped')
        with self.assertRaises(m.MailerError):
            m.send(self.dir, 'report', 'beck', 'x', 'x',
                   transport=self.transport)
        m.send(self.dir, 'results', 'beck', 'x', 'x',
               transport=self.transport)
        self.assertEqual(len(self.sent), 1)

    def test_hard_stopped_gets_nothing_and_the_attempt_is_a_ledgered_defect(self):
        m = self._mailer()
        subs_mod().customer_update(self.dir, 'beck',
                                   contact_email='b@beck.de',
                                   contact_state='hard_stopped')
        for kind in ('report', 'results', 'confirm'):
            with self.assertRaises(m.MailerError):
                m.send(self.dir, kind, 'beck', 'x', 'x',
                       transport=self.transport)
        self.assertEqual(self.sent, [])
        import ledger
        refused = [r for r in ledger.read(self.dir, 'app_events')
                   if r['kind'] == 'send_refused']
        self.assertEqual(len(refused), 3)

    def test_every_send_is_a_ledger_event(self):
        subs_mod().customer_update(self.dir, 'beck', contact_email='b@b.de')
        self._mailer().send(self.dir, 'confirm', 'beck', 'x', 'x',
                            transport=self.transport)
        import ledger
        sends = [r for r in ledger.read(self.dir, 'app_events')
                 if r['kind'] == 'send']
        self.assertEqual(len(sends), 1)

    def test_unknown_customer_field_is_refused(self):
        with self.assertRaises(subs_mod().SubscriptionError):
            subs_mod().customer_update(self.dir, 'beck', emial='x@y.de')
        with self.assertRaises(subs_mod().SubscriptionError):
            subs_mod().customer_update(self.dir, 'beck',
                                       contact_state='paused')


class Tokens(Base):
    def test_values_are_long_and_unique(self):
        seen = {tokens.mint(self.dir, 's', f'sub{i}') for i in range(50)}
        self.assertEqual(len(seen), 50)
        for v in seen:
            self.assertGreaterEqual(len(v), 32)

    def test_an_f_token_without_a_verdict_is_refused(self):
        """Not defaulted: a click on a token whose verdict became None would
        record an opinion nobody holds, and a click is not repeatable."""
        with self.assertRaises(tokens.TokenError):
            tokens.mint(self.dir, 'f', 'beck', procedure_id='p1', lot_id='L1')

    def test_non_f_tokens_carry_no_lot(self):
        with self.assertRaises(tokens.TokenError):
            tokens.mint(self.dir, 's', 'beck', procedure_id='p1')

    def test_unknown_purpose_is_refused(self):
        with self.assertRaises(tokens.TokenError):
            tokens.mint(self.dir, 'x', 'beck')

    def test_standing_tokens_are_reused(self):
        """A fresh stop link per report would leave a trail of equally valid
        links that can never be revoked as a set."""
        a = tokens.standing(self.dir, 's', 'beck')
        b = tokens.standing(self.dir, 's', 'beck')
        self.assertEqual(a, b)

    def test_a_revoked_standing_token_is_replaced_not_resurrected(self):
        a = tokens.standing(self.dir, 's', 'beck')
        tokens.revoke(self.dir, a)
        b = tokens.standing(self.dir, 's', 'beck')
        self.assertNotEqual(a, b)
        self.assertIsNone(tokens.resolve(self.dir, 's', a))

    def test_revocation_keeps_the_first_timestamp(self):
        value = tokens.mint(self.dir, 's', 'beck')
        tokens.revoke(self.dir, value, now='2026-08-10T00:00:00+00:00')
        tokens.revoke(self.dir, value, now='2026-08-11T00:00:00+00:00')
        con = __import__('db').connect(self.dir)
        row = con.execute('SELECT revoked_at FROM token WHERE token = ?',
                          (value,)).fetchone()
        con.close()
        self.assertEqual(row['revoked_at'], '2026-08-10T00:00:00+00:00')

    def test_revoke_all_kills_every_live_link_a_customer_holds(self):
        """What a hard stop needs: not merely "stop sending", but "the links
        already sitting in their inbox stop working"."""
        s = tokens.standing(self.dir, 's', 'beck')
        c = tokens.standing(self.dir, 'c', 'beck')
        other = tokens.standing(self.dir, 's', 'n3bau')
        self.assertEqual(tokens.revoke_all(self.dir, 'beck'), 2)
        self.assertIsNone(tokens.resolve(self.dir, 's', s))
        self.assertIsNone(tokens.resolve(self.dir, 'c', c))
        self.assertIsNotNone(tokens.resolve(self.dir, 's', other))

    def test_only_the_first_eight_characters_are_loggable(self):
        value = tokens.mint(self.dir, 's', 'beck')
        self.assertNotIn(value, tokens.short(value))
        self.assertTrue(value.startswith(tokens.short(value).rstrip('…')))

    def test_resolve_without_a_database_authorises_nothing(self):
        self.assertIsNone(tokens.resolve(self.dir, 's', 'anything'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
