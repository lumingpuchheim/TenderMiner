# PROMPT — grow the relevance benchmark by hand-reading 10% of the store

The relevance gate answers one question per lot: **is this tender the
customer's business?** Its only honest scorekeeper is
[`benchmark_relevance.jsonl`](../benchmark_relevance.jsonl) — 126 cases today,
which is too few to separate two configurations that differ on five lots
(`RELEVANCE.md`, phase 8e). This prompt grows it by reading a **10% random
sample of the store**: 2,335 of 23,354 lots.

Two files are involved and nothing else changes:

- `benchmark_relevance.jsonl` — the labels. Read by `evidence.py`
  (`--benchmark`, `--judge-benchmark`, `--sweep`) and `lexicon_receipt.py`.
- `benchmark_skipped.jsonl` — the lots a reader could not decide, with the
  reason. No program reads it; it exists so the undecidable cases are
  countable and re-readable instead of vanishing.

Everything below §5 is the prompt handed to one labeling agent. §1–§4 are the
coordinator's part: sample, shard, merge, verify.

---

## 1. Standing rules

- **The label is a reading, never a computation.** You decide by reading the
  lot's title and its Leistung section. You must not run the gate on a case
  you are labeling — not `explain.py`, not `evidence.py --keywords`, not
  `tryout.py`. A benchmark built from the gate's own output measures nothing.
- **When you cannot decide, do not label.** Undecidable is a normal outcome
  and costs nothing; a guessed label is worse than no label, because it
  silently vetoes configurations for the rest of the project's life. The skip
  list is where those go.
- **Append only.** Never edit or delete an existing line in
  `benchmark_relevance.jsonl`, including ones you disagree with. If a line
  looks wrong, say so in your report and leave it alone.
- Work in a git worktree, as `CLAUDE.md` requires.

## 2. The case format

One JSON object per line. Comment lines start with `#`.

```jsonl
{"pub": "00432657-2026", "firm": "Jebsen GmbH", "expect": "in", "note": "Schönkirchen Blitzschutz+Erdung school lot — trade buried in project prose"}
{"pub": "00510556-2025", "title_contains": "Los 11", "firm": "Jebsen GmbH", "expect": "out", "note": "SWN sibling lot: Allgemeiner Tiefbau"}
```

| field | meaning |
| --- | --- |
| `pub` | `publication_number` of the lot's notice |
| `title_contains` | substring of the lot title. **Mandatory whenever the publication holds more than one lot** — the matcher judges *every* lot under `pub` whose title contains this string, so an omitted value on a 12-lot notice silently labels all twelve. 659 publications in the store hold multiple lots; your packet tells you which |
| `firm` | winner name **exactly** as it appears in `data/store/awards.parquet` (copy it from the packet; do not retype) |
| `expect` | `in` = this lot is that firm's business, `out` = it is not |
| `note` | one line, ≤120 chars: the trade you read, and why it is or is not the firm's. This is what the next reader audits |

A skip line is the same object with `expect` replaced by
`"skip": "<reason>"`.

## 3. Sampling and sharding (coordinator, run once)

Mechanics only — the script chooses *what* to read and never *how* to judge
it. Save as `label_packets.py`, run from the repo root:

```python
"""Build the reading packets for the 10% benchmark sample. Mechanics only."""
import collections, json, random
from pathlib import Path

import pandas as pd

from evidence import LEISTUNG_RE   # the same Leistung split the gate reads

SEED, FRACTION, SHARD_SIZE, MIN_WINS = 7, 0.10, 50, 3
KEY = ['procedure_id', 'lot_id']
out = Path('data/label_packets')
out.mkdir(parents=True, exist_ok=True)

tenders = pd.read_parquet('data/store/tenders.parquet')
awards = pd.read_parquet('data/store/awards.parquet')
lots = tenders.drop_duplicates(subset=KEY)
row = {(r.procedure_id, r.lot_id): r for r in lots.itertuples(index=False)}
keys = sorted(row)
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
for line in Path('benchmark_relevance.jsonl').read_text(
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
    ks = sorted(firms[firm])[:8]
    return {'firm': firm, 'n_wins': len(firms[firm]),
            'won_lots': [{'title': str(row[k].title),
                          'cpv_main': str(row[k].cpv_main),
                          'buyer': str(row[k].buyer_name)} for k in ks]}

sample = sorted(random.Random(SEED).sample(keys, round(len(keys) * FRACTION)))
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
print(f'{len(packets)} packets -> {out}/shard_*.json '
      f'({(len(packets) + SHARD_SIZE - 1) // SHARD_SIZE} shards)')
```

Why these pairs, and no others:

- **the lot's own winner** (~10% of lots have one with ≥3 store wins) — a
  firm's own award is the cleanest `in` there is, and the gate is scored on it
  leave-one-out, so the case is real and not circular;
- **one firm from the lot's 4-digit CPV class** — deliberately the hard
  region. `RELEVANCE.md` names it as the one label gap no code proxy can
  close: *same-CPV-class wrong trades (Blitzschutz vs Starkstrom)*. Most of
  these are `out`; some are the recall cases the gate misses. Both matter.
  The 4-digit classes in branches 450–452 are object branches, not trades, so
  some draws are obvious (a medical-equipment firm against a window lot):
  label them `out` in one line and move on — a cheap label is still a label.

Spawn one agent per shard file, each with §5 plus its shard number. Shards
never touch the same output file.

## 4. Merge and verify (coordinator, after the shards return)

1. Concatenate `data/label_shards/shard_*.jsonl` in shard order, drop any
   line whose `(pub, firm, title_contains)` already appears in
   `benchmark_relevance.jsonl`, and append the rest under a dated comment
   header saying how the batch was made and how many lots were read to get
   it. Same for the skips into `benchmark_skipped.jsonl`.
2. `python evidence.py --judge-benchmark` — seconds, both gate modes,
   IN and OUT counted separately.
3. Report the new IN/OUT totals and the biggest disagreement classes.
   **A gate that now fails cases is a finding about the gate.** Do not touch
   the labels to make a receipt look better; that is the one move this file
   exists to prevent.

---

## 5. The labeling prompt (one agent, one shard)

> You are labeling shard **`<N>`** of the TenderMining relevance benchmark.
> Read `data/label_packets/shard_<N>.json`: a list of lots, each with its
> title, description, `leistung` (the procured-work section — present in only
> ~2% of notices, because only they carry the heading), CPV codes, buyer, and
> one or two **candidate firms**. Each candidate carries up to eight titles of tenders that firm
> actually won — that, not its name, is what tells you its trade.
>
> For every (lot, candidate firm) pair answer one question: **would this lot
> be that firm's own business?** Not "could they subcontract it", not "is it
> the same industry" — would this firm bid this work as their own scope.
>
> **How to read a lot.** Take the trade from the title first, then confirm or
> correct it from `leistung`. If `leistung` is empty, read `description` and
> remember that its first half is usually project prose (which building, which
> Bauabschnitt) and says nothing about the trade. The work is what is
> procured: "Neubau Grundschule — Los 12 Blitzschutzanlagen" is a lightning
> protection lot, not a school-building lot.
>
> **How to read a firm.** Look for the trade its won titles have in common.
> Three Estrich lots means an Estrich firm. Titles spanning Rohbau, TGA and
> Ausbau mean a general contractor — see the skip rules.
>
> **Decide one of three:**
>
> - `in` — the lot's procured work is the firm's trade. It stays `in` when the
>   trade sits inside a bigger project, when the text is thin but
>   unambiguous, and when the firm won this very lot (note it as
>   *eigener Zuschlag*).
> - `out` — the procured work is a different trade. This is the label the
>   benchmark is starved of: the neighbouring trade under the same CPV class
>   (Starkstrom vs Blitzschutz, Gerüstbau vs Estrich, Nachrichtentechnik vs
>   Gleisbau), the object-not-trade lot, the wrong-code lot.
> - **skip** — see below. No quota, no balance to hit. A shard that returns 20
>   labels and 30 skips from 50 lots has done the job correctly.
>
> **Skip — mandatory, not a judgment call:**
>
> 1. **The firm has no single trade.** Deutsche Bahn and its subsidiaries are
>    the standard example: everything from track to buildings to IT is
>    plausibly "their business", so `in` and `out` both have arguments and the
>    case would encode nothing but the labeler's mood. The same holds for
>    general contractors and holdings whose won titles span unrelated trades
>    (Rohbau *and* TGA *and* Ausbau), and for any candidate whose eight titles
>    do not agree on a trade. Skip **both** directions, reason
>    `"firm has no single trade: <what the titles span>"`.
> 2. **Generalunternehmer / Komplettleistung on the lot side** — the lot
>    procures a whole building, so the firm's trade is genuinely inside it as
>    a sub-scope. Skip; reason `"GU lot, trade is sub-scope"`.
> 3. **The trade is only mentioned in passing** — one "inkl. Blitzschutz" in
>    an LV list while the lot is plainly another trade. Skip rather than
>    guess; this class is an open question in the spec (`RELEVANCE.md`, risk
>    (b)) and a guessed label would decide it by accident.
> 4. **No readable work** — empty or boilerplate description with an
>    uninformative title (~8% of the store), or a title written in acronyms
>    you cannot expand (`ESTW Mühlacker - KTB`, `LST ESTW MOF`, `BÜ km
>    62,181`). Reason `"unreadable: <title>"`. This is our blindness, not the
>    document's silence — which is exactly why it is recorded.
> 5. **Anything else you would have to guess at.** Write the reason in your
>    own words.
>
> Never resolve a doubt by running the gate, and never look at
> `benchmark_relevance.jsonl` for a similar case before deciding — read the
> two texts in front of you and decide, or skip.
>
> **Write two files, yours alone** (create the directories if needed):
>
> - `data/label_shards/shard_<N>.jsonl` — one label per line:
>   `{"pub": ..., "title_contains": ..., "firm": ..., "expect": "in"|"out", "note": ...}`
>   `title_contains` is **required when the packet says `lots_in_pub` > 1** —
>   use a distinctive substring of that lot's title (a `Los` number is ideal)
>   that no sibling lot shares. Copy `pub` and `firm` verbatim from the packet.
> - `data/label_skips/shard_<N>.jsonl` — same shape with
>   `"skip": "<reason>"` in place of `expect`.
>
> UTF-8, umlauts fine, one JSON object per line, no trailing commas, nothing
> else in the files. Do not touch `benchmark_relevance.jsonl` — the
> coordinator merges.
>
> **Report back**, ≤10 lines: counts (lots read / in / out / skipped), the
> skip reasons by frequency, any case you labeled with visible unease and why,
> and any lot where the store data itself looks wrong (title and CPV
> disagreeing, description belonging to another notice).
