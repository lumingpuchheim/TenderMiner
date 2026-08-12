"""Playback: would TenderMining have recommended a firm's solo win BEFORE the
deadline, knowing only the past?

Time isolation lives in `asof.py` (REFACTOR.md phase 5): the as-of world
under data/asof/win holds only notices published before the cutoff; profile
references, trust list, thresholds and the CatBoost model are all rebuilt
inside it, and the engine's guarantees (and disclosed sidecar residual) are
documented there. The outcome (bid count) is read from the full store only
at evaluation time.

Usage:
    python rewind_win.py                                # Jebsen's 2026 solo win
    python rewind_win.py --firm "Firma GmbH" --since 2025-06-01
"""

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

import asof
import calibrate as cal
import config
import relevance as rel
import single_bidder as sb

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

ap = argparse.ArgumentParser(description=__doc__,
                             formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument('--firm', default='Jebsen GmbH', help='winner name to replay')
ap.add_argument('--since', default='2026-01-01',
                help='earliest award publication date of the target solo win')
ap.add_argument('--cpv', default='45', help='CPV prefix of the replayed slice')
ap.add_argument('--nuts', default='DE6,DEF',
                help='comma-separated NUTS prefixes of the replayed slice')
ap.add_argument('--data-dir', default=config.data_root())
args = ap.parse_args()
NUTS = [p for p in args.nuts.split(',') if p]

FULL = Path(args.data_dir)
t0 = time.time()

# ---- step 1: the target and the cutoff --------------------------------------
tenders_full = pd.read_parquet(FULL / 'store' / 'tenders.parquet')
awards_full = pd.read_parquet(FULL / 'store' / 'awards.parquet')
jb = awards_full[awards_full['winner_names'].apply(
    lambda x: x is not None and args.firm in list(x))]
solo = jb[(jb['n_tenders'] == 1)
          & (jb['publication_date'].astype(str) >= args.since)]
if solo.empty:
    sys.exit(f'[rewind_win] no solo win for {args.firm!r} since {args.since}')
tgt_award = solo.iloc[0]
key = (tgt_award['procedure_id'], tgt_award['lot_id'])
tlots = tenders_full.drop_duplicates(subset=sb.KEY)
tgt = tlots[(tlots['procedure_id'] == key[0]) & (tlots['lot_id'] == key[1])].iloc[0]
D = pd.Timestamp(tgt['deadline_date']) - pd.Timedelta(days=14)
print(f"[rewind_win] target: {tgt['title']!r} | buyer {tgt['buyer_name']}")
print(f"[rewind_win] notice published {tgt['publication_date']}, deadline {tgt['deadline_date']}")
print(f"[rewind_win] award published {tgt_award['publication_date']} with "
      f"{int(tgt_award['n_tenders'])} bid(s)")
print(f"[rewind_win] cutoff D = {D.date()} (deadline - 14d, the last honest cycle)")

# ---- step 2: the as-of world (asof.py owns the guarantees) ------------------
world = asof.World(FULL, FULL / 'asof' / 'win')
world.rewind(D)
print(f'[rewind_win] as-of store: {len(world.tenders)} tender rows, '
      f'{len(world.awards)} award rows '
      f'({len(tenders_full) - len(world.tenders)} / '
      f'{len(awards_full) - len(world.awards)} future rows excluded)')

# ---- step 3: as-of calibration (trust list + thresholds) --------------------
try:
    r = world.calibrate()
except cal.WorldTooThin as e:
    raise SystemExit(f'[rewind_win] {e}\n'
                     f'[rewind_win] too little of the store predates {D.date()} '
                     'to calibrate against. Pick a later cutoff.')
F = r['configs']['F hard/soft codes + floor/consensus']
print(f"[rewind_win] as-of F: text {F['threshold']:.3f}, hard {F['code_threshold']:.3f}, "
      f"soft {F['soft_threshold']:.3f} (floor {F['soft_floor']:.2f}/k{F['soft_consensus']}), "
      f"leakage {F['leakage']:.1%}")

# ---- step 4: as-of profile + gate -------------------------------------------
# Recipe F carries the bars on the config; the sub below carries the same
# values, as this program always has — either carrier alone decides the same.
cfg = world.calibrated_config('F')
gate = world.gate(cfg)
refs = asof.refs_for_firm(gate, world.awards, args.firm)
if not refs:
    sys.exit(f'[rewind_win] {args.firm!r} had no resolvable win before {D.date()}')
print(f'[rewind_win] profile as of {D.date()}: {len(refs)} won tenders {refs}')
sub = {'sub_id': 'playback', 'version': 1, 'name': args.firm,
       'profile_refs': refs, 'min_relevance': F['threshold'],
       'min_code_hard': F['code_threshold'], 'min_code_soft': F['soft_threshold']}
profile = rel.build_profile(gate, sub)

# ---- step 5: as-of model ----------------------------------------------------
model = world.model()
print(f'[rewind_win] model trained on {world.data.groupby(sb.KEY).ngroups} '
      f'labeled lots (all pre-{D.date()}) in {time.time() - t0:.0f}s')

# ---- step 6: replay the cycle at D ------------------------------------------
open_t = sb.open_tenders(world.tenders, world.aw)
deadline = pd.to_datetime(open_t.get('deadline_date'), errors='coerce')
open_t = open_t[deadline >= D + pd.Timedelta(days=14)]  # min_deadline_days promise
Xo, _, _, _ = sb.build_features(open_t, world.roles, list_frame=world.tenders)
scores = sb.predict(model, Xo)
flags = scores >= 0.5

in_slice, verdicts = [], {}
for (i, row), s, fl in zip(open_t.iterrows(), scores, flags):
    cpv = str(row.get('cpv_main') or '')
    nuts = str(row.get('place_nuts3') or '')
    if not cpv.startswith(args.cpv):
        continue
    if NUTS and not any(nuts.startswith(p) for p in NUTS):
        continue
    d = {'procedure_id': row['procedure_id'], 'lot_id': row['lot_id'],
         'buyer_name': row.get('buyer_name'), 'title': row.get('title'),
         'publication_number': row.get('publication_number'),
         'score': float(s), 'flag': bool(fl)}
    ok, near, tx, cd, why, ch = rel.judge(gate, profile, d)
    verdicts[(d['procedure_id'], d['lot_id'])] = (ok, tx, cd, ch, why)
    if ok:
        d.update(text=tx, code=cd, hard=ch)
        in_slice.append(d)

# ONE bar (RELEVANCE.md decision 2026-08-05): passing the gate means
# recommendable; a pick just needs the competition flag on top
in_slice.sort(key=lambda d: -d['score'])
picks = [d for d in in_slice if d['flag']][:5]
print(f'[rewind_win] market at {D.date()}: {len(in_slice)} gated lots, {len(picks)} picks')
print()
aw_full, _ = sb.latest_awards(awards_full)
outcome = {(a['procedure_id'], a['lot_id']): a['n_tenders']
           for _, a in aw_full.iterrows()}
for rank, d in enumerate(picks, 1):
    n = outcome.get((d['procedure_id'], d['lot_id']))
    res = f'-> ended with {int(n)} bid(s)' if n is not None else '-> outcome not yet published'
    star = '  <== THE TARGET' if (d['procedure_id'], d['lot_id']) == key else ''
    print(f"  pick {rank}: {str(d['title'])[:55]!r} [{str(d['buyer_name'])[:30]}] "
          f"score {d['score']:.2f} text {d['text'] if d['text'] is None else round(d['text'], 3)} "
          f"hard {round(d['hard'], 3)} {res}{star}")
print()
v = verdicts.get(key)
if v is None:
    print('[rewind_win] TARGET WAS NOT IN THE CANDIDATE SET (check filters)')
else:
    ok, tx, cd, ch, why = v
    tgt_row = [d for d in in_slice if (d['procedure_id'], d['lot_id']) == key]
    in_picks = any((d['procedure_id'], d['lot_id']) == key for d in picks)
    print(f'[rewind_win] TARGET verdict: gate passed={ok}, text={tx:.3f}, code={cd:.3f}, '
          f'hard={ch:.3f}, why={why}')
    if tgt_row:
        d = tgt_row[0]
        rank = in_slice.index(d) + 1
        print(f'[rewind_win] competition: score={d["score"]:.3f} flag={d["flag"]} '
              f'| rank {rank}/{len(in_slice)} in the slice')
    print(f'[rewind_win] RECOMMENDED AS PICK: {in_picks}')
