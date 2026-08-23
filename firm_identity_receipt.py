"""What firm identity does to the store, before anything uses it.

    python firm_identity_receipt.py --store data/store
    python firm_identity_receipt.py --store data/store --cpv 45   # construction only

Prints, and writes nothing:

  A. the count — how many "firms" the store holds today, how many companies
     that really is, and how many of the merges a registration number proved
     rather than a resemblance,
  B. the biggest merges, so a person can see what changed and object,
  C. the pairs a registration number REFUSED to merge although the names are
     near-identical — the short list worth reading by hand,
  D. the names that are not companies at all and would leave the prospect list.

The point of C is that it is short. The old alternative was to read 22,034
names; this is a couple of dozen.
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

import firms


def load(store, cpv=None):
    """The awards store, optionally cut to trade divisions by the tenders
    table's main CPV — the same join the trade pages use. `cpv` is a comma-
    separated list of prefixes, so the receipt can be read over exactly the
    scope the cycle sells into (45,48,72) and over construction alone."""
    store = Path(store)
    awards = pd.read_parquet(store / 'awards.parquet')
    if cpv:
        lots = pd.read_parquet(store / 'tenders.parquet',
                               columns=['procedure_id', 'lot_id', 'cpv_main'])
        lots = lots.dropna(subset=['cpv_main']).drop_duplicates(
            ['procedure_id', 'lot_id'])
        awards = awards.merge(lots, on=['procedure_id', 'lot_id'], how='left')
        wanted = tuple(c.strip() for c in str(cpv).split(',') if c.strip())
        awards = awards[awards['cpv_main'].astype(str).str.startswith(wanted)]
    return awards


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--store', default='data/store', help='folder holding awards.parquet')
    ap.add_argument('--cpv', default=None, metavar='CODES',
                    help='keep lots whose main CPV starts with any of these, '
                         'comma-separated (e.g. 45,48,72 — the scope the cycle sells)')
    ap.add_argument('--show', type=int, default=15, help='rows per section (default 15)')
    args = ap.parse_args()

    awards = load(args.store, args.cpv)
    have_ids = 'winner_national_ids' in awards.columns
    scope = f'CPV {args.cpv}' if args.cpv else 'whole store'
    print(f'# Firm identity — {args.store}, {scope}, {len(awards):,} award lots')
    if not have_ids:
        print('! this store predates the winner identity columns — names only,\n'
              '  so nothing below is proven by a registration number.\n'
              '  Rebuild with: python features.py --xml-dir data/raw/xml --all')
    print()

    everyone = firms.from_awards(awards)
    named = [f for f in everyone if firms.is_firm_name(f.name)]
    dropped = [f for f in everyone if not firms.is_firm_name(f.name)]
    clusters, blocked = firms.resolve(everyone)
    merged = [c for c in clusters if len(c.spellings) > 1]
    proven = [c for c in merged if c.proven]

    print('## A. the count')
    print(f'  winner names in the store        {len(everyone):>7,}')
    print(f'  ... not a company at all         {len(dropped):>7,}'
          f'   ({sum(f.wins for f in dropped)} wins)')
    print(f'  ... spellings of a real firm     {len(named):>7,}')
    print(f'  companies after merging          {len(clusters):>7,}')
    print(f'  names that were duplicates       {len(named) - len(clusters):>7,}'
          f'   ({sum(c.wins for c in merged):,} wins involved)')
    print(f'  merged companies                 {len(merged):>7,}'
          f'   of which a registration number proved {len(proven):,}')
    direct = [b for b in blocked if 'numbers differ' in b[2]]
    group = [b for b in blocked if b not in direct]
    # The same name with two different numbers is the case a person must judge:
    # either a clerk mistyped a digit, or two companies really do share a name.
    # Merely SIMILAR names with different numbers are the ordinary state of the
    # world (S+T Fassaden and AS Fassaden are two firms) and need nobody.
    same_name = [b for b in direct
                 if firms.core(b[0].clean) == firms.core(b[1].clean)]
    print(f'  same name, two numbers           {len(same_name):>7,}   <- the list to read')
    print(f'  similar name, two numbers        {len(direct) - len(same_name):>7,}'
          f'   (ordinary: two firms, alike names)')
    print(f'  merges refused to protect a group{len(group):>7,}'
          f'   (a spelling matched two firms at once)')
    print()

    print(f'## B. the biggest merges (top {args.show})')
    for c in sorted(merged, key=lambda c: -c.wins)[:args.show]:
        mark = 'number' if c.proven else 'name  '
        print(f'  {c.wins:5d} wins  [{mark}]  {c.name[:58]}')
        for f in c.members[1:]:
            print(f'         {f.wins:5d}   + {f.name[:64]}')
    print()

    print('## C. near-identical names the registration number kept apart')
    print('     (read these — a wrong one here is a letter to the wrong firm)')
    if not same_name:
        print('  none')
    for a, b, why in sorted(same_name, key=lambda p: -(p[0].wins + p[1].wins))[:args.show]:
        for f in (a, b):
            numbers = sorted(f.ids['vat']) or sorted(f.ids['reg'])
            shown = ', '.join(numbers[:4]) + (' …' if len(numbers) > 4 else '')
            print(f'  {f.wins:4d} {f.name[:46]:48s} {shown}')
        print()

    print('## D. names that are not companies')
    if not dropped:
        print('  none')
    for f in sorted(dropped, key=lambda f: -f.wins):
        print(f'  {f.wins:4d} wins  {f.name[:96]!r}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
