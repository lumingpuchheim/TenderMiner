"""The guards added after 2026-08-23/24: every heavy job is fenced.

That weekend, in order: a manual calibration ran beside a replay and the
kernel's global OOM killer chose the victim (19:23); the replay was memcg-
killed one cutoff from the end (23:26); and a 04:00 backplay starved the
Monday delivery (the clearance for that one is tested in test_backplay).
These tests pin the two remaining fences: the calibration CLI takes the
heavy lock, and the scripted replay launcher carries the memory caps.
"""
import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backplay                                                   # noqa: E402
import calibrate                                                  # noqa: E402
import heavy_lock                                                 # noqa: E402
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


class TheCalibrationTakesTheLock(unittest.TestCase):
    def test_a_held_lock_refuses_the_cli_before_any_work(self):
        """The 19:23 hole: a manual `python calibrate.py` beside a running
        replay. A stranger must be refused at once (wait=0, manual job) —
        and refused BEFORE the store is opened, which is why `calibrate` is
        patched to explode: reaching it would mean the lock came second."""
        with tempfile.TemporaryDirectory() as tmp:
            with heavy_lock.held(tmp, 'test holder'):
                with mock.patch.object(calibrate, 'calibrate',
                                       side_effect=AssertionError('lock came second')), \
                     mock.patch.object(sys, 'argv', ['calibrate.py', '--data-dir', tmp]):
                    with self.assertRaises(SystemExit) as ctx:
                        calibrate.main()
            self.assertIn('another heavy job', str(ctx.exception))

    def test_a_free_lock_lets_the_cli_through(self):
        """The lock is a fence, not a wall: with nobody holding it the CLI
        proceeds into the calibration (stubbed here — WorldTooThin is the
        cheapest true exit that proves the gate opened)."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(calibrate, 'calibrate',
                                   side_effect=calibrate.WorldTooThin('thin test world')), \
                 mock.patch.object(sys, 'argv', ['calibrate.py', '--data-dir', tmp]):
                with self.assertRaises(SystemExit) as ctx:
                    calibrate.main()
            self.assertIn('nothing to calibrate', str(ctx.exception))


class TheReplayScriptCarriesTheFences(unittest.TestCase):
    """docker/replay.sh scripts the 2026-08-24 hand-run. The caps are the
    point of its existence, so their absence is a test failure, not a style
    choice."""

    def setUp(self):
        self.script = (REPO / 'docker' / 'replay.sh').read_text(encoding='utf-8')

    def test_the_container_is_capped_with_swap_headroom(self):
        self.assertIn('--memory 6g', self.script)
        self.assertIn('--memory-swap 10g', self.script)

    def test_the_peak_is_always_measured(self):
        """A run without memory_receipt is a run whose regression is
        discovered by the OOM killer — the exact archaeology this repo just
        paid for once."""
        self.assertIn('memory_receipt.py', self.script)

    def test_latest_json_moves_only_behind_a_non_empty_document(self):
        copy = self.script.index('cp "$STATE/$OUT"')
        self.assertLess(self.script.index('[ -s "$STATE/$OUT" ]'), copy,
                        'the emptiness guard must run before the copy')
        self.assertLess(self.script.index('"--measure-only" ]'), copy,
                        'measure-only must be able to exit before any swap')

    def test_the_compose_services_inherit_the_fence(self):
        compose = (REPO / 'docker-compose.yml').read_text(encoding='utf-8')
        self.assertIn('mem_limit: 6g', compose)
        self.assertIn('memswap_limit: 10g', compose)


class ADeployNeverKillsAMeasurementSilently(unittest.TestCase):
    """2026-08-24 and 08-25: two deploys in a row recreated the scheduler
    mid-backplay and discarded multi-hour measurement nights, the only trace
    a missing 'done' line. The deploy waits; the button-presser is told."""

    def setUp(self):
        self.script = (REPO / 'docker' / 'deploy.sh').read_text(encoding='utf-8')

    def test_switch_to_checks_the_heavy_lock_before_recreating(self):
        guard = self.script.index('flock -n "$STATE/heavy.lock"')
        self.assertLess(self.script.index('switch_to() {'), guard,
                        'the guard lives in switch_to, after build and probe')
        self.assertLess(guard, self.script.index('docker compose', guard),
                        'the lock is checked before any compose recreate')

    def test_the_override_is_explicit_and_named(self):
        self.assertIn('TM_DEPLOY_FORCE', self.script)


class MeasurementNightsAreDurable(unittest.TestCase):
    """The other half of the same incident: rows were batched per question
    and lines printed only at the end, so a killed run left NOTHING — not in
    the ledger, not in the log. Now every completed measurement is a ledger
    row immediately, every line reaches the caller immediately, and a value
    measured under the current evidence is not re-measured."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        import gc
        self.addCleanup(gc.collect)
        self.paths = util.Paths(self.tmp.name, Path(self.tmp.name) / 'models')
        self.q = dataclasses.replace(Q)

    def _payload(self, recall):
        return {'counts': {'n_pos': 400, 'n_neg': 4000},
                'configurations': [{'name': '(committed)', 'recall': recall,
                                    'leakage': 0.01, 'volume': 0.05}]}

    def test_a_run_killed_mid_question_keeps_its_finished_measurements(self):
        """Baseline and first candidate land in the ledger even though the
        second candidate explodes (standing in for SIGTERM/timeout)."""
        calls = []

        def harness(paths, value, use, **kw):
            calls.append(value)
            if len(calls) == 3:
                raise RuntimeError('killed')
            return self._payload(0.5)

        with mock.patch.object(backplay, 'measure', side_effect=harness):
            backplay.run(self.paths, [self.q], today='2026-08-20')
        rows = ledger.read(self.paths.ledger_home, 'backplays')
        self.assertEqual([(r['value'], r['role']) for r in rows],
                         [(0.55, 'current'), (0.50, 'candidate')],
                         'two finished measurements survive the third dying')

    def test_lines_reach_the_caller_as_they_happen(self):
        emitted = []
        with mock.patch.object(backplay, 'measure',
                               side_effect=lambda *a, **k: self._payload(0.5)):
            returned = backplay.run(self.paths, [self.q], today='2026-08-20',
                                    emit=emitted.append)
        self.assertEqual(emitted, returned,
                         'emit gets exactly the lines the return carries')
        self.assertTrue(emitted, 'and there were lines')

    def test_a_value_measured_under_this_evidence_is_not_remeasured(self):
        """The resume: night one measures baseline + one candidate and dies;
        night two measures ONLY what is missing. A question too big for one
        night accumulates instead of restarting forever."""
        stamp = backplay.evidence_stamp(self.paths)
        ledger.append(self.paths.ledger_home, 'backplays', [
            {'ts': '2026-08-19T04:00:00+00:00', 'question': self.q.id,
             'knob': self.q.knob, 'value': 0.50, 'harness': 'judge',
             'benchmark': self.q.benchmark, 'stamp': stamp, 'role': 'candidate',
             'rejected': False, 'reason': 'survives', 'n_measurements': 1,
             'metric': 0.5, 'n': 400, 'leakage': 0.01}])
        measured = []

        def harness(paths, value, use, **kw):
            measured.append(value)
            return self._payload(0.5)

        with mock.patch.object(backplay, 'measure', side_effect=harness):
            lines = backplay.run(self.paths, [self.q], today='2026-08-20')
        self.assertEqual(measured, [0.55, 0.60],
                         '0.50 stands from night one; only baseline and the '
                         'unmeasured neighbour run')
        self.assertTrue(any('already measured under this evidence' in l
                            for l in lines), lines)

    def test_a_moved_stamp_remeasures_everything(self):
        """Resume never serves stale science: a row from other evidence does
        not count."""
        ledger.append(self.paths.ledger_home, 'backplays', [
            {'ts': '2026-08-19T04:00:00+00:00', 'question': self.q.id,
             'knob': self.q.knob, 'value': 0.50, 'harness': 'judge',
             'benchmark': self.q.benchmark, 'stamp': 'other evidence entirely',
             'role': 'candidate', 'rejected': False, 'reason': 'survives',
             'n_measurements': 1, 'metric': 0.5, 'n': 400, 'leakage': 0.01}])
        measured = []
        with mock.patch.object(backplay, 'measure',
                               side_effect=lambda p, v, u, **kw: measured.append(v) or self._payload(0.5)):
            backplay.run(self.paths, [self.q], today='2026-08-20')
        self.assertEqual(measured, [0.55, 0.50, 0.60])

    def test_the_timeout_outlives_the_slowest_measured_harness(self):
        """The replay is 3 h 34 m at the 118k-row store; a ceiling below that
        is not a hang-guard, it is a nightly guaranteed failure."""
        self.assertGreaterEqual(backplay.MEASURE_TIMEOUT, 6 * 3600)
        seen = {}

        def fake_run(argv, env=None, timeout=None, **kw):
            seen['timeout'] = timeout
            Path(argv[-1]).write_text('{}', encoding='utf-8')
            import subprocess as sp
            return sp.CompletedProcess(argv, 0, '', '')

        import subprocess as sp
        with mock.patch.object(sp, 'run', fake_run):
            backplay.measure(self.paths, 0.5, 'judge')
        self.assertEqual(seen['timeout'], backplay.MEASURE_TIMEOUT)


if __name__ == '__main__':
    unittest.main()
