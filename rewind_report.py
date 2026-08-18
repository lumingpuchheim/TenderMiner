"""Replay: two customer reports across time — the prediction and its check.

Renders what a subscription's REAL weekly report would have said at a past
cutoff D (picks made by a model that provably could not see past D), then a
second report at --check-date (default today) where "Ihre Empfehlungen im
Rückblick" grades those picks against the outcomes the market has published
since. Two HTML files, side by side: what we said, and whether it was right.

Time isolation lives in `asof.py` (REFACTOR.md phase 5): an as-of world
under data/asof/report holds only pre-D publications; trust list, thresholds
(configuration H), profile and CatBoost model are rebuilt inside it, and the
engine's guarantees are documented there. The full store is consulted only
to grade. Everything is written to data/replay/<sub_id>/ — real ledgers and
reports are never touched.

Choosing D: awards follow deadlines by ~3 months, so a cutoff 3-6 months
back usually has graded outcomes; `python rewind_all.py` prints per-subscription
pick weeks with outcomes — any week listed there works.

The subject should be REAL: --firm replays any actual winner company from
the awards store (profile from their as-of wins, region from where they
won) — a track record for an invented demo customer proves nothing. Use
--scan first to see which real firms have graded picks at a cutoff.

Usage:
    python rewind_report.py --scan --cutoff 2026-04-15
    python rewind_report.py --firm "Firma GmbH" --cutoff 2026-04-15
    python rewind_report.py --sub jebsen-blitzschutz --cutoff 2026-03-25 --check-date 2026-08-01
"""

import argparse
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import asof
import calibrate as cal
import config
import ledger
import relevance as rel
import selection
import single_bidder as sb
import subscriptions
import util
import predicting
import delivering

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

FLAG_THRESHOLD = sb.THRESHOLD  # the loop's --threshold default, one value (PARAMETERS.md 13)


def freeze_clock(day):
    """deliver() reads the clock; a replay must render 'today' = the cutoff."""
    util.now_utc = lambda: datetime(day.year, day.month, day.day, 8, 0,
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
    ap.add_argument('--data-dir', default=config.data_root())
    args = ap.parse_args()
    if not args.scan and not (bool(args.sub) ^ bool(args.firm)):
        sys.exit('[rewind_report] give exactly one of --sub / --firm (or --scan)')
    t0 = time.time()

    D = pd.Timestamp(args.cutoff)
    D2 = pd.Timestamp(args.check_date) if args.check_date else pd.Timestamp.today().normalize()
    if D2 <= D:
        sys.exit('[rewind_report] --check-date must lie after --cutoff')
    FULL = Path(args.data_dir)

    sub = None
    if args.sub:
        sub = subscriptions.one(FULL, str(D.date()), args.sub)
        if sub is None:  # subscription may postdate the cutoff — take today's line
            sub = subscriptions.one(FULL, str(pd.Timestamp.today().date()),
                                    args.sub)
        if sub is None:
            sys.exit(f'[rewind_report] no active subscription {args.sub!r}')

    # ---- the as-of world (asof.py owns the guarantees) -----------------------
    world = asof.World(FULL, FULL / 'asof' / 'report')
    world.rewind(D)
    print(f'[rewind_report] as-of world at {D.date()} built in {time.time() - t0:.0f}s')

    # ---- as-of calibration (configuration H) + gate constants ---------------
    try:
        r = world.calibrate()
    except cal.WorldTooThin as e:
        raise SystemExit(
            f'[rewind_report] {e}\n'
            f'[rewind_report] the store barely reaches back to {D.date()}; a gate '
            'calibrated on that little would not be the gate this replay '
            'claims to show. Pick a later cutoff — --scan lists the ones '
            'with graded picks.')
    H = r['configs']['H single bar + trade-read corroboration']
    cfg = world.calibrated_config('H')
    print(f"[rewind_report] as-of gate H: text {H['threshold']:.3f}, hard "
          f"{H['code_threshold']:.3f}, soft {H['soft_threshold']:.3f}, "
          f"corr {H['corr_form']}@{H['corr_param']:.3f}")

    # ---- as-of profile from the wins the firm had at D ----------------------
    gate = world.gate(cfg)
    awards_asof = world.awards
    lots_nuts = {(p, l): n for p, l, n in zip(
        world.tenders['procedure_id'], world.tenders['lot_id'],
        world.tenders['place_nuts3'])}

    def firm_asof(firm_name):
        """(refs, NUTS1 prefixes) of a real firm's resolvable as-of wins.
        Refs come from the engine; the NUTS prefixes are this program's own
        need (a firm's region = where it won)."""
        f_refs = asof.refs_for_firm(gate, awards_asof, firm_name)
        aw_f = awards_asof[awards_asof['winner_names'].apply(
            lambda x: x is not None and firm_name in list(x))]
        keys = [(p, l) for p, l in zip(aw_f['procedure_id'], aw_f['lot_id'])
                if (p, l) in gate.by_key]
        nuts = sorted({str(lots_nuts.get(k))[:3] for k in keys
                       if isinstance(lots_nuts.get(k), str) and lots_nuts.get(k)})
        return f_refs, nuts

    if args.firm:
        f_refs, nuts1 = firm_asof(args.firm)
        if len(f_refs) < 2:
            sys.exit(f'[rewind_report] {args.firm!r} has fewer than 2 resolvable wins '
                     f'before {D.date()} — try --scan to find replayable firms')
        slug = re.sub(r'[^a-z0-9]+', '-', args.firm.casefold()).strip('-')
        sub = {'sub_id': f'firm-{slug}', 'name': args.firm,
               'cpv_prefixes': ['45'], 'nuts_prefixes': nuts1,
               'min_deadline_days': 14, 'max_picks': 5,
               'profile_refs': f_refs}
        print(f'[rewind_report] real firm {args.firm!r}: {len(f_refs)} as-of wins, '
              f'region {"/".join(nuts1)}')

    firm = sub.get('name') if sub else None
    replay_sub = None
    if sub is not None:
        refs, _ = firm_asof(firm) if firm else ([], [])
        if not refs and sub.get('profile_refs'):
            sys.exit(f'[rewind_report] {firm!r} had no resolvable win before '
                     f'{D.date()} — pick a later cutoff')
        replay_sub = dict(sub, profile_refs=refs or None,
                          min_relevance=H['threshold'] if refs else None,
                          min_code_hard=H['code_threshold'],
                          min_code_soft=H['soft_threshold'],
                          version=sub.get('version', 1),
                          effective_from=str(D.date()))
        print(f'[rewind_report] profile as of {D.date()}: {len(refs)} won tenders')

    # ---- as-of model + scores for the open market at D ----------------------
    model = world.model()
    print(f'[rewind_report] model trained on {world.data.groupby(sb.KEY).ngroups} '
          f'pre-{D.date()} lots in {time.time() - t0:.0f}s')

    open_t = sb.open_tenders(world.tenders, world.aw)
    deadline = pd.to_datetime(open_t.get('deadline_date'), errors='coerce')
    open_t = open_t[deadline.isna() | (deadline >= D)]
    Xo, cats_o, _, _ = sb.build_features(open_t, world.roles,
                                         list_frame=world.tenders)
    scores = sb.predict(model, Xo)
    why_lonely, why_crowded = predicting.explain_rows(model, Xo, cats_o)
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
    print(f'[rewind_report] {len(scored)} open lots scored at {D.date()}')

    # ---- scan: which REAL firms have graded picks at this cutoff? -----------
    if args.scan:
        awards_full = pd.read_parquet(FULL / 'store' / 'awards.parquet')
        aw_latest, _ = sb.latest_awards(awards_full)
        pub = pd.to_datetime(aw_latest['publication_date'], errors='coerce')
        graded_aw = aw_latest[(pub > D) & (pub <= D2)]
        outcome = {(a['procedure_id'], a['lot_id']): int(a['n_tenders'])
                   for _, a in graded_aw.iterrows() if pd.notna(a['n_tenders'])}
        w = awards_asof[awards_asof['winner_names'].apply(
            lambda x: x is not None and len(x) > 0)].explode('winner_names')
        firms = [f for f, c in w['winner_names'].value_counts().items() if c >= 2]
        print(f'[rewind_report] scanning {len(firms)} real firms with >= 2 as-of wins')
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
            # the same selection the firm would have been served, not a
            # fourth copy of it (REFACTOR.md phase 4a): a firm's region is
            # where it won, and 14 days is the horizon this scan asks about
            picks = selection.for_sub(
                {'nuts_prefixes': nuts1, 'min_deadline_days': 14,
                 'max_picks': 5},
                scored, D.date(), gate=gate, profile=profile).picks
            graded = [(r0, outcome[(r0['procedure_id'], r0['lot_id'])])
                      for r0 in picks
                      if (r0['procedure_id'], r0['lot_id']) in outcome]
            if graded:
                hits = sum(1 for _, n in graded if n <= 1)
                results.append((hits, len(graded), len(picks), firm_name, graded))
        results.sort(key=lambda t: (-t[0], -t[1]))
        if not results:
            print('[rewind_report] no real firm has a graded pick at this cutoff — '
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
    subscriptions.write_sandbox(OUT, [replay_sub])
    paths = util.Paths(str(world.work), 'models')
    paths.subs_home = OUT
    # one sandbox home for every ledger this replay writes. OUT is recreated on
    # each run (rmtree above), so appending to an empty store is what the old
    # truncate-and-write of two files amounted to.
    paths.ledger_home = ledger.start(OUT)
    paths.deliveries_home = OUT
    ledger.append(OUT, 'predictions', scored)
    paths.reports = OUT / 'reports'

    weeks = max(1, int((D2 - D).days / 7) + 2)
    render_args = argparse.Namespace(track_window=f'{weeks}w', top_slice=0.2,
                                     tier_high=0.10, tier_medium=0.20,
                                     min_slice_grades=25,
                                     mail=False)   # a rewind mails nobody

    # ---- report #1: the prediction, rendered at D ---------------------------
    freeze_clock(D.date())
    delivering.deliver(paths, scored, render_args)
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
    ledger.append(paths.ledger_home, 'grades', grades)
    print(f'[rewind_report] {len(grades)} of the scored lots have outcomes published '
          f'by {D2.date()}')

    # ---- report #2: the check, rendered at the check date -------------------
    # empty scored: this render is about the Rückblick, not a new market view
    freeze_clock(D2.date())
    delivering.deliver(paths, [], render_args)
    rep2 = paths.reports / 'subscriptions' / sub_id / f'report_{D2.date()}.html'

    print()
    delivered = ledger.read(paths.deliveries_home, 'deliveries')
    outcome = {(g['procedure_id'], g['lot_id']): g for g in grades}
    for d in [d for d in delivered if d.get('kind') == 'pick']:
        g = outcome.get((d['procedure_id'], d['lot_id']))
        res = (f"{g['n_tenders']} bid(s)" if g and g['n_tenders'] is not None
               else 'outcome still unpublished')
        print(f"  pick @{str(d['ts'])[:10]}: {str(d['title'])[:55]!r} -> {res}")
    none = '(no report — nothing to recommend / nothing graded)'
    print(f'\n[rewind_report] prediction: {rep1 if rep1.exists() else none}')
    print(f'[rewind_report] check:      {rep2 if rep2.exists() else none}')


if __name__ == '__main__':
    main()
