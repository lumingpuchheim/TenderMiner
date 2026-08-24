"""memory_receipt.py — run a command and print its peak memory, to the console.

The tool behind doc/MEMORY_BUDGET.md's numbers, committed so the measurement
can be repeated when the store grows instead of re-invented (it was
re-invented once already; the 2026-08-13 harness was never checked in, and on
2026-08-24 the only way to know the replay's new peak was to let the OOM
killer find it).

    python memory_receipt.py -- python rewind_all.py --step 21 --out /tmp/r.json
    python memory_receipt.py --label replay -- python evidence.py --judge ...

Samples the whole process TREE (the command plus every descendant — backplay
spawns its harnesses as subprocesses) at 4 Hz, prints a progress line every
minute and one final line with the peak. Progress goes to stderr so a command
whose stdout is a document stays clean; the child's own streams pass through
untouched. Exit code is the child's.

Linux /proc only, no dependencies — it runs where the memory question lives
(the container, the server), not on the laptop.
"""
import subprocess
import sys
import time
from pathlib import Path

INTERVAL = 0.25          # 4 Hz, the MEMORY_BUDGET.md sampling rate
REPORT_EVERY = 60.0      # seconds between progress lines


def tree_pids(root):
    """The root pid and every live descendant, via /proc children lists."""
    pids, queue = [], [root]
    while queue:
        pid = queue.pop()
        pids.append(pid)
        for task in Path(f'/proc/{pid}/task').glob('*/children'):
            try:
                queue += [int(c) for c in task.read_text().split()]
            except OSError:          # the task exited between glob and read
                pass
    return pids


def tree_rss_kb(root):
    total = 0
    for pid in tree_pids(root):
        try:
            for line in open(f'/proc/{pid}/status'):
                if line.startswith('VmRSS:'):
                    total += int(line.split()[1])
                    break
        except OSError:              # exited mid-walk; its memory is gone too
            pass
    return total


def main(argv):
    label = 'mem'
    if argv[:1] == ['--label']:
        label, argv = argv[1], argv[2:]
    if argv[:1] == ['--']:
        argv = argv[1:]
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    child = subprocess.Popen(argv)
    t0 = time.monotonic()
    peak, last_report = 0, t0
    while child.poll() is None:
        peak = max(peak, tree_rss_kb(child.pid))
        now = time.monotonic()
        if now - last_report >= REPORT_EVERY:
            print(f'[{label}] t+{now - t0:.0f}s peak {peak // 1024} MB',
                  file=sys.stderr, flush=True)
            last_report = now
        time.sleep(INTERVAL)
    print(f'[{label}] PEAK {peak // 1024} MB over {time.monotonic() - t0:.0f}s '
          f'— {" ".join(argv[:3])}{" ..." if len(argv) > 3 else ""} '
          f'(exit {child.returncode})', file=sys.stderr, flush=True)
    return child.returncode


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
