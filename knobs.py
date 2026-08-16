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

**Who files the questions — the queue (PARAMETERS.md 11).** The operator
asked that no value and no grid ever be hand-made. So `KNOBS` is the
register's list of knobs that may be LIVE, each with a program-owned grid
(`lo`, `hi`, `step`), and `queue()` keeps exactly one question open per
bucket: it opens the next knob in the bucket's rotation, with the value the
code holds today as `current`, a stop date `STOP_WEEKS` out, and closes it on
§8.4's conditions — stop date, flat twice, or every neighbour rejected —
writing the receipt and moving on. `LIVE` is still the place for a question a
person files by hand (empty), and such a question takes its bucket's slot.

A queue question has no `run` of its own: its numbers are what `backplay.py`
measured at night, read back from the `backplays` ledger. So one pipeline —
backplay measures, `weekly()` reads and proposes, the operator accepts or
holds — and nothing in it is a hand-picked value.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import date, timedelta

import grading
import util

# ------------------------------------------------------------- the constants
# Printed under the weekly lines so a reader knows what the verdict meant.
MIN_CASES = 30          # below this denominator every verdict is `hold`
HARD_BAR = 0.022        # wrong-trade leakage a candidate may not breach (§3)
FLAT_TO_CLOSE = 2       # consecutive `flat` cycles that end a question (§8.4)
STOP_WEEKS = 8          # a queue question's backstop: opened + this (§8.4)

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
    # payload -> [{'metric', 'n', 'leakage'}], one per measurement: how this
    # question reads its own harness's document (PARAMETERS.md 10). Only
    # `backplay.py` calls it, and only a question knows which row of which
    # table its metric lives in — which is why there is no universal parser.
    read: object = None
    harness: str = 'judge'      # backplay.HARNESSES
    note: str = ''

    def neighbours(self):
        """The one step up and one step down this cycle may propose."""
        i = self.grid.index(self.current)
        return ([self.grid[i - 1]] if i > 0 else []
                ) + ([self.grid[i + 1]] if i + 1 < len(self.grid) else [])


LIVE: tuple = ()


# ------------------------------------------------------------- the knobs
#
# The register's LIVE-eligible knobs (PARAMETERS.md 2.7), each with the grid
# the program walks. `lo`/`hi`/`step` are the software's own bounds — a
# grid, not a value: nobody picks a number, the queue walks one step per
# cycle from wherever the code stands. Order within a bucket is the rotation
# order. Only knobs the override lever reaches (`TM_GATE_OVERRIDE`, §10.1) and
# the judge harness measures are here; the competitiveness knobs need the
# replay harness AND a lever that reaches `--threshold`, which does not exist
# yet — §11.4 says so rather than listing them and measuring nothing.

@dataclass(frozen=True)
class Knob:
    knob: str            # 'module.CONSTANT', verbatim — the override key is the CONSTANT
    bucket: str
    lo: object
    hi: object
    step: object
    question: str
    metric: str = 'recall'
    harness: str = 'judge'
    note: str = ''
    values: tuple = None     # an explicit grid — (False, True) for a switch

    def grid(self):
        """lo..hi in steps, rounded so 0.775 + 2*0.025 is 0.825 and not
        0.8250000000000001 — the grid must contain the code's own value."""
        if self.values is not None:
            return tuple(self.values)
        out, i = [], 0
        while True:
            v = self.lo + i * self.step
            if isinstance(self.step, float):
                v = round(v, 6)
            if v > self.hi:
                break
            out.append(v)
            i += 1
        return tuple(out)

    def current(self):
        """The value the code holds today, read from the module — never
        stored anywhere the queue could disagree with the code."""
        module, name = self.knob.rsplit('.', 1)
        return getattr(importlib.import_module(module), name)


# Not here: `relevance.EVIDENCE_NOMINATION_MIN`. The queue's first run
# (2026-08-16, PARAMETERS.md 11.5) measured K=1/2/3 under their own
# fingerprints and got identical recall and leakage to the last digit —
# `passed = nominated and convicting`, and with CONVICTION_NOMINATES on,
# convicting already nominates, so K cannot change a verdict. DEAD while that
# switch is on; it comes back the day the switch goes off — and that switch
# is the first step of the rotation below, so the program will say whether
# "off" is defensible instead of anyone guessing.

def _switch(knob, question, note=''):
    return Knob(knob, 'gate', False, True, 1, question, note=note,
                   values=(False, True))


def _grid(knob, lo, hi, step, question, note=''):
    return Knob(knob, 'gate', lo, hi, step, question, note=note)


# Every verdict-affecting rule the override lever reaches (evidence.RULES and
# relevance's switches), each with the program's own grid. Order is the
# rotation order: the switch that decides the gate's shape first, then the
# conviction rules, then the lexicon and the dictionaries. One knob per
# night; the whole rotation is ~5 weeks, then it starts again on whatever
# evidence moved. Not here: CORE_TITLE_UNION (a string), NOMINATION_BAR and
# BORDERLINE_ADMIT_P (dead / placeholder while their switches are off),
# EVIDENCE_NOMINATION_MIN (above).
KNOBS = (
    _switch('relevance.CONVICTION_NOMINATES',
            'Does what convicts also nominate — is the gate "convicting alone" '
            'the right gate, or should the witness rule and route 1 decide?',
            note='phase 8k; K and the nomination layer are inert while on'),
    _grid('relevance.DEFAULT_MIN_CODE_HARD', 0.775, 0.875, 0.025,
            'Where does the hard-code similarity bar sit best — does a step '
            'either way buy recall or cost leakage?', note='configuration F'),
    _grid('evidence.CONVICT_BODY_MIN', 1, 4, 1,
            'How many distinct keywords must a body-only conviction rest on?'),
    _switch('evidence.LONE_TITLE_NEEDS_CODE',
            'Must a lone non-core title keyword have the code channel agree?', note='9g'),
    _switch('evidence.TITLE_CONTRADICTS_BODY',
            'Does a foreign trade in the title veto a body conviction?', note='9i'),
    _switch('evidence.TRADE_READ_FORGIVES',
            'May the embedding lift the foreign-title veto?', note='9j, off'),
    _switch('evidence.CORE_TITLE_CONVICTS',
            'Does a recurring trade root in the title convict on its own?', note='8o'),
    _grid('evidence.CORE_SHARE', 0.3, 0.7, 0.1,
            'How often must a root recur across the references to be core?'),
    _switch('evidence.CORE_SINGLE_TITLE', 'May a single title root be core?'),
    _switch('evidence.CORE_NEED_BEARING', 'Must a core root be a bearing word?'),
    _switch('evidence.CORE_FAMILY', 'Do inflection families count as one core root?'),
    _switch('evidence.CORE_TITLE_FALLBACK', 'Does the title fall back to core roots?'),
    _switch('evidence.CORE_HYSTERESIS', 'Should core roots stick once found?', note='off'),
    _switch('relevance.SIMILARITY_NOMINATES',
            'Should whole-document similarity nominate again (route 2)?', note='8i, off'),
    _switch('evidence.WIDE_NOMINATION', 'May the wide root lexicon nominate?', note='8n'),
    _switch('evidence.NAME_KEYWORDS', 'Do keywords derived from the firm name count?'),
    _switch('evidence.ROOT_LEXICON', 'Does the committed root vocabulary feed the lexicon?'),
    _switch('evidence.TRADE_ROOTS', 'Are trade roots read from cpv_trade_roots.txt?'),
    _grid('evidence.MIN_WITNESSES', 1, 3, 1, 'How many witnesses is evidence at all?'),
    _grid('evidence.SYN_THRESHOLD', 0.70, 0.90, 0.05,
            'How close must a word embedding be to count as a synonym?'),
    _grid('evidence.TYPO_MIN_LEN', 6, 10, 1, 'From what length may a keyword match with a typo?'),
    _grid('evidence.MIN_STEM_LEN', 4, 8, 1, 'How short may a stem be before it hits by accident?'),
    _grid('evidence.MAX_KEYWORDS', 15, 35, 5, 'How many keywords may a profile carry?'),
    _grid('evidence.MAX_DOC_FREQ', 0.01, 0.04, 0.01,
            'How common may a keyword be in the store and still discriminate?'),
    _grid('evidence.LABEL_DF_MAX', 3, 7, 1, 'How many CPV labels may a definitional word name?'),
    _grid('evidence.WORD_MIN_REF_SHARE', 0.24, 0.44, 0.05,
            'What share of the references must carry a word?'),
    _switch('evidence.TRADE_DICTS', 'Do the trade dictionaries run at all?'),
    _switch('evidence.DICT_TRUSTED_ONLY', 'Only trusted codes into a dictionary?', note='off'),
    _grid('evidence.DICT_CODE_SHARE', 0.24, 0.44, 0.05,
            "What share of a trade's lots must carry a dictionary word?"),
    _switch('evidence.DICT_VOTE', 'Do dictionaries vote for a signature?'),
    _grid('evidence.DICT_VOTE_MARGIN', 0.3, 0.7, 0.1, 'How close to the top must a root poll?'),
    _grid('evidence.DICT_VOTE_MAX', 4, 8, 1, 'How many roots may a signature hold?'),
    _grid('evidence.DICT_MIN_IN', 0.05, 0.20, 0.05, 'How often inside the trade must a word occur?'),
    _grid('evidence.DICT_MIN_RATIO', 4.0, 12.0, 2.0, 'How many times more inside than outside?'),
    _grid('evidence.DICT_MIN_LOTS', 10, 30, 5, 'How many coded lots keep a dictionary?'),
    _grid('evidence.DICT_MIN_BUYERS', 1, 3, 1, 'How many buyers must a dictionary word have?'),
    _grid('evidence.DICT_MIN_BUYER_SHARE', 0.05, 0.20, 0.05,
            "What share of the trade's buyers must a word have?"),
    _switch('evidence.BUYER_DIVERSITY', 'Is buyer diversity required of a dictionary word?'),
    _grid('evidence.DICT_MAX_WORDS', 20, 40, 5, 'How many words may a trade dictionary hold?'),
)


def question_from(t, opened, stop=None, current=None):
    """The queue's Question for a knob — id stable per knob so a
    rejection outlives one opening (they expire on their own, §10.4)."""
    cur = t.current() if current is None else current
    grid = t.grid()
    if cur not in grid:
        raise ValueError(f'knobs: {t.knob} holds {cur!r}, which is not on its '
                         f'grid {grid} — the grid or the constant is wrong')
    return Question(
        id=f'auto:{t.knob}', knob=t.knob, bucket=t.bucket, question=t.question,
        metric=t.metric, benchmark='(judge harness names it)', grid=grid,
        current=cur, opened=opened,
        stop=stop or (date.fromisoformat(opened)
                      + timedelta(weeks=STOP_WEEKS)).isoformat(),
        harness=t.harness, note=t.note)


def _queue_path(paths):
    return paths.data / 'logs' / 'knobs_queue.json'


def read_queue(paths):
    return util.read_json(_queue_path(paths), {'open': {}, 'closed': []})


def queue(paths, today=None, knobs_list=None, filed=None):
    """The live questions: hand-filed ones (`LIVE`) plus, for every bucket
    without one, the queue's own. Opens the next knob of a bucket's rotation
    when nothing is open there — the knob after the last one closed, wrapping
    round — and persists that. Never closes; `weekly()` does, on a receipt."""
    today = today or util.now_utc().date().isoformat()
    knobs_list = KNOBS if knobs_list is None else knobs_list
    filed = LIVE if filed is None else filed
    state = read_queue(paths)
    questions = list(filed)
    taken = {q.bucket for q in filed}
    changed = False
    for bucket in dict.fromkeys(t.bucket for t in knobs_list):
        if bucket in taken:
            continue
        rotation = [t for t in knobs_list if t.bucket == bucket]
        entry = state['open'].get(bucket)
        if not entry or entry.get('knob') not in {t.knob for t in rotation}:
            last = next((c['knob'] for c in reversed(state['closed'])
                         if c.get('bucket') == bucket), None)
            names = [t.knob for t in rotation]
            nxt = rotation[(names.index(last) + 1) % len(rotation)] \
                if last in names else rotation[0]
            entry = {'knob': nxt.knob, 'opened': today}
            state['open'][bucket] = entry
            changed = True
        t = next(t for t in rotation if t.knob == entry['knob'])
        questions.append(question_from(t, entry['opened'], entry.get('stop')))
    if changed:
        util.write_json(_queue_path(paths), state)
    return questions


def close_question(paths, q, verdict_, detail, today, proposed=None):
    """Write the receipt into the queue and free the bucket. The next call
    to `queue()` opens the next knob of the rotation. `proposed` is the value
    a `move` names — what the forward channel (shadow.py) then judges."""
    state = read_queue(paths)
    if state['open'].get(q.bucket, {}).get('knob') != q.knob:
        return False                       # not open here: nothing to close, no duplicate row
    state['closed'].append({'knob': q.knob, 'bucket': q.bucket, 'opened': q.opened,
                            'closed': today, 'current': q.current, 'proposed': proposed,
                            'verdict': verdict_, 'receipt': detail})
    del state['open'][q.bucket]
    util.write_json(_queue_path(paths), state)
    return True


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
    seen = set()
    for t in KNOBS:
        if t.knob in seen or len(t.grid()) < 2 or t.bucket not in ('gate', 'competitiveness', 'delivery'):
            raise ValueError(f'knobs: knob {t.knob} is duplicated, has no grid, '
                             f'or names no bucket')
        seen.add(t.knob)
    return True


_validate()


# ----------------------------------------------------------------- the verdict

def verdict(q, results, today, flat_streak=0):
    """One question's verdict from its sweep. Returns (verdict, detail).

    The grid, in order — the first that applies wins:

      `stop date reached`  the backstop in §8.4; the question ends whatever
                           the numbers say, and the operator writes the receipt.
      `hold (underpowered)` fewer than MIN_CASES behind the current value: an
                           interval that wide cannot separate two grid points.
      `move up` / `move down`  a neighbour is clearly better — its Wilson
                           lower bound clears the current value's upper bound.
                           One step, never a jump, and never a candidate that
                           breaches the hard bar.
      `no effect`              every measured step identical — the knob cannot
                           move a verdict under the current switches. Closes
                           at once; the register marks the knob DEAD.
      `flat`               nobody is clearly better. Twice running ends the
                           question (§8.4) — that is a finding, not a failure.
    """
    by_value = {r['value']: r for r in results}
    cur = by_value.get(q.current)
    if today > q.stop:
        return 'stop date reached', f'opened {q.opened}, stop {q.stop} — write the receipt'
    if cur is None:
        return 'hold (underpowered)', 'nothing measured for the current value yet'
    if cur['n'] < MIN_CASES:
        return ('hold (underpowered)',
                f'{cur["n"]} cases behind {q.current} (need {MIN_CASES})')
    # Every measured step identical to the last digit: the knob cannot move a
    # verdict under the current switches — DEAD, not flat. Found on the first
    # queue run (EVIDENCE_NOMINATION_MIN under CONVICTION_NOMINATES): no
    # point holding the bucket two cycles to learn it twice.
    measured = [by_value[v] for v in q.neighbours() if v in by_value]
    if measured and all((r['metric'], r['n'], r.get('leakage'))
                        == (cur['metric'], cur['n'], cur.get('leakage')) for r in measured):
        return ('no effect', f'{[q.current] + [r["value"] for r in measured]} give identical '
                         f'{q.metric} {cur["metric"]:.3f} on {cur["n"]} — the knob is DEAD '
                         f'under the current switches; mark it so in the register')

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


def judge_question(paths, q, today, streak=0):
    """One question's verdict from what backplay measured: (verdict, detail,
    results, killed). Shared by `weekly()` and `backplay.run()` so the line
    Monday prints and the line the night job prints are the same line."""
    import backplay          # lazy: backplay imports this module (PARAMETERS.md 10)
    results = list(q.run()) if q.run else backplay.measurements(paths, q, today)
    # A candidate the night job has already killed is not proposed, and the
    # line says who killed it — the operator sees the rejection, not a
    # silently shorter grid (§10).
    killed = backplay.rejected_values(paths, q.id, today)
    results = [r for r in results if r['value'] not in killed]
    v, detail = verdict(q, results, today, streak)
    if v == 'hold (underpowered)' and not q.run and not results:
        detail = 'not measured yet — backplay measures it on its next run'
    if killed:
        detail += ('; backplay rejected ' +
                   ', '.join(f'{value} ({why})' for value, why in sorted(killed.items())))
    return v, detail, results, killed


def close_if_answered(paths, q, v, detail, results, killed, today):
    """A queue question closes the moment it is answered — the night it was
    measured, not the Monday after — and the queue moves on. Answered means:
    no effect; a clear move (recorded as a standing proposal); flat with
    every neighbour measured or rejected; every neighbour rejected; the stop
    date. Not answered: `hold` (a neighbour still unmeasured, a harness that
    failed) — it stays until measured or until the stop date. Returns the
    closing verdict or None. Hand-filed questions are never closed here."""
    if not q.id.startswith('auto:'):
        return None
    measured = {r['value'] for r in results} | set(killed)
    answered = all(n in measured for n in q.neighbours())
    proposed = None
    if v in ('stop date reached', 'no effect'):
        v_close = v
    elif q.neighbours() and all(n in killed for n in q.neighbours()):
        v_close = 'every neighbour rejected — current stands'
    elif v.startswith('move ') and answered:
        v_close = f'proposal: {v}'
        proposed = proposed_value(q, detail)
    elif v == 'flat' and answered:
        v_close = 'flat — current stands'
    else:
        return None
    return v_close if close_question(paths, q, v_close, detail, today, proposed) else None


def proposed_value(q, detail):
    """The grid value a `move` detail names (`2 -> 3: ...`), as the grid's
    own object — so a float proposal stays a float and a switch a bool."""
    head = detail.split(':', 1)[0]
    text = head.split('->', 1)[1].strip() if '->' in head else ''
    return next((v for v in q.grid if str(v) == text), None)


def standing_proposals(paths, knobs_list=None):
    """Closed queue questions whose verdict was a move and whose knob still
    holds the value it held then — proposals nobody has acted on. Read from
    the queue's closed list against the modules' live values."""
    knobs_list = KNOBS if knobs_list is None else knobs_list
    by_knob = {t.knob: t for t in knobs_list}
    out, seen = [], set()
    for c in reversed(read_queue(paths)['closed']):
        if not str(c.get('verdict', '')).startswith('proposal') or c['knob'] in seen:
            continue
        seen.add(c['knob'])
        t = by_knob.get(c['knob'])
        if t is not None and t.current() == c['current']:
            out.append(c)
    return out


def weekly(paths, today=None, questions=None):
    """One line per live question, the questions the queue closed this week,
    the proposals standing, and the flat-streak bookkeeping §8.4 needs.

    Returns a list of strings — the report prints them, nothing writes a file
    of its own. With no question at all this is one quiet sentence.
    """
    today = today or util.now_utc().date().isoformat()
    qs = queue(paths, today) if questions is None else questions
    if not qs:
        return ['- knobs: no live question — every knob frozen '
                '(PARAMETERS.md 8.1 files one)']

    state = util.read_json(_state_path(paths), {})
    lines, new_state = [], {}
    for q in qs:
        streak = int(state.get(q.id, {}).get('flat_streak', 0))
        try:
            v, detail, results, killed = judge_question(paths, q, today, streak)
        except Exception as e:                      # a sweep must not fail a cycle
            lines.append(f'- knob {q.knob}: sweep skipped ({e})')
            new_state[q.id] = {'flat_streak': streak, 'verdict': 'skipped'}
            continue
        weeks = max(0, (date.fromisoformat(today) - date.fromisoformat(q.opened)).days // 7)
        lines.append(f'- knob {q.knob} ({q.bucket}): **{v}** — {detail}; '
                     f'grid {grid_text(q, results, killed)}; '
                     f'live {weeks}w, stop {q.stop}')
        new_state[q.id] = {'flat_streak': streak + 1 if v == 'flat' else 0,
                           'verdict': v, 'at': today}
        if q.id.startswith('auto:'):
            v_close = close_if_answered(paths, q, v, detail, results, killed, today)
        elif v == 'flat' and streak + 1 >= FLAT_TO_CLOSE:
            v_close = 'flat twice — close the question (§8.4)'   # hand-filed: the author closes
        else:
            v_close = None
        if v_close:
            lines[-1] += f' — **closed** ({v_close}); the next knob opens tonight'
    util.write_json(_state_path(paths), new_state)
    if questions is None:
        week_ago = (date.fromisoformat(today) - timedelta(days=7)).isoformat()
        closed = [c for c in read_queue(paths)['closed'] if c.get('closed', '') > week_ago]
        for c in closed:
            lines.append(f'- closed this week: {c["knob"]} at {c["current"]} — '
                         f'{c["verdict"]} ({c["receipt"]})')
        import shadow            # lazy: shadow imports this module (PARAMETERS.md 12)
        for c in standing_proposals(paths):
            status, detail, _ = shadow.verdict(paths, c['knob'], c.get('proposed'), today)
            lines.append(f'- PROPOSAL standing since {c["closed"]}: {c["knob"]} '
                         f'{c["current"]} -> {c.get("proposed")} ({c["receipt"]}); '
                         f'forward: **{status}** — {detail}')
    return lines


def grid_text(q, results, killed):
    """The grid in one glance: `1 ✗ | [2] .81 | 3 ✓ .84 | 4 ·` — rejected,
    current, survives with its metric, not yet measured."""
    by_value = {r['value']: r for r in results}
    cells = []
    for v in q.grid:
        if v == q.current:
            r = by_value.get(v)
            cells.append(f'[{v}]' + (f' {r["metric"]:.3f}' if r else ''))
        elif v in killed:
            cells.append(f'{v} x')
        elif v in by_value:
            cells.append(f'{v} ok {by_value[v]["metric"]:.3f}')
        else:
            cells.append(f'{v} .')
    return ' | '.join(cells)


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
          f'{FLAT_TO_CLOSE} flat cycles close a question, queue stop {STOP_WEEKS}w; '
          f'`python backplay.py --show` for the grids and the record')


if __name__ == '__main__':
    main()
