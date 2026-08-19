"""The per-trade forecast section — doc/TRADE_PAGES.md, doc/METHODS.md 0.

This is the **forecast** precision/recall (did a flagged lot really end with
0-1 bids), never the gate's. It can only come from `rewind_all.py`'s as-of
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


def slice_of(rec, keys, base=None):
    """`base` is the trade's own 0/1 rate (the tile). None here means "the
    pool's own", which is what a test that is not about the denominator
    wants — production always passes the market's rate."""
    lots = frame(keys)
    sel = pd.Series([True] * len(lots))
    if base is None:
        from grading import flag_stats
        rows = [{'flag': r['flag'], 'label': int(r['n_tenders'] <= 1)}
                for r in rec['lots'] if r['n_tenders'] is not None
                and (r['procedure_id'], r['lot_id']) in set(keys)]
        base = flag_stats(rows)['base'] if rows else None
    return tp.forecast_for(rec, lots, sel, base)


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

    def test_the_conventional_file_on_the_state_volume_is_found(self):
        """2026-08-18: the deploy and the cycle build without flags, so the
        operator's replay reaches the pages through one conventional path,
        `<data>/replay/latest.json`. `--replay` still wins when given; a
        missing file is still no claim."""
        self.assertIsNone(tp.replay_path(self.dir))
        f = self.dir / tp.REPLAY_FILE
        f.parent.mkdir()
        f.write_text(json.dumps(receipt([('p', 'L', True, 1)])),
                     encoding='utf-8')
        self.assertEqual(tp.replay_path(self.dir), f)
        self.assertEqual(tp.replay_path(self.dir, 'other.json'), 'other.json')
        self.assertIsNotNone(tp.load_replay(tp.replay_path(self.dir)))

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
        from grading import flag_stats
        rec = receipt([('p1', 'L1', True, 1), ('p1', 'L2', False, 0)])
        st, _ = slice_of(rec, [('p1', 'L1'), ('p1', 'L2')])
        own = flag_stats([{'flag': True, 'label': 1},
                          {'flag': False, 'label': 1}])
        self.assertEqual({k: v for k, v in st.items() if k != 'pool_base'},
                         own)
        self.assertEqual(st['pool_base'], own['base'])


class TheDenominator(unittest.TestCase):
    """2026-08-19: the page showed "10 % of this trade end 0/1" in the tile
    and "7 % without our forecast" in the text — the 7 % was the replay
    pool's own rate (lots open a week or more at a cutoff), which is more
    contested than the trade. A reader who can divide sees a product
    scoring itself against a smaller number than the one it prints.
    One rate per trade: the tile's, `market.low_bid_rate`, and every claim
    is measured against it."""

    def _rec(self):
        # 40 alarms, 10 hit; the pool has 20 more lonely lots among 140
        # unflagged, so the pool's own rate is 30/180 = 17 %
        lots, i = [], 0
        for flag, n, count in ((True, 1, 10), (True, 9, 30),
                               (False, 0, 20), (False, 8, 120)):
            for _ in range(count):
                i += 1
                lots.append(('p', f'L{i}', flag, n))
        return receipt(lots), [(l[0], l[1]) for l in lots]

    def test_the_base_is_the_trades_rate_not_the_pools(self):
        rec, keys = self._rec()
        st, _ = slice_of(rec, keys, base=0.10)
        self.assertAlmostEqual(st['precision'], 0.25)
        self.assertAlmostEqual(st['base'], 0.10)
        self.assertAlmostEqual(st['pool_base'], 30 / 180)
        self.assertTrue(st['beats_base'])
        lv = tp.level((st, '2026-08-19'))
        self.assertEqual(lv['state'], 'beats')
        self.assertAlmostEqual(lv['factor'], 2.5)

    def test_a_pool_that_flatters_is_not_believed(self):
        """Heizung's shape: 10 % precision, 7 % in the pool, 10 % in the
        trade. Against the pool it reads 1,5-fach; against the trade it is
        not better than the average, and the page must say that."""
        rec, keys = self._rec()
        st, _ = slice_of(rec, keys, base=0.25)
        self.assertFalse(st['beats_base'])
        html = tp.forecast_section((st, '2026-08-19'))
        self.assertIn('nicht besser als der Durchschnitt', html)
        self.assertNotIn('-Fache', html)
        self.assertEqual(tp.level((st, '2026-08-19'))['state'], 'no_better')

    def test_the_page_names_the_tile_as_its_denominator(self):
        rec, keys = self._rec()
        st, _ = slice_of(rec, keys, base=0.10)
        html = tp.forecast_section((st, '2026-08-19'))
        self.assertIn('Im Gewerk insgesamt sind es 10 %', html)
        self.assertIn('die Kennzahl oben', html)
        self.assertIn('2,5-Fache der Quote des Gewerks', html)
        self.assertNotIn('Ohne jede Einschätzung', html)
        # recall stays the pool's, and says so
        self.assertIn('die wir damals prüfen konnten', html)
        self.assertNotIn('allen Losen dieses Gewerks', html)

    def test_a_lift_that_would_print_as_1_0_fach_is_no_advantage(self):
        """Heizung on the server, 2026-08-19: 4 of 39 (10,3 %) against a
        trade rate of 9,9 % — 'beats' by 0.4 points, and the tile would say
        „1,0-fach". The floor is the display's own: an advantage tile must
        show an advantage."""
        rec, keys = self._rec()                 # precision 0.25
        just_under = 0.25 / 1.04                # factor 1.04 -> '1,0-fach'
        st, _ = slice_of(rec, keys, base=just_under)
        self.assertTrue(st['beats_base'])       # strictly above, still...
        lv = tp.level((st, '2026-08-19'))
        self.assertEqual(lv['state'], 'no_better')
        self.assertEqual(tp.level_tile(lv), '')
        html = tp.forecast_section((st, '2026-08-19'))
        self.assertIn('nicht besser als der Durchschnitt', html)
        self.assertNotIn('-Fache', html)
        st, _ = slice_of(rec, keys, base=0.25 / 1.06)
        self.assertEqual(tp.level((st, '2026-08-19'))['state'], 'beats')
        self.assertIn('1,1-fach', tp.level_tile(tp.level((st, '2026-08-19'))))

    def test_the_overall_line_takes_the_store_wide_rate(self):
        rec, keys = self._rec()
        self.assertAlmostEqual(tp.forecast_all(rec, 0.09)[0]['base'], 0.09)
        self.assertAlmostEqual(tp.forecast_all(rec, 0.09)[0]['pool_base'],
                               30 / 180)
        html = tp.overall_html(tp.level(tp.forecast_all(rec, 0.09)))
        self.assertIn('über alle ausgewerteten Lose im Register sind es 9 %',
                      html)

    def test_market_low_bid_rate_is_the_tile(self):
        months = [pd.Period('2026-01', freq='M')]
        lots = pd.DataFrame({
            'month': months * 40,
            'resolved': [True] * 40,
            'n_tenders': [0] * 2 + [1] * 3 + [7] * 35,
            'award_value': [100_000.0] * 40,
            'result_code': ['selec-w'] * 40})
        import market
        rate, n = market.low_bid_rate(lots, months)
        self.assertAlmostEqual(rate, 5 / 40)
        self.assertEqual(n, 40)
        f = tp.figures(lots, pd.Series([True] * 40), months, months)
        self.assertAlmostEqual(f['low_bid'], rate)
        self.assertEqual(market.low_bid_rate(lots.iloc[:0], months), (None, 0))


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

    def test_the_level_is_a_tile_only_when_it_beats_chance(self):
        """2026-08-18: 'show the level when our prediction is better than
        guessing'. One verdict dict feeds the tile, the operator page and
        the message; a trade without an edge gets no tile."""
        lv = tp.level(self._stats(20, 20, 10, 150))
        self.assertEqual(lv['state'], 'beats')
        self.assertAlmostEqual(lv['factor'], 0.5 / 0.15, places=3)
        self.assertIn('3,3-fach', tp.level_tile(lv))
        self.assertIn('verglichen mit Zufall', tp.level_tile(lv))
        self.assertEqual(tp.level(self._stats(4, 36, 40, 20))['state'],
                         'no_better')
        self.assertEqual(tp.level_tile(tp.level(self._stats(4, 36, 40, 20))),
                         '')
        self.assertEqual(tp.level(self._stats(5, 5, 10, 80))['state'], 'thin')
        self.assertEqual(tp.level(None), {'state': 'none'})
        self.assertEqual(tp.level_tile(tp.level(None)), '')

    def test_the_overall_record_stands_under_every_state(self):
        """2026-08-18: a trade with too few checked alarms still belongs to
        a product with a measured record, so the all-trades line is printed
        under every state — and only when the overall itself beats chance."""
        all_fc = self._stats(180, 860, 380, 4570)   # 17 % vs 9 %, 1042 checked
        line = ('Über alle Gewerke zusammen: 1.040 unserer Hinweise konnten '
                'bisher gegen das veröffentlichte Ergebnis geprüft werden')
        for fc in (None, self._stats(5, 5, 10, 80), self._stats(20, 20, 10, 150),
                   self._stats(4, 36, 40, 20)):
            html = tp.forecast_section(fc, all_fc)
            self.assertIn(line, html)
            self.assertIn('-mal so oft', html)
            self.assertNotIn(line, tp.forecast_section(fc))
        weak = self._stats(4, 36, 40, 20)
        self.assertNotIn('Über alle Gewerke', tp.forecast_section(None, weak))
        rec = receipt([('p1', 'L1', True, 1), ('p1', 'L2', True, 9)] * 20
                      + [('p2', 'L1', False, 1)] * 5 + [('p2', 'L2', False, 9)] * 95)
        self.assertEqual(tp.forecast_all(rec, 0.09)[0]['flagged'], 40)
        self.assertIsNone(tp.forecast_all(None, 0.09))

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
