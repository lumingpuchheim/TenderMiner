"""TenderMining forward backtest — replay history as the loop would have run it.

For every weekly cutoff D, an as-of world is materialised (only notices
published before D), the champion is retrained inside it, open lots are
scored, gated subscriptions get their as-of profiles, and the week's picks
are recorded. Afterwards every pick is graded against the outcomes the full
store later learned. The result is the live track record, bootstrapped
backwards: "across the replayed period, picks ended with 0-1 bids X in 100,
base rate Y" — plus, per gated subscription, whether the customer's own
eventual wins appeared in their replayed market.

Time isolation is by construction: one working directory whose store
parquets are rewritten per cutoff (pyarrow filter — preserves the role
metadata), and every component (calibration, trust list, profile, model)
reads only from it. Disclosed residuals: embedding vectors are per-notice
lookups from the full sidecar; the system's design postdates the replayed
period; trust/thresholds are recalibrated every RECAL_EVERY cutoffs, not
weekly (cost).

Usage:
    python backtest.py                        # weekly, whole feasible range
    python backtest.py --step 14 --sub jebsen-blitzschutz
"""

import argparse
import json
import shutil
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

import relevance as rel
import single_bidder as sb
from calibrate import calibrate as run_calibration
from embed import MODEL_TAG

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

MIN_TRAIN_LOTS = 300   # first cutoff needs this many labeled lots
RECAL_EVERY = 8        # cutoffs between trust/threshold recalibrations
FLAG_THRESHOLD = 0.5   # mirrors the loop's --threshold default
MIN_DEADLINE_DAYS = 14
MAX_PICKS = 5


def write_world(full_store, work_store, D):
    """Rewrite the working store to contain only pre-D publications."""
    for name in ('tenders', 'awards'):
        tab = pq.read_table(full_store / f'{name}.parquet')
        mask = pc.less(tab.column('publication_date'), D.date())
        pq.write_table(tab.filter(mask), work_store / f'{name}.parquet')


def as_of_profile(gate, sub, awards_asof, cal_f):
    """The subscription's profile as it would have stood: wins awarded (i.e.
    award notice published) before the cutoff, thresholds from the as-of
    calibration. None when the customer had no resolvable win yet."""
    firm = sub.get('name')
    aw = awards_asof[awards_asof['winner_names'].apply(
        lambda x: x is not None and firm in list(x))]
    refs = sorted({gate.rows[gate.by_key[(p, l)]]['publication_number']
                   for p, l in zip(aw['procedure_id'], aw['lot_id'])
                   if (p, l) in gate.by_key})
    if not refs:
        return None
    spec = dict(sub, profile_refs=refs,
                min_relevance=cal_f['threshold'],
                min_code_hard=cal_f['code_threshold'],
                min_code_soft=cal_f['soft_threshold'])
    return rel.build_profile(gate, spec)


def replay(data_dir, step_days, sub_ids):
    full_store = Path(data_dir) / 'store'
    work = Path(data_dir) / 'backtest_world'
    (work / 'store').mkdir(parents=True, exist_ok=True)
    if not (work / 'embeddings').exists():
        shutil.copytree(Path(data_dir) / 'embeddings', work / 'embeddings')

    tenders_full = pd.read_parquet(full_store / 'tenders.parquet')
    awards_full = pd.read_parquet(full_store / 'awards.parquet')
    aw_latest, _ = sb.latest_awards(awards_full)
    outcome = {(a['procedure_id'], a['lot_id']): int(a['n_tenders'])
               for _, a in aw_latest.iterrows()}

    subs = {}
    for line in (Path(data_dir) / 'subscriptions.jsonl').read_text(
            encoding='utf-8').splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get('sub_id') in sub_ids and row.get('active', True):
                subs[row['sub_id']] = row  # last active version wins

    pub_a = pd.to_datetime(awards_full['publication_date'])
    first = pub_a.min() + pd.Timedelta(days=1)
    last = pd.to_datetime(tenders_full['publication_date']).max()
    cutoffs = pd.date_range(first, last, freq=f'{step_days}D')

    flagged = {}          # lot -> first week's score (global record, dedup)
    scored_lonely = {}    # lot -> ever scored while open (base-rate pool)
    sub_picks = {s: {} for s in subs}
    sub_market = {s: set() for s in subs}
    cal_f, n_run = None, 0
    for D in cutoffs:
        write_world(full_store, work / 'store', D)
        tenders_r, roles = sb.load_with_roles(work / 'store' / 'tenders.parquet')
        awards_r, _ = sb.load_with_roles(work / 'store' / 'awards.parquet')
        data, aw, _ = sb.assemble(tenders_r, awards_r)
        n_lots = data.groupby(sb.KEY).ngroups
        if n_lots < MIN_TRAIN_LOTS:
            continue
        if cal_f is None or n_run % RECAL_EVERY == 0:
            r = run_calibration(str(work))
            cal_f = r['configs']['F hard/soft codes + floor/consensus']
            trust = work / 'trusted_codes_asof.json'
            trust.write_text(json.dumps(
                {'baseline': r['baseline'], 'cut': r['trust_cut'],
                 'codes': {k: {'n': v['n'], 'cohesion': v['cohesion'],
                               'trusted': v['cohesion'] >= r['trust_cut']}
                           for k, v in r['cohesion'].items()}}), encoding='utf-8')
            rel.TRUSTED_CODES = trust
            rel.SOFT_FLOOR = cal_f['soft_floor']
            rel.SOFT_CONSENSUS = cal_f['soft_consensus']
        n_run += 1

        X, cat_cols, _, _ = sb.build_features(data, roles, list_frame=tenders_r)
        k = data.groupby(sb.KEY)['label'].transform('size')
        model = sb.train(X, data['label'].to_numpy(), (1.0 / k).to_numpy(), cat_cols)

        open_t = sb.open_tenders(tenders_r, aw)
        deadline = pd.to_datetime(open_t.get('deadline_date'), errors='coerce')
        open_t = open_t[deadline >= D]
        if open_t.empty:
            continue
        Xo, _, _, _ = sb.build_features(open_t, roles, list_frame=tenders_r)
        scores = sb.predict(model, Xo)

        gate = rel.Gate(str(work))
        profiles = {s: as_of_profile(gate, subs[s], awards_r, cal_f)
                    for s in subs}
        dl_ok = (deadline.loc[open_t.index]
                 >= D + pd.Timedelta(days=MIN_DEADLINE_DAYS))
        rows = []
        for (i, row), s in zip(open_t.iterrows(), scores):
            lot = (row['procedure_id'], row['lot_id'])
            scored_lonely.setdefault(lot, True)
            if s >= FLAG_THRESHOLD:
                flagged.setdefault(lot, (str(D.date()), float(s)))
            rows.append((lot, row, float(s), bool(dl_ok.loc[i])))
        for s, profile in profiles.items():
            if profile is None:
                continue
            cand = []
            for lot, row, sc, ok_dl in rows:
                cpv = str(row.get('cpv_main') or '')
                nuts = str(row.get('place_nuts3') or '')
                if not any(cpv.startswith(p) for p in subs[s].get('cpv_prefixes') or ['']):
                    continue
                if subs[s].get('nuts_prefixes') and not any(
                        nuts.startswith(p) for p in subs[s]['nuts_prefixes']):
                    continue
                d = {'procedure_id': lot[0], 'lot_id': lot[1],
                     'buyer_name': row.get('buyer_name'),
                     'title': row.get('title'), 'score': sc}
                ok, near, tx, cd, why, ch = rel.judge(gate, profile, d)
                if ok:
                    sub_market[s].add(lot)
                    if ok_dl:
                        d.update(text=tx, hard=ch,
                                 confident=rel.is_confident(profile, tx, ch))
                        cand.append(d)
            cand.sort(key=lambda d: -d['score'])
            for d in [c for c in cand
                      if c['score'] >= FLAG_THRESHOLD and c['confident']][:MAX_PICKS]:
                sub_picks[s].setdefault(
                    (d['procedure_id'], d['lot_id']),
                    {'week': str(D.date()), **{k2: d[k2] for k2 in
                                               ('title', 'buyer_name', 'score')}})
        print(f'[backtest] {D.date()}: {n_lots} train lots, '
              f'{len(open_t)} open, {sum(1 for *_, s2, _ in rows if s2 >= FLAG_THRESHOLD)} flagged',
              flush=True)

    return {'flagged': flagged, 'scored': scored_lonely, 'sub_picks': sub_picks,
            'sub_market': sub_market, 'outcome': outcome, 'subs': subs,
            'awards_full': awards_full, 'gate_dir': str(work)}


def report(res, out_path):
    outcome = res['outcome']
    graded_pool = {lot: outcome[lot] for lot in res['scored'] if lot in outcome}
    base = np.mean([n <= 1 for n in graded_pool.values()]) if graded_pool else 0
    graded_flags = {lot: outcome[lot] for lot in res['flagged'] if lot in outcome}
    hit = (np.mean([n <= 1 for n in graded_flags.values()])
           if graded_flags else float('nan'))
    lines = [f'# Forward backtest — {MODEL_TAG} — generated {date.today().isoformat()}',
             '',
             f'- Replayed lots ever scored while open: {len(res["scored"])} '
             f'({len(graded_pool)} with published outcomes; base rate '
             f'{base:.0%} ended with 0-1 bids)',
             f'- **Global flagged picks: {len(res["flagged"])} lots; of the '
             f'{len(graded_flags)} graded, {hit:.0%} ended with 0-1 bids '
             f'(lift {hit / base:.2f}x)**' if graded_flags else
             '- no graded global flags',
             '']
    for s, picks in res['sub_picks'].items():
        firm = res['subs'][s].get('name', s)
        lines += [f'## {firm}', '']
        graded = {lot: outcome.get(lot) for lot in picks}
        n_lonely = sum(1 for n in graded.values() if n is not None and n <= 1)
        n_graded = sum(1 for n in graded.values() if n is not None)
        lines += [f'- Picks across the replay: {len(picks)} '
                  f'({n_graded} graded, {n_lonely} ended with 0-1 bids)', '']
        for lot, p in sorted(picks.items(), key=lambda kv: kv[1]['week']):
            n = outcome.get(lot)
            res_s = f'{n} bid(s)' if n is not None else 'outcome pending'
            lines.append(f'  - {p["week"]}: {str(p["title"])[:60]!r} '
                         f'[{str(p["buyer_name"])[:35]}] -> {res_s}')
        # recall of the customer's own eventual wins
        aw = res['awards_full']
        wins = aw[aw['winner_names'].apply(
            lambda x: x is not None and firm in list(x))]
        rows = []
        for _, w in wins.iterrows():
            lot = (w['procedure_id'], w['lot_id'])
            in_market = lot in res['sub_market'][s]
            in_picks = lot in picks
            rows.append((str(w['publication_date'])[:10], lot, in_market, in_picks,
                         w['n_tenders']))
        lines += ['', f'- Own wins visible in the replayed market: '
                  f'{sum(1 for r in rows if r[2])}/{len(rows)} '
                  f'(as picks: {sum(1 for r in rows if r[3])})']
        for d, lot, im, ip, n in sorted(rows):
            nb = int(n) if pd.notna(n) else '?'
            lines.append(f'  - win awarded {d} ({nb} bids): in market={im}, pick={ip}')
        lines.append('')
    Path(out_path).write_text('\n'.join(lines), encoding='utf-8')
    print('\n'.join(lines))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', default='data')
    ap.add_argument('--step', type=int, default=7, help='days between cutoffs')
    ap.add_argument('--sub', action='append', default=None,
                    help='subscription id(s) to replay (default: all gated)')
    args = ap.parse_args()
    sub_ids = args.sub
    if sub_ids is None:
        sub_ids = []
        for line in (Path(args.data_dir) / 'subscriptions.jsonl').read_text(
                encoding='utf-8').splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get('profile_refs'):
                    sub_ids.append(row['sub_id'])
    res = replay(args.data_dir, args.step, set(sub_ids))
    out = Path(args.data_dir) / 'reports' / f'backtest_{date.today().isoformat()}.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    report(res, out)
    print(f'[backtest] report -> {out}')


if __name__ == '__main__':
    main()
