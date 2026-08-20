"""Two-stage procedures and the participation deadline — doc/MODELING.md 10.

2026-08-20: ~7 % of stored lots (neg-w-call / restricted) publish no offer
deadline — firms request participation by eForms BT-1311 first. The
extractor read only BT-131, so `deadline_date` was null, the replay dropped
the lots forever and the delivery promise refused them; they end 0-1 bids
~3x as often as the market. Found because the operator refused to believe
"5 % of tenders have no deadline".
"""

import sys
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import features                                                   # noqa: E402
import render                                                     # noqa: E402
import single_bidder as sb                                        # noqa: E402
import subscriptions                                              # noqa: E402
import util                                                       # noqa: E402


class TheExtractor(unittest.TestCase):
    def test_bt1311_is_read_with_the_same_path_style_as_the_offer_deadline(self):
        xml = ('<cac:TenderingProcess '
               'xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:'
               'CommonAggregateComponents-2" '
               'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:'
               'CommonBasicComponents-2">'
               '<cac:ParticipationRequestReceptionPeriod>'
               '<cbc:EndDate>2026-03-16+01:00</cbc:EndDate>'
               '</cac:ParticipationRequestReceptionPeriod>'
               '</cac:TenderingProcess>')
        process = ET.fromstring(xml)
        val = features._text(
            process, 'cac:ParticipationRequestReceptionPeriod/cbc:EndDate')
        self.assertEqual(features._date(val, 'test'), date(2026, 3, 16))

    def test_the_column_is_declared_in_roles_and_schema(self):
        self.assertEqual(features.ROLES['participation_deadline_date'], 'date')
        self.assertIn('participation_deadline_date', features.SCHEMA.names)


class ActionDeadline(unittest.TestCase):
    def frame(self):
        return pd.DataFrame({
            'deadline_date': ['2026-09-01', None, None],
            'participation_deadline_date': [None, '2026-03-16', None]})

    def test_offer_deadline_wins_participation_fills_neither_is_nat(self):
        d = sb.action_deadline(self.frame())
        self.assertEqual(str(d.iloc[0].date()), '2026-09-01')
        self.assertEqual(str(d.iloc[1].date()), '2026-03-16')
        self.assertTrue(pd.isna(d.iloc[2]))

    def test_a_store_without_the_column_still_works(self):
        old = pd.DataFrame({'deadline_date': ['2026-09-01', None]})
        d = sb.action_deadline(old)
        self.assertEqual(str(d.iloc[0].date()), '2026-09-01')
        self.assertTrue(pd.isna(d.iloc[1]))


class TheDeadlinePromise(unittest.TestCase):
    SUB = {'sub_id': 's', 'min_deadline_days': 14}

    def test_the_promise_holds_against_the_participation_deadline(self):
        today = date(2026, 3, 1)
        row = {'deadline_date': None,
               'participation_deadline_date': '2026-03-16'}
        self.assertTrue(subscriptions.deadline_ok(self.SUB, row, today))
        self.assertFalse(subscriptions.deadline_ok(
            self.SUB, row, date(2026, 3, 10)))    # 6 days left < 14

    def test_a_lot_with_neither_date_still_fails_a_promise(self):
        row = {'deadline_date': None, 'participation_deadline_date': None}
        self.assertFalse(subscriptions.deadline_ok(
            self.SUB, row, date(2026, 3, 1)))


class TheDisplays(unittest.TestCase):
    def test_util_frist_names_the_kind(self):
        self.assertEqual(util.frist({'deadline_date': '2026-09-01'}),
                         ('2026-09-01', False))
        self.assertEqual(
            util.frist({'deadline_date': None,
                        'participation_deadline_date': '2026-03-16+01:00'}),
            ('2026-03-16', True))
        # ledger rows carry 'None'/'nan' STRINGS from the str() era
        self.assertEqual(util.frist({'deadline_date': 'None',
                                     'participation_deadline_date': 'nan'}),
                         (None, False))

        class Lot:
            deadline_date = None
            participation_deadline_date = date(2026, 3, 16)
        self.assertEqual(util.frist(Lot()), ('2026-03-16', True))

    def test_the_report_cell_says_teilnahmeantrag(self):
        self.assertEqual(
            render.frist_de({'deadline_date': None,
                             'participation_deadline_date': '2026-03-16'}),
            '16.03.2026 (Teilnahmeantrag)')
        self.assertEqual(render.frist_de({'deadline_date': '2026-09-01'}),
                         '01.09.2026')


if __name__ == '__main__':
    unittest.main(verbosity=2)
