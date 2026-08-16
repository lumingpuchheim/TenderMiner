"""Step 2 of the cycle: grade what the market has since decided — PARAMETERS.md 9.

`grade()` turns a published award into the verdict on the prediction that
preceded it, for the delivering arm (one `grade` row per lot) and for every
open experiment's arms (`arm_grade`, EXPERIMENTS.md 6). The rest of the
module is how a graded row is *read back*: `wilson` for the interval every
rate is quoted with, `flag_stats` for precision/recall at the flag cut-off,
`track_record` for the per-trade record the weekly report and the trade
pages both use.

Read-only on the store; the ledger is the record it writes.

`SECTOR` comes along because `track_record` is its only user here. Two more
copies live in `simulation.py` and `render_dashboard.py` — a pre-existing
triplication this split neither widens nor fixes.
"""
from __future__ import annotations

import pandas as pd

import experiments
import ledger
import single_bidder as sb
import util

SECTOR = {'450': 'general construction', '451': 'site preparation', '452': 'civil engineering',
          '453': 'building installation', '454': 'finishing trades'}


def grade(paths, tenders, aw, args, plan=None):
    """Grade ledger predictions for lots whose award has now been published.
    The headline grades the LAST prediction made before the award appeared.
    Each grade row is stamped with the slicing keys (cpv3 trade code,
    place_nuts3) and the award notice's TED publication number, at write time
    (SUBSCRIPTIONS.md: the ledger is the frozen record — a stamped row cannot
    drift, a join against a rebuilt store can).

    During a trial (plan.is_trial) the same step also writes the arm-vs-arm
    record: one `arm_grades` row per arm per newly awarded lot the arm had
    predicted (doc/EXPERIMENTS.md §6). `grades` itself keeps meaning "the
    delivering arm" — it is what the customer track record is built from."""
    already = {(g['procedure_id'], g['lot_id'])
               for g in ledger.read(paths.ledger_home, 'grades')}
    exp = plan.experiment if plan and plan.is_trial else None
    arm_already = experiments.graded_lots(paths.ledger_home, exp.id) if exp else {}
    delivering_of = experiments.delivering_map(paths.ledger_home)

    lot_meta = {}
    for r in tenders[sb.KEY + ['cpv_main', 'place_nuts3']].itertuples():
        lot_meta[(r.procedure_id, r.lot_id)] = {
            'cpv3': str(r.cpv_main)[:3] if pd.notna(r.cpv_main) else None,
            'place_nuts3': util.stamp(r.place_nuts3),
        }

    labeled = {(r.procedure_id, r.lot_id):
               (int(r.label), str(r.publication_date),
                util.stamp(getattr(r, 'publication_number', None)),
                int(r.n_tenders))
               for r in aw.itertuples()}
    # only lots whose award has published can be graded, so only their
    # predictions are needed — a handful of the ledger, asked for by key
    need = {k for k in labeled if k not in already}
    if exp:
        # plus the lots some arm has not graded yet — usually the same lots
        for arm in exp.arms:
            need |= {k for k in labeled if k not in arm_already.get(arm.id, set())}
    by_lot = ledger.predictions_by_lot(paths.ledger_home, lots=need)
    if exp:
        arm_rows = experiments.arm_grade_rows(
            exp, labeled, lot_meta,
            {lot: rows for lot, rows in by_lot.items()
             if any(lot not in arm_already.get(a.id, set()) for a in exp.arms)},
            args.threshold, util.now_utc().isoformat(timespec='seconds'))
        arm_rows = [r for r in arm_rows
                    if (r['procedure_id'], r['lot_id']) not in arm_already.get(r['arm'], set())]
        n_arm = ledger.append(paths.ledger_home, 'arm_grades', arm_rows)
        by_arm = {}
        for r in arm_rows:
            by_arm[r['arm']] = by_arm.get(r['arm'], 0) + 1
        print(f'[grade:{exp.id}] {n_arm} arm-graded rows ('
              + ', '.join(f'{exp.label(a)} {by_arm.get(a, 0)}' for a in by_arm) + ')'
              if n_arm else f'[grade:{exp.id}] no newly awarded lots any arm had scored')
    new_grades = []
    for lot, rows in by_lot.items():
        if lot in already or lot not in labeled:
            continue
        label, award_pub, award_pub_nr, n_tenders = labeled[lot]
        meta = lot_meta.get(lot, {})
        # the customer track record is the DELIVERING arm's: a shadow arm's
        # row on the same lot (same Monday, same ts) must never be "the last
        # prediction". A stamped row counts iff its arm is (or was) that
        # experiment's delivering arm — from the state table, which outlives
        # the trial; rows without a stamp are from outside any trial.
        rows = sorted((r for r in rows
                       if not r.get('arm')
                       or delivering_of.get(r.get('experiment')) == r.get('arm')),
                      key=lambda r: r['ts'])
        if not rows:
            continue
        before = [r for r in rows if str(r['ts'])[:10] <= award_pub[:10]]
        last = (before or rows)[-1]
        flag = bool(last['score'] >= last.get('threshold', args.threshold))
        new_grades.append({
            'graded_at': util.now_utc().isoformat(timespec='seconds'),
            'procedure_id': lot[0], 'lot_id': lot[1],
            'label': label, 'n_tenders': n_tenders, 'award_pub': award_pub,
            'award_publication_number': award_pub_nr,
            'cpv3': meta.get('cpv3'), 'place_nuts3': meta.get('place_nuts3'),
            'score': last['score'], 'tier': last.get('tier'), 'flag': flag,
            'correct': flag == bool(label), 'model': last['model'],
        })
    ledger.append(paths.ledger_home, 'grades', new_grades)
    print(f'[grade] {len(new_grades)} newly graded lots '
          f'({len(by_lot)} lots consulted, {len(already)} previously graded)')
    return new_grades


def _top_slice_stats(rows, share):
    """Hit rate of the top `share` of rows by score, vs the rows' base rate."""
    if not rows:
        return None
    base = sum(g['label'] for g in rows) / len(rows)
    k = max(1, round(len(rows) * share))
    top = sorted(rows, key=lambda g: -g['score'])[:k]
    hit = sum(g['label'] for g in top) / len(top)
    return {'n': len(rows), 'k': k, 'base': base, 'hit': hit,
            'lift': (hit / base) if base > 0 else None}


def wilson(k, n, z=1.96):
    """95% Wilson interval for k of n. Every flag-view rate is printed with
    one: a precision resting on four graded lots must not read like a
    precision resting on four hundred, and the width says so without anyone
    having to look up the denominator."""
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def flag_stats(rows):
    """The binary call — we said lonely / we said not — scored against the
    outcome, with the only baseline that can embarrass it: calling EVERY lot
    lonely, which scores precision = the base rate at recall 1.0.

    Takes any rows carrying `flag` and `label`, so the backtest can score its
    replayed lots with this exact function. That is the point of it being
    public: until live awards accumulate, the replayed number is the one we
    quote, and it must be the same statistic — not a second implementation
    that agrees by coincidence.

    Vocabulary matches sb.metrics: precision is 'the flags right', recall is
    'coverage'. The rank-based headline cannot show a flag that is worse than
    no flag at all; precision below base does exactly that."""
    if not rows:
        return None
    tp = sum(1 for g in rows if g['flag'] and g['label'] == 1)
    fp = sum(1 for g in rows if g['flag'] and g['label'] == 0)
    fn = sum(1 for g in rows if not g['flag'] and g['label'] == 1)
    tn = sum(1 for g in rows if not g['flag'] and g['label'] == 0)
    n, positives, flagged = len(rows), tp + fn, tp + fp
    precision = (tp / flagged) if flagged else None
    recall = (tp / positives) if positives else None
    base = positives / n
    # F1 is undefined only when a *denominator* was empty; a precision or
    # recall of exactly 0 gives F1 0, which is a measurement, not a gap
    f1 = None
    if precision is not None and recall is not None:
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        'n': n, 'tp': tp, 'fp': fp, 'fn': fn, 'tn': tn,
        'flagged': flagged, 'positives': positives,
        'precision': precision, 'recall': recall, 'f1': f1,
        'precision_ci': wilson(tp, flagged),
        'recall_ci': wilson(tp, positives),
        # "flag everything": precision is the base rate, recall is perfect
        'base': base,
        'base_f1': (2 * base / (base + 1)) if base else None,
        'beats_base': precision is not None and precision > base,
    }


def track_record(paths, args):
    """Rolling verified performance over the track window (a parameter).

    Two views of the same graded rows, and they answer different questions.
    The RANK view — 'did the top of our ranking end lonely more often than the
    rest?' — matches the product action, which is picking the most attractive
    tenders. The FLAG view scores the binary lonely/not-lonely call at the
    cut-off, which is the claim precision and recall are about. A model can
    rank well and flag badly, so neither subsumes the other and both are
    computed here.

    Derived, never stored: both views are a pure function of grades.jsonl plus
    the window, so they are recomputed each cycle rather than written to the
    database — a persisted copy is one more thing that can disagree with the
    ledger it came from."""
    grades = ledger.read(paths.ledger_home, 'grades')
    if not grades:
        return None
    cutoff = (util.now_utc() - util.parse_window(args.track_window)).date().isoformat()
    recent = [g for g in grades if str(g['award_pub'])[:10] >= cutoff]
    if not recent:
        return None
    flagged = [g for g in recent if g['flag']]
    positives = [g for g in recent if g['label'] == 1]

    by_trade = {}
    for g in recent:
        if g.get('cpv3'):
            by_trade.setdefault(g['cpv3'], []).append(g)
    trades = []
    for cpv3, rows in sorted(by_trade.items()):
        if len(rows) < args.min_trade_grades:
            continue
        s = _top_slice_stats(rows, args.top_slice)
        trades.append({'cpv3': cpv3, 'name': SECTOR.get(cpv3, ''),
                       'flag': flag_stats(rows), **s})

    tiers = []
    for tier in ('HIGH', 'MEDIUM', 'LOW'):
        rows = [g for g in recent if g.get('tier') == tier]
        if rows:
            tiers.append({'tier': tier, 'n': len(rows),
                          'hit': sum(g['label'] for g in rows) / len(rows)})

    return {
        'window': args.track_window,
        'graded': len(recent),
        'base_rate': sum(g['label'] for g in recent) / len(recent),
        'top': _top_slice_stats(recent, args.top_slice),
        'top_share': args.top_slice,
        'trades': trades,
        'tiers': tiers,
        'flag': flag_stats(recent),
        'min_flag_grades': args.min_flag_grades,
        'flags': len(flagged),
        'flags_right': (sum(g['label'] for g in flagged) / len(flagged)) if flagged else None,
        'coverage': (sum(1 for g in positives if g['flag']) / len(positives)) if positives else None,
    }


