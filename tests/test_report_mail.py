"""The weekly report goes out by e-mail, with the footer every mail carries —
doc/ONBOARDING.md 9.3–9.4, doc/APP.md 8. Own temp directory, fake transport,
no network; the renderer and the app are driven directly."""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import delivering                                              # noqa: E402
import ledger                                                  # noqa: E402
import mailer                                                  # noqa: E402
import render                                                  # noqa: E402
import subscriptions                                           # noqa: E402
import tokens                                                  # noqa: E402
from tests.test_app import request                             # noqa: E402
from tests.test_render import SUB, lot, report_of, sel_of      # noqa: E402

TODAY = date(2026, 8, 17)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        import gc
        self.addCleanup(gc.collect)
        self.sent = []
        self.transport = lambda payload: self.sent.append(payload) or 'id-1'

    def events(self, kind):
        return [r for r in ledger.read(self.dir, 'app_events')
                if r['kind'] == kind]


class Footer(Base):
    def test_footer_carries_stop_recall_art21_and_headers(self):
        subscriptions.customer_update(self.dir, 'beck', contact_email='b@b.de')
        html, headers = mailer.footer(self.dir, 'beck', base_url='https://a')
        self.assertIn('https://a/s/', html)
        self.assertIn('https://a/c/', html)
        self.assertIn('Abbestellen', html)
        self.assertIn('Art. 21', html)
        stop = headers['List-Unsubscribe'].strip('<>')
        self.assertTrue(stop.startswith('https://a/s/'))
        self.assertEqual(headers['List-Unsubscribe-Post'],
                         'List-Unsubscribe=One-Click')
        # the standing tokens resolve for their purpose, and only that one
        s_tok = stop.rsplit('/', 1)[1]
        self.assertIsNotNone(tokens.resolve(self.dir, 's', s_tok))
        self.assertIsNone(tokens.resolve(self.dir, 'c', s_tok))
        # standing: the same link next time, no trail of equally valid ones
        html2, _ = mailer.footer(self.dir, 'beck', base_url='https://a')
        self.assertEqual(html, html2)

    def test_headers_reach_the_transport(self):
        subscriptions.customer_update(self.dir, 'beck', contact_email='b@b.de')
        html, headers = mailer.footer(self.dir, 'beck')
        mailer.send(self.dir, 'report', 'beck', 'S', '<p>x</p>' + html,
                    headers=headers, transport=self.transport)
        self.assertEqual(self.sent[0]['headers']['List-Unsubscribe-Post'],
                         'List-Unsubscribe=One-Click')
        self.assertIn('Abbestellen', self.sent[0]['html'])

    def test_one_click_unsubscribe_is_the_hard_stop(self):
        subscriptions.customer_update(self.dir, 'beck', contact_email='b@b.de')
        _, headers = mailer.footer(self.dir, 'beck', base_url='https://a')
        path = headers['List-Unsubscribe'].strip('<>')[len('https://a'):]
        status, _, body = request(self.dir, path, 'POST',
                                  {'List-Unsubscribe': 'One-Click'})
        self.assertEqual(status, '200 OK')
        cust = subscriptions.customer_get(self.dir, 'beck')
        self.assertEqual(cust['contact_state'], 'hard_stopped')
        self.assertEqual(len(self.events('stop_hard')), 1)


class Report(Base):
    def test_every_pick_carries_two_feedback_links_and_the_criterion(self):
        subscriptions.customer_update(self.dir, SUB['sub_id'],
                                      contact_email='t@t.de')
        link, footer_html, headers = delivering.mail_links(self.dir,
                                                           SUB['sub_id'])
        picks = [lot('p1', 'L1', price_weight_pct=100.0),
                 lot('p2', 'L2', price_weight_pct=70.0)]
        page, _ = report_of(SUB, sel_of(ranked=picks, picks=picks),
                            feedback_link=link, footer_html=footer_html)
        self.assertIn('100 % Preis', page)
        self.assertIn('Preis 70 / Qualität 30', page)
        self.assertEqual(page.count('ist unser Geschäft'), 2)
        self.assertEqual(page.count('nicht unser Geschäft'), 2)
        self.assertIn('Abbestellen', page)
        self.assertNotIn('TenderMining', page)
        # an f link resolves to its lot and verdict, and works in the app
        import re
        f_urls = re.findall(r'https://app\.murara\.eu/f/([A-Za-z0-9_-]+)', page)
        self.assertEqual(len(f_urls), 4)
        row = tokens.resolve(self.dir, 'f', f_urls[1])
        self.assertEqual((row['procedure_id'], row['lot_id'], row['verdict']),
                         ('p1', 'L1', 'nicht unser Geschäft'))
        status, _, body = request(self.dir, f'/f/{f_urls[1]}', 'POST')
        self.assertEqual(status, '200 OK')
        ev = self.events('feedback')
        self.assertEqual((ev[0]['procedure_id'], ev[0]['verdict']),
                         ('p1', 'nicht unser Geschäft'))
        # exactly one stop link, resolving as `s` for this customer
        s_urls = re.findall(r'https://app\.murara\.eu/s/([A-Za-z0-9_-]+)', page)
        self.assertEqual(len(set(s_urls)), 1)
        self.assertEqual(tokens.resolve(self.dir, 's', s_urls[0])['sub_id'],
                         SUB['sub_id'])

    def test_no_address_means_file_only_and_no_tokens(self):
        link, footer_html, headers = delivering.mail_links(self.dir, 'nobody')
        self.assertIsNone(link)
        self.assertEqual(footer_html, '')
        picks = [lot('p1', 'L1')]
        page, _ = report_of(SUB, sel_of(ranked=picks, picks=picks),
                            feedback_link=link, footer_html=footer_html)
        self.assertNotIn('unser Geschäft', page)
        self.assertNotIn('Abbestellen', page)

    def test_send_report_goes_out_and_is_ledgered(self):
        subscriptions.customer_update(self.dir, SUB['sub_id'],
                                      contact_email='t@t.de')
        _, footer_html, headers = delivering.mail_links(self.dir, SUB['sub_id'])
        mid = delivering.send_report(self.dir, SUB, TODAY, '<p>r</p>',
                                     headers, transport=self.transport)
        self.assertEqual(mid, 'id-1')
        self.assertEqual(self.sent[0]['to'], ['t@t.de'])
        self.assertIn('Murara-Bericht 17.08.2026', self.sent[0]['subject'])
        self.assertIn('List-Unsubscribe', self.sent[0]['headers'])
        self.assertEqual(len(self.events('send')), 1)

    def test_send_report_never_raises(self):
        subscriptions.customer_update(self.dir, SUB['sub_id'],
                                      contact_email='t@t.de',
                                      contact_state='soft_stopped')
        mid = delivering.send_report(self.dir, SUB, TODAY, '<p>r</p>', None,
                                     transport=self.transport)
        self.assertIsNone(mid)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.events('send_refused')), 1)

        def boom(payload):
            raise OSError('resend down')
        subscriptions.customer_update(self.dir, SUB['sub_id'],
                                      contact_state='active')
        self.assertIsNone(delivering.send_report(self.dir, SUB, TODAY,
                                                 '<p>r</p>', None,
                                                 transport=boom))
        self.assertEqual(len(self.events('send_failed')), 1)
        self.assertIn('resend down', self.events('send_failed')[0]['detail'])


class Criterion(unittest.TestCase):
    def test_lines(self):
        self.assertEqual(render.criterion_line({'price_weight_pct': 100}),
                         '100 % Preis')
        self.assertEqual(render.criterion_line({'price_weight_pct': 60.0}),
                         'Preis 60 / Qualität 40')
        self.assertEqual(render.criterion_line({'award_criterion_kind': 'price'}),
                         '100 % Preis')
        self.assertEqual(render.criterion_line({'price_weight_pct': float('nan'),
                                                'award_criterion_kind': 'quality'}),
                         'Preis und Qualität')
        self.assertEqual(render.criterion_line({}), '')


if __name__ == '__main__':
    unittest.main()
