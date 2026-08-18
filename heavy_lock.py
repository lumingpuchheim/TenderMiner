"""One lock, so the heavy jobs never run at the same time.

The cycle, the replay and the vocabulary backfill each peak between
1.0 and 3.0 GB (`doc/HOSTING.md` §0a). Any one of them fits a 4 GB machine.
Any two of them at once do not — and the loser is whichever allocates second,
which on a Monday morning is the cycle, halfway through writing a customer's
report. Cron cannot collide with itself; a person running a replay at 08:20
is what this exists for.

Deadlock is the obvious risk of adding a lock, and it is worse than the
crash it prevents: a cycle that hangs forever is a week with no delivery.
Five properties, each removing one way that could happen.

1. **The kernel holds the lock, not a file's contents.** A PID file outlives
   its owner — kill the container mid-cycle and every later Monday refuses to
   start. `flock`/`msvcrt` locks are attached to an open file descriptor, so
   the lock dies with the process no matter how it dies: SIGKILL, the
   OOM-killer, `docker stop`, power loss. There is no stale state to clean up
   and no "unlock" step that can be skipped.

2. **There is exactly one lock.** A deadlock cycle needs two locks taken in
   opposite orders. With a single lockfile there is no second one to invert,
   so the classic case is impossible by construction rather than by care.
   Adding a second lock to this repository would undo that.

3. **Every wait is bounded.** Nothing blocks indefinitely: the cycle and
   the delivery wait with a ceiling and then give up loudly, and the manual
   jobs do not wait at all.

4. **The policy is asymmetric, so nothing is silently skipped.** The cycle
   waits (`wait=3600`) because a missed Monday is a week of stale
   predictions; the delivery (`deliver.py`, 90 minutes later) waits the same
   way, so a cycle that overruns delays the mail rather than skipping it —
   and never reads predictions the cycle is still writing. The replay and
   the backfill fail immediately, because they are manual, cheap
   to repeat, and their operator is sitting right there.

5. **Only these four take it.** `app.py` must never wait behind batch work —
   a customer clicking a link at 08:20 on a Monday is not part of this. And
   each job takes the lock *before* it opens the database and releases it
   *after*, so the lock is always outermost: no holder ever waits on SQLite
   while something waiting on this lock holds it.
"""
from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

try:                                   # POSIX: the container, the server
    import fcntl

    def _try_lock(fh):
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
except ImportError:                    # Windows: the operator's laptop
    import msvcrt

    def _try_lock(fh):
        # `msvcrt.locking` locks a byte RANGE starting at the current file
        # position, unlike flock which locks the file. The lockfile is opened
        # append-mode, so without this seek each holder would lock a different
        # offset — a lock that excludes nobody. Caught by
        # tests/test_heavy_lock.py, which held it twice at once on Windows.
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

NAME = 'heavy.lock'
# A job the lock HOLDER starts as a subprocess (backplay runs the replay and
# the judge under its own lock, PARAMETERS.md 10-13) must not fail "Busy" on
# its parent's lock. The holder sets this in the child's environment; `held`
# then yields without locking. Only ever set by a holder for its children —
# a person exporting it by hand would defeat the lock, so its value is the
# holder's pid and the line says so.
INHERITED_ENV = 'TM_HEAVY_LOCK_HELD'


class Busy(RuntimeError):
    """Another heavy job holds the lock."""


def path_for(data_dir):
    return Path(data_dir) / NAME


@contextlib.contextmanager
def held(data_dir, what, wait=0, poll=5.0, log=print):
    """Hold the heavy-job lock for the duration of the block.

    `wait` is seconds to keep trying before giving up; 0 means fail at once.
    Raises `Busy` rather than blocking forever — see property 3 above.
    """
    lock = path_for(data_dir)
    inherited = os.environ.get(INHERITED_ENV)
    if inherited and inherited != str(os.getpid()):      # a CHILD of the holder, not the holder itself
        log(f'[lock] {what} runs under the lock held by pid {inherited}')
        yield lock
        return
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock, 'a+')              # never truncate: another holder has it open
    try:
        deadline = time.monotonic() + wait
        announced = False
        while True:
            if _try_lock(fh):
                break
            if time.monotonic() >= deadline:
                raise Busy(
                    f'another heavy job holds {lock} — {what} did not start. '
                    'The weekly cycle, the replay and the vocabulary backfill '
                    'cannot share a 4 GB machine (doc/HOSTING.md 0a).')
            if not announced:
                log(f'[lock] waiting for {lock} before {what} '
                    f'(up to {wait:.0f}s)')
                announced = True
            time.sleep(min(poll, max(0.0, deadline - time.monotonic())))
        fh.write(f'{what} pid {os.getpid()}\n')
        fh.flush()
        os.environ[INHERITED_ENV] = str(os.getpid())      # children inherit the lock
        try:
            yield lock
        finally:
            os.environ.pop(INHERITED_ENV, None)
    finally:
        # Closing the descriptor releases the lock; doing it explicitly keeps
        # the release visible rather than leaving it to the garbage collector.
        fh.close()
