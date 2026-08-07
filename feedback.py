"""TenderMining learned references — RELEVANCE.md phase 9.

A customer's own award is ground truth that the lot was their business:
they bid on it and they won it. Revealed preference outranks anything the
gate computes, so every win becomes a profile reference — **including the
ones the gate rejected** (operator decision 2026-08-06: "if a customer
wins a tender that we believe it is not his business, we should add it").
Those are the false negatives: the recall gap made visible, one lot at a
time. They are learned like any other win and listed in the cycle output,
because a firm's own win is the only label nobody has to judge.

Learned references are DERIVED data, never operator intent:

  * they live in `data/ledger/learned_refs.jsonl`, never in
    `subscriptions.jsonl` — so a subscription version keeps meaning "the
    operator decided something", not "another week went by";
  * append-only, each row stamped with `learned_at`, so "which profile
    produced this pick" stays exactly answerable — the same contract the
    delivery ledger already depends on;
  * disposable: `python feedback.py --rebuild` regenerates the whole file
    from `awards.parquet`.

**Time isolation is fail-safe** (operator requirement 2026-08-06).
`relevance.Gate(data_dir)` — no `as_of` — loads NO learned references at
all, so every existing caller (playback.py, backtest.py, replay.py, the
receipt harnesses) keeps exactly the behaviour it has today. Only
`Gate(data_dir, as_of='YYYY-MM-DD')` unions references learned on or
before that date. Forgetting the parameter can only ever *understate* a
profile; it can never leak a future win into a historical replay, which
is the direction that would silently flatter every backtest.

Usage:
    python feedback.py --list                 # what has been learned
    python feedback.py --dry-run              # what WOULD be learned now
    python feedback.py --learn                # discover + append
    python feedback.py --rebuild              # regenerate from awards
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

import subscriptions

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

KEY = ['procedure_id', 'lot_id']
LEDGER_NAME = 'learned_refs.jsonl'
# Growth guard (RELEVANCE.md phase 9, open): profiles grow without bound
# today. A specialist with 3 wins and one with 300 derive very different
# lexicons, and derivation cost is linear in references. Capping is a
# union-time decision (keep the newest N), deliberately NOT a record-time
# one — the ledger must stay a complete history. Unbounded until a receipt
# shows growth moving leakage.


def ledger_path(data_dir):
    return Path(data_dir) / 'ledger' / LEDGER_NAME


def read_learned(data_dir):
    """All learned rows, oldest first. Missing file is not an error."""
    p = ledger_path(data_dir)
    if not p.exists():
        return []
    return [json.loads(line) for line in
            p.read_text(encoding='utf-8').splitlines() if line.strip()]


def refs_for(rows, sub_id, as_of):
    """Publication numbers learned for `sub_id` on or before `as_of`.

    `as_of` is required and never defaults to "now": a caller that cannot
    say which day it is reconstructing must not receive learned data at
    all (see the module docstring)."""
    if not as_of:
        return []
    as_of = str(as_of)
    return list(dict.fromkeys(
        r['pub'] for r in rows
        if r.get('sub_id') == sub_id and str(r.get('learned_at', '')) <= as_of))


def append_learned(data_dir, new_rows):
    """Append rows, skipping any (sub_id, pub) already recorded."""
    if not new_rows:
        return 0
    p = ledger_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    seen = {(r.get('sub_id'), r.get('pub')) for r in read_learned(data_dir)}
    fresh = [r for r in new_rows if (r['sub_id'], r['pub']) not in seen]
    if fresh:
        with p.open('a', encoding='utf-8') as f:
            for r in fresh:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
    return len(fresh)


def award_names(sub):
    """The firm's name(s) as they appear in `winner_names`. Explicit
    `award_names` wins; otherwise the display name — a fallback that is
    deliberately loud in --dry-run, because a silent mismatch means a
    customer simply never learns anything."""
    names = sub.get('award_names') or ([sub['name']] if sub.get('name') else [])
    return [str(n) for n in names]


def wins_of(awards, tenders, names):
    """-> {pub: (procedure_id, lot_id, publication_date)} for lots won by
    any of `names`. The reference is the CONTRACT NOTICE's publication
    number (RUNBOOK §2), so the award rows are joined back to the store."""
    if not names or awards is None or not len(awards):
        return {}
    want = set(names)
    hit = awards['winner_names'].apply(
        lambda x: x is not None and not isinstance(x, float)
        and bool(want.intersection(map(str, x))))
    won = awards[hit]
    if not len(won):
        return {}
    cols = [c for c in ('publication_number', 'publication_date')
            if c in tenders.columns]
    lots = tenders.drop_duplicates(subset=KEY)[KEY + cols]
    m = won[KEY].merge(lots, on=KEY, how='left')
    out = {}
    for r in m.itertuples(index=False):
        pub = getattr(r, 'publication_number', None)
        if pub is None or (isinstance(pub, float) and pd.isna(pub)):
            continue
        out[str(pub)] = (r.procedure_id, r.lot_id,
                         str(getattr(r, 'publication_date', '') or ''))
    return out


def discover(sub, awards, tenders, learned_rows, as_of):
    """Wins of this subscription's firm that are not references yet.
    -> {pub: (procedure_id, lot_id, publication_date)}"""
    known = set(sub.get('profile_refs') or [])
    known.update(refs_for(learned_rows, sub['sub_id'], as_of))
    found = wins_of(awards, tenders, award_names(sub))
    return {p: v for p, v in found.items() if p not in known}


def learn(data_dir, subs, awards, tenders, today, gate_factory=None,
          dry_run=False, verbose=True):
    """Discover and record new learned references for every subscription.

    The gate's verdict on each win is recorded as it stood BEFORE the win
    was learned — that is what makes `gate_verdict == 'out'` a truthful
    false-negative flag rather than a tautology. Building the relevance
    gate is deferred until a candidate actually exists, so an ordinary
    cycle with no new wins costs nothing.
    """
    today = str(today)
    learned_rows = read_learned(data_dir)
    candidates = {}
    for sub in subs:
        if not award_names(sub):
            if verbose:
                print(f"[learn] {sub['sub_id']}: no award_names / name — skipped")
            continue
        found = discover(sub, awards, tenders, learned_rows, today)
        if found:
            candidates[sub['sub_id']] = (sub, found)
    if not candidates:
        if verbose:
            print('[learn] no new wins to learn')
        return []

    gate = None
    if gate_factory is not None:
        try:
            gate = gate_factory()
        except Exception as e:  # a gate failure must not cost the record
            if verbose:
                print(f'[learn] gate unavailable ({e}) — verdicts unrecorded')

    import relevance as rel
    buyers = {}
    if gate is None:
        lots = tenders.drop_duplicates(subset=KEY)
        buyers = {(r.procedure_id, r.lot_id): r.buyer_name
                  for r in lots.itertuples(index=False)}

    new_rows = []
    for sub_id, (sub, found) in candidates.items():
        for pub, (pid, lid, pub_date) in sorted(found.items()):
            verdict = 'unknown'
            if gate is not None:
                if pub not in gate.by_pub:
                    if verbose:
                        print(f'[learn] {sub_id}: {pub} not in the embedding '
                              f'sidecar yet — deferred to a later cycle')
                    continue
                try:
                    verdict = _verdict(rel, gate, sub, pid, lid)
                except Exception as e:
                    if verbose:
                        print(f'[learn] {sub_id}: {pub} verdict failed ({e})')
            new_rows.append({
                'sub_id': sub_id, 'pub': pub, 'learned_at': today,
                'source': 'award', 'gate_verdict': verdict,
                'procedure_id': pid, 'lot_id': lid,
                'award_publication_date': pub_date,
                'note': ('false negative — won it, the gate rejected it'
                         if verdict == 'out' else
                         'confirmed win' if verdict == 'in' else
                         'won it; gate verdict unavailable'),
            })
    if verbose:
        misses = [r for r in new_rows if r['gate_verdict'] == 'out']
        print(f'[learn] {len(new_rows)} new reference(s) across '
              f'{len(candidates)} subscription(s); '
              f'{len(misses)} were FALSE NEGATIVES (won, gate said no)')
        for r in misses:
            print(f"   miss: {r['sub_id']} {r['pub']}")
    if not dry_run:
        n = append_learned(data_dir, new_rows)
        if verbose and n != len(new_rows):
            print(f'[learn] {len(new_rows) - n} already recorded — skipped')
    return new_rows


def _verdict(rel, gate, sub, pid, lid):
    """'in' / 'out' for one lot under the profile as it stands now."""
    if not rel.wants_gate(sub):
        return 'ungated'
    profile = rel.build_profile(gate, sub)
    i = gate.by_key.get((pid, lid))
    if i is None:
        return 'unknown'
    ok, *_ = rel.judge(gate, profile, {
        'procedure_id': pid, 'lot_id': lid,
        'buyer_name': gate.all_buyer[i]})
    return 'in' if ok else 'out'


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', default='data')
    ap.add_argument('--list', action='store_true',
                    help='print the learned-reference ledger')
    ap.add_argument('--dry-run', action='store_true',
                    help='show what would be learned, write nothing')
    ap.add_argument('--learn', action='store_true',
                    help='discover new wins and append them')
    ap.add_argument('--rebuild', action='store_true',
                    help='DELETE the ledger and regenerate it from awards')
    ap.add_argument('--as-of', default=None,
                    help='reconstruct the ledger as of this date (--list)')
    args = ap.parse_args()

    if args.list:
        rows = read_learned(args.data_dir)
        cut = args.as_of
        shown = [r for r in rows
                 if not cut or str(r.get('learned_at', '')) <= str(cut)]
        print(f'[learn] {len(shown)} learned reference(s)'
              + (f' as of {cut}' if cut else ''))
        for r in shown:
            print(f"  {r.get('learned_at')}  {r.get('sub_id'):24s} "
                  f"{r.get('pub'):16s} gate={r.get('gate_verdict')}")
        return

    if not (args.learn or args.dry_run or args.rebuild):
        ap.error('nothing to do — pass --list, --dry-run, --learn or --rebuild')

    data_dir = Path(args.data_dir)
    tenders = pd.read_parquet(data_dir / 'store' / 'tenders.parquet')
    awards = pd.read_parquet(data_dir / 'store' / 'awards.parquet')
    today = date.today().isoformat()
    subs = subscriptions.load(data_dir / 'subscriptions.jsonl', today)
    print(f'[learn] {len(subs)} active subscription(s)')

    if args.rebuild:
        p = ledger_path(data_dir)
        if p.exists():
            p.unlink()
            print(f'[learn] removed {p}')

    def gate_factory():
        import relevance as rel
        return rel.Gate(data_dir, as_of=today)

    learn(data_dir, subs, awards, tenders, today,
          gate_factory=gate_factory, dry_run=args.dry_run)


if __name__ == '__main__':
    main()
