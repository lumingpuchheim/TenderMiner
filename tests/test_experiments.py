"""A/B arms — doc/EXPERIMENTS.md §12.

    python -m unittest discover -t . -s tests        # from the repository root
    python tests/test_experiments.py                 # or directly

Sandbox data dir with a database, no network, no real training: predictions
are written as rows, awards are a small frame, and the pieces under test are
the declaration checks, the state row, the arm-vs-arm grading (idempotent,
paired), the verdict thresholds, the two operator commands, the zero-open
case and the hidden page.
"""

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
import experiments as ex
import ledger
import loop
import single_bidder as sb
import util
import grading

EXP = ex.DECLARED['cpv-additional-encoding']
ARGS = argparse.Namespace(threshold=0.5, track_window='12w', top_slice=0.2,
                          min_trade_grades=25, min_flag_grades=30)


def arm(id_, label, **kw):
    return ex.Arm(id_, label, **kw)


def exp(id_='x-1', arms=None, opened='2026-08-18', deadline='2026-11-30', deliver='a'):
    return ex.Experiment(id=id_, question='q?', opened=opened, deadline=deadline,
                         arms=tuple(arms or (arm('a', 'A'), arm('b', 'B'))),
                         default_delivering=deliver)


class Declarations(unittest.TestCase):

    def test_the_shipped_declaration_validates(self):
        self.assertIn('cpv-additional-encoding', ex.validate())

    def test_bad_declarations_fail_at_import_time(self):
        bad = [
            [exp(), exp()],                                        # duplicate id
            [exp(id_='Bad_Id')],
            [exp(arms=[arm('a', 'A')])],                           # one arm
            [exp(arms=[arm('a', 'A'), arm('a', 'B')])],            # duplicate arm id
            [exp(arms=[arm('a', 'A'), arm('b', 'A')])],            # duplicate label
            [exp(arms=[arm('a', 'A', feature_build='nope'), arm('b', 'B')])],
            [exp(deliver='zzz')],
            [exp(opened='2026-08-18', deadline='2026-08-18')],
            [exp(opened='2026-08-18', deadline='not-a-date')],
        ]
        for decl in bad:
            with self.subTest(decl=decl), self.assertRaises(ex.DeclarationError):
                ex.validate(decl)


class Home(unittest.TestCase):
    """A sandbox data dir WITH a database, plus a models dir."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / 'data'
        self.models = self.tmp / 'models'
        self.data.mkdir()
        self.models.mkdir()
        db.init(self.data)
        self.paths = util.Paths(self.data, self.models)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class State(Home):

    def test_no_row_before_opened_and_a_row_from_opened_on(self):
        self.assertEqual(ex.ensure_state(self.data, '2026-08-17'), [])
        self.assertFalse(ex.plan(self.data, '2026-08-17').is_trial)
        self.assertEqual(ex.ensure_state(self.data, '2026-08-18'), ['cpv-additional-encoding'])
        row = ex.state(self.data)['cpv-additional-encoding']
        self.assertEqual((row['status'], row['delivering']), ('open', 'onehot'))
        p = ex.plan(self.data, '2026-08-25')
        self.assertTrue(p.is_trial)
        self.assertEqual([a.id for a in p.arms], ['onehot', 'ts'])
        self.assertTrue(p.is_delivering(EXP.arm('onehot')))
        self.assertFalse(p.is_delivering(EXP.arm('ts')))
        # a second call creates nothing more
        self.assertEqual(ex.ensure_state(self.data, '2026-08-25'), [])

    def test_a_jsonl_only_home_never_gets_a_database(self):
        plain = self.tmp / 'plain'
        plain.mkdir()
        p = ex.plan(plain, '2026-09-01')
        self.assertFalse(p.is_trial)
        self.assertFalse(db.path_for(plain).exists())

    def test_declaration_missing_is_marked_not_deleted(self):
        con = db.connect(self.data)
        con.execute("INSERT INTO experiment VALUES ('gone', 'open', 'a', '2026-01-01', "
                    "'2026-02-01', NULL, 't', 't')")
        con.commit()
        ov = ex.overview(self.data, self.models, '2026-09-01')
        self.assertEqual([r['id'] for r in ov['missing']], ['gone'])
        # and it does not count as the open experiment
        self.assertIsNone(ex.open_experiment(self.data))


# ------------------------------------------------------ grading, paired

def frames(lots, awarded):
    """tenders (KEY + slicing cols) and awards (label from n_tenders)."""
    tenders = pd.DataFrame([{'procedure_id': p, 'lot_id': l, 'cpv_main': '45210000',
                             'place_nuts3': 'DE212'} for p, l in lots])
    aw = pd.DataFrame([{'procedure_id': p, 'lot_id': l, 'n_tenders': n,
                        'label': int(n <= 1), 'publication_date': pub,
                        'publication_number': f'{i:08d}-2026'}
                       for i, (p, l, n, pub) in enumerate(awarded)])
    return tenders, aw


def pred(p, l, model, score, ts, arm_id=None, exp_id=None):
    row = {'ts': ts, 'model': model, 'procedure_id': p, 'lot_id': l, 'notice_id': f'{p}-n1',
           'score': score, 'threshold': 0.5, 'flag': score >= 0.5,
           'tier': 'HIGH' if score >= 0.7 else 'LOW'}
    if arm_id:
        row['arm'] = arm_id
        row['experiment'] = exp_id
    return row


class ArmGrading(Home):

    def setUp(self):
        super().setUp()
        ex.ensure_state(self.data, '2026-08-18')
        self.plan = ex.plan(self.data, '2026-08-25')
        lots = [('p1', 'L1'), ('p2', 'L1'), ('p3', 'L1'), ('p4', 'L1')]
        # p1, p2: both arms scored; p3: only ts scored; p4: pre-trial row only
        rows = [
            pred('p1', 'L1', 'm1-onehot', 0.8, '2026-08-18T06:00:00', 'onehot', EXP.id),
            pred('p1', 'L1', 'm1-ts', 0.3, '2026-08-18T06:00:01', 'ts', EXP.id),
            pred('p2', 'L1', 'm1-onehot', 0.2, '2026-08-18T06:00:00', 'onehot', EXP.id),
            pred('p2', 'L1', 'm1-ts', 0.9, '2026-08-18T06:00:01', 'ts', EXP.id),
            pred('p3', 'L1', 'm1-ts', 0.6, '2026-08-18T06:00:01', 'ts', EXP.id),
            pred('p4', 'L1', 'm0', 0.6, '2026-08-01T06:00:00'),
        ]
        ledger.append(self.data, 'predictions', rows)
        self.tenders, self.aw = frames(lots, [
            ('p1', 'L1', 1, '2026-09-20'), ('p2', 'L1', 4, '2026-09-21'),
            ('p3', 'L1', 0, '2026-09-22'), ('p4', 'L1', 1, '2026-09-23')])

    def test_one_row_per_arm_and_lot_and_a_second_run_adds_none(self):
        with contextlib.redirect_stdout(io.StringIO()):
            grading.grade(self.paths, self.tenders, self.aw, ARGS, self.plan)
        by = ex.rows_by_arm(self.data, EXP.id)
        self.assertEqual({a: sorted(r['procedure_id'] for r in rows) for a, rows in by.items()},
                         {'onehot': ['p1', 'p2'], 'ts': ['p1', 'p2', 'p3']})
        r = next(r for r in by['onehot'] if r['procedure_id'] == 'p1')
        self.assertEqual((r['label'], r['flag'], r['score'], r['model']), (1, True, 0.8, 'm1-onehot'))
        with contextlib.redirect_stdout(io.StringIO()):
            grading.grade(self.paths, self.tenders, self.aw, ARGS, self.plan)
        self.assertEqual(sum(len(v) for v in ex.rows_by_arm(self.data, EXP.id).values()), 5)

    def test_customer_record_is_the_delivering_arms_prediction(self):
        with contextlib.redirect_stdout(io.StringIO()):
            grading.grade(self.paths, self.tenders, self.aw, ARGS, self.plan)
        grades = {g['procedure_id']: g for g in ledger.read(self.data, 'grades')}
        # p1: the ts row was appended LAST on the same lot, but onehot delivers
        self.assertEqual(grades['p1']['model'], 'm1-onehot')
        self.assertEqual(grades['p2']['model'], 'm1-onehot')
        # p3: only a shadow scored it — no customer record for it
        self.assertNotIn('p3', grades)
        # p4: pre-trial row, graded as always
        self.assertEqual(grades['p4']['model'], 'm0')

    def test_paired_restriction(self):
        with contextlib.redirect_stdout(io.StringIO()):
            grading.grade(self.paths, self.tenders, self.aw, ARGS, self.plan)
        pr, common = ex.paired(ex.rows_by_arm(self.data, EXP.id), ['onehot', 'ts'])
        self.assertEqual(sorted(p for p, _ in common), ['p1', 'p2'])
        self.assertEqual(len(pr['ts']), 2)      # p3 is in neither comparison


# --------------------------------------------------------------- verdict

def synthetic(n, hits_a, flagged_a, hits_b, flagged_b, seed=1):
    """n paired lots; arm a flags flagged_a of them with hits_a positives, arm
    b likewise; the rest unflagged, label spread so bases are equal."""
    rng = np.random.default_rng(seed)
    labels = np.zeros(n, dtype=int)
    rows = {'a': [], 'b': []}
    for arm_id, hits, flagged in (('a', hits_a, flagged_a), ('b', hits_b, flagged_b)):
        flags = np.zeros(n, dtype=bool)
        flags[:flagged] = True
        lab = labels.copy()
        lab[:hits] = 1
        for i in range(n):
            rows[arm_id].append({'procedure_id': f'p{i}', 'lot_id': 'L', 'flag': bool(flags[i]),
                                 'label': int(lab[i]), 'tier': 'HIGH' if i < 5 else 'LOW',
                                 'award_pub': f'2026-09-{(i % 28) + 1:02d}', 'score': 0.5})
    return rows


class Verdict(unittest.TestCase):

    def setUp(self):
        self.e = exp()
        self.row = {'delivering': 'a', 'deadline': '2026-11-30', 'status': 'open'}

    def v(self, rows, today='2026-10-01', trip=None):
        return ex.verdict(self.e, self.row, rows, today, tripwire_ok=trip)

    def test_collecting_below_the_minimums(self):
        self.assertEqual(self.v(synthetic(99, 20, 30, 10, 30))['status'], 'collecting')
        self.assertEqual(self.v(synthetic(120, 20, 29, 10, 30))['status'], 'collecting')
        v = self.v(synthetic(120, 20, 30, 10, 30))
        self.assertNotEqual(v['status'], 'collecting')

    def test_ready_leaning_and_no_difference(self):
        clear = self.v(synthetic(300, 55, 60, 10, 60), trip={'a': True, 'b': True})
        self.assertEqual((clear['status'], clear['winner']), ('ready', 'a'))
        self.assertGreaterEqual(clear['p'], ex.P_READY)
        blocked = self.v(synthetic(300, 55, 60, 10, 60), trip={'a': False, 'b': True})
        self.assertEqual(blocked['status'], 'leaning')
        self.assertTrue(blocked['ready_blocked_by_tripwire'])
        same = self.v(synthetic(300, 30, 60, 30, 60))
        self.assertEqual((same['status'], same['winner']), ('no difference yet', None))
        # identical evidence: the tie goes to the delivering arm
        self.assertGreaterEqual(same['p_by_arm']['a'], 0.45)
        lean = self.v(synthetic(300, 36, 60, 30, 60))
        self.assertIn(lean['status'], ('leaning', 'no difference yet'))

    def test_deadline_overlay_and_line_uses_labels_verbatim(self):
        v = self.v(synthetic(10, 2, 3, 1, 3), today='2026-12-01')
        self.assertTrue(v['deadline_reached'])
        line = ex.status_line(self.e, v)
        self.assertIn('DEADLINE REACHED', line)
        self.assertIn('A 10 / B 10', line)
        self.assertIn('delivering A', line)

    def test_posterior_is_reproducible(self):
        a = ex.posterior_best([(50, 60), (10, 60)])
        b = ex.posterior_best([(50, 60), (10, 60)])
        self.assertEqual(a, b)
        self.assertGreater(a[0], 0.99)


# ------------------------------------------------------ operator commands

class Commands(Home):

    def setUp(self):
        super().setUp()
        ex.ensure_state(self.data, '2026-08-18')
        for a in ('onehot', 'ts'):
            p = ex.arm_current_path(self.models, a)
            p.parent.mkdir(parents=True)
            p.write_text(f'm1-{a}\n')
        (self.models / 'CURRENT').write_text('m1-onehot\n')
        (self.models / 'other.txt').write_text('untouched')

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ex.main(['--data-dir', str(self.data), '--models-dir', str(self.models), *argv])
        return code, out.getvalue()

    def test_deliver_rewrites_current_and_nothing_else(self):
        code, out = self.run_cli('deliver', 'cpv-additional-encoding', 'ts')
        self.assertEqual(code, 0)
        self.assertEqual((self.models / 'CURRENT').read_text().strip(), 'm1-ts')
        self.assertEqual(ex.arm_current_path(self.models, 'onehot').read_text().strip(), 'm1-onehot')
        self.assertEqual((self.models / 'other.txt').read_text(), 'untouched')
        self.assertEqual(ex.state(self.data)['cpv-additional-encoding']['delivering'], 'ts')
        self.assertIn('target statistics', out)

    def test_close_records_and_hints_but_does_not_switch(self):
        code, out = self.run_cli('close', 'cpv-additional-encoding', '--winner', 'ts',
                                 '--note', 'clear after 12 weeks')
        self.assertEqual(code, 0)
        row = ex.state(self.data)['cpv-additional-encoding']
        self.assertEqual(row['status'], 'closed')
        dec = json.loads(row['decision'])
        self.assertEqual((dec['winner'], dec['note']), ('ts', 'clear after 12 weeks'))
        self.assertIn('deliver cpv-additional-encoding ts', out)
        self.assertEqual((self.models / 'CURRENT').read_text().strip(), 'm1-onehot')
        # closed -> the next plan is the plain single-arm cycle
        self.assertFalse(ex.plan(self.data, '2026-12-01').is_trial)
        # and closing twice is refused
        with self.assertRaises(RuntimeError):
            ex.close(self.data, 'cpv-additional-encoding', 'none', '', None)

    def test_list_and_show(self):
        code, out = self.run_cli('list')
        self.assertEqual(code, 0)
        self.assertIn('cpv-additional-encoding: collecting', out)
        code, out = self.run_cli('show', 'cpv-additional-encoding')
        self.assertEqual(code, 0)
        self.assertIn('one hot', out)
        self.assertIn('target statistics', out)


class ShadowModels(Home):

    def test_only_non_delivering_arms_models_are_shadows(self):
        ex.ensure_state(self.data, '2026-08-18')
        util.append_jsonl(self.models / 'registry.jsonl', [
            {'model_id': 'm0', 'promoted': True},
            {'model_id': 'm1-onehot', 'arm': 'onehot', 'experiment': EXP.id},
            {'model_id': 'm1-ts', 'arm': 'ts', 'experiment': EXP.id},
        ])
        self.assertEqual(ex.shadow_models(self.models, self.data), {'m1-ts'})
        ex.set_delivering(self.data, EXP.id, 'ts', self.models)
        self.assertEqual(ex.shadow_models(self.models, self.data), {'m1-onehot'})


class ZeroOpen(Home):

    def test_grade_without_a_trial_is_unchanged(self):
        ledger.append(self.data, 'predictions', [pred('p1', 'L1', 'm0', 0.6, '2026-08-01T06:00:00')])
        tenders, aw = frames([('p1', 'L1')], [('p1', 'L1', 1, '2026-09-20')])
        with contextlib.redirect_stdout(io.StringIO()):
            g_none = grading.grade(self.paths, tenders, aw, ARGS)
        self.assertEqual(len(g_none), 1)
        self.assertEqual(ledger.read(self.data, 'arm_grades'), [])
        p = ex.plan(self.data, '2026-08-01')      # before opened: no trial
        self.assertFalse(p.is_trial)


# ---------------------------------------------------------------- the page

class HiddenPage(Home):

    def setUp(self):
        super().setUp()
        import app
        self.app = app
        self.wsgi = app.make_app(self.data)
        ex.ensure_state(self.data, '2026-08-18')

    def get(self, path):
        status = {}
        env = {'REQUEST_METHOD': 'GET', 'PATH_INFO': path, 'REMOTE_ADDR': '127.0.0.1'}
        body = b''.join(self.wsgi(env, lambda s, h: status.setdefault('s', s)))
        return status['s'], body.decode('utf-8')

    def test_no_key_no_route(self):
        os.environ.pop('TM_EXPERIMENTS_KEY', None)
        s, _ = self.get('/experiments/anything-at-all-here')
        self.assertTrue(s.startswith('404'))

    def test_key_serves_the_page_with_labels_verbatim(self):
        os.environ['TM_EXPERIMENTS_KEY'] = 'k' * 24
        os.environ['TM_MODELS_DIR'] = str(self.models)
        try:
            s, body = self.get('/experiments/' + 'k' * 24)
            self.assertTrue(s.startswith('200'), s)
            self.assertIn('one hot', body)
            self.assertIn('target statistics', body)
            self.assertIn('cpv-additional-encoding', body)
            self.assertIn('MIN_PAIRED', body)
            s, _ = self.get('/experiments/' + 'x' * 24)
            self.assertTrue(s.startswith('404'))
        finally:
            os.environ.pop('TM_EXPERIMENTS_KEY', None)
            os.environ.pop('TM_MODELS_DIR', None)


class ComposeForwardsTheKey(unittest.TestCase):
    """A .env line reaches the app container only through docker-compose.yml's
    environment block. Found 2026-08-16: the key was in .env.example and the
    route in app.py, and the server still had no page."""

    def test_docker_compose_forwards_tm_experiments_key(self):
        root = Path(__file__).resolve().parent.parent
        compose = (root / 'docker-compose.yml').read_text(encoding='utf-8')
        self.assertIn('TM_EXPERIMENTS_KEY: ${TM_EXPERIMENTS_KEY:-}', compose)
        example = (root / '.env.example').read_text(encoding='utf-8')
        self.assertIn('TM_EXPERIMENTS_KEY=', example)


if __name__ == '__main__':
    unittest.main()
