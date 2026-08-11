"""The generated trade pages — doc/TRADE_PAGES.md.

Two kinds of test here. The pure ones (formatting, slugs, the floor) run on
synthetic frames and need nothing. The others read the **committed output** in
`site/gewerke/`, which is the thing that actually gets uploaded — a generator
that is correct and output that is stale would still serve a wrong page.

Nothing here loads the real store: building 32 pages takes ~40 s and a test
suite that slow is a test suite nobody runs.
"""

import re
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import trade_pages as tp                                        # noqa: E402

GEWERKE = Path(__file__).resolve().parent.parent / 'site' / 'gewerke'


class MoneyGerman(unittest.TestCase):
    """`market.money` is the console's terse form ('204 k', '34.08 M'). A
    German page needs '.' for thousands and ',' for decimals — and the naive
    two-pass swap turns the thousands dot into a comma, which is how
    '34,1 Mio.' first came out as '34,1 Mio,'."""

    def test_thousands_and_decimals(self):
        self.assertEqual(tp.money_de(204_000), '204.000')
        self.assertEqual(tp.money_de(34_080_000), '34,1 Mio.')
        self.assertEqual(tp.money_de(1_500_000), '1,5 Mio.')
        self.assertEqual(tp.money_de(1_250_000_000), '1,25 Mrd.')
        self.assertEqual(tp.money_de(999), '999')

    def test_missing_is_a_dash_not_a_zero(self):
        self.assertEqual(tp.money_de(None), '—')


class Slugs(unittest.TestCase):
    def test_umlauts_and_punctuation(self):
        self.assertEqual(tp.slugify('Maler- und Lackierarbeiten'),
                         'maler-und-lackierarbeiten')
        self.assertEqual(tp.slugify('Lüftung, Klima und Kälte'),
                         'lueftung-klima-und-kaelte')
        self.assertEqual(tp.slugify('Brandschutz (baulich)'),
                         'brandschutz-baulich')
        self.assertEqual(tp.slugify('Klempner, Spengler, Flaschner'),
                         'klempner-spengler-flaschner')


def frame(n_awarded, low_bid=5):
    """A minimal lots frame: `n_awarded` resolved lots in one mature month."""
    return pd.DataFrame({
        'month': [pd.Period('2026-01', freq='M')] * n_awarded,
        'resolved': [True] * n_awarded,
        'n_tenders': [1] * low_bid + [7] * (n_awarded - low_bid),
        'award_value': [100_000.0] * n_awarded,
        'result_code': ['selec-w'] * n_awarded,
    })


class TheFloor(unittest.TestCase):
    """doc/TRADE_PAGES.md 3: a page whose headline figure cannot honestly be
    quoted should not exist. The floor is market.py's own SMALL_SAMPLE, not a
    number invented for the website."""

    def setUp(self):
        self.months = [pd.Period('2026-01', freq='M')]

    def test_below_the_floor_gets_no_page(self):
        lots = frame(tp.MIN_AWARDED - 1)
        sel = pd.Series([True] * len(lots))
        self.assertIsNone(tp.figures(lots, sel, self.months, self.months))

    def test_at_the_floor_gets_a_page(self):
        lots = frame(tp.MIN_AWARDED)
        sel = pd.Series([True] * len(lots))
        self.assertIsNotNone(tp.figures(lots, sel, self.months, self.months))

    def test_the_floor_is_market_pys_own_line(self):
        import market
        self.assertEqual(tp.MIN_AWARDED, market.SMALL_SAMPLE)

    def test_the_share_matches_its_denominator(self):
        lots = frame(40, low_bid=10)
        sel = pd.Series([True] * len(lots))
        f = tp.figures(lots, sel, self.months, self.months)
        self.assertEqual(f['n_awarded'], 40)
        self.assertEqual(f['one'], 10)
        self.assertAlmostEqual(f['low_bid'], 0.25)


@unittest.skipUnless(GEWERKE.exists(), 'trade pages not generated')
class GeneratedOutput(unittest.TestCase):
    """The committed pages — what actually gets uploaded."""

    def setUp(self):
        self.pages = sorted(p for p in GEWERKE.glob('*/index.html'))

    def test_there_are_pages(self):
        self.assertGreater(len(self.pages), 20)

    def test_no_trade_page_links_to_another_trade_page(self):
        """The rule the whole design exists to protect: a Maler is never shown
        anything about Elektro."""
        for p in self.pages:
            for href in re.findall(r'href="([^"]+)"',
                                   p.read_text(encoding='utf-8')):
                self.assertNotRegex(
                    href, r'^\.\./(?!index\.html)[a-z]',
                    f'{p.parent.name} links sideways to {href}')

    def test_every_page_links_up_to_the_index_and_home(self):
        for p in self.pages:
            html = p.read_text(encoding='utf-8')
            self.assertIn('href="../index.html"', html, p.parent.name)
            self.assertIn('href="../../index.html"', html, p.parent.name)

    def test_every_page_states_its_denominator_and_date(self):
        """A share without the number it was computed over is a claim, not a
        figure — and a market page with no date silently ages into a lie."""
        for p in self.pages:
            html = p.read_text(encoding='utf-8')
            self.assertRegex(html, r'Von \d+ ausgewerteten Losen',
                             p.parent.name)
            self.assertIn('Stand:', html)
            self.assertIn('vollständig erfasste Monate', html)

    def test_every_page_names_its_source_and_the_award_lag(self):
        for p in self.pages:
            html = p.read_text(encoding='utf-8')
            self.assertIn('Tenders Electronic Daily', html)
            self.assertIn('drei Monate nach', html)

    def test_no_forecast_language(self):
        """LAUNCH.md 4.1 claim rules: 452 is the only trade with measured
        lift, so the forecast stays off these pages entirely."""
        for p in self.pages:
            html = p.read_text(encoding='utf-8')
            for word in ('voraussichtlich', 'Prognose', 'Kandidat',
                         'Trefferquote'):
                self.assertNotIn(word, html, f'{p.parent.name}: {word}')

    def test_no_firm_names_and_no_lot_listings(self):
        for p in self.pages:
            html = p.read_text(encoding='utf-8')
            self.assertNotIn('GmbH', html, p.parent.name)
            self.assertNotIn('<li>', html, p.parent.name)

    def test_every_page_carries_the_same_cta(self):
        for p in self.pages:
            self.assertIn('was Sie in den letzten Jahren gewonnen haben',
                          p.read_text(encoding='utf-8'), p.parent.name)

    def test_index_lists_every_page_and_nothing_else(self):
        index = (GEWERKE / 'index.html').read_text(encoding='utf-8')
        linked = set(re.findall(r'href="([a-z0-9-]+)/index\.html"', index))
        self.assertEqual(linked, {p.parent.name for p in self.pages})

    def test_sitemap_covers_every_page(self):
        xml = (GEWERKE.parent / 'sitemap.xml').read_text(encoding='utf-8')
        for p in self.pages:
            self.assertIn(f'/gewerke/{p.parent.name}/', xml)

    def test_money_is_german_not_console_format(self):
        for p in self.pages:
            html = p.read_text(encoding='utf-8')
            self.assertNotRegex(html, r'\d+ [kM] €', p.parent.name)


if __name__ == '__main__':
    unittest.main(verbosity=2)
