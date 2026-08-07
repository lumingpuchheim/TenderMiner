"""TenderMining lexicon receipt â€” is a customer's lexicon made of trade
words, and do the hand-labeled cases still judge correctly?

Answers the question the aggregate receipts cannot: `evidence.py --sweep`
scores against synthetic off-class negatives, and its benchmark total hides
direction â€” the committed cases are recall-heavy, so a change that stops
wrong-trade picks can look like a regression in the total while doing
exactly what it was built for. This receipt therefore

  1. runs benchmark_relevance.jsonl through the REAL judge() under each
     lexicon configuration, and reports IN (should pass -> recall) and OUT
     (should be rejected -> precision) SEPARATELY, and
  2. prints the lexicons themselves, because the operator's test for a
     lexicon is reading it: every word should name a Gewerk or a material.

Configurations are the two committed switches, so this is also the A/B for
rolling either back â€” and 'roots only' is the arm that answers whether the
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
    """Set the switches and drop the memoised dictionaries â€” they are
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


def firm_lexicons(data_dir, trade_roots):
    """Every firm with >= MIN_WINS wins -> its three lists under this
    vocabulary setting: the narrow lexicon that CONVICTS, plus the core and
    wide root lists. Vocabulary work shows up here and nowhere else: a trade
    whose words are missing produces starved lexicons, which the 122
    hand-labeled cases cannot see because they cover a handful of firms.

    -> {firm: {'wins', 'codes', 'narrow', 'core', 'wide'}}"""
    from calibrate import firm_win_rows, lot_codes
    from embed import read_cpv_labels
    apply(evd.BUYER_DIVERSITY, trade_roots)
    tenders, awards, lots, texts, raw, docfreq = evd.load_world(data_dir)
    labels = read_cpv_labels()
    trust = json.loads(Path(f'trusted_codes_{__import__("embed").MODEL_TAG}'
                            '.json').read_text(encoding='utf-8'))
    trusted = {c for c, v in trust['codes'].items() if v['trusted']}
    dicts = evd.trade_dictionaries(tenders, trusted, docfreq,
                                   Path(data_dir) / 'trade_dicts.json')
    wins = firm_win_rows(awards, tenders)
    wins = wins[[k in texts
                 for k in zip(wins['procedure_id'], wins['lot_id'])]]
    wins = wins.drop_duplicates(subset=['winner_names'] + evd.KEY)
    out = {}
    for firm, g in wins.groupby('winner_names'):
        if len(g) < evd.MIN_WINS:
            continue
        keys = [k for k in zip(g['procedure_id'], g['lot_id']) if k in texts]
        if not keys:
            continue
        codes = [lot_codes(m, a) for m, a in zip(g['cpv_main'],
                                                 g['cpv_additional'])]
        lbl = [labels[c] for cs in codes for c in cs
               if c in trusted and c in labels]
        tc = {c for cs in codes for c in cs if c in trusted}
        refs = [(texts[k], raw[k][4]) for k in keys]
        why = {}
        out[firm] = {
            'wins': len(keys), 'codes': sorted(tc),
            'narrow': evd.firm_keywords(refs, docfreq, lbl, tc, dicts, why),
            'core': evd.core_keywords(refs),
            'wide': evd.wide_keywords(refs),
            'why': why}
    return out


def coverage(data_dir, trade_roots, lex=None):
    """The size table over firm_lexicons()."""
    import numpy as np
    lex = firm_lexicons(data_dir, trade_roots) if lex is None else lex
    a = np.array([len(v['narrow']) for v in lex.values()])
    return {'firms': len(a), 'median': int(np.median(a)),
            'mean': round(float(a.mean()), 1),
            'empty': int((a == 0).sum()), 'under3': int((a < 3).sum())}


def empty_dump(data_dir, limit=None):
    """The firms the vocabulary leaves with NO convicting lexicon, and the
    words it took from them — the only way to tell the two causes apart:
    (a) their trade is missing from cpv_trade_roots.txt, or (b) their texts
    are boilerplate carrying no trade vocabulary at all, in which case empty
    is CORRECT. The numbers cannot separate these; reading the words can.

    Also prints each firm's core/wide root lists, because an empty narrow
    lexicon is not the same as a mute profile: a core root in a lot's TITLE
    convicts on its own (phase 8o)."""
    off = firm_lexicons(data_dir, False)
    on = firm_lexicons(data_dir, True)
    rows = []
    for firm, v in on.items():
        if v['narrow']:
            continue
        had = off.get(firm, {}).get('narrow', [])
        rows.append((len(had), firm, v, had))
    rows.sort(key=lambda r: -r[0])
    print(f'[empty] {len(rows)} of {len(on)} firms have NO narrow lexicon '
          f'with the vocabulary on; {sum(1 for r in rows if r[0])} of them '
          f'had words before it')
    n_core = sum(1 for _, _, v, _ in rows if v['core'])
    print(f'[empty] {n_core} of those still carry core roots (a core root '
          f'in a TITLE convicts), {len(rows) - n_core} are fully mute')
    # the cross-tab that separates a vocabulary gap from a TRUST gap: a firm
    # with no trusted CPV code inherits no trade dictionary and no
    # definitional label words, so its own reference texts are the only
    # source its narrow lexicon has
    coded = {f for f, v in on.items() if v['codes']}
    empty_f = {f for _, f, _, _ in rows}
    print(f'[empty] trusted CPV code: {len(coded)}/{len(on)} firms overall, '
          f'{len(coded & empty_f)}/{len(rows)} of the empty ones')
    print(f'[empty]   coded   -> empty {len(coded & empty_f):4d} / '
          f'{len(coded):4d}')
    print(f'[empty]   uncoded -> empty {len(empty_f - coded):4d} / '
          f'{len(on) - len(coded):4d}')

    # The firms that were empty BEFORE the vocabulary. The roots file is not
    # their cause -- they lost every word to one of the four older filters --
    # so name the one that did it. Read from the vocabulary-OFF arm, where
    # names_trade() never fires and the attribution is unambiguous.
    born_empty = [f for n, f, _v, _h in rows if not n]
    tally, touched = Counter(), Counter()
    for f in born_empty:
        w = off.get(f, {}).get('why', {})
        tally.update(w.values())
        touched.update(set(w.values()))  # firms each filter rejected in
    print(f'\n[empty] {len(born_empty)} firms were empty BEFORE the '
          f'vocabulary. First filter that rejected each of their words:')
    for why, n in tally.most_common():
        print(f'[empty]   {why:22s} {n:6d} words   '
              f'{touched[why]:3d}/{len(born_empty)} firms')
    for had_n, firm, v, had in rows[:limit]:
        print(f'\n- {firm} ({v["wins"]} wins, codes {v["codes"]})')
        print(f'    dropped ({had_n}): {" ".join(had) if had else "-"}')
        print(f'    core:  {" ".join(v["core"]) or "-"}')
        print(f'    wide:  {" ".join(v["wide"]) or "-"}')


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
                    help="print every benchmark firm's lexicon â€” the words "
                         'should each name a Gewerk or a material')
    ap.add_argument('--out', default=None, help='also write markdown here')
    ap.add_argument('--coverage', action='store_true',
                    help='lexicon sizes over EVERY firm with >= 3 wins, '
                         'vocabulary on vs off â€” the measure for vocabulary '
                         'work, which the 122 cases cannot see')
    ap.add_argument('--empty', action='store_true',
                    help='the firms left with NO narrow lexicon, the words '
                         'the vocabulary took from them, and their core/wide '
                         'roots â€” for READING, which is the only way to tell '
                         'a missing trade from honest boilerplate')
    ap.add_argument('--limit', type=int, default=None,
                    help='(--empty) show only the first N firms')
    args = ap.parse_args()

    if args.empty:
        empty_dump(args.data_dir, args.limit)
        return

    if args.coverage:
        print(f'{"vocabulary":12s} {"firms":>6s} {"median":>7s} {"mean":>6s} '
              f'{"empty":>6s} {"<3":>5s}')
        for label, tr in (('off', False), ('on', True)):
            c = coverage(args.data_dir, tr)
            print(f'{label:12s} {c["firms"]:6d} {c["median"]:7d} '
                  f'{c["mean"]:6.1f} {c["empty"]:6d} {c["under3"]:5d}')
        return

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
