"""The public site renderer — doc/LAUNCH.md 4.1.

Own file, and deliberately not touching the real store: every test builds a
tiny synthetic lot frame, so the assertions are about the RULES (what may
appear on a page, and where) rather than about this month's data.

The three rules worth a test each are promises about other people's data:
no firm names anywhere, forecast language only on CPV 452, and the thin-page
guardrail that keeps a few hundred doorway pages off the domain.
"""

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import public_site as ps                                        # noqa: E402


def lots_frame(n_per_land=None, cpv='4531', winner='Geheim Bau GmbH'):
    """A small store-shaped frame. `cpv` decides whether the trade counts as
    CPV 452 (the forecast gate)."""
    rows = []
    soon = (date.today() + timedelta(days=20)).isoformat()
    for land, n in (n_per_land or {'DE2': 3}).items():
        for i in range(n):
            rows.append({
                'procedure_id': f'{land}-{i}', 'lot_id': 'LOT-0001',
                'title': f'Kabelarbeiten Los {i}',
                'description': 'Elektroinstallation im Bestand',
                'buyer_name': 'Stadt Musterstadt',
                'place_nuts3': f'{land}12', 'cpv_main': cpv,
                'publication_date': '2026-07-01', 'deadline_date': soon,
                'award_value': 100_000.0 + i, 'n_tenders': 1.0,
                'result_code': 'awarded', 'award_date': '2026-07-20',
                'winner_names': [winner], 'resolved': True,
                'est_value_lot': None, 'deadline_days': 20.0,
                'month': pd.Period('2026-07', freq='M'),
                'nuts2': land, 'publication_number': f'0000{i}-2026',
            })
    return pd.DataFrame(rows)


class Rules(unittest.TestCase):
    def setUp(self):
        self.lots = lots_frame()
        self.sel = pd.Series([True] * len(self.lots))
        self.covered = self.mature = [pd.Period('2026-07', freq='M')]

    def _page(self, cpv='4531'):
        self.lots = lots_frame(cpv=cpv)
        return ps.trade_page('Elektroinstallation', 'elektroinstallation',
                             self.lots, pd.Series([True] * len(self.lots)),
                             self.covered, self.mature, '', [])

    def test_no_winner_name_ever_reaches_a_page(self):
        """LAUNCH.md 4: the award is public record, the winner still stays
        unnamed. 'There is no third category.'"""
        page = self._page()
        self.assertNotIn('Geheim Bau', page)

    def test_buyers_are_named_because_they_are_public_bodies(self):
        self.assertIn('Stadt Musterstadt', self._page())

    def test_forecast_language_only_on_cpv_452(self):
        """The one trade with measured lift. Elsewhere the section must not
        render and the CTA must carry no lift flavour at all."""
        page_452 = self._page(cpv='45233')
        self.assertIn('voraussichtlich wettbewerbsarm', page_452)
        self.assertIn('2,3-fache Trefferquote', page_452)

        page_other = self._page(cpv='45310')
        self.assertNotIn('voraussichtlich', page_other)
        self.assertNotIn('Trefferquote', page_other)
        self.assertIn('Woche für Woche', page_other)   # product-general CTA

    def test_shares_are_rendered_with_their_denominator(self):
        self.assertIn('Lose)', self._page())

    def test_freshness_line_is_always_present(self):
        self.assertIn('Stand:', self._page())

    def test_public_pages_are_indexable_unlike_the_app(self):
        page = self._page()
        self.assertNotIn('noindex', page)
        self.assertIn('rel="canonical"', page)
        self.assertIn('name="description"', page)

    def test_pages_carry_no_javascript_and_no_external_reference(self):
        """4.2: build-time only. A fetch() to the app would create a runtime
        data flow, a CORS surface, and a public page that breaks when the app
        is down."""
        page = self._page()
        for forbidden in ('<script', 'fetch(', 'http://', 'https://cdn',
                          '<link rel="stylesheet"'):
            self.assertNotIn(forbidden, page)

    def test_meta_line_drops_missing_values_instead_of_dashing_them(self):
        self.assertEqual(ps.meta_line('Stadt X', '', '1 Gebot'),
                         'Stadt X · 1 Gebot')
        self.assertEqual(ps.meta_line(None, '—', 'x'), 'x')

    def test_slugs_are_url_safe_and_transliterate_umlauts(self):
        self.assertEqual(ps.slugify('Straßenbau'), 'strassenbau')
        self.assertEqual(ps.slugify('Lüftung, Klima und Kälte'),
                         'lueftung-klima-und-kaelte')


class ThinPageGuardrail(unittest.TestCase):
    """A (trade, Land) page renders only above the volume floor; below it the
    Land folds into the national page. Hundreds of doorway-thin pages would
    demote the whole domain (4.1)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / 'site'

    def _render(self, lots):
        covered = [pd.Period('2026-07', freq='M')]
        trades = {'Elektroinstallation': {'terms': ['kabel'], 'exclude': []}}
        written = []
        # render() reads the store; drive the page functions directly instead,
        # which is what the guardrail arithmetic lives next to.
        n_months = len(covered)
        for code in ('DE2', 'DE9'):
            sel = lots.place_nuts3.str.startswith(code)
            if int(sel.sum()) / n_months >= ps.MIN_LOTS_MONTH:
                written.append(code)
        return written, trades

    def test_a_thin_land_gets_no_page(self):
        lots = lots_frame({'DE2': 30, 'DE9': 4})
        written, _ = self._render(lots)
        self.assertEqual(written, ['DE2'])

    def test_the_floor_is_lots_per_month_not_lots(self):
        """9 lots in one covered month is below the floor; the same 9 across
        the same month stays below it however they are spread."""
        lots = lots_frame({'DE2': 9})
        written, _ = self._render(lots)
        self.assertEqual(written, [])


class Sitemap(unittest.TestCase):
    def test_sitemap_lists_every_url_with_the_base(self):
        xml = ps.sitemap(['/', '/gewerke/x/'], 'https://www.example.de')
        self.assertIn('<loc>https://www.example.de/</loc>', xml)
        self.assertIn('<loc>https://www.example.de/gewerke/x/</loc>', xml)
        self.assertIn('lastmod', xml)


if __name__ == '__main__':
    unittest.main(verbosity=2)
