"""The multi-hot encoding of cpv_additional — doc/EXPERIMENTS.md §3.

    python -m unittest discover -t . -s tests        # from the repository root
    python tests/test_multihot.py                    # or directly

Behaviours promised, not implementation: no combination-string column survives
for a list-hierarchical column; the vocabulary is fixed on the frame it was
fitted on and reproduces identical columns on a subset that carries an unseen
code; the leakage-rule-4 guard passes however many distinct combinations the
archive has grown to; the vocabulary round-trips through JSON (it lives in the
champion's meta.json); CatBoost trains and scores on the result.
"""

import json
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import single_bidder as sb

ROLES = {
    'procedure_id': None, 'lot_id': None, 'buyer_name': None,
    'publication_date': 'date',
    'cpv_main': 'hierarchical', 'cpv_additional': 'hierarchical',
    'est_value_lot': 'numeric', 'procedure_type': 'categorical',
    'selection_criteria_types': 'categorical',      # a flat LIST categorical
}
CRITERIA = ['slc-abil', 'slc-stand', 'slc-suit', 'slc-econ', 'slc-rare']


def frame(n_lots, rng, extra_codes=()):
    """n_lots lots, each with 0-3 additional codes drawn from a small pool so a
    handful of codes clear the support threshold and the rest do not."""
    pool = ['45210000', '45230000', '45310000', '45330000', '45400000',
            '71000000', '90000000'] + list(extra_codes)
    weights = np.array([8, 8, 6, 5, 4, 1, 1] + [1] * len(extra_codes), dtype=float)
    weights /= weights.sum()
    rows = []
    for i in range(n_lots):
        k = rng.integers(0, 4)
        codes = list(rng.choice(pool, size=k, replace=False, p=weights)) if k else []
        kc = rng.integers(0, 4)
        crit = list(rng.choice(CRITERIA, size=kc, replace=False,
                               p=[.3, .3, .2, .19, .01])) if kc else []
        rows.append({
            'procedure_id': f'p{i}', 'lot_id': 'LOT-0001', 'buyer_name': 'B',
            'publication_date': '2026-01-01',
            'cpv_main': rng.choice(['45210000', '45230000', '45310000']),
            'cpv_additional': codes,
            'est_value_lot': float(rng.integers(1, 100)) * 1000,
            'procedure_type': rng.choice(['open', 'restricted']),
            'selection_criteria_types': crit,
        })
    return pd.DataFrame(rows)


class Vocabulary(unittest.TestCase):

    def setUp(self):
        self.rng = np.random.default_rng(7)
        self.tenders = frame(400, self.rng)

    def test_support_is_counted_over_lots_not_rows(self):
        # duplicate every row (a second notice per lot): support must not double
        doubled = pd.concat([self.tenders, self.tenders], ignore_index=True)
        a = sb.fit_multihot(self.tenders, ROLES, min_support=30)
        b = sb.fit_multihot(doubled, ROLES, min_support=30)
        self.assertEqual(a, b)

    def test_rare_codes_are_not_in_the_vocabulary(self):
        mh = sb.fit_multihot(self.tenders, ROLES, min_support=30)
        cpv4 = mh['vocab']['cpv_additional']['cpv4']
        self.assertIn('4521', cpv4)
        self.assertNotIn('7100', cpv4)          # weight 1 of ~34: well under 30 lots
        self.assertEqual(cpv4, sorted(cpv4))
        self.assertEqual(mh['min_support'], 30)

    def test_round_trips_through_json(self):
        mh = sb.fit_multihot(self.tenders, ROLES)
        self.assertEqual(json.loads(json.dumps(mh)), mh)


class Columns(unittest.TestCase):

    def setUp(self):
        self.rng = np.random.default_rng(11)
        self.tenders = frame(400, self.rng)
        self.X, self.cats, self.nums, self.excl = sb.build_features(
            self.tenders, ROLES, list_frame=self.tenders)

    def test_no_combination_column_survives(self):
        for lvl in ('cpv2', 'cpv3', 'cpv4'):
            self.assertNotIn(f'cpv_additional__{lvl}', self.cats)
            self.assertNotIn(f'cpv_additional__{lvl}', self.X.columns)
        # the non-list hierarchical column keeps its per-level truncation
        self.assertIn('cpv_main__cpv4', self.cats)

    def test_flags_rare_and_total_are_numeric_and_named(self):
        self.assertIn('cpv_additional__cpv4__has_4521', self.nums)
        self.assertIn('cpv_additional__cpv4__n_rare', self.nums)
        self.assertIn('cpv_additional__cpv4__n', self.nums)
        has = self.X['cpv_additional__cpv4__has_4521']
        self.assertTrue(set(has.unique()) <= {0, 1})
        expected = self.tenders['cpv_additional'].map(
            lambda v: int(any(str(c)[:4] == '4521' for c in v)))
        self.assertTrue((has.to_numpy() == expected.to_numpy()).all())
        n = self.X['cpv_additional__cpv4__n']
        self.assertTrue((n.to_numpy() == self.tenders['cpv_additional'].map(
            lambda v: len({str(c)[:4] for c in v})).to_numpy()).all())

    def test_flat_list_categorical_is_multihot_too(self):
        # 32 criterion types made 2,102 combinations on the real store: the
        # combination column must be gone, one flag per value in its place
        self.assertNotIn('selection_criteria_types', self.cats)
        self.assertNotIn('selection_criteria_types', self.X.columns)
        self.assertIn('selection_criteria_types__has_slc-abil', self.nums)
        self.assertIn('selection_criteria_types__n_rare', self.nums)
        self.assertIn('selection_criteria_types__n', self.nums)
        self.assertNotIn('selection_criteria_types__has_slc-rare', self.nums)  # under support
        # a scalar categorical is untouched
        self.assertIn('procedure_type', self.cats)
        expected = self.tenders['selection_criteria_types'].map(lambda v: int('slc-abil' in v))
        self.assertTrue((self.X['selection_criteria_types__has_slc-abil'].to_numpy()
                         == expected.to_numpy()).all())

    def test_empty_list_is_zero_everywhere(self):
        empty = self.tenders['cpv_additional'].map(len) == 0
        self.assertTrue(empty.any())
        cols = [c for c in self.nums if c.startswith('cpv_additional__')]
        self.assertTrue((self.X.loc[empty, cols] == 0).all().all())

    def test_subset_with_unseen_code_gets_identical_columns(self):
        # an "open lots" frame carrying a code the vocabulary never saw:
        # with the SAME vocabulary the columns are identical, the unseen code
        # lands in n_rare, and nothing new appears
        mh = sb.fit_multihot(self.tenders, ROLES)
        open_t = frame(20, self.rng, extra_codes=['99999999'])
        open_t.at[0, 'cpv_additional'] = ['99999999', '45210000']
        Xo, cats_o, nums_o, _ = sb.build_features(open_t, ROLES,
                                                  list_frame=self.tenders, multihot=mh)
        self.assertEqual(list(Xo.columns), list(self.X.columns))
        self.assertEqual(cats_o + nums_o, self.cats + self.nums)
        self.assertEqual(int(Xo.loc[0, 'cpv_additional__cpv4__n_rare']), 1)
        self.assertEqual(int(Xo.loc[0, 'cpv_additional__cpv4__has_4521']), 1)
        self.assertEqual(int(Xo.loc[0, 'cpv_additional__cpv4__n']), 2)

    def test_guard_passes_however_many_combinations_exist(self):
        # 2,000 lots with near-unique combinations — the case that refused every
        # candidate on the server — is no longer a categorical at all
        rng = np.random.default_rng(3)
        many = frame(2000, rng, extra_codes=[f'{45000000 + i * 1000:08d}' for i in range(300)])
        X, cats, _, _ = sb.build_features(many, ROLES, list_frame=many)
        card = sb.assert_pure_one_hot(X, cats)          # raises if any cat > 1024
        self.assertLessEqual(int(card.max()), sb.ONE_HOT_MAX_SIZE)


class TrainsAndScores(unittest.TestCase):

    def test_catboost_accepts_the_build_and_scores_open_lots(self):
        rng = np.random.default_rng(5)
        tenders = frame(300, rng)
        mh = sb.fit_multihot(tenders, ROLES)
        X, cats, nums, _ = sb.build_features(tenders, ROLES, list_frame=tenders, multihot=mh)
        y = (X['cpv_additional__cpv4__has_4521'].to_numpy() ^ rng.integers(0, 2, len(X)) > 0).astype(int)
        w = np.ones(len(X))
        model = sb.train(X, y, w, cats, iterations=20)
        open_t = frame(15, rng, extra_codes=['12345678'])
        Xo, _, _, _ = sb.build_features(open_t, ROLES, list_frame=tenders, multihot=mh)
        self.assertEqual(list(Xo.columns), list(model.feature_names_))
        p = sb.predict(model, Xo)
        self.assertEqual(len(p), 15)
        self.assertTrue(((p >= 0) & (p <= 1)).all())


class DateSpans(unittest.TestCase):
    def test_a_buyers_impossible_date_is_missing_not_a_failed_cycle(self):
        """2026-08-17: one notice carried a deadline in the year 3032 and the
        Monday cycle died in build_features on it — no delivery. A date pandas
        cannot hold is NaN in the span feature; the lot still scores."""
        rng = np.random.default_rng(3)
        t = frame(60, rng)
        t['deadline_date'] = '2026-02-01'
        t.loc[5, 'deadline_date'] = '3032-06-30'
        roles = dict(ROLES, deadline_date='date')
        X, cats, nums, _ = sb.build_features(t, roles, list_frame=t)
        self.assertIn('span__deadline_date', nums)
        self.assertTrue(np.isnan(X['span__deadline_date'].iloc[5]))
        self.assertEqual(X['span__deadline_date'].iloc[0], 31)


if __name__ == '__main__':
    unittest.main()
