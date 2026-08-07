"""TenderMining lexicon receipt — is a customer's lexicon made of trade
words, and do the hand-labeled cases still judge correctly?

Answers the question the aggregate receipts cannot: `evidence.py --sweep`
scores against synthetic off-class negatives, and its benchmark total hides
direction — the committed cases are recall-heavy, so a change that stops
wrong-trade picks can look like a regression in the total while doing
exactly what it was built for. This receipt therefore

  1. runs benchmark_relevance.jsonl through the REAL judge() under each
     lexicon configuration, and reports IN (should pass -> recall) and OUT
     (should be rejected -> precision) SEPARATELY, and
  2. prints the lexicons themselves, because the operator's test for a
     lexicon is reading it: every word should name a Gewerk or a material.

Configurations are the two committed switches, so this is also the A/B for
rolling either back — and 'roots only' is the arm that answers whether the
dictionary buyer-share test still earns its place now that the vocabulary
judges each word directly:
    base        BUYER_DIVERSITY=0 TRADE_ROOTS=0   (phase 8e, as shipped)
    dicts       BUYER_DIVERSITY=1 TRADE_ROOTS=0   (phase 8f (A) alone)
    roots only  BUYER_DIVERSITY=0 TRADE_ROOTS=1   (vocabulary alone)
    both        BUYER_DIVERSITY=1 TRADE_ROOTS=1   (live default)

Usage:
    python lexicon_receipt.py                     # all three, per direction
    python lexicon_receipt.py --lexicons          # + every firm's lexicon
    python lexicon_receipt.py --config roots      # one configuration only
    python lexicon_receipt.py --out receipt.md    # also write it as markdown
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import evidence as evd

CONFIGS = [('base', False, False), ('dicts', True, False),
           ('roots-only', False, True), ('both', True, True)]
BENCH = Path('benchmark_relevance.jsonl')


def load_cases():
    return [json.loads(ln) for ln in
            BENCH.read_text(encoding='utf-8').splitlines()
            if ln.strip() and not ln.startswith('#')]


def apply(buyer_diversity, trade_roots):
    """Set the switches and drop the memoised dictionaries — they are
    derived under the switches and must not survive a configuration
    change (this is why the on-disk cache carries them in its key)."""
    evd.BUYER_DIVERSITY = buyer_diversity
    evd.TRADE_ROOTS = trade_roots
    evd._TRADE_DICTS = None


def run_config(data_dir, name, buyer_diversity, trade_roots, want_lexicons):
    """-> (per-direction counts, {firm: lexicon}). Cases are judged leave-
    one-out, the same discipline evidence.judge_benchmark uses."""
    import relevance as rel
    apply(buyer_diversity, trade_roots)
    rel.GATE_MODE = 'evidence'
    gate = rel.Gate(data_dir)
    tenders, awards, lots, texts, raw, docfreq = evd.load_world(data_dir)
    all_keys = [k for k in texts if k in gate.by_key]
    by_pub = {}
    for k in all_keys:
        by_pub.setdefault(raw[k][3], []).append(k)

    counts = Counter()
    misses, lexicons = [], {}
    for case in load_cases():
        firm, expect = case['firm'], case['expect']
        refs = [k for k in evd.firm_profile_texts(awards, texts, firm)
                if k in gate.by_key]
        sel = [k for k in by_pub.get(case['pub'], [])
               if case.get('title_contains', '') in str(raw[k][0])]
        if not sel:
            counts[(expect, 'missing')] += 1
            continue
        for k in sel:
            spec = {'sub_id': 'lexicon-receipt', 'version': 0, 'name': firm,
                    'profile_refs': [gate.rows[gate.by_key[r]]
                                     ['publication_number']
                                     for r in refs if r != k],
                    'min_relevance': rel.DEFAULT_MIN_RELEVANCE}
            if not spec['profile_refs']:
                counts[(expect, 'missing')] += 1
                continue
            profile = rel.build_profile(gate, spec)
            if want_lexicons:
                lexicons.setdefault(firm, sorted(profile['keywords'] or []))
            ok, *_ = rel.judge(gate, profile, {
                'procedure_id': k[0], 'lot_id': k[1],
                'buyer_name': raw[k][4]})
            got = 'in' if ok else 'out'
            counts[(expect, 'ok' if got == expect else 'bad')] += 1
            if got != expect:
                misses.append((expect, str(raw[k][0])[:44], str(firm)[:26]))
    return counts, lexicons, misses


def fmt(counts):
    rows = []
    for d in ('in', 'out'):
        ok = counts[(d, 'ok')]
        tot = ok + counts[(d, 'bad')] + counts[(d, 'missing')]
        rows.append((d, ok, tot))
    total_ok = sum(r[1] for r in rows)
    total = sum(r[2] for r in rows)
    return rows, total_ok, total


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', default='data')
    ap.add_argument('--config', choices=[c[0] for c in CONFIGS], default=None,
                    help='run one configuration instead of all three')
    ap.add_argument('--lexicons', action='store_true',
                    help="print every benchmark firm's lexicon — the words "
                         'should each name a Gewerk or a material')
    ap.add_argument('--out', default=None, help='also write markdown here')
    args = ap.parse_args()

    todo = [c for c in CONFIGS if args.config in (None, c[0])]
    lines = ['| configuration | IN (should pass) | OUT (should reject) | total |',
             '|---|---|---|---|']
    last_lex, all_misses = {}, {}
    for name, bd, tr in todo:
        counts, lex, misses = run_config(args.data_dir, name, bd, tr,
                                         args.lexicons)
        rows, total_ok, total = fmt(counts)
        cells = ' | '.join(f'{ok}/{tot}' for _, ok, tot in rows)
        lines.append(f'| {name} | {cells} | {total_ok}/{total} |')
        print(f'[receipt] {name:7s} '
              + '  '.join(f'{d.upper()} {ok}/{tot}' for d, ok, tot in rows)
              + f'  total {total_ok}/{total}', flush=True)
        last_lex, all_misses[name] = lex, misses

    print()
    print('\n'.join(lines))
    if args.lexicons and last_lex:
        print('\n## Lexicons (last configuration run)\n')
        for firm, kws in sorted(last_lex.items()):
            print(f'- **{firm}** ({len(kws)}): {" ".join(kws)}')
    for name, misses in all_misses.items():
        if misses:
            print(f'\n## {name}: {len(misses)} misjudged')
            for expect, title, firm in misses[:20]:
                print(f'  [{expect:>3}] {title!r} [{firm}]')
    if args.out:
        Path(args.out).write_text('\n'.join(lines) + '\n', encoding='utf-8')
        print(f'\n[receipt] -> {args.out}')


if __name__ == '__main__':
    main()
