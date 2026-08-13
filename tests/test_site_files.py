"""The public site is plain HTML in `site/` — no renderer, no build step.

So these tests are the only automated thing standing behind it. They check the
two properties a hand-edited page can lose without anyone noticing:

1. **the legal texts must not drift from the app's**, because the app serves
   its own Impressum/Datenschutz to token visitors and the site serves these
   to strangers — one of them being older than the other is the failure;
2. **the landing page must keep the promises the copy was approved on** —
   no trade names, no form, no unbacked figure, and no `schätzen`.
"""

import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app                                                      # noqa: E402

SITE = Path(__file__).resolve().parent.parent / 'site'


def text_of(path):
    """Visible text of a page: tags out, entities left alone."""
    html = path.read_text(encoding='utf-8')
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html))


class Landing(unittest.TestCase):
    def setUp(self):
        self.html = (SITE / 'index.html').read_text(encoding='utf-8')
        self.text = text_of(SITE / 'index.html')

    def test_says_what_the_service_is(self):
        self.assertIn('kaum jemand mitbietet', self.text)

    def test_names_no_trade(self):
        """A visitor has one trade; a page about someone else's spends his
        time to save his time."""
        for trade in ('Elektro', 'Maler', 'Dachdecker', 'Sanitär',
                      'Straßenbau', 'Gewerke'):
            self.assertNotIn(trade, self.text)

    def test_asks_for_nothing_but_an_email(self):
        self.assertNotIn('<form', self.html)
        self.assertNotIn('<input', self.html)
        self.assertIn('mailto:info@murara.eu', self.html)

    def test_the_cta_offers_the_lookup(self):
        """'Kontaktieren Sie uns' asks a stranger to risk something for
        nothing. The lookup is the free, concrete thing only we can make."""
        self.assertIn('was Sie in den letzten Jahren gewonnen haben',
                      self.text)

    def test_no_ambiguous_schaetzen(self):
        """„Wir schätzen den Wettbewerb" reads as *we value competition* — the
        opposite of the product, stated as a position. The mistake is
        invisible in English, so it gets a test."""
        self.assertNotIn('schätzen', self.text.casefold())

    def test_no_figure_without_its_basis(self):
        self.assertEqual(re.findall(r'\d+\s?%', self.text), [])

    def test_is_indexable_and_self_contained(self):
        self.assertNotIn('noindex', self.html)
        self.assertIn('rel="canonical"', self.html)
        self.assertNotIn('<script', self.html)
        # the only external reference allowed is our own canonical URL
        self.assertEqual(re.findall(r'https?://(?!www\.murara\.eu)',
                                    self.html), [])

    def test_the_brand_is_murara_not_the_repo_name(self):
        """Murara is the customer-facing name; TenderMining is the repository
        and the system. Nothing a visitor reads may say TenderMining."""
        self.assertIn('Murara', self.html)
        self.assertNotIn('TenderMining', self.html)


class LegalTextsMatchTheApp(unittest.TestCase):
    """Two copies exist on purpose (the site must render with the app down),
    so the drift has to be caught mechanically."""

    def _app_text(self, handler):
        _, _, doc = handler({})
        return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', doc))

    def test_datenschutz_clauses_appear_in_both(self):
        site = text_of(SITE / 'datenschutz' / 'index.html')
        served = self._app_text(app.get_datenschutz)
        for clause in ('Art. 14 Abs. 2 lit. f DSGVO',
                       'Art. 6 Abs. 1 lit. f DSGVO',
                       'Widerspruch (Art. 21 DSGVO)',
                       'Auftragsverarbeitungsvertrag',
                       'Keine Cookies'):
            self.assertIn(clause, site, f'missing from site: {clause}')
            self.assertIn(clause, served, f'missing from app: {clause}')

    def test_both_impressums_say_the_same_thing(self):
        """Better an obvious gap than a plausible invention — and if one gets
        filled in, the other must not be forgotten.

        It was forgotten once: the site page got the real Anbieterkennzeichnung
        while the app still served "[Noch nicht eingetragen]", so a customer
        clicking Impressum in the app got the unfilled one. The app now reads
        the site page instead of keeping a second copy, and the two assertions
        below are the two halves of that: same filled/unfilled state, and —
        when filled — the same provider named on both.
        """
        site = text_of(SITE / 'impressum' / 'index.html')
        served = self._app_text(app.get_impressum)
        self.assertEqual('§ 5 TMG' in site, '§ 5 TMG' in served)
        for line in app._site_impressum_lines():
            self.assertIn(line, served, f'named on the site, missing from the app: {line}')


class Files(unittest.TestCase):
    """Link checks run against the BUILT site, not the source tree.

    `site/` is source: the hand-written pages plus the stylesheet. The trade
    pages are generated into `<data-dir>/public/` and never committed, so
    `site/index.html`'s link to `gewerke/` resolves only after a build — which
    is exactly the state that gets uploaded, and therefore the state worth
    checking. The build here is `publish()` plus a stub trade index, so no
    store and no 40 s."""

    def setUp(self):
        import trade_pages
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.built = Path(self.tmp.name) / 'public'
        trade_pages.publish(self.built, SITE)
        gew = self.built / 'gewerke'
        gew.mkdir(parents=True)
        (gew / 'index.html').write_text(
            trade_pages.index_page([('Malerarbeiten', 'malerarbeiten')]),
            encoding='utf-8')
        (gew / 'malerarbeiten').mkdir()
        (gew / 'malerarbeiten' / 'index.html').write_text('x', encoding='utf-8')
        (self.built / 'sitemap.xml').write_text(
            trade_pages.sitemap([('Malerarbeiten', 'malerarbeiten')]),
            encoding='utf-8')

    def test_the_source_tree_is_the_hand_written_half_only(self):
        for rel in ('index.html', 'style.css', 'robots.txt',
                    'impressum/index.html', 'datenschutz/index.html'):
            self.assertTrue((SITE / rel).exists(), rel)
        self.assertFalse((SITE / 'gewerke').exists(),
                         'generated pages must not be committed')
        self.assertFalse((SITE / 'sitemap.xml').exists(),
                         'the sitemap is generated — it knows both halves')

    def test_publish_copies_every_hand_written_file(self):
        for rel in ('index.html', 'style.css', 'robots.txt',
                    'impressum/index.html', 'datenschutz/index.html'):
            self.assertTrue((self.built / rel).exists(), rel)

    def test_robots_allows_indexing_unlike_the_app(self):
        self.assertIn('Allow: /',
                      (self.built / 'robots.txt').read_text(encoding='utf-8'))

    def test_every_link_and_stylesheet_resolves_to_a_real_file_ALL(self):
        """Same check as below, but over EVERY html file in site/, including
        the generated trade pages — a generator that emits a wrong relative
        depth breaks 32 pages at once."""
        checked = 0
        for page in sorted(self.built.rglob('*.html')):
            html = page.read_text(encoding='utf-8')
            for href in re.findall(r'(?:href|src)="([^"]+)"', html):
                if href.startswith(('mailto:', 'http://', 'https://', '#')):
                    continue
                rel = page.relative_to(self.built)
                self.assertFalse(href.startswith('/'),
                                 f'{rel}: "{href}" is absolute')
                self.assertTrue((page.parent / href).resolve().exists(),
                                f'{rel}: "{href}" points at nothing')
                checked += 1
        self.assertGreater(checked, 10)

    def test_every_link_and_stylesheet_resolves_to_a_real_file(self):
        """Follows every href/src on every page and checks the target exists
        on disk, relative to the page that names it.

        This exists because the first version used absolute paths (`/style.css`,
        `/impressum/`), which resolve to the drive root when the file is opened
        directly — so every link was dead and the page rendered unstyled, and
        nothing caught it. Relative links work both from `file://` and from a
        host, and this test is what keeps them that way."""
        pages = ['index.html', 'impressum/index.html', 'datenschutz/index.html']
        checked = 0
        for rel in pages:
            page = self.built / rel
            html = page.read_text(encoding='utf-8')
            for href in re.findall(r'(?:href|src)="([^"]+)"', html):
                if href.startswith(('mailto:', 'http://', 'https://', '#')):
                    continue
                self.assertFalse(
                    href.startswith('/'),
                    f'{rel}: "{href}" is absolute — it breaks when the file '
                    f'is opened directly. Use a relative path.')
                target = (page.parent / href).resolve()
                self.assertTrue(target.exists(),
                                f'{rel}: "{href}" points at nothing')
                checked += 1
        self.assertGreater(checked, 8)   # guards against a vacuous pass


if __name__ == '__main__':
    unittest.main(verbosity=2)
