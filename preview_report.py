"""TenderMining tryout — render one customer's report from the last cycle's
scores, with subscription overrides, in a disposable sandbox.

Answers "what would customer X's report look like if <field> were <value>?"
in seconds: no download, no retraining, no writes outside the sandbox. The
real data directory is read read-only (store, embeddings, predictions,
grades, the customer's subscription line); everything the renderer writes
(report, annex, delivery rows) lands under data/tryout/<sub_id>/, recreated
on every run. The real delivery ledger and reports are never touched.

Usage:
    python preview_report.py --sub jebsen-blitzschutz
    python preview_report.py --sub jebsen-blitzschutz --set min_deadline_days=0
    python preview_report.py --sub jebsen-blitzschutz --set min_relevance=0.6 --keep-expired
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

import pandas as pd

import config
import ledger
import loop
import subscriptions
from util import Paths, read_jsonl
import util
import training
import delivering

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--sub', required=True, help='subscription id to render')
    ap.add_argument('--data-dir', default=config.data_root())
    ap.add_argument('--models-dir', default=config.models_root())
    ap.add_argument('--set', action='append', default=[], metavar='FIELD=VALUE',
                    dest='overrides',
                    help='override a subscription field for this render only '
                         '(VALUE parsed as JSON when possible; repeatable)')
    ap.add_argument('--keep-expired', action='store_true',
                    help='also render lots whose deadline has already passed '
                         '(awarded lots are always dropped)')
    args = ap.parse_args()

    today = util.now_utc().date()
    paths = Paths(args.data_dir, args.models_dir)
    base = subscriptions.one(paths.subs_home, today.isoformat(), args.sub)
    if base is None:
        sys.exit(f'[preview_report] no active subscription {args.sub!r} in '
                 f'{paths.subs_home}')

    fields = {}
    for kv in args.overrides:
        key, sep, value = kv.partition('=')
        if not sep:
            sys.exit(f'[preview_report] --set expects FIELD=VALUE, got {kv!r}')
        try:
            fields[key] = json.loads(value)
        except json.JSONDecodeError:
            fields[key] = value
    # a tryout version is sandbox-only and never appended to the real file;
    # bumping keeps the "newest version speaks" rule meaningful in stamps
    fields['version'] = int(base.get('version', 1)) + 1
    fields['effective_from'] = today.isoformat()
    try:
        sub = subscriptions.override(base, **fields)
    except subscriptions.SubscriptionError as e:
        # a misspelled --set used to render the unchanged report, which looks
        # exactly like "that setting made no difference"
        sys.exit(f'[preview_report] {e}')

    sandbox = paths.data / 'tryout' / args.sub
    if sandbox.exists():
        shutil.rmtree(sandbox)
    subscriptions.write_sandbox(sandbox, [sub])

    # redirect ONLY what deliver writes into the sandbox; reads (store,
    # embeddings via paths.data, predictions, grades) stay on the real dir
    paths.subs_home = sandbox
    paths.deliveries_home = ledger.start(sandbox)
    paths.reports = sandbox / 'reports'

    champ = training.current_champion(paths)
    if champ is None:
        sys.exit('[preview_report] no champion model — run a loop cycle first')
    scored = [r for r in ledger.read(args.data_dir, 'predictions')
              if r['model'] == champ['model_id']]
    if not scored:
        sys.exit(f"[preview_report] no ledger rows for champion {champ['model_id']}")

    awards = pd.read_parquet(Path(args.data_dir) / 'store' / 'awards.parquet')
    awarded = set(zip(awards['procedure_id'], awards['lot_id']))
    scored = [r for r in scored
              if (r['procedure_id'], r['lot_id']) not in awarded]
    if not args.keep_expired:
        deadline = pd.to_datetime([r.get('deadline_date') for r in scored],
                                  errors='coerce')
        scored = [r for r, d in zip(scored, deadline)
                  if pd.isna(d) or d.date() >= today]
    print(f"[preview_report] {len(scored)} open scored lots (champion "
          f"{champ['model_id']}), overrides: {args.overrides or 'none'}")

    render_args = argparse.Namespace(track_window='12w', top_slice=0.2,
                                     tier_high=0.10, tier_medium=0.20,
                                     min_slice_grades=25,
                                     mail=False)   # a tryout mails nobody
    delivering.deliver(paths, scored, render_args)

    by_key = {(r['procedure_id'], r['lot_id']): r for r in scored}
    rows = ledger.read(paths.deliveries_home, 'deliveries')
    for kind, label in (('pick', 'PICKS'), ('avoid', 'WARNINGS')):
        sel = [r for r in rows if r.get('kind') == kind]
        if not sel:
            continue
        print(f'\n{label}:')
        for r in sel:
            s = by_key.get((r['procedure_id'], r['lot_id']), {})
            gate = (f" text={r['relevance_score']:.3f} code={r['code_relevance']:.3f}"
                    if r.get('relevance_score') is not None else '')
            print(f"  [{r['slice_tier']}] score={r['score']:.3f}{gate} "
                  f"deadline={s.get('deadline_date')} | "
                  f"{str(r.get('title'))[:55]!r} | "
                  f"{str(r.get('buyer_name'))[:35]}")
    report = paths.reports / 'subscriptions' / args.sub / f'report_{today.isoformat()}.html'
    if report.exists():
        print(f'\n[preview_report] report: {report}')
    else:
        print('\n[preview_report] no report written (nothing to recommend, nothing '
              'graded to look back on)')


if __name__ == '__main__':
    main()
