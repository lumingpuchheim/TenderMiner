"""TenderMining calibration — RELEVANCE.md phase 2.

Derives the default `min_relevance` gate threshold and the trusted-code list from
the data, no customers required.

Positives: each repeat winner's held-out win scored (max cosine) against the firm's
remaining references — the threshold is set so the recall promise holds ("90% of a
firm's own wins pass their own gate").

Random lots are NOT negatives (decision 2026-08-04): a random lot won by another
firm of the same trade is a competitor's win, which the gate must pass. And naively
deep-coded lots are not negatives either — deep codes can be nonsense, so only
codes that prove themselves (cohesion: their lots read alike) may label a lot
"definitely another trade". The pass-rate over random lots is reported as admitted
market volume, never as an error rate.

The gate is reported in three configurations (RELEVANCE.md):
  A  text-only vs naively deep-coded negatives   (historical baseline, label noise)
  B  text-only vs trusted negatives              (label noise removed)
  C  hybrid: profile expansion + auto-pass       (the shipping configuration)

Artifacts: calibration_<model_tag>.md (receipt) and trusted_codes_<model_tag>.json.
Re-run only when the embedding model (MODEL_TAG) changes or the store has grown
substantially.

Usage:
    python calibrate.py                   # writes both artifacts
    python calibrate.py --data-dir data
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from embed import KEY, MODEL_TAG, load_sidecar

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

MIN_WINS = 3            # a firm needs this many embedded wins to contribute
RECALL_TARGET = 0.90    # share of held-out wins that must pass their own gate
NEG_PER_FIRM = 50       # clean negatives sampled per firm
VOL_PER_FIRM = 200      # random lots sampled per firm for the volume estimate
TRUST_MIN_LOTS = 10     # below this, cohesion is too noisy to certify a code
TRUST_MARGIN = 0.15     # trusted = cohesion >= random baseline + this
COHESION_SAMPLE = 60    # lots sampled per code for the cohesion estimate
PSEUDO_REF_CAP = 200    # pseudo-references kept per trusted code (closest first)
SEED = 7


def is_deep(cpv):
    """More specific than class level: digits 5-8 carry information."""
    return isinstance(cpv, str) and len(cpv) == 8 and cpv[4:] != '0000'


def firm_win_rows(awards, tenders):
    """(winner_name, procedure_id, lot_id, cpv_main) — one row per embedded win."""
    aw = awards[awards['winner_names'].apply(lambda x: x is not None and len(x) > 0)]
    w = aw.explode('winner_names')[['winner_names'] + KEY]
    cpv = tenders.drop_duplicates(subset=KEY)[KEY + ['cpv_main']]
    return w.merge(cpv, on=KEY, how='inner')


def code_trust(mat, cpv, rng):
    """Random-pair baseline and per-deep-code cohesion; a code is trusted when its
    lots read alike (cohesion >= baseline + TRUST_MARGIN). No code is assumed."""
    base = mat[rng.choice(len(mat), 800, replace=False)]
    baseline = float((base @ base.T).mean())
    cohesion = {}
    counts = pd.Series([c for c in cpv if is_deep(c)]).value_counts()
    for code, n in counts.items():
        if n < TRUST_MIN_LOTS:
            break  # value_counts is sorted; everything after is smaller
        ix = np.flatnonzero(cpv == code)
        s = rng.choice(ix, min(len(ix), COHESION_SAMPLE), replace=False)
        S = mat[s] @ mat[s].T
        cohesion[code] = {'n': int(n),
                          'cohesion': float((S.sum() - len(s)) / (len(s) ** 2 - len(s)))}
    cut = baseline + TRUST_MARGIN
    trusted = {c for c, v in cohesion.items() if v['cohesion'] >= cut}
    return baseline, cut, cohesion, trusted


def pseudo_refs(mat, cpv, trusted, cohesion, baseline):
    """Per trusted code: member rows that survive the outlier guard, capped to the
    PSEUDO_REF_CAP most typical. Outlier = mean similarity to siblings clearly
    below the code's cohesion (past halfway toward the random baseline)."""
    pools = {}
    for code in trusted:
        ix = np.flatnonzero(cpv == code)
        S = mat[ix] @ mat[ix].T
        mean_sim = (S.sum(axis=1) - 1.0) / (len(ix) - 1)
        floor = (cohesion[code]['cohesion'] + baseline) / 2
        keep = ix[mean_sim >= floor]
        order = np.argsort(-mean_sim[mean_sim >= floor])
        pools[code] = keep[order][:PSEUDO_REF_CAP]
    return pools


def threshold_for_recall(scores, n_auto, target):
    """Threshold on embedding scores such that (auto-passes + scores above it)
    reach the recall target over the whole positive set."""
    total = len(scores) + n_auto
    needed = max(0.0, min(1.0, (target * total - n_auto) / max(len(scores), 1)))
    return float(np.quantile(scores, 1 - needed)) if needed > 0 else float(
        np.quantile(scores, 0.90))


def calibrate(data_dir):
    rows, mat = load_sidecar(data_dir)
    pos_of = {(r['procedure_id'], r['lot_id']): i for i, r in enumerate(rows)}
    tenders = pd.read_parquet(Path(data_dir) / 'store' / 'tenders.parquet')
    awards = pd.read_parquet(Path(data_dir) / 'store' / 'awards.parquet')

    wins = firm_win_rows(awards, tenders)
    wins['row'] = [pos_of.get((p, l)) for p, l in zip(wins['procedure_id'], wins['lot_id'])]
    wins = wins.dropna(subset=['row']).astype({'row': int}).drop_duplicates(
        subset=['winner_names', 'row'])
    by_firm = {name: g for name, g in wins.groupby('winner_names') if len(g) >= MIN_WINS}
    print(f'[calibrate] {len(by_firm)} firms with >= {MIN_WINS} embedded wins '
          f'({sum(len(g) for g in by_firm.values())} wins total)')

    lots = tenders.drop_duplicates(subset=KEY)
    key_cpv = dict(zip(zip(lots['procedure_id'], lots['lot_id']), lots['cpv_main']))
    all_cpv = np.array([key_cpv.get((r['procedure_id'], r['lot_id'])) for r in rows],
                       dtype=object)
    deep_mask = np.array([is_deep(c) for c in all_cpv])
    all_class = np.array([c[:4] if isinstance(c, str) else '' for c in all_cpv])

    rng = np.random.default_rng(SEED)
    baseline, cut, cohesion, trusted = code_trust(mat, all_cpv, rng)
    print(f'[calibrate] cohesion baseline {baseline:.3f}, trust cut {cut:.3f}: '
          f'{len(trusted)}/{len(cohesion)} deep codes trusted')
    pools = pseudo_refs(mat, all_cpv, trusted, cohesion, baseline)

    pos_text, pos_hyb, auto_pass = [], [], 0
    neg_naive, neg_trust, neg_hyb, volumes = [], [], [], []
    for name, g in by_firm.items():
        idx = g['row'].to_numpy()
        codes = g['cpv_main'].to_numpy(dtype=object)
        sims = mat[idx] @ mat[idx].T
        np.fill_diagonal(sims, -1.0)
        pos_text.extend(sims.max(axis=1))

        # hybrid positives: held-out win vs other wins + their trusted-code pools
        for i in range(len(idx)):
            others = np.delete(idx, i)
            other_codes = {c for c in np.delete(codes, i) if c in trusted}
            if isinstance(codes[i], str) and codes[i] in other_codes:
                auto_pass += 1
                continue
            ref = others
            for c in other_codes:
                ref = np.concatenate([ref, pools[c][pools[c] != idx[i]]])
            pos_hyb.append(float((mat[ref] @ mat[idx[i]]).max()))

        # negatives: deep different-class (naive) vs trusted-only (clean)
        firm_classes = {c[:4] for c in codes if isinstance(c, str)}
        off_class = ~np.isin(all_class, list(firm_classes))
        naive_pool = np.flatnonzero(deep_mask & off_class)
        trust_pool = np.flatnonzero(
            np.isin(all_cpv.astype(str), list(trusted)) & off_class)
        full_ref = idx
        for c in {c for c in codes if c in trusted}:
            full_ref = np.concatenate([full_ref, pools[c]])
        full_ref = np.unique(full_ref)
        for pool, sink, ref in ((naive_pool, neg_naive, idx),
                                (trust_pool, neg_trust, idx),
                                (trust_pool, neg_hyb, full_ref)):
            pool = pool[~np.isin(pool, idx)]
            if len(pool):
                pick = rng.choice(pool, min(NEG_PER_FIRM, len(pool)), replace=False)
                sink.append((mat[pick] @ mat[ref].T).max(axis=1))

        vol = rng.choice(len(mat), VOL_PER_FIRM, replace=False)
        volumes.append((mat[vol] @ mat[full_ref].T).max(axis=1))

    pos_text = np.array(pos_text)
    pos_hyb = np.array(pos_hyb)
    neg = {k: np.concatenate(v) for k, v in
           (('naive', neg_naive), ('trusted', neg_trust), ('hybrid', neg_hyb))}
    volumes = np.concatenate(volumes)

    thr_text = float(np.quantile(pos_text, 1 - RECALL_TARGET))
    thr_hyb = threshold_for_recall(pos_hyb, auto_pass, RECALL_TARGET)
    n_pos = len(pos_hyb) + auto_pass
    return {
        'n_firms': len(by_firm), 'n_positives': len(pos_text),
        'baseline': baseline, 'trust_cut': cut, 'n_deep_codes': len(cohesion),
        'n_trusted': len(trusted), 'cohesion': cohesion,
        'pos_text': pos_text, 'pos_hyb': pos_hyb, 'auto_pass': auto_pass,
        'neg': neg, 'volumes': volumes,
        'configs': {
            'A text-only, naive negatives': {
                'threshold': thr_text,
                'recall': float((pos_text >= thr_text).mean()),
                'leakage': float((neg['naive'] >= thr_text).mean())},
            'B text-only, trusted negatives': {
                'threshold': thr_text,
                'recall': float((pos_text >= thr_text).mean()),
                'leakage': float((neg['trusted'] >= thr_text).mean())},
            'C hybrid (expansion + auto-pass)': {
                'threshold': thr_hyb,
                'recall': float(
                    (auto_pass + (pos_hyb >= thr_hyb).sum()) / n_pos),
                'auto_pass_share': float(auto_pass / n_pos),
                'leakage': float((neg['hybrid'] >= thr_hyb).mean()),
                'volume': float((volumes >= thr_hyb).mean())},
        },
    }


def hist_lines(scores, threshold, lo=0.2, hi=1.0, bins=16, width=40):
    counts, edges = np.histogram(scores, bins=bins, range=(lo, hi))
    peak = counts.max() or 1
    out = []
    for c, a, b in zip(counts, edges, edges[1:]):
        mark = ' <-- threshold' if a <= threshold < b else ''
        out.append(f'    {a:.2f}-{b:.2f}  {"#" * round(width * c / peak):<{width}} '
                   f'{c}{mark}')
    return out


def write_receipt(path, r):
    c = r['configs']['C hybrid (expansion + auto-pass)']
    lines = [
        f'# Calibration receipt — {MODEL_TAG}',
        '',
        f'Generated by `python calibrate.py` on {date.today().isoformat()}. '
        f'Re-run on embedding model change or substantial store growth '
        f'(RELEVANCE.md phase 2).',
        '',
        f'- Firms (>= {MIN_WINS} embedded wins): **{r["n_firms"]}**; '
        f'held-out positives: **{r["n_positives"]}**',
        f'- Trusted codes: **{r["n_trusted"]}** of {r["n_deep_codes"]} deep codes '
        f'with >= {TRUST_MIN_LOTS} lots (cohesion >= {r["trust_cut"]:.3f}; '
        f'random baseline {r["baseline"]:.3f})',
        f'- **Default `min_relevance` = {c["threshold"]:.3f}** '
        f'(hybrid gate, recall promise {RECALL_TARGET:.0%})',
        '',
        '| configuration | threshold | recall | wrong-trade leakage |',
        '| --- | --- | --- | --- |',
    ]
    for name, v in r['configs'].items():
        lines.append(f'| {name} | {v["threshold"]:.3f} | {v["recall"]:.1%} '
                     f'| {v["leakage"]:.1%} |')
    lines += [
        '',
        f'- Hybrid auto-pass covers {c["auto_pass_share"]:.1%} of positives '
        f'(trusted code shared with the profile — no embedding needed).',
        f'- Admitted market volume at the hybrid gate: {c["volume"]:.1%} of random '
        f'lots (a sizing number, not an error rate).',
        '',
        'Hybrid positive scores (held-out win vs expanded profile, '
        'auto-passes excluded):',
        '',
        *hist_lines(r['pos_hyb'], c['threshold']),
        '',
        'Trusted-negative scores vs expanded profiles:',
        '',
        *hist_lines(r['neg']['hybrid'], c['threshold']),
        '',
    ]
    path.write_text('\n'.join(lines), encoding='utf-8')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', default='data')
    args = ap.parse_args()
    r = calibrate(args.data_dir)
    receipt = Path(f'calibration_{MODEL_TAG}.md')
    write_receipt(receipt, r)
    codes = Path(f'trusted_codes_{MODEL_TAG}.json')
    codes.write_text(json.dumps(
        {'model_tag': MODEL_TAG, 'generated': date.today().isoformat(),
         'baseline': round(r['baseline'], 4), 'cut': round(r['trust_cut'], 4),
         'min_lots': TRUST_MIN_LOTS,
         'codes': {k: {'n': v['n'], 'cohesion': round(v['cohesion'], 4),
                       'trusted': v['cohesion'] >= r['trust_cut']}
                   for k, v in sorted(r['cohesion'].items())}},
        indent=1), encoding='utf-8')
    for name, v in r['configs'].items():
        extra = (f', volume {v["volume"]:.1%}' if 'volume' in v else '')
        print(f'[calibrate] {name}: thr={v["threshold"]:.3f} '
              f'recall={v["recall"]:.1%} leakage={v["leakage"]:.1%}{extra}')
    print(f'[calibrate] receipts -> {receipt}, {codes}')


if __name__ == '__main__':
    main()
