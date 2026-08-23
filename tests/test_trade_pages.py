"""The generated trade pages — doc/TRADE_PAGES.md.

Everything here runs on synthetic frames — nothing loads the real store, which
would cost ~40 s and 2 GB for a suite that has to stay quick enough to run.
The pages under test are produced by `page()` and `index_page()`, the same
functions the cycle calls, into a temporary directory.

The output itself is deliberately not committed and not read from disk: it is
built into `<data-dir>/public/` (doc/TRADE_PAGES.md 6), which in the container
is the mounted volume rather than the read-only image.
"""

import re
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import trade_pages as tp                                        # noqa: E402

SITE = Path(__file__).resolve().parent.parent / 'site'


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


class FirmsOnPages(unittest.TestCase):
    """`trades_of_titles`: which page a firm belongs to, from the titles of
    its reference wins — the same title match that puts a lot on the page,
    so the operator page and the message quote the right page's figures."""

    def test_half_the_titles_must_carry_the_trade(self):
        t = tp.trades_of_titles(['Blitzschutzarbeiten Schule Nord',
                                 'Blitzschutz und Erdung Rathaus',
                                 'Malerarbeiten Turnhalle'])
        self.assertEqual(t[0][0], 'Blitzschutz und Erdung')
        self.assertEqual(t[0][1], 2)
        self.assertNotIn('Maler- und Lackierarbeiten', [n for n, _ in t])
        self.assertEqual(tp.trades_of_titles([]), [])
        self.assertEqual(tp.trades_of_titles(['Neubau Kita, Los 3']), [])

    def test_the_words_are_matched_folded_like_the_pages(self):
        t = tp.trades_of_titles(['Fußbodenbelagsarbeiten Bauteil A'])
        self.assertIn('Bodenbelagsarbeiten', [n for n, _ in t])


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


class GeneratedOutput(unittest.TestCase):
    """The generated pages.

    Built here from synthetic figures rather than read off disk: the output is
    no longer committed (it goes to `<data-dir>/public/`, doc/TRADE_PAGES.md
    6), and building it for real needs the 2 GB store and ~40 s — a suite that
    slow is a suite nobody runs. `page()` is the same function the cycle
    calls, so these assertions hold for the real pages too."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        out = Path(self.tmp.name)
        months = [pd.Period('2026-01', freq='M')]
        self.pages = []
        for name, n, low in (('Maler- und Lackierarbeiten', 60, 2),
                             ('Lüftung, Klima und Kälte', 40, 12),
                             ('Brandschutz (baulich)', 30, 30)):
            lots = frame(n, low_bid=low)
            f = tp.figures(lots, pd.Series([True] * len(lots)), months, months)
            slug = tp.slugify(name)
            d = out / 'gewerke' / slug
            d.mkdir(parents=True)
            (d / 'index.html').write_text(tp.page(name, slug, f),
                                          encoding='utf-8')
            self.pages.append(d / 'index.html')
        (out / 'gewerke' / 'index.html').write_text(
            tp.index_page([(n, tp.slugify(n)) for n in
                           ('Maler- und Lackierarbeiten',
                            'Lüftung, Klima und Kälte',
                            'Brandschutz (baulich)')]), encoding='utf-8')
        (out / 'sitemap.xml').write_text(
            tp.sitemap([(n, tp.slugify(n)) for n in
                        ('Maler- und Lackierarbeiten',
                         'Lüftung, Klima und Kälte',
                         'Brandschutz (baulich)')]), encoding='utf-8')
        self.out = out

    def test_there_are_pages(self):
        self.assertEqual(len(self.pages), 3)

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
            # the table caption is the one place the denominator lives now;
            # the prose sentences that repeated it were dropped 2026-08-15
            self.assertRegex(html, r'über \d+ ausgewertete Lose',
                             p.parent.name)
            self.assertIn('Stand:', html)
            self.assertIn('vollständig erfasste Monate', html)

    def test_the_distribution_uses_the_stylesheets_own_classes(self):
        """The first version emitted `.tiles/.tile`, which the stylesheet does
        not define — so four numbers rendered as bare divs running together
        into one unreadable line. Class names are a correctness property here,
        not a cosmetic one."""
        css = (SITE / 'style.css').read_text(encoding='utf-8')
        for cls in ('.figs', '.fig', 'table.dist', '.bar', '.barcell'):
            self.assertIn(cls, css)
        for p in self.pages:
            html = p.read_text(encoding='utf-8')
            self.assertIn('class="figs"', html, p.parent.name)
            self.assertIn('class="dist"', html, p.parent.name)
            self.assertNotIn('class="tile', html, p.parent.name)

    def test_the_distribution_shares_add_up(self):
        """Five buckets over the same denominator: if they do not sum to
        100 %, a lot is being double-counted or dropped."""
        for p in self.pages:
            html = p.read_text(encoding='utf-8')
            table = re.search(r'<table class="dist".*?</table>', html, re.S)
            shares = [int(x) for x in
                      re.findall(r'<td class="num">(\d+) %</td>', table.group(0))]
            self.assertEqual(len(shares), 5, p.parent.name)
            self.assertAlmostEqual(sum(shares), 100, delta=3,
                                   msg=f'{p.parent.name}: {shares}')

    def test_the_bars_are_scaled_to_the_biggest_bucket(self):
        """Scaled to 100 % of the total instead, a thin market's shape
        flattens into nothing — so the largest bucket must be full width.
        At least one, not exactly one: two buckets can genuinely tie, and
        two trades currently do."""
        for p in self.pages:
            widths = [float(w) for w in
                      re.findall(r'class="bar" style="width:([\d.]+)%"',
                                 p.read_text(encoding='utf-8'))]
            self.assertGreaterEqual(sum(1 for w in widths if w == 100.0), 1,
                                    p.parent.name)
            self.assertLessEqual(max(widths), 100.0, p.parent.name)

    def test_an_empty_bucket_draws_no_bar(self):
        """The stylesheet's min-width would draw a 2px stub for 0 %, which
        reads as 'a few' rather than 'none'."""
        for p in self.pages:
            html = p.read_text(encoding='utf-8')
            table = re.search(r'<table class="dist".*?</table>', html, re.S)
            for row in re.findall(r'<tr>.*?</tr>', table.group(0), re.S):
                if '<td class="num">0</td>' in row:
                    self.assertNotIn('class="bar"', row, p.parent.name)

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

    def test_the_brand_is_murara_not_the_repo_name(self):
        """Murara is what a customer sees; TenderMining is the repository."""
        for p in self.pages:
            html = p.read_text(encoding='utf-8')
            self.assertIn('Murara', html, p.parent.name)
            self.assertNotIn('TenderMining', html, p.parent.name)
            self.assertIn('murara.eu', html, p.parent.name)

    def test_every_page_carries_the_same_cta(self):
        for p in self.pages:
            self.assertIn('was Sie in den letzten Jahren gewonnen haben',
                          p.read_text(encoding='utf-8'), p.parent.name)

    def test_index_lists_every_page_and_nothing_else(self):
        index = (self.out / 'gewerke' / 'index.html').read_text(encoding='utf-8')
        linked = set(re.findall(r'href="([a-z0-9-]+)/index\.html"', index))
        self.assertEqual(linked, {p.parent.name for p in self.pages})

    def test_index_groups_the_list_under_headings_in_file_order(self):
        built = [(n, tp.slugify(n)) for n in
                 ('Estricharbeiten', 'IT-Sicherheit', 'Maurerarbeiten')]
        groups = {'Maurerarbeiten': 'Bau und Ausbau',
                  'Estricharbeiten': 'Bau und Ausbau',
                  'IT-Sicherheit': 'IT und Software'}
        html = tp.index_page(built, groups)
        self.assertLess(html.index('Bau und Ausbau'),
                        html.index('IT und Software'))
        self.assertLess(html.index('IT und Software'),
                        html.index('it-sicherheit/index.html'))
        # a heading may not swallow a trade: both lists are still complete
        for _, slug in built:
            self.assertIn(f'{slug}/index.html', html)
        self.assertEqual(html.count('<ul class="plain">'), 2)

    def test_index_without_groups_is_one_plain_list(self):
        built = [(n, tp.slugify(n)) for n in ('Estricharbeiten', 'Maurerarbeiten')]
        self.assertEqual(tp.index_page(built), tp.index_page(built, {}))
        self.assertEqual(tp.index_page(built).count('<ul class="plain">'), 1)
        self.assertNotIn('<h2>', tp.index_page(built))

    def test_a_trade_with_no_group_is_listed_last_not_dropped(self):
        built = [(n, tp.slugify(n)) for n in ('Estricharbeiten', 'IT-Sicherheit')]
        html = tp.index_page(built, {'IT-Sicherheit': 'IT und Software'})
        self.assertIn('estricharbeiten/index.html', html)
        self.assertLess(html.index('it-sicherheit/index.html'),
                        html.index('estricharbeiten/index.html'))

    def test_sitemap_covers_every_page(self):
        xml = (self.out / 'sitemap.xml').read_text(encoding='utf-8')
        for p in self.pages:
            self.assertIn(f'/gewerke/{p.parent.name}/', xml)

    def test_money_is_german_not_console_format(self):
        for p in self.pages:
            html = p.read_text(encoding='utf-8')
            self.assertNotRegex(html, r'\d+ [kM] €', p.parent.name)


class Release(unittest.TestCase):
    """`release`: the served site is `public/current`, a link to the one
    complete build beside it. Never partial, never empty, nothing kept
    (operator, 2026-08-15). The edge bind-mounts `public/` itself, so
    `public/` is never deleted or recreated — the old `rmtree(out)` left the
    container serving a directory that no longer existed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.public = Path(self.tmp.name) / 'public'
        # a link is what the edge follows; a laptop without the privilege
        # gets a junction, which is not a symlink — but on any host where the
        # first release cannot make a link at all, none of this can be tested
        try:
            tp.release(self.public, lambda d: (d / 'index.html').write_text('0'))
        except OSError as e:                     # pragma: no cover
            self.skipTest(f'no symlink/junction here: {e}')

    def served(self):
        return (self.public / tp.CURRENT / 'index.html').read_text()

    def builds(self):
        return sorted(p.name for p in self.public.iterdir()
                      if p.name.startswith(tp.BUILD_PREFIX))

    def test_current_serves_the_latest_complete_build(self):
        self.assertEqual(self.served(), '0')
        tp.release(self.public, lambda d: (d / 'index.html').write_text('1'))
        self.assertEqual(self.served(), '1')

    def test_the_previous_build_is_deleted_and_nothing_accumulates(self):
        for i in range(1, 4):
            tp.release(self.public,
                       lambda d, i=i: (d / 'index.html').write_text(str(i)))
        self.assertEqual(len(self.builds()), 1)
        names = sorted(p.name for p in self.public.iterdir())
        self.assertEqual(names, sorted([tp.CURRENT, self.builds()[0]]))

    def test_a_build_that_dies_leaves_the_last_site_untouched(self):
        def die(d):
            (d / 'index.html').write_text('half')
            raise RuntimeError('store went away')
        with self.assertRaises(RuntimeError):
            tp.release(self.public, die)
        self.assertEqual(self.served(), '0')
        self.assertEqual(len(self.builds()), 1)     # the half build is gone

    def test_a_leftover_from_a_crash_is_swept_by_the_next_release(self):
        (self.public / (tp.BUILD_PREFIX + 'orphan')).mkdir()
        tp.release(self.public, lambda d: (d / 'index.html').write_text('1'))
        self.assertEqual(len(self.builds()), 1)
        self.assertEqual(self.served(), '1')

    def test_the_directory_the_edge_mounts_is_never_recreated(self):
        import os
        ino = os.stat(self.public).st_ino
        tp.release(self.public, lambda d: (d / 'index.html').write_text('1'))
        self.assertEqual(os.stat(self.public).st_ino, ino)

    def test_the_link_is_relative_so_it_resolves_inside_the_container(self):
        import os
        link = self.public / tp.CURRENT
        if not link.is_symlink():
            self.skipTest('junction, not a symlink')
        self.assertFalse(Path(os.readlink(link)).is_absolute())

    def test_a_flat_legacy_layout_is_kept_once_then_swept(self):
        """Migration from the pre-`current` layout: the old edge still serves
        the flat files until it is recreated, so the first release leaves
        them; the second one — the edge now on `current` — removes them."""
        pub = Path(self.tmp.name) / 'legacy'
        pub.mkdir()
        (pub / 'index.html').write_text('flat')
        (pub / 'gewerke').mkdir()
        (pub / 'gewerke' / 'index.html').write_text('flat')
        tp.release(pub, lambda d: (d / 'index.html').write_text('1'))
        self.assertTrue((pub / 'index.html').exists())
        self.assertTrue((pub / 'gewerke' / 'index.html').exists())
        tp.release(pub, lambda d: (d / 'index.html').write_text('2'))
        self.assertFalse((pub / 'index.html').exists())
        self.assertFalse((pub / 'gewerke').exists())
        self.assertEqual((pub / tp.CURRENT / 'index.html').read_text(), '2')


if __name__ == '__main__':
    unittest.main(verbosity=2)
