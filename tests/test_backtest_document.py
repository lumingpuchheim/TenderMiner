"""`backtest.py` produces ONE document, and every readable form renders it —
doc/TRADE_PAGES.md §6d.

The replay costs about half an hour (measured 2026-08-11: 33 min, 46 trained
cutoffs), so nothing here replays anything. These tests pin the contract
around it: what the document must carry, that stdout carries the document and
only the document, and that the prose is a function of the document rather
than of the replay's memory.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backtest                                                  # noqa: E402


def minimal_res(**over):
    """The smallest `replay()` result `build_payload` accepts. No subscription
    picks, so the awards frame is never consulted."""
    res = {'outcome': {('p1', 'L1'): 1}, 'winners': {}, 'flagged': {},
           'scored': {('p1', 'L1'): '452', ('p2', 'L2'): '453'},
           'sub_picks': {}, 'subs': {}, 'step_days': 7, 'n_cutoffs': 46}
    res.update(over)
    return res


class TheDocument(unittest.TestCase):
    def test_an_unawarded_lot_is_kept_with_a_null_outcome(self):
        """One list carries both denominators: every examined lot is a row,
        and `n_tenders is None` is the ones no award has published for. Drop
        them and the report can no longer say how many were examined."""
        p = backtest.build_payload(minimal_res())
        self.assertEqual(p['n_lots'], 2)
        by_lot = {r['lot_id']: r for r in p['lots']}
        self.assertEqual(by_lot['L1']['n_tenders'], 1)
        self.assertIsNone(by_lot['L2']['n_tenders'])

    def test_a_lot_row_carries_cpv3_but_never_a_trade(self):
        """cpv3 is a raw store field and cannot drift. A trade is a title
        word-match that trades.txt redefines, so a document carrying trades
        would silently disagree with trades.txt the day it changed."""
        row = backtest.build_payload(minimal_res())['lots'][0]
        self.assertEqual(row['cpv3'], '452')
        self.assertNotIn('trade', row)
        self.assertNotIn('title', row)

    def test_the_document_is_json_serialisable(self):
        """Tuple lot keys and numpy scalars have both leaked into payloads
        here before; json.dumps is the only honest check."""
        json.dumps(backtest.build_payload(minimal_res()))

    def test_metadata_says_which_run_this_was(self):
        p = backtest.build_payload(minimal_res())
        for k in ('schema', 'generated', 'model_tag', 'step_days',
                  'cutoffs_trained'):
            self.assertIn(k, p)


class StdoutIsTheDocument(unittest.TestCase):
    """`calibrate`, `subscriptions` and `embed` all print progress with a bare
    `print`. One such line in the middle of the stream is a corrupt JSON file
    that took half an hour to produce — this happened on the first real run.
    """

    def run_main(self, argv, replay_impl):
        real_replay = backtest.replay
        backtest.replay = replay_impl
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                sys.argv = argv
                backtest.main()
        finally:
            backtest.replay = real_replay
        return buf.getvalue()

    def test_a_module_printing_to_stdout_cannot_corrupt_the_document(self):
        def noisy(*a, **k):
            print('[calibrate] cohesion baseline 0.32, trust cut 0.47')
            print('[subscriptions] ignoring retired field')
            return minimal_res()

        out = self.run_main(['backtest.py', '--sub', 'x'], noisy)
        doc = json.loads(out)          # the assertion: it still parses
        self.assertEqual(doc['n_lots'], 2)
        self.assertNotIn('calibrate', out)

    def test_out_writes_the_named_file_and_leaves_stdout_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'run.json'
            out = self.run_main(
                ['backtest.py', '--sub', 'x', '--out', str(p)],
                lambda *a, **k: minimal_res())
            self.assertEqual(out, '')
            self.assertEqual(json.loads(p.read_text(encoding='utf-8'))
                             ['n_lots'], 2)


class BadInput(unittest.TestCase):
    """Valid JSON is not a valid document. An old run, a truncated file or an
    unrelated .json all parse fine and then raise KeyError mid-render — which
    on the site path would kill a build over an optional section."""

    def bad(self, payload):
        with self.assertRaises(backtest.BadDocument) as c:
            backtest.validate(payload)
        return str(c.exception)

    def test_a_good_document_passes(self):
        doc = backtest.build_payload(minimal_res())
        self.assertIs(backtest.validate(doc), doc)

    def test_not_an_object(self):
        self.assertIn('JSON object', self.bad([1, 2, 3]))

    def test_no_schema_field_names_the_fix(self):
        doc = backtest.build_payload(minimal_res())
        del doc['schema']
        self.assertIn('re-run', self.bad(doc))

    def test_a_newer_schema_says_so_instead_of_crashing(self):
        """The failure mode this prevents: a schema-2 document rendered by
        schema-1 code, guessing at fields it does not have."""
        doc = backtest.build_payload(minimal_res())
        doc['schema'] = backtest.SCHEMA + 1
        msg = self.bad(doc)
        self.assertIn('newer', msg)
        self.assertIn(str(backtest.SCHEMA), msg)

    def test_an_older_document_missing_a_lot_field_is_named(self):
        doc = backtest.build_payload(minimal_res())
        del doc['lots'][0]['cpv3']
        msg = self.bad(doc)
        self.assertIn('cpv3', msg)
        self.assertIn('older', msg)

    def test_missing_lots_and_missing_metadata(self):
        doc = backtest.build_payload(minimal_res())
        del doc['lots']
        self.assertIn('nothing to render', self.bad(doc))
        doc = backtest.build_payload(minimal_res())
        del doc['generated']
        self.assertIn('generated', self.bad(doc))

    def test_unparseable_and_absent_files_carry_the_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'run.json'
            p.write_text('{ not json', encoding='utf-8')
            with self.assertRaises(backtest.BadDocument) as c:
                backtest.read_payload(str(p))
            self.assertIn('not valid JSON', str(c.exception))
            with self.assertRaises(backtest.BadDocument) as c:
                backtest.read_payload(str(Path(d) / 'absent.json'))
            self.assertIn('cannot read', str(c.exception))


class TheSiteDowngradesButSaysSo(unittest.TestCase):
    """Opposite policy to --render, deliberately: the site has market pages to
    protect, so a bad document costs the forecast section only. But silently
    would make a typo'd path indistinguishable from a deliberate omission."""

    def load(self, path):
        import trade_pages
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = trade_pages.load_replay(path)
        return got, err.getvalue()

    def test_no_path_is_silent(self):
        got, err = self.load(None)
        self.assertIsNone(got)
        self.assertEqual(err, '')

    def test_a_bad_path_is_none_but_announced(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'typo.json'
            got, err = self.load(p)
        self.assertIsNone(got)
        self.assertIn('ignored', err)
        self.assertIn('no forecast claim', err)

    def test_a_wrong_shaped_document_does_not_raise(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'old.json'
            p.write_text(json.dumps({'lots': [], 'generated': '2026-01-01'}),
                         encoding='utf-8')
            got, err = self.load(p)
        self.assertIsNone(got)
        self.assertIn('schema', err)


class TheProseIsARenderer(unittest.TestCase):
    def test_the_report_is_a_function_of_the_document_alone(self):
        """No replay, no store, no data dir — if this needs anything else,
        'render it many ways' is not true."""
        doc = json.loads(json.dumps(backtest.build_payload(minimal_res())))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            backtest.report(doc)
        text = buf.getvalue()
        self.assertIn('Tenders examined while open: 2', text)
        self.assertIn('results known for 1', text)

    def test_render_reads_a_document_from_a_path(self):
        doc = backtest.build_payload(minimal_res())
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'run.json'
            p.write_text(json.dumps(doc), encoding='utf-8')
            self.assertEqual(backtest.read_payload(str(p))['n_lots'], 2)

    def test_the_report_writes_no_file(self):
        """It used to leave a dated .md nobody read. The prose is printed."""
        doc = backtest.build_payload(minimal_res())
        with tempfile.TemporaryDirectory() as d:
            before = set(Path(d).rglob('*'))
            with contextlib.redirect_stdout(io.StringIO()):
                backtest.report(doc)
            self.assertEqual(before, set(Path(d).rglob('*')))


if __name__ == '__main__':
    unittest.main()
