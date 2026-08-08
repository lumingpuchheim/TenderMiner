"""Market view tests — the promises market.py makes to a business developer.

    python -m unittest discover -t . -s tests     # from the repository root
    python tests/test_market.py                   # or directly

Same properties as test_storage.py: no real data (every test builds its own
store in a temporary directory), stdlib `unittest`, and behaviours rather than
implementations — what a caller is promised, so the matching or the storage can
be rewritten underneath them.

The assertions worth naming, because each is a number a person would otherwise
quote wrongly in a sales call:

* a gap month must not become a denominator (the store is downloaded in
  packages; a month that never landed is absent data, not a quiet market),
* a corrigendum must not re-date a lot into a different month,
* a withheld award sum (0.00) must not be averaged in as free work,
* a firm's two spellings must be flagged and must NOT be silently merged.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market


def lot(procedure='p1', lot_id='L1', version=1, pub='2026-01-05', title='x',
        description='', **over):
    row = {'procedure_id': procedure, 'lot_id': lot_id,
           'notice_version': version, 'publication_date': pub, 'title': title,
           'description': description, 'est_value_lot': None,
           'est_value_procedure': None, 'place_nuts3': 'DE211',
           'buyer_name': 'Stadt X', 'procedure_type': 'open',
           'is_framework': False, 'n_lots': 1, 'deadline_days': 30,
           'bid_bond_required': False, 'cpv_main': '45000000',
           'n_selection_criteria': 2, 'publication_number': '0001-2026'}
    row.update(over)
    return row


def award(procedure='p1', lot_id='L1', pub='2026-04-05', n_tenders=3,
          amount=100_000.0, winner='Acme GmbH', size='small', **over):
    row = {'procedure_id': procedure, 'lot_id': lot_id,
           'publication_date': pub, 'n_tenders': n_tenders,
           'result_code': 'selec-w', 'winner_names': [winner],
           'winner_size': size,
           'winning_bids': None if amount is None
                           else [{'amount': amount, 'currency': 'EUR'}]}
    row.update(over)
    return row


class Store(unittest.TestCase):
    """A throwaway two-table store per test."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        (self.home / 'store').mkdir()

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def write(self, lots, awards=()):
        pd.DataFrame(lots).to_parquet(self.home / 'store' / 'tenders.parquet')
        pd.DataFrame(list(awards) or [award(procedure='none', lot_id='none')]
                     ).to_parquet(self.home / 'store' / 'awards.parquet')
        return market.add_text(market.load_lots(self.home))


class TradeList(unittest.TestCase):

    def parse(self, text):
        p = Path(tempfile.mkdtemp()) / 'trades.txt'
        p.write_text(text, encoding='utf-8')
        self.addCleanup(shutil.rmtree, p.parent, True)
        return market.load_trades(p)

    def test_words_and_exclusions_are_separated_and_folded(self):
        t = self.parse('= Blitzschutz\nBlitzschutz\nErdungsanlage\n'
                       '-blitzleuchte\n# a comment\n')
        self.assertEqual(t['Blitzschutz']['terms'],
                         ['blitzschutz', 'erdungsanlage'])
        self.assertEqual(t['Blitzschutz']['exclude'], ['blitzleuchte'])

    def test_umlauts_fold_so_one_entry_covers_both_spellings(self):
        t = self.parse('= Türen\nTür-Element\n')
        self.assertEqual(t['Türen']['terms'], ['tuer-element'])

    def test_a_word_too_short_to_be_a_safe_substring_is_rejected(self):
        with self.assertRaises(SystemExit) as e:
            self.parse('= Bau\nbau\n')
        self.assertIn('under 5 characters', str(e.exception))

    def test_a_trade_with_no_words_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.parse('= Empty\n= Other\nestrich\n')

    def test_a_word_before_any_trade_header_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.parse('estrich\n= Estrich\nestrich\n')


class Resolve(unittest.TestCase):

    TRADES = {'Blitzschutz und Erdung': {'terms': ['blitzschutz'],
                                         'exclude': []},
              'Bodenbelagsarbeiten': {'terms': ['parkett'], 'exclude': []}}

    def test_a_partial_name_finds_the_one_trade_it_means(self):
        name, trade = market.resolve(self.TRADES, 'Blitzschutz')
        self.assertEqual(name, 'Blitzschutz und Erdung')
        self.assertEqual(trade['terms'], ['blitzschutz'])

    def test_an_ambiguous_name_refuses_rather_than_guessing(self):
        trades = dict(self.TRADES, **{'Blitzschutz Wartung':
                                      {'terms': ['wartung'], 'exclude': []}})
        with self.assertRaises(SystemExit):
            market.resolve(trades, 'Blitzschutz')

    def test_an_unknown_name_still_works_as_an_ad_hoc_word(self):
        name, trade = market.resolve(self.TRADES, 'Estrich')
        self.assertIn('ad-hoc', name)
        self.assertEqual(trade['terms'], ['estrich'])

    def test_an_unknown_name_too_short_to_match_safely_is_refused(self):
        with self.assertRaises(SystemExit):
            market.resolve(self.TRADES, 'Bau')


class AwardValue(unittest.TestCase):

    def test_winning_bids_are_summed(self):
        self.assertEqual(market.bid_sum([{'amount': 10.0, 'currency': 'EUR'},
                                         {'amount': 5.0, 'currency': 'EUR'}]),
                         15.0)

    def test_a_withheld_zero_is_missing_not_free_work(self):
        self.assertIsNone(market.bid_sum([{'amount': 0.0, 'currency': 'EUR'}]))

    def test_a_foreign_currency_is_dropped_rather_than_converted(self):
        self.assertIsNone(market.bid_sum([{'amount': 99.0, 'currency': 'CHF'}]))

    def test_no_bids_is_no_value(self):
        self.assertIsNone(market.bid_sum([]))
        self.assertIsNone(market.bid_sum(None))

    def test_a_four_figure_award_is_not_printed_as_zero(self):
        self.assertEqual(market.money(2500.0), '2.5 k')
        self.assertEqual(market.money(250.0), '250')


class Loading(Store):

    def test_one_row_per_lot_keeping_the_newest_version(self):
        lots = self.write([lot(version=1, title='old'),
                           lot(version=2, title='new')])
        self.assertEqual(len(lots), 1)
        self.assertEqual(lots.iloc[0].title, 'new')

    def test_a_corrigendum_does_not_re_date_the_lot(self):
        """The market event is when the work was first published, not when a
        clerk fixed a typo — otherwise a lot moves month and the monthly rate
        moves with it."""
        lots = self.write([lot(version=1, pub='2026-01-05'),
                           lot(version=2, pub='2026-03-20')])
        self.assertEqual(lots.iloc[0].publication_date, '2026-01-05')
        self.assertEqual(str(lots.iloc[0].month), '2026-01')

    def test_an_unawarded_lot_is_unresolved_not_zero_bidders(self):
        lots = self.write([lot()], [award(procedure='other', lot_id='X')])
        self.assertFalse(bool(lots.iloc[0].resolved))
        self.assertTrue(pd.isna(lots.iloc[0].n_tenders))


class Coverage(Store):

    def test_a_download_gap_is_not_a_quiet_market(self):
        """40 lots in January, 1 in February: February never downloaded. It
        must not become a denominator."""
        lots = self.write([lot(procedure=f'p{i}', pub='2026-01-05')
                           for i in range(40)]
                          + [lot(procedure='gap', pub='2026-02-05')])
        covered, _, _ = market.coverage(lots)
        self.assertEqual([str(m) for m in covered], ['2026-01'])

    def test_a_month_with_too_few_awards_cannot_carry_a_bidder_rate(self):
        lots = self.write(
            [lot(procedure=f'p{i}', pub='2026-01-05') for i in range(10)]
            + [lot(procedure=f'q{i}', pub='2026-02-05') for i in range(10)],
            [award(procedure=f'p{i}', lot_id='L1') for i in range(8)])
        covered, mature, _ = market.coverage(lots)
        self.assertEqual(len(covered), 2)
        self.assertEqual([str(m) for m in mature], ['2026-01'])


class Matching(Store):

    TRADE = {'terms': ['blitzschutz'], 'exclude': ['blitzleuchte']}

    def test_the_title_is_the_biddable_lot_the_body_is_a_subcontract_lead(self):
        lots = self.write([
            lot(procedure='a', title='Blitzschutzarbeiten VE 4402'),
            lot(procedure='b', title='Neubau Schule',
                description='Leistungsumfang: Blitzschutz und Erdung'),
            lot(procedure='c', title='Malerarbeiten')])
        core = market.match(lots, self.TRADE, 'core')
        ment = market.match(lots, self.TRADE, 'mentioned')
        self.assertEqual(list(lots[core].procedure_id), ['a'])
        self.assertEqual(list(lots[ment].procedure_id), ['b'])
        self.assertEqual(sorted(lots[market.match(lots, self.TRADE, 'both')]
                                .procedure_id), ['a', 'b'])

    def test_a_compound_matches_without_an_entry_of_its_own(self):
        lots = self.write([lot(title='Erneuerung der Blitzschutzanlage')])
        self.assertTrue(market.match(lots, self.TRADE, 'core').iloc[0])

    def test_umlaut_spellings_match_each_other(self):
        lots = self.write([lot(procedure='a', title='Türelemente'),
                           lot(procedure='b', title='Tuerelemente')])
        core = market.match(lots, {'terms': ['tuerelement'], 'exclude': []},
                            'core')
        self.assertEqual(sorted(lots[core].procedure_id), ['a', 'b'])

    def test_an_exclusion_disqualifies_the_lot_even_when_a_word_matched(self):
        lots = self.write([lot(title='Blitzschutz und Blitzleuchte')])
        self.assertFalse(market.match(lots, self.TRADE, 'core').iloc[0])


class Firms(Store):

    def build(self):
        lots = self.write(
            [lot(procedure='a', title='Blitzschutz', place_nuts3='DE211'),
             lot(procedure='b', title='Blitzschutz', place_nuts3='DEA12'),
             lot(procedure='c', title='Blitzschutz', place_nuts3='DE211')],
            [award(procedure='a', n_tenders=1, winner='Acme GmbH'),
             award(procedure='b', n_tenders=6, winner='Acme GmbH'),
             award(procedure='c', n_tenders=4, winner='Bosch KG')])
        sel = market.match(lots, {'terms': ['blitzschutz'], 'exclude': []},
                           'core')
        return market.firm_rows(lots, sel)[0]

    def test_wins_and_uncontested_wins_are_counted_per_firm(self):
        acme = next(r for r in self.build() if r['firm'] == 'Acme GmbH')
        self.assertEqual(acme['wins'], 2)
        self.assertEqual(acme['low'], 1)

    def test_regions_are_reported_against_the_regions_the_trade_tenders_in(self):
        rows = {r['firm']: r for r in self.build()}
        self.assertEqual(rows['Acme GmbH']['regions'], '2/2')
        self.assertEqual(rows['Bosch KG']['regions'], '1/2')

    def test_two_spellings_are_flagged_and_not_merged(self):
        rows = [{'firm': 'NDB Elektrotechnik GmbH & Co. KG', 'wins': 1,
                 'flag': ''},
                {'firm': 'NDB ELEKTROTECHNIK GmbH, NL Berlin', 'wins': 2,
                 'flag': ''},
                {'firm': 'Hans Hund GmbH', 'wins': 5, 'flag': ''}]
        rows, groups = market.alias_groups(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(sum(r['wins'] for r in groups[0]), 3)
        self.assertEqual([r['wins'] for r in rows], [1, 2, 5],
                         'the rows themselves must stay unmerged')
        self.assertEqual(rows[2]['flag'], '')


class Rates(unittest.TestCase):

    def test_a_share_below_the_sample_bar_says_so(self):
        sub = pd.DataFrame({'resolved': [True] * 3, 'n_tenders': [1, 1, 5]})
        line = market.low_bid_line(sub, 'x')
        self.assertIn('67% of 3', line)
        self.assertIn('indicative', line)

    def test_a_share_always_carries_its_denominator(self):
        sub = pd.DataFrame({'resolved': [True] * 40,
                            'n_tenders': [1] * 4 + [5] * 36})
        self.assertIn('10% of 40', market.low_bid_line(sub, 'x'))

    def test_no_award_yet_is_not_a_zero_percent_rate(self):
        sub = pd.DataFrame({'resolved': [False], 'n_tenders': [None]})
        self.assertIn('no awarded lot yet', market.low_bid_line(sub, 'x'))

    def test_a_small_share_keeps_a_decimal(self):
        self.assertEqual(market.pct(1, 100), '1.0%')
        self.assertEqual(market.pct(30, 100), '30%')


class CommittedTradeList(unittest.TestCase):
    """The shipped trades.txt must parse and stay reviewable."""

    def test_it_parses(self):
        trades = market.load_trades()
        self.assertGreater(len(trades), 20)

    def test_every_word_is_a_safe_substring(self):
        for name, trade in market.load_trades().items():
            for word in trade['terms'] + trade['exclude']:
                self.assertGreaterEqual(len(word), market.MIN_TERM_LEN,
                                        f'{name}: {word}')

    def test_no_word_is_claimed_by_two_trades(self):
        """The same word in two trades makes both counts wrong and neither
        obviously so. Lots may overlap; words may not."""
        seen = {}
        for name, trade in market.load_trades().items():
            for word in trade['terms']:
                self.assertNotIn(word, seen,
                                 f'"{word}" is in both {seen.get(word)} '
                                 f'and {name}')
                seen[word] = name


if __name__ == '__main__':
    unittest.main(verbosity=2)
