"""The app's routing and the token layer — doc/APP.md 2 and 3.

Own file, like tests/test_housekeeping.py: importing `app` costs only stdlib,
and keeping it out of test_storage.py leaves that suite free of anything that
opens a socket. Nothing here binds a port — the WSGI callable is called
directly, which is what a real request reduces to anyway.

No real data: every test builds its own temporary directory.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app                                                      # noqa: E402
import tokens                                                   # noqa: E402


def request(data_dir, path, method='GET'):
    """-> (status, headers dict, body). A request without a server."""
    captured = {}

    def start_response(status, headers):
        captured['status'] = status
        captured['headers'] = dict(headers)

    body = app.make_app(data_dir)(
        {'REQUEST_METHOD': method, 'PATH_INFO': path}, start_response)
    return captured['status'], captured['headers'], b''.join(body).decode('utf-8')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)


class PublicPages(Base):
    def test_root_is_html_and_says_what_this_is(self):
        status, headers, body = request(self.dir, '/')
        self.assertEqual(status, '200 OK')
        self.assertTrue(headers['Content-Type'].startswith('text/html'))
        self.assertIn('<!doctype html>', body)
        self.assertIn('TenderMining', body)

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

    def test_healthz_reports_unknown_freshness_without_a_cycle(self):
        """A fresh deployment has no checkpoint. Health must still answer —
        one that raises when the data is missing is worse than one that
        admits it does not know."""
        status, _, body = request(self.dir, '/healthz')
        self.assertEqual(status, '200 OK')
        self.assertIn('cycle_last_success=unknown', body)

    def test_healthz_reports_the_cycles_last_success(self):
        logs = self.dir / 'logs'
        logs.mkdir()
        (logs / 'loop_checkpoint.json').write_text(
            '{"last_success_to": "20260810"}', encoding='utf-8')
        _, _, body = request(self.dir, '/healthz')
        self.assertIn('cycle_last_success=20260810', body)

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

    def test_post_to_an_unbuilt_handler_refuses_visibly(self):
        value = tokens.mint(self.dir, 't', 'beck')
        status, _, _ = request(self.dir, f'/t/{value}', method='POST')
        self.assertEqual(status, '405 Method Not Allowed')


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
