"""A/B arms between models — the operator decides, the software says when.

Spec: doc/EXPERIMENTS.md. The declaration of every experiment lives here in
code (EXPERIMENTS); its state (open/closed, which arm delivers, the closing
decision) lives in the `experiment` table; the arm-vs-arm outcomes live in the
`arm_grades` ledger; the verdict is computed on request and never stored.

    python experiments.py                       # open + closed, one verdict line each
    python experiments.py show <id>             # per-arm tables, cumulative and weekly
    python experiments.py deliver <id> <arm>    # switch the delivering arm (rewrites models/CURRENT)
    python experiments.py close <id> --winner <arm|none> --note "..."

The rule (spec §0): both arms predict the same lots before the award exists;
the software flags "ready"; the operator decides. Nothing here switches a
model on its own, at the deadline or ever.
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

import config
import db
import ledger
import single_bidder as sb

# ------------------------------------------------------------- the constants
# Printed at the bottom of the page and in `show`, so a reader knows what
# "ready" meant when the page was read (spec §6).
MIN_PAIRED = 100        # paired lots below which the verdict is `collecting`
MIN_FLAGGED = 30        # flagged-and-graded lots per arm below which: `collecting`
P_LEANING = 0.80        # posterior P(best) at or above which: `leaning <label>`
P_READY = 0.95          # ... at or above which, with tripwires passed: `ready`
DRAWS = 20_000          # Beta posterior draws; fixed seed so the page is reproducible
SEED = 42
PRIOR = 0.5             # Jeffreys prior on each arm's flagged precision


# ------------------------------------------------------------ declarations

@dataclass(frozen=True)
class Arm:
    id: str
    label: str                              # verbatim in every output
    feature_build: str = 'default'          # one of single_bidder.FEATURE_BUILDS
    catboost: tuple = ()                    # ((param, value), ...) forwarded to make_model
    guard_exempt: tuple = ()                # columns assert_pure_one_hot may let exceed the cap

    @property
    def overrides(self):
        return dict(self.catboost)

    @property
    def suffix(self):
        return f'-{self.id}'


@dataclass(frozen=True)
class Experiment:
    id: str
    question: str
    opened: str                             # ISO date, first Monday both arms train
    deadline: str                           # ISO date, the backstop
    arms: tuple
    default_delivering: str

    def arm(self, arm_id):
        for a in self.arms:
            if a.id == arm_id:
                return a
        raise KeyError(f'{self.id}: no arm {arm_id!r} '
                       f'(arms: {", ".join(a.id for a in self.arms)})')

    def label(self, arm_id):
        return self.arm(arm_id).label


EXPERIMENTS = (
    Experiment(
        id='cpv-additional-encoding',
        question='Which encoding of the additional-CPV codes sorts 0/1-bidder '
                 'lots from the rest better, on real predictions?',
        opened='2026-08-18', deadline='2026-11-30',
        arms=(Arm('onehot', 'one hot', feature_build='default'),
              Arm('ts', 'target statistics', feature_build='cpv_additional_combination',
                  guard_exempt=('cpv_additional__cpv2', 'cpv_additional__cpv3',
                                'cpv_additional__cpv4'))),
        default_delivering='onehot',
    ),
)


class DeclarationError(ValueError):
    pass


def _iso(s, what):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        raise DeclarationError(f'{what} must be an ISO date, got {s!r}')


def validate(experiments=EXPERIMENTS):
    """Checked at import time (below), so a bad declaration fails the test
    suite and never a Monday cycle."""
    import re
    ident = re.compile(r'^[a-z0-9-]+$')
    seen = set()
    for e in experiments:
        if not ident.match(e.id):
            raise DeclarationError(f'experiment id {e.id!r} must match [a-z0-9-]+')
        if e.id in seen:
            raise DeclarationError(f'duplicate experiment id {e.id!r}')
        seen.add(e.id)
        if not e.arms or len(e.arms) < 2:
            raise DeclarationError(f'{e.id}: at least two arms')
        arm_ids, labels = set(), set()
        for a in e.arms:
            if not ident.match(a.id):
                raise DeclarationError(f'{e.id}: arm id {a.id!r} must match [a-z0-9-]+')
            if a.id in arm_ids:
                raise DeclarationError(f'{e.id}: duplicate arm id {a.id!r}')
            if not a.label or a.label in labels:
                raise DeclarationError(f'{e.id}: arm labels must be non-empty and unique')
            arm_ids.add(a.id)
            labels.add(a.label)
            if a.feature_build not in sb.FEATURE_BUILDS:
                raise DeclarationError(
                    f'{e.id}/{a.id}: unknown feature_build {a.feature_build!r}; '
                    f'known: {", ".join(sb.FEATURE_BUILDS)}')
            if not isinstance(a.catboost, tuple) or not isinstance(a.guard_exempt, tuple):
                raise DeclarationError(f'{e.id}/{a.id}: catboost and guard_exempt are tuples')
        if e.default_delivering not in arm_ids:
            raise DeclarationError(f'{e.id}: default_delivering {e.default_delivering!r} is not an arm')
        if _iso(e.deadline, f'{e.id}.deadline') <= _iso(e.opened, f'{e.id}.opened'):
            raise DeclarationError(f'{e.id}: deadline must be after opened')
    return {e.id: e for e in experiments}


DECLARED = validate()


# ------------------------------------------------------------------ state

def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _rows(con):
    return {r['id']: dict(r) for r in con.execute('SELECT * FROM experiment')}


def state(data_dir):
    """{id: state row} for every experiment that has one — declared or not."""
    con = db.connect(data_dir, create=False)
    if con is None:
        return {}
    try:
        return _rows(con)
    finally:
        con.close()


def delivering_map(data_dir):
    """{experiment id: delivering arm} for every state row, open or closed —
    what loop.grade uses to tell a delivering arm's prediction from a
    shadow's on the same lot, during and after a trial."""
    return {i: r['delivering'] for i, r in state(data_dir).items()}


def ensure_state(data_dir, today):
    """Create the state row for every declared experiment whose `opened` date
    has come and that has none yet: open, delivering = default. Returns the
    ids created. A row is never deleted here; a declaration removed from code
    leaves its row, which the page marks "declaration missing".

    Never CREATES a database: a JSONL-only home (a rewind sandbox, a test dir)
    has no experiments — creating tendermining.db there would silently flip
    every ledger in that home over to the database (ledger.storage)."""
    con = db.connect(data_dir, create=False)
    if con is None:
        return []
    have = _rows(con)
    created = []
    for e in DECLARED.values():
        if e.id in have or str(today) < e.opened:
            continue
        con.execute('INSERT INTO experiment (id, status, delivering, opened, deadline, '
                    'decision, created_at, updated_at) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)',
                    (e.id, 'open', e.default_delivering, e.opened, e.deadline, _now(), _now()))
        created.append(e.id)
    con.commit()
    con.close()
    return created


def open_experiment(data_dir, today=None):
    """The one open, declared experiment (with its state row), or None.

    One at a time, on purpose: two open experiments would need two delivering
    arms feeding one set of customers, which is not a thing. A second open
    row is refused loudly rather than half-run."""
    st = state(data_dir)
    open_ids = [i for i, r in st.items() if r['status'] == 'open' and i in DECLARED]
    if not open_ids:
        return None
    if len(open_ids) > 1:
        raise RuntimeError(f'more than one open experiment ({", ".join(sorted(open_ids))}); '
                           f'close all but one: python experiments.py close <id> ...')
    e = DECLARED[open_ids[0]]
    return e, st[e.id]


def set_delivering(data_dir, exp_id, arm_id, models_dir):
    """Switch the delivering arm: state row + models/CURRENT — nothing else."""
    e = DECLARED[exp_id]
    e.arm(arm_id)                                   # KeyError if unknown
    con = db.connect(data_dir, create=False)
    row = _rows(con).get(exp_id) if con else None
    if row is None:
        raise RuntimeError(f'{exp_id} has no state row yet (opens {e.opened})')
    con.execute('UPDATE experiment SET delivering = ?, updated_at = ? WHERE id = ?',
                (arm_id, _now(), exp_id))
    con.commit()
    con.close()
    champ = arm_champion(models_dir, arm_id)
    if champ:
        current_path(models_dir).parent.mkdir(parents=True, exist_ok=True)
        current_path(models_dir).write_text(champ + '\n', encoding='utf-8')
    return champ


def close(data_dir, exp_id, winner, note, verdict_now):
    """Record the decision; status -> closed. Does NOT switch delivery."""
    e = DECLARED[exp_id]
    if winner not in (None, 'none'):
        e.arm(winner)
    con = db.connect(data_dir, create=False)
    row = _rows(con).get(exp_id) if con else None
    if row is None:
        raise RuntimeError(f'{exp_id} has no state row')
    if row['status'] != 'open':
        raise RuntimeError(f'{exp_id} is already {row["status"]}')
    decision = {'winner': None if winner in (None, 'none') else winner,
                'note': note or '', 'closed_at': _now(),
                'verdict_at_close': verdict_now}
    con.execute('UPDATE experiment SET status = ?, decision = ?, updated_at = ? WHERE id = ?',
                ('closed', json.dumps(decision, ensure_ascii=False), _now(), exp_id))
    con.commit()
    con.close()
    return decision


# ---------------------------------------------------- the cycle's arm plan

@dataclass
class Plan:
    """What this cycle trains, scores and grades. `experiment` None means the
    implicit single arm — exactly today's cycle."""
    experiment: object = None
    arms: tuple = ()
    delivering: str = None

    @property
    def is_trial(self):
        return self.experiment is not None

    def is_delivering(self, arm):
        return arm is None or arm.id == self.delivering


def plan(data_dir, today):
    """The plan for a cycle on `today` (an ISO date string or date)."""
    ensure_state(data_dir, today)
    got = open_experiment(data_dir, today)
    if got is None:
        return Plan()
    e, row = got
    return Plan(experiment=e, arms=e.arms, delivering=row['delivering'])


# ------------------------------------------------------ models and pointers

def current_path(models_dir):
    return Path(models_dir) / 'CURRENT'


def arm_current_path(models_dir, arm_id):
    return Path(models_dir) / 'arms' / arm_id / 'CURRENT'


def arm_champion(models_dir, arm_id):
    p = arm_current_path(models_dir, arm_id)
    return p.read_text(encoding='utf-8').strip() if p.exists() else None


def shadow_models(models_dir, data_dir):
    """Model ids that never delivered: every registry row with an arm stamp
    whose arm is not (or was not) its experiment's delivering arm. The report
    shortlist and the drift monitor leave these out (loop.py)."""
    reg = Path(models_dir) / 'registry.jsonl'
    if not reg.exists():
        return set()
    deliv = delivering_map(data_dir)
    out = set()
    for line in reg.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get('arm') and deliv.get(r.get('experiment')) != r['arm']:
            out.add(r['model_id'])
    return out


def latest_candidate(models_dir, arm_id):
    """The newest registry row for this arm (None if it never trained), with
    its meta.json's gate merged in — the tripwire text the page shows."""
    reg = Path(models_dir) / 'registry.jsonl'
    if not reg.exists():
        return None
    last = None
    for line in reg.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get('arm') == arm_id:
            last = r
    if last is None:
        return None
    meta_p = Path(models_dir) / last['model_id'] / 'meta.json'
    try:
        meta = json.loads(meta_p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        meta = {}
    gate = meta.get('gate') or {}
    return {**last, 'failures': gate.get('failures', []), 'checks': gate.get('checks', {}),
            'warnings': gate.get('warnings', [])}


# ------------------------------------------------------------- arm grades

def arm_grade_rows(exp, labeled, lot_meta, by_lot, threshold, now_iso):
    """Rows for the `arm_grades` ledger: per arm, per awarded lot the arm had
    predicted, the arm's LAST prediction before the award — the same rule
    loop.grade applies for the delivering arm's record.

    labeled: {(procedure_id, lot_id): (label, award_pub, award_pub_nr, n_tenders)}
    by_lot: {(procedure_id, lot_id): [prediction rows]} — rows carry
            'experiment' and 'arm' (predict_open stamps them); rows without an
            arm are from before the experiment and never count (spec §0: no
            re-grading of history).
    """
    out = []
    for lot, rows in by_lot.items():
        if lot not in labeled:
            continue
        label, award_pub, _nr, n_tenders = labeled[lot]
        meta = lot_meta.get(lot, {})
        for arm in exp.arms:
            mine = sorted((r for r in rows
                           if r.get('experiment') == exp.id and r.get('arm') == arm.id),
                          key=lambda r: r['ts'])
            if not mine:
                continue
            before = [r for r in mine if str(r['ts'])[:10] <= award_pub[:10]]
            last = (before or mine)[-1]
            flag = bool(last['score'] >= last.get('threshold', threshold))
            out.append({
                'experiment': exp.id, 'arm': arm.id,
                'procedure_id': lot[0], 'lot_id': lot[1],
                'model': last['model'], 'ts': last['ts'],
                'score': last['score'], 'threshold': last.get('threshold', threshold),
                'flag': flag, 'tier': last.get('tier'),
                'label': label, 'n_tenders': n_tenders, 'award_pub': award_pub,
                'cpv3': meta.get('cpv3'), 'place_nuts3': meta.get('place_nuts3'),
                'graded_at': now_iso,
            })
    return out


def graded_lots(home, exp_id):
    """{arm_id: {(procedure_id, lot_id)}} already in the ledger."""
    out = {}
    for r in ledger.read(home, 'arm_grades'):
        if r['experiment'] == exp_id:
            out.setdefault(r['arm'], set()).add((r['procedure_id'], r['lot_id']))
    return out


def rows_by_arm(home, exp_id):
    out = {}
    for r in ledger.read(home, 'arm_grades'):
        if r['experiment'] == exp_id:
            out.setdefault(r['arm'], []).append(r)
    return out


# ---------------------------------------------------------------- verdict

def paired(by_arm, arm_ids):
    """The rows of each arm restricted to lots graded for EVERY arm."""
    sets = [{(r['procedure_id'], r['lot_id']) for r in by_arm.get(a, [])} for a in arm_ids]
    common = set.intersection(*sets) if sets else set()
    return {a: [r for r in by_arm.get(a, []) if (r['procedure_id'], r['lot_id']) in common]
            for a in arm_ids}, common


def arm_stats(rows):
    """flag_stats plus the top-tier precision — loop's functions, reused."""
    import loop
    if not rows:
        return None
    fs = loop.flag_stats(rows)
    high = [r for r in rows if r.get('tier') == 'HIGH']
    fs['high_n'] = len(high)
    fs['high_precision'] = (sum(r['label'] for r in high) / len(high)) if high else None
    return fs


def posterior_best(hits_flagged, draws=DRAWS, seed=SEED):
    """P(each arm has the highest flagged precision) from independent
    Beta(hits+½, flagged-hits+½) posteriors. hits_flagged: [(hits, flagged)]."""
    rng = np.random.default_rng(seed)
    sample = np.column_stack([rng.beta(h + PRIOR, max(f - h, 0) + PRIOR, draws)
                              for h, f in hits_flagged])
    best = np.argmax(sample, axis=1)
    return [float(np.mean(best == i)) for i in range(len(hits_flagged))]


def verdict(exp, row, by_arm, today, tripwire_ok=None):
    """The software's reading of the evidence — spec §6. Never stored."""
    arm_ids = [a.id for a in exp.arms]
    pr, common = paired(by_arm, arm_ids)
    stats = {a: arm_stats(pr[a]) for a in arm_ids}
    graded = {a: len(by_arm.get(a, [])) for a in arm_ids}
    n_paired = len(common)
    deadline_reached = str(today) >= row['deadline']
    days_left = (date.fromisoformat(row['deadline']) - date.fromisoformat(str(today)[:10])).days
    out = {'experiment': exp.id, 'n_paired': n_paired, 'graded': graded, 'stats': stats,
           'delivering': row['delivering'], 'deadline': row['deadline'],
           'days_left': days_left, 'deadline_reached': deadline_reached,
           'p': None, 'winner': None, 'status': 'collecting'}
    enough = (n_paired >= MIN_PAIRED and
              all(stats[a] and stats[a]['flagged'] >= MIN_FLAGGED for a in arm_ids))
    if not enough:
        return out
    probs = posterior_best([(stats[a]['tp'], stats[a]['flagged']) for a in arm_ids])
    out['p_by_arm'] = dict(zip(arm_ids, probs))
    # ties break in favour of the delivering arm: the burden of proof is on the shadow
    order = sorted(arm_ids, key=lambda a: (-out['p_by_arm'][a], a != row['delivering']))
    best, p = order[0], out['p_by_arm'][order[0]]
    out['winner'], out['p'] = best, p
    if p >= P_READY:
        ok = (tripwire_ok or {}).get(best)
        out['status'] = 'ready' if ok else 'leaning'
        out['ready_blocked_by_tripwire'] = (ok is False)
    elif p >= P_LEANING:
        out['status'] = 'leaning'
    else:
        out['status'] = 'no difference yet'
        out['winner'] = None
    return out


def _pct(x):
    return '—' if x is None else f'{x:.2f}'


def status_line(exp, v):
    """The one line — cycle log, report, dashboard, page (spec §5)."""
    arm_ids = [a.id for a in exp.arms]
    lab = {a.id: a.label for a in exp.arms}
    if v['status'] == 'collecting':
        head = 'collecting'
    elif v['status'] == 'ready':
        head = f"ready: {lab[v['winner']]} better, {v['p'] * 100:.0f}%"
    elif v['status'] == 'leaning':
        head = f"leaning {lab[v['winner']]} — P {v['p']:.2f}"
        if v.get('ready_blocked_by_tripwire'):
            head += ' (tripwire failing on that arm)'
    else:
        head = 'no difference yet'
    if v['deadline_reached']:
        head = 'DEADLINE REACHED — ' + head
    graded = ' / '.join(f"{lab[a]} {v['graded'][a]}" for a in arm_ids)
    prec = ' vs '.join(_pct(v['stats'][a]['precision'] if v['stats'][a] else None)
                       for a in arm_ids)
    base = next((v['stats'][a]['base'] for a in arm_ids if v['stats'][a]), None)
    return (f"{exp.id}: {head} — {v['n_paired']} paired lots ({graded}), "
            f"flagged precision {prec} (base {_pct(base)}), "
            f"delivering {lab[v['delivering']]}, deadline {v['deadline']} "
            f"({v['days_left']} d)")


def weekly(by_arm, arm_ids):
    """{iso_week: {arm: flag_stats}} over PAIRED lots, by award publication week."""
    pr, _ = paired(by_arm, arm_ids)
    weeks = {}
    for a in arm_ids:
        for r in pr[a]:
            d = date.fromisoformat(str(r['award_pub'])[:10])
            y, w, _ = d.isocalendar()
            weeks.setdefault(f'{y}-W{w:02d}', {}).setdefault(a, []).append(r)
    return {wk: {a: arm_stats(rows.get(a, [])) for a in arm_ids}
            for wk, rows in sorted(weeks.items())}


# ------------------------------------------------------- reading it all

def read_verdict(data_dir, models_dir, exp, row, today):
    by_arm = rows_by_arm(data_dir, exp.id)
    trip = {}
    for a in exp.arms:
        c = latest_candidate(models_dir, a.id)
        trip[a.id] = None if c is None else not c['failures']
    return verdict(exp, row, by_arm, today, tripwire_ok=trip), by_arm


def overview(data_dir, models_dir, today):
    """Everything the CLI and the page show."""
    st = state(data_dir)
    out = {'open': [], 'closed': [], 'missing': [], 'today': str(today)}
    for exp_id, row in sorted(st.items()):
        e = DECLARED.get(exp_id)
        if e is None:
            out['missing'].append(row)
            continue
        if row['status'] == 'open':
            v, by_arm = read_verdict(data_dir, models_dir, e, row, today)
            cands = {a.id: latest_candidate(models_dir, a.id) for a in e.arms}
            out['open'].append({'exp': e, 'row': row, 'verdict': v,
                                'line': status_line(e, v),
                                'weekly': weekly(by_arm, [a.id for a in e.arms]),
                                'candidates': cands})
        else:
            dec = json.loads(row['decision']) if row.get('decision') else {}
            out['closed'].append({'exp': e, 'row': row, 'decision': dec})
    return out


# ------------------------------------------------------------ the page

def _esc(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;'))


def _ci(fs, key):
    ci = fs.get(key) if fs else None
    return f' [{ci[0]:.2f}–{ci[1]:.2f}]' if ci else ''


def _arm_table(exp, stats, graded):
    arm_ids = [a.id for a in exp.arms]
    head = ''.join(f'<th>{_esc(exp.label(a))}</th>' for a in arm_ids)
    rows = []

    def line(name, cells):
        rows.append(f'<tr><td>{_esc(name)}</td>' + ''.join(f'<td>{c}</td>' for c in cells) + '</tr>')

    line('graded lots (all)', [graded.get(a, 0) for a in arm_ids])
    line('paired lots', [stats[a]['n'] if stats[a] else 0 for a in arm_ids])
    line('positives / base rate', [f"{stats[a]['positives']} / {stats[a]['base']:.2f}" if stats[a] else '—'
                                   for a in arm_ids])
    line('flagged', [stats[a]['flagged'] if stats[a] else '—' for a in arm_ids])
    line('flagged precision', [(_pct(stats[a]['precision']) + _ci(stats[a], 'precision_ci'))
                               if stats[a] else '—' for a in arm_ids])
    line('recall', [_pct(stats[a]['recall']) if stats[a] else '—' for a in arm_ids])
    line('beats base', [('yes' if stats[a]['beats_base'] else 'no') if stats[a] else '—'
                        for a in arm_ids])
    line('HIGH tier n / precision', [f"{stats[a]['high_n']} / {_pct(stats[a]['high_precision'])}"
                                     if stats[a] else '—' for a in arm_ids])
    return f'<table><tr><th></th>{head}</tr>{"".join(rows)}</table>'


def _weekly_table(exp, wk):
    arm_ids = [a.id for a in exp.arms]
    if not wk:
        return '<p class="muted">no paired lots yet</p>'
    head = ''.join(f'<th>{_esc(exp.label(a))} n / flagged / precision</th>' for a in arm_ids)
    rows = []
    for week, per in wk.items():
        cells = []
        for a in arm_ids:
            fs = per.get(a)
            cells.append(f"{fs['n']} / {fs['flagged']} / {_pct(fs['precision'])}" if fs else '—')
        rows.append(f'<tr><td>{week}</td>' + ''.join(f'<td>{c}</td>' for c in cells) + '</tr>')
    return f'<table><tr><th>award week</th>{head}</tr>{"".join(rows)}</table>'


def _background_table(exp, cands):
    rows = []
    for a in exp.arms:
        c = cands.get(a.id)
        if c is None:
            rows.append(f'<tr><td>{_esc(a.label)}</td><td colspan="4">not trained yet</td></tr>')
            continue
        trip = '; '.join(c['failures']) if c['failures'] else 'all passed'
        pr = c.get('val_pr_auc')
        rows.append(f"<tr><td>{_esc(a.label)}</td><td>{_esc(c['model_id'])}</td>"
                    f"<td>{'promoted' if c.get('promoted') else 'kept champion'}</td>"
                    f"<td>{'—' if pr is None else f'{pr:.4f}'}</td><td>{_esc(trip)}</td></tr>")
    return ('<table><tr><th>arm</th><th>latest candidate</th><th></th>'
            '<th>val PR-AUC</th><th>tripwires</th></tr>' + ''.join(rows) + '</table>')


def render_html(data_dir, models_dir, today, last_cycle=None):
    """The body of the hidden page (spec §9). app.py wraps it in its page()."""
    ov = overview(data_dir, models_dir, today)
    parts = [f'<h1>Experimente</h1>',
             f'<p class="muted">Stand: letzter Zyklus {_esc(last_cycle or "unbekannt")}, '
             f'heute {_esc(ov["today"])}. Die Zahlen ändern sich nur, wenn ein Zyklus läuft.</p>']
    parts.append('<h2>Offen</h2>')
    if not ov['open']:
        parts.append('<p class="muted">kein offenes Experiment</p>')
    for o in ov['open']:
        e, v = o['exp'], o['verdict']
        red = ' style="color:#b00"' if v['deadline_reached'] else ''
        parts.append(f'<h3>{_esc(e.id)}</h3><p>{_esc(e.question)}</p>'
                     f'<p>geöffnet {_esc(o["row"]["opened"])} → Deadline '
                     f'<span{red}>{_esc(o["row"]["deadline"])} ({v["days_left"]} Tage)</span>, '
                     f'liefernder Arm: <b>{_esc(e.label(v["delivering"]))}</b></p>'
                     f'<p><b>{_esc(o["line"])}</b></p>')
        if v['status'] == 'collecting':
            parts.append('<p class="muted">Vergaben werden 1–3 Monate nach der Bekanntmachung '
                         'veröffentlicht; „collecting“ ist in den ersten Wochen normal.</p>')
        parts.append(_arm_table(e, v['stats'], v['graded']))
        parts.append('<details><summary>pro Woche (Vergabe-Woche, gepaarte Lose)</summary>'
                     + _weekly_table(e, o['weekly']) + '</details>')
        parts.append('<details><summary>Trainings-Hintergrund (nie die Entscheidungszeile)</summary>'
                     + _background_table(e, o['candidates']) + '</details>')
    parts.append('<h2>Geschlossen</h2>')
    if not ov['closed']:
        parts.append('<p class="muted">—</p>')
    else:
        rows = []
        for c in ov['closed']:
            e, d = c['exp'], c['decision']
            win = e.label(d['winner']) if d.get('winner') else 'kein Unterschied'
            vac = d.get('verdict_at_close') or ''
            rows.append(f"<tr><td>{_esc(e.id)}</td><td>{_esc(e.question)}</td>"
                        f"<td>{_esc(win)}</td><td>{_esc(d.get('note', ''))}</td>"
                        f"<td>{_esc(str(d.get('closed_at', ''))[:10])}</td><td>{_esc(vac)}</td></tr>")
        parts.append('<table><tr><th>id</th><th>Frage</th><th>Gewinner</th><th>Notiz</th>'
                     '<th>geschlossen</th><th>Stand beim Schließen</th></tr>' + ''.join(rows) + '</table>')
    if ov['missing']:
        parts.append('<h2>Deklaration fehlt</h2><ul>' + ''.join(
            f'<li>{_esc(r["id"])} ({_esc(r["status"])}) — im Code nicht mehr deklariert</li>'
            for r in ov['missing']) + '</ul>')
    parts.append(f'<h2>Konstanten</h2><p class="muted">MIN_PAIRED={MIN_PAIRED}, '
                 f'MIN_FLAGGED={MIN_FLAGGED}, P_LEANING={P_LEANING}, P_READY={P_READY}, '
                 f'DRAWS={DRAWS}, prior Beta({PRIOR},{PRIOR}); Gleichstand geht an den '
                 f'liefernden Arm.</p>')
    return ''.join(parts)


# ---------------------------------------------------------------- the CLI

def _print_show(o):
    e, v = o['exp'], o['verdict']
    print(o['line'])
    arm_ids = [a.id for a in e.arms]
    w = max(len(e.label(a)) for a in arm_ids) + 2
    print(f'{"":28s}' + ''.join(f'{e.label(a):>{w}s}' for a in arm_ids))
    def line(name, f):
        print(f'{name:28s}' + ''.join(f'{f(a):>{w}s}' for a in arm_ids))
    s = v['stats']
    line('graded lots (all)', lambda a: str(v['graded'].get(a, 0)))
    line('paired lots', lambda a: str(s[a]['n']) if s[a] else '0')
    line('flagged', lambda a: str(s[a]['flagged']) if s[a] else '—')
    line('flagged precision', lambda a: _pct(s[a]['precision']) if s[a] else '—')
    line('recall', lambda a: _pct(s[a]['recall']) if s[a] else '—')
    line('base rate', lambda a: _pct(s[a]['base']) if s[a] else '—')
    line('HIGH tier precision', lambda a: _pct(s[a]['high_precision']) if s[a] else '—')
    if v.get('p_by_arm'):
        line('P(best)', lambda a: f"{v['p_by_arm'][a]:.2f}")
    if o['weekly']:
        print('\nper award week (paired lots): n / flagged / precision')
        for wk, per in o['weekly'].items():
            cells = []
            for a in arm_ids:
                fs = per.get(a)
                cells.append(f"{fs['n']}/{fs['flagged']}/{_pct(fs['precision'])}" if fs else '—')
            print(f'  {wk}  ' + '   '.join(f'{c:>{w}s}' for c in cells))
    print('\ntraining background:')
    for a in e.arms:
        c = o['candidates'].get(a.id)
        if c is None:
            print(f'  {a.label}: not trained yet')
        else:
            trip = '; '.join(c['failures']) if c['failures'] else 'all tripwires passed'
            print(f"  {a.label}: {c['model_id']} val PR-AUC {c.get('val_pr_auc')} "
                  f"{'promoted' if c.get('promoted') else 'kept champion'} — {trip}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', default=None)
    ap.add_argument('--models-dir', default=None)
    sub = ap.add_subparsers(dest='cmd')
    sub.add_parser('list')
    p = sub.add_parser('show'); p.add_argument('id')
    p = sub.add_parser('deliver'); p.add_argument('id'); p.add_argument('arm')
    p = sub.add_parser('close'); p.add_argument('id')
    p.add_argument('--winner', required=True, help='arm id, or none')
    p.add_argument('--note', default='')
    args = ap.parse_args(argv)
    data_dir = config.data_root(args.data_dir)
    models_dir = config.models_root(args.models_dir)
    today = date.today().isoformat()
    cmd = args.cmd or 'list'

    if cmd == 'list':
        ov = overview(data_dir, models_dir, today)
        print('open:')
        for o in ov['open']:
            print('  ' + o['line'])
        if not ov['open']:
            print('  —')
        print('closed:')
        for c in ov['closed']:
            d = c['decision']
            win = c['exp'].label(d['winner']) if d.get('winner') else 'no difference'
            print(f"  {c['exp'].id}: {win} — {d.get('note', '')} "
                  f"(closed {str(d.get('closed_at', ''))[:10]})")
        if not ov['closed']:
            print('  —')
        for r in ov['missing']:
            print(f"  {r['id']}: {r['status']} — DECLARATION MISSING in experiments.py")
        return 0

    if cmd == 'show':
        ov = overview(data_dir, models_dir, today)
        for o in ov['open']:
            if o['exp'].id == args.id:
                _print_show(o)
                return 0
        for c in ov['closed']:
            if c['exp'].id == args.id:
                d = c['decision']
                print(f"{args.id}: closed {d.get('closed_at')} — winner "
                      f"{d.get('winner') or 'none'}; note: {d.get('note')}\n"
                      f"verdict at close: {d.get('verdict_at_close')}")
                return 0
        print(f'no experiment {args.id!r}')
        return 1

    if cmd == 'deliver':
        champ = set_delivering(data_dir, args.id, args.arm, models_dir)
        e = DECLARED[args.id]
        print(f'{args.id}: delivering arm is now {e.label(args.arm)}; '
              + (f'models/CURRENT -> {champ}' if champ else
                 'that arm has no champion yet — models/CURRENT unchanged until it promotes'))
        return 0

    if cmd == 'close':
        e = DECLARED[args.id]
        row = state(data_dir).get(args.id)
        line = None
        if row and row['status'] == 'open':
            v, _ = read_verdict(data_dir, models_dir, e, row, today)
            line = status_line(e, v)
        dec = close(data_dir, args.id, args.winner, args.note, line)
        print(f"{args.id}: closed. winner: {e.label(dec['winner']) if dec['winner'] else 'none'}")
        if dec['winner'] and row and row['delivering'] != dec['winner']:
            print(f"note: the winner is not the delivering arm ({e.label(row['delivering'])}). "
                  f"To switch: python experiments.py deliver {args.id} {dec['winner']}")
        return 0
    return 1


if __name__ == '__main__':
    sys.exit(main())
