"""Backplay: the automated REJECTOR — PARAMETERS.md 10.

The asymmetry this rests on: a wrong rejection costs an improvement nobody
sees; a wrong promotion reaches customers. So this job may kill a candidate
knob value on its own, at night, and may never move one into production.

    python backplay.py                    # every filed question's candidates
    python backplay.py --self-check       # prove the wiring, one second, no harness
    python backplay.py --show             # what has been rejected, and what expired
    python backplay.py --knob evidence.NOMINATION_BAR --grid 0.50,0.55,0.60 --current 0.55

Scheduled: `docker/backplay.sh`, Sunday 04:00 (docker/crontab), so Monday's
report carries fresh rejections. With no filed question it exits in a second
and says so — which is how you know it is still wired in.

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
        use = harness or getattr(q, 'harness', None) or 'judge'
        try:
            base = measure(paths, q.current, use)
        except Exception as e:
            lines.append(f'[backplay] {q.knob}: baseline failed ({e}) — nothing rejected')
            continue
        cur_metrics = q.read(base) if getattr(q, 'read', None) else []
        for value in q.neighbours():
            try:
                payload = measure(paths, value, use, knob=q.knob.split('.')[-1])
            except Exception as e:
                lines.append(f'[backplay] {q.knob}={value}: harness failed ({e}) — not rejected')
                continue
            cand = q.read(payload) if getattr(q, 'read', None) else []
            killed, reason = rejects(cur_metrics, cand)
            rows.append({
                'ts': util.now_utc().isoformat(timespec='seconds'),
                'question': q.id, 'knob': q.knob, 'value': value,
                'harness': use,
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


def judge_read(payload, row='(committed)'):
    """The default reader for the `judge` harness: the shipped configuration's
    row, with the denominators its rates rest on.

    Under `TM_GATE_OVERRIDE` that row IS the candidate, which is what makes one
    generic reader enough for knobs the judge table reads off live module state
    (`NOMINATION_BAR`, `BORDERLINE_ADMIT_P`). A knob the table enumerates
    itself instead — the witness minimum K, whose rows are named `K>=1/2/3` —
    needs its own reader, and that is why `Question.read` exists.
    """
    counts = payload.get('counts') or {}
    for cfg in payload.get('configurations') or []:
        if row in cfg['name']:
            return [{'metric': cfg['recall'], 'n': counts.get('n_pos') or 0,
                     'leakage': cfg['leakage']}]
    return []


def _ad_hoc(knob, grid, current, metric='recall', harness='judge'):
    """A question that is NOT filed — for looking, not for moving.

    `--knob` on the command line builds one of these so the whole path can be
    exercised on real data without a knob becoming LIVE. Nothing it measures
    can promote anything; the rejections it writes are real, because a real
    measurement of a real candidate is a real result.
    """
    values = tuple(_coerce(v) for v in grid.split(','))
    return knobs.Question(
        id=f'ad-hoc:{knob}', knob=knob, bucket='gate',
        question=f'Ad-hoc: is a neighbouring value of {knob} defensible?',
        metric=metric, benchmark='(ad-hoc)', grid=values,
        current=_coerce(current), opened=util.now_utc().date().isoformat(),
        stop='2099-01-01', read=judge_read, harness=harness)


def _coerce(text):
    text = str(text).strip()
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    return text


def self_check(paths):
    """Prove the wiring in a second, without a harness, a store or a question.

    Not a substitute for a real measurement — it runs the rule, the ledger and
    the weekly line over numbers this function makes up, and says so. It exists
    because "is this thing actually connected" deserves an answer that does not
    cost half an hour of CPU.
    """
    lines = ['[self-check] synthetic numbers, no harness, no store — wiring only']
    killed, why = rejects([{'metric': .50, 'n': 400}] * 4,
                          [{'metric': .70, 'n': 400, 'leakage': .031},
                           {'metric': .70, 'n': 400, 'leakage': .028},
                           {'metric': .70, 'n': 400, 'leakage': .030},
                           {'metric': .70, 'n': 400, 'leakage': .010}])
    lines.append(f'[self-check] a majority of hard-bar breaches -> '
                 f'{"REJECTED" if killed else "survives"}: {why}')
    killed, why = rejects([{'metric': .50, 'n': 400}] * 4,
                          [{'metric': .70, 'n': 400, 'leakage': .031},
                           {'metric': .70, 'n': 400, 'leakage': .010},
                           {'metric': .70, 'n': 400, 'leakage': .012},
                           {'metric': .70, 'n': 400, 'leakage': .010}])
    lines.append(f'[self-check] a minority of breaches       -> '
                 f'{"REJECTED" if killed else "survives"}: {why}')
    live = rejected_values(paths, 'nomination-bar')
    lines.append(f'[self-check] rejections standing today for nomination-bar: '
                 f'{live or "none"}')
    lines.append(f'[self-check] filed questions: {len(knobs.LIVE)} '
                 f'(cron runs this job Sunday 04:00 either way)')
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    import config as cfg
    ap.add_argument('--data-dir', default=cfg.data_root())
    ap.add_argument('--models-dir', default=cfg.models_root())
    ap.add_argument('--show', action='store_true', help='print the record, measure nothing')
    ap.add_argument('--self-check', action='store_true', dest='self_check',
                    help='prove the wiring on synthetic numbers, in a second')
    ap.add_argument('--knob', metavar='module.CONSTANT',
                    help='measure this knob ad hoc, without filing a question')
    ap.add_argument('--grid', help='with --knob: comma-separated values, in order')
    ap.add_argument('--current', help='with --knob: the value in the code today')
    ap.add_argument('--harness', choices=sorted(HARNESSES), default=None)
    args = ap.parse_args()
    paths = util.Paths(args.data_dir, args.models_dir)
    if args.self_check:
        for line in self_check(paths):
            print(line)
        return
    if args.show:
        for line in show(paths):
            print(line)
        return
    if args.knob:
        if not (args.grid and args.current):
            raise SystemExit('--knob needs --grid and --current')
        q = _ad_hoc(args.knob, args.grid, args.current,
                    harness=args.harness or 'judge')
        print(f'[backplay] ad hoc: {q.knob} at {q.current}, '
              f'candidates {q.neighbours()} — one harness run each')
        with heavy_lock.held(paths.data, 'backplay (ad hoc)', wait=7200):
            for line in run(paths, [q], harness=args.harness):
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
