"""The guards added after 2026-08-23/24: every heavy job is fenced.

That weekend, in order: a manual calibration ran beside a replay and the
kernel's global OOM killer chose the victim (19:23); the replay was memcg-
killed one cutoff from the end (23:26); and a 04:00 backplay starved the
Monday delivery (the clearance for that one is tested in test_backplay).
These tests pin the two remaining fences: the calibration CLI takes the
heavy lock, and the scripted replay launcher carries the memory caps.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import calibrate                                                  # noqa: E402
import heavy_lock                                                 # noqa: E402

REPO = Path(__file__).resolve().parent.parent


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


if __name__ == '__main__':
    unittest.main()
