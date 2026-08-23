"""A store too thin to calibrate says so — it does not crash.

    python -m unittest discover -t . -s tests     # from the repository root

`calibrate.WorldTooThin` is the project's word for "this world cannot carry a
threshold yet", and every as-of harness catches it and skips the cutoff
(`rewind_all.py`, `rewind_report.py`, `rewind_win.py`). It was raised for one
of the three ways that happens — too few embedded lots — and the other two
reached numpy instead:

    ValueError: need at least one array to concatenate

which is what a replay over the widened store hit on 2026-08-23, twenty-five
minutes into a run, at the first cutoff that had any firms at all. Both of
those states are ORDINARY at the start of an archive: six months in, nobody
has won three times yet, and the few winners there are all sit in one corner
of the market. Ordinary states get a sentence, not a stack trace.

The fixture is a real sidecar on disk and real frames in memory — the guards
under test live in `calibrate()` itself, so the test calls it.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import calibrate as cal
from embed import DIM, sidecar_dir

N_LOTS = cal.BASELINE_SAMPLE + 100      # past the thin-world rule, so the
                                        # guards under test are what fires


def _store(tmp, n_lots=N_LOTS, cpv='72000000'):
    """A world of `n_lots` embedded lots, all under one CPV code. Written the
    way `embed.py` writes it, because that is what `calibrate` reads."""
    d = sidecar_dir(tmp)
    d.mkdir(parents=True, exist_ok=True)
    keys = [(f'P{i:05d}', 'LOT-0001') for i in range(n_lots)]
    rng = np.random.default_rng(11)
    mat = rng.normal(size=(n_lots, DIM)).astype(np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True)
    np.save(d / 'lots.npy', mat)
    with open(d / 'lots_index.jsonl', 'w', encoding='utf-8') as f:
        for p, l in keys:
            f.write(json.dumps({'procedure_id': p, 'lot_id': l}) + '\n')
    # the label sidecar: one code, enough for the dictionary to exist
    np.save(d / 'cpv_labels.npy', mat[:1])
    with open(d / 'cpv_labels_index.jsonl', 'w', encoding='utf-8') as f:
        f.write(json.dumps({'code': cpv, 'label': 'IT'}) + '\n')
    tenders = pd.DataFrame({
        'procedure_id': [p for p, _ in keys],
        'lot_id': [l for _, l in keys],
        'cpv_main': [cpv] * n_lots,
        'cpv_additional': [[] for _ in range(n_lots)],
    })
    return tenders


def _awards(tenders, wins_per_firm):
    """One award row per lot, handing `wins_per_firm` lots to each firm."""
    winners = []
    for i in range(len(tenders)):
        winners.append([f'Firma {i // wins_per_firm}'])
    return pd.DataFrame({
        'procedure_id': tenders['procedure_id'],
        'lot_id': tenders['lot_id'],
        'winner_names': winners,
    })


class TooThinToCalibrate(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_nobody_has_won_three_times_yet(self):
        """The first months of any archive. Not an error — a state."""
        tenders = _store(self.dir)
        awards = _awards(tenders, wins_per_firm=1)
        with self.assertRaises(cal.WorldTooThin) as e:
            cal.calibrate(self.dir, tenders=tenders, awards=awards)
        self.assertIn(str(cal.MIN_WINS), str(e.exception))
        self.assertIn('nothing to calibrate', str(e.exception))

    def test_firms_but_no_lot_outside_their_own_class(self):
        """The 2026-08-23 crash: firms exist, every one of them is skipped
        because a negative must come from another line of business, and the
        non-match pile ends up empty."""
        tenders = _store(self.dir)
        awards = _awards(tenders, wins_per_firm=5)
        with self.assertRaises(cal.WorldTooThin) as e:
            cal.calibrate(self.dir, tenders=tenders, awards=awards)
        self.assertIn('non-match pile is empty', str(e.exception))
        self.assertIn('another line of business', str(e.exception))

    def test_it_is_never_a_numpy_error(self):
        """The point of the whole file: whatever is missing, the caller gets
        WorldTooThin — which every as-of harness already catches — and never
        a ValueError from three hundred lines further down."""
        tenders = _store(self.dir)
        for wins in (1, 5):
            with self.subTest(wins_per_firm=wins):
                try:
                    cal.calibrate(self.dir, tenders=tenders,
                                  awards=_awards(tenders, wins))
                except cal.WorldTooThin:
                    pass
                except ValueError as bad:        # noqa: PERF203
                    self.fail(f'numpy error instead of WorldTooThin: {bad}')


if __name__ == '__main__':
    unittest.main()
