"""Shadow: the gate's FORWARD channel — PARAMETERS.md 12.

Backplay may only reject (§0.5); a value moves into production on forward
evidence — live lots, judged before anyone knew the outcome, read blind by
the operator. This module is that half for the gate:

  * every cycle, each STANDING PROPOSAL (a `move` the queue found and nobody
    has acted on, knobs.standing_proposals) is judged beside the champion on
    the same live lots for the same subscriptions — a subprocess per
    configuration under `TM_GATE_OVERRIDE`, exactly as backplay measures;
  * where the two disagree, a row is written (`gate_shadows`, role `diff`);
    the counts of the cycle go in a `summary` row;
  * `python shadow.py --label` shows the unread disagreements BLIND — title,
    buyer, description, the customer's profile — and records in / out
    (`gate_labels`). It never says which configuration said what, so the
    labels cannot drift toward the champion (§3, blind labelling);
  * the verdict per challenger is computed from those labels on request:
    ready to promote / bar breached / challenger loses / collecting.

    python shadow.py                # this cycle, from the last predictions
    python shadow.py --label        # read the disagreements, answer i / o / s
    python shadow.py --show         # every challenger: cycles, diffs, labels, verdict
    python shadow.py --judge --lots FILE --out FILE   # (subprocess) one config's verdicts

What promotes: still a commit — constant, receipt, register row (§8.2). What
this gives the operator is the receipt, on live lots, with the leakage number
the hard bar is stated in. What it rejects by itself: a challenger whose extra
admissions breach the bar, or that loses the labelled disagreements clearly —
that proposal stops standing.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import grading
import knobs
import ledger
import util

REPO = Path(__file__).resolve().parent

MIN_LABELLED = 20        # labelled disagreements before a challenger can be ready
HARD_BAR = knobs.HARD_BAR


# ------------------------------------------------------------- the subprocess

def judge_lots(paths, lots_path, out_path, as_of):
    """Under THIS process's gate configuration (champion, or a candidate via
    TM_GATE_OVERRIDE): every subscription that wants the gate, every scored
    lot in its market -> in / near / out. Writes one JSON document."""
    import relevance as rel
    import subscriptions
    rows = json.loads(Path(lots_path).read_text(encoding='utf-8'))
    subs = [s for s in subscriptions.load(paths.subs_home, as_of) if rel.wants_gate(s)]
    gate = rel.Gate(paths.data, as_of=as_of)
    verdicts = []
    for sub in subs:
        try:
            profile = rel.build_profile(gate, sub)
        except Exception as e:
            print(f'[shadow] {sub["sub_id"]}: profile error ({e}) — skipped')
            continue
        for row in rows:
            if not subscriptions.in_market(sub, row):
                continue
            ok, near, *_ = rel.judge(gate, profile, row)
            i = gate.by_key.get((row['procedure_id'], row['lot_id']))
            desc = str(gate.all_desc[i] or '')[:500] if i is not None else ''
            verdicts.append({
                'sub_id': sub['sub_id'], 'sub_name': sub.get('name') or sub['sub_id'],
                'procedure_id': row['procedure_id'], 'lot_id': row['lot_id'],
                'verdict': 'in' if ok else 'near' if near else 'out',
                'title': row.get('title'), 'buyer_name': row.get('buyer_name'),
                'cpv_main': row.get('cpv_main'),
                'publication_number': row.get('publication_number'), 'desc': desc})
    doc = {'fingerprint': gate.config.fingerprint, 'override': util.gate_override(),
           'n_subs': len(subs), 'verdicts': verdicts}
    Path(out_path).write_text(json.dumps(doc, default=str), encoding='utf-8')
    print(f'[shadow] {gate.config.fingerprint}: {len(verdicts)} verdicts for {len(subs)} subscriptions')


def measure(paths, lots_path, override=None, as_of=None, timeout=3600):
    """Run one configuration in a subprocess. Raises on failure — a verdict
    resting on a crash is not a verdict."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / 'verdicts.json'
        env = dict(os.environ)
        env.pop('TM_GATE_OVERRIDE', None)
        if override:
            env['TM_GATE_OVERRIDE'] = json.dumps(override)
        argv = [sys.executable, str(REPO / 'shadow.py'), '--judge',
                '--data-dir', str(paths.data), '--models-dir', str(paths.models),
                '--lots', str(lots_path), '--out', str(out)]
        if as_of:
            argv += ['--as-of', as_of]
        proc = subprocess.run(argv, env=env, timeout=timeout, capture_output=True,
                              encoding='utf-8', errors='replace')
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or '').strip().splitlines()[-3:]
            raise RuntimeError('shadow judge exited '
                               f'{proc.returncode}: ' + ' | '.join(tail))
        if not out.exists():
            raise RuntimeError('shadow judge wrote nothing')
        return json.loads(out.read_text(encoding='utf-8'))


# ------------------------------------------------------------------ the cycle

def challengers(paths, today=None):
    """The standing proposals the forward channel has not yet rejected."""
    out = []
    for c in knobs.standing_proposals(paths):
        if c.get('proposed') is None:
            continue
        status, _, _ = verdict(paths, c['knob'], c['proposed'], today)
        if status in ('bar breached', 'challenger loses'):
            continue
        out.append(c)
    return out


def run(paths, scored, today=None):
    """Judge this cycle's scored lots under the champion and every standing
    proposal; record disagreements. Returns the lines the report carries."""
    today = today or util.now_utc().date().isoformat()
    props = challengers(paths, today)
    if not props:
        return ['- shadow: no standing proposal — nothing to judge beside the champion']
    lines, rows = [], []
    with tempfile.TemporaryDirectory() as tmp:
        lots = Path(tmp) / 'lots.json'
        lots.write_text(json.dumps(list(scored), default=str), encoding='utf-8')
        try:
            champ = measure(paths, lots, as_of=today)
        except Exception as e:
            return [f'- shadow: champion judge failed ({e}) — nothing recorded']
        by_key = {(v['sub_id'], v['procedure_id'], v['lot_id']): v for v in champ['verdicts']}
        for c in props:
            value = c['proposed']
            name = c['knob'].split('.')[-1]
            try:
                cand = measure(paths, lots, override={name: value}, as_of=today)
            except Exception as e:
                lines.append(f'- shadow {c["knob"]}={value}: judge failed ({e})')
                continue
            ts = util.now_utc().isoformat(timespec='seconds')
            n_in_ch = n_in_cand = n_diff = 0
            for v in cand['verdicts']:
                key = (v['sub_id'], v['procedure_id'], v['lot_id'])
                base = by_key.get(key)
                if base is None:
                    continue
                n_in_ch += base['verdict'] == 'in'
                n_in_cand += v['verdict'] == 'in'
                if base['verdict'] != v['verdict']:
                    n_diff += 1
                    rows.append({'ts': ts, 'cycle': today, 'knob': c['knob'], 'value': value,
                                 'challenger_fp': cand['fingerprint'],
                                 'champion_fp': champ['fingerprint'], 'role': 'diff',
                                 'sub_id': v['sub_id'], 'sub_name': v['sub_name'],
                                 'procedure_id': v['procedure_id'], 'lot_id': v['lot_id'],
                                 'champion': base['verdict'], 'challenger': v['verdict'],
                                 'title': v['title'], 'buyer_name': v['buyer_name'],
                                 'cpv_main': v['cpv_main'], 'desc': v['desc'],
                                 'publication_number': v['publication_number']})
            rows.append({'ts': ts, 'cycle': today, 'knob': c['knob'], 'value': value,
                         'challenger_fp': cand['fingerprint'], 'champion_fp': champ['fingerprint'],
                         'role': 'summary', 'n_judged': len(cand['verdicts']),
                         'n_subs': cand['n_subs'], 'champion_in': n_in_ch,
                         'challenger_in': n_in_cand, 'n_diff': n_diff})
            lines.append(f'- shadow {c["knob"]}={value} ({cand["fingerprint"]}): '
                         f'{len(cand["verdicts"])} lot×profile judged, champion admits '
                         f'{n_in_ch}, challenger {n_in_cand}, {n_diff} disagree'
                         + (' — read them: python shadow.py --label' if n_diff else ''))
    if rows:
        ledger.append(paths.ledger_home, 'gate_shadows', rows)
    return lines


# ---------------------------------------------------------------- the verdict

def _labels(paths):
    """{(sub, procedure, lot): expect} — the latest reading per lot."""
    out = {}
    for r in ledger.read(paths.ledger_home, 'gate_labels'):
        out[(r['sub_id'], r['procedure_id'], r['lot_id'])] = r['expect']
    return out


def verdict(paths, knob, value, today=None):
    """(status, detail, stats) for one challenger from the forward record.

    Wrong-trade leakage here is the challenger's ADDED leakage: its extra
    admissions the operator read as out, over everything it admits on live
    lots. The agreements' leakage is the champion's and is not re-read, so a
    breach is certain and a pass is a lower bound — which is the right way
    round for a bar.
    """
    rows = [r for r in ledger.read(paths.ledger_home, 'gate_shadows')
            if r.get('knob') == knob and r.get('value') == value]
    if not rows:
        return 'no cycle yet', 'first shadow cycle is the next Monday', {}
    summaries = [r for r in rows if r.get('role') == 'summary']
    diffs = {(r['sub_id'], r['procedure_id'], r['lot_id']): r
             for r in rows if r.get('role') == 'diff'}
    labels = _labels(paths)
    admits = sum(int(s.get('challenger_in') or 0) for s in summaries)
    n_lab = ch_right = base_right = extra_wrong = 0
    for key, d in diffs.items():
        exp = labels.get(key)
        if exp not in ('in', 'out'):
            continue
        n_lab += 1
        ch_right += (d['challenger'] == 'in') == (exp == 'in')
        base_right += (d['champion'] == 'in') == (exp == 'in')
        if d['challenger'] == 'in' and d['champion'] != 'in' and exp == 'out':
            extra_wrong += 1
    stats = {'cycles': len(summaries), 'diffs': len(diffs), 'labelled': n_lab,
             'challenger_right': ch_right, 'champion_right': base_right,
             'admits': admits, 'extra_wrong': extra_wrong,
             'added_leakage': (extra_wrong / admits) if admits else None}
    if admits and extra_wrong / admits > HARD_BAR:
        return ('bar breached',
                f'added leakage {extra_wrong / admits * 100:.1f}% > {HARD_BAR * 100:.1f}% '
                f'({extra_wrong} wrong admissions of {admits}) — proposal dropped', stats)
    if n_lab < MIN_LABELLED:
        return ('collecting',
                f'{len(summaries)} cycle(s), {len(diffs)} disagreements, {n_lab} read '
                f'(need {MIN_LABELLED}) — python shadow.py --label', stats)
    lo, hi = grading.wilson(ch_right, n_lab)
    if lo > 0.5:
        return ('ready to promote',
                f'challenger right on {ch_right}/{n_lab} read disagreements '
                f'(lower bound {lo:.2f}), added leakage '
                f'{extra_wrong / admits * 100 if admits else 0:.1f}% — the three-file commit is yours', stats)
    if hi < 0.5:
        return ('challenger loses',
                f'champion right on {base_right}/{n_lab} read disagreements '
                f'(challenger upper bound {hi:.2f}) — proposal dropped', stats)
    return ('undecided',
            f'{ch_right}/{n_lab} to the challenger, interval {lo:.2f}-{hi:.2f} '
            f'straddles 0.5 — keep reading', stats)


def status_lines(paths, today=None):
    """One line per standing proposal with its forward status — for the
    report, and the whole record for --show."""
    lines = []
    for c in knobs.standing_proposals(paths):
        value = c['proposed']
        status, detail, _ = verdict(paths, c['knob'], value, today)
        lines.append(f'- shadow {c["knob"]} -> {value}: **{status}** — {detail}')
    return lines


# ------------------------------------------------------------- the labelling

def unread(paths):
    """Disagreements without a reading, one per (sub, lot), newest first,
    WITHOUT the verdicts — the reader must not see them."""
    labels = _labels(paths)
    seen, out = set(), []
    rows = [r for r in ledger.read(paths.ledger_home, 'gate_shadows') if r.get('role') == 'diff']
    for r in sorted(rows, key=lambda r: str(r.get('ts')), reverse=True):
        key = (r['sub_id'], r['procedure_id'], r['lot_id'])
        if key in labels or key in seen:
            continue
        seen.add(key)
        out.append({k: r.get(k) for k in ('sub_id', 'sub_name', 'procedure_id', 'lot_id',
                                           'title', 'buyer_name', 'cpv_main', 'desc',
                                           'publication_number')})
    return out


def label(paths, answers=None):
    """Interactive: show each unread disagreement blind, take i / o / s / q.
    `answers` (a list) replaces stdin for tests."""
    todo = unread(paths)
    if not todo:
        print('[shadow] nothing to read — no unlabelled disagreement')
        return 0
    print(f'[shadow] {len(todo)} disagreement(s) to read. For each: is this lot '
          f'this customer\'s business? i = in, o = out, s = skip, q = quit')
    it = iter(answers) if answers is not None else None
    rows = []
    for n, d in enumerate(todo, 1):
        print(f'\n[{n}/{len(todo)}] profile: {d["sub_name"]}   ({d["sub_id"]})')
        print(f'  title : {d["title"]}')
        print(f'  buyer : {d["buyer_name"]}   cpv {d["cpv_main"]}   {d["publication_number"]}')
        print(f'  text  : {(d["desc"] or "")[:400]}')
        ans = (next(it, 'q') if it is not None else input('  in / out / skip / quit > ')).strip().lower()[:1]
        if ans == 'q':
            break
        if ans in ('i', 'o'):
            rows.append({'ts': util.now_utc().isoformat(timespec='seconds'),
                         'sub_id': d['sub_id'], 'procedure_id': d['procedure_id'],
                         'lot_id': d['lot_id'], 'expect': 'in' if ans == 'i' else 'out',
                         'note': ''})
    if rows:
        ledger.append(paths.ledger_home, 'gate_labels', rows)
    print(f'[shadow] {len(rows)} reading(s) recorded')
    return len(rows)


def show(paths, today=None):
    lines = ['[shadow] the forward channel — PARAMETERS.md 12']
    props = knobs.standing_proposals(paths)
    if not props:
        lines.append('[shadow] no standing proposal; nothing is being shadowed')
    for c in props:
        value = c['proposed']
        status, detail, st = verdict(paths, c['knob'], value, today)
        lines.append(f'[shadow] {c["knob"]} {c["current"]} -> {value}: {status} — {detail}')
        if st:
            lines.append(f'[shadow]   cycles {st["cycles"]}, disagreements {st["diffs"]}, '
                         f'read {st["labelled"]} (challenger right {st["challenger_right"]}, '
                         f'champion right {st["champion_right"]}), challenger admits '
                         f'{st["admits"]}, wrong extra admissions {st["extra_wrong"]}')
    n = len(unread(paths))
    lines.append(f'[shadow] {n} disagreement(s) unread' + (' — python shadow.py --label' if n else ''))
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    import config as cfg
    ap.add_argument('--data-dir', default=cfg.data_root())
    ap.add_argument('--models-dir', default=cfg.models_root())
    ap.add_argument('--judge', action='store_true', help='(subprocess) judge --lots under this config')
    ap.add_argument('--lots'); ap.add_argument('--out'); ap.add_argument('--as-of')
    ap.add_argument('--label', action='store_true', help='read the unlabelled disagreements blind')
    ap.add_argument('--show', action='store_true')
    args = ap.parse_args()
    paths = util.Paths(args.data_dir, args.models_dir)
    if args.judge:
        judge_lots(paths, args.lots, args.out, args.as_of or util.now_utc().date().isoformat())
        return
    if args.label:
        label(paths)
        return
    if args.show:
        for line in show(paths):
            print(line)
        return
    # a cycle by hand: the last prediction per open lot, as the cycle's
    # `scored` would be (the cycle itself calls run() from loop.py)
    scored = list(ledger.prediction_latest_per_lot(paths.ledger_home).values())
    for line in run(paths, scored):
        print(line)


if __name__ == '__main__':
    main()
