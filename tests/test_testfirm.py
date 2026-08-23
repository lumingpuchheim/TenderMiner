"""testfirm.py — doc/PAYMENT.md 3b: twins carry a real firm's profile and a
test identity, and cannot touch the real firm's row."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import admin                                                    # noqa: E402
import invite                                                   # noqa: E402
import ledger                                                   # noqa: E402
import subscriptions                                            # noqa: E402
import testfirm                                                 # noqa: E402
from tests.test_invite import DUNKEL, write_store               # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        import gc
        self.addCleanup(gc.collect)
        write_store(self.dir)


class Twins(Base):
    def test_add_builds_a_real_profile_under_a_test_identity(self):
        sub_id, tname = testfirm.add(self.dir, DUNKEL, email='op@example.org')
        self.assertTrue(sub_id.startswith('test-'))
        self.assertEqual(tname, f'TEST {DUNKEL}')
        cust = subscriptions.customer_get(self.dir, sub_id)
        self.assertEqual(cust['contact_email'], 'op@example.org')
        self.assertEqual(cust['contact_state'], 'active')
        sub = subscriptions.one(self.dir, '2026-12-31', sub_id)
        self.assertTrue(sub['profile_refs'])       # the REAL firm's awards
        self.assertEqual(sub['award_names'], [tname])
        # the real firm is untouched and still invitable
        self.assertIsNone(subscriptions.customer_get(
            self.dir, invite.slug(DUNKEL)))
        invite.add(self.dir, DUNKEL)               # raises if the twin claimed it

    def test_add_twice_refuses_and_paid_skips_the_clock(self):
        testfirm.add(self.dir, DUNKEL, email='op@example.org')
        with self.assertRaises(testfirm.TestfirmError):
            testfirm.add(self.dir, DUNKEL, email='op@example.org')
        testfirm.remove(self.dir, DUNKEL)
        sub_id, _ = testfirm.add(self.dir, DUNKEL, email='op@example.org',
                                 paid=True)
        sub = subscriptions.one(self.dir, '2026-12-31', sub_id)
        self.assertEqual(sub.get('plan'), 'paid')

    def test_remove_is_restlos_and_only_reaches_twins(self):
        sub_id, _ = testfirm.add(self.dir, DUNKEL, email='op@example.org')
        _, gone = testfirm.remove(self.dir, DUNKEL)
        self.assertIsNone(subscriptions.customer_get(self.dir, sub_id))
        self.assertEqual([e for e in ledger.read(self.dir, 'app_events')
                          if e['sub_id'] == sub_id], [])
        # no twin on file -> refuses, and never resolves to the real firm
        with self.assertRaises(testfirm.TestfirmError):
            testfirm.remove(self.dir, DUNKEL)

    def test_counts_line_skips_twins(self):
        testfirm.add(self.dir, DUNKEL, email='op@example.org', paid=True)
        c = admin.counts(admin.state_of(self.dir))
        self.assertEqual(c['Kunde'], 0)
        self.assertEqual(c['angemeldet'], 0)

    def test_default_email_comes_from_sales_owners(self):
        with mock.patch.dict('os.environ',
                             {'TM_SALES_OWNERS': 'op=op@murara.eu'}):
            sub_id, _ = testfirm.add(self.dir, DUNKEL)
        self.assertEqual(subscriptions.customer_get(
            self.dir, sub_id)['contact_email'], 'op@murara.eu')
        testfirm.remove(self.dir, DUNKEL)
        with mock.patch.dict('os.environ', {'TM_SALES_OWNERS': ''}):
            with self.assertRaises(testfirm.TestfirmError):
                testfirm.add(self.dir, DUNKEL)


if __name__ == '__main__':
    unittest.main()
