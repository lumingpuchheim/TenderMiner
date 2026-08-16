"""TenderMining online-learning loop — the predict -> grade -> retrain cycle.

Concept: ONLINE_LEARNING.md. Model logic: single_bidder.py (v1 notice-only
features; no buyer-derived features by design decision).

    python loop.py run --last 7d              # weekly cycle
    python loop.py run --last 6m              # first backfill
    python loop.py run --last 7d --skip-download   # offline: reuse the store

The interval is a parameter, never a constant: --last X (Nd/Nw/Nm) sets the
download window; the effective window is widened to cover everything since the
last successful run (checkpoint), so gaps self-heal and overlaps dedup.

Every run: download new notices -> rebuild the store parquets from the raw
archive -> grade past ledger predictions against newly published awards ->
retrain a candidate and gate it against the champion (tripwires + validation
PR-AUC) -> score all open lots with the champion -> append to the ledger ->
write a markdown report.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import time
import sys
from html import escape
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import config
import drift
import experiments
import grading
import housekeeping
import heavy_lock
import ledger
import predicting
import render
import selection
import single_bidder as sb
import subscriptions
import training
import util

REPO = Path(__file__).resolve().parent


# ------------------------------------------------------------- step 1: download

def download(paths, args, checkpoint):
    """bulk.py fetches the window's packages; features.py rebuilds the store
    parquets from the ENTIRE raw archive (full rebuild == growing store, since
    the archive only grows; bulk.py skips already-processed packages itself)."""
    today = util.now_utc().date()
    requested_from = today - util.parse_window(args.last)
    last_to = checkpoint.get('last_success_to')
    effective_from = requested_from
    if last_to:
        effective_from = min(requested_from, datetime.strptime(last_to, '%Y%m%d').date())
    date_from, date_to = effective_from.strftime('%Y%m%d'), today.strftime('%Y%m%d')
    print(f'[download] window {date_from}..{date_to} '
          f'(requested --last {args.last}, checkpoint {last_to or "none"})')

    subprocess.run(
        [sys.executable, str(REPO / 'bulk.py'), '--from', date_from, '--to', date_to,
         '--country', args.country, '--cpv', args.cpv, '--out-dir', str(paths.xml)],
        check=True)
    subprocess.run(
        [sys.executable, str(REPO / 'features.py'), '--xml-dir', str(paths.xml),
         '--cpv', args.cpv,
         '--tenders-out', str(paths.store_tenders),
         '--awards-out', str(paths.store_awards)],
        check=True)
    return date_to



# --------------------------------------------------------------- step 5: report

def _rate_ci(rate, ci):
    """'25 in 100 (95% CI 5-70)' — the interval is not decoration; it is the
    difference between a number you may quote and one you may not."""
    if rate is None:
        return '—'
    s = f'{rate*100:.0f} in 100'
    if ci:
        s += f' (95% CI {ci[0]*100:.0f}-{ci[1]*100:.0f})'
    return s


def flag_view_lines(record, args):
    """The precision/recall section: what the binary lonely/not-lonely call was
    worth, next to the one baseline that can beat it for free.

    Printed on every cycle that graded anything — including cycles where the
    model flagged nothing, which is itself a result and used to vanish from
    the report entirely."""
    if not record:
        return []
    f = record.get('flag')
    if not f:
        return []
    lines = ['## The flag: precision and recall '
             f'(binary view at the {args.threshold:.2f} cut-off)', '']

    thin = f['n'] < record.get('min_flag_grades', 0)
    if thin:
        lines += [f"**Too thin to read: {f['n']} graded lots against a floor of "
                  f"{record['min_flag_grades']}.** The numbers below are printed so the "
                  'series exists from day one, not because they mean anything yet — '
                  'awards publish a median 84 days after the tender, so this section '
                  'fills up roughly a quarter behind the predictions. Read the '
                  'confidence intervals, not the point estimates.', '']

    lines += [f"Over the trailing {record['window']}, {f['n']} graded lots: we called "
              f"{f['flagged']} of them lonely, and {f['positives']} really ended with "
              '0-1 bids.', '',
              '| | we said lonely | we said not | total |',
              '|---|---|---|---|',
              f"| **ended 0-1 bids** | {f['tp']} | {f['fn']} | {f['positives']} |",
              f"| **ended 2+ bids** | {f['fp']} | {f['tn']} | {f['n'] - f['positives']} |",
              f"| total | {f['flagged']} | {f['n'] - f['flagged']} | {f['n']} |", '']

    if f['flagged'] == 0:
        lines += ['We flagged nothing in this window, so precision is undefined and '
                  'recall is 0 — every single-bid lot was missed. A cut-off no lot '
                  'clears is a broken cut-off, not a cautious one.', '']
    else:
        lines += [f"- **precision** (the flags right): {_rate_ci(f['precision'], f['precision_ci'])}",
                  f"- **recall** (single-bid lots caught): {_rate_ci(f['recall'], f['recall_ci'])}",
                  f"- **F1**: {f['f1']:.2f}" if f['f1'] is not None else '- **F1**: —',
                  '']

    if f['positives'] == 0:
        # Degenerate window: with nothing to catch, precision is 0 for us AND
        # for the baseline, and comparing the two says nothing about either.
        lines += ['Not one graded lot in this window ended with 0-1 bids, so there was '
                  'nothing to catch: precision is 0 by construction and no comparison '
                  'against a baseline means anything here. Wait for a window that '
                  'contains positives.', '']
        return lines

    lines += ['Against the only free baseline — **call every lot lonely**: '
              f"precision {f['base']*100:.0f} in 100, recall 100 in 100"
              + (f", F1 {f['base_f1']:.2f}." if f['base_f1'] is not None else '.'), '']
    if f['precision'] is not None and not f['beats_base']:
        lines += [f"**The flag is not paying for itself:** its precision "
                  f"({f['precision']*100:.0f} in 100) is at or below the "
                  f"{f['base']*100:.0f} in 100 you get by flagging everything, so at this "
                  'cut-off the model is costing recall and buying nothing. '
                  + ('On this sample that is noise, not a verdict.' if thin else
                     'On this sample that is a real finding — move the cut-off or '
                     'retrain before quoting the flag to anyone.'), '']

    trades = [t for t in record.get('trades', []) if t.get('flag')]
    if trades:
        lines += [f"Per trade (trades with at least {args.min_trade_grades} graded lots):", '',
                  '| trade | graded | flags | precision | recall | flag everything |',
                  '|---|---|---|---|---|---|']
        for t in trades:
            tf = t['flag']
            prec = f"{tf['precision']*100:.0f} in 100" if tf['precision'] is not None else '—'
            rec = f"{tf['recall']*100:.0f} in 100" if tf['recall'] is not None else '—'
            lines.append(f"| {t['cpv3']} {t['name']} | {tf['n']} | {tf['flagged']} | "
                         f"{prec} | {rec} | {tf['base']*100:.0f} in 100 |")
        lines.append('')
    return lines


def report(paths, tenders, args, record, gate, drift, model_id, n_graded, n_predicted,
           trial_lines=()):
    latest_model = ledger.prediction_latest_per_lot(
        paths.ledger_home,
        exclude_models=experiments.shadow_models(paths.models, paths.ledger_home))
    open_rows = sorted(latest_model.values(), key=lambda r: -r['score'])

    info = {}
    for t in tenders.itertuples():
        info[(t.procedure_id, t.lot_id)] = t
    lines = [f'# TenderMining weekly report — {util.now_utc().date().isoformat()}', '']
    if record and record.get('top'):
        t = record['top']
        lines += ['## Verified track record (rank-based — the product view)', '',
                  f"Over the trailing {record['window']}: {record['graded']} predicted lots got their outcome. "
                  f"Of the **top {record['top_share']:.0%} of our ranking** ({t['k']} lots), "
                  f"**{t['hit']*100:.0f} in 100 ended with 0-1 bids**, vs {t['base']*100:.0f} in 100 "
                  f"across all graded lots — **lift {t['lift']:.1f}x**." if t['lift'] is not None else
                  'Top-slice lift not computable (no positives in the window).',
                  '']
        if record['trades']:
            lines += ['Per trade (trades with enough graded lots):', '']
            for tr in record['trades']:
                lines.append(f"- {tr['cpv3']} {tr['name']}: top {record['top_share']:.0%} of our ranking hit "
                             f"{tr['hit']*100:.0f} in 100, base {tr['base']*100:.0f} in 100 "
                             f"(lift {tr['lift']:.1f}x, {tr['n']} graded lots)")
            lines.append('')
        if record.get('tiers'):
            lines += ['What each tier really meant (graded outcomes per tier):', '']
            for t_ in record['tiers']:
                lines.append(f"- {t_['tier']}: {t_['hit']*100:.0f} in 100 ended with 0-1 bids "
                             f"({t_['n']} graded lots)")
            lines.append('')
    else:
        lines += ['## Verified track record', '',
                  'No graded outcomes in the window yet — grading starts as awards arrive.', '']

    lines += flag_view_lines(record, args)

    lines += [f'## This week\'s shortlist (top {args.report_top} of the ranking)', '',
              '| tier | score | deadline | est. value | title |', '|---|---|---|---|---|']
    for r in open_rows[:args.report_top]:
        t = info.get((r['procedure_id'], r['lot_id']))
        title = (str(getattr(t, 'title', ''))[:60] if t is not None else '')
        value = getattr(t, 'est_value_lot', None) if t is not None else None
        value = f'{value:,.0f}' if isinstance(value, (int, float)) and pd.notna(value) else ''
        lines.append(f"| {r.get('tier') or ''} | {r['score']:.2f} | "
                     f"{str(r.get('deadline_date'))[:10]} | {value} | {title} |")

    lines += ['', '## Health', '',
              f'- candidate model: {model_id} ({ "promoted" if gate and not gate.get("warnings") else "see warnings"})',
              f'- newly graded lots: {n_graded}',
              f'- new predictions: {n_predicted}']
    if gate:
        for name, status in gate.get('checks', {}).items():
            lines.append(f'- check {name}: {status}')
        for fmsg in gate.get('failures', []):
            lines.append(f'- TRUST CHECK FAILED: {fmsg}')
        for wmsg in gate.get('warnings', []):
            lines.append(f'- WARNING: {wmsg}')
    if drift:
        for name, status in drift['checks'].items():
            lines.append(f'- drift {name}: {status}')
        for wmsg in drift['warnings']:
            lines.append(f'- DRIFT WARNING: {wmsg}')
    if trial_lines:
        lines += ['', '## Experiments (doc/EXPERIMENTS.md)', '']
        lines += [f'- {tl}' for tl in trial_lines]

    paths.reports.mkdir(parents=True, exist_ok=True)
    out = paths.reports / f'report_{util.now_utc().date().isoformat()}.md'
    out.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'[report] {out}')
    return out


# ----------------------------------------------------------------------- main

def cmd_run(args):
    paths = util.Paths(args.data_dir, args.models_dir)
    # The cycle WAITS for the heavy-job lock rather than failing on it: a
    # replay someone started at 08:10 is over in minutes, while a skipped
    # Monday is a week with no delivery. The wait is bounded — heavy_lock
    # raises rather than hanging, and property 3 in that module is why.
    with heavy_lock.held(paths.data, 'the weekly cycle', wait=3600):
        _run_cycle(paths, args)


def _run_cycle(paths, args):
    # which state this cycle is operating on, before it operates on it
    # (doc/STORAGE.md 6.1) — a cycle that silently used the wrong root would
    # look exactly like a cycle with nothing to do
    print(f'[config] data root: {config.describe(paths.data)}')
    # and which gate rules this process resolved to (PARAMETERS.md 4.3): three
    # GateConfig knobs and twenty evidence.py ones read env vars at import, so
    # a stray variable in cron's environment would otherwise change
    # production silently. The line is the assertion; the fingerprint on it
    # is the one every delivery row of this cycle will carry.
    import relevance as rel
    print(f'[config] gate: {rel.DEFAULT_CONFIG.describe()}')
    checkpoint = util.read_json(paths.checkpoint, {})

    if args.skip_download:
        print('[download] skipped (--skip-download)')
        date_to = checkpoint.get('last_success_to')
    else:
        date_to = download(paths, args, checkpoint)

    tenders, roles = sb.load_with_roles(paths.store_tenders)
    awards, _ = sb.load_with_roles(paths.store_awards)
    data, aw, n_dropped = sb.assemble(tenders, awards)
    print(f'[store] {len(tenders)} tender rows, {len(awards)} award rows, '
          f'{data.groupby(sb.KEY).ngroups} labeled lots ({n_dropped} reporting errors dropped)')

    # ONE open of the embedding model, for both jobs that need it.
    #
    # Opening it costs ~1.2 GB whether it is then handed 278 tender texts or a
    # single word — the expense is the opening. There are exactly two jobs:
    # the lot texts (here), and the individual words that evidence tier 3
    # falls back on when neither an exact nor a typo-tolerant match hits.
    # Scoring, selection and the reports never open it; they read numbers off
    # the sidecar.
    #
    # Left apart, the second job opened it again on its own later — in
    # delivery, mid-report, for a handful of words a new week had never seen,
    # and again in the next replay. Doing it here while the model is already
    # open costs nothing extra and leaves the rest of the week clean.
    import embed
    try:
        embed.ensure_embeddings(paths.data, tenders)
        import embed_vocab
        embed_vocab.top_up(paths.data)
    except Exception as e:  # nothing reads the sidecar until RELEVANCE.md phase 3; never fail a cycle over it
        print(f'[embed] sidecar update failed: {e}')
    finally:
        # Whatever happened above, the model does not travel into grading,
        # training and delivery — none of them embed anything.
        embed.unload_model()

    # A/B arms (doc/EXPERIMENTS.md §8): with an open experiment the cycle
    # trains and scores once per arm; only the delivering arm's outputs go on
    # to the monitors, the report, delivery and the simulation. With none it
    # is the single implicit arm — exactly the cycle as it always was.
    plan = experiments.plan(paths.ledger_home, util.now_utc().date().isoformat())
    if plan.is_trial:
        print(f'[experiment] {plan.experiment.id}: arms '
              + ', '.join(f'{a.label}{" (delivering)" if plan.is_delivering(a) else " (shadow)"}'
                          for a in plan.arms))
    new_grades = grading.grade(paths, tenders, aw, args, plan)
    record = grading.track_record(paths, args)
    if not plan.is_trial:
        model_id, gate = training.learn(paths, tenders, roles, data, aw, args, checkpoint)
        rows, scores_now, scored = predicting.predict_open(paths, tenders, roles, aw, args)
    else:
        model_id = gate = None
        rows, scores_now, scored = [], np.array([]), []
        for arm in plan.arms:
            mid, g = training.learn(paths, tenders, roles, data, aw, args, checkpoint, arm=arm, plan=plan)
            r_, s_, sc_ = predicting.predict_open(paths, tenders, roles, aw, args, arm=arm, plan=plan)
            if plan.is_delivering(arm):
                model_id, gate, rows, scores_now, scored = mid, g, r_, s_, sc_
    drift_checks = drift.drift_monitors(paths, tenders, aw, scores_now, args)
    # persisted so the dashboard can show the monitors (SUBSCRIPTIONS.md phase 5)
    util.write_json(paths.drift,
                    {'at': util.now_utc().isoformat(timespec='seconds'), **drift_checks})
    trial_lines = []
    if plan.is_trial:
        row = experiments.state(paths.ledger_home)[plan.experiment.id]
        v, _ = experiments.read_verdict(paths.ledger_home, paths.models, plan.experiment,
                                        row, util.now_utc().date().isoformat())
        trial_lines.append(experiments.status_line(plan.experiment, v))
        print(f'[experiment] {trial_lines[-1]}')
    report(paths, tenders, args, record, gate, drift_checks, model_id, len(new_grades), len(rows),
           trial_lines=trial_lines)
    delivering.learn_references(paths, tenders, awards, args)
    delivering.deliver(paths, scored, args)
    import simulation
    simulation.simulate(paths.data, scored, tenders, aw,
                        max_picks=args.sim_max_picks,
                        min_deadline_days=args.sim_min_deadline_days)

    try:
        import render_dashboard
        render_dashboard.main(data_dir=paths.data, models_dir=paths.models)
    except Exception as e:  # the dashboard is a convenience; never fail the cycle over it
        print(f'[dashboard] rendering failed: {e}')

    # The public site (doc/TRADE_PAGES.md): the hand-written pages copied from
    # `site/` plus the generated trade pages, built into `<data>/public/`.
    #
    # Into the DATA directory, not back into `site/`: in the container the
    # checkout is the image, so a build writing there would be discarded with
    # the container and would break the read-only filesystem the cycle runs
    # under. This is also why the generated pages are not committed.
    #
    # Non-fatal by the same rule the dashboard gets: a week-stale market page
    # is acceptable, a missing customer report is not. Nothing uploads here:
    # the edge serves `<data>/public/current` directly, and `release` swaps it
    # all-or-nothing. A page-TEMPLATE change does not wait for Monday either —
    # docker/deploy.sh runs the same build after every deploy.
    if not args.skip_trade_pages:
        try:
            import trade_pages
            built, skipped = trade_pages.build(paths.data)
            print(f'[public] site -> {paths.data / "public"}: '
                  f'{len(built)} trade pages, {len(skipped)} trades below the '
                  f'floor of {trade_pages.MIN_AWARDED} awarded lots')
        except Exception as e:                                 # noqa: BLE001
            print(f'[public] site build failed, cycle continues: {e!r}')

    housekeeping.prune_caches(paths)

    checkpoint['last_success_at'] = util.now_utc().isoformat(timespec='seconds')
    if date_to:
        checkpoint['last_success_to'] = date_to
    util.write_json(paths.checkpoint, checkpoint)
    print('[done]')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    run = sub.add_parser('run', help='execute one full cycle')
    run.add_argument('--last', default='7d', metavar='NdNwNm',
                     help='download window: the last N days/weeks/months (default 7d)')
    run.add_argument('--cpv', default='45', help='CPV scope (default 45 = construction)')
    run.add_argument('--country', default='DEU', help='buyer country (default DEU)')
    run.add_argument('--threshold', type=float, default=0.5, help='flagging cut-off')
    run.add_argument('--val-window', default='8w', dest='val_window',
                     help='validation window for the promotion gate (default 8w)')
    run.add_argument('--track-window', default='12w', dest='track_window',
                     help='track-record reporting window (default 12w)')
    run.add_argument('--min-val-lots', type=int, default=30, dest='min_val_lots',
                     help='minimum lots in the validation window to run the gate')
    run.add_argument('--min-shuffle-positives', type=int, default=20, dest='min_shuffle_positives',
                     help='minimum positive val lots for the shuffled-label check to be meaningful')
    run.add_argument('--top-slice', type=float, default=0.2, dest='top_slice',
                     help='share of the ranking counted as "our picks" in rank-based metrics (default 0.2)')
    run.add_argument('--tier-high', type=float, default=0.10, dest='tier_high',
                     help='share of the weekly ranking tiered HIGH (default 0.10)')
    run.add_argument('--tier-medium', type=float, default=0.20, dest='tier_medium',
                     help='share of the weekly ranking tiered MEDIUM, after HIGH (default 0.20)')
    run.add_argument('--min-trade-grades', type=int, default=25, dest='min_trade_grades',
                     help='minimum graded lots per trade before its track record is reported')
    run.add_argument('--min-flag-grades', type=int, default=30, dest='min_flag_grades',
                     help='graded lots below which the precision/recall section says so '
                          'out loud before quoting itself (default 30)')
    run.add_argument('--min-slice-grades', type=int, default=25, dest='min_slice_grades',
                     help='minimum graded lots in a subscription slice before its own '
                          'track record is quoted (below: the fallback ladder speaks)')
    run.add_argument('--promote-epsilon', type=float, default=0.005, dest='promote_epsilon',
                     help='candidate may trail the champion by this much and still promote')
    run.add_argument('--drift-window', default='4w', dest='drift_window',
                     help='"recent" window for the drift monitors (default 4w)')
    run.add_argument('--drift-min-lots', type=int, default=30, dest='drift_min_lots',
                     help='minimum rows on each side before a drift monitor speaks (default 30)')
    run.add_argument('--missing-jump', type=float, default=0.15, dest='missing_jump',
                     help='null-rate change that counts as missingness drift (default 0.15)')
    run.add_argument('--psi-warn', type=float, default=0.25, dest='psi_warn',
                     help='PSI above which the score distribution has drifted (default 0.25)')
    run.add_argument('--iterations', type=int, default=None,
                     help='CatBoost iterations override (testing)')
    run.add_argument('--report-top', type=int, default=30, dest='report_top',
                     help='open lots listed in the report')
    run.add_argument('--sim-max-picks', type=int, default=5, dest='sim_max_picks',
                     help='picks per simulated winner company per cycle (default 5)')
    run.add_argument('--sim-min-deadline-days', type=int, default=14,
                     dest='sim_min_deadline_days',
                     help='deadline floor for simulated picks, like the product default')
    run.add_argument('--data-dir', default=config.data_root(), dest='data_dir')
    run.add_argument('--models-dir', default=config.models_root(), dest='models_dir')
    run.add_argument('--skip-download', action='store_true', dest='skip_download',
                     help='reuse the existing store (offline run)')
    run.add_argument('--skip-trade-pages', action='store_true',
                     dest='skip_trade_pages',
                     help='do not rebuild site/gewerke/ (doc/TRADE_PAGES.md)')
    run.set_defaults(func=cmd_run)
    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
