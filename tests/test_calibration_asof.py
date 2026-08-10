"""Calibration tests — the two ways an as-of calibration stopped being as-of.

    python -m unittest discover -t . -s tests     # from the repository root
    python tests/test_calibration_asof.py         # or directly

No real data and no parquet fixtures: both defects live in how `calibrate`
samples an already-loaded matrix, so the matrix is the fixture. Behaviours,
not implementations — a caller is promised that a calibration run against a
replayed world cannot be moved by notices that world has not published, and
that every threshold the search can return is a threshold a subscription can
actually carry.

The two regressions pinned here:

* the cohesion baseline and the admitted-volume pool were drawn from
  `range(len(mat))` — the shared embedding sidecar, which outruns any single
  as-of world — so the trust cut was set partly by later notices,
* `CODE_GRID` was `np.arange(0.70, 1.0, 0.025)`, whose binary error emits a
  13th point at 1.0000000000000002: past arange's own stop, unreachable by
  any cosine, and rejected by `subscriptions.validate`. It only ever surfaced
  when a thin world made it the least-leakage point, which is to say in a
  backtest and never in production.
"""

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backtest
import relevance
import subscriptions
from calibrate import (BASELINE_SAMPLE, CODE_GRID, SEED, SOFT_GRID,
                       TRUST_MARGIN, code_trust)

SOFT_OFF = 2.0   # the sentinel in SOFT_GRID meaning "soft channel off"


def _split_world(n_world, n_future, dim=4):
    """A matrix whose world rows and future rows are perfectly separable.

    World rows are all the same unit vector, so every world pair scores 1.0;
    future rows are orthogonal to them. Any baseline that touches a future row
    is dragged below 1.0, which makes the leak a visible number rather than a
    statistical argument.
    """
    mat = np.zeros((n_world + n_future, dim), dtype=np.float32)
    mat[:n_world, 0] = 1.0
    mat[n_world:, 1] = 1.0
    cpv = np.array([None] * len(mat), dtype=object)   # no deep codes, no cohesion
    return mat, cpv, np.arange(n_world)


class BaselineStaysInsideTheWorld(unittest.TestCase):
    """The cohesion baseline sets `cut`, so it decides which CPV codes are
    trusted. Drawn from the whole sidecar it decides that partly from notices
    the replayed week has not seen."""

    def test_future_rows_cannot_move_the_baseline(self):
        mat, cpv, world = _split_world(BASELINE_SAMPLE, BASELINE_SAMPLE * 3)
        baseline, cut, _, _ = code_trust(
            mat, cpv, np.random.default_rng(SEED), world)
        self.assertAlmostEqual(baseline, 1.0, places=6)
        self.assertAlmostEqual(cut, 1.0 + TRUST_MARGIN, places=6)

    def test_growing_the_future_changes_nothing(self):
        """The same world calibrates the same however much later data exists —
        the property a weekly replay actually relies on."""
        seen = set()
        for n_future in (0, 1, BASELINE_SAMPLE, BASELINE_SAMPLE * 5):
            mat, cpv, world = _split_world(BASELINE_SAMPLE, n_future)
            baseline, _, _, _ = code_trust(
                mat, cpv, np.random.default_rng(SEED), world)
            seen.add(round(baseline, 9))
        self.assertEqual(len(seen), 1, f'baseline moved with future rows: {seen}')

    def test_a_full_world_draws_exactly_what_it_used_to(self):
        """Live calibration must be untouched: there, every sidecar row is in
        the store, so `world` is every row and the draw is the old draw."""
        rng_state = np.random.default_rng(SEED)
        mat = rng_state.normal(size=(BASELINE_SAMPLE * 2, 8)).astype(np.float32)
        mat /= np.linalg.norm(mat, axis=1, keepdims=True)
        cpv = np.array([None] * len(mat), dtype=object)

        before = np.random.default_rng(SEED).choice(
            len(mat), BASELINE_SAMPLE, replace=False)          # the pre-fix draw
        # bound to one name on purpose: `m[i] @ m[i].T` builds two distinct
        # arrays and BLAS takes the general path, where `base @ base.T` takes
        # the symmetric one. Same sample, last-bit-different sum — and this
        # assertion is about the sample.
        base = mat[before]
        expected = float((base @ base.T).mean())

        baseline, _, _, _ = code_trust(
            mat, cpv, np.random.default_rng(SEED), np.arange(len(mat)))
        self.assertEqual(baseline, expected)


class EveryGridPointIsUsable(unittest.TestCase):
    """A threshold the search can return must be one a subscription can hold.
    `subscriptions.validate` is the contract that broke, so it is the oracle
    here rather than a hand-written range check."""

    def test_code_grid_is_the_intended_ladder(self):
        self.assertEqual(len(CODE_GRID), 12)
        np.testing.assert_allclose(
            CODE_GRID, [0.700, 0.725, 0.750, 0.775, 0.800, 0.825,
                        0.850, 0.875, 0.900, 0.925, 0.950, 0.975])

    def test_no_grid_point_exceeds_one(self):
        for name, grid in (('CODE_GRID', CODE_GRID),
                           ('SOFT_GRID', SOFT_GRID)):
            for v in grid:
                if float(v) == SOFT_OFF:
                    continue
                self.assertLessEqual(float(v), 1.0, f'{name} point {v!r} > 1')

    def test_every_grid_point_survives_subscription_validation(self):
        for field, grid in (('min_code_hard', CODE_GRID),
                            ('min_code_soft', SOFT_GRID)):
            for v in grid:
                if float(v) == SOFT_OFF:
                    continue
                row = {'sub_id': 'grid-probe', field: float(v)}
                try:
                    subscriptions.validate(row, source='<grid>')
                except subscriptions.SubscriptionError as e:
                    self.fail(f'{field}={v!r} rejected: {e}')

    def test_soft_grid_nests_the_hard_grid(self):
        """Configuration E (one threshold for both origins) has to stay inside
        F's search space, which is what the shared ladder buys."""
        np.testing.assert_allclose(SOFT_GRID[:len(CODE_GRID)], CODE_GRID)
        self.assertEqual(float(SOFT_GRID[-1]), SOFT_OFF)


class ChannelOffIsNotCustomerInput(unittest.TestCase):
    """A calibration switches a channel off by going out of range — text off
    is `inf`, soft off is SOFT_GRID's 2.0. Those are search results, not
    customer input, and the split of who accepts them is load-bearing: the
    backtest crashed for weeks of replay because a thin world calibrated a
    channel off and the value was routed through a subscription line."""

    OFF = {'min_relevance': float('inf'), 'min_code_soft': SOFT_OFF}

    def test_a_customer_line_refuses_an_off_switch(self):
        for field, value in self.OFF.items():
            with self.assertRaises(subscriptions.SubscriptionError):
                subscriptions.validate({'sub_id': 'probe', field: value},
                                       source='<off>')

    def test_the_gate_config_takes_it(self):
        cfg = relevance.DEFAULT_CONFIG.replace(**self.OFF)
        self.assertEqual(cfg.min_code_soft, SOFT_OFF)
        self.assertEqual(cfg.min_relevance, float('inf'))

    def test_the_backtest_never_puts_a_bar_on_the_subscription(self):
        """as_of_profile must DROP the customer's bars, not override them —
        otherwise an off-switch is handed to validate all over again.

        Only `build_profile` is stubbed, so `subscriptions.override` and its
        validation really run: the spec this produces is a spec the wire
        format accepts.
        """
        class Gate:
            rows = [{'publication_number': 'PN-1'}]
            by_key = {('p1', 'l1'): 0}

        awards = pd.DataFrame({'procedure_id': ['p1'], 'lot_id': ['l1'],
                               'winner_names': [['F GmbH']]})
        sub = {'sub_id': 's', 'name': 'F GmbH', 'min_relevance': 0.7,
               'min_code_hard': 0.8, 'min_code_soft': 0.75}

        real = backtest.rel.build_profile
        backtest.rel.build_profile = lambda _gate, spec: spec
        try:
            spec = backtest.as_of_profile(Gate(), sub, awards)
        finally:
            backtest.rel.build_profile = real

        self.assertEqual(spec['profile_refs'], ['PN-1'])
        for bar in backtest.CALIBRATED_BARS:
            self.assertNotIn(bar, spec, f'{bar} reached the subscription spec')


if __name__ == '__main__':
    unittest.main()
