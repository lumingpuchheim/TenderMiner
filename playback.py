"""Playback: would TenderMining have recommended a firm's solo win BEFORE the
deadline, knowing only the past?

Time isolation by construction: an as-of data directory (data/playback_asof)
holds only notices published before the cutoff; profile, trust list,
thresholds and the CatBoost model are all rebuilt inside it. The outcome
(bid count) is read from the full store only at evaluation time. Residuals
disclosed: the embedding vectors are looked up from the full sidecar
(per-notice, no cross-notice info) and the system's *design* postdates the
replayed period.

Usage:
    python playback.py                                # Jebsen's 2026 solo win
    python playback.py --firm "Firma GmbH" --since 2025-06-01
"""

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

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
ASOF = FULL / 'playback_asof'
t0 = time.time()

# ---- step 1: the target and the cutoff --------------------------------------
tenders_full = pd.read_parquet(FULL / 'store' / 'tenders.parquet')
awards_full = pd.read_parquet(FULL / 'store' / 'awards.parquet')
jb = awards_full[awards_full['winner_names'].apply(
    lambda x: x is not None and args.firm in list(x))]
solo = jb[(jb['n_tenders'] == 1)
          & (jb['publication_date'].astype(str) >= args.since)]
if solo.empty:
    sys.exit(f'[playback] no solo win for {args.firm!r} since {args.since}')
tgt_award = solo.iloc[0]
key = (tgt_award['procedure_id'], tgt_award['lot_id'])
tlots = tenders_full.drop_duplicates(subset=sb.KEY)
tgt = tlots[(tlots['procedure_id'] == key[0]) & (tlots['lot_id'] == key[1])].iloc[0]
D = pd.Timestamp(tgt['deadline_date']) - pd.Timedelta(days=14)
print(f"[playback] target: {tgt['title']!r} | buyer {tgt['buyer_name']}")
print(f"[playback] notice published {tgt['publication_date']}, deadline {tgt['deadline_date']}")
print(f"[playback] award published {tgt_award['publication_date']} with "
      f"{int(tgt_award['n_tenders'])} bid(s)")
print(f"[playback] cutoff D = {D.date()} (deadline - 14d, the last honest cycle)")

# ---- step 2: the as-of world ------------------------------------------------
if ASOF.exists():
    shutil.rmtree(ASOF)
(ASOF / 'data' / 'store').mkdir(parents=True)
import pyarrow.compute as pc
import pyarrow.parquet as pq
for name in ('tenders', 'awards'):
    tab = pq.read_table(FULL / 'store' / f'{name}.parquet')
    mask = pc.less(tab.column('publication_date'), D.date())
    # pyarrow filter preserves the schema including the per-column role
    # metadata that load_with_roles depends on (a pandas round-trip drops it)
    pq.write_table(tab.filter(mask), ASOF / 'data' / 'store' / f'{name}.parquet')
pub_t = pd.to_datetime(tenders_full['publication_date'])
pub_a = pd.to_datetime(awards_full['publication_date'])
tenders = tenders_full[pub_t < D].copy()
awards = awards_full[pub_a < D].copy()
shutil.copytree(FULL / 'embeddings', ASOF / 'data' / 'embeddings')
print(f'[playback] as-of store: {len(tenders)} tender rows, {len(awards)} award rows '
      f'({len(tenders_full) - len(tenders)} / {len(awards_full) - len(awards)} future rows excluded)')

# ---- step 3: as-of calibration (trust list + thresholds) --------------------
r = cal.calibrate(str(ASOF / 'data'))
F = r['configs']['F hard/soft codes + floor/consensus']
trust_json = ASOF / 'trusted_codes_asof.json'
trust_json.write_text(json.dumps(
    {'baseline': r['baseline'], 'cut': r['trust_cut'],
     'codes': {k: {'n': v['n'], 'cohesion': v['cohesion'],
                   'trusted': v['cohesion'] >= r['trust_cut']}
               for k, v in r['cohesion'].items()}}), encoding='utf-8')
print(f"[playback] as-of F: text {F['threshold']:.3f}, hard {F['code_threshold']:.3f}, "
      f"soft {F['soft_threshold']:.3f} (floor {F['soft_floor']:.2f}/k{F['soft_consensus']}), "
      f"leakage {F['leakage']:.1%}")

# ---- step 4: as-of profile + gate -------------------------------------------
# the as-of calibration as a config value (REFACTOR.md phase 3); assigning
# to relevance's module globals no longer reaches the gate
cfg = rel.DEFAULT_CONFIG.replace(trusted_codes=trust_json,
                                 soft_floor=F['soft_floor'],
                                 soft_consensus=F['soft_consensus'])
gate = rel.Gate(str(ASOF / 'data'), config=cfg)
jb_asof = awards[awards['winner_names'].apply(
    lambda x: x is not None and args.firm in list(x))]
refs = sorted({gate.rows[gate.by_key[(p, l)]]['publication_number']
               for p, l in zip(jb_asof['procedure_id'], jb_asof['lot_id'])
               if (p, l) in gate.by_key})
if not refs:
    sys.exit(f'[playback] {args.firm!r} had no resolvable win before {D.date()}')
print(f'[playback] profile as of {D.date()}: {len(refs)} won tenders {refs}')
sub = {'sub_id': 'playback', 'version': 1, 'name': args.firm,
       'profile_refs': refs, 'min_relevance': F['threshold'],
       'min_code_hard': F['code_threshold'], 'min_code_soft': F['soft_threshold']}
profile = rel.build_profile(gate, sub)

# ---- step 5: as-of model ----------------------------------------------------
tenders_r, roles = sb.load_with_roles(ASOF / 'data' / 'store' / 'tenders.parquet')
awards_r, _ = sb.load_with_roles(ASOF / 'data' / 'store' / 'awards.parquet')
data, aw, _ = sb.assemble(tenders_r, awards_r)
X, cat_cols, num_cols, _ = sb.build_features(data, roles, list_frame=tenders_r)
k = data.groupby(sb.KEY)['label'].transform('size')
model = sb.train(X, data['label'].to_numpy(), (1.0 / k).to_numpy(), cat_cols)
print(f'[playback] model trained on {data.groupby(sb.KEY).ngroups} labeled lots '
      f'(all pre-{D.date()}) in {time.time() - t0:.0f}s')

# ---- step 6: replay the cycle at D ------------------------------------------
open_t = sb.open_tenders(tenders_r, aw)
deadline = pd.to_datetime(open_t.get('deadline_date'), errors='coerce')
open_t = open_t[deadline >= D + pd.Timedelta(days=14)]  # min_deadline_days promise
Xo, _, _, _ = sb.build_features(open_t, roles, list_frame=tenders_r)
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
print(f'[playback] market at {D.date()}: {len(in_slice)} gated lots, {len(picks)} picks')
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
    print('[playback] TARGET WAS NOT IN THE CANDIDATE SET (check filters)')
else:
    ok, tx, cd, ch, why = v
    tgt_row = [d for d in in_slice if (d['procedure_id'], d['lot_id']) == key]
    in_picks = any((d['procedure_id'], d['lot_id']) == key for d in picks)
    print(f'[playback] TARGET verdict: gate passed={ok}, text={tx:.3f}, code={cd:.3f}, '
          f'hard={ch:.3f}, why={why}')
    if tgt_row:
        d = tgt_row[0]
        rank = in_slice.index(d) + 1
        print(f'[playback] competition: score={d["score"]:.3f} flag={d["flag"]} '
              f'| rank {rank}/{len(in_slice)} in the slice')
    print(f'[playback] RECOMMENDED AS PICK: {in_picks}')
