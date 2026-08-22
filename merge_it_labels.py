"""Merge the IT labeling shards into the benchmark — doc/PROMPT_label_benchmark.md §4.

Coordinator step, mechanics only. Verifies before it writes, and refuses on any
finding rather than merging a file that would then be permanent:

  * every (pub, firm) is a pair the packet actually offered — no invented case;
  * no case duplicates another shard's, or one already in the benchmark;
  * every pair in every packet is accounted for exactly once (labeled or
    skipped) — a shard that died mid-write is caught here, not weeks later;
  * `title_contains` resolves to exactly ONE lot of its publication in the
    store. The shard agents could not check this (no store in the worktree),
    and it is the error that silently labels twelve sibling lots at once.

    --store DIR must hold tenders.parquet; pass the server's copy.

Writes nothing until every check passes. Append-only: existing lines are never
touched, and the batch goes in under a dated header saying how it was made.

    python merge_it_labels.py --store /data/store            # verify only
    python merge_it_labels.py --store /data/store --write    # verify, then append
"""

import argparse
import collections
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
BENCH = REPO / 'benchmark_relevance.jsonl'
SKIPPED = REPO / 'benchmark_skipped.jsonl'
PACKETS = REPO / 'data' / 'label_packets_it'
SHARDS = REPO / 'data' / 'label_shards_it'
SKIPS = REPO / 'data' / 'label_skips_it'


def read_jsonl(path):
    return [json.loads(l) for l in path.read_text(encoding='utf-8').splitlines()
            if l.strip() and not l.startswith('#')]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--store', default=None,
                    help='directory holding tenders.parquet (for the '
                         'title_contains uniqueness check)')
    ap.add_argument('--drop-ambiguous', action='store_true', dest='drop_ambiguous',
                    help='move labels whose title_contains matches several lots '
                         'to the skip file instead of refusing the merge')
    ap.add_argument('--write', action='store_true',
                    help='append to the benchmark; without it, verify only')
    args = ap.parse_args()
    problems, dropped = [], []

    # ---- the packets: what was offered to be judged
    offered = collections.defaultdict(set)     # pub -> {firm}
    pairs = set()                              # (pub, firm)
    shard_of = {}
    for f in sorted(PACKETS.glob('shard_*.json')):
        n = f.stem.split('_')[1]
        for p in json.loads(f.read_text(encoding='utf-8')):
            for c in p['candidates']:
                offered[p['pub']].add(c['firm'])
                pairs.add((p['pub'], c['firm']))
                shard_of[(p['pub'], c['firm'])] = n

    labels, skips = [], []
    for f in sorted(SHARDS.glob('shard_*.jsonl')):
        labels += [dict(r, _shard=f.stem.split('_')[1]) for r in read_jsonl(f)]
    for f in sorted(SKIPS.glob('shard_*.jsonl')):
        skips += [dict(r, _shard=f.stem.split('_')[1]) for r in read_jsonl(f)]

    done_shards = {r['_shard'] for r in labels}
    missing = sorted({s for _, s in shard_of.items()} - done_shards)
    if missing:
        problems.append(f'shards with no label file: {", ".join(missing)}')

    # ---- every label names a pair the packet offered
    for r in labels + skips:
        if r['firm'] not in offered.get(r['pub'], ()):
            problems.append(f"invented case: {r['pub']} / {r['firm']} "
                            f"(shard {r['_shard']})")

    # ---- coverage: each offered pair judged exactly once, in finished shards
    seen = collections.Counter((r['pub'], r['firm']) for r in labels + skips)
    for pair, n in shard_of.items():
        if n in done_shards and seen[pair] == 0:
            problems.append(f'unjudged pair in finished shard {n}: {pair[0]} / {pair[1]}')
    # a pub may legitimately hold two labeled lots for one firm, separated by
    # title_contains; only an exact (pub, firm, title_contains) repeat is a bug
    key = collections.Counter((r['pub'], r['firm'], r.get('title_contains', ''))
                              for r in labels + skips)
    for k, n in key.items():
        if n > 1:
            problems.append(f'duplicate case x{n}: {k}')

    # ---- no collision with what the benchmark already holds
    existing = {(c['pub'], c['firm'], c.get('title_contains', ''))
                for c in read_jsonl(BENCH)}
    for r in labels:
        if (r['pub'], r['firm'], r.get('title_contains', '')) in existing:
            problems.append(f"already in benchmark: {r['pub']} / {r['firm']}")

    # ---- title_contains resolves to exactly one lot of its publication
    titles_index = REPO / 'data' / 'pub_titles.json'
    if args.store or titles_index.exists():
        if args.store:
            import pandas as pd
            t = pd.read_parquet(Path(args.store) / 'tenders.parquet',
                                columns=['publication_number', 'procedure_id',
                                         'lot_id', 'title'])
            t = t.drop_duplicates(subset=['procedure_id', 'lot_id'])
            by_pub = collections.defaultdict(list)
            for pub, title in zip(t['publication_number'], t['title']):
                by_pub[str(pub)].append(str(title))
        else:
            # publication -> [lot titles], built from the server's store (the
            # worktree has none); see pipeline/all-trades.md
            by_pub = json.loads(titles_index.read_text(encoding='utf-8'))
        for r in labels:
            titles = by_pub.get(r['pub'], [])
            if not titles:
                problems.append(f"publication not in store: {r['pub']}")
                continue
            if len(titles) == 1:
                continue
            tc = r.get('title_contains')
            if not tc:
                problems.append(f"{r['pub']} holds {len(titles)} lots but the "
                                f"case has no title_contains ({r['firm']})")
                continue
            hits = sum(1 for x in titles if tc in x)
            if hits != 1:
                msg = (f"title_contains {tc!r} matches {hits} of "
                       f"{len(titles)} lots in {r['pub']}")
                if args.drop_ambiguous:
                    # Not silently discarded: it becomes a skip, so the case
                    # stays countable and re-readable. Some of these are lots
                    # whose siblings are identical work, where the broad match
                    # would be harmless — but telling those from the ones that
                    # would cover a DIFFERENT lot needs a reader, and a guess
                    # here is exactly what the skip list exists to prevent.
                    r['_drop'] = f'ambiguous title_contains: {hits} of {len(titles)} lots'
                    dropped.append(msg)
                else:
                    problems.append(msg)
    else:
        print('[warn] no --store: title_contains uniqueness NOT checked')

    if dropped:
        moved = [r for r in labels if r.get('_drop')]
        labels = [r for r in labels if not r.get('_drop')]
        for r in moved:
            r['skip'] = r.pop('_drop')
            r.pop('expect', None)
        skips += moved
        print(f'[drop] {len(moved)} labels moved to skips (ambiguous title_contains)')

    n_in = sum(1 for r in labels if r.get('expect') == 'in')
    n_out = sum(1 for r in labels if r.get('expect') == 'out')
    print(f'shards labeled: {len(done_shards)} of {len(set(shard_of.values()))}')
    print(f'labels {len(labels)} ({n_in} in, {n_out} out), skips {len(skips)}')
    print(f'problems: {len(problems)}')
    for p in problems[:40]:
        print('  -', p)
    if problems:
        print('\nNothing merged. Every problem above is a case that would have '
              'been permanent.')
        return 1
    if not args.write:
        print('\nAll checks pass. Re-run with --write to append.')
        return 0

    header = (f'# IT benchmark batch (CPV 48/72), {len(done_shards)} shards, '
              f'2026-08-22: a 10% sample of the widened store\'s IT lots read '
              f'by labeling agents under doc/PROMPT_label_benchmark.md §5 '
              f'(data/PROMPT_shard_it.md), each lot paired with its own winner '
              f'and one same-CPV-class firm. {len(labels)} labels '
              f'({n_in} in, {n_out} out) from {len(labels) + len(skips)} pairs; '
              f'{len(skips)} undecidable, in benchmark_skipped.jsonl.')
    with BENCH.open('a', encoding='utf-8', newline='\n') as fh:
        fh.write(header + '\n')
        for r in labels:
            fh.write(json.dumps({k: v for k, v in r.items() if k != '_shard'},
                                ensure_ascii=False) + '\n')
    with SKIPPED.open('a', encoding='utf-8', newline='\n') as fh:
        fh.write(header.replace('labels', 'skips') + '\n')
        for r in skips:
            fh.write(json.dumps({k: v for k, v in r.items() if k != '_shard'},
                                ensure_ascii=False) + '\n')
    print(f'\nappended {len(labels)} cases to {BENCH.name}, '
          f'{len(skips)} to {SKIPPED.name}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
