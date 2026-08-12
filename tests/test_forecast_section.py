"""The per-trade forecast section — doc/TRADE_PAGES.md, doc/METHODS.md 0.

This is the **forecast** precision/recall (did a flagged lot really end with
0-1 bids), never the gate's. It can only come from `backtest.py`'s as-of
replay, so these tests build a synthetic document rather than replaying
anything — a real replay retrains the model at every weekly cutoff and takes
about half an hour (measured 2026-08-11: 33 min, 46 cutoffs).

Three states are asserted, and the unflattering one matters most: a trade
where the forecast does NOT beat the base rate must say so on the page
(operator's call, 2026-08-11), because silent omission is the version a
reader cannot audit.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import trade_pages as tp                                        # noqa: E402


def receipt(lots, generated='2026-08-11'):
    """lots: [(procedure_id, lot_id, flag, n_tenders)] — n_tenders None means
    examined while open but no award published yet."""
    return {'schema': 1, 'generated': generated, 'model_tag': 'test',
            'step_days': 7, 'cutoffs_trained': 3, 'n_lots': len(lots),
            'lots': [{'procedure_id': p, 'lot_id': l, 'cpv3': '452', 'flag': f,
                      'n_tenders': n} for p, l, f, n in lots],
            'subs': []}


def frame(keys):
    return pd.DataFrame({'procedure_id': [k[0] for k in keys],
                         'lot_id': [k[1] for k in keys]})


def slice_of(rec, keys):
    lots = frame(keys)
    return tp.forecast_for(rec, lots, pd.Series([True] * len(lots)))


class NoReceipt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_no_replay_given_is_no_claim(self):
        """No --replay is the state on a fresh checkout, and on every build
        that simply does not want to quote one. It must produce silence, not a
        figure."""
        self.assertIsNone(tp.load_replay(None))
        html = tp.forecast_section(None)
        self.assertIn('behaupten wir dazu nichts', html)
        self.assertNotIn('%', html)

    def test_a_missing_file_is_treated_as_absent(self):
        """Named but not there: the forecast section goes quiet, the market
        pages still build. A bad path must not cost the whole site."""
        self.assertIsNone(tp.load_replay(self.dir / 'nope.json'))

    def test_a_corrupt_document_is_treated_as_absent(self):
        p = self.dir / 'run.json'
        p.write_text('{ not json', encoding='utf-8')
        self.assertIsNone(tp.load_replay(p))

    def test_a_written_document_round_trips(self):
        p = self.dir / 'run.json'
        p.write_text(json.dumps(receipt([('p1', 'L1', True, 1)])),
                     encoding='utf-8')
        self.assertEqual(tp.load_replay(p)['n_lots'], 1)

    def test_an_unawarded_lot_is_neither_hit_nor_miss(self):
        """A lot examined while open but not yet awarded carries the
        `examined` denominator in the document. It must not reach a rate."""
        rec = receipt([('p1', 'L1', True, 1), ('p1', 'L2', True, None)])
        st, _ = slice_of(rec, [('p1', 'L1'), ('p1', 'L2')])
        self.assertEqual(st['n'], 1)
        self.assertEqual(st['flagged'], 1)


class Slicing(unittest.TestCase):
    def test_only_this_trades_lots_are_counted(self):
        """The slice is by the trade's own lots — the whole point, since the
        backtest's own table groups by CPV3, which buyers fill in wrongly."""
        rec = receipt([('p1', 'L1', True, 1), ('p2', 'L2', True, 9)])
        st, _ = slice_of(rec, [('p1', 'L1')])
        self.assertEqual(st['n'], 1)
        self.assertEqual(st['tp'], 1)

    def test_a_trade_with_no_replayed_lots_is_none(self):
        rec = receipt([('p1', 'L1', True, 1)])
        self.assertIsNone(slice_of(rec, [('other', 'L9')]))

    def test_the_statistic_is_loops_own(self):
        """Shared with the weekly report and the backtest deliberately: the
        replayed number is the one quoted until live awards accumulate, so it
        must be the same statistic, not a second implementation."""
        from loop import flag_stats
        rec = receipt([('p1', 'L1', True, 1), ('p1', 'L2', False, 0)])
        st, _ = slice_of(rec, [('p1', 'L1'), ('p1', 'L2')])
        self.assertEqual(st, flag_stats([{'flag': True, 'label': 1},
                                         {'flag': False, 'label': 1}]))


class ThreeStates(unittest.TestCase):
    def _stats(self, n_flagged_hits, n_flagged_miss, n_unflagged_hits,
               n_unflagged_miss):
        lots, i = [], 0
        for flag, label, count in ((True, 1, n_flagged_hits),
                                   (True, 0, n_flagged_miss),
                                   (False, 1, n_unflagged_hits),
                                   (False, 0, n_unflagged_miss)):
            for _ in range(count):
                i += 1
                lots.append(('p', f'L{i}', flag, 1 if label else 9))
        keys = [(l[0], l[1]) for l in lots]
        return slice_of(receipt(lots), keys)

    def test_too_few_checked_alarms_refuses_to_quote(self):
        """Precision over a handful of alarms is noise: flag one lot a year,
        get it right, claim 100 %."""
        fc = self._stats(5, 5, 10, 80)          # 10 alarms, below the floor
        html = tp.forecast_section(fc)
        self.assertIn('zu wenige für eine belastbare Quote', html)
        self.assertIn(str(tp.MIN_CHECKED), html)
        self.assertNotIn('-Fache', html)

    def test_a_forecast_that_beats_chance_is_quoted_with_both_denominators(self):
        fc = self._stats(20, 20, 10, 150)       # 40 alarms, 50 % vs 15 % base
        html = tp.forecast_section(fc)
        self.assertIn('40 Hinweisen', html)
        self.assertIn('20 mit höchstens einem Angebot (50 %)', html)
        self.assertIn('-Fache', html)
        self.assertIn('Rücktest', html)
        self.assertIn('2026-08-11', html)

    def test_german_number_formatting(self):
        """Same class of bug as the money one: Python's `:.0%` gives '50%'
        (English) where the rest of the page prints '50 %', and a lift factor
        must carry a decimal comma, not a point."""
        fc = self._stats(20, 20, 10, 150)
        html = tp.forecast_section(fc)
        self.assertIn('50 %', html)
        self.assertNotIn('50%', html)
        self.assertRegex(html, r'\d,\d-Fache')
        self.assertNotRegex(html, r'\d\.\d-Fache')

    def test_a_forecast_that_does_not_beat_chance_says_so(self):
        """The operator's call: print it rather than drop the section. A page
        that admits this is auditable; a silently missing section is not."""
        fc = self._stats(4, 36, 40, 20)         # 10 % precision, 44 % base
        html = tp.forecast_section(fc)
        self.assertIn('nicht besser als der Durchschnitt', html)
        self.assertIn('vollständigen Übersicht', html)
        self.assertNotIn('-Fache', html)

    def test_recall_is_always_stated_when_quotable(self):
        """Precision alone cannot be wrong in an interesting way. Recall is
        the half that says what the forecast walked past."""
        fc = self._stats(20, 20, 30, 150)
        html = tp.forecast_section(fc)
        self.assertIn('wir finden nicht alle', html)
        self.assertIn('40 %', html)             # 20 of 50 positives caught


class OnThePage(unittest.TestCase):
    def test_the_section_is_rendered_into_the_trade_page(self):
        months = [pd.Period('2026-01', freq='M')]
        lots = pd.DataFrame({
            'month': [pd.Period('2026-01', freq='M')] * 40,
            'resolved': [True] * 40,
            'n_tenders': [1] * 5 + [7] * 35,
            'award_value': [100_000.0] * 40,
            'result_code': ['selec-w'] * 40})
        f = tp.figures(lots, pd.Series([True] * 40), months, months)
        html = tp.page('Testgewerk', 'testgewerk', f, None)
        self.assertIn('Wie gut trifft unsere Einschätzung?', html)
        # and it sits before the method section, not after the CTA
        self.assertLess(html.index('Wie gut trifft'),
                        html.index('Woher die Zahlen kommen'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
