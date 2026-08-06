"""Replay: two customer reports across time — the prediction and its check.

Renders what a subscription's REAL weekly report would have said at a past
cutoff D (picks made by a model that provably could not see past D), then a
second report at --check-date (default today) where "Ihre Empfehlungen im
Rückblick" grades those picks against the outcomes the market has published
since. Two HTML files, side by side: what we said, and whether it was right.

Time isolation as in playback.py/backtest.py: an as-of world under
data/replay_asof/ holds only pre-D publications; trust list, thresholds
(configuration H), profile and CatBoost model are rebuilt inside it. The
full store is consulted only to grade. Everything is written to
data/replay/<sub_id>/ — real ledgers and reports are never touched.

Choosing D: awards follow deadlines by ~3 months, so a cutoff 3-6 months
back usually has graded outcomes; the backtest report
(data/reports/backtest_<date>.md) lists per-subscription pick weeks with
outcomes — any week listed there works.

The subject should be REAL: --firm replays any actual winner company from
the awards store (profile from their as-of wins, region from where they
won) — a track record for an invented demo customer proves nothing. Use
--scan first to see which real firms have graded picks at a cutoff.

Usage:
    python replay.py --scan --cutoff 2026-04-15
    python replay.py --firm "Firma GmbH" --cutoff 2026-04-15
    python replay.py --sub jebsen-blitzschutz --cutoff 2026-03-25 --check-date 2026-08-01
"""

import argparse
import json
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

import calibrate as cal
import loop
import relevance as rel
import single_bidder as sb

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

FLAG_THRESHOLD = 0.5  # mirrors the loop's --threshold default


def freeze_clock(day):
    """deliver() reads the clock; a replay must render 'today' = the cutoff."""
    loop.now_utc = lambda: datetime(day.year, day.month, day.day, 8, 0,
                                    tzinfo=timezone.utc)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--sub', help='subscription id to replay')
    ap.add_argument('--firm', help='replay a REAL winner company from the '
                    'awards store instead of a subscription: profile from '
                    'their as-of wins, region from where they won')
    ap.add_argument('--scan', action='store_true',
                    help='no render: check EVERY real winner company at this '
                    'cutoff and list the ones whose picks have graded outcomes')
    ap.add_argument('--cutoff', required=True, metavar='YYYY-MM-DD',
                    help='the past cycle date D — picks are made as of here')
    ap.add_argument('--check-date', default=None, metavar='YYYY-MM-DD',
                    help='date of the check report (default: today)')
    ap.add_argument('--data-dir', default='data')
    args = ap.parse_args()
    if not args.scan and not (bool(args.sub) ^ bool(args.firm)):
        sys.exit('[replay] give exactly one of --sub / --firm (or --scan)')
    t0 = time.time()

    D = pd.Timestamp(args.cutoff)
    D2 = pd.Timestamp(args.check_date) if args.check_date else pd.Timestamp.today().normalize()
    if D2 <= D:
        sys.exit('[replay] --check-date must lie after --cutoff')
    FULL = Path(args.data_dir)

    sub = None
    if args.sub:
        subs = loop.load_subscriptions(FULL / 'subscriptions.jsonl', str(D.date()))
        sub = next((s for s in subs if s['sub_id'] == args.sub), None)
        if sub is None:  # subscription may postdate the cutoff — take today's line
            subs = loop.load_subscriptions(FULL / 'subscriptions.jsonl',
                                           str(pd.Timestamp.today().date()))
            sub = next((s for s in subs if s['sub_id'] == args.sub), None)
        if sub is None:
            sys.exit(f'[replay] no active subscription {args.sub!r}')

    # ---- the as-of world -----------------------------------------------------
    ASOF = FULL / 'replay_asof'
    if ASOF.exists():
        shutil.rmtree(ASOF)
    (ASOF / 'store').mkdir(parents=True)
    for name in ('tenders', 'awards'):
        tab = pq.read_table(FULL / 'store' / f'{name}.parquet')
        pq.write_table(tab.filter(pc.less(tab.column('publication_date'), D.date())),
                       ASOF / 'store' / f'{name}.parquet')
    shutil.copytree(FULL / 'embeddings', ASOF / 'embeddings')
    print(f'[replay] as-of world at {D.date()} built in {time.time() - t0:.0f}s')

    # ---- as-of calibration (configuration H) + gate constants ---------------
    r = cal.calibrate(str(ASOF))
    H = r['configs']['H single bar + trade-read corroboration']
    trust_json = ASOF / 'trusted_codes_asof.json'
    trust_json.write_text(json.dumps(
        {'baseline': r['baseline'], 'cut': r['trust_cut'],
         'codes': {k: {'n': v['n'], 'cohesion': v['cohesion'],
                       'trusted': v['cohesion'] >= r['trust_cut']}
                   for k, v in r['cohesion'].items()}}), encoding='utf-8')
    rel.TRUSTED_CODES = trust_json
    rel.SOFT_FLOOR = H['soft_floor']
    rel.SOFT_CONSENSUS = H['soft_consensus']
    rel.TRADE_READ_FORM = H['corr_form'] if H['corr_form'] != 'off' else 'off'
    rel.TRADE_READ_PARAM = H['corr_param']
    print(f"[replay] as-of gate H: text {H['threshold']:.3f}, hard "
          f"{H['code_threshold']:.3f}, soft {H['soft_threshold']:.3f}, "
          f"corr {H['corr_form']}@{H['corr_param']:.3f}")

    # ---- as-of profile from the wins the firm had at D ----------------------
    gate = rel.Gate(str(ASOF))
    awards_asof = pd.read_parquet(ASOF / 'store' / 'awards.parquet')
    tenders_asof = pd.read_parquet(ASOF / 'store' / 'tenders.parquet')
    lots_nuts = {(p, l): n for p, l, n in zip(
        tenders_asof['procedure_id'], tenders_asof['lot_id'],
        tenders_asof['place_nuts3'])}

    def firm_asof(firm_name):
        """(refs, NUTS1 prefixes) of a real firm's resolvable as-of wins."""
        aw_f = awards_asof[awards_asof['winner_names'].apply(
            lambda x: x is not None and firm_name in list(x))]
        keys = [(p, l) for p, l in zip(aw_f['procedure_id'], aw_f['lot_id'])
                if (p, l) in gate.by_key]
        f_refs = sorted({gate.rows[gate.by_key[k]]['publication_number']
                         for k in keys})
        nuts = sorted({str(lots_nuts.get(k))[:3] for k in keys
                       if isinstance(lots_nuts.get(k), str) and lots_nuts.get(k)})
        return f_refs, nuts

    if args.firm:
        f_refs, nuts1 = firm_asof(args.firm)
        if len(f_refs) < 2:
            sys.exit(f'[replay] {args.firm!r} has fewer than 2 resolvable wins '
                     f'before {D.date()} — try --scan to find replayable firms')
        slug = re.sub(r'[^a-z0-9]+', '-', args.firm.casefold()).strip('-')
        sub = {'sub_id': f'firm-{slug}', 'name': args.firm,
               'cpv_prefixes': ['45'], 'nuts_prefixes': nuts1,
               'min_deadline_days': 14, 'max_picks': 5,
               'profile_refs': f_refs}
        print(f'[replay] real firm {args.firm!r}: {len(f_refs)} as-of wins, '
              f'region {"/".join(nuts1)}')

    firm = sub.get('name') if sub else None
    replay_sub = None
    if sub is not None:
        refs, _ = firm_asof(firm) if firm else ([], [])
        if not refs and sub.get('profile_refs'):
            sys.exit(f'[replay] {firm!r} had no resolvable win before '
                     f'{D.date()} — pick a later cutoff')
        replay_sub = dict(sub, profile_refs=refs or None,
                          min_relevance=H['threshold'] if refs else None,
                          min_code_hard=H['code_threshold'],
                          min_code_soft=H['soft_threshold'],
                          version=sub.get('version', 1),
                          effective_from=str(D.date()))
        print(f'[replay] profile as of {D.date()}: {len(refs)} won tenders')

    # ---- as-of model + scores for the open market at D ----------------------
    tenders_r, roles = sb.load_with_roles(ASOF / 'store' / 'tenders.parquet')
    awards_r, _ = sb.load_with_roles(ASOF / 'store' / 'awards.parquet')
    data, aw, _ = sb.assemble(tenders_r, awards_r)
    X, cat_cols, _, _ = sb.build_features(data, roles, list_frame=tenders_r)
    k = data.groupby(sb.KEY)['label'].transform('size')
    model = sb.train(X, data['label'].to_numpy(), (1.0 / k).to_numpy(), cat_cols)
    print(f'[replay] model trained on {data.groupby(sb.KEY).ngroups} pre-{D.date()} '
          f'lots in {time.time() - t0:.0f}s')

    open_t = sb.open_tenders(tenders_r, aw)
    deadline = pd.to_datetime(open_t.get('deadline_date'), errors='coerce')
    open_t = open_t[deadline.isna() | (deadline >= D)]
    Xo, cats_o, _, _ = sb.build_features(open_t, roles, list_frame=tenders_r)
    scores = sb.predict(model, Xo)
    why_lonely, why_crowded = loop.explain_rows(model, Xo, cats_o)
    scored = []
    for (i, t), s, w_l, w_c in zip(open_t.iterrows(), scores, why_lonely,
                                   why_crowded):
        cpv = t.get('cpv_main')
        scored.append({
            'ts': str(D.date()), 'model': f'replay-{D.date()}',
            'procedure_id': t['procedure_id'], 'lot_id': t['lot_id'],
            'notice_id': t.get('notice_id'),
            'publication_date': str(t.get('publication_date')),
            'deadline_date': str(t.get('deadline_date')),
            'score': float(s), 'threshold': FLAG_THRESHOLD,
            'flag': bool(s >= FLAG_THRESHOLD),
            'cpv3': str(cpv)[:3] if pd.notna(cpv) else None,
            'cpv_main': cpv if pd.notna(cpv) else None,
            'place_nuts3': t.get('place_nuts3'),
            'publication_number': t.get('publication_number'),
            'buyer_name': t.get('buyer_name'), 'title': t.get('title'),
            'why_lonely': w_l, 'why_crowded': w_c,
        })
    print(f'[replay] {len(scored)} open lots scored at {D.date()}')

    # ---- scan: which REAL firms have graded picks at this cutoff? -----------
    if args.scan:
        awards_full = pd.read_parquet(FULL / 'store' / 'awards.parquet')
        aw_latest, _ = sb.latest_awards(awards_full)
        pub = pd.to_datetime(aw_latest['publication_date'], errors='coerce')
        graded_aw = aw_latest[(pub > D) & (pub <= D2)]
        outcome = {(a['procedure_id'], a['lot_id']): int(a['n_tenders'])
                   for _, a in graded_aw.iterrows() if pd.notna(a['n_tenders'])}
        dl = pd.to_datetime([r['deadline_date'] for r in scored], errors='coerce')
        min_dl = D + pd.Timedelta(days=14)
        pool = [r for r, d in zip(scored, dl) if pd.notna(d) and d >= min_dl]
        pool.sort(key=lambda r: -r['score'])
        w = awards_asof[awards_asof['winner_names'].apply(
            lambda x: x is not None and len(x) > 0)].explode('winner_names')
        firms = [f for f, c in w['winner_names'].value_counts().items() if c >= 2]
        print(f'[replay] scanning {len(firms)} real firms with >= 2 as-of wins')
        results = []
        for firm_name in firms:
            f_refs, nuts1 = firm_asof(firm_name)
            if len(f_refs) < 2 or not nuts1:
                continue
            try:
                profile = rel.build_profile(gate, {
                    'profile_refs': f_refs, 'min_relevance': H['threshold'],
                    'min_code_hard': H['code_threshold'],
                    'min_code_soft': H['soft_threshold'], 'version': 0})
            except ValueError:
                continue
            picks = []
            for r0 in pool:
                if r0['score'] < FLAG_THRESHOLD:
                    break
                if not str(r0.get('place_nuts3') or '').startswith(tuple(nuts1)):
                    continue
                ok, *_ = rel.judge(gate, profile, r0)
                if ok:
                    picks.append(r0)
                if len(picks) == 5:
                    break
            graded = [(r0, outcome[(r0['procedure_id'], r0['lot_id'])])
                      for r0 in picks
                      if (r0['procedure_id'], r0['lot_id']) in outcome]
            if graded:
                hits = sum(1 for _, n in graded if n <= 1)
                results.append((hits, len(graded), len(picks), firm_name, graded))
        results.sort(key=lambda t: (-t[0], -t[1]))
        if not results:
            print('[replay] no real firm has a graded pick at this cutoff — '
                  'try an earlier one')
        for hits, n_graded, n_picks, firm_name, graded in results[:15]:
            print(f'\n  {firm_name}  ({n_picks} picks, {n_graded} graded, '
                  f'{hits} ended 0-1 bids)')
            for r0, n in graded:
                mark = 'HIT ' if n <= 1 else 'miss'
                print(f'    [{mark}] {str(r0["title"])[:55]!r} -> {n} bid(s)')
        return

    # ---- sandbox for both renders -------------------------------------------
    sub_id = sub['sub_id']
    OUT = FULL / 'replay' / sub_id
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / 'ledger').mkdir(parents=True)
    (OUT / 'subscriptions.jsonl').write_text(
        json.dumps(replay_sub, ensure_ascii=False, default=str) + '\n',
        encoding='utf-8')
    with open(OUT / 'ledger' / 'predictions.jsonl', 'w', encoding='utf-8') as f:
        for row in scored:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
    (OUT / 'ledger' / 'grades.jsonl').write_text('', encoding='utf-8')
    paths = loop.Paths(str(ASOF), 'models')
    paths.subscriptions = OUT / 'subscriptions.jsonl'
    paths.predictions = OUT / 'ledger' / 'predictions.jsonl'
    paths.grades = OUT / 'ledger' / 'grades.jsonl'
    paths.deliveries = OUT / 'ledger' / 'deliveries.jsonl'
    paths.reports = OUT / 'reports'

    weeks = max(1, int((D2 - D).days / 7) + 2)
    render_args = argparse.Namespace(track_window=f'{weeks}w', top_slice=0.2,
                                     tier_high=0.10, tier_medium=0.20,
                                     min_slice_grades=25)

    # ---- report #1: the prediction, rendered at D ---------------------------
    freeze_clock(D.date())
    loop.deliver(paths, scored, render_args)
    rep1 = paths.reports / 'subscriptions' / sub_id / f'report_{D.date()}.html'

    # ---- grades from the outcomes published between D and the check date ----
    awards_full = pd.read_parquet(FULL / 'store' / 'awards.parquet')
    aw_latest, _ = sb.latest_awards(awards_full)
    pub = pd.to_datetime(aw_latest['publication_date'], errors='coerce')
    graded = aw_latest[(pub > D) & (pub <= D2)]
    scored_keys = {(r['procedure_id'], r['lot_id']) for r in scored}
    grades = []
    for _, a in graded.iterrows():
        if (a['procedure_id'], a['lot_id']) not in scored_keys:
            continue
        n = a['n_tenders']
        grades.append({'procedure_id': a['procedure_id'], 'lot_id': a['lot_id'],
                       'award_pub': str(a['publication_date'])[:10],
                       'n_tenders': None if pd.isna(n) else int(n),
                       'label': bool(pd.notna(n) and n <= 1),
                       'publication_number': a.get('publication_number')})
    with open(paths.grades, 'w', encoding='utf-8') as f:
        for g in grades:
            f.write(json.dumps(g, ensure_ascii=False) + '\n')
    print(f'[replay] {len(grades)} of the scored lots have outcomes published '
          f'by {D2.date()}')

    # ---- report #2: the check, rendered at the check date -------------------
    # empty scored: this render is about the Rückblick, not a new market view
    freeze_clock(D2.date())
    loop.deliver(paths, [], render_args)
    rep2 = paths.reports / 'subscriptions' / sub_id / f'report_{D2.date()}.html'

    print()
    delivered = [json.loads(line) for line in
                 open(paths.deliveries, encoding='utf-8')]
    outcome = {(g['procedure_id'], g['lot_id']): g for g in grades}
    for d in [d for d in delivered if d.get('kind') == 'pick']:
        g = outcome.get((d['procedure_id'], d['lot_id']))
        res = (f"{g['n_tenders']} bid(s)" if g and g['n_tenders'] is not None
               else 'outcome still unpublished')
        print(f"  pick @{str(d['ts'])[:10]}: {str(d['title'])[:55]!r} -> {res}")
    none = '(no report — nothing to recommend / nothing graded)'
    print(f'\n[replay] prediction: {rep1 if rep1.exists() else none}')
    print(f'[replay] check:      {rep2 if rep2.exists() else none}')


if __name__ == '__main__':
    main()
