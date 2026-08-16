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
import experiments
import grading
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


# --------------------------------------------------- step 4b: deliver to subs

# The lot's identity — the key of every per-lot side table in deliver(), and
# of selection.py's `judged`, which is where it now lives: the two must agree
# or a verdict lands next to the wrong customer's pick.
_lot_key = selection.lot_key
# the HTML helpers moved to render.py with the renderers that use
# them (REFACTOR.md phase 4b); the operator report below still calls
# them, so they are imported rather than re-implemented
clean_cell = render.clean_cell
date_de = render.date_de
html_page = render.html_page
table_html = render.table_html
receipt_html = render.receipt_html


def record_gate_config(paths, config):
    """Append this configuration to the gate-config registry the first time
    its fingerprint is seen, so the stamp on a delivery row is resolvable
    from the data directory alone — not from git archaeology over whichever
    commit was deployed that week. Append-only, one line per configuration,
    like every other ledger here.

    Scoped to the DELIVERY ledger, not to the data dir: preview_report.py and
    rewind_report.py redirect deliveries into a sandbox while still reading the real
    store, and a sandbox experiment must not append a configuration to the
    record of what customers were actually served under."""
    home = paths.deliveries_home
    if any(r.get('fingerprint') == config.fingerprint
           for r in ledger.read(home, 'gate_configs')):
        return False
    ledger.append(home, 'gate_configs',
                  [{'fingerprint': config.fingerprint,
                    'first_seen': util.now_utc().isoformat(timespec='seconds'),
                    **config.as_dict()}])
    print(f'[deliver] new gate configuration recorded: {config.describe()}')
    return True


def learn_references(paths, tenders, awards, args):
    """Step 4c (RELEVANCE.md phase 9): a customer's own wins become profile
    references — including the ones this gate rejected, which are the
    false negatives worth seeing. Derived data: appended to
    data/ledger/learned_refs.jsonl, never to the subscription file, so
    subscription versions keep meaning "the operator decided something".
    Runs before deliver() so this cycle's report already benefits. Never
    fails a cycle — a feedback problem is not a delivery problem."""
    try:
        import feedback
        today = util.now_utc().date().isoformat()
        subs = subscriptions.load(paths.subs_home, today)
        if not subs:
            return []

        def gate_factory():
            import relevance as rel
            return rel.Gate(paths.data, as_of=today)

        return feedback.learn(paths.data, subs, awards, tenders, today,
                              gate_factory=gate_factory)
    except Exception as e:
        print(f'[learn] skipped ({e})')
        return []


def deliver(paths, scored, args):
    """The dispatcher: one run, many views. Filter this cycle's scored open
    lots per subscription, re-rank and re-tier WITHIN the slice, write the
    customer's report, append delivery-ledger rows (the frozen record of what
    this customer actually saw). Never a model call, never a store join."""
    today = util.now_utc().date()
    subs = subscriptions.load(paths.subs_home, today.isoformat())
    if not subs:
        print('[deliver] no active subscriptions — skipped')
        return 0
    # latest revision per lot: a customer sees each lot once, as last published
    latest = {}
    for row in scored:
        key = (row['procedure_id'], row['lot_id'])
        if key not in latest or str(row['publication_date']) >= str(latest[key]['publication_date']):
            latest[key] = row
    past = ledger.read(paths.deliveries_home, 'deliveries')
    already = {(d['sub_id'], d['procedure_id'], d['lot_id'], str(d['ts'])[:10])
               for d in past}
    by_sub = {}
    for d in past:
        by_sub.setdefault(d['sub_id'], []).append(d)
    cutoff = (util.now_utc() - util.parse_window(args.track_window)).date().isoformat()
    grades_recent = [g for g in ledger.read(paths.ledger_home, 'grades')
                     if str(g['award_pub'])[:10] >= cutoff]
    # receipt fallback for delivery rows written before title/buyer were stamped
    pred_info = ledger.prediction_titles(paths.ledger_home)
    ts = util.now_utc().isoformat(timespec='seconds')
    # relevance gate (RELEVANCE.md phase 3): loaded once per cycle, only when a
    # subscription asks for it; unavailable sidecars degrade to ungated delivery
    # with a loud line, never a failed cycle
    gate = None
    try:
        import relevance as rel
        if any(rel.wants_gate(s) for s in subs):
            # as_of=today unions each customer's learned references
            # (feedback.py); without it a profile is the subscription line
            # alone — see relevance.Gate
            gate = rel.Gate(paths.data, as_of=today.isoformat())
            # the rules this cycle judges under, on the record before any
            # verdict is written (REFACTOR.md phase 3)
            record_gate_config(paths, gate.config)
    except Exception as e:
        print(f'[deliver] relevance gate unavailable ({e}) — delivering ungated')
        gate = None
    n_rows = 0
    for sub in subs:
        profile = None
        if gate is not None and rel.wants_gate(sub):
            try:
                profile = rel.build_profile(gate, sub)
                # a profile with no lexicon and no core root cannot pass ANY
                # lot — the customer gets an empty report every cycle and
                # nothing says why. Say why.
                mute = rel.mute_reason(profile, gate.config)
                if mute:
                    print(f"[deliver] {sub['sub_id']}: ** MUTE PROFILE ** "
                          f'{mute}')
            except Exception as e:
                print(f"[deliver] {sub['sub_id']}: profile error ({e}) — "
                      f'delivering ungated')
        # slice -> gate -> rank -> cap, in selection.py because the all-lots
        # rewind runs the same four steps and must run THESE (REFACTOR.md
        # phase 4). The gate sees the widest candidate set — deadline ignored,
        # since the annex needs a verdict for short-deadline lots too — and
        # near-misses render separately.
        sel = selection.for_sub(sub, latest.values(), today, gate=gate,
                                profile=profile)
        # render.py turns the SliceResult into the two documents and the
        # delivery rows (REFACTOR.md phase 4b). This loop keeps only the
        # dispatch and the writing: everything above is "what does this
        # customer get", everything below is "where does it go".
        receipts = render.receipt_html(grades_recent,
                                       by_sub.get(sub['sub_id'], []), pred_info)
        page, deliveries = render.customer_report(
            sub, sel, today=today, profile=profile, receipts=receipts,
            tier_high=args.tier_high, tier_medium=args.tier_medium,
            ts=ts, already=already)
        annex_name, annex = render.market_annex(
            sub, sel, today=today, profile=profile, top_slice=args.top_slice)
        out = paths.reports / 'subscriptions' / sub['sub_id'] / f'report_{today.isoformat()}.html'
        out.parent.mkdir(parents=True, exist_ok=True)
        (out.parent / annex_name).write_text(annex, encoding='utf-8')
        if page is not None:
            out.write_text(page, encoding='utf-8')
        else:
            # nothing to recommend and nothing graded to look back on -> no
            # report this cycle (decision 2026-08-06); the annex above is
            # still written as the operator's lookup
            print(f"[deliver] {sub['sub_id']}: nothing to report — "
                  f'no report written')
        ledger.append(paths.deliveries_home, 'deliveries', deliveries)
        n_rows += len(deliveries)
        print(f"[deliver] {sub['sub_id']}: {len(sel.picks)} lots delivered "
              f'({len(sel.ranked)} matched, {len(deliveries)} new delivery rows)')
    return n_rows


# ------------------------------------------------------- housekeeping

def _prune_scratch_world(paths, max_age_days):
    """Delete stale as-of scratch worlds once nothing in them has been
    touched for `max_age_days`. -> (files, bytes).

    The rewind programs rebuild these directories from the real store on
    every run (`asof.py`) — a filtered copy of the parquet store plus a full
    copy of the embeddings, entirely reconstructible, and nothing reads them
    between runs. At 203.8 MB apiece they were the second largest thing
    under `data/` after the notice archive. Swept per subdirectory: each
    world under `data/asof/` ages on its own clock, so a fresh rewind never
    protects a stale one. The three pre-phase-5 homes are swept by the same
    rule until they stop existing on operator machines.

    Age is the safety catch, not a policy: a rewind in progress has fresh
    files, so a sweep cannot pull the floor out from under a half-hour run.
    """
    asof_root = paths.data / 'asof'
    worlds = ([d for d in asof_root.iterdir() if d.is_dir()]
              if asof_root.exists() else [])
    worlds += [paths.data / n for n in ('backtest_world', 'playback_asof',
                                        'replay_asof')]
    n_total, freed_total = 0, 0
    for world in worlds:
        if not world.exists():
            continue
        files = [f for f in world.rglob('*') if f.is_file()]
        if not files:
            continue
        newest = max(f.stat().st_mtime for f in files)
        if newest >= time.time() - max_age_days * 86400:
            continue
        freed_total += sum(f.stat().st_size for f in files)
        n_total += len(files)
        shutil.rmtree(world, ignore_errors=True)
    return n_total, freed_total


def prune_caches(paths, max_age_days=30):
    """Delete discovery caches older than `max_age_days`.

    The TED search resume cache is keyed by a hash of the query, and a query
    names a date window — so a cache is unresumable the day after its window
    passes. Nothing removed them and the directory reached 1.13 GB across 1,132
    dead scopes, all written within a fortnight. It is not the weekly cycle that
    creates them (bulk.py borrows only helpers from download.py, never
    search_all), but the cycle is the only thing that runs regularly, so it is
    where the sweeping belongs.

    Safe by construction: these are derived files. The notices are in the raw
    archive and the parquet store, and the worst case is re-querying a scope
    that happens to be repeated. Never fails a cycle.
    """
    try:
        import download
        n, freed = download.prune_discovery(max_age_days)
        if n:
            print(f'[prune] {n} stale discovery cache file(s), '
                  f'freed {freed / 1e6:.1f} MB')
        wn, wfreed = _prune_scratch_world(paths, max_age_days)
        if wn:
            print(f'[prune] as-of scratch worlds untouched for '
                  f'{max_age_days}d, freed {wfreed / 1e6:.1f} MB ({wn} files)')
        return n + wn
    except Exception as e:
        print(f'[prune] skipped ({e})')
        return 0


# ------------------------------------------------------------- drift monitors


def _psi(hist, now, bins=10):
    """Population stability index of `now` vs `hist`, on hist's quantile bins.
    ~0 = same shape; >0.25 is the conventional 'population has shifted' mark."""
    edges = np.unique(np.quantile(hist, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return None  # hist scores nearly constant — no meaningful histogram
    edges[0], edges[-1] = -np.inf, np.inf
    p = np.histogram(hist, bins=edges)[0] / len(hist)
    q = np.histogram(now, bins=edges)[0] / len(now)
    p, q = np.clip(p, 1e-4, None), np.clip(q, 1e-4, None)
    return float(np.sum((q - p) * np.log(q / p)))


def drift_monitors(paths, tenders, aw, scores_now, args):
    """The four every-cycle drift monitors from ONLINE_LEARNING.md — pure
    reads that WARN in the report footer, never block promotion. They are what
    says "the market moved" before the track record sours.

    Recent = the trailing --drift-window; each monitor skips itself (and says
    so) when either side has too few rows to mean anything."""
    checks, warnings = {}, []
    cutoff = util.now_utc() - util.parse_window(args.drift_window)

    def result(name, status, detail):
        checks[name] = f'{status} ({detail})'
        if status == 'WARN':
            warnings.append(f'{name}: {detail}')

    # base-rate drift: single-bid rate of recently awarded lots vs the band of
    # monthly rates over history (mean ± max(2σ, 0.02) across qualifying months)
    award_pub = pd.to_datetime(aw['publication_date'], errors='coerce')
    recent_mask = award_pub >= pd.Timestamp(cutoff.date())
    hist_lots, recent_lots = aw[~recent_mask], aw[recent_mask]
    monthly = hist_lots.groupby(award_pub[~recent_mask].dt.to_period('M'))['label'] \
        .agg(['mean', 'size'])
    monthly = monthly[monthly['size'] >= args.drift_min_lots]
    if len(monthly) < 3 or len(recent_lots) < args.drift_min_lots:
        result('base_rate', 'skipped',
               f'{len(monthly)} qualifying months, {len(recent_lots)} recent awards — too little history')
    else:
        mid, half = monthly['mean'].mean(), max(2 * monthly['mean'].std(), 0.02)
        rate = recent_lots['label'].mean()
        detail = (f'single-bid rate {rate:.3f} vs historical band '
                  f'{mid - half:.3f}..{mid + half:.3f} over {len(monthly)} months')
        result('base_rate', 'ok' if mid - half <= rate <= mid + half else 'WARN', detail)

    # missingness drift: a notice field's null-rate jumping means the source
    # schema changed under us — compare recent notices vs all earlier ones
    tender_pub = pd.to_datetime(tenders['publication_date'], errors='coerce')
    t_recent = tenders[tender_pub >= pd.Timestamp(cutoff.date())]
    t_hist = tenders[tender_pub < pd.Timestamp(cutoff.date())]
    if len(t_recent) < args.drift_min_lots or len(t_hist) < args.drift_min_lots:
        result('missingness', 'skipped',
               f'{len(t_recent)} recent / {len(t_hist)} historical notices — too few rows')
    else:
        jumps = (t_recent.isna().mean() - t_hist.isna().mean()).abs().sort_values(ascending=False)
        moved = jumps[jumps > args.missing_jump]
        if moved.empty:
            result('missingness', 'ok',
                   f'max null-rate change {jumps.iloc[0]:.2f} ({jumps.index[0]}), '
                   f'threshold {args.missing_jump:.2f}')
        else:
            top = ', '.join(f'{c} {t_hist[c].isna().mean():.2f}->{t_recent[c].isna().mean():.2f}'
                            for c in moved.index[:4])
            result('missingness', 'WARN', f'{len(moved)} column(s) jumped: {top}')

    # award-latency drift: median tender→award gap shifting stretches (or
    # shortens) how long predictions stay ungraded — the report should say so
    first_pub = tender_pub.groupby([tenders[k] for k in sb.KEY]).min() \
        .rename('first_pub').reset_index()
    joined = aw[sb.KEY].assign(award_pub=award_pub.to_numpy()) \
        .merge(first_pub, on=sb.KEY, how='left')
    gap_days = (joined['award_pub'] - joined['first_pub']).dt.days
    g_recent = gap_days[recent_mask.to_numpy()].dropna()
    g_hist = gap_days[~recent_mask.to_numpy()].dropna()
    if len(g_recent) < args.drift_min_lots or len(g_hist) < args.drift_min_lots:
        result('award_latency', 'skipped',
               f'{len(g_recent)} recent / {len(g_hist)} historical gaps — too few awards')
    else:
        med_r, med_h = float(g_recent.median()), float(g_hist.median())
        # material = a shift a human would call one: ≥14 days AND ≥25% of the norm
        material = max(14.0, 0.25 * med_h)
        detail = f'median gap {med_r:.0f}d recently vs {med_h:.0f}d historically'
        result('award_latency', 'WARN' if abs(med_r - med_h) >= material else 'ok', detail)

    # score-distribution drift: this cycle's scores vs the trailing month of
    # ledger scores (before this run) — a shifted histogram means the open-lot
    # population or the champion's view of it moved
    ledger_cut = (util.now_utc() - timedelta(days=35)).isoformat(timespec='seconds')
    # the trailing window is a WHERE clause, not a filter over the whole ledger.
    # This runs AFTER predict_open has appended, so the window now includes this
    # cycle's own rows -- which it did not when the caller snapshotted the file
    # beforehand. Excluded explicitly, so the comparison stays "this cycle
    # against the month before it".
    hist_scores = np.array([
        s for s in ledger.prediction_scores_since(
            paths.ledger_home, ledger_cut,
            exclude_models=experiments.shadow_models(paths.models, paths.ledger_home))])
    if len(scores_now) and len(hist_scores) > len(scores_now):
        hist_scores = hist_scores[:-len(scores_now)]
    if len(scores_now) < args.drift_min_lots or len(hist_scores) < args.drift_min_lots:
        result('score_distribution', 'skipped',
               f'{len(scores_now)} scores this cycle / {len(hist_scores)} in trailing month — too few')
    else:
        psi = _psi(hist_scores, np.asarray(scores_now))
        if psi is None:
            result('score_distribution', 'skipped', 'trailing-month scores nearly constant')
        else:
            result('score_distribution', 'WARN' if psi >= args.psi_warn else 'ok',
                   f'PSI {psi:.3f} vs trailing month ({len(hist_scores)} ledger scores), '
                   f'warn at {args.psi_warn:.2f}')

    for name, status in checks.items():
        print(f'[drift] {name}: {status}')
    return {'checks': checks, 'warnings': warnings}


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
    drift = drift_monitors(paths, tenders, aw, scores_now, args)
    # persisted so the dashboard can show the monitors (SUBSCRIPTIONS.md phase 5)
    util.write_json(paths.drift, {'at': util.now_utc().isoformat(timespec='seconds'), **drift})
    trial_lines = []
    if plan.is_trial:
        row = experiments.state(paths.ledger_home)[plan.experiment.id]
        v, _ = experiments.read_verdict(paths.ledger_home, paths.models, plan.experiment,
                                        row, util.now_utc().date().isoformat())
        trial_lines.append(experiments.status_line(plan.experiment, v))
        print(f'[experiment] {trial_lines[-1]}')
    report(paths, tenders, args, record, gate, drift, model_id, len(new_grades), len(rows),
           trial_lines=trial_lines)
    learn_references(paths, tenders, awards, args)
    deliver(paths, scored, args)
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

    prune_caches(paths)

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
