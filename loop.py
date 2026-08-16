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

**This file is the orchestrator and nothing else** (PARAMETERS.md 9): the
paths, the checkpoint, the ordered calls, the CLI. Each step lives in the
module named for it — `grading`, `training`, `predicting`, `delivering`,
`drift`, `report`, `housekeeping`, over `util` — and `_run_cycle` below reads
as that list in order. It re-exports none of them: a caller that wants
`flag_stats` imports `grading`, not this. Step 1 stayed here because it is
two subprocess calls and a window subtraction — orchestration, not a step's
logic.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

import config
import delivering
import drift
import experiments
import grading
import heavy_lock
import housekeeping
import knobs
import predicting
import report
import single_bidder as sb
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
    # The knob protocol's two halves (PARAMETERS.md 8.3). Proposing is one
    # line per live question, printed and carried into the report; blocking is
    # the gate guard, and it skips DELIVERY only — this week's grading,
    # training and predictions are already written and are not lost to it.
    knob_lines = knobs.weekly(paths)
    for line in knob_lines:
        print(f'[knobs] {line.lstrip("- ")}')
    gate_ok, guard_lines = knobs.gate_guard(paths)
    for line in guard_lines:
        print(line)
    report.report(paths, tenders, args, record, gate, drift_checks, model_id, len(new_grades), len(rows),
           trial_lines=trial_lines, knob_lines=knob_lines + guard_lines)
    if gate_ok:
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
