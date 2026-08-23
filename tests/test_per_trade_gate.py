"""The gate is measured per trade — pipeline/gate-per-trade.md.

    python -m unittest discover -t . -s tests

The defect these pin: once the store spans trades, a POOLED leakage number
lets one trade shelter behind another. A shared rule that leaks 2.4% on
construction and 1.4% on IT averages to 1.9%, passes the 2.2% bar, and the
construction customer carries a breach nobody measured. No real data — the
judge payload is the fixture, because the defect lives entirely in how the
rejector reads it.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backplay
import evidence
import knobs


def payload(pooled, by_trade=None, n_neg=1000, recall=0.6, n_pos=500):
    return {'kind': 'judge', 'counts': {'n_pos': n_pos, 'n_neg': n_neg},
            'configurations': [{'name': 'evidence gate (committed)',
                                'hard19': None, 'benchmark': '1/1',
                                'recall': recall, 'leakage': pooled,
                                'volume': None, 'hard_fails': [],
                                'by_trade': by_trade}]}


class TheTradeGroups(unittest.TestCase):

    def test_the_three_divisions_map_to_two_groups(self):
        self.assertEqual(evidence.trade_of('45213150'), 'construction')
        self.assertEqual(evidence.trade_of('48000000'), 'it')
        self.assertEqual(evidence.trade_of('72268000'), 'it')

    def test_a_code_outside_the_scope_has_no_group(self):
        self.assertIsNone(evidence.trade_of('30200000'))
        self.assertIsNone(evidence.trade_of(None))

    def test_a_firm_belongs_to_the_majority_of_its_wins(self):
        self.assertEqual(
            evidence.firm_trade(['45213150', '45350000', '72000000']),
            'construction')

    def test_a_firm_without_a_majority_is_pooled_only(self):
        """A system house that wins both is not evidence about either trade."""
        self.assertIsNone(evidence.firm_trade(['45213150', '72000000']))
        self.assertIsNone(evidence.firm_trade([]))


class TheHardBarHoldsPerTrade(unittest.TestCase):

    def test_a_trade_over_the_bar_is_not_sheltered_by_the_average(self):
        """The whole point: pooled 1.9% passes, construction 2.4% does not."""
        m = backplay.judge_read(payload(
            0.019, {'construction': {'leakage': 0.024, 'n_neg': 600,
                                     'recall': 0.6, 'n_pos': 300},
                    'it': {'leakage': 0.014, 'n_neg': 400,
                           'recall': 0.6, 'n_pos': 200}}))
        self.assertEqual(len(m), 1)
        self.assertAlmostEqual(m[0]['leakage'], 0.024)
        self.assertEqual(m[0]['leakage_trade'], 'construction')
        self.assertGreater(m[0]['leakage'], knobs.HARD_BAR)

    def test_the_pooled_number_still_binds_when_it_is_the_worst(self):
        m = backplay.judge_read(payload(
            0.030, {'construction': {'leakage': 0.010, 'n_neg': 600,
                                     'recall': 0.6, 'n_pos': 300}}))
        self.assertAlmostEqual(m[0]['leakage'], 0.030)
        self.assertIsNone(m[0].get('leakage_trade'))

    def test_a_trade_with_too_few_negatives_cannot_bind(self):
        """One flipped lot in a thin trade moves the rate more than a real
        change would, so it may not kill a candidate."""
        m = backplay.judge_read(payload(
            0.019, {'it': {'leakage': 0.5, 'n_neg': backplay.TRADE_MIN_NEG - 1,
                           'recall': 0.6, 'n_pos': 10}}))
        self.assertAlmostEqual(m[0]['leakage'], 0.019)
        self.assertIsNone(m[0].get('leakage_trade'))

    def test_an_older_payload_without_trades_reads_as_before(self):
        m = backplay.judge_read(payload(0.019))
        self.assertAlmostEqual(m[0]['leakage'], 0.019)
        self.assertNotIn('leakage_by_trade', m[0])

    def test_the_rejector_kills_on_the_binding_trade_and_names_it(self):
        """`rejects()` needs no per-trade logic of its own — judge_read hands
        it the worst trade's number, so the existing bar does the work."""
        cand = backplay.judge_read(payload(
            0.019, {'construction': {'leakage': 0.024, 'n_neg': 600,
                                     'recall': 0.6, 'n_pos': 300}}))
        dead, why = backplay.rejects([], cand)
        self.assertTrue(dead)
        self.assertIn('construction', why)

    def test_a_candidate_clean_in_every_trade_survives(self):
        cand = backplay.judge_read(payload(
            0.015, {'construction': {'leakage': 0.018, 'n_neg': 600,
                                     'recall': 0.6, 'n_pos': 300},
                    'it': {'leakage': 0.012, 'n_neg': 400,
                           'recall': 0.6, 'n_pos': 200}}))
        dead, _ = backplay.rejects([], cand)
        self.assertFalse(dead)


if __name__ == '__main__':
    unittest.main()
