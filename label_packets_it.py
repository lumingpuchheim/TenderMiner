"""Build the reading packets for the IT benchmark sample (divisions 48 + 72).

The doc/PROMPT_label_benchmark.md coordinator script, adapted 2026-08-22 for
the widened store: the sampling universe is lots whose cpv_main starts with 48
or 72 — construction's 10% was read in task-2/3, this is the IT counterpart.
Mechanics only — the script chooses *what* to read and never *how* to judge.
"""
import collections, json, random
from pathlib import Path

import pandas as pd

from evidence import LEISTUNG_RE   # the same Leistung split the gate reads

SEED, FRACTION, SHARD_SIZE, MIN_WINS = 7, 0.10, 50, 3
IT_PREFIXES = ('48', '72')
KEY = ['procedure_id', 'lot_id']
out = Path('/data/scratch/label_packets_it')
out.mkdir(parents=True, exist_ok=True)

tenders = pd.read_parquet('/data/store/tenders.parquet')
awards = pd.read_parquet('/data/store/awards.parquet')
lots = tenders.drop_duplicates(subset=KEY)
row = {(r.procedure_id, r.lot_id): r for r in lots.itertuples(index=False)}
it_keys = sorted(k for k, r in row.items()
                 if str(r.cpv_main or '').startswith(IT_PREFIXES))
lots_per_pub = collections.Counter(r.publication_number for r in row.values())

aw = awards[awards['winner_names'].apply(
    lambda x: x is not None and len(x) > 0)].explode('winner_names')
aw = aw[[k in row for k in zip(aw['procedure_id'], aw['lot_id'])]]
aw = aw.drop_duplicates(subset=['winner_names'] + KEY)
wins = collections.defaultdict(list)
for w, p, l in zip(aw['winner_names'], aw['procedure_id'], aw['lot_id']):
    wins[w].append((p, l))
firms = {w: ks for w, ks in wins.items() if len(ks) >= MIN_WINS}

winner_of = collections.defaultdict(list)
for w, ks in firms.items():
    for k in ks:
        winner_of[k].append(w)

def cpv_class(k):
    return str(row[k].cpv_main or '')[:4]

by_class = collections.defaultdict(set)
for w, ks in firms.items():
    for k in ks:
        if cpv_class(k):
            by_class[cpv_class(k)].add(w)

done = set()
for line in Path('/app/benchmark_relevance.jsonl').read_text(
        encoding='utf-8').splitlines():
    if line.strip() and not line.startswith('#'):
        c = json.loads(line)
        done.add((c['pub'], c['firm']))

def leistung(desc):
    m = None
    for m in LEISTUNG_RE.finditer(str(desc or '')):
        pass          # the LAST marker wins: project prose comes first
    return str(desc or '')[m.end():] if m else ''

def dossier(firm):
    # One win per procedure before a second from any: sorting by key alone put
    # eight lots of ONE framework award in front of a firm with 83 wins, and
    # the readers rightly called its trade unreadable (2026-08-22 reports:
    # CANCOM, EduXpert, New Horizons). The spread is what shows a trade; the
    # count of near-identical siblings shows nothing.
    ks, seen = [], set()
    for k in sorted(firms[firm]):
        if k[0] in seen:
            continue
        seen.add(k[0])
        ks.append(k)
    if len(ks) < 8:                       # fill from the rest, still deterministic
        ks += [k for k in sorted(firms[firm]) if k not in set(ks)]
    ks = ks[:8]
    return {'firm': firm, 'n_wins': len(firms[firm]),
            'won_lots': [{'title': str(row[k].title),
                          'cpv_main': str(row[k].cpv_main),
                          'buyer': str(row[k].buyer_name)} for k in ks]}

sample = sorted(random.Random(SEED).sample(
    it_keys, round(len(it_keys) * FRACTION)))
packets = []
for k in sample:
    r = row[k]
    rng = random.Random(f'{SEED}|{k[0]}|{k[1]}')
    own = sorted(winner_of[k])[:1]
    pool = sorted(by_class[cpv_class(k)] - set(winner_of[k]))
    near = [rng.choice(pool)] if pool else []
    pairs = [f for f in own + near if (r.publication_number, f) not in done]
    if not pairs:
        continue
    packets.append({
        'pub': str(r.publication_number),
        'title': str(r.title),
        'lots_in_pub': lots_per_pub[r.publication_number],
        'buyer_name': str(r.buyer_name),
        'contract_type': str(r.contract_type),
        'cpv_main': str(r.cpv_main), 'cpv_additional': str(r.cpv_additional),
        'description': str(r.description or '')[:4000],
        'leistung': leistung(r.description)[:4000],
        'candidates': [dict(dossier(f), won_this_lot=f in own) for f in pairs],
    })

for i in range(0, len(packets), SHARD_SIZE):
    n = i // SHARD_SIZE
    (out / f'shard_{n:02d}.json').write_text(
        json.dumps(packets[i:i + SHARD_SIZE], ensure_ascii=False, indent=1),
        encoding='utf-8')
print(f'{len(it_keys)} IT lots in store; sampled {len(sample)}; '
      f'{len(packets)} packets -> {out}/shard_*.json '
      f'({(len(packets) + SHARD_SIZE - 1) // SHARD_SIZE} shards)')
