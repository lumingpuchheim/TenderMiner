"""CPV depth receipt — why cpv_main is expanded to 8 digits (TRAINING.md).

Regenerates every number behind the depth decision of 2026-08-06. Read-only:
no model is registered, no champion is touched, nothing is written unless
--out is given.

The question: the competition model used to see the main CPV code truncated to
2, 3 and 4 digits. Digits 5-8 name the actual trade (45312310 = Blitzschutz,
not just 4531 = electrical installation). Does that depth predict "0-1 bids"?

Four measurements, in order of how much they can be argued with:

  1. cardinality — what one_hot_max_size actually permits, per level, on the
     frame loop.learn() trains on. This is the constraint, not a preference.
  2. within-cpv4 spread — how far cpv6 sub-buckets diverge inside a cpv4
     bucket the model can only price at one rate, against a permutation null
     that destroys sub-bucket structure while keeping bucket sizes.
  3. A/B retrain — the shipped feature build vs deeper variants on the
     temporal holdout, several seeds, and across several split dates.
  4. tripwires — the shuffled-label and too-good checks on the deep arm, so a
     gain cannot be a leak wearing a new column.

Usage:
    python cpv_depth_receipt.py
    python cpv_depth_receipt.py --quick          # skip the split sweep
    python cpv_depth_receipt.py --out receipt.md
"""

import argparse
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np
import pandas as pd

import config
import single_bidder as sb
from calibrate import is_deep

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# every arm differs from the shipped build ONLY in the cpv_main levels
ARMS = {
    'A pre-2026-08-06 (cpv2,3,4)': [('cpv2', 2), ('cpv3', 3), ('cpv4', 4)],
    'B +cpv5/6        (cpv3,4,5,6)': [('cpv3', 3), ('cpv4', 4), ('cpv5', 5), ('cpv6', 6)],
    'C shipped        (cpv3,4,6,8)': [('cpv3', 3), ('cpv4', 4), ('cpv6', 6), ('cpv8', 8)],
    'D drop-constant  (cpv3,4)': [('cpv3', 3), ('cpv4', 4)],
}
SHIPPED = 'C shipped        (cpv3,4,6,8)'
BASELINE = 'A pre-2026-08-06 (cpv2,3,4)'
_ORIG_LEVELS = sb._hier_levels


def build(data, tenders, roles, levels):
    """Feature build with cpv_main forced to `levels`; everything else as shipped."""
    sb._hier_levels = (lambda col: levels if col == 'cpv_main' else _ORIG_LEVELS(col))
    try:
        X, cats, nums, _ = sb.build_features(data, roles, list_frame=tenders)
    finally:
        sb._hier_levels = _ORIG_LEVELS
    return X, cats


def wrate(g):
    return float(np.average(g['y'], weights=g['w']))


def lot_frame(data):
    """1/k-weighted rows (leakage rule 3) with the codes we slice by."""
    k = data.groupby(sb.KEY)['label'].transform('size')
    df = pd.DataFrame({
        'y': data['label'].to_numpy(),
        'w': (1.0 / k).to_numpy(),
        'cpv': data['cpv_main'].astype('string').fillna('').to_numpy(),
    })
    df['cpv4'] = df['cpv'].str[:4]
    df['cpv6'] = df['cpv'].str[:6]
    return df


def joined(v, n):
    """cpv_additional as build_features encodes it: one combination string."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return sb.NA
    return '|'.join(sorted({str(x)[:n] for x in list(v)})) or sb.NA


def section_cardinality(data, tenders, roles):
    print('## 1. Cardinality — what the one-hot guard permits\n')
    X, cats = build(data, tenders, roles, ARMS[SHIPPED])
    card = sb.assert_pure_one_hot(X, cats)
    print(f'Shipped build, {len(cats)} categorical columns, '
          f'one_hot_max_size={sb.ONE_HOT_MAX_SIZE}: max cardinality '
          f'{int(card.max())} ({card.idxmax()}) — guard passes.\n')
    print('| level | cpv_main | cpv_additional (joined combos) | verdict |')
    print('|---|---|---|---|')
    for n in (2, 3, 4, 5, 6, 8):
        m = data['cpv_main'].map(
            lambda v, n=n: sb.NA if v is None or (isinstance(v, float) and pd.isna(v))
            else str(v)[:n]).nunique()
        a = data['cpv_additional'].map(lambda v, n=n: joined(v, n)).nunique()
        v = ('additional OVER CAP' if a > sb.ONE_HOT_MAX_SIZE else 'both fit')
        print(f'| cpv{n} | {m} | {a} | {v} |')
    print('\nSo cpv_main can go to full depth for free; cpv_additional cannot go '
          'past cpv4 at all without a different encoding.\n')

    lots = tenders.drop_duplicates(subset=sb.KEY)
    deep_main = sum(1 for v in lots['cpv_main'] if isinstance(v, str) and is_deep(v))
    n = len(lots)
    print(f'Depth of the store itself: {deep_main}/{n} ({deep_main / n:.1%}) of '
          f'stored lots have a deep cpv_main, i.e. digits 5-8 were being '
          f'discarded on that share of the market.\n')


def section_spread(data, shuffles):
    df = lot_frame(data)
    base = wrate(df)
    print('## 2. Signal below cpv4\n')
    print(f'Weighted base rate: {base:.1%}. Inside one cpv4 bucket the old build '
          'could express exactly one rate. Spread of cpv6 sub-bucket rates '
          '(buckets >= 100 weighted lots, sub-buckets >= 30):\n')

    def mean_spread(frame):
        rows = []
        for _, g4 in frame.groupby('cpv4'):
            if g4['w'].sum() < 100:
                continue
            subs = [wrate(g6) for _, g6 in g4.groupby('cpv6') if g6['w'].sum() >= 30]
            if len(subs) >= 2:
                rows.append((g4['w'].sum(), max(subs) - min(subs)))
        if not rows:
            return float('nan')
        return sum(w * s for w, s in rows) / sum(w for w, _ in rows)

    print('| cpv4 | lots | cpv4 rate | cpv6 buckets | min | max | spread |')
    print('|---|---|---|---|---|---|---|')
    rows = []
    for c4, g4 in df.groupby('cpv4'):
        if g4['w'].sum() < 100:
            continue
        subs = [wrate(g6) for _, g6 in g4.groupby('cpv6') if g6['w'].sum() >= 30]
        if len(subs) < 2:
            continue
        rows.append((c4, g4['w'].sum(), wrate(g4), len(subs),
                     min(subs), max(subs), max(subs) - min(subs)))
    for c4, w, r4, ns, lo, hi, sp in sorted(rows, key=lambda r: -r[6]):
        print(f'| {c4} | {w:.0f} | {r4:.1%} | {ns} | {lo:.1%} | {hi:.1%} '
              f'| {sp * 100:.1f}pt |')

    obs = mean_spread(df)
    rng = np.random.default_rng(11)
    null = []
    for _ in range(shuffles):
        sh = df.copy()
        # shuffle the cpv6 sub-label WITHIN each cpv4 bucket: kills real
        # sub-bucket structure, preserves bucket sizes and the cpv4 rate
        sh['cpv6'] = sh.groupby('cpv4')['cpv6'].transform(
            lambda s: rng.permutation(s.to_numpy()))
        v = mean_spread(sh)
        if not np.isnan(v):
            null.append(v)
    null = np.array(null)
    print(f'\nWeighted-mean spread **{obs * 100:.1f}pt**; permutation null over '
          f'{len(null)} shuffles: mean {null.mean() * 100:.1f}pt, 95th pct '
          f'{np.quantile(null, 0.95) * 100:.1f}pt. Observed is '
          f'{obs / null.mean():.2f}x the noise floor, p ~ {(null >= obs).mean():.3f}.\n')


def section_ab(data, tenders, roles, seeds, quantiles):
    print('## 3. A/B retrain on the temporal holdout\n')
    print(f'Seeds {list(seeds)}, split at the default quantile.\n')
    print('| arm | val PR-AUC | seed sd | ROC-AUC | precision@0.5 | recall@0.5 |')
    print('|---|---|---|---|---|---|')
    means = {}
    for name, levels in ARMS.items():
        X, cats = build(data, tenders, roles, levels)
        sb.assert_pure_one_hot(X, cats)
        split = sb.temporal_split(data, X)
        per = []
        for seed in seeds:
            m = sb.train(split.Xtr, split.ytr, split.wtr, cats, random_seed=seed)
            per.append(sb.metrics(split.yte, sb.predict(m, split.Xte), split.wte))
        mean = {k: float(np.mean([p[k] for p in per])) for k in per[0]}
        sd = float(np.std([p['pr_auc'] for p in per]))
        means[name] = mean['pr_auc']
        print(f'| {name} | {mean["pr_auc"]:.4f} | {sd:.4f} | '
              f'{mean["roc_auc"]:.4f} | {mean["precision"]:.3f} | '
              f'{mean["recall"]:.3f} |')
    X, cats = build(data, tenders, roles, ARMS[SHIPPED])
    split = sb.temporal_split(data, X)
    bl = sb.metrics(split.yte, sb.cpv4_baseline(split), split.wte)
    print(f'\nSingle-feature cpv4 baseline: PR-AUC {bl["pr_auc"]:.4f}. '
          f'Split {split.threshold.date()}, {split.n_train_lots} train / '
          f'{split.n_test_lots} test lots, test base rate '
          f'{split.base_rate_test:.1%}.\n')
    a, c = means[BASELINE], means[SHIPPED]
    print(f'Shipped arm vs the old one: **{c - a:+.4f} PR-AUC '
          f'({(c - a) / a:+.1%} relative)**. The drop-constant arm reproduces '
          f'the old arm to {abs(means["D drop-constant  (cpv3,4)"] - a):.4f} — '
          f'cpv2 was a constant column under a CPV-45 scope.\n')

    if not quantiles:
        return
    print('Across split dates (2 seeds each):\n')
    print('| quantile | split date | test lots | base rate | old | shipped | delta |')
    print('|---|---|---|---|---|---|---|')
    deltas = []
    for q in quantiles:
        vals = {}
        s = None
        for name in (BASELINE, SHIPPED):
            X, cats = build(data, tenders, roles, ARMS[name])
            s = sb.temporal_split(data, X, quantile=q)
            ps = [sb.metrics(s.yte, sb.predict(
                sb.train(s.Xtr, s.ytr, s.wtr, cats, random_seed=seed), s.Xte),
                s.wte)['pr_auc'] for seed in (42, 43)]
            vals[name] = float(np.mean(ps))
        d = vals[SHIPPED] - vals[BASELINE]
        deltas.append(d)
        print(f'| {q:.2f} | {s.threshold.date()} | {s.n_test_lots} | '
              f'{s.base_rate_test:.1%} | {vals[BASELINE]:.4f} | '
              f'{vals[SHIPPED]:.4f} | {d:+.4f} |')
    print(f'\nDelta mean {np.mean(deltas):+.4f}, range {min(deltas):+.4f} to '
          f'{max(deltas):+.4f}, positive in {sum(d > 0 for d in deltas)}/'
          f'{len(deltas)} splits.\n')


def section_tripwires(data, tenders, roles):
    print('## 4. Tripwires on the shipped (deep) arm\n')
    X, cats = build(data, tenders, roles, ARMS[SHIPPED])
    split = sb.temporal_split(data, X)
    mapping = sb.permuted_lot_labels(data, mask=split.is_train)
    y_shuf = sb.labels_from_mapping(data, split.is_train, mapping)
    m = sb.train(split.Xtr, y_shuf, split.wtr, cats)
    pr = sb.metrics(split.yte, sb.predict(m, split.Xte), split.wte)['pr_auc']
    ok_shuf = pr < split.base_rate_test * 1.5
    real = sb.train(split.Xtr, split.ytr, split.wtr, cats)
    mm = sb.metrics(split.yte, sb.predict(real, split.Xte), split.wte)
    ok_good = mm['roc_auc'] < sb.TOO_GOOD_ROC
    print(f'- shuffled labels: PR-AUC {pr:.4f} vs test base rate '
          f'{split.base_rate_test:.4f} — '
          f'{"PASS (collapsed)" if ok_shuf else "FAIL"}')
    print(f'- too-good alarm: ROC-AUC {mm["roc_auc"]:.4f} < '
          f'{sb.TOO_GOOD_ROC} — {"PASS" if ok_good else "FAIL"}\n')
    imp = sb.feature_importance(real, split.Xtr, split.ytr, split.wtr, cats)
    order = list(imp.index)
    print(f'Feature importance rank of the CPV columns, out of {len(order)}:\n')
    for c in ('cpv_main__cpv8', 'cpv_main__cpv6', 'cpv_main__cpv4',
              'cpv_main__cpv3', 'cpv_additional__cpv4'):
        if c in imp.index:
            print(f'- {c}: rank {order.index(c) + 1} (importance {imp[c]:.2f})')
    print()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', default=config.data_root())
    ap.add_argument('--quick', action='store_true',
                    help='one split and 40 permutations instead of the full sweep')
    ap.add_argument('--out', default=None, metavar='PATH',
                    help='also write the receipt as markdown')
    args = ap.parse_args()

    D = Path(args.data_dir)
    tenders, roles = sb.load_with_roles(D / 'store' / 'tenders.parquet')
    awards, _ = sb.load_with_roles(D / 'store' / 'awards.parquet')
    data, aw, dropped = sb.assemble(tenders, awards)

    buf = io.StringIO()
    with redirect_stdout(buf):
        print('# CPV depth receipt\n')
        print(f'Store: {len(tenders)} tender rows, {len(data)} labeled rows, '
              f'{data.groupby(sb.KEY).ngroups} labeled lots '
              f'({dropped} reporting errors dropped).\n')
        section_cardinality(data, tenders, roles)
        section_spread(data, shuffles=40 if args.quick else 200)
        section_ab(data, tenders, roles,
                   seeds=(42, 43) if args.quick else (42, 43, 44),
                   quantiles=() if args.quick else (0.70, 0.75, 0.80, 0.85))
        section_tripwires(data, tenders, roles)
        print('Scope caveat: one store snapshot, CPV 45 / Germany. When the '
              'scope widens, cpv2 and cpv3 stop being near-constant and every '
              'cardinality above must be re-checked against one_hot_max_size.')
    text = buf.getvalue()
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding='utf-8')
        print(f'[receipt] written to {args.out}')


if __name__ == '__main__':
    main()
