"""The heavy-job lock: it excludes, it releases, and it never hangs.

Every test here is a way the lock could strand the Monday cycle, which is the
only reason to be careful about a lock at all — see heavy_lock's docstring.
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import heavy_lock                                              # noqa: E402

REPO = Path(__file__).resolve().parents[1]


class Excludes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='tm-lock-')

    def test_second_holder_is_refused_at_once(self):
        with heavy_lock.held(self.dir, 'first'):
            t0 = time.monotonic()
            with self.assertRaises(heavy_lock.Busy):
                with heavy_lock.held(self.dir, 'second'):
                    self.fail('two holders at once')
            # "fail fast" has to mean fast: a manual job that waits without
            # saying so is indistinguishable from one that hung.
            self.assertLess(time.monotonic() - t0, 1.0)

    def test_a_child_of_the_holder_runs_under_the_parents_lock(self):
        """backplay holds the lock and starts the replay as a subprocess
        (PARAMETERS.md 13); the child must not fail Busy on its parent's
        lock — while an unrelated process still must."""
        code = ('import sys; sys.path.insert(0, %r); import heavy_lock; '
                'f = heavy_lock.held(%r, "child"); f.__enter__(); print("ok")'
                % (str(REPO), self.dir))
        with heavy_lock.held(self.dir, 'parent'):
            child = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
            self.assertEqual(child.stdout.strip().splitlines()[-1], 'ok', child.stderr)
            env = {k: v for k, v in os.environ.items() if k != heavy_lock.INHERITED_ENV}
            stranger = subprocess.run([sys.executable, '-c', code], capture_output=True,
                                      text=True, env=env)
            self.assertNotEqual(stranger.returncode, 0)
            self.assertIn('Busy', stranger.stderr)
        # the holder's own environment is clean again after the block
        self.assertNotIn(heavy_lock.INHERITED_ENV, os.environ)

    def test_lock_is_free_again_after_the_block(self):
        with heavy_lock.held(self.dir, 'first'):
            pass
        with heavy_lock.held(self.dir, 'second'):
            pass

    def test_wait_gives_up_instead_of_hanging(self):
        """Property 3: bounded. A cycle that waits forever is a missed week."""
        with heavy_lock.held(self.dir, 'first'):
            t0 = time.monotonic()
            with self.assertRaises(heavy_lock.Busy):
                with heavy_lock.held(self.dir, 'waiter', wait=1.0, poll=0.1,
                                     log=lambda *_: None):
                    self.fail('acquired a held lock')
            waited = time.monotonic() - t0
        self.assertGreaterEqual(waited, 1.0)
        self.assertLess(waited, 5.0)

    def test_waiter_acquires_once_the_holder_leaves(self):
        holder = subprocess.Popen(
            [sys.executable, '-c',
             'import sys, time; sys.path.insert(0, %r); import heavy_lock; '
             'f = heavy_lock.held(%r, "holder"); f.__enter__(); '
             'print("held", flush=True); time.sleep(1.5)'
             % (str(REPO), self.dir)],
            stdout=subprocess.PIPE, text=True)
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        with heavy_lock.held(self.dir, 'waiter', wait=30, poll=0.1,
                             log=lambda *_: None):
            pass
        holder.wait(timeout=10)

    def test_killed_holder_does_not_strand_the_lock(self):
        """Property 1, the one a PID file gets wrong.

        SIGKILL leaves no chance to clean up. The lock must still be free,
        or one OOM-killed cycle disables every Monday that follows.
        """
        holder = subprocess.Popen(
            [sys.executable, '-c',
             'import sys, time; sys.path.insert(0, %r); import heavy_lock; '
             'f = heavy_lock.held(%r, "holder"); f.__enter__(); '
             'print("held", flush=True); time.sleep(60)'
             % (str(REPO), self.dir)],
            stdout=subprocess.PIPE, text=True)
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        holder.kill()
        holder.wait(timeout=10)
        with heavy_lock.held(self.dir, 'after the kill'):
            pass
        self.assertTrue(heavy_lock.path_for(self.dir).exists(),
                        'the lockfile itself is not the lock and stays put')

    def test_lockfile_is_never_truncated(self):
        """It is opened 'a+' by every holder: truncating a file another
        process holds open is how a lock quietly becomes two locks."""
        with heavy_lock.held(self.dir, 'first'):
            pass
        before = heavy_lock.path_for(self.dir).read_text(encoding='utf-8')
        with heavy_lock.held(self.dir, 'second'):
            pass
        after = heavy_lock.path_for(self.dir).read_text(encoding='utf-8')
        self.assertTrue(after.startswith(before))
        self.assertIn('first', after)
        self.assertIn('second', after)


class OnlyTheThree(unittest.TestCase):
    """Property 5: the customer-facing app never waits behind batch work."""

    def test_app_does_not_take_the_lock(self):
        self.assertNotIn('heavy_lock', (REPO / 'app.py').read_text(encoding='utf-8'))

    def test_the_three_heavy_jobs_do(self):
        for name in ('loop.py', 'rewind_all.py', 'embed_vocab.py'):
            self.assertIn('heavy_lock', (REPO / name).read_text(encoding='utf-8'),
                          f'{name} can collide with the others unguarded')

    def test_there_is_exactly_one_lock_name(self):
        """Property 2: one lock, so no ordering to invert."""
        users = [p for p in REPO.glob('*.py')
                 if 'heavy_lock.' in p.read_text(encoding='utf-8') and p.name != 'heavy_lock.py']
        for p in users:
            self.assertNotIn('heavy_lock.NAME =', p.read_text(encoding='utf-8'))
        self.assertEqual(heavy_lock.NAME, 'heavy.lock')


if __name__ == '__main__':
    unittest.main()
