"""The knob protocol, in code — PARAMETERS.md 8.

The operator asked not to be the instance that decides knob values ("I don't
trust myself"). So this module holds the two halves the software owns:

  * **it proposes** — for every knob with an open, filed question, the sweep
    at the *neighbouring* grid values, and one verdict line: `move up`,
    `move down`, `flat`, `hold (underpowered)` or `stop date reached`. It
    never edits a constant, and it never proposes a jump: one grid step per
    weekly cycle (§8.2), because a jump straight to a sweep's optimum is
    fitted to the benchmark.
  * **it blocks** — `gate_guard()` refuses delivery when the gate
    configuration this process resolved to is not the one the register
    records, which is what an edited constant without the three-file ritual
    (constant, receipt, register row) looks like from the outside.

    python knobs.py           # the weekly lines, same text the report carries

`LIVE` is empty on purpose. A knob is LIVE only while a filed question is
open (§8.1: question, metric, benchmark by blob hash, grid, stop date), and
filing one is a commit that adds a `Question` here. With none filed, the
weekly line says so in one sentence and nothing is swept — frozen knobs are
not re-swept every Monday (§8.3).

A question carries its own `run`, a callable returning one dict per grid
value. There is deliberately no general sweep engine: the two harnesses that
exist (`evidence.py --sweep`, `--judge`) already know how to measure the gate
on the frozen benchmark, and a third mechanism invented ahead of a real
question would be a guess about what the next question needs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import grading
import util

# ------------------------------------------------------------- the constants
# Printed under the weekly lines so a reader knows what the verdict meant.
MIN_CASES = 30          # below this denominator every verdict is `hold`
HARD_BAR = 0.022        # wrong-trade leakage a candidate may not breach (§3)
FLAT_TO_CLOSE = 2       # consecutive `flat` cycles that end a question (§8.4)

# The gate configuration the register records — PARAMETERS.md 2.2. Moving any
# knob that `evidence.RULES` or `GateConfig` covers moves this, and moving it
# is the third file of the ritual: constant, receipt comment, register row.
# `tests/test_parameters.py` reads it from here, so there is one value to
# update, not two.
EXPECTED_GATE_FINGERPRINT = '7931c8e9cd'


# ------------------------------------------------------------ declarations

@dataclass(frozen=True)
class Question:
    """One filed question — the entry ticket a knob needs to be LIVE (§8.1)."""

    id: str
    knob: str                   # 'module.CONSTANT', verbatim
    bucket: str                 # gate | competitiveness | delivery
    question: str               # one sentence, ending in a question mark
    metric: str                 # what `run` returns as 'metric'
    benchmark: str              # blob hash from evidence.benchmark_cases()
    grid: tuple                 # every value this knob may take, in order
    current: object             # the value in the code today; must be in grid
    opened: str                 # ISO date
    stop: str                   # ISO date — §8.4's backstop
    run: object = None          # () -> [{'value', 'metric', 'n', 'leakage'}]
    note: str = ''

    def neighbours(self):
        """The one step up and one step down this cycle may propose."""
        i = self.grid.index(self.current)
        return ([self.grid[i - 1]] if i > 0 else []
                ) + ([self.grid[i + 1]] if i + 1 < len(self.grid) else [])


LIVE: tuple = ()


def _validate(questions=None):
    """Checked at import, so a bad declaration fails the suite and never a
    Monday. Mirrors experiments.py, deliberately: same failure mode, same
    place to look."""
    qs = LIVE if questions is None else questions
    seen, buckets = set(), set()
    for q in qs:
        if not q.id or q.id in seen:
            raise ValueError(f'knobs: duplicate or empty question id {q.id!r}')
        seen.add(q.id)
        if q.bucket in buckets:
            raise ValueError(f'knobs: two live questions in bucket {q.bucket!r} '
                             '— one live knob per bucket (PARAMETERS.md 8.1)')
        buckets.add(q.bucket)
        if q.current not in q.grid:
            raise ValueError(f'knobs: {q.id} current {q.current!r} is not on its grid')
        if q.stop <= q.opened:
            raise ValueError(f'knobs: {q.id} stop date {q.stop} is not after {q.opened}')
        if not q.question.strip().endswith('?'):
            raise ValueError(f'knobs: {q.id} question must be a question')
    return True


_validate()


# ----------------------------------------------------------------- the verdict

def verdict(q, results, today, flat_streak=0):
    """One question's verdict from its sweep. Returns (verdict, detail).

    The ladder, in order — the first that applies wins:

      `stop date reached`  the backstop in §8.4; the question ends whatever
                           the numbers say, and the operator writes the receipt.
      `hold (underpowered)` fewer than MIN_CASES behind the current value: an
                           interval that wide cannot separate two grid points.
      `move up` / `move down`  a neighbour is clearly better — its Wilson
                           lower bound clears the current value's upper bound.
                           One step, never a jump, and never a candidate that
                           breaches the hard bar.
      `flat`               nobody is clearly better. Twice running ends the
                           question (§8.4) — that is a finding, not a failure.
    """
    by_value = {r['value']: r for r in results}
    cur = by_value.get(q.current)
    if today > q.stop:
        return 'stop date reached', f'opened {q.opened}, stop {q.stop} — write the receipt'
    if cur is None:
        return 'hold (underpowered)', 'the sweep returned nothing for the current value'
    if cur['n'] < MIN_CASES:
        return ('hold (underpowered)',
                f'{cur["n"]} cases behind {q.current} (need {MIN_CASES})')

    _, cur_hi = grading.wilson(round(cur['metric'] * cur['n']), cur['n'])
    best, best_lo, barred = None, None, []
    for value in q.neighbours():
        r = by_value.get(value)
        if r is None:
            continue
        if r.get('leakage') is not None and r['leakage'] > HARD_BAR:
            barred.append(f'{value} leaks {r["leakage"] * 100:.1f}%')
            continue
        lo, _ = grading.wilson(round(r['metric'] * r['n']), r['n'])
        if lo > cur_hi and (best is None or r['metric'] > by_value[best]['metric']):
            best, best_lo = value, lo

    bar_note = f'; barred: {", ".join(barred)}' if barred else ''
    if best is not None:
        direction = 'up' if q.grid.index(best) > q.grid.index(q.current) else 'down'
        return (f'move {direction}',
                f'{q.current} -> {best}: {q.metric} {by_value[best]["metric"]:.3f} '
                f'(lower bound {best_lo:.3f}) clears {q.current}\'s {cur_hi:.3f}{bar_note}')
    detail = (f'no neighbour of {q.current} is clearly better on {q.metric} '
              f'({cur["n"]} cases){bar_note}')
    if flat_streak + 1 >= FLAT_TO_CLOSE:
        return 'flat', detail + f' — {FLAT_TO_CLOSE} cycles running, close the question (§8.4)'
    return 'flat', detail


# -------------------------------------------------------------- the weekly line

def _state_path(paths):
    return paths.data / 'logs' / 'knobs_latest.json'


def weekly(paths, today=None, questions=None):
    """One line per live question, and the flat-streak bookkeeping §8.4 needs.

    Returns a list of strings — the report prints them, nothing writes a file
    of its own. With no question filed this is one quiet sentence, which is
    the honest state of a protocol whose entry ticket nobody has filed yet.
    """
    qs = LIVE if questions is None else questions
    today = today or util.now_utc().date().isoformat()
    if not qs:
        return ['- knobs: no live question — every knob frozen '
                '(PARAMETERS.md 8.1 files one)']

    state = util.read_json(_state_path(paths), {})
    lines, new_state = [], {}
    for q in qs:
        streak = int(state.get(q.id, {}).get('flat_streak', 0))
        try:
            results = list(q.run()) if q.run else []
        except Exception as e:                      # a sweep must not fail a cycle
            lines.append(f'- knob {q.knob}: sweep skipped ({e})')
            new_state[q.id] = {'flat_streak': streak, 'verdict': 'skipped'}
            continue
        v, detail = verdict(q, results, today, streak)
        weeks = max(0, (date.fromisoformat(today) - date.fromisoformat(q.opened)).days // 7)
        lines.append(f'- knob {q.knob} ({q.bucket}): **{v}** — {detail}; '
                     f'live {weeks}w, stop {q.stop}')
        new_state[q.id] = {'flat_streak': streak + 1 if v == 'flat' else 0,
                           'verdict': v, 'at': today}
    util.write_json(_state_path(paths), new_state)
    return lines


# ------------------------------------------------------------------ the guard

def gate_guard(paths, config=None):
    """(ok, lines). Not-ok means the gate this process resolved to is not the
    one the register records — an edited constant, or a stray environment
    variable in cron's environment (twenty of the evidence rules are env
    driven). The caller skips DELIVERY only: grading, training and prediction
    still run, so the week's data is never lost to this.

    When the expected configuration was itself recorded by an earlier cycle,
    the message names the knobs that differ rather than two opaque hashes —
    `record_gate_config` stored the whole configuration, so the diff is a
    lookup rather than git archaeology.
    """
    if config is None:
        import relevance as rel
        config = rel.DEFAULT_CONFIG
    if config.fingerprint == EXPECTED_GATE_FINGERPRINT:
        return True, []
    lines = [f'[knobs] GATE MISMATCH — resolved {config.fingerprint}, '
             f'register says {EXPECTED_GATE_FINGERPRINT} (PARAMETERS.md 2.2)']
    lines += [f'[knobs]   {d}' for d in _diff_against_recorded(paths, config)]
    lines.append('[knobs] delivery skipped. Either restore the value, or make the '
                 'move properly: constant, receipt comment, register row, '
                 'EXPECTED_GATE_FINGERPRINT — one commit (PARAMETERS.md 8.2).')
    return False, lines


def _diff_against_recorded(paths, config):
    """Which knobs differ from the recorded expected configuration, if a cycle
    ever recorded it. Never raises: this runs inside a failure message."""
    try:
        import ledger
        rows = {r.get('fingerprint'): r
                for r in ledger.read(paths.deliveries_home, 'gate_configs')}
        was = rows.get(EXPECTED_GATE_FINGERPRINT)
        if not was:
            return ['the register\'s configuration was never recorded here — '
                    'nothing to diff against; git is the record']
        now = config.as_dict()
        was_rules = dict(was.get('evidence_rules') or {})
        now_rules = dict(now.get('evidence_rules') or {})
        out = []
        for k in sorted(set(was) | set(now)):
            if k in ('evidence_rules', 'first_seen', 'seq', 'raw'):
                continue
            if was.get(k) != now.get(k) and k in now:
                out.append(f'{k}: {was.get(k)!r} -> {now.get(k)!r}')
        for k in sorted(set(was_rules) | set(now_rules)):
            if was_rules.get(k) != now_rules.get(k):
                out.append(f'evidence.{k}: {was_rules.get(k)!r} -> {now_rules.get(k)!r}')
        return out or ['no field differs — the hash covers something this diff does not']
    except Exception as e:
        return [f'(diff unavailable: {e})']


def main():
    import config as cfg
    paths = util.Paths(cfg.data_root(), cfg.models_root())
    for line in weekly(paths):
        print(line)
    ok, lines = gate_guard(paths)
    for line in lines:
        print(line)
    print(f'[knobs] gate configuration {"as recorded" if ok else "NOT as recorded"}; '
          f'thresholds: MIN_CASES {MIN_CASES}, hard bar {HARD_BAR * 100:.1f}%, '
          f'{FLAT_TO_CLOSE} flat cycles close a question')


if __name__ == '__main__':
    main()
