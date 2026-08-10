"""TenderMining winner simulation — SIMULATION.md.

Every winner company in the awards store is a simulated customer: market
derived from what they won (cpv3 trades x NUTS-1 regions), product-faithful
picks (flag floor, deadline floor, top N), appended to
Recorded in the `simulation` table (doc/STORAGE.md 5.4), one row per
(company, pick), deduped
per company/lot forever. No rendering, no HTML. Checked against
the grade ledger as awards publish.

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
import ledger
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
    home = Path(data_dir)
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

    seen = {(s['company'], s['procedure_id'], s['lot_id'])
            for s in ledger.read(home, 'simulations')}
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
    ledger.append(home, 'simulations', new_rows)
    print(f'[simulate] {len(new_rows)} new simulated picks for {n_companies} '
          f'winner companies ({len(profiles)} profiles, '
          f'{sum(len(b) for b in buckets.values())} eligible lots)')
    # SIMULATION.md "the gate rides along": every pick gets a relevance
    # verdict, backlog included. Instrumentation, never delivery — a broken
    # verdict pass must not cost a cycle.
    try:
        gate_verdicts(data_dir, aw)
    except Exception as e:                                # noqa: BLE001
        print(f'[simulate] gate verdicts FAILED, cycle continues: {e!r}')
    return new_rows


def gate_verdicts(data_dir, aw=None, limit=None):
    """One relevance-gate verdict per simulated pick (SIMULATION.md,
    2026-08-10). Judges every pick on record that has no verdict yet —
    the backlog deliberately included, so picks already graded by
    published awards carry verdicts from the first pass and the
    verdict x outcome join has signal before the ~90-day award lag.

    The profile is built from the company's wins exactly as onboarding
    would build it; a company with no resolvable win records
    `no_profile` rather than being skipped — the size of that bucket is
    the new-customer bootstrap rate, a number worth quoting on its own.
    """
    import relevance as rel
    home = Path(data_dir)
    sims = ledger.read(home, 'simulations')
    have = {(g['company'], g['procedure_id'], g['lot_id'])
            for g in ledger.read(home, 'simulations_gate')}
    todo = [s for s in sims
            if (s['company'], s['procedure_id'], s['lot_id']) not in have]
    if limit:
        todo = todo[:int(limit)]
    if not todo:
        print('[gate-verdicts] nothing to judge — all picks have verdicts')
        return []
    if aw is None:
        awards, _ = sb.load_with_roles(
            Path(data_dir) / 'store' / 'awards.parquet')
        _, aw, _ = sb.assemble(pd.read_parquet(
            Path(data_dir) / 'store' / 'tenders.parquet'), awards)
    gate = rel.Gate(str(home))
    wins_of = {}
    for r in aw.itertuples():
        names = getattr(r, 'winner_names', None)
        if names is None or (isinstance(names, float) and pd.isna(names)):
            continue
        for nm in ([names] if isinstance(names, str) else list(names)):
            nm = ' '.join(str(nm).split())
            if nm:
                wins_of.setdefault(nm, []).append(
                    (r.procedure_id, r.lot_id))
    ts = now_utc().isoformat(timespec='seconds')
    out, prof_cache = [], {}
    for s in todo:
        company = s['company']
        key = (s['procedure_id'], s['lot_id'])
        if key not in gate.by_key:
            out.append({'ts': ts, 'company': company,
                        'procedure_id': key[0], 'lot_id': key[1],
                        'verdict': 'not_in_sidecar', 'gate_pass': None})
            continue
        if company not in prof_cache:
            refs = sorted({gate.rows[gate.by_key[k]]['publication_number']
                           for k in wins_of.get(company, ())
                           if k in gate.by_key})
            prof_cache[company] = rel.build_profile(gate, {
                'sub_id': 'simulation', 'version': 0, 'name': company,
                'profile_refs': refs,
                'min_relevance': rel.DEFAULT_MIN_RELEVANCE}) if refs else None
        profile = prof_cache[company]
        if profile is None:
            out.append({'ts': ts, 'company': company,
                        'procedure_id': key[0], 'lot_id': key[1],
                        'verdict': 'no_profile', 'gate_pass': None})
            continue
        i = gate.by_key[key]
        ok, near, tx, cd, why, _ = rel.judge(gate, profile, {
            'procedure_id': key[0], 'lot_id': key[1],
            'buyer_name': gate.all_buyer[i]})
        out.append({'ts': ts, 'company': company,
                    'procedure_id': key[0], 'lot_id': key[1],
                    'verdict': ('admit' if ok
                                else 'borderline' if near else 'reject'),
                    'gate_pass': int(ok), 'text': float(tx),
                    'code': float(cd),
                    'why': (f'{why[0]}: {why[1]}' if why else None)})
    n = ledger.append(home, 'simulations_gate', out)
    from collections import Counter
    print(f'[gate-verdicts] {n} verdicts written '
          f'({len(prof_cache)} profiles): '
          f'{dict(Counter(r["verdict"] for r in out))}')
    return out


def verdict_outcomes(data_dir):
    """The verdict x outcome join, one row per gate verdict: how often the
    picks the gate admitted / rejected ended with 0-1 bids, and how often
    the simulated customer itself won them. One aggregation, two readers —
    `check` prints it, the dashboard renders it every cycle."""
    data = Path(data_dir)
    sims = ledger.read(data, 'simulations')
    grades = {(g['procedure_id'], g['lot_id']): g
              for g in ledger.read(data, 'grades')}
    verdicts = {(g['company'], g['procedure_id'], g['lot_id']): g['verdict']
                for g in ledger.read(data, 'simulations_gate')}
    winners = {}
    aw_path = data / 'store' / 'awards.parquet'
    if aw_path.exists():
        afull = pd.read_parquet(aw_path)
        latest = afull.sort_values('publication_date').drop_duplicates(
            subset=['procedure_id', 'lot_id'], keep='last')
        for r in latest.itertuples():
            names = getattr(r, 'winner_names', None)
            if names is not None and not (
                    isinstance(names, float) and pd.isna(names)):
                winners[(r.procedure_id, r.lot_id)] = {
                    ' '.join(str(n).split()) for n in
                    ([names] if isinstance(names, str) else list(names))}
    by_verdict = {}
    for s in sims:
        lot = (s['procedure_id'], s['lot_id'])
        if lot not in grades:
            continue
        v = verdicts.get((s['company'],) + lot, 'no_verdict')
        won = s['company'] in winners.get(lot, ())
        by_verdict.setdefault(v, []).append((grades[lot]['label'], won))
    out = []
    for v, ls in sorted(by_verdict.items()):
        out.append({'verdict': v, 'graded': len(ls),
                    'lonely_rate': sum(l for l, _ in ls) / len(ls),
                    'own_wins': sum(1 for _, w in ls if w),
                    'own_rate': sum(1 for _, w in ls if w) / len(ls)})
    return out


def check(data_dir, min_company_picks=3):
    """Join simulations against grades and print the market-scale answer."""
    data = Path(data_dir)
    sims = ledger.read(data, 'simulations')
    all_grades = ledger.read(data, 'grades')
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
    # SIMULATION.md "the gate rides along": the same graded picks, split by
    # what the relevance gate said. The own-win column is the business-fit
    # signal at market scale: did the simulated customer itself win the lot.
    print('\nby gate verdict (SIMULATION.md, "the gate rides along"):')
    for row in verdict_outcomes(data):
        print(f'  {row["verdict"]:13s} {row["graded"]:6d} graded   '
              f'{row["lonely_rate"]*100:3.0f} in 100 ended 0-1 bids   '
              f'own-win {row["own_wins"]} ({row["own_rate"]*100:.1f}%)')
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
    scored = [r for r in ledger.read(args.data_dir, 'predictions')
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
    gv = sub.add_parser('gate', help='judge every simulated pick that has no '
                                     'relevance verdict yet (backlog included)')
    gv.add_argument('--data-dir', default=config.data_root(), dest='data_dir')
    gv.add_argument('--limit', type=int, default=None,
                    help='cap this pass (smoke test); reruns pick up the rest')
    gv.set_defaults(func=lambda a: gate_verdicts(a.data_dir, limit=a.limit))
    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
