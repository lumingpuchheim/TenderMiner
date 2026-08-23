"""Every verdict that changes when the trusted-codes list is replaced.

The receipt that gates the artifact swap (pipeline/gate-per-trade.md 4). The
serving list was calibrated when the store held construction only; the
candidate is calibrated per division over the widened store. Both are read
by the same gate code, over the same lots, with the same profiles — the only
difference is the file.

Why the flips are readable rather than a rate: the text channel cannot move
(a lot's embedding is a function of its own text, so no lot's score changes
because other lots joined the store), so every flip is the code channel
changing its mind, and there should be few enough to read. Any flip on a
lot the paying customer was shown is a change the operator sees before it
ships, as a title, not a percentage.

    python receipt_gate_flips.py --serving trusted_codes_X.json \\
                                 --candidate calib2/trusted_codes_X.json \\
                                 --data-dir /data

Prints to the console; writes nothing. Exits 1 if any construction benchmark
case flips to the WRONG answer — that is a regression, not a change.
"""

import argparse
import json
import sys
from pathlib import Path


def load_cases(path, divisions):
    cases = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        if line.strip() and not line.startswith('#'):
            cases.append(json.loads(line))
    return cases


def judge_all(data_dir, trust_path, cases, divisions):
    """{(pub, firm, title_contains): (verdict, title, division)} under one
    trusted-codes file. One Gate per file — the list is read at construction."""
    import relevance as rel
    import evidence

    cfg = rel.DEFAULT_CONFIG.replace(trusted_codes=Path(trust_path))
    gate = rel.Gate(data_dir, config=cfg)
    tenders, awards, lots, texts, raw, docfreq = evidence.load_world(data_dir)

    def resolvable(firm):
        keys = evidence.firm_profile_texts(awards, texts, firm)
        return [k for k in keys if k in gate.by_key]

    def pub_of(k):
        return gate.rows[gate.by_key[k]]['publication_number']

    all_keys = [k for k in texts if k in gate.by_key]
    out = {}
    for case in cases:
        firm = case['firm']
        refs = resolvable(firm)
        sel = [k for k in all_keys if raw[k][3] == case['pub']
               and case.get('title_contains', '') in str(raw[k][0])]
        for k in sel:
            div = str(raw[k][2] or '')[:2]
            if divisions and div not in divisions:
                continue
            use = [r for r in refs if r != k]
            profile = rel.build_profile(gate, {
                'sub_id': 'flip-receipt', 'version': 0, 'name': firm,
                'profile_refs': [pub_of(r) for r in use],
                'min_relevance': rel.DEFAULT_MIN_RELEVANCE})
            ok, *_ = rel.judge(gate, profile, {
                'procedure_id': k[0], 'lot_id': k[1],
                'buyer_name': raw[k][4]}, config=cfg)
            key = (case['pub'], firm, case.get('title_contains', ''))
            out[key] = ('in' if ok else 'out', str(raw[k][0]), div,
                        case['expect'])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data-dir', default='data')
    ap.add_argument('--serving', required=True,
                    help='the trusted-codes file in service today')
    ap.add_argument('--candidate', required=True,
                    help='the trusted-codes file proposed to replace it')
    ap.add_argument('--benchmark', default='benchmark_relevance.jsonl')
    ap.add_argument('--divisions', default='45',
                    help='comma-separated CPV divisions to judge '
                         '(default 45: the trades with paying customers)')
    args = ap.parse_args()
    divisions = {d for d in args.divisions.split(',') if d}

    cases = load_cases(args.benchmark, divisions)
    print(f'[flips] {len(cases)} benchmark cases; divisions '
          f'{sorted(divisions) or "all"}')
    for name, path in (('serving', args.serving), ('candidate', args.candidate)):
        t = json.loads(Path(path).read_text(encoding='utf-8'))
        n = sum(1 for v in t['codes'].values() if v['trusted'])
        print(f'[flips] {name:9s} {Path(path).name}: {n} trusted codes, '
              f'generated {t.get("generated")}')

    before = judge_all(args.data_dir, args.serving, cases, divisions)
    after = judge_all(args.data_dir, args.candidate, cases, divisions)

    flips = [(k, before[k], after[k]) for k in sorted(before)
             if k in after and before[k][0] != after[k][0]]
    print(f'\n[flips] judged {len(before)} lots; {len(flips)} verdicts change')

    worse = []
    for (pub, firm, tc), (v0, title, div, expect), (v1, _, _, _) in flips:
        # a flip TOWARD the operator's reading is a fix, away from it a
        # regression; both are printed, only regressions fail the receipt
        verdict = ('fixed' if v1 == expect else
                   'REGRESSION' if v0 == expect else 'changed')
        if verdict == 'REGRESSION':
            worse.append((pub, firm, title))
        print(f'  {verdict:11s} {v0}->{v1} (operator says {expect})  '
              f'{firm[:28]:28s}  {title[:60]}')
        print(f'              {pub}')

    fixed = sum(1 for _, (v0, _, _, e), (v1, _, _, _) in flips if v1 == e)
    print(f'\n[flips] {fixed} fixed, {len(worse)} regressions, '
          f'{len(flips) - fixed - len(worse)} changed with no operator reading')
    if worse:
        print('[flips] RECEIPT FAILS — the candidate list breaks cases the '
              'serving list gets right. Read them above before swapping.')
        return 1
    print('[flips] receipt passes: no case the operator has read gets worse.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
