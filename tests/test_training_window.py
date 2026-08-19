"""`single_bidder.training_window` — how far back the model may learn.

2026-08-19, operator: CatBoost cannot learn incrementally, so every Monday
trains from scratch on everything, and the Monday grows with the archive;
the market also drifts. Which matters is measured by the replay under the
knob `TRAIN_WINDOW_MONTHS` (knobs.py). Here: the cut is by a lot's FIRST
publication, whole lots only, and None cuts nothing.
"""

import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import knobs                                                       # noqa: E402
import single_bidder as sb                                         # noqa: E402


def frame():
    # three lots; L2 has a corrigendum published much later than its first notice
    return pd.DataFrame({
        'procedure_id': ['p', 'p', 'p', 'p'],
        'lot_id': ['L1', 'L2', 'L2', 'L3'],
        'publication_date': ['2024-01-10', '2024-06-01', '2026-05-01', '2026-03-01'],
        'label': [1, 0, 0, 1],
    })


class TrainingWindow(unittest.TestCase):
    def test_none_is_everything_and_the_same_object(self):
        d = frame()
        self.assertIs(sb.training_window(d, months=None), d)

    def test_a_lot_is_in_or_out_whole_by_its_first_publication(self):
        d = frame()
        # 12 months before 2026-08-01: lots first published after 2025-08-01
        out = sb.training_window(d, as_of='2026-08-01', months=12)
        self.assertEqual(sorted(out.lot_id), ['L3'])
        # L2's corrigendum (2026-05) does not pull the lot back in: first
        # publication 2024-06 is outside, so BOTH of its rows are out
        self.assertNotIn('L2', set(out.lot_id))
        # 36 months: everything
        self.assertEqual(len(sb.training_window(d, as_of='2026-08-01', months=36)), 4)

    def test_as_of_defaults_to_the_newest_publication(self):
        d = frame()
        # newest is 2026-05-01; 12 months back -> after 2025-05-01: L3 only
        # (L2's first publication is 2024-06, out; its later row goes with it)
        out = sb.training_window(d, months=12)
        self.assertEqual(sorted(out.lot_id), ['L3'])

    def test_the_knob_is_on_the_queue_with_none_as_the_codes_value(self):
        k = next(k for k in knobs.KNOBS if k.knob == 'single_bidder.TRAIN_WINDOW_MONTHS')
        self.assertEqual(k.bucket, 'competitiveness')
        self.assertEqual(k.harness, 'replay')
        self.assertIn(k.current(), k.grid())
        self.assertIn(None, k.grid())
        self.assertIn(24, k.grid())


if __name__ == '__main__':
    unittest.main(verbosity=2)
