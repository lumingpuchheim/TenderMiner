"""The two receipts that gate the all-trades flag day (pipeline/all-trades.md).

Receipt A — is there a product in IT? Per division (45 reference, 48, 72):
labeled lots, single-bid base rate, weekly published volume, and whether a
champion-recipe model ranks that division's validation lots above chance.

Receipt B — does construction survive? Train twice with the cycle's exact
recipe — once on the 45-only store, once on 45+48+72 — and grade both on the
IDENTICAL construction-only validation window (same lots, same denominator).
Pass: widened PR-AUC >= 45-only PR-AUC - 0.005 (the promotion epsilon).

Prints to the console; writes nothing outside --scratch. Reads the raw XML
archive through features.py (production code), so the two stores are built the
way the cycle builds them, not approximated.

    python receipt_all_trades.py --data-dir /data --scratch /data/scratch/receipts
"""

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import single_bidder as sb
import util

EPSILON = 0.005          # cycle.py --promote-epsilon default, the bar Receipt B uses
TOP_SLICE = 0.2          # cycle.py --top-slice default
VAL_WINDOW = '8w'        # cycle.py --val-window default
DIVISIONS = ('45', '48', '72')


def build_store(xml_dir, cpv, tenders_out, awards_out):
    """features.py, the production refiner — never a private parser."""
    if tenders_out.exists() and awards_out.exists():
        print(f'[build] {tenders_out.name} exists — reusing')
        return
    subprocess.run(
        [sys.executable, str(Path(__file__).parent / 'features.py'),
         '--xml-dir', str(xml_dir), '--cpv', cpv,
         '--tenders-out', str(tenders_out), '--awards-out', str(awards_out)],
        check=True)


def slice_45(tenders, awards):
    """The 45-only store, derived in memory from the widened frames — the same
    rows features.py --cpv 45 selects (cpv_main prefix, awards follow their
    tenders), without a second pass over 170k XML files."""
    t = tenders[tenders['cpv_main'].astype(str).str.startswith('45')].copy()
    keys = set(map(tuple, t[sb.KEY].drop_duplicates().values))
    a = awards[[tuple(r) in keys for r in awards[sb.KEY].values]].copy()
    return t, a


def arm(tag, tenders, roles, awards, threshold_date):
    """One training arm, the cycle's exact recipe: assemble, training window,
    multihot vocabulary on the full tenders frame, temporal split at the given
    date, eval model on the train side only."""
    data, _, n_dropped = sb.assemble(tenders, awards)
    data = sb.training_window(data)
    multihot = sb.fit_multihot(tenders, roles, feature_build=sb.FEATURE_BUILD)
    X, cat_cols, num_cols, _ = sb.build_features(
        data, roles, list_frame=tenders, multihot=multihot,
        feature_build=sb.FEATURE_BUILD)
    split = sb.temporal_split(data, X, threshold=threshold_date)
    print(f'[{tag}] {data.groupby(sb.KEY).ngroups} labeled lots '
          f'({n_dropped} reporting errors dropped), {X.shape[1]} features, '
          f'{split.n_train_lots} train / {split.n_test_lots} val lots')
    model = sb.train(split.Xtr, split.ytr, split.wtr, cat_cols)
    p_val = sb.predict(model, split.Xte)
    val_div = data.loc[~split.is_train, 'cpv_main'].astype(str).str[:2].to_numpy()
    return data, split, p_val, val_div


def slice_grades(split, p_val, mask, label):
    if not mask.any():
        return {'label': label, 'n_rows': 0, 'base_rate': float('nan')}
    y, p, w = split.yte[mask], p_val[mask], split.wte[mask]
    out = {'label': label, 'n_rows': int(mask.sum()),
           'base_rate': float(np.average(y, weights=w))}
    if len(set(y)) == 2:
        out['pr_auc'] = sb.metrics(y, p, w)['pr_auc']
        k = max(1, round(len(p) * TOP_SLICE))
        idx = np.argsort(-p)[:k]
        out['top_hit'] = float(np.average(y[idx], weights=w[idx]))
        out['top_lift'] = out['top_hit'] / out['base_rate'] if out['base_rate'] else None
    return out


def show(g):
    lift = f"{g['top_lift']:.2f}x" if g.get('top_lift') else '—'
    pr = f"{g['pr_auc']:.4f}" if 'pr_auc' in g else '—'
    print(f"  {g['label']:<28} n={g['n_rows']:<6} base={g['base_rate']:.3f} "
          f"PR-AUC={pr} top-{int(TOP_SLICE*100)}% lift={lift}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data-dir', default='data')
    ap.add_argument('--scratch', default=None)
    args = ap.parse_args()
    data_dir = Path(args.data_dir)
    scratch = Path(args.scratch or data_dir / 'scratch' / 'receipts')
    scratch.mkdir(parents=True, exist_ok=True)

    t_wide = scratch / 'tenders_wide.parquet'
    a_wide = scratch / 'awards_wide.parquet'
    build_store(data_dir / 'raw' / 'xml', '45,48,72', t_wide, a_wide)

    tenders, roles = sb.load_with_roles(t_wide)
    awards, _ = sb.load_with_roles(a_wide)
    print(f'[store] widened: {len(tenders)} tender rows, {len(awards)} award rows, '
          f'feature build {sb.FEATURE_BUILD}')

    pub = pd.to_datetime(tenders['publication_date'])
    threshold_date = pub.max() - util.parse_window(VAL_WINDOW)
    print(f'[split] validation = lots first published after {threshold_date.date()}')

    # ---- volume per division (what a customer of that trade would see)
    print('\n== Receipt A — the IT product ==')
    div = tenders['cpv_main'].astype(str).str[:2]
    recent = pub >= pub.max() - pd.Timedelta(weeks=26)
    for d in DIVISIONS:
        lots = tenders[(div == d)]
        weekly = (tenders[(div == d) & recent].groupby(sb.KEY).ngroups) / 26
        print(f'  division {d}: {lots.groupby(sb.KEY).ngroups} lots in store, '
              f'~{weekly:.0f} new lots/week (26-week mean)')

    # ---- widened arm (also serves Receipt B)
    t45, a45 = slice_45(tenders, awards)
    data_w, split_w, p_w, div_w = arm('wide 45+48+72', tenders, roles, awards,
                                      threshold_date)
    for d in DIVISIONS:
        show(slice_grades(split_w, p_w, div_w == d, f'division {d} (widened model)'))

    # ---- Receipt B: 45-only arm, identical construction exam
    print('\n== Receipt B — does construction survive? ==')
    data_c, split_c, p_c, div_c = arm('45-only', t45, roles, a45, threshold_date)
    g_c = slice_grades(split_c, p_c, np.ones(len(p_c), bool), 'construction, 45-only model')
    g_w = slice_grades(split_w, p_w, div_w == '45', 'construction, widened model')
    if g_c['n_rows'] != g_w['n_rows']:
        print(f'  WARNING: exam sizes differ ({g_c["n_rows"]} vs {g_w["n_rows"]}) '
              f'— the comparison is not the same exam')
    show(g_c)
    show(g_w)
    gap = g_w['pr_auc'] - g_c['pr_auc']
    verdict = 'PASS' if gap >= -EPSILON else 'FAIL'
    print(f'\n  construction PR-AUC: 45-only {g_c["pr_auc"]:.4f} -> '
          f'widened {g_w["pr_auc"]:.4f} (gap {gap:+.4f}, epsilon {EPSILON})')
    print(f'  RECEIPT B: {verdict}')
    return 0 if verdict == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
