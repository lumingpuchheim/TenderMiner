"""Explain the relevance gate for one subscription — why a lot is in or out.

Prints the profile's fingerprint (hard trade labels from trusted codes on the
customer's wins; soft labels inferred from reference texts), then for every
lot under the given TED publication numbers: the gate's verdict with its pass
path (text / hard code / soft code / same-buyer / contract-type) and the top
text->label projections ("what trade does this text read as" — the phase-5
signal of RELEVANCE.md). With no publication numbers, the subscription's own
profile references are explained — a sanity check that the profile reads as
the customer's trade. Read-only.

Usage:
    python explain.py --sub jebsen-blitzschutz 00490813-2026 00510556-2025
    python explain.py --sub jebsen-blitzschutz
"""

import argparse
import sys

import numpy as np

import loop
import relevance as rel
from calibrate import is_deep

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')


def explain_lot(gate, profile, i):
    r = gate.rows[i]
    title = str(gate.all_title[i] or '')[:48]
    buyer = str(gate.all_buyer[i] or '')[:28]
    row = {'procedure_id': r['procedure_id'], 'lot_id': r['lot_id'],
           'buyer_name': gate.all_buyer[i]}
    ok, near, tx, cd, why, ch = rel.judge(gate, profile, row)

    # pass-path bookkeeping, mirroring judge()'s ladder
    ctype = gate.all_ctype[i]
    type_out = (profile['ref_types'] and isinstance(ctype, str) and ctype
                and ctype not in profile['ref_types'])
    same_buyer = rel._norm_buyer(gate.all_buyer[i]) in profile['ref_buyers']
    cand = [gate.lpos[c] for c in rel._codes_of(gate, i)
            if is_deep(c) and c in gate.lpos]
    c_hard = c_soft = 0.0
    hard_l = soft_l = ''
    for cr in cand:
        if len(profile['hard_rows']):
            s = gate.lmat[profile['hard_rows']] @ gate.lmat[cr]
            j = int(np.argmax(s))
            if s[j] > c_hard:
                c_hard = float(s[j])
                hard_l = gate.lrows[profile['hard_rows'][j]]['label_de']
        if len(profile['soft_rows']):
            s = gate.lmat[profile['soft_rows']] @ gate.lmat[cr]
            j = int(np.argmax(s))
            if s[j] > c_soft:
                c_soft = float(s[j])
                soft_l = gate.lrows[profile['soft_rows'][j]]['label_de']
    path = []
    if type_out:
        path.append('TYPE->borderline')
    if same_buyer:
        path.append('SAME-BUYER (text abstains)')
    if tx is not None and tx >= profile['min_relevance']:
        path.append(f'TEXT pass {tx:.3f}')
    if c_hard:
        word = 'HARD pass' if c_hard >= profile['min_code_hard'] else 'hard'
        path.append(f'{word} {c_hard:.3f} [{hard_l[:30]}]')
    if c_soft:
        word = 'SOFT pass' if c_soft >= profile['min_code_soft'] else 'soft'
        path.append(f'{word} {c_soft:.3f} [{soft_l[:30]}]')

    # what trade does this text read as (candidate text vs all CPV labels)
    sims = gate.lmat @ gate.mat[i]
    top = np.argsort(-sims)[:3]
    proj = '; '.join(f"{gate.lrows[t]['label_de'][:38]} {sims[t]:.3f}"
                     for t in top)
    if len(profile['hard_rows']):
        tread, world = rel.trade_read(gate, profile, i)
        corr = rel.corroborated(gate, profile, i)
        path.append(f'trade-read {tread:.3f}/{world:.3f} '
                    f'{"corroborated" if corr else "NOT corroborated"} '
                    f'({rel.TRADE_READ_FORM}@{rel.TRADE_READ_PARAM})')
    print(f'{title!r} | {buyer}')
    print(f'   verdict ok={ok} borderline={near} '
          f'text={tx if tx is None else round(tx, 3)}'
          f' | {" / ".join(path) or "no channel"}')
    print(f'   reads-as: {proj}')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pubs', nargs='*', metavar='PUBLICATION_NUMBER',
                    help='TED publication numbers to explain '
                         '(default: the profile references)')
    ap.add_argument('--sub', required=True, help='subscription id')
    ap.add_argument('--data-dir', default='data')
    args = ap.parse_args()

    today = loop.now_utc().date().isoformat()
    subs = loop.load_subscriptions(f'{args.data_dir}/subscriptions.jsonl', today)
    sub = next((s for s in subs if s['sub_id'] == args.sub), None)
    if sub is None:
        sys.exit(f'[explain] no active subscription {args.sub!r}')
    if not rel.wants_gate(sub):
        sys.exit(f'[explain] subscription {args.sub!r} is ungated '
                 '(no min_relevance / profile)')
    gate = rel.Gate(args.data_dir)
    profile = rel.build_profile(gate, sub)

    print(f"profile: {sub.get('name', args.sub)} "
          f"(v{sub.get('version')}, min_relevance {profile['min_relevance']})")
    print('hard labels (trusted codes on the wins):')
    for r in profile['hard_rows']:
        print(f"   {gate.lrows[r]['code']}  {gate.lrows[r]['label_de']}")
    print(f'soft labels (inferred from reference texts, floor '
          f'{rel.SOFT_FLOOR}/k{rel.SOFT_CONSENSUS}):')
    R = gate.lmat[profile['soft_rows']] @ profile['ref_matrix'].T
    for j, r in enumerate(profile['soft_rows']):
        print(f"   {gate.lrows[r]['code']}  {gate.lrows[r]['label_de']}"
              f'  (best ref sim {R[j].max():.3f})')
    print()

    pubs = args.pubs or sub.get('profile_refs') or []
    for pub in pubs:
        rows = [i for i, r in enumerate(gate.rows)
                if r['publication_number'] == pub]
        if not rows:
            print(f'{pub}: not in the embedding sidecar')
            continue
        for i in rows:
            explain_lot(gate, profile, i)


if __name__ == '__main__':
    main()
