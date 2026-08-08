"""TenderMining winner simulation — SIMULATION.md.

Every winner company in the awards store is a simulated customer: market
derived from what they won (cpv3 trades x NUTS-1 regions), product-faithful
picks (flag floor, deadline floor, top N), appended to
data/ledger/simulations.jsonl — one JSON line per (company, pick), deduped
per company/lot forever. No rendering, no HTML. Checked against
data/ledger/grades.jsonl as awards publish.

Usage:
    python simulation.py check            # join simulations vs grades, print hit rates
    python simulation.py run              # standalone pass from the current champion's
                                          # ledger rows (the loop runs this every cycle)
"""
from __future__ import annotations

import argparse
import heapq
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import config
import single_bidder as sb

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

SECTOR = {'450': 'general construction', '451': 'site preparation', '452': 'civil engineering',
          '453': 'building installation', '454': 'finishing trades'}


def now_utc():
    return datetime.now(timezone.utc)


def read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding='utf-8').splitlines() if l.strip()]


def append_jsonl(path, rows):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('a', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + '\n')


def simulate(data_dir, scored, tenders, aw, max_picks=5, min_deadline_days=14):
    """One simulation pass over the given scored open lots. Returns new rows."""
    ledger = Path(data_dir) / 'ledger' / 'simulations.jsonl'
    today = now_utc().date()
    lot_meta = {}
    for r in tenders[sb.KEY + ['cpv_main', 'place_nuts3']].itertuples():
        lot_meta[(r.procedure_id, r.lot_id)] = (
            str(r.cpv_main)[:3] if pd.notna(r.cpv_main) else None,
            str(r.place_nuts3)[:3] if pd.notna(r.place_nuts3) else None)

    profiles = {}
    for r in aw.itertuples():
        names = getattr(r, 'winner_names', None)
        if names is None or (isinstance(names, float) and pd.isna(names)):
            continue
        cpv3, nuts1 = lot_meta.get((r.procedure_id, r.lot_id), (None, None))
        for nm in ([names] if isinstance(names, str) else list(names)):
            nm = ' '.join(str(nm).split())
            if not nm:
                continue
            p = profiles.setdefault(nm, {'cpv3': set(), 'nuts1': set()})
            if cpv3:
                p['cpv3'].add(cpv3)
            if nuts1:
                p['nuts1'].add(nuts1)

    # product-faithful candidate pool: latest revision per lot, flagged,
    # enough days to the deadline — the same floor a real subscriber gets
    latest = {}
    for row in scored:
        key = (row['procedure_id'], row['lot_id'])
        if key not in latest or str(row['publication_date']) >= str(latest[key]['publication_date']):
            latest[key] = row
    min_deadline = (today + timedelta(days=min_deadline_days)).isoformat()
    buckets = {}
    for r in latest.values():
        deadline = pd.to_datetime(r.get('deadline_date'), errors='coerce')
        if not r.get('flag') or not r.get('cpv3') or pd.isna(deadline) \
                or deadline.date().isoformat() < min_deadline:
            continue
        buckets.setdefault(r['cpv3'], []).append(r)
    for rows_ in buckets.values():
        rows_.sort(key=lambda r: -r['score'])

    seen = {(s['company'], s['procedure_id'], s['lot_id']) for s in read_jsonl(ledger)}
    ts = now_utc().isoformat(timespec='seconds')
    new_rows, n_companies = [], 0
    for company, p in profiles.items():
        merged = heapq.merge(*[buckets.get(c, []) for c in sorted(p['cpv3'])],
                             key=lambda r: -r['score'])
        picked = 0
        for r in merged:
            if picked >= max_picks:
                break
            nuts1 = str(r.get('place_nuts3') or '')[:3]
            if p['nuts1'] and (not nuts1 or nuts1 not in p['nuts1']):
                continue
            key = (company, r['procedure_id'], r['lot_id'])
            if key in seen:
                picked += 1  # already on record from an earlier cycle
                continue
            seen.add(key)
            new_rows.append({
                'ts': ts, 'company': company,
                'procedure_id': r['procedure_id'], 'lot_id': r['lot_id'],
                'notice_id': r.get('notice_id'), 'model': r['model'],
                'score': r['score'], 'cpv3': r.get('cpv3'),
                'place_nuts3': r.get('place_nuts3'),
                'publication_number': r.get('publication_number'),
                'deadline_date': r.get('deadline_date'),
            })
            picked += 1
        if picked:
            n_companies += 1
    append_jsonl(ledger, new_rows)
    print(f'[simulate] {len(new_rows)} new simulated picks for {n_companies} '
          f'winner companies ({len(profiles)} profiles, '
          f'{sum(len(b) for b in buckets.values())} eligible lots)')
    return new_rows


def check(data_dir, min_company_picks=3):
    """Join simulations against grades and print the market-scale answer."""
    data = Path(data_dir)
    sims = read_jsonl(data / 'ledger' / 'simulations.jsonl')
    all_grades = read_jsonl(data / 'ledger' / 'grades.jsonl')
    grades = {(g['procedure_id'], g['lot_id']): g for g in all_grades}
    if not sims:
        print('no simulation rows yet — run a cycle (or: python simulation.py run)')
        return
    graded = [(s, grades[(s['procedure_id'], s['lot_id'])]) for s in sims
              if (s['procedure_id'], s['lot_id']) in grades]
    companies = {s['company'] for s in sims}
    print(f'{len(sims)} simulated picks · {len(companies)} companies · '
          f'{len(graded)} picks graded so far')
    if not graded:
        print('no graded picks yet — outcomes arrive with the award notices '
              '(~90-day median lag)')
        return
    hit = sum(g['label'] for _, g in graded) / len(graded)
    base = (sum(g['label'] for g in all_grades) / len(all_grades)) if all_grades else 0
    print(f'picks that ended with 0-1 bids: {hit*100:.0f} in 100 '
          f'(graded market base rate: {base*100:.0f} in 100)')
    by_trade = {}
    for s, g in graded:
        by_trade.setdefault(s.get('cpv3'), []).append(g['label'])
    for cpv3, labels in sorted(by_trade.items()):
        print(f'  {cpv3} {SECTOR.get(cpv3, ""):24s} {len(labels):5d} graded  '
              f'{sum(labels)/len(labels)*100:3.0f} in 100')
    by_company = {}
    for s, g in graded:
        by_company.setdefault(s['company'], []).append(g['label'])
    enough = {c: ls for c, ls in by_company.items() if len(ls) >= min_company_picks}
    if enough:
        rates = sorted(sum(ls) / len(ls) for ls in enough.values())
        majority = sum(1 for r in rates if r > 0.5)
        print(f'companies with >= {min_company_picks} graded picks: {len(enough)} · '
              f'median hit rate {rates[len(rates)//2]*100:.0f} in 100 · '
              f'majority-right for {majority} ({majority/len(enough)*100:.0f}%)')
    else:
        print(f'no company has {min_company_picks}+ graded picks yet')


def cmd_run(args):
    """Standalone pass: rebuild the frame the loop would hand over — the
    current champion's frozen ledger rows — and simulate from those."""
    tenders, _ = sb.load_with_roles(Path(args.data_dir) / 'store' / 'tenders.parquet')
    awards, _ = sb.load_with_roles(Path(args.data_dir) / 'store' / 'awards.parquet')
    _, aw, _ = sb.assemble(tenders, awards)
    current = Path(args.models_dir) / 'CURRENT'
    if not current.exists():
        raise SystemExit('no champion model — run the loop first')
    champ = current.read_text(encoding='utf-8').strip()
    scored = [r for r in read_jsonl(Path(args.data_dir) / 'ledger' / 'predictions.jsonl')
              if r['model'] == champ]
    print(f'champion {champ}, {len(scored)} scored ledger rows')
    simulate(args.data_dir, scored, tenders, aw,
             max_picks=args.max_picks, min_deadline_days=args.min_deadline_days)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    chk = sub.add_parser('check', help='join simulations vs grades, print hit rates')
    chk.add_argument('--min-company-picks', type=int, default=3, dest='min_company_picks')
    chk.add_argument('--data-dir', default=config.data_root(), dest='data_dir')
    chk.set_defaults(func=lambda a: check(a.data_dir, a.min_company_picks))
    run = sub.add_parser('run', help='standalone simulation pass from the champion ledger rows')
    run.add_argument('--max-picks', type=int, default=5, dest='max_picks')
    run.add_argument('--min-deadline-days', type=int, default=14, dest='min_deadline_days')
    run.add_argument('--data-dir', default=config.data_root(), dest='data_dir')
    run.add_argument('--models-dir', default='models', dest='models_dir')
    run.set_defaults(func=cmd_run)
    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
