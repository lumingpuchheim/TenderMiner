"""TenderMining online-learning loop — the predict -> grade -> retrain cycle.

Concept: ONLINE_LEARNING.md. Model logic: single_bidder.py (v1 notice-only
features; no buyer-derived features by design decision).

    python loop.py run --last 7d              # weekly cycle
    python loop.py run --last 6m              # first backfill
    python loop.py run --last 7d --skip-download   # offline: reuse the store

The interval is a parameter, never a constant: --last X (Nd/Nw/Nm) sets the
download window; the effective window is widened to cover everything since the
last successful run (checkpoint), so gaps self-heal and overlaps dedup.

Every run: download new notices -> rebuild the store parquets from the raw
archive -> grade past ledger predictions against newly published awards ->
retrain a candidate and gate it against the champion (tripwires + validation
PR-AUC) -> score all open lots with the champion -> append to the ledger ->
write a markdown report.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
import sys
from html import escape
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import config
import experiments
import heavy_lock
import ledger
import render
import selection
import single_bidder as sb
import subscriptions

REPO = Path(__file__).resolve().parent

SECTOR = {'450': 'general construction', '451': 'site preparation', '452': 'civil engineering',
          '453': 'building installation', '454': 'finishing trades'}


# ------------------------------------------------------------------ small utils

def parse_window(spec):
    """'7d' / '2w' / '3m' -> timedelta (months approximated as 30 days)."""
    m = re.fullmatch(r'(\d+)([dwm])', spec.strip().lower())
    if not m:
        raise SystemExit(f"--last '{spec}' is not of the form 7d / 2w / 3m")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(days=n * {'d': 1, 'w': 7, 'm': 30}[unit])


def now_utc():
    return datetime.now(timezone.utc)


def read_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding='utf-8'))


def write_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, default=str), encoding='utf-8')


def read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding='utf-8').splitlines() if line.strip()]


def stamp(v):
    """NaN/NaT -> None so ledger rows carry JSON null, never 'nan' strings."""
    try:
        return None if pd.isna(v) else v
    except (TypeError, ValueError):
        return v


def append_jsonl(path, rows):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('a', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + '\n')


class Paths:
    """All on-disk locations, rooted at --data-dir / --models-dir (parameters,
    like every window in this program)."""

    def __init__(self, data_dir, models_dir):
        self.data = Path(data_dir)
        self.xml = self.data / 'raw' / 'xml'
        self.store_tenders = self.data / 'store' / 'tenders.parquet'
        self.store_awards = self.data / 'store' / 'awards.parquet'
        # the HOME whose storage holds the cycle's own record — predictions
        # and grades. A directory, not two files (ledger.py owns the format);
        # rewind_report.py points it at its as-of sandbox.
        self.ledger_home = self.data
        # the HOME whose storage holds the delivery record and the gate-config
        # registry — a directory, not a file (ledger.py owns the format).
        # tryout/replay point this at a sandbox.
        self.deliveries_home = self.data
        # the DIRECTORY subscriptions live in, not the file: the storage
        # format belongs to subscriptions.py (tryout/replay point this at a
        # sandbox dir instead)
        self.subs_home = self.data
        self.checkpoint = self.data / 'logs' / 'loop_checkpoint.json'
        self.drift = self.data / 'logs' / 'drift_latest.json'
        self.reports = self.data / 'reports'
        self.models = Path(models_dir)
        self.registry = self.models / 'registry.jsonl'
        self.current = self.models / 'CURRENT'


# ------------------------------------------------------------- step 1: download

def download(paths, args, checkpoint):
    """bulk.py fetches the window's packages; features.py rebuilds the store
    parquets from the ENTIRE raw archive (full rebuild == growing store, since
    the archive only grows; bulk.py skips already-processed packages itself)."""
    today = now_utc().date()
    requested_from = today - parse_window(args.last)
    last_to = checkpoint.get('last_success_to')
    effective_from = requested_from
    if last_to:
        effective_from = min(requested_from, datetime.strptime(last_to, '%Y%m%d').date())
    date_from, date_to = effective_from.strftime('%Y%m%d'), today.strftime('%Y%m%d')
    print(f'[download] window {date_from}..{date_to} '
          f'(requested --last {args.last}, checkpoint {last_to or "none"})')

    subprocess.run(
        [sys.executable, str(REPO / 'bulk.py'), '--from', date_from, '--to', date_to,
         '--country', args.country, '--cpv', args.cpv, '--out-dir', str(paths.xml)],
        check=True)
    subprocess.run(
        [sys.executable, str(REPO / 'features.py'), '--xml-dir', str(paths.xml),
         '--cpv', args.cpv,
         '--tenders-out', str(paths.store_tenders),
         '--awards-out', str(paths.store_awards)],
        check=True)
    return date_to


# ---------------------------------------------------------------- step 2: grade

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
            'place_nuts3': stamp(r.place_nuts3),
        }

    labeled = {(r.procedure_id, r.lot_id):
               (int(r.label), str(r.publication_date),
                stamp(getattr(r, 'publication_number', None)),
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
            args.threshold, now_utc().isoformat(timespec='seconds'))
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
            'graded_at': now_utc().isoformat(timespec='seconds'),
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
    cutoff = (now_utc() - parse_window(args.track_window)).date().isoformat()
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


# ------------------------------------------------------- step 3: learn + promote

def current_champion(paths, arm=None):
    """The champion: models/CURRENT, or the arm's own pointer during a trial
    (doc/EXPERIMENTS.md §4 — each arm is gated against ITS OWN champion)."""
    pointer = paths.current if arm is None else experiments.arm_current_path(paths.models, arm.id)
    if not pointer.exists():
        return None
    model_id = pointer.read_text(encoding='utf-8').strip()
    meta = read_json(paths.models / model_id / 'meta.json', None)
    return {'model_id': model_id, 'meta': meta}


def learn(paths, tenders, roles, data, aw, args, checkpoint, arm=None, plan=None):
    """Train a candidate on v1 notice-only features, run the trust checks, gate
    against the champion, persist to the registry. Returns (model_id, gate).

    A failed trust check BLOCKS PROMOTION and keeps the champion — it never
    aborts the cycle (ONLINE_LEARNING.md: "blocks keep the champion and
    notify; nothing fails silently").

    arm/plan (doc/EXPERIMENTS.md §8): during a trial this runs once per arm
    with the arm's feature build, CatBoost overrides and guard exemptions,
    gated against the arm's own champion; the delivering arm's promotion also
    rewrites models/CURRENT. Without an arm it is exactly the single-arm cycle."""
    feature_build = arm.feature_build if arm else 'default'
    exempt = arm.guard_exempt if arm else ()
    tag = f'[learn:{arm.id}]' if arm else '[learn]'
    # the multi-hot vocabulary is fitted once, on the full tenders frame, and
    # travels with the candidate (meta.json) so predict_open scores with the
    # columns the model was trained on — not with whatever the archive holds
    # the week it scores (doc/EXPERIMENTS.md §3)
    multihot = sb.fit_multihot(tenders, roles, feature_build=feature_build)
    X, cat_cols, num_cols, _ = sb.build_features(data, roles, list_frame=tenders,
                                                 multihot=multihot, feature_build=feature_build)
    features = cat_cols + num_cols
    gate = {'val_window': args.val_window, 'checks': {}, 'warnings': [], 'failures': []}
    if arm:
        gate['arm'] = arm.id

    try:
        card = sb.assert_pure_one_hot(X, cat_cols, exempt=exempt)
        ctr = sb.ctr_columns(card, exempt)
        checked_max = int(card.drop(labels=[c for c in exempt if c in card.index]).max())
        gate['checks']['pure_one_hot'] = (
            f'passed (max cardinality {checked_max}'
            + (f'; CTR columns: ' + ', '.join(f'{c} {n}' for c, n in ctr.items()) if ctr else '')
            + ')')
    except AssertionError as e:
        # cannot train a trustworthy candidate at all — keep champion, skip training
        gate['failures'].append(f'pure_one_hot: {e}')
        print(f'{tag} TRUST CHECK FAILED: {e} — no candidate this cycle, champion kept')
        return None, gate

    pub = pd.to_datetime(data['publication_date'])
    val_threshold = pub.max() - parse_window(args.val_window)
    split = sb.temporal_split(data, X, threshold=val_threshold)
    gate['val_threshold'] = str(val_threshold.date())
    gate['n_val_lots'] = split.n_test_lots

    overrides = {'iterations': args.iterations} if args.iterations else {}
    if arm:
        overrides.update(arm.overrides)
    small_val = split.n_test_lots < args.min_val_lots or len(set(split.yte)) < 2
    val_metrics = None
    if small_val:
        gate['warnings'].append(
            f'validation window too small ({split.n_test_lots} lots) — gate skipped')
    else:
        eval_model = sb.train(split.Xtr, split.ytr, split.wtr, cat_cols, **overrides)
        p_val = sb.predict(eval_model, split.Xte)
        val_metrics = sb.metrics(split.yte, p_val, split.wte, threshold=args.threshold)
        # the product-shaped sorting check: hit rate of the top slice of the
        # ranking on the validation window, vs the window's base rate
        k = max(1, round(len(p_val) * args.top_slice))
        idx = np.argsort(-p_val)[:k]
        top_hit = float(np.average(split.yte[idx], weights=split.wte[idx]))
        val_metrics['top_slice_share'] = args.top_slice
        val_metrics['top_slice_hit'] = top_hit
        val_metrics['top_slice_lift'] = (top_hit / val_metrics['base_rate']
                                         if val_metrics['base_rate'] else None)
        gate['val_metrics'] = val_metrics
        # tripwire: too good to be true
        if val_metrics['roc_auc'] >= sb.TOO_GOOD_ROC:
            gate['failures'].append(
                f"too_good: val ROC-AUC {val_metrics['roc_auc']:.3f} >= {sb.TOO_GOOD_ROC} — hunt the leak")
        else:
            gate['checks']['too_good'] = f"passed (ROC-AUC {val_metrics['roc_auc']:.3f})"
        # tripwire: shuffled labels (monthly, or when never run). Median of 3
        # shuffles — a single shuffle's PR-AUC is too noisy on small windows —
        # and only when the validation window has enough positives for the
        # null distribution to be meaningful at all.
        last_shuffled = checkpoint.get('last_shuffled_check')
        n_pos_val = int((split.yte == 1).sum())
        if n_pos_val < args.min_shuffle_positives:
            gate['checks']['shuffled_label'] = (
                f'skipped ({n_pos_val} positive val lots < {args.min_shuffle_positives} — '
                'too few for a reliable null)')
        elif not last_shuffled or (now_utc().date() - datetime.strptime(last_shuffled, '%Y-%m-%d').date()).days >= 30:
            prs = []
            for seed in (42, 43, 44):
                mapping = sb.permuted_lot_labels(data, mask=split.is_train, seed=seed)
                ytr_shuf = sb.labels_from_mapping(data, split.is_train, mapping)
                shuf_model = sb.train(split.Xtr, ytr_shuf, split.wtr, cat_cols, **overrides)
                prs.append(sb.metrics(split.yte, sb.predict(shuf_model, split.Xte), split.wte)['pr_auc'])
            pr_shuf = float(np.median(prs))
            base = split.base_rate_test
            # collapse bound adapts to the observed shuffle noise on small windows
            bound = max(base * 1.5, base + 3 * float(np.std(prs)) + 0.02)
            if pr_shuf >= bound:
                gate['failures'].append(
                    f'shuffled_label: median PR-AUC {pr_shuf:.3f} did not collapse '
                    f'(bound {bound:.3f}, base {base:.3f})')
            else:
                gate['checks']['shuffled_label'] = (
                    f'passed (median PR-AUC {pr_shuf:.3f} < bound {bound:.3f}, base {base:.3f})')
                checkpoint['last_shuffled_check'] = now_utc().date().isoformat()
        else:
            gate['checks']['shuffled_label'] = f'skipped (last run {last_shuffled})'

    # tripwire: computability on open lots (production dry-run)
    open_t = sb.open_tenders(tenders, aw)
    X_open, cats_o, nums_o, _ = sb.build_features(open_t, roles, list_frame=tenders,
                                                  multihot=multihot, feature_build=feature_build)
    if cats_o + nums_o != features:
        gate['failures'].append('computability: feature set differs for open lots — a feature depends on the future')
    else:
        gate['checks']['computability'] = f'passed ({len(open_t)} open rows)'

    # deploy model: trained on ALL labeled rows
    y = data['label'].to_numpy()
    k = data.groupby(sb.KEY)['label'].transform('size')
    w = (1.0 / k).to_numpy()
    deploy = sb.train(X, y, w, cat_cols, **overrides)

    # promotion: all trust checks passed AND match-or-beat the champion's val PR-AUC
    champ = current_champion(paths, arm)
    # A champion trained on a different feature schema cannot score today's
    # build AT ALL (predict_open compares the column list), so comparing PR-AUC
    # to it decides nothing: the candidate is the only usable model. A feature
    # change is therefore a forced, announced flag day — never a promotion rule
    # that keeps a champion the next step cannot call.
    champ_meta = (champ or {}).get('meta') or {}
    champ_features = champ_meta.get('features')
    incompatible = bool(champ and champ_features is not None
                        and list(champ_features) != list(features))
    if gate['failures']:
        promote = False
        gate['warnings'].append('champion kept: trust check failed')
        if incompatible:
            gate['warnings'].append(
                'AND the kept champion cannot score the current feature build — '
                'this cycle will not predict until the failure above is fixed')
    elif incompatible:
        promote = True
        added = [f for f in features if f not in set(champ_features)]
        dropped = [f for f in champ_features if f not in set(features)]
        gate['feature_schema_change'] = {'added': added, 'dropped': dropped,
                                         'champion': champ['model_id']}
        gate['warnings'].append(
            f"feature schema changed vs champion {champ['model_id']} "
            f'(+{len(added)}: {added or "-"}; -{len(dropped)}: {dropped or "-"}) '
            '— candidate promoted unconditionally: the champion can no longer '
            'score this build, so a PR-AUC comparison is not defined')
    elif small_val:
        promote = champ is None
        if champ:
            gate['warnings'].append('champion kept: cannot compare without a usable validation window')
    elif champ is None or champ['meta'] is None or champ['meta'].get('val_pr_auc') is None:
        promote = True
    else:
        champ_pr = champ['meta']['val_pr_auc']
        promote = val_metrics['pr_auc'] >= champ_pr - args.promote_epsilon
        gate['champion'] = {'model_id': champ['model_id'], 'val_pr_auc': champ_pr}
        if not promote:
            gate['warnings'].append(
                f"candidate val PR-AUC {val_metrics['pr_auc']:.4f} < champion {champ_pr:.4f} — champion kept")

    model_id = 'm' + now_utc().strftime('%Y-%m-%d-%H%M%S') + (arm.suffix if arm else '')
    mdir = paths.models / model_id
    mdir.mkdir(parents=True, exist_ok=True)
    deploy.save_model(str(mdir / 'model.cbm'))
    meta = {
        'model_id': model_id,
        'trained_at': now_utc().isoformat(timespec='seconds'),
        'n_train_rows': len(data),
        'n_train_lots': int(data.groupby(sb.KEY).ngroups),
        'features': features, 'n_features': len(features),
        'max_cardinality': int(card.max()),
        'multihot': multihot,
        'feature_build': feature_build,
        'val_pr_auc': None if val_metrics is None else val_metrics['pr_auc'],
        'val_roc_auc': None if val_metrics is None else val_metrics['roc_auc'],
        'val_top_hit': None if val_metrics is None else val_metrics.get('top_slice_hit'),
        'val_top_lift': None if val_metrics is None else val_metrics.get('top_slice_lift'),
        'gate': gate, 'promoted': promote,
        'threshold': args.threshold,
    }
    if arm:
        meta.update({'experiment': plan.experiment.id, 'arm': arm.id, 'label': arm.label,
                     'guard_exempt': list(arm.guard_exempt), 'catboost': dict(arm.catboost)})
    write_json(mdir / 'meta.json', meta)
    append_jsonl(paths.registry, [{'model_id': model_id, 'promoted': promote,
                                   'val_pr_auc': meta['val_pr_auc'],
                                   'val_top_hit': meta['val_top_hit'],
                                   'val_top_lift': meta['val_top_lift'],
                                   'trained_at': meta['trained_at'],
                                   **({'arm': arm.id, 'experiment': plan.experiment.id} if arm else {})}])
    if promote:
        # the arm's own pointer during a trial; models/CURRENT only for the
        # delivering arm — a shadow's promotion never reaches a customer
        if arm:
            ap = experiments.arm_current_path(paths.models, arm.id)
            ap.parent.mkdir(parents=True, exist_ok=True)
            ap.write_text(model_id + '\n', encoding='utf-8')
        if arm is None or plan.is_delivering(arm):
            paths.current.parent.mkdir(parents=True, exist_ok=True)
            paths.current.write_text(model_id + '\n', encoding='utf-8')
    lift = f" | top-{args.top_slice:.0%} val hit {meta['val_top_hit']:.2f} (lift {meta['val_top_lift']:.1f}x)" \
        if meta['val_top_hit'] is not None else ''
    print(f'{tag} candidate {model_id} '
          f"val PR-AUC {meta['val_pr_auc']}{lift} -> {'PROMOTED' if promote else 'champion kept'}")
    for wmsg in gate['warnings']:
        print(f'{tag} warning: {wmsg}')
    return model_id, gate


# ----------------------------------------------- plain-language pick reasons

# The customer boundary for model internals (SUBSCRIPTIONS.md): feature
# groups -> plain phrases; None = technical, never surfaces.
WHY_PHRASES = {
    'notice_kind': None, 'notice_subtype': None, 'quality_flags': None,
    'est_value_lot_currency': None, 'est_value_procedure_currency': None,
    'span__issue_date': None,
    'procedure_type': 'das Vergabeverfahren', 'accelerated': 'das Vergabeverfahren',
    'award_criterion_kind': 'die Zuschlagskriterien', 'price_weight_pct': 'die Zuschlagskriterien',
    'contract_type': 'die Vertragsart',
    'buyer_legal_type': 'die Art des Auftraggebers', 'buyer_activity': 'die Art des Auftraggebers',
    'buyer_is_cpb_awarding': 'die Art des Auftraggebers',
    'buyer_is_cpb_acquiring': 'die Art des Auftraggebers',
    'service_provider_type': 'die Art des Auftraggebers',
    'buyer_country': 'der Sitz des Auftraggebers',
    'eu_funded': 'die Finanzierung', 'funding_programs': 'die Finanzierung',
    'duration_source': 'die Vertragslaufzeit', 'duration_unit_raw': 'die Vertragslaufzeit',
    'duration_measure_raw': 'die Vertragslaufzeit', 'duration_days': 'die Vertragslaufzeit',
    'span__period_start': 'die Vertragslaufzeit', 'span__period_end': 'die Vertragslaufzeit',
    'est_value_lot': 'das Auftragsvolumen', 'est_value_procedure': 'das Auftragsvolumen',
    'selection_criteria_types': 'die Eignungsanforderungen',
    'n_selection_criteria': 'die Eignungsanforderungen',
    'n_criteria_suitability': 'die Eignungsanforderungen',
    'n_criteria_financial': 'die Eignungsanforderungen',
    'n_criteria_technical': 'die Eignungsanforderungen',
    'n_criteria_other': 'die Eignungsanforderungen',
    'bid_bond_required': 'die geforderte Bietersicherheit',
    'bid_validity_unit': 'die geforderte Bindefrist',
    'bid_validity_days': 'die geforderte Bindefrist',
    'bid_validity_raw': 'die geforderte Bindefrist',
    'exclusion_grounds': 'die Ausschlussgründe', 'n_exclusion_grounds': 'die Ausschlussgründe',
    'cv_required': 'die geforderten Qualifikationsnachweise',
    'legal_form_required': 'die geforderte Rechtsform',
    'security_clearance_required': 'die geforderte Sicherheitsüberprüfung',
    'docs_restricted': 'der beschränkte Zugang zu den Unterlagen',
    'variants': 'die Vorgaben zur Angebotsform', 'multiple_bids': 'die Vorgaben zur Angebotsform',
    'esubmission': 'der Einreichungsweg', 'sme_suitable': 'die Eignung für kleinere Betriebe',
    'framework_type': 'die Rahmenvereinbarung', 'is_framework': 'die Rahmenvereinbarung',
    'procurement_additional_types': 'besondere Vergabebedingungen',
    'is_strategic': 'besondere Vergabebedingungen',
    'procedure_languages': 'die Sprachanforderungen',
    'gpa_covered': 'die internationale Ausschreibungspflicht',
    'recurring': 'die regelmäßige Wiederkehr', 'eauction': 'das E-Auktions-Format',
    'is_corrigendum': 'die bisherigen Korrekturen', 'change_reasons': 'die bisherigen Korrekturen',
    'n_corrections_so_far': 'die bisherigen Korrekturen',
    'platform_name': 'die genutzte Vergabeplattform',
    'deadline_days': 'die Angebotsfrist', 'deadline_days_published': 'die Angebotsfrist',
    'span__deadline_date': 'die Angebotsfrist',
    'span__bid_opening_date': 'der Verfahrenszeitplan',
    'span__question_deadline_date': 'der Verfahrenszeitplan',
    'question_window_days': 'der Verfahrenszeitplan',
    'opening_lag_days': 'der Verfahrenszeitplan',
    'n_lots': 'der Loszuschnitt',
    'n_doc_references': 'der Umfang der Unterlagen',
}
WHY_PREFIXES = [
    (('cpv_main__', 'cpv_additional__'), 'das spezialisierte Gewerk'),
    (('place_nuts3__', 'place_postal_zone__'), 'der Standort — dort bieten weniger Firmen'),
    (('buyer_nuts__', 'buyer_postal_zone__'), 'der Sitz des Auftraggebers'),
]


def _why_phrase(feat):
    if feat in WHY_PHRASES:
        return WHY_PHRASES[feat]
    for prefixes, phrase in WHY_PREFIXES:
        if feat.startswith(prefixes):
            return phrase
    return None


def explain_rows(model, X, cat_cols, k=3):
    """Per-row top-k plain-language reasons the model leans lonely (positive
    SHAP) and crowded (negative), deduped through the phrase book."""
    from catboost import Pool
    shap = model.get_feature_importance(type='ShapValues',
                                        data=Pool(X, cat_features=cat_cols))
    feats = list(X.columns)

    def collect(contrib, order, sign):
        phrases = []
        for j in order:
            if sign * contrib[j] <= 0:
                break
            p = _why_phrase(feats[j])
            if p and p not in phrases:
                phrases.append(p)
            if len(phrases) == k:
                break
        return phrases

    lonely, crowded = [], []
    for row in shap:
        contrib = row[:-1]
        order = np.argsort(-contrib)
        lonely.append(collect(contrib, order, 1))
        crowded.append(collect(contrib, order[::-1], -1))
    return lonely, crowded


# -------------------------------------------------------------- step 4: predict

def predict_open(paths, tenders, roles, aw, args, arm=None, plan=None):
    """Score every open lot with the champion; append new rows to the ledger.
    Returns (new_ledger_rows, all_scores_this_cycle, scored) — the full score
    array feeds the score-distribution drift monitor, and `scored` (one dict
    per open lot, ledger-row shaped, dedup or not) feeds the subscription
    renderer even on cycles where dedup writes no new ledger rows.

    arm/plan (doc/EXPERIMENTS.md §8): during a trial this runs once per arm
    with the arm's own champion; every row is stamped with `experiment` and
    `arm` so the arm-vs-arm grading can find it. Only the delivering arm's
    return value reaches deliver(), the drift monitors and the simulation."""
    tag = f'[predict:{arm.id}]' if arm else '[predict]'
    champ = current_champion(paths, arm)
    if champ is None:
        print(f'{tag} no champion model — nothing to score')
        return [], np.array([]), []
    from catboost import CatBoostClassifier
    model = CatBoostClassifier()
    model.load_model(str(paths.models / champ['model_id'] / 'model.cbm'))

    open_t = sb.open_tenders(tenders, aw)
    today = now_utc().date().isoformat()
    deadline = pd.to_datetime(open_t.get('deadline_date'), errors='coerce')
    open_t = open_t[(deadline.isna()) | (deadline.dt.date.astype(str) >= today)]
    if open_t.empty:
        print(f'{tag} no open lots')
        return [], np.array([]), []
    # the champion's own multi-hot vocabulary and feature build: the columns
    # it was trained on. A champion from before the vocabulary existed has
    # none; the default fit is then the best guess and the schema check below
    # still decides.
    meta = champ.get('meta') or {}
    multihot = meta.get('multihot')
    feature_build = meta.get('feature_build', 'default')
    X_open, cats_open, _, _ = sb.build_features(open_t, roles, list_frame=tenders,
                                                multihot=multihot, feature_build=feature_build)
    if list(X_open.columns) != list(model.feature_names_):
        # Reachable only when learn() could not promote a candidate (a trust
        # check blocked it) across a feature change: the champion predates the
        # current build and cannot score it. Loud skip, not a stack trace —
        # ONLINE_LEARNING.md: "blocks keep the champion and notify; nothing
        # fails silently". No scores this cycle means no picks this cycle, which
        # the report and every subscription already state as an outcome.
        now = set(X_open.columns)
        had = set(model.feature_names_)
        print(f'{tag} SCHEMA MISMATCH — champion {champ["model_id"]} was '
              f'trained on a different feature set, so it cannot score this '
              f'build. Not scoring this cycle. '
              f'new: {sorted(now - had) or "-"}; '
              f'gone: {sorted(had - now) or "-"}. '
              f'Fix the blocked trust check in [learn] and rerun.')
        return [], np.array([]), []
    scores = sb.predict(model, X_open)
    why_lonely, why_crowded = explain_rows(model, X_open, cats_open)

    # rank-based tiers ("is it a good one?"), never probabilities: HIGH = top
    # tier_high share of this batch's ranking, MEDIUM = next tier_medium share,
    # LOW = the rest. Their real-world meaning comes from the graded track
    # record ("HIGH picks ended lonely X in 100"), not from the score values.
    n = len(scores)
    ranks = np.empty(n, dtype=int)
    ranks[np.argsort(-scores)] = np.arange(n)
    n_high = max(1, round(n * args.tier_high))
    n_med = max(1, round(n * args.tier_medium))
    tiers = np.where(ranks < n_high, 'HIGH', np.where(ranks < n_high + n_med, 'MEDIUM', 'LOW'))

    seen = ledger.prediction_keys(paths.ledger_home)
    ts = now_utc().isoformat(timespec='seconds')
    scored, rows = [], []
    for (idx, t), score, tier, w_l, w_c in zip(open_t.iterrows(), scores, tiers,
                                               why_lonely, why_crowded):
        cpv = t.get('cpv_main')
        row = {
            'ts': ts, 'model': champ['model_id'],
            'procedure_id': t['procedure_id'], 'lot_id': t['lot_id'],
            'notice_id': t.get('notice_id'),
            'publication_date': str(t.get('publication_date')),
            'deadline_date': str(t.get('deadline_date')),
            'score': float(score), 'threshold': args.threshold,
            'flag': bool(score >= args.threshold), 'tier': str(tier),
            # slicing keys + audit link + rendering columns, stamped at write
            # time (SUBSCRIPTIONS.md) — rendering columns never feed features
            'cpv3': str(cpv)[:3] if pd.notna(cpv) else None,
            'cpv_main': stamp(cpv),  # full code for the relevance code channel
            'place_nuts3': stamp(t.get('place_nuts3')),
            'publication_number': stamp(t.get('publication_number')),
            'buyer_name': stamp(t.get('buyer_name')),
            'est_value_lot': stamp(t.get('est_value_lot')),
            'title': stamp(t.get('title')),
            'why_lonely': w_l, 'why_crowded': w_c,
        }
        if arm:
            # the arm-vs-arm grading finds an arm's rows by these two stamps;
            # rows from before the experiment have none and never count
            row['experiment'] = plan.experiment.id
            row['arm'] = arm.id
        scored.append(row)
        key = (t['procedure_id'], t['lot_id'], t.get('notice_id'), champ['model_id'])
        if key not in seen:  # idempotent re-runs: same notice + same model scored once
            rows.append(row)
    ledger.append(paths.ledger_home, 'predictions', rows)
    print(f'{tag} {len(rows)} new ledger rows ({len(open_t)} open rows scored, '
          f'model {champ["model_id"]})')
    return rows, scores, scored


# --------------------------------------------------- step 4b: deliver to subs

# The lot's identity — the key of every per-lot side table in deliver(), and
# of selection.py's `judged`, which is where it now lives: the two must agree
# or a verdict lands next to the wrong customer's pick.
_lot_key = selection.lot_key
# the HTML helpers moved to render.py with the renderers that use
# them (REFACTOR.md phase 4b); the operator report below still calls
# them, so they are imported rather than re-implemented
clean_cell = render.clean_cell
date_de = render.date_de
html_page = render.html_page
table_html = render.table_html
receipt_html = render.receipt_html


def record_gate_config(paths, config):
    """Append this configuration to the gate-config registry the first time
    its fingerprint is seen, so the stamp on a delivery row is resolvable
    from the data directory alone — not from git archaeology over whichever
    commit was deployed that week. Append-only, one line per configuration,
    like every other ledger here.

    Scoped to the DELIVERY ledger, not to the data dir: preview_report.py and
    rewind_report.py redirect deliveries into a sandbox while still reading the real
    store, and a sandbox experiment must not append a configuration to the
    record of what customers were actually served under."""
    home = paths.deliveries_home
    if any(r.get('fingerprint') == config.fingerprint
           for r in ledger.read(home, 'gate_configs')):
        return False
    ledger.append(home, 'gate_configs',
                  [{'fingerprint': config.fingerprint,
                    'first_seen': now_utc().isoformat(timespec='seconds'),
                    **config.as_dict()}])
    print(f'[deliver] new gate configuration recorded: {config.describe()}')
    return True


def learn_references(paths, tenders, awards, args):
    """Step 4c (RELEVANCE.md phase 9): a customer's own wins become profile
    references — including the ones this gate rejected, which are the
    false negatives worth seeing. Derived data: appended to
    data/ledger/learned_refs.jsonl, never to the subscription file, so
    subscription versions keep meaning "the operator decided something".
    Runs before deliver() so this cycle's report already benefits. Never
    fails a cycle — a feedback problem is not a delivery problem."""
    try:
        import feedback
        today = now_utc().date().isoformat()
        subs = subscriptions.load(paths.subs_home, today)
        if not subs:
            return []

        def gate_factory():
            import relevance as rel
            return rel.Gate(paths.data, as_of=today)

        return feedback.learn(paths.data, subs, awards, tenders, today,
                              gate_factory=gate_factory)
    except Exception as e:
        print(f'[learn] skipped ({e})')
        return []


def deliver(paths, scored, args):
    """The dispatcher: one run, many views. Filter this cycle's scored open
    lots per subscription, re-rank and re-tier WITHIN the slice, write the
    customer's report, append delivery-ledger rows (the frozen record of what
    this customer actually saw). Never a model call, never a store join."""
    today = now_utc().date()
    subs = subscriptions.load(paths.subs_home, today.isoformat())
    if not subs:
        print('[deliver] no active subscriptions — skipped')
        return 0
    # latest revision per lot: a customer sees each lot once, as last published
    latest = {}
    for row in scored:
        key = (row['procedure_id'], row['lot_id'])
        if key not in latest or str(row['publication_date']) >= str(latest[key]['publication_date']):
            latest[key] = row
    past = ledger.read(paths.deliveries_home, 'deliveries')
    already = {(d['sub_id'], d['procedure_id'], d['lot_id'], str(d['ts'])[:10])
               for d in past}
    by_sub = {}
    for d in past:
        by_sub.setdefault(d['sub_id'], []).append(d)
    cutoff = (now_utc() - parse_window(args.track_window)).date().isoformat()
    grades_recent = [g for g in ledger.read(paths.ledger_home, 'grades')
                     if str(g['award_pub'])[:10] >= cutoff]
    # receipt fallback for delivery rows written before title/buyer were stamped
    pred_info = ledger.prediction_titles(paths.ledger_home)
    ts = now_utc().isoformat(timespec='seconds')
    # relevance gate (RELEVANCE.md phase 3): loaded once per cycle, only when a
    # subscription asks for it; unavailable sidecars degrade to ungated delivery
    # with a loud line, never a failed cycle
    gate = None
    try:
        import relevance as rel
        if any(rel.wants_gate(s) for s in subs):
            # as_of=today unions each customer's learned references
            # (feedback.py); without it a profile is the subscription line
            # alone — see relevance.Gate
            gate = rel.Gate(paths.data, as_of=today.isoformat())
            # the rules this cycle judges under, on the record before any
            # verdict is written (REFACTOR.md phase 3)
            record_gate_config(paths, gate.config)
    except Exception as e:
        print(f'[deliver] relevance gate unavailable ({e}) — delivering ungated')
        gate = None
    n_rows = 0
    for sub in subs:
        profile = None
        if gate is not None and rel.wants_gate(sub):
            try:
                profile = rel.build_profile(gate, sub)
                # a profile with no lexicon and no core root cannot pass ANY
                # lot — the customer gets an empty report every cycle and
                # nothing says why. Say why.
                mute = rel.mute_reason(profile, gate.config)
                if mute:
                    print(f"[deliver] {sub['sub_id']}: ** MUTE PROFILE ** "
                          f'{mute}')
            except Exception as e:
                print(f"[deliver] {sub['sub_id']}: profile error ({e}) — "
                      f'delivering ungated')
        # slice -> gate -> rank -> cap, in selection.py because the all-lots
        # rewind runs the same four steps and must run THESE (REFACTOR.md
        # phase 4). The gate sees the widest candidate set — deadline ignored,
        # since the annex needs a verdict for short-deadline lots too — and
        # near-misses render separately.
        sel = selection.for_sub(sub, latest.values(), today, gate=gate,
                                profile=profile)
        # render.py turns the SliceResult into the two documents and the
        # delivery rows (REFACTOR.md phase 4b). This loop keeps only the
        # dispatch and the writing: everything above is "what does this
        # customer get", everything below is "where does it go".
        receipts = render.receipt_html(grades_recent,
                                       by_sub.get(sub['sub_id'], []), pred_info)
        page, deliveries = render.customer_report(
            sub, sel, today=today, profile=profile, receipts=receipts,
            tier_high=args.tier_high, tier_medium=args.tier_medium,
            ts=ts, already=already)
        annex_name, annex = render.market_annex(
            sub, sel, today=today, profile=profile, top_slice=args.top_slice)
        out = paths.reports / 'subscriptions' / sub['sub_id'] / f'report_{today.isoformat()}.html'
        out.parent.mkdir(parents=True, exist_ok=True)
        (out.parent / annex_name).write_text(annex, encoding='utf-8')
        if page is not None:
            out.write_text(page, encoding='utf-8')
        else:
            # nothing to recommend and nothing graded to look back on -> no
            # report this cycle (decision 2026-08-06); the annex above is
            # still written as the operator's lookup
            print(f"[deliver] {sub['sub_id']}: nothing to report — "
                  f'no report written')
        ledger.append(paths.deliveries_home, 'deliveries', deliveries)
        n_rows += len(deliveries)
        print(f"[deliver] {sub['sub_id']}: {len(sel.picks)} lots delivered "
              f'({len(sel.ranked)} matched, {len(deliveries)} new delivery rows)')
    return n_rows


# ------------------------------------------------------- housekeeping

def _prune_scratch_world(paths, max_age_days):
    """Delete stale as-of scratch worlds once nothing in them has been
    touched for `max_age_days`. -> (files, bytes).

    The rewind programs rebuild these directories from the real store on
    every run (`asof.py`) — a filtered copy of the parquet store plus a full
    copy of the embeddings, entirely reconstructible, and nothing reads them
    between runs. At 203.8 MB apiece they were the second largest thing
    under `data/` after the notice archive. Swept per subdirectory: each
    world under `data/asof/` ages on its own clock, so a fresh rewind never
    protects a stale one. The three pre-phase-5 homes are swept by the same
    rule until they stop existing on operator machines.

    Age is the safety catch, not a policy: a rewind in progress has fresh
    files, so a sweep cannot pull the floor out from under a half-hour run.
    """
    asof_root = paths.data / 'asof'
    worlds = ([d for d in asof_root.iterdir() if d.is_dir()]
              if asof_root.exists() else [])
    worlds += [paths.data / n for n in ('backtest_world', 'playback_asof',
                                        'replay_asof')]
    n_total, freed_total = 0, 0
    for world in worlds:
        if not world.exists():
            continue
        files = [f for f in world.rglob('*') if f.is_file()]
        if not files:
            continue
        newest = max(f.stat().st_mtime for f in files)
        if newest >= time.time() - max_age_days * 86400:
            continue
        freed_total += sum(f.stat().st_size for f in files)
        n_total += len(files)
        shutil.rmtree(world, ignore_errors=True)
    return n_total, freed_total


def prune_caches(paths, max_age_days=30):
    """Delete discovery caches older than `max_age_days`.

    The TED search resume cache is keyed by a hash of the query, and a query
    names a date window — so a cache is unresumable the day after its window
    passes. Nothing removed them and the directory reached 1.13 GB across 1,132
    dead scopes, all written within a fortnight. It is not the weekly cycle that
    creates them (bulk.py borrows only helpers from download.py, never
    search_all), but the cycle is the only thing that runs regularly, so it is
    where the sweeping belongs.

    Safe by construction: these are derived files. The notices are in the raw
    archive and the parquet store, and the worst case is re-querying a scope
    that happens to be repeated. Never fails a cycle.
    """
    try:
        import download
        n, freed = download.prune_discovery(max_age_days)
        if n:
            print(f'[prune] {n} stale discovery cache file(s), '
                  f'freed {freed / 1e6:.1f} MB')
        wn, wfreed = _prune_scratch_world(paths, max_age_days)
        if wn:
            print(f'[prune] as-of scratch worlds untouched for '
                  f'{max_age_days}d, freed {wfreed / 1e6:.1f} MB ({wn} files)')
        return n + wn
    except Exception as e:
        print(f'[prune] skipped ({e})')
        return 0


# ------------------------------------------------------------- drift monitors


def _psi(hist, now, bins=10):
    """Population stability index of `now` vs `hist`, on hist's quantile bins.
    ~0 = same shape; >0.25 is the conventional 'population has shifted' mark."""
    edges = np.unique(np.quantile(hist, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return None  # hist scores nearly constant — no meaningful histogram
    edges[0], edges[-1] = -np.inf, np.inf
    p = np.histogram(hist, bins=edges)[0] / len(hist)
    q = np.histogram(now, bins=edges)[0] / len(now)
    p, q = np.clip(p, 1e-4, None), np.clip(q, 1e-4, None)
    return float(np.sum((q - p) * np.log(q / p)))


def drift_monitors(paths, tenders, aw, scores_now, args):
    """The four every-cycle drift monitors from ONLINE_LEARNING.md — pure
    reads that WARN in the report footer, never block promotion. They are what
    says "the market moved" before the track record sours.

    Recent = the trailing --drift-window; each monitor skips itself (and says
    so) when either side has too few rows to mean anything."""
    checks, warnings = {}, []
    cutoff = now_utc() - parse_window(args.drift_window)

    def result(name, status, detail):
        checks[name] = f'{status} ({detail})'
        if status == 'WARN':
            warnings.append(f'{name}: {detail}')

    # base-rate drift: single-bid rate of recently awarded lots vs the band of
    # monthly rates over history (mean ± max(2σ, 0.02) across qualifying months)
    award_pub = pd.to_datetime(aw['publication_date'], errors='coerce')
    recent_mask = award_pub >= pd.Timestamp(cutoff.date())
    hist_lots, recent_lots = aw[~recent_mask], aw[recent_mask]
    monthly = hist_lots.groupby(award_pub[~recent_mask].dt.to_period('M'))['label'] \
        .agg(['mean', 'size'])
    monthly = monthly[monthly['size'] >= args.drift_min_lots]
    if len(monthly) < 3 or len(recent_lots) < args.drift_min_lots:
        result('base_rate', 'skipped',
               f'{len(monthly)} qualifying months, {len(recent_lots)} recent awards — too little history')
    else:
        mid, half = monthly['mean'].mean(), max(2 * monthly['mean'].std(), 0.02)
        rate = recent_lots['label'].mean()
        detail = (f'single-bid rate {rate:.3f} vs historical band '
                  f'{mid - half:.3f}..{mid + half:.3f} over {len(monthly)} months')
        result('base_rate', 'ok' if mid - half <= rate <= mid + half else 'WARN', detail)

    # missingness drift: a notice field's null-rate jumping means the source
    # schema changed under us — compare recent notices vs all earlier ones
    tender_pub = pd.to_datetime(tenders['publication_date'], errors='coerce')
    t_recent = tenders[tender_pub >= pd.Timestamp(cutoff.date())]
    t_hist = tenders[tender_pub < pd.Timestamp(cutoff.date())]
    if len(t_recent) < args.drift_min_lots or len(t_hist) < args.drift_min_lots:
        result('missingness', 'skipped',
               f'{len(t_recent)} recent / {len(t_hist)} historical notices — too few rows')
    else:
        jumps = (t_recent.isna().mean() - t_hist.isna().mean()).abs().sort_values(ascending=False)
        moved = jumps[jumps > args.missing_jump]
        if moved.empty:
            result('missingness', 'ok',
                   f'max null-rate change {jumps.iloc[0]:.2f} ({jumps.index[0]}), '
                   f'threshold {args.missing_jump:.2f}')
        else:
            top = ', '.join(f'{c} {t_hist[c].isna().mean():.2f}->{t_recent[c].isna().mean():.2f}'
                            for c in moved.index[:4])
            result('missingness', 'WARN', f'{len(moved)} column(s) jumped: {top}')

    # award-latency drift: median tender→award gap shifting stretches (or
    # shortens) how long predictions stay ungraded — the report should say so
    first_pub = tender_pub.groupby([tenders[k] for k in sb.KEY]).min() \
        .rename('first_pub').reset_index()
    joined = aw[sb.KEY].assign(award_pub=award_pub.to_numpy()) \
        .merge(first_pub, on=sb.KEY, how='left')
    gap_days = (joined['award_pub'] - joined['first_pub']).dt.days
    g_recent = gap_days[recent_mask.to_numpy()].dropna()
    g_hist = gap_days[~recent_mask.to_numpy()].dropna()
    if len(g_recent) < args.drift_min_lots or len(g_hist) < args.drift_min_lots:
        result('award_latency', 'skipped',
               f'{len(g_recent)} recent / {len(g_hist)} historical gaps — too few awards')
    else:
        med_r, med_h = float(g_recent.median()), float(g_hist.median())
        # material = a shift a human would call one: ≥14 days AND ≥25% of the norm
        material = max(14.0, 0.25 * med_h)
        detail = f'median gap {med_r:.0f}d recently vs {med_h:.0f}d historically'
        result('award_latency', 'WARN' if abs(med_r - med_h) >= material else 'ok', detail)

    # score-distribution drift: this cycle's scores vs the trailing month of
    # ledger scores (before this run) — a shifted histogram means the open-lot
    # population or the champion's view of it moved
    ledger_cut = (now_utc() - timedelta(days=35)).isoformat(timespec='seconds')
    # the trailing window is a WHERE clause, not a filter over the whole ledger.
    # This runs AFTER predict_open has appended, so the window now includes this
    # cycle's own rows -- which it did not when the caller snapshotted the file
    # beforehand. Excluded explicitly, so the comparison stays "this cycle
    # against the month before it".
    hist_scores = np.array([
        s for s in ledger.prediction_scores_since(
            paths.ledger_home, ledger_cut,
            exclude_models=experiments.shadow_models(paths.models, paths.ledger_home))])
    if len(scores_now) and len(hist_scores) > len(scores_now):
        hist_scores = hist_scores[:-len(scores_now)]
    if len(scores_now) < args.drift_min_lots or len(hist_scores) < args.drift_min_lots:
        result('score_distribution', 'skipped',
               f'{len(scores_now)} scores this cycle / {len(hist_scores)} in trailing month — too few')
    else:
        psi = _psi(hist_scores, np.asarray(scores_now))
        if psi is None:
            result('score_distribution', 'skipped', 'trailing-month scores nearly constant')
        else:
            result('score_distribution', 'WARN' if psi >= args.psi_warn else 'ok',
                   f'PSI {psi:.3f} vs trailing month ({len(hist_scores)} ledger scores), '
                   f'warn at {args.psi_warn:.2f}')

    for name, status in checks.items():
        print(f'[drift] {name}: {status}')
    return {'checks': checks, 'warnings': warnings}


# --------------------------------------------------------------- step 5: report

def _rate_ci(rate, ci):
    """'25 in 100 (95% CI 5-70)' — the interval is not decoration; it is the
    difference between a number you may quote and one you may not."""
    if rate is None:
        return '—'
    s = f'{rate*100:.0f} in 100'
    if ci:
        s += f' (95% CI {ci[0]*100:.0f}-{ci[1]*100:.0f})'
    return s


def flag_view_lines(record, args):
    """The precision/recall section: what the binary lonely/not-lonely call was
    worth, next to the one baseline that can beat it for free.

    Printed on every cycle that graded anything — including cycles where the
    model flagged nothing, which is itself a result and used to vanish from
    the report entirely."""
    if not record:
        return []
    f = record.get('flag')
    if not f:
        return []
    lines = ['## The flag: precision and recall '
             f'(binary view at the {args.threshold:.2f} cut-off)', '']

    thin = f['n'] < record.get('min_flag_grades', 0)
    if thin:
        lines += [f"**Too thin to read: {f['n']} graded lots against a floor of "
                  f"{record['min_flag_grades']}.** The numbers below are printed so the "
                  'series exists from day one, not because they mean anything yet — '
                  'awards publish a median 84 days after the tender, so this section '
                  'fills up roughly a quarter behind the predictions. Read the '
                  'confidence intervals, not the point estimates.', '']

    lines += [f"Over the trailing {record['window']}, {f['n']} graded lots: we called "
              f"{f['flagged']} of them lonely, and {f['positives']} really ended with "
              '0-1 bids.', '',
              '| | we said lonely | we said not | total |',
              '|---|---|---|---|',
              f"| **ended 0-1 bids** | {f['tp']} | {f['fn']} | {f['positives']} |",
              f"| **ended 2+ bids** | {f['fp']} | {f['tn']} | {f['n'] - f['positives']} |",
              f"| total | {f['flagged']} | {f['n'] - f['flagged']} | {f['n']} |", '']

    if f['flagged'] == 0:
        lines += ['We flagged nothing in this window, so precision is undefined and '
                  'recall is 0 — every single-bid lot was missed. A cut-off no lot '
                  'clears is a broken cut-off, not a cautious one.', '']
    else:
        lines += [f"- **precision** (the flags right): {_rate_ci(f['precision'], f['precision_ci'])}",
                  f"- **recall** (single-bid lots caught): {_rate_ci(f['recall'], f['recall_ci'])}",
                  f"- **F1**: {f['f1']:.2f}" if f['f1'] is not None else '- **F1**: —',
                  '']

    if f['positives'] == 0:
        # Degenerate window: with nothing to catch, precision is 0 for us AND
        # for the baseline, and comparing the two says nothing about either.
        lines += ['Not one graded lot in this window ended with 0-1 bids, so there was '
                  'nothing to catch: precision is 0 by construction and no comparison '
                  'against a baseline means anything here. Wait for a window that '
                  'contains positives.', '']
        return lines

    lines += ['Against the only free baseline — **call every lot lonely**: '
              f"precision {f['base']*100:.0f} in 100, recall 100 in 100"
              + (f", F1 {f['base_f1']:.2f}." if f['base_f1'] is not None else '.'), '']
    if f['precision'] is not None and not f['beats_base']:
        lines += [f"**The flag is not paying for itself:** its precision "
                  f"({f['precision']*100:.0f} in 100) is at or below the "
                  f"{f['base']*100:.0f} in 100 you get by flagging everything, so at this "
                  'cut-off the model is costing recall and buying nothing. '
                  + ('On this sample that is noise, not a verdict.' if thin else
                     'On this sample that is a real finding — move the cut-off or '
                     'retrain before quoting the flag to anyone.'), '']

    trades = [t for t in record.get('trades', []) if t.get('flag')]
    if trades:
        lines += [f"Per trade (trades with at least {args.min_trade_grades} graded lots):", '',
                  '| trade | graded | flags | precision | recall | flag everything |',
                  '|---|---|---|---|---|---|']
        for t in trades:
            tf = t['flag']
            prec = f"{tf['precision']*100:.0f} in 100" if tf['precision'] is not None else '—'
            rec = f"{tf['recall']*100:.0f} in 100" if tf['recall'] is not None else '—'
            lines.append(f"| {t['cpv3']} {t['name']} | {tf['n']} | {tf['flagged']} | "
                         f"{prec} | {rec} | {tf['base']*100:.0f} in 100 |")
        lines.append('')
    return lines


def report(paths, tenders, args, record, gate, drift, model_id, n_graded, n_predicted,
           trial_lines=()):
    latest_model = ledger.prediction_latest_per_lot(
        paths.ledger_home,
        exclude_models=experiments.shadow_models(paths.models, paths.ledger_home))
    open_rows = sorted(latest_model.values(), key=lambda r: -r['score'])

    info = {}
    for t in tenders.itertuples():
        info[(t.procedure_id, t.lot_id)] = t
    lines = [f'# TenderMining weekly report — {now_utc().date().isoformat()}', '']
    if record and record.get('top'):
        t = record['top']
        lines += ['## Verified track record (rank-based — the product view)', '',
                  f"Over the trailing {record['window']}: {record['graded']} predicted lots got their outcome. "
                  f"Of the **top {record['top_share']:.0%} of our ranking** ({t['k']} lots), "
                  f"**{t['hit']*100:.0f} in 100 ended with 0-1 bids**, vs {t['base']*100:.0f} in 100 "
                  f"across all graded lots — **lift {t['lift']:.1f}x**." if t['lift'] is not None else
                  'Top-slice lift not computable (no positives in the window).',
                  '']
        if record['trades']:
            lines += ['Per trade (trades with enough graded lots):', '']
            for tr in record['trades']:
                lines.append(f"- {tr['cpv3']} {tr['name']}: top {record['top_share']:.0%} of our ranking hit "
                             f"{tr['hit']*100:.0f} in 100, base {tr['base']*100:.0f} in 100 "
                             f"(lift {tr['lift']:.1f}x, {tr['n']} graded lots)")
            lines.append('')
        if record.get('tiers'):
            lines += ['What each tier really meant (graded outcomes per tier):', '']
            for t_ in record['tiers']:
                lines.append(f"- {t_['tier']}: {t_['hit']*100:.0f} in 100 ended with 0-1 bids "
                             f"({t_['n']} graded lots)")
            lines.append('')
    else:
        lines += ['## Verified track record', '',
                  'No graded outcomes in the window yet — grading starts as awards arrive.', '']

    lines += flag_view_lines(record, args)

    lines += [f'## This week\'s shortlist (top {args.report_top} of the ranking)', '',
              '| tier | score | deadline | est. value | title |', '|---|---|---|---|---|']
    for r in open_rows[:args.report_top]:
        t = info.get((r['procedure_id'], r['lot_id']))
        title = (str(getattr(t, 'title', ''))[:60] if t is not None else '')
        value = getattr(t, 'est_value_lot', None) if t is not None else None
        value = f'{value:,.0f}' if isinstance(value, (int, float)) and pd.notna(value) else ''
        lines.append(f"| {r.get('tier') or ''} | {r['score']:.2f} | "
                     f"{str(r.get('deadline_date'))[:10]} | {value} | {title} |")

    lines += ['', '## Health', '',
              f'- candidate model: {model_id} ({ "promoted" if gate and not gate.get("warnings") else "see warnings"})',
              f'- newly graded lots: {n_graded}',
              f'- new predictions: {n_predicted}']
    if gate:
        for name, status in gate.get('checks', {}).items():
            lines.append(f'- check {name}: {status}')
        for fmsg in gate.get('failures', []):
            lines.append(f'- TRUST CHECK FAILED: {fmsg}')
        for wmsg in gate.get('warnings', []):
            lines.append(f'- WARNING: {wmsg}')
    if drift:
        for name, status in drift['checks'].items():
            lines.append(f'- drift {name}: {status}')
        for wmsg in drift['warnings']:
            lines.append(f'- DRIFT WARNING: {wmsg}')
    if trial_lines:
        lines += ['', '## Experiments (doc/EXPERIMENTS.md)', '']
        lines += [f'- {tl}' for tl in trial_lines]

    paths.reports.mkdir(parents=True, exist_ok=True)
    out = paths.reports / f'report_{now_utc().date().isoformat()}.md'
    out.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'[report] {out}')
    return out


# ----------------------------------------------------------------------- main

def cmd_run(args):
    paths = Paths(args.data_dir, args.models_dir)
    # The cycle WAITS for the heavy-job lock rather than failing on it: a
    # replay someone started at 08:10 is over in minutes, while a skipped
    # Monday is a week with no delivery. The wait is bounded — heavy_lock
    # raises rather than hanging, and property 3 in that module is why.
    with heavy_lock.held(paths.data, 'the weekly cycle', wait=3600):
        _run_cycle(paths, args)


def _run_cycle(paths, args):
    # which state this cycle is operating on, before it operates on it
    # (doc/STORAGE.md 6.1) — a cycle that silently used the wrong root would
    # look exactly like a cycle with nothing to do
    print(f'[config] data root: {config.describe(paths.data)}')
    # and which gate rules this process resolved to (PARAMETERS.md 4.3): three
    # GateConfig knobs and twenty evidence.py ones read env vars at import, so
    # a stray variable in cron's environment would otherwise change
    # production silently. The line is the assertion; the fingerprint on it
    # is the one every delivery row of this cycle will carry.
    import relevance as rel
    print(f'[config] gate: {rel.DEFAULT_CONFIG.describe()}')
    checkpoint = read_json(paths.checkpoint, {})

    if args.skip_download:
        print('[download] skipped (--skip-download)')
        date_to = checkpoint.get('last_success_to')
    else:
        date_to = download(paths, args, checkpoint)

    tenders, roles = sb.load_with_roles(paths.store_tenders)
    awards, _ = sb.load_with_roles(paths.store_awards)
    data, aw, n_dropped = sb.assemble(tenders, awards)
    print(f'[store] {len(tenders)} tender rows, {len(awards)} award rows, '
          f'{data.groupby(sb.KEY).ngroups} labeled lots ({n_dropped} reporting errors dropped)')

    # ONE open of the embedding model, for both jobs that need it.
    #
    # Opening it costs ~1.2 GB whether it is then handed 278 tender texts or a
    # single word — the expense is the opening. There are exactly two jobs:
    # the lot texts (here), and the individual words that evidence tier 3
    # falls back on when neither an exact nor a typo-tolerant match hits.
    # Scoring, selection and the reports never open it; they read numbers off
    # the sidecar.
    #
    # Left apart, the second job opened it again on its own later — in
    # delivery, mid-report, for a handful of words a new week had never seen,
    # and again in the next replay. Doing it here while the model is already
    # open costs nothing extra and leaves the rest of the week clean.
    import embed
    try:
        embed.ensure_embeddings(paths.data, tenders)
        import embed_vocab
        embed_vocab.top_up(paths.data)
    except Exception as e:  # nothing reads the sidecar until RELEVANCE.md phase 3; never fail a cycle over it
        print(f'[embed] sidecar update failed: {e}')
    finally:
        # Whatever happened above, the model does not travel into grading,
        # training and delivery — none of them embed anything.
        embed.unload_model()

    # A/B arms (doc/EXPERIMENTS.md §8): with an open experiment the cycle
    # trains and scores once per arm; only the delivering arm's outputs go on
    # to the monitors, the report, delivery and the simulation. With none it
    # is the single implicit arm — exactly the cycle as it always was.
    plan = experiments.plan(paths.ledger_home, now_utc().date().isoformat())
    if plan.is_trial:
        print(f'[experiment] {plan.experiment.id}: arms '
              + ', '.join(f'{a.label}{" (delivering)" if plan.is_delivering(a) else " (shadow)"}'
                          for a in plan.arms))
    new_grades = grade(paths, tenders, aw, args, plan)
    record = track_record(paths, args)
    if not plan.is_trial:
        model_id, gate = learn(paths, tenders, roles, data, aw, args, checkpoint)
        rows, scores_now, scored = predict_open(paths, tenders, roles, aw, args)
    else:
        model_id = gate = None
        rows, scores_now, scored = [], np.array([]), []
        for arm in plan.arms:
            mid, g = learn(paths, tenders, roles, data, aw, args, checkpoint, arm=arm, plan=plan)
            r_, s_, sc_ = predict_open(paths, tenders, roles, aw, args, arm=arm, plan=plan)
            if plan.is_delivering(arm):
                model_id, gate, rows, scores_now, scored = mid, g, r_, s_, sc_
    drift = drift_monitors(paths, tenders, aw, scores_now, args)
    # persisted so the dashboard can show the monitors (SUBSCRIPTIONS.md phase 5)
    write_json(paths.drift, {'at': now_utc().isoformat(timespec='seconds'), **drift})
    trial_lines = []
    if plan.is_trial:
        row = experiments.state(paths.ledger_home)[plan.experiment.id]
        v, _ = experiments.read_verdict(paths.ledger_home, paths.models, plan.experiment,
                                        row, now_utc().date().isoformat())
        trial_lines.append(experiments.status_line(plan.experiment, v))
        print(f'[experiment] {trial_lines[-1]}')
    report(paths, tenders, args, record, gate, drift, model_id, len(new_grades), len(rows),
           trial_lines=trial_lines)
    learn_references(paths, tenders, awards, args)
    deliver(paths, scored, args)
    import simulation
    simulation.simulate(paths.data, scored, tenders, aw,
                        max_picks=args.sim_max_picks,
                        min_deadline_days=args.sim_min_deadline_days)

    try:
        import render_dashboard
        render_dashboard.main(data_dir=paths.data, models_dir=paths.models)
    except Exception as e:  # the dashboard is a convenience; never fail the cycle over it
        print(f'[dashboard] rendering failed: {e}')

    # The public site (doc/TRADE_PAGES.md): the hand-written pages copied from
    # `site/` plus the generated trade pages, built into `<data>/public/`.
    #
    # Into the DATA directory, not back into `site/`: in the container the
    # checkout is the image, so a build writing there would be discarded with
    # the container and would break the read-only filesystem the cycle runs
    # under. This is also why the generated pages are not committed.
    #
    # Non-fatal by the same rule the dashboard gets: a week-stale market page
    # is acceptable, a missing customer report is not. Nothing uploads here:
    # the edge serves `<data>/public/current` directly, and `release` swaps it
    # all-or-nothing. A page-TEMPLATE change does not wait for Monday either —
    # docker/deploy.sh runs the same build after every deploy.
    if not args.skip_trade_pages:
        try:
            import trade_pages
            built, skipped = trade_pages.build(paths.data)
            print(f'[public] site -> {paths.data / "public"}: '
                  f'{len(built)} trade pages, {len(skipped)} trades below the '
                  f'floor of {trade_pages.MIN_AWARDED} awarded lots')
        except Exception as e:                                 # noqa: BLE001
            print(f'[public] site build failed, cycle continues: {e!r}')

    prune_caches(paths)

    checkpoint['last_success_at'] = now_utc().isoformat(timespec='seconds')
    if date_to:
        checkpoint['last_success_to'] = date_to
    write_json(paths.checkpoint, checkpoint)
    print('[done]')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    run = sub.add_parser('run', help='execute one full cycle')
    run.add_argument('--last', default='7d', metavar='NdNwNm',
                     help='download window: the last N days/weeks/months (default 7d)')
    run.add_argument('--cpv', default='45', help='CPV scope (default 45 = construction)')
    run.add_argument('--country', default='DEU', help='buyer country (default DEU)')
    run.add_argument('--threshold', type=float, default=0.5, help='flagging cut-off')
    run.add_argument('--val-window', default='8w', dest='val_window',
                     help='validation window for the promotion gate (default 8w)')
    run.add_argument('--track-window', default='12w', dest='track_window',
                     help='track-record reporting window (default 12w)')
    run.add_argument('--min-val-lots', type=int, default=30, dest='min_val_lots',
                     help='minimum lots in the validation window to run the gate')
    run.add_argument('--min-shuffle-positives', type=int, default=20, dest='min_shuffle_positives',
                     help='minimum positive val lots for the shuffled-label check to be meaningful')
    run.add_argument('--top-slice', type=float, default=0.2, dest='top_slice',
                     help='share of the ranking counted as "our picks" in rank-based metrics (default 0.2)')
    run.add_argument('--tier-high', type=float, default=0.10, dest='tier_high',
                     help='share of the weekly ranking tiered HIGH (default 0.10)')
    run.add_argument('--tier-medium', type=float, default=0.20, dest='tier_medium',
                     help='share of the weekly ranking tiered MEDIUM, after HIGH (default 0.20)')
    run.add_argument('--min-trade-grades', type=int, default=25, dest='min_trade_grades',
                     help='minimum graded lots per trade before its track record is reported')
    run.add_argument('--min-flag-grades', type=int, default=30, dest='min_flag_grades',
                     help='graded lots below which the precision/recall section says so '
                          'out loud before quoting itself (default 30)')
    run.add_argument('--min-slice-grades', type=int, default=25, dest='min_slice_grades',
                     help='minimum graded lots in a subscription slice before its own '
                          'track record is quoted (below: the fallback ladder speaks)')
    run.add_argument('--promote-epsilon', type=float, default=0.005, dest='promote_epsilon',
                     help='candidate may trail the champion by this much and still promote')
    run.add_argument('--drift-window', default='4w', dest='drift_window',
                     help='"recent" window for the drift monitors (default 4w)')
    run.add_argument('--drift-min-lots', type=int, default=30, dest='drift_min_lots',
                     help='minimum rows on each side before a drift monitor speaks (default 30)')
    run.add_argument('--missing-jump', type=float, default=0.15, dest='missing_jump',
                     help='null-rate change that counts as missingness drift (default 0.15)')
    run.add_argument('--psi-warn', type=float, default=0.25, dest='psi_warn',
                     help='PSI above which the score distribution has drifted (default 0.25)')
    run.add_argument('--iterations', type=int, default=None,
                     help='CatBoost iterations override (testing)')
    run.add_argument('--report-top', type=int, default=30, dest='report_top',
                     help='open lots listed in the report')
    run.add_argument('--sim-max-picks', type=int, default=5, dest='sim_max_picks',
                     help='picks per simulated winner company per cycle (default 5)')
    run.add_argument('--sim-min-deadline-days', type=int, default=14,
                     dest='sim_min_deadline_days',
                     help='deadline floor for simulated picks, like the product default')
    run.add_argument('--data-dir', default=config.data_root(), dest='data_dir')
    run.add_argument('--models-dir', default=config.models_root(), dest='models_dir')
    run.add_argument('--skip-download', action='store_true', dest='skip_download',
                     help='reuse the existing store (offline run)')
    run.add_argument('--skip-trade-pages', action='store_true',
                     dest='skip_trade_pages',
                     help='do not rebuild site/gewerke/ (doc/TRADE_PAGES.md)')
    run.set_defaults(func=cmd_run)
    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
