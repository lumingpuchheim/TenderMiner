"""PARAMETERS.md 10: the automated rejector, and the override lever it rides on.

No harness is actually run here — the subprocess is a stub script — because
what needs testing is the rejection rule, the expiry, and the fact that a
candidate is measured under its OWN gate configuration rather than the
champion's.
"""
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backplay                                                   # noqa: E402
import knobs                                                      # noqa: E402
import ledger                                                     # noqa: E402
import util                                                       # noqa: E402

REPO = Path(__file__).resolve().parent.parent

Q = knobs.Question(
    id='nomination-bar', knob='evidence.NOMINATION_BAR', bucket='gate',
    question='Does a lower nomination bar buy recall without breaching the bar?',
    metric='recall', benchmark='abc123abc123',
    grid=(0.50, 0.55, 0.60), current=0.55,
    opened='2026-08-01', stop='2026-11-30')


def m(metric, n, leakage=None):
    row = {'metric': metric, 'n': n}
    if leakage is not None:
        row['leakage'] = leakage
    return row


class TheOverrideLever(unittest.TestCase):
    """The lever is what lets a candidate be measured without editing a
    constant. It has to reach BOTH modules and it has to refuse a typo."""

    def _run(self, override, code):
        env = dict(os.environ)
        env['TM_GATE_OVERRIDE'] = override
        env['PYTHONIOENCODING'] = 'utf-8'
        return subprocess.run([sys.executable, '-c', code], env=env, cwd=str(REPO),
                              capture_output=True, text=True)

    def test_an_evidence_rule_can_be_overridden_and_moves_the_fingerprint(self):
        p = self._run('{"SYN_THRESHOLD": 0.9}',
                      'import evidence as e, relevance as r;'
                      'print(e.SYN_THRESHOLD, r.DEFAULT_CONFIG.fingerprint)')
        self.assertEqual(p.returncode, 0, p.stderr)
        value, fingerprint = p.stdout.strip().splitlines()[-1].split()
        self.assertEqual(value, '0.9')
        self.assertNotEqual(fingerprint, knobs.EXPECTED_GATE_FINGERPRINT)

    def test_a_relevance_constant_can_be_overridden(self):
        p = self._run('{"NOMINATION_BAR": 0.60}',
                      'import relevance as r;'
                      'print(r.DEFAULT_CONFIG.nomination_bar)')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip().splitlines()[-1], '0.6')

    def test_an_unknown_key_refuses_rather_than_measuring_the_champion(self):
        """The failure this exists to prevent: a run that reports a candidate's
        name and measures the value that was already there."""
        p = self._run('{"NOMINATON_BAR": 0.60}',
                      'import relevance as r; r.DEFAULT_CONFIG.fingerprint')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('NOMINATON_BAR', p.stderr)

    def test_malformed_json_refuses(self):
        p = self._run('{not json',
                      'import relevance as r; r.DEFAULT_CONFIG.fingerprint')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('not valid JSON', p.stderr)

    def test_no_override_leaves_the_shipped_configuration_alone(self):
        env = dict(os.environ)
        env.pop('TM_GATE_OVERRIDE', None)
        p = subprocess.run([sys.executable, '-c',
                            'import relevance as r; print(r.DEFAULT_CONFIG.fingerprint)'],
                           env=env, cwd=str(REPO), capture_output=True, text=True)
        self.assertEqual(p.stdout.strip().splitlines()[-1],
                         knobs.EXPECTED_GATE_FINGERPRINT)


class TheRejectionRule(unittest.TestCase):
    def test_a_majority_of_hard_bar_breaches_kills(self):
        killed, why = backplay.rejects([m(.5, 400)] * 4,
                                       [m(.7, 400, .031), m(.7, 400, .028),
                                        m(.7, 400, .030), m(.7, 400, .010)])
        self.assertTrue(killed)
        self.assertIn('3/4', why)

    def test_a_minority_of_breaches_survives(self):
        """Conservative on purpose: several candidates over several cutoffs
        guarantee some bad-looking runs, and an eager rejector deletes good
        values quietly."""
        killed, why = backplay.rejects([m(.5, 400)] * 4,
                                       [m(.7, 400, .031), m(.7, 400, .010),
                                        m(.7, 400, .012), m(.7, 400, .010)])
        self.assertFalse(killed)
        self.assertEqual(why, 'survives')

    def test_exactly_half_is_not_a_majority(self):
        killed, _ = backplay.rejects([m(.5, 400)] * 2,
                                     [m(.7, 400, .031), m(.7, 400, .010)])
        self.assertFalse(killed)

    def test_losing_on_every_measurement_kills(self):
        killed, why = backplay.rejects([m(.80, 400), m(.80, 400), m(.80, 400)],
                                       [m(.40, 400), m(.42, 400), m(.38, 400)])
        self.assertTrue(killed)
        self.assertIn('all 3', why)

    def test_losing_on_most_but_not_all_survives(self):
        killed, _ = backplay.rejects([m(.80, 400), m(.80, 400), m(.80, 400)],
                                     [m(.40, 400), m(.42, 400), m(.85, 400)])
        self.assertFalse(killed)

    def test_one_bad_measurement_alone_is_weather_not_a_verdict(self):
        killed, _ = backplay.rejects([m(.80, 400)], [m(.40, 400)])
        self.assertFalse(killed)

    def test_overlapping_intervals_are_not_a_loss(self):
        killed, _ = backplay.rejects([m(.52, 400)] * 3, [m(.50, 400)] * 3)
        self.assertFalse(killed)

    def test_no_measurement_rejects_nothing(self):
        killed, why = backplay.rejects([m(.5, 400)], [])
        self.assertFalse(killed)
        self.assertEqual(why, 'no measurement')


class RejectionsExpire(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        import gc
        self.addCleanup(gc.collect)
        self.paths = util.Paths(self.tmp.name, Path(self.tmp.name) / 'models')

    def _row(self, ts, value=0.60, rejected=True):
        ledger.append(self.paths.ledger_home, 'backplays', [{
            'ts': ts, 'question': Q.id, 'knob': Q.knob, 'value': value,
            'harness': 'judge', 'gate_fingerprint': 'abc0000000',
            'benchmark': Q.benchmark, 'rejected': rejected,
            'reason': 'leaks above 2.2%', 'n_measurements': 4}])

    def test_a_fresh_rejection_stands(self):
        self._row('2026-08-10T00:00:00+00:00')
        live = backplay.rejected_values(self.paths, Q.id, '2026-08-16')
        self.assertIn(0.60, live)
        self.assertIn('leaks', live[0.60])

    def test_a_rejection_ages_out(self):
        """A value killed in one market is not dead forever — same reasoning
        as the 90-day ROLLBACK retirement in §8.1."""
        old = (date.fromisoformat('2026-08-16')
               - timedelta(days=backplay.REJECTION_TTL_DAYS + 1)).isoformat()
        self._row(old + 'T00:00:00+00:00')
        self.assertEqual(backplay.rejected_values(self.paths, Q.id, '2026-08-16'), {})

    def test_a_survivor_is_not_a_rejection(self):
        self._row('2026-08-10T00:00:00+00:00', rejected=False)
        self.assertEqual(backplay.rejected_values(self.paths, Q.id, '2026-08-16'), {})

    def test_another_question_is_not_this_question(self):
        self._row('2026-08-10T00:00:00+00:00')
        self.assertEqual(backplay.rejected_values(self.paths, 'other', '2026-08-16'), {})


class WeeklyReadsTheRejections(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        import gc
        self.addCleanup(gc.collect)
        self.paths = util.Paths(self.tmp.name, Path(self.tmp.name) / 'models')

    def test_a_rejected_candidate_is_not_proposed_and_the_line_says_who_killed_it(self):
        """The operator must see a rejection, never a silently shorter grid."""
        rows = [{'value': 0.50, 'metric': 0.40, 'n': 400},
                {'value': 0.55, 'metric': 0.50, 'n': 400},
                {'value': 0.60, 'metric': 0.95, 'n': 400}]
        q = dataclasses.replace(Q, run=lambda: rows)
        # without a rejection the sweep's winner is proposed
        line = knobs.weekly(self.paths, '2026-08-16', [q])[0]
        self.assertIn('move up', line)
        # once backplay has killed 0.60 it is neither proposed nor hidden
        ledger.append(self.paths.ledger_home, 'backplays', [{
            'ts': '2026-08-15T00:00:00+00:00', 'question': Q.id, 'knob': Q.knob,
            'value': 0.60, 'harness': 'judge', 'gate_fingerprint': 'abc0000000',
            'benchmark': Q.benchmark, 'rejected': True,
            'reason': 'leaks above 2.2% on 3/4 measurements', 'n_measurements': 4}])
        line = knobs.weekly(self.paths, '2026-08-23', [q])[0]
        self.assertNotIn('move up', line)
        self.assertIn('backplay rejected 0.6', line)
        self.assertIn('leaks above 2.2%', line)


class Measure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = util.Paths(self.tmp.name, Path(self.tmp.name) / 'models')

    def test_the_candidates_value_reaches_the_harness_as_an_override(self):
        seen = {}

        def fake_run(argv, env=None, **kw):
            seen['override'] = env.get('TM_GATE_OVERRIDE')
            Path(argv[-1]).write_text('{"kind": "judge"}', encoding='utf-8')
            return subprocess.CompletedProcess(argv, 0, '', '')

        with mock.patch.object(subprocess, 'run', fake_run):
            payload = backplay.measure(self.paths, 0.60, 'judge', knob='NOMINATION_BAR')
        self.assertEqual(json.loads(seen['override']), {'NOMINATION_BAR': 0.60})
        self.assertEqual(payload['kind'], 'judge')

    def test_a_crashed_harness_raises_rather_than_rejecting(self):
        """A rejection resting on a crash is the worst kind of silent kill."""
        def fake_run(argv, env=None, **kw):
            return subprocess.CompletedProcess(argv, 1, '', 'boom')

        with mock.patch.object(subprocess, 'run', fake_run):
            with self.assertRaises(RuntimeError):
                backplay.measure(self.paths, 0.60, 'judge', knob='NOMINATION_BAR')

    def test_a_harness_that_writes_nothing_raises(self):
        def fake_run(argv, env=None, **kw):
            return subprocess.CompletedProcess(argv, 0, '', '')

        with mock.patch.object(subprocess, 'run', fake_run):
            with self.assertRaises(RuntimeError):
                backplay.measure(self.paths, 0.60, 'judge', knob='NOMINATION_BAR')

    def test_no_live_question_measures_nothing(self):
        lines = backplay.run(self.paths, [], '2026-08-16')
        self.assertEqual(len(lines), 1)
        self.assertIn('no live question', lines[0])


if __name__ == '__main__':
    unittest.main()
