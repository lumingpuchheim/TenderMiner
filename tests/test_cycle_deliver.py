"""The split of 2026-08-18: `cycle.py` is the update, `deliver.py` is the
sending — RUNBOOK 1. Behaviours promised: the cycle never mails and never
writes a delivery row; the delivery trains nothing and reads the delivering
model's rows for the lots still open, latest publication per lot; a delivery
that finds its predictions older than --max-age refuses with exit 2 and
sends nothing.

    python -m unittest tests.test_cycle_deliver
"""
import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db                                                     # noqa: E402
import deliver                                                # noqa: E402
import delivering                                             # noqa: E402
import ledger                                                 # noqa: E402
import predicting                                             # noqa: E402
import util                                                   # noqa: E402

REPO = Path(__file__).resolve().parent.parent


class TheSeam(unittest.TestCase):
    """What each file may and may not do, read off the source: the promise
    is structural, so the test is too."""

    def test_no_picks_means_no_mail(self):
        """Operator, 2026-08-20: 'if there is no good tender, we wont send
        anything to the customer.' The guard stands between the rendered
        page and the send: a report without picks is written to the file
        (the operator's record) and never mailed — structural, like the
        seam promises above."""
        src = (REPO / 'delivering.py').read_text(encoding='utf-8')
        guard = src.index('feedback_link is not None and not sel.picks')
        send = src.index('mid = send_report(')
        self.assertLess(guard, send)
        self.assertIn('no picks — report written', src)

    def test_the_cycle_never_delivers(self):
        src = (REPO / 'cycle.py').read_text(encoding='utf-8')
        self.assertNotIn('delivering.deliver(', src)
        self.assertNotIn('learn_references(', src)
        self.assertNotIn('import delivering', src)
        self.assertNotIn('--no-mail', src, 'the cycle has no mail to switch off')

    def test_the_delivery_never_trains_or_downloads(self):
        src = (REPO / 'deliver.py').read_text(encoding='utf-8')
        for forbidden in ('training.learn(', 'predict_open(', 'bulk.py', 'features.py',
                          'ensure_embeddings', 'subprocess'):
            self.assertNotIn(forbidden, src, f'deliver.py must not {forbidden}')
        self.assertIn('delivering.deliver(', src)
        self.assertIn('learn_references(', src)

    def test_both_hold_the_heavy_lock_and_wait(self):
        for name in ('cycle.py', 'deliver.py'):
            src = (REPO / name).read_text(encoding='utf-8')
            self.assertIn('heavy_lock.held(', src)
            self.assertIn('wait=3600', src, f'{name} must wait, not fail, behind the other')

    def test_loop_py_is_gone(self):
        self.assertFalse((REPO / 'loop.py').exists(), 'loop.py was split into cycle.py and deliver.py')
        self.assertFalse((REPO / 'docker' / 'weekly.sh').exists())
        for name in ('cycle.sh', 'deliver.sh'):
            self.assertTrue((REPO / 'docker' / name).exists(), name)

    def test_cron_runs_the_cycle_before_the_delivery(self):
        lines = [l for l in (REPO / 'docker' / 'crontab').read_text(encoding='utf-8').splitlines()
                 if l.strip() and not l.lstrip().startswith('#') and '/app/docker/' in l]
        jobs = {l.split('/app/docker/')[1].split()[0]: l.split()[:5] for l in lines}
        self.assertIn('cycle.sh', jobs)
        self.assertIn('deliver.sh', jobs)
        self.assertNotIn('weekly.sh', jobs)
        c, d = jobs['cycle.sh'], jobs['deliver.sh']
        self.assertEqual((c[4], d[4]), ('1', '1'), 'both on Monday')
        c_min = int(c[1]) * 60 + int(c[0])
        d_min = int(d[1]) * 60 + int(d[0])
        self.assertGreaterEqual(d_min - c_min, 60, 'the delivery runs at least an hour after the cycle')


class Freshness(unittest.TestCase):

    def setUp(self):
        self.now = datetime(2026, 8, 24, 8, 30, tzinfo=timezone.utc)

    def test_fresh_passes_and_reports_the_age(self):
        age = deliver.check_fresh('2026-08-24T07:10:00+00:00', '1d', now=self.now)
        self.assertEqual(age, timedelta(hours=1, minutes=20))

    def test_stale_refuses_and_names_the_fix(self):
        with self.assertRaises(deliver.Stale) as cm:
            deliver.check_fresh('2026-08-17T07:10:00+00:00', '1d', now=self.now)
        self.assertIn('cycle.py run', str(cm.exception))
        self.assertIn('7d', str(cm.exception))

    def test_a_larger_max_age_is_the_operators_call(self):
        deliver.check_fresh('2026-08-20T07:10:00+00:00', '1w', now=self.now)

    def test_nothing_to_deliver_from_is_stale(self):
        with self.assertRaises(deliver.Stale):
            deliver.check_fresh(None, '1d', now=self.now)

    def test_a_naive_timestamp_is_read_as_utc(self):
        age = deliver.check_fresh('2026-08-24T07:10:00', '1d', now=self.now)
        self.assertEqual(age, timedelta(hours=1, minutes=20))

    def test_the_cli_exits_2_on_stale(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            data, models = tmp / 'data', tmp / 'models'
            (data / 'store').mkdir(parents=True)
            models.mkdir()
            db.init(data)
            # a champion that wrote one row, a week ago
            (models / 'm-old').mkdir()
            util.write_json(models / 'm-old' / 'meta.json', {'model_id': 'm-old'})
            (models / 'CURRENT').write_text('m-old\n')
            tenders = pd.DataFrame({'procedure_id': ['p1'], 'lot_id': ['L1'],
                                    'notice_id': ['n1'], 'publication_date': ['2026-08-10'],
                                    'deadline_date': ['2026-12-31'], 'n_tenders': [None],
                                    'buyer_name': ['B'], 'title': ['t'],
                                    'cpv_main': ['45210000'], 'est_value_lot': [1.0],
                                    'source': ['ted'], 'form_type': ['competition']})
            tenders.to_parquet(data / 'store' / 'tenders.parquet')
            awards = pd.DataFrame({'procedure_id': ['p0'], 'lot_id': ['L1'],
                                   'publication_date': ['2026-07-01'],
                                   'n_tenders': [3.0], 'quality_flags': [None]})
            awards.to_parquet(data / 'store' / 'awards.parquet')
            ledger.append(data, 'predictions', [{
                'ts': (util.now_utc() - timedelta(days=7)).isoformat(timespec='seconds'),
                'model': 'm-old', 'procedure_id': 'p1', 'lot_id': 'L1', 'notice_id': 'n1',
                'publication_date': '2026-08-10', 'score': 0.7, 'threshold': 0.5,
                'flag': True, 'tier': 'HIGH'}])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = deliver.main(['run', '--data-dir', str(data), '--models-dir', str(models),
                                     '--no-mail'])
            self.assertEqual(code, 2, out.getvalue())
            self.assertIn('STALE', out.getvalue())
            self.assertEqual(ledger.read(data, 'deliveries'), [], 'nothing was delivered')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SeenBefore(unittest.TestCase):
    """`delivering.seen_before`: the no-repeat rule's memory. A lot in an
    earlier report is excluded from today's picks; a lot from TODAY's own
    rows is not, so re-running the same day regenerates the same report."""

    ROWS = [
        {'ts': '2026-08-17T08:30:00+00:00', 'sub_id': 'a',
         'procedure_id': 'p1', 'lot_id': 'L1'},
        {'ts': '2026-08-24T08:30:00+00:00', 'sub_id': 'a',
         'procedure_id': 'p2', 'lot_id': 'L1'},
        {'ts': '2026-08-17T08:30:00+00:00', 'sub_id': 'b',
         'procedure_id': 'p3', 'lot_id': 'L1'},
    ]

    def test_earlier_reports_count_todays_do_not(self):
        seen = delivering.seen_before(self.ROWS, date(2026, 8, 24))
        self.assertEqual(seen, {'a': {('p1', 'L1')}, 'b': {('p3', 'L1')}})

    def test_memory_is_per_subscription(self):
        """Customer b never saw a's lot; it must stay recommendable to b."""
        seen = delivering.seen_before(self.ROWS, date(2026, 8, 24))
        self.assertNotIn(('p1', 'L1'), seen['b'])

    def test_no_history_no_memory(self):
        self.assertEqual(delivering.seen_before([], date(2026, 8, 24)), {})


class OpenScored(unittest.TestCase):
    """`predicting.open_scored`: the delivering model's rows, open lots only."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data, self.models = self.tmp / 'data', self.tmp / 'models'
        self.data.mkdir()
        self.models.mkdir()
        db.init(self.data)
        self.paths = util.Paths(self.data, self.models)
        (self.models / 'CURRENT').write_text('m2\n')
        (self.models / 'm2').mkdir()
        util.write_json(self.models / 'm2' / 'meta.json', {'model_id': 'm2'})
        today = util.now_utc().date()
        far = (today + timedelta(days=30)).isoformat()
        gone = (today - timedelta(days=1)).isoformat()
        cols = ['procedure_id', 'lot_id', 'notice_id', 'publication_date', 'deadline_date',
                'n_tenders', 'buyer_name']
        self.tenders = pd.DataFrame([
            ('p1', 'L1', 'n1', '2026-08-01', far, None, 'B'),     # open, both models scored
            ('p2', 'L1', 'n2', '2026-08-01', far, None, 'B'),     # open, only m1 (shadow) scored
            ('p3', 'L1', 'n3', '2026-08-01', gone, None, 'B'),    # deadline passed
            ('p4', 'L1', 'n4', '2026-08-01', far, None, 'B'),     # awarded
            ('p5', 'L1', 'n5', '2026-08-01', far, None, 'B'),     # open, m2 scored twice (revision)
            ('p5', 'L1', 'n6', '2026-08-05', far, None, 'B'),
        ], columns=cols)
        self.aw = pd.DataFrame([('p4', 'L1', 3)], columns=['procedure_id', 'lot_id', 'n_tenders'])
        rows = [
            self._row('m1', 'p1', 'n1', 0.1, '2026-08-01'), self._row('m2', 'p1', 'n1', 0.8, '2026-08-01'),
            self._row('m1', 'p2', 'n2', 0.5, '2026-08-01'),
            self._row('m2', 'p3', 'n3', 0.9, '2026-08-01'),
            self._row('m2', 'p4', 'n4', 0.9, '2026-08-01'),
            self._row('m2', 'p5', 'n5', 0.6, '2026-08-01'), self._row('m2', 'p5', 'n6', 0.7, '2026-08-05'),
        ]
        ledger.append(self.data, 'predictions', rows)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _row(self, model, p, n, score, pub):
        return {'ts': f'2026-08-24T07:00:0{n[-1]}+00:00', 'model': model, 'procedure_id': p,
                'lot_id': 'L1', 'notice_id': n, 'publication_date': pub, 'score': score,
                'threshold': 0.5, 'flag': score >= 0.5, 'tier': 'LOW'}

    def test_delivering_models_rows_for_open_lots_only(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rows, newest = predicting.open_scored(self.paths, self.tenders, self.aw)
        got = sorted((r['procedure_id'], r['notice_id'], r['model']) for r in rows)
        self.assertEqual(got, [('p1', 'n1', 'm2'), ('p5', 'n5', 'm2'), ('p5', 'n6', 'm2')])
        self.assertEqual(newest, '2026-08-24T07:00:06+00:00')

    def test_deliver_reduces_to_the_latest_publication_per_lot(self):
        """delivering.deliver keeps one row per lot, the latest publication —
        the reduction it has always applied to the in-memory list."""
        with contextlib.redirect_stdout(io.StringIO()):
            rows, _ = predicting.open_scored(self.paths, self.tenders, self.aw)
        latest = {}
        for row in rows:
            key = (row['procedure_id'], row['lot_id'])
            if key not in latest or str(row['publication_date']) >= str(latest[key]['publication_date']):
                latest[key] = row
        self.assertEqual(latest[('p5', 'L1')]['notice_id'], 'n6')

    def test_switching_the_delivering_model_switches_the_rows(self):
        (self.models / 'CURRENT').write_text('m1\n')
        (self.models / 'm1').mkdir()
        util.write_json(self.models / 'm1' / 'meta.json', {'model_id': 'm1'})
        with contextlib.redirect_stdout(io.StringIO()):
            rows, _ = predicting.open_scored(self.paths, self.tenders, self.aw)
        self.assertEqual(sorted(r['procedure_id'] for r in rows), ['p1', 'p2'])

    def test_no_champion_is_nothing_to_deliver_from(self):
        (self.models / 'CURRENT').unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            rows, newest = predicting.open_scored(self.paths, self.tenders, self.aw)
        self.assertEqual((rows, newest), ([], None))


if __name__ == '__main__':
    unittest.main()
