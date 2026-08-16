"""Backplay: the automated REJECTOR — PARAMETERS.md 10.

The asymmetry this rests on: a wrong rejection costs an improvement nobody
sees; a wrong promotion reaches customers. So this job may kill a candidate
knob value on its own, at night, and may never move one into production.

    python backplay.py                    # run every live question's candidates
    python backplay.py --show             # what has been rejected, and on what
    python backplay.py --question <id> --value 0.60

Per candidate: a SUBPROCESS with `TM_GATE_OVERRIDE` set to that value, so the
measurement runs under the candidate's own gate configuration without anybody
editing a constant, and stamps itself with its own fingerprint (util's lever).
The harness is the question's — `evidence.py --judge` for a gate knob today,
`rewind_all.py` for the end-to-end replay when a question asks for it. There
is no universal parser: a question knows how to read its own metric out of
its own harness.

**The rule is deliberately hard to satisfy.** Running several candidates over
several measurements guarantees some look bad by chance, and an eager
rejector would quietly delete good values. A candidate dies only when it
breaches the hard bar on a MAJORITY of measurements, or loses to the current
value with disjoint intervals on EVERY one. Anything else survives and is
simply proposed as usual.

Rejections are rows in the `backplays` ledger, stamped with the code
fingerprint, the benchmark and the day, and they EXPIRE (`REJECTION_TTL_DAYS`)
— a value killed in one market is not dead forever, exactly like the ROLLBACK
retirement in §8.1. `knobs.weekly()` reads them and stops proposing what is
still dead.

What it never does: edit a constant, promote anything, touch the real ledger
(the harnesses read the store; the as-of worlds are scratch and are pruned
after), or run while the weekly cycle holds the heavy lock.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import grading
import heavy_lock
import knobs
import ledger
import util

REPO = Path(__file__).resolve().parent

# A rejection stands this long before the candidate is measured again. Same
# reasoning as §8.1's 90-day ROLLBACK retirement: the market moves, and a
# permanent verdict from one season is a superstition.
REJECTION_TTL_DAYS = 90
# Of N measurements, how many must breach the hard bar for a kill.
MAJORITY = 0.5


def judge_harness(data_dir, out_path):
    """The gate harness: `evidence.py --judge`, whose table is the leakage
    number the hard bar is stated in. Returns the argv; the caller supplies
    the environment that makes it a candidate rather than the champion."""
    return [sys.executable, str(REPO / 'evidence.py'), '--judge',
            '--data-dir', str(data_dir), '--out', str(out_path)]


def replay_harness(data_dir, out_path):
    """The end-to-end harness: `rewind_all.py` replays every weekly cutoff in
    an as-of world. ~33 minutes and ~200 MB of scratch per run, so a question
    asks for it deliberately, not by default."""
    return [sys.executable, str(REPO / 'rewind_all.py'),
            '--data-dir', str(data_dir), '--out', str(out_path)]


HARNESSES = {'judge': judge_harness, 'replay': replay_harness}


def measure(paths, value, harness='judge', knob=None, timeout=7200):
    """Run one candidate under its own gate configuration. -> the payload dict.

    Raises on a non-zero exit: a harness that fell over measured nothing, and
    a rejection resting on a crash would be the worst kind of silent kill.
    """
    build = HARNESSES[harness]
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / 'payload.json'
        env = dict(os.environ)
        if knob is not None:
            env['TM_GATE_OVERRIDE'] = json.dumps({knob: value})
        proc = subprocess.run(build(paths.data, out), env=env, timeout=timeout,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or '').strip().splitlines()[-3:]
            raise RuntimeError(f'{harness} exited {proc.returncode}: '
                               + ' | '.join(tail))
        if not out.exists():
            raise RuntimeError(f'{harness} wrote no payload')
        return json.loads(out.read_text(encoding='utf-8'))


def rejects(current, candidate, hard_bar=knobs.HARD_BAR):
    """Does the evidence kill this candidate? -> (bool, reason).

    `current` and `candidate` are lists of measurements, one per cutoff or
    per harness run, each {'metric', 'n', 'leakage'}. Two ways to die, both
    conservative on purpose (see the module docstring):

      * the hard bar, breached on a majority of measurements — the operator's
        standing rule is that leakage above the bar is a refusal whatever
        recall it buys, so one clear pattern of breaches is enough;
      * a clean loss on EVERY measurement — the candidate's upper bound below
        the current value's lower bound each time. One bad cutoff is weather.
    """
    if not candidate:
        return False, 'no measurement'
    breaches = [m for m in candidate
                if m.get('leakage') is not None and m['leakage'] > hard_bar]
    if len(breaches) > MAJORITY * len(candidate):
        worst = max(m['leakage'] for m in breaches)
        return True, (f'leaks above {hard_bar * 100:.1f}% on '
                      f'{len(breaches)}/{len(candidate)} measurements '
                      f'(worst {worst * 100:.1f}%)')
    if current and len(current) == len(candidate):
        losses = 0
        for cur, cand in zip(current, candidate):
            _, cand_hi = grading.wilson(round(cand['metric'] * cand['n']), cand['n'])
            cur_lo, _ = grading.wilson(round(cur['metric'] * cur['n']), cur['n'])
            if cand_hi < cur_lo:
                losses += 1
        if losses == len(candidate) and losses > 1:
            return True, (f'worse than the current value on all {losses} '
                          'measurements, intervals disjoint')
    return False, 'survives'


def rejected_values(paths, question_id, today=None):
    """{value: reason} still standing today — what `knobs` must not propose."""
    today = today or util.now_utc().date().isoformat()
    horizon = (date.fromisoformat(today) - timedelta(days=REJECTION_TTL_DAYS)).isoformat()
    out = {}
    for row in ledger.read(paths.ledger_home, 'backplays'):
        if row.get('question') != question_id or not row.get('rejected'):
            continue
        if str(row.get('ts'))[:10] < horizon:
            continue                       # expired: measure it again
        out[row['value']] = f"{row.get('reason', 'rejected')} ({str(row['ts'])[:10]})"
    return out


def run(paths, questions=None, today=None, harness=None):
    """Measure every live question's candidates and record the verdicts."""
    questions = knobs.LIVE if questions is None else questions
    today = today or util.now_utc().date().isoformat()
    lines, rows = [], []
    for q in questions:
        try:
            base = measure(paths, q.current, harness or 'judge')
        except Exception as e:
            lines.append(f'[backplay] {q.knob}: baseline failed ({e}) — nothing rejected')
            continue
        cur_metrics = q.read(base) if hasattr(q, 'read') and q.read else []
        for value in q.neighbours():
            try:
                payload = measure(paths, value, harness or 'judge', knob=q.knob.split('.')[-1])
            except Exception as e:
                lines.append(f'[backplay] {q.knob}={value}: harness failed ({e}) — not rejected')
                continue
            cand = q.read(payload) if hasattr(q, 'read') and q.read else []
            killed, reason = rejects(cur_metrics, cand)
            rows.append({
                'ts': util.now_utc().isoformat(timespec='seconds'),
                'question': q.id, 'knob': q.knob, 'value': value,
                'harness': harness or 'judge',
                'gate_fingerprint': payload.get('gate_fingerprint'),
                'benchmark': q.benchmark,
                'rejected': bool(killed), 'reason': reason,
                'n_measurements': len(cand),
            })
            lines.append(f'[backplay] {q.knob}={value}: '
                         f'{"REJECTED" if killed else "survives"} — {reason}')
    if rows:
        ledger.append(paths.ledger_home, 'backplays', rows)
    if not questions:
        lines.append('[backplay] no live question — nothing to measure '
                     '(PARAMETERS.md 8.1 files one)')
    return lines


def show(paths, today=None):
    today = today or util.now_utc().date().isoformat()
    rows = ledger.read(paths.ledger_home, 'backplays')
    if not rows:
        return ['[backplay] nothing measured yet']
    lines = []
    for r in sorted(rows, key=lambda r: str(r.get('ts')), reverse=True)[:40]:
        live = r['value'] in rejected_values(paths, r.get('question'), today)
        mark = 'REJECTED' if r.get('rejected') else 'survives'
        if r.get('rejected') and not live:
            mark = 'rejected (EXPIRED)'
        lines.append(f'{str(r["ts"])[:10]}  {r.get("knob")}={r.get("value")}  '
                     f'{mark:18s} {r.get("reason", "")}')
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    import config as cfg
    ap.add_argument('--data-dir', default=cfg.data_root())
    ap.add_argument('--models-dir', default=cfg.models_root())
    ap.add_argument('--show', action='store_true', help='print the record, measure nothing')
    ap.add_argument('--harness', choices=sorted(HARNESSES), default=None)
    args = ap.parse_args()
    paths = util.Paths(args.data_dir, args.models_dir)
    if args.show:
        for line in show(paths):
            print(line)
        return
    # The harnesses open the embedding model and rewrite as-of worlds; the
    # weekly cycle must never meet one halfway. Waiting is right here — this
    # is a night job with nowhere to be.
    with heavy_lock.held(paths.data, 'backplay', wait=7200):
        for line in run(paths, harness=args.harness):
            print(line)


if __name__ == '__main__':
    main()
