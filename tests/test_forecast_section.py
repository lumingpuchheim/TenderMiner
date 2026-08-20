"""The per-trade forecast section — doc/TRADE_PAGES.md, doc/METHODS.md 0.

Since 2026-08-20 the section describes the RANKING (the product: a ranked
shortlist), not the flag at 0.5: of the trade's graded lots, how often did
the top fifth (grading.TOP_SHARE, frozen) end with 0-1 bids, against the
trade's own rate — the tile — plus the sorting check (AUC) in plain words.
These tests build synthetic schema-3 documents; a real replay retrains the
model at every weekly cutoff and takes hours.

The unflattering state still matters most: a trade where the top fifth does
NOT beat the trade's rate must say so on the page (operator's call,
2026-08-11), because silent omission is the version a reader cannot audit.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import trade_pages as tp                                        # noqa: E402
from grading import TOP_SHARE, score_stats                      # noqa: E402


def receipt(lots, generated='2026-08-19'):
    """lots: [(procedure_id, lot_id, score, n_tenders)] — n_tenders None
    means examined while open but no award published yet."""
    return {'schema': 3, 'generated': generated, 'model_tag': 'test',
            'step_days': 7, 'cutoffs_trained': 3, 'n_lots': len(lots),
            'lots': [{'procedure_id': p, 'lot_id': l, 'cpv3': '452',
                      'flag': s >= 0.5, 'score': s, 'week': None,
                      'first_week': '2026-01-05', 'times_scored': 1,
                      'n_tenders': n} for p, l, s, n in lots],
            'subs': []}


def frame(keys):
    return pd.DataFrame({'procedure_id': [k[0] for k in keys],
                         'lot_id': [k[1] for k in keys]})


def slice_of(rec, keys, base):
    lots = frame(keys)
    return tp.rank_stats(rec, base, lots=lots,
                         sel=pd.Series([True] * len(lots)))


def graded(n, top_hits, rest_hits):
    """n lots with strictly descending scores; `top_hits` of the top fifth
    lonely, `rest_hits` of the rest lonely. -> (rows, keys)"""
    k = max(1, round(n * TOP_SHARE))
    lots = []
    for i in range(n):
        in_top = i < k
        hit = (i < top_hits) if in_top else (i - k < rest_hits)
        lots.append(('p', f'L{i}', 1.0 - i / n, 1 if hit else 9))
    return receipt(lots), [(l[0], l[1]) for l in lots]


class NoReceipt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_no_replay_given_is_no_claim(self):
        self.assertIsNone(tp.load_replay(None))
        html = tp.forecast_section(None)
        self.assertIn('behaupten wir dazu nichts', html)
        self.assertNotIn('%', html)

    def test_the_conventional_file_on_the_state_volume_is_found(self):
        self.assertIsNone(tp.replay_path(self.dir))
        f = self.dir / tp.REPLAY_FILE
        f.parent.mkdir()
        f.write_text(json.dumps(receipt([('p', 'L', 0.9, 1)])),
                     encoding='utf-8')
        self.assertEqual(tp.replay_path(self.dir), f)
        self.assertEqual(tp.replay_path(self.dir, 'other.json'), 'other.json')
        self.assertIsNotNone(tp.load_replay(tp.replay_path(self.dir)))

    def test_a_missing_file_is_treated_as_absent(self):
        self.assertIsNone(tp.load_replay(self.dir / 'nope.json'))

    def test_a_corrupt_document_is_treated_as_absent(self):
        p = self.dir / 'run.json'
        p.write_text('{ not json', encoding='utf-8')
        self.assertIsNone(tp.load_replay(p))

    def test_a_schema2_document_without_scores_makes_no_claim(self):
        """The documents from before 2026-08-19 carry flags but no scores.
        They must not crash the build — and must not be quoted either."""
        rec = receipt([('p1', 'L1', 0.9, 1)])
        for r in rec['lots']:
            del r['score']
        self.assertIsNone(slice_of(rec, [('p1', 'L1')], base=0.10))

    def test_an_unawarded_lot_is_neither_hit_nor_miss(self):
        rec = receipt([('p1', 'L1', 0.9, 1), ('p1', 'L2', 0.8, None)])
        st, _ = slice_of(rec, [('p1', 'L1'), ('p1', 'L2')], base=0.10)
        self.assertEqual(st['n'], 1)


class Slicing(unittest.TestCase):
    def test_only_this_trades_lots_are_counted(self):
        rec = receipt([('p1', 'L1', 0.9, 1), ('p2', 'L2', 0.8, 9)])
        st, _ = slice_of(rec, [('p1', 'L1')], base=0.10)
        self.assertEqual(st['n'], 1)
        self.assertEqual(st['hits'], 1)

    def test_a_trade_with_no_replayed_lots_is_none(self):
        rec = receipt([('p1', 'L1', 0.9, 1)])
        self.assertIsNone(slice_of(rec, [('other', 'L9')], base=0.10))

    def test_only_mature_months_are_graded(self):
        """2026-08-19: awards published within 60 days are 33 % lonely,
        later ones 7 % — so grading fresh lots flatters the forecast. The
        slice takes only lots whose month is in the page's mature list."""
        rec = receipt([('p1', 'L1', 0.9, 1), ('p1', 'L2', 0.8, 1)])
        lots = frame([('p1', 'L1'), ('p1', 'L2')])
        lots['month'] = pd.PeriodIndex(['2026-01', '2026-07'], freq='M')
        st, _ = tp.rank_stats(rec, 0.10, lots=lots,
                              sel=pd.Series([True, True]),
                              months=[pd.Period('2026-01', freq='M')])
        self.assertEqual(st['n'], 1)

    def test_the_statistic_is_gradings_own(self):
        """Shared with the weekly report's rank view deliberately: the
        replayed number is the one quoted until live awards accumulate, so
        it must be the same statistic, not a second implementation."""
        rec = receipt([('p1', 'L1', 0.9, 1), ('p1', 'L2', 0.4, 0)])
        st, _ = slice_of(rec, [('p1', 'L1'), ('p1', 'L2')], base=0.10)
        own = score_stats([{'score': 0.9, 'label': 1},
                           {'score': 0.4, 'label': 1}])
        for key in ('n', 'k', 'hit', 'hits', 'auc', 'share'):
            self.assertEqual(st[key], own[key])
        self.assertEqual(st['pool_base'], own['base'])


class ThreeStates(unittest.TestCase):
    def test_too_few_checked_lots_refuses_to_quote(self):
        """A top fifth of a handful is noise: rank three lots, get one
        right, claim 33 %. Below MIN_CHECKED lots in the top fifth the page
        names the count and refuses the rate."""
        rec, keys = graded(100, top_hits=10, rest_hits=5)   # top fifth = 20
        fc = slice_of(rec, keys, base=0.10)
        html = tp.forecast_section(fc)
        self.assertIn('zu wenige für eine belastbare Quote', html)
        self.assertIn(str(tp.MIN_CHECKED), html)
        self.assertNotIn('-Fache', html)
        self.assertEqual(tp.level(fc)['state'], 'thin')

    def test_a_ranking_that_beats_the_trade_is_quoted_with_the_tile(self):
        rec, keys = graded(200, top_hits=10, rest_hits=6)   # top 40: 25 %
        fc = slice_of(rec, keys, base=0.10)
        html = tp.forecast_section(fc)
        self.assertIn('200 Lose dieses Gewerks', html)
        self.assertIn('obersten Fünftel', html)
        self.assertIn('10 mit höchstens einem Angebot (25 %)', html)
        self.assertIn('Im Gewerk insgesamt sind es 10 %', html)
        self.assertIn('die Kennzahl oben', html)
        self.assertIn('2,5-Fache der Quote des Gewerks', html)
        self.assertIn('Rücktest', html)
        self.assertIn('2026-08-19', html)

    def test_the_sorting_check_is_stated_in_plain_words(self):
        """The operator asked for the AUC on the page (2026-08-20). It is
        printed as a sentence a contractor can check — pairs, not jargon —
        with the term named once."""
        rec, keys = graded(200, top_hits=10, rest_hits=6)
        fc = slice_of(rec, keys, base=0.10)
        st, _ = fc
        html = tp.forecast_section(fc)
        self.assertIn('von 100 Fällen weiter oben', html)
        self.assertIn('AUC', html)
        self.assertIn(f'{100 * st["auc"]:.0f} von 100', html)

    def test_german_number_formatting(self):
        rec, keys = graded(200, top_hits=10, rest_hits=6)
        html = tp.forecast_section(slice_of(rec, keys, base=0.10))
        self.assertIn('25 %', html)
        self.assertNotIn('25%', html)
        self.assertRegex(html, r'\d,\d-Fache')
        self.assertNotRegex(html, r'\d\.\d-Fache')

    def test_the_level_is_a_tile_only_when_it_beats_chance(self):
        rec, keys = graded(200, top_hits=10, rest_hits=6)
        lv = tp.level(slice_of(rec, keys, base=0.10))
        self.assertEqual(lv['state'], 'beats')
        self.assertAlmostEqual(lv['factor'], 2.5)
        self.assertIn('2,5-fach', tp.level_tile(lv))
        self.assertIn('verglichen mit Zufall', tp.level_tile(lv))
        weak = slice_of(rec, keys, base=0.30)      # trade rate above our 25 %
        self.assertEqual(tp.level(weak)['state'], 'no_better')
        self.assertEqual(tp.level_tile(tp.level(weak)), '')
        self.assertEqual(tp.level(None), {'state': 'none'})
        self.assertEqual(tp.level_tile(tp.level(None)), '')

    def test_a_lift_that_would_print_as_1_0_fach_is_no_advantage(self):
        """The display floor (MIN_FACTOR): an advantage tile must show an
        advantage, not „1,0-fach"."""
        rec, keys = graded(200, top_hits=10, rest_hits=6)   # top fifth 25 %
        st, _ = slice_of(rec, keys, base=0.25 / 1.04)
        self.assertTrue(st['beats_base'])          # strictly above, still...
        lv = tp.level((st, '2026-08-19'))
        self.assertEqual(lv['state'], 'no_better')
        html = tp.forecast_section((st, '2026-08-19'))
        self.assertIn('nicht besser als der Durchschnitt', html)
        self.assertNotIn('-Fache', html)
        st2, _ = slice_of(rec, keys, base=0.25 / 1.06)
        self.assertEqual(tp.level((st2, '2026-08-19'))['state'], 'beats')

    def test_a_ranking_that_does_not_beat_the_trade_says_so(self):
        rec, keys = graded(200, top_hits=4, rest_hits=30)
        fc = slice_of(rec, keys, base=0.20)
        html = tp.forecast_section(fc)
        self.assertIn('nicht besser als der Durchschnitt', html)
        self.assertIn('vollständigen Übersicht', html)
        self.assertNotIn('-Fache', html)
        # the AUC is still printed: the whole picture, not only when it wins
        self.assertIn('AUC', html)

    def test_recall_is_always_stated_when_quotable(self):
        rec, keys = graded(200, top_hits=10, rest_hits=10)
        html = tp.forecast_section(slice_of(rec, keys, base=0.05))
        self.assertIn('wir finden nicht alle', html)
        self.assertIn('50 %', html)               # 10 of 20 lonely in the top

    def test_the_overall_record_stands_under_every_state(self):
        rec, keys = graded(1000, top_hits=40, rest_hits=50)  # top 200: 20 %
        all_fc = tp.rank_stats(rec, 0.09, lots=frame(keys))
        line = ('Über alle Gewerke zusammen: 1.000 Lose konnten bisher '
                'gegen das veröffentlichte Ergebnis geprüft werden')
        thin_rec, thin_keys = graded(100, top_hits=10, rest_hits=5)
        for fc in (None, slice_of(thin_rec, thin_keys, base=0.10)):
            html = tp.forecast_section(fc, all_fc)
            self.assertIn(line, html)
            self.assertIn('obersten Fünftel', html)
            self.assertIn('-mal so oft', html)
            self.assertNotIn(line, tp.forecast_section(fc))
        # an overall that does not beat prints nothing
        weak_all = tp.rank_stats(rec, 0.30, lots=frame(keys))
        self.assertNotIn('Über alle Gewerke', tp.forecast_section(None, weak_all))


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
        self.assertLess(html.index('Wie gut trifft'),
                        html.index('Woher die Zahlen kommen'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
