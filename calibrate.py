"""TenderMining calibration — RELEVANCE.md phase 2.

Derives the default gate thresholds and the trusted-code list from the data, no
customers required.

Positives: each repeat winner's held-out win scored against the firm's remaining
references — thresholds are set so the recall promise holds ("90% of a firm's own
wins pass their own gate").

Random lots are NOT negatives (decision 2026-08-04): a random lot won by another
firm of the same trade is a competitor's win, which the gate must pass. And naively
deep-coded lots are not negatives either — deep codes can be nonsense, so only
codes that prove themselves (cohesion: their lots read alike) may label a lot
"definitely another trade". The pass-rate over random lots is reported as admitted
market volume, never as an error rate.

The gate is reported in four configurations (RELEVANCE.md):
  A  text-only vs naively deep-coded negatives   (historical baseline, label noise)
  B  text-only vs trusted negatives              (label noise removed)
  C  hybrid: profile expansion + auto-pass
  D  hybrid + code-label channel                 (the shipping configuration)

Artifacts: calibration_<model_tag>.md (receipt) and trusted_codes_<model_tag>.json.
Requires both sidecars (python embed.py --labels). Re-run only when the embedding
model (MODEL_TAG) changes or the store has grown substantially.

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

from embed import KEY, MODEL_TAG, load_label_sidecar, load_sidecar

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
FP_K = 8                # trade-fingerprint size (top label matches)
CODE_GRID = np.arange(0.70, 1.0, 0.025)  # code-channel thresholds tried
# configuration F: the fingerprint splits by origin. HARD codes are facts
# (trusted codes on the customer's actual won lots); SOFT labels are our own
# guesses (dictionary labels near the reference texts) and must earn
# membership — at least `consensus` references above `floor` — and even then
# never carry exact-match certainty (their threshold is searched separately;
# 2.0 in the grid = soft channel off).
SOFT_FLOORS = (0.45, 0.50, 0.55, 0.60)
SOFT_CONSENSUS = (1, 2)
# starts at CODE_GRID's floor so the search space nests configuration E
# (single threshold for both origins); 2.0 = soft channel off
SOFT_GRID = np.concatenate([np.arange(0.70, 1.0, 0.025), [2.0]])
SEED = 7


def is_deep(cpv):
    """More specific than class level: digits 5-8 carry information."""
    return isinstance(cpv, str) and len(cpv) == 8 and cpv[4:] != '0000'


def firm_win_rows(awards, tenders):
    """(winner_name, procedure_id, lot_id, cpv_main, cpv_additional) — one row
    per embedded win."""
    aw = awards[awards['winner_names'].apply(lambda x: x is not None and len(x) > 0)]
    w = aw.explode('winner_names')[['winner_names'] + KEY]
    cpv = tenders.drop_duplicates(subset=KEY)[KEY + ['cpv_main', 'cpv_additional']]
    return w.merge(cpv, on=KEY, how='inner')


def lot_codes(main, add):
    """All of a lot's CPV codes, main + additional — the channel must see both
    (buyers often put the real trade in the additional codes)."""
    out = [main] if isinstance(main, str) else []
    if add is not None and not isinstance(add, float):
        out.extend(str(a) for a in add)
    return out


def best_code_score(codes, fp_rows, lmat, lpos):
    """Code channel over a code list: best deep, dictionary-known code wins."""
    scores = [code_score(c, fp_rows, lmat, lpos)
              for c in codes if is_deep(c) and c in lpos]
    return max(scores) if scores else 0.0


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


def fingerprint(win_rows, win_codes, mat, lmat, lpos, trusted):
    """Label rows of the profile's trade fingerprint: the FP_K labels closest to
    any reference text, plus the references' own trusted codes."""
    sims = (lmat @ mat[win_rows].T).max(axis=1)
    fp = set(np.argsort(-sims)[:FP_K].tolist())
    for c in win_codes:
        if c in trusted and c in lpos:
            fp.add(lpos[c])
    return np.fromiter(fp, int, len(fp))


def code_score(code, fp_rows, lmat, lpos):
    """Code channel: best label-to-label cosine between the candidate's deep code
    and the fingerprint. A code outside the dictionary contributes nothing."""
    if is_deep(code) and code in lpos:
        return float((lmat[fp_rows] @ lmat[lpos[code]]).max())
    return 0.0


def threshold_for_recall(scores, n_auto, target):
    """Threshold on embedding scores such that (auto-passes + scores above it)
    reach the recall target over the whole positive set."""
    total = len(scores) + n_auto
    needed = max(0.0, min(1.0, (target * total - n_auto) / max(len(scores), 1)))
    return float(np.quantile(scores, 1 - needed)) if needed > 0 else float(
        np.quantile(scores, 0.90))


def calibrate(data_dir):
    rows, mat = load_sidecar(data_dir)
    lpos, lrows, lmat = load_label_sidecar(data_dir)
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
    key_add = dict(zip(zip(lots['procedure_id'], lots['lot_id']),
                       lots['cpv_additional']))
    all_cpv = np.array([key_cpv.get((r['procedure_id'], r['lot_id'])) for r in rows],
                       dtype=object)
    all_codes = [lot_codes(key_cpv.get((r['procedure_id'], r['lot_id'])),
                           key_add.get((r['procedure_id'], r['lot_id'])))
                 for r in rows]
    deep_mask = np.array([is_deep(c) for c in all_cpv])
    all_class = np.array([c[:4] if isinstance(c, str) else '' for c in all_cpv])

    rng = np.random.default_rng(SEED)
    baseline, cut, cohesion, trusted = code_trust(mat, all_cpv, rng)
    print(f'[calibrate] cohesion baseline {baseline:.3f}, trust cut {cut:.3f}: '
          f'{len(trusted)}/{len(cohesion)} deep codes trusted')
    pools = pseudo_refs(mat, all_cpv, trusted, cohesion, baseline)

    # label-to-label similarities once for configuration F (~350 MB float32,
    # freed with the function; turns every code-vs-fingerprint score into a
    # table lookup)
    LL = lmat @ lmat.T
    fk = [(f, k) for f in SOFT_FLOORS for k in SOFT_CONSENSUS]

    def label_rows(codes):
        return [lpos[c] for c in codes if is_deep(c) and c in lpos]

    def best_ll(cand_rows, fp_rows):
        if not cand_rows or not len(fp_rows):
            return 0.0
        return float(LL[np.ix_(cand_rows, fp_rows)].max())

    pos_plain, pos_auto, pos_text, pos_code = [], [], [], []
    neg_naive, neg_trust = [], []
    neg_hyb_text, neg_code_s, vol_plain, vol_text, vol_code = [], [], [], [], []
    pos_hard, pos_soft = [], {key: [] for key in fk}
    neg_hard, neg_soft = [], {key: [] for key in fk}
    vol_hard, vol_soft = [], {key: [] for key in fk}
    for name, g in by_firm.items():
        idx = g['row'].to_numpy()
        win_codes = [lot_codes(m, a) for m, a in
                     zip(g['cpv_main'], g['cpv_additional'])]
        flat_codes = [c for cs in win_codes for c in cs]
        sims = mat[idx] @ mat[idx].T
        np.fill_diagonal(sims, -1.0)
        pos_plain.extend(sims.max(axis=1))

        full_ref = idx
        for c in {c for c in flat_codes if c in trusted}:
            full_ref = np.concatenate([full_ref, pools[c]])
        full_ref = np.unique(full_ref)
        full_fp = fingerprint(idx, flat_codes, mat, lmat, lpos, trusted)

        # configuration F structures: HARD = trusted codes on the wins (facts);
        # SOFT = labels near the reference texts, requiring `consensus` refs
        # above `floor` (guesses that earned membership). Per-holdout variants
        # drop the held-out win's contribution from both.
        R = lmat @ mat[idx].T
        floors_ge = {f: R >= f for f in SOFT_FLOORS}
        row_max = R.max(axis=1)
        row_arg = R.argmax(axis=1)
        row_2nd = (np.partition(R, -2, axis=1)[:, -2] if R.shape[1] >= 2
                   else np.full(len(R), -np.inf))
        hard_full = sorted({lpos[c] for c in flat_codes
                            if c in trusted and c in lpos})

        def soft_rows(counts, maxes, f, k):
            ok = np.flatnonzero(counts >= k)
            return ok[np.argsort(-maxes[ok])][:FP_K]

        soft_full = {(f, k): soft_rows(floors_ge[f].sum(axis=1), row_max, f, k)
                     for f, k in fk}

        for i in range(len(idx)):
            others = np.delete(idx, i)
            other_codes = [c for j, cs in enumerate(win_codes) if j != i
                           for c in cs]
            other_trusted = {c for c in other_codes if c in trusted}
            auto = any(c in other_trusted for c in win_codes[i])
            pos_auto.append(auto)
            if auto:
                pos_text.append(np.inf)
            else:
                ref = others
                for c in other_trusted:
                    ref = np.concatenate([ref, pools[c][pools[c] != idx[i]]])
                pos_text.append(float((mat[ref] @ mat[idx[i]]).max()))
            fp_i = fingerprint(others, other_codes, mat, lmat, lpos, trusted)
            pos_code.append(best_code_score(win_codes[i], fp_i, lmat, lpos))

            cand = label_rows(win_codes[i])
            hard_i = sorted({lpos[c] for c in other_codes
                             if c in trusted and c in lpos})
            pos_hard.append(best_ll(cand, np.array(hard_i, dtype=int)))
            max_wo = np.where(row_arg == i, row_2nd, row_max)
            for f, k in fk:
                counts_wo = floors_ge[f].sum(axis=1) - floors_ge[f][:, i]
                pos_soft[(f, k)].append(
                    best_ll(cand, soft_rows(counts_wo, max_wo, f, k)))

        firm_classes = {c[:4] for c in flat_codes}
        off_class = ~np.isin(all_class, list(firm_classes))
        naive_pool = np.flatnonzero(deep_mask & off_class)
        naive_pool = naive_pool[~np.isin(naive_pool, idx)]
        if len(naive_pool):
            pick = rng.choice(naive_pool, min(NEG_PER_FIRM, len(naive_pool)),
                              replace=False)
            neg_naive.append((mat[pick] @ mat[idx].T).max(axis=1))
        # ONE trusted pick, scored three ways (plain refs / expanded refs /
        # code channel) so every configuration compares on identical negatives
        trust_pool = np.flatnonzero(
            np.isin(all_cpv.astype(str), list(trusted)) & off_class)
        trust_pool = trust_pool[~np.isin(trust_pool, idx)]
        if len(trust_pool):
            pick = rng.choice(trust_pool, min(NEG_PER_FIRM, len(trust_pool)),
                              replace=False)
            neg_trust.append((mat[pick] @ mat[idx].T).max(axis=1))
            neg_hyb_text.append((mat[pick] @ mat[full_ref].T).max(axis=1))
            neg_code_s.extend(best_code_score(all_codes[p], full_fp, lmat, lpos)
                              for p in pick)
            hf = np.array(hard_full, dtype=int)
            for p in pick:
                cand = label_rows(all_codes[p])
                neg_hard.append(best_ll(cand, hf))
                for key in fk:
                    neg_soft[key].append(best_ll(cand, soft_full[key]))

        vol = rng.choice(len(mat), VOL_PER_FIRM, replace=False)
        vol_plain.append((mat[vol] @ mat[idx].T).max(axis=1))
        vol_text.append((mat[vol] @ mat[full_ref].T).max(axis=1))
        vol_code.extend(best_code_score(all_codes[p], full_fp, lmat, lpos)
                        for p in vol)
        hf = np.array(hard_full, dtype=int)
        for p in vol:
            cand = label_rows(all_codes[p])
            vol_hard.append(best_ll(cand, hf))
            for key in fk:
                vol_soft[key].append(best_ll(cand, soft_full[key]))

    pos_plain = np.array(pos_plain)
    pos_auto = np.array(pos_auto)
    pos_text = np.array(pos_text)
    pos_code = np.array(pos_code)
    neg = {'naive': np.concatenate(neg_naive), 'trusted': np.concatenate(neg_trust)}
    neg_hyb_text = np.concatenate(neg_hyb_text)
    neg_code_s = np.array(neg_code_s)
    vol_plain = np.concatenate(vol_plain)
    vol_text = np.concatenate(vol_text)
    vol_code = np.array(vol_code)
    n_pos = len(pos_text)
    n_auto = int(pos_auto.sum())

    thr_plain = float(np.quantile(pos_plain, 1 - RECALL_TARGET))
    thr_c_text = threshold_for_recall(pos_text[~pos_auto], n_auto, RECALL_TARGET)

    # configurations D and E: joint grid over the code-channel threshold; for
    # each, the text threshold is set so the two channels together keep the
    # recall promise, and the pair with the least wrong-trade leakage wins.
    # D uses the expanded profile as text channel, E the plain references.
    def joint_best(text_pos, text_neg, text_vol):
        best = None
        for tc in CODE_GRID:
            code_pass = pos_code >= tc
            rem = text_pos[~code_pass]
            needed = RECALL_TARGET * n_pos - code_pass.sum()
            tt = (float(np.quantile(rem[np.isfinite(rem)],
                                    max(0.0, 1 - needed / max((np.isfinite(rem)).sum(), 1))))
                  if needed > 0 else np.inf)
            recall = float((code_pass | (text_pos >= tt)).mean())
            leak = float(((text_neg >= tt) | (neg_code_s >= tc)).mean())
            vol = float(((text_vol >= tt) | (vol_code >= tc)).mean())
            if best is None or leak < best['leakage']:
                best = {'threshold': tt, 'code_threshold': float(tc),
                        'recall': recall, 'leakage': leak, 'volume': vol,
                        'code_share': float(code_pass.mean())}
        return best

    best_d = joint_best(pos_text, neg_hyb_text, vol_text)
    best_e = joint_best(pos_plain, neg['trusted'], vol_plain)

    # configuration F: like E, but the code channel splits by origin — hard
    # codes (facts from wins) and soft labels (earned guesses) get separate
    # thresholds; floor/consensus/thresholds jointly searched, recall promise
    # enforced by percentile at every grid point, least leakage wins.
    pos_hard_a = np.array(pos_hard)
    neg_hard_a = np.array(neg_hard)
    vol_hard_a = np.array(vol_hard)
    best_f, f_pareto = None, {}
    for key in fk:
        ps = np.array(pos_soft[key])
        ns = np.array(neg_soft[key])
        vs = np.array(vol_soft[key])
        for ht in CODE_GRID:
            hp, hn, hv = pos_hard_a >= ht, neg_hard_a >= ht, vol_hard_a >= ht
            for st in SOFT_GRID:
                code_pass = hp | (ps >= st)
                rem = pos_plain[~code_pass]
                needed = RECALL_TARGET * n_pos - code_pass.sum()
                tt = (float(np.quantile(rem, max(0.0, 1 - needed / max(len(rem), 1))))
                      if needed > 0 else np.inf)
                recall = float((code_pass | (pos_plain >= tt)).mean())
                leak = float(((neg['trusted'] >= tt) | hn | (ns >= st)).mean())
                volu = float(((vol_plain >= tt) | hv | (vs >= st)).mean())
                point = {'threshold': tt, 'code_threshold': float(ht),
                         'soft_threshold': float(st),
                         'soft_floor': key[0], 'soft_consensus': key[1],
                         'recall': recall, 'leakage': leak, 'volume': volu,
                         'code_share': float(code_pass.mean())}
                if key not in f_pareto or leak < f_pareto[key]['leakage']:
                    f_pareto[key] = point
                if best_f is None or leak < best_f['leakage']:
                    best_f = point

    return {
        'n_firms': len(by_firm), 'n_positives': n_pos,
        'baseline': baseline, 'trust_cut': cut, 'n_deep_codes': len(cohesion),
        'n_trusted': len(trusted), 'cohesion': cohesion,
        'pos_text': pos_text, 'pos_auto': pos_auto, 'pos_code': pos_code,
        'neg_hyb_text': neg_hyb_text, 'neg_code': neg_code_s,
        'configs': {
            'A text-only, naive negatives': {
                'threshold': thr_plain,
                'recall': float((pos_plain >= thr_plain).mean()),
                'leakage': float((neg['naive'] >= thr_plain).mean())},
            'B text-only, trusted negatives': {
                'threshold': thr_plain,
                'recall': float((pos_plain >= thr_plain).mean()),
                'leakage': float((neg['trusted'] >= thr_plain).mean())},
            'C hybrid (expansion + auto-pass)': {
                'threshold': thr_c_text,
                'recall': float(
                    (pos_auto | (pos_text >= thr_c_text)).mean()),
                'leakage': float((neg_hyb_text >= thr_c_text).mean()),
                'volume': float((vol_text >= thr_c_text).mean())},
            'D hybrid + code channel': best_d,
            'E plain refs + code channel': best_e,
            'F hard/soft codes + floor/consensus': best_f,
        },
        'f_pareto': f_pareto,
    }


def trade_fingerprint_demo(data_dir, firm_name=None):
    """Console demo: a real firm's trade fingerprint, named in official CPV
    vocabulary. Not written to any committed artifact."""
    rows, mat = load_sidecar(data_dir)
    lpos, lrows, lmat = load_label_sidecar(data_dir)
    pos_of = {(r['procedure_id'], r['lot_id']): i for i, r in enumerate(rows)}
    tenders = pd.read_parquet(Path(data_dir) / 'store' / 'tenders.parquet')
    awards = pd.read_parquet(Path(data_dir) / 'store' / 'awards.parquet')
    wins = firm_win_rows(awards, tenders)
    wins['row'] = [pos_of.get((p, l)) for p, l in zip(wins['procedure_id'], wins['lot_id'])]
    wins = wins.dropna(subset=['row']).astype({'row': int})
    if firm_name is None:
        firm_name = wins['winner_names'].value_counts().index[0]
    g = wins[wins['winner_names'] == firm_name]
    if g.empty:
        print(f'[fingerprint] no embedded wins for {firm_name!r}')
        return
    sims = (lmat @ mat[g['row'].to_numpy()].T).max(axis=1)
    print(f'[fingerprint] {firm_name} ({len(g)} embedded wins):')
    for i in np.argsort(-sims)[:FP_K]:
        print(f"    {sims[i]:.3f}  {lrows[i]['code']}  {lrows[i]['label_de']}")


def hist_lines(scores, threshold, lo=0.2, hi=1.0, bins=16, width=40):
    scores = scores[np.isfinite(scores)]
    counts, edges = np.histogram(scores, bins=bins, range=(lo, hi))
    peak = counts.max() or 1
    out = []
    for c, a, b in zip(counts, edges, edges[1:]):
        mark = ' <-- threshold' if a <= threshold < b else ''
        out.append(f'    {a:.2f}-{b:.2f}  {"#" * round(width * c / peak):<{width}} '
                   f'{c}{mark}')
    return out


def write_receipt(path, r):
    two_channel = {k: v for k, v in r['configs'].items() if 'code_threshold' in v}
    best_name, d = min(two_channel.items(), key=lambda kv: kv[1]['leakage'])
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
        f'- **Defaults: `min_relevance` = {d["threshold"]:.3f}, '
        f'`min_code_relevance` = {d["code_threshold"]:.3f}** '
        f'(best two-channel configuration: {best_name}; '
        f'recall promise {RECALL_TARGET:.0%})',
        '',
        '| configuration | text thr | code thr | recall | wrong-trade leakage |',
        '| --- | --- | --- | --- | --- |',
    ]
    for name, v in r['configs'].items():
        lines.append(
            f'| {name} | {v["threshold"]:.3f} '
            f'| {v.get("code_threshold", float("nan")):.3f} '
            f'| {v["recall"]:.1%} | {v["leakage"]:.1%} |')
    lines += [
        '',
        f'- Code channel alone passes {d["code_share"]:.1%} of positives.',
        f'- Admitted market volume at configuration D: {d["volume"]:.1%} of '
        f'random lots (a sizing number, not an error rate).',
        '',
        'Text-channel positive scores (held-out win vs expanded profile, '
        'auto-passes excluded):',
        '',
        *hist_lines(r['pos_text'], d['threshold']),
        '',
        'Trusted-negative text scores vs expanded profiles:',
        '',
        *hist_lines(r['neg_hyb_text'], d['threshold']),
        '',
        'Code-channel scores — positives vs negatives share one axis:',
        '',
        *hist_lines(r['pos_code'], d['code_threshold']),
        '',
        *hist_lines(r['neg_code'], d['code_threshold']),
        '',
    ]
    path.write_text('\n'.join(lines), encoding='utf-8')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', default='data')
    ap.add_argument('--fingerprint', metavar='FIRM', nargs='?', const='',
                    help='print a trade-fingerprint demo (optionally for FIRM) and exit')
    args = ap.parse_args()
    if args.fingerprint is not None:
        trade_fingerprint_demo(args.data_dir, args.fingerprint or None)
        return
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
        extra = ''.join([f', volume {v["volume"]:.1%}' if 'volume' in v else '',
                         f', code thr {v["code_threshold"]:.3f}'
                         if 'code_threshold' in v else '',
                         f', soft thr {v["soft_threshold"]:.3f} '
                         f'(floor {v["soft_floor"]:.2f}, consensus '
                         f'{v["soft_consensus"]})'
                         if 'soft_threshold' in v else ''])
        print(f'[calibrate] {name}: thr={v["threshold"]:.3f} '
              f'recall={v["recall"]:.1%} leakage={v["leakage"]:.1%}{extra}')
    print('[calibrate] F pareto (best per membership rule):')
    for (f, k), v in sorted(r['f_pareto'].items()):
        print(f'  floor {f:.2f} consensus {k}: leakage {v["leakage"]:.1%} '
              f'(text {v["threshold"]:.3f}, hard {v["code_threshold"]:.3f}, '
              f'soft {v["soft_threshold"]:.3f})')
    print(f'[calibrate] receipts -> {receipt}, {codes}')


if __name__ == '__main__':
    main()
