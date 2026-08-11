"""The public site — the page a stranger lands on.

The site is one page plus the legal texts, so these tests are about the
promises it makes rather than about arithmetic: that it never segments by
trade, that it asks for nothing, that it stays readable with the app down, and
that the legal pages are the app's own texts rather than a second copy that
can drift.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import public_site as ps                                        # noqa: E402


class LandingPage(unittest.TestCase):
    def setUp(self):
        self.page = ps.landing_page('')

    def test_says_what_the_service_is_in_the_headline(self):
        self.assertIn('kaum jemand mitbietet', self.page)

    def test_never_names_a_trade(self):
        """The objection that started the rewrite: an electrician has no use
        for a page about Maler, and a site that browses trades spends his time
        to save his time."""
        for trade in ('Elektro', 'Maler', 'Dachdecker', 'Sanitär',
                      'Straßenbau', 'Gewerke'):
            self.assertNotIn(trade, self.page)

    def test_asks_for_nothing_but_an_email(self):
        """No form, no survey, no trade picker — the whole premise is that the
        profile comes from what the firm has won."""
        self.assertNotIn('<form', self.page)
        self.assertNotIn('<input', self.page)
        self.assertIn('mailto:', self.page)

    def test_the_cta_promises_the_lookup_not_just_contact(self):
        """'Kontaktieren Sie uns' asks a stranger to take a risk for nothing.
        The offer to look up what he won is the free, concrete thing only this
        product can make."""
        self.assertIn('was Sie in den letzten Jahren gewonnen haben',
                      self.page)

    def test_the_limit_is_stated(self):
        self.assertIn('weiß vorher niemand', self.page)

    def test_no_ambiguous_schaetzen(self):
        """'Wir schätzen den Wettbewerb' reads as 'we VALUE competition' —
        the opposite of the product, stated as a position. Guarded because the
        mistake is invisible in English."""
        self.assertNotIn('schätzen', self.page)
        self.assertNotIn('Schätzen', self.page)

    def test_quotes_no_figure_without_its_basis(self):
        """No numbers on the page at all right now (operator's call): a figure
        on a cold page needs its denominator, date and method beside it, or it
        is just a claim."""
        import re
        # ignore the CSS block and any year-like token in the legal footer
        body = self.page.split('</style>', 1)[1]
        self.assertEqual(re.findall(r'\b\d+\s?%', body), [])

    def test_is_indexable_unlike_the_app(self):
        self.assertNotIn('noindex', self.page)
        self.assertIn('rel="canonical"', self.page)
        self.assertIn('name="description"', self.page)

    def test_carries_no_script_and_no_external_reference(self):
        """LAUNCH.md 4.2: build-time only. A fetch() to the app would create a
        runtime data flow, a CORS surface, and a public page that breaks when
        the app is down."""
        for forbidden in ('<script', 'fetch(', 'http://', 'https://cdn',
                          '<link rel="stylesheet"'):
            self.assertNotIn(forbidden, self.page)


class LegalPages(unittest.TestCase):
    def test_legal_texts_are_the_apps_own(self):
        """Imported, not copied: two Datenschutzerklärungen that can drift
        apart is the failure mode, and the public one is what a stranger
        reads."""
        imp, dat = ps.legal_pages('')
        self.assertIn('Art. 21', dat)
        self.assertIn('Widerspruch', dat)
        self.assertIn('Impressum', imp)
        for page in (imp, dat):
            self.assertNotIn('noindex', page)


class Site(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / 'site'

    def test_render_writes_a_complete_static_site(self):
        urls = ps.render(self.out, 'https://www.example.de')
        self.assertEqual(urls, ['/', '/impressum/', '/datenschutz/'])
        for rel in ('index.html', 'impressum/index.html',
                    'datenschutz/index.html', 'sitemap.xml', 'robots.txt'):
            self.assertTrue((self.out / rel).exists(), rel)

    def test_robots_allows_everything_unlike_the_app(self):
        ps.render(self.out, '')
        self.assertIn('Allow: /', (self.out / 'robots.txt').read_text('utf-8'))

    def test_sitemap_carries_the_base_url(self):
        ps.render(self.out, 'https://www.example.de')
        xml = (self.out / 'sitemap.xml').read_text('utf-8')
        self.assertIn('<loc>https://www.example.de/</loc>', xml)
        self.assertIn('<loc>https://www.example.de/impressum/</loc>', xml)

    def test_a_stale_page_cannot_survive_a_rebuild(self):
        ps.render(self.out, '')
        (self.out / 'gewerke').mkdir()
        (self.out / 'gewerke' / 'alt.html').write_text('old', encoding='utf-8')
        ps.render(self.out, '')
        self.assertFalse((self.out / 'gewerke').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
