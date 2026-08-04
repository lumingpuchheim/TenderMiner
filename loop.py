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
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import single_bidder as sb

REPO = Path(__file__).resolve().parent

SECTOR = {'450': 'general construction', '451': 'site preparation', '452': 'civil engineering',
          '453': 'building installation', '454': 'finishing trades'}


# ------------------------------------------------------------------ small utils

def parse_window(spec):
    """'7d' / '2w' / '3m' -> timedelta (months approximated as 30 days)."""
    m = re.fullmatch(r'(\d+)([dwm])', spec.strip().lower())
    if not m:
        raise SystemExit(f"--last '{spec}' is not of the form 7d / 2w / 3m")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(days=n * {'d': 1, 'w': 7, 'm': 30}[unit])


def now_utc():
    return datetime.now(timezone.utc)


def read_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding='utf-8'))


def write_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, default=str), encoding='utf-8')


def read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding='utf-8').splitlines() if line.strip()]


def clean_cell(v, width):
    """Markdown-table-safe cell: collapse all whitespace (newlines break table
    rows), replace pipes (they split cells), truncate."""
    return ' '.join(str(v).split()).replace('|', '/')[:width]


def stamp(v):
    """NaN/NaT -> None so ledger rows carry JSON null, never 'nan' strings."""
    try:
        return None if pd.isna(v) else v
    except (TypeError, ValueError):
        return v


def append_jsonl(path, rows):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('a', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + '\n')


class Paths:
    """All on-disk locations, rooted at --data-dir / --models-dir (parameters,
    like every window in this program)."""

    def __init__(self, data_dir, models_dir):
        self.data = Path(data_dir)
        self.xml = self.data / 'raw' / 'xml'
        self.store_tenders = self.data / 'store' / 'tenders.parquet'
        self.store_awards = self.data / 'store' / 'awards.parquet'
        self.predictions = self.data / 'ledger' / 'predictions.jsonl'
        self.grades = self.data / 'ledger' / 'grades.jsonl'
        self.deliveries = self.data / 'ledger' / 'deliveries.jsonl'
        self.subscriptions = self.data / 'subscriptions.jsonl'
        self.checkpoint = self.data / 'logs' / 'loop_checkpoint.json'
        self.drift = self.data / 'logs' / 'drift_latest.json'
        self.reports = self.data / 'reports'
        self.models = Path(models_dir)
        self.registry = self.models / 'registry.jsonl'
        self.current = self.models / 'CURRENT'


# ------------------------------------------------------------- step 1: download

def download(paths, args, checkpoint):
    """bulk.py fetches the window's packages; features.py rebuilds the store
    parquets from the ENTIRE raw archive (full rebuild == growing store, since
    the archive only grows; bulk.py skips already-processed packages itself)."""
    today = now_utc().date()
    requested_from = today - parse_window(args.last)
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


# ---------------------------------------------------------------- step 2: grade

def grade(paths, tenders, aw, args):
    """Grade ledger predictions for lots whose award has now been published.
    The headline grades the LAST prediction made before the award appeared.
    Each grade row is stamped with the slicing keys (cpv3 trade code,
    place_nuts3) and the award notice's TED publication number, at write time
    (SUBSCRIPTIONS.md: the ledger is the frozen record — a stamped row cannot
    drift, a join against a rebuilt store can)."""
    predictions = read_jsonl(paths.predictions)
    already = {(g['procedure_id'], g['lot_id']) for g in read_jsonl(paths.grades)}
    by_lot = {}
    for row in predictions:
        by_lot.setdefault((row['procedure_id'], row['lot_id']), []).append(row)

    lot_meta = {}
    for r in tenders[sb.KEY + ['cpv_main', 'place_nuts3']].itertuples():
        lot_meta[(r.procedure_id, r.lot_id)] = {
            'cpv3': str(r.cpv_main)[:3] if pd.notna(r.cpv_main) else None,
            'place_nuts3': stamp(r.place_nuts3),
        }

    labeled = {(r.procedure_id, r.lot_id):
               (int(r.label), str(r.publication_date),
                stamp(getattr(r, 'publication_number', None)),
                int(r.n_tenders))
               for r in aw.itertuples()}
    new_grades = []
    for lot, rows in by_lot.items():
        if lot in already or lot not in labeled:
            continue
        label, award_pub, award_pub_nr, n_tenders = labeled[lot]
        meta = lot_meta.get(lot, {})
        rows = sorted(rows, key=lambda r: r['ts'])
        before = [r for r in rows if str(r['ts'])[:10] <= award_pub[:10]]
        last = (before or rows)[-1]
        flag = bool(last['score'] >= last.get('threshold', args.threshold))
        new_grades.append({
            'graded_at': now_utc().isoformat(timespec='seconds'),
            'procedure_id': lot[0], 'lot_id': lot[1],
            'label': label, 'n_tenders': n_tenders, 'award_pub': award_pub,
            'award_publication_number': award_pub_nr,
            'cpv3': meta.get('cpv3'), 'place_nuts3': meta.get('place_nuts3'),
            'score': last['score'], 'tier': last.get('tier'), 'flag': flag,
            'correct': flag == bool(label), 'model': last['model'],
        })
    append_jsonl(paths.grades, new_grades)
    print(f'[grade] {len(new_grades)} newly graded lots '
          f'({len(predictions)} ledger rows, {len(already)} previously graded)')
    return new_grades


def _top_slice_stats(rows, share):
    """Hit rate of the top `share` of rows by score, vs the rows' base rate."""
    if not rows:
        return None
    base = sum(g['label'] for g in rows) / len(rows)
    k = max(1, round(len(rows) * share))
    top = sorted(rows, key=lambda g: -g['score'])[:k]
    hit = sum(g['label'] for g in top) / len(top)
    return {'n': len(rows), 'k': k, 'base': base, 'hit': hit,
            'lift': (hit / base) if base > 0 else None}


def track_record(paths, args):
    """Rolling verified performance over the track window (a parameter).

    The headline is RANK-based — 'did the top of our ranking end lonely more
    often than the rest?' — because the product action is picking the most
    attractive tenders, not applying a score threshold. The flag-based numbers
    are kept as a secondary view."""
    grades = read_jsonl(paths.grades)
    if not grades:
        return None
    cutoff = (now_utc() - parse_window(args.track_window)).date().isoformat()
    recent = [g for g in grades if str(g['award_pub'])[:10] >= cutoff]
    if not recent:
        return None
    flagged = [g for g in recent if g['flag']]
    positives = [g for g in recent if g['label'] == 1]

    by_trade = {}
    for g in recent:
        if g.get('cpv3'):
            by_trade.setdefault(g['cpv3'], []).append(g)
    trades = []
    for cpv3, rows in sorted(by_trade.items()):
        if len(rows) < args.min_trade_grades:
            continue
        s = _top_slice_stats(rows, args.top_slice)
        trades.append({'cpv3': cpv3, 'name': SECTOR.get(cpv3, ''), **s})

    tiers = []
    for tier in ('HIGH', 'MEDIUM', 'LOW'):
        rows = [g for g in recent if g.get('tier') == tier]
        if rows:
            tiers.append({'tier': tier, 'n': len(rows),
                          'hit': sum(g['label'] for g in rows) / len(rows)})

    return {
        'window': args.track_window,
        'graded': len(recent),
        'base_rate': sum(g['label'] for g in recent) / len(recent),
        'top': _top_slice_stats(recent, args.top_slice),
        'top_share': args.top_slice,
        'trades': trades,
        'tiers': tiers,
        'flags': len(flagged),
        'flags_right': (sum(g['label'] for g in flagged) / len(flagged)) if flagged else None,
        'coverage': (sum(1 for g in positives if g['flag']) / len(positives)) if positives else None,
    }


# ------------------------------------------------------- step 3: learn + promote

def current_champion(paths):
    if not paths.current.exists():
        return None
    model_id = paths.current.read_text(encoding='utf-8').strip()
    meta = read_json(paths.models / model_id / 'meta.json', None)
    return {'model_id': model_id, 'meta': meta}


def learn(paths, tenders, roles, data, aw, args, checkpoint):
    """Train a candidate on v1 notice-only features, run the trust checks, gate
    against the champion, persist to the registry. Returns (model_id, gate).

    A failed trust check BLOCKS PROMOTION and keeps the champion — it never
    aborts the cycle (ONLINE_LEARNING.md: "blocks keep the champion and
    notify; nothing fails silently")."""
    X, cat_cols, num_cols, _ = sb.build_features(data, roles, list_frame=tenders)
    features = cat_cols + num_cols
    gate = {'val_window': args.val_window, 'checks': {}, 'warnings': [], 'failures': []}

    try:
        card = sb.assert_pure_one_hot(X, cat_cols)
        gate['checks']['pure_one_hot'] = f'passed (max cardinality {int(card.max())})'
    except AssertionError as e:
        # cannot train a trustworthy candidate at all — keep champion, skip training
        gate['failures'].append(f'pure_one_hot: {e}')
        print(f'[learn] TRUST CHECK FAILED: {e} — no candidate this cycle, champion kept')
        return None, gate

    pub = pd.to_datetime(data['publication_date'])
    val_threshold = pub.max() - parse_window(args.val_window)
    split = sb.temporal_split(data, X, threshold=val_threshold)
    gate['val_threshold'] = str(val_threshold.date())
    gate['n_val_lots'] = split.n_test_lots

    overrides = {'iterations': args.iterations} if args.iterations else {}
    small_val = split.n_test_lots < args.min_val_lots or len(set(split.yte)) < 2
    val_metrics = None
    if small_val:
        gate['warnings'].append(
            f'validation window too small ({split.n_test_lots} lots) — gate skipped')
    else:
        eval_model = sb.train(split.Xtr, split.ytr, split.wtr, cat_cols, **overrides)
        p_val = sb.predict(eval_model, split.Xte)
        val_metrics = sb.metrics(split.yte, p_val, split.wte, threshold=args.threshold)
        # the product-shaped sorting check: hit rate of the top slice of the
        # ranking on the validation window, vs the window's base rate
        k = max(1, round(len(p_val) * args.top_slice))
        idx = np.argsort(-p_val)[:k]
        top_hit = float(np.average(split.yte[idx], weights=split.wte[idx]))
        val_metrics['top_slice_share'] = args.top_slice
        val_metrics['top_slice_hit'] = top_hit
        val_metrics['top_slice_lift'] = (top_hit / val_metrics['base_rate']
                                         if val_metrics['base_rate'] else None)
        gate['val_metrics'] = val_metrics
        # tripwire: too good to be true
        if val_metrics['roc_auc'] >= sb.TOO_GOOD_ROC:
            gate['failures'].append(
                f"too_good: val ROC-AUC {val_metrics['roc_auc']:.3f} >= {sb.TOO_GOOD_ROC} — hunt the leak")
        else:
            gate['checks']['too_good'] = f"passed (ROC-AUC {val_metrics['roc_auc']:.3f})"
        # tripwire: shuffled labels (monthly, or when never run). Median of 3
        # shuffles — a single shuffle's PR-AUC is too noisy on small windows —
        # and only when the validation window has enough positives for the
        # null distribution to be meaningful at all.
        last_shuffled = checkpoint.get('last_shuffled_check')
        n_pos_val = int((split.yte == 1).sum())
        if n_pos_val < args.min_shuffle_positives:
            gate['checks']['shuffled_label'] = (
                f'skipped ({n_pos_val} positive val lots < {args.min_shuffle_positives} — '
                'too few for a reliable null)')
        elif not last_shuffled or (now_utc().date() - datetime.strptime(last_shuffled, '%Y-%m-%d').date()).days >= 30:
            prs = []
            for seed in (42, 43, 44):
                mapping = sb.permuted_lot_labels(data, mask=split.is_train, seed=seed)
                ytr_shuf = sb.labels_from_mapping(data, split.is_train, mapping)
                shuf_model = sb.train(split.Xtr, ytr_shuf, split.wtr, cat_cols, **overrides)
                prs.append(sb.metrics(split.yte, sb.predict(shuf_model, split.Xte), split.wte)['pr_auc'])
            pr_shuf = float(np.median(prs))
            base = split.base_rate_test
            # collapse bound adapts to the observed shuffle noise on small windows
            bound = max(base * 1.5, base + 3 * float(np.std(prs)) + 0.02)
            if pr_shuf >= bound:
                gate['failures'].append(
                    f'shuffled_label: median PR-AUC {pr_shuf:.3f} did not collapse '
                    f'(bound {bound:.3f}, base {base:.3f})')
            else:
                gate['checks']['shuffled_label'] = (
                    f'passed (median PR-AUC {pr_shuf:.3f} < bound {bound:.3f}, base {base:.3f})')
                checkpoint['last_shuffled_check'] = now_utc().date().isoformat()
        else:
            gate['checks']['shuffled_label'] = f'skipped (last run {last_shuffled})'

    # tripwire: computability on open lots (production dry-run)
    open_t = sb.open_tenders(tenders, aw)
    X_open, cats_o, nums_o, _ = sb.build_features(open_t, roles, list_frame=tenders)
    if cats_o + nums_o != features:
        gate['failures'].append('computability: feature set differs for open lots — a feature depends on the future')
    else:
        gate['checks']['computability'] = f'passed ({len(open_t)} open rows)'

    # deploy model: trained on ALL labeled rows
    y = data['label'].to_numpy()
    k = data.groupby(sb.KEY)['label'].transform('size')
    w = (1.0 / k).to_numpy()
    deploy = sb.train(X, y, w, cat_cols, **overrides)

    # promotion: all trust checks passed AND match-or-beat the champion's val PR-AUC
    champ = current_champion(paths)
    if gate['failures']:
        promote = False
        gate['warnings'].append('champion kept: trust check failed')
    elif small_val:
        promote = champ is None
        if champ:
            gate['warnings'].append('champion kept: cannot compare without a usable validation window')
    elif champ is None or champ['meta'] is None or champ['meta'].get('val_pr_auc') is None:
        promote = True
    else:
        champ_pr = champ['meta']['val_pr_auc']
        promote = val_metrics['pr_auc'] >= champ_pr - args.promote_epsilon
        gate['champion'] = {'model_id': champ['model_id'], 'val_pr_auc': champ_pr}
        if not promote:
            gate['warnings'].append(
                f"candidate val PR-AUC {val_metrics['pr_auc']:.4f} < champion {champ_pr:.4f} — champion kept")

    model_id = 'm' + now_utc().strftime('%Y-%m-%d-%H%M%S')
    mdir = paths.models / model_id
    mdir.mkdir(parents=True, exist_ok=True)
    deploy.save_model(str(mdir / 'model.cbm'))
    meta = {
        'model_id': model_id,
        'trained_at': now_utc().isoformat(timespec='seconds'),
        'n_train_rows': len(data),
        'n_train_lots': int(data.groupby(sb.KEY).ngroups),
        'features': features, 'n_features': len(features),
        'max_cardinality': int(card.max()),
        'val_pr_auc': None if val_metrics is None else val_metrics['pr_auc'],
        'val_roc_auc': None if val_metrics is None else val_metrics['roc_auc'],
        'val_top_hit': None if val_metrics is None else val_metrics.get('top_slice_hit'),
        'val_top_lift': None if val_metrics is None else val_metrics.get('top_slice_lift'),
        'gate': gate, 'promoted': promote,
        'threshold': args.threshold,
    }
    write_json(mdir / 'meta.json', meta)
    append_jsonl(paths.registry, [{'model_id': model_id, 'promoted': promote,
                                   'val_pr_auc': meta['val_pr_auc'],
                                   'val_top_hit': meta['val_top_hit'],
                                   'val_top_lift': meta['val_top_lift'],
                                   'trained_at': meta['trained_at']}])
    if promote:
        paths.current.parent.mkdir(parents=True, exist_ok=True)
        paths.current.write_text(model_id + '\n', encoding='utf-8')
    lift = f" | top-{args.top_slice:.0%} val hit {meta['val_top_hit']:.2f} (lift {meta['val_top_lift']:.1f}x)" \
        if meta['val_top_hit'] is not None else ''
    print(f'[learn] candidate {model_id} '
          f"val PR-AUC {meta['val_pr_auc']}{lift} -> {'PROMOTED' if promote else 'champion kept'}")
    for wmsg in gate['warnings']:
        print(f'[learn] warning: {wmsg}')
    return model_id, gate


# ----------------------------------------------- plain-language pick reasons

# The customer boundary for model internals (SUBSCRIPTIONS.md): feature
# groups -> plain phrases; None = technical, never surfaces.
WHY_PHRASES = {
    'notice_kind': None, 'notice_subtype': None, 'quality_flags': None,
    'est_value_lot_currency': None, 'est_value_procedure_currency': None,
    'span__issue_date': None,
    'procedure_type': 'the procedure used', 'accelerated': 'the procedure used',
    'award_criterion_kind': 'how bids are scored', 'price_weight_pct': 'how bids are scored',
    'contract_type': 'the kind of contract',
    'buyer_legal_type': 'the kind of buyer', 'buyer_activity': 'the kind of buyer',
    'buyer_is_cpb_awarding': 'the kind of buyer', 'buyer_is_cpb_acquiring': 'the kind of buyer',
    'service_provider_type': 'the kind of buyer', 'buyer_country': 'where the buyer sits',
    'eu_funded': 'the funding setup', 'funding_programs': 'the funding setup',
    'duration_source': 'the contract duration', 'duration_unit_raw': 'the contract duration',
    'duration_measure_raw': 'the contract duration', 'duration_days': 'the contract duration',
    'span__period_start': 'the contract duration', 'span__period_end': 'the contract duration',
    'est_value_lot': 'the contract size', 'est_value_procedure': 'the contract size',
    'selection_criteria_types': 'the qualification requirements',
    'n_selection_criteria': 'the qualification requirements',
    'n_criteria_suitability': 'the qualification requirements',
    'n_criteria_financial': 'the qualification requirements',
    'n_criteria_technical': 'the qualification requirements',
    'n_criteria_other': 'the qualification requirements',
    'bid_bond_required': 'the bid-bond requirement',
    'bid_validity_unit': 'the required bid validity',
    'bid_validity_days': 'the required bid validity',
    'bid_validity_raw': 'the required bid validity',
    'exclusion_grounds': 'the exclusion rules', 'n_exclusion_grounds': 'the exclusion rules',
    'cv_required': 'the CV requirements', 'legal_form_required': 'the required legal form',
    'security_clearance_required': 'the security-clearance requirement',
    'docs_restricted': 'restricted tender documents',
    'variants': 'the bid-format rules', 'multiple_bids': 'the bid-format rules',
    'esubmission': 'the submission method', 'sme_suitable': 'its fit for smaller firms',
    'framework_type': 'the framework setup', 'is_framework': 'the framework setup',
    'procurement_additional_types': 'special procurement conditions',
    'is_strategic': 'special procurement conditions',
    'procedure_languages': 'the language requirements',
    'gpa_covered': 'international-agreement coverage',
    'recurring': 'it recurs regularly', 'eauction': 'the e-auction format',
    'is_corrigendum': 'its correction history', 'change_reasons': 'its correction history',
    'n_corrections_so_far': 'its correction history',
    'platform_name': 'the procurement platform used',
    'deadline_days': 'the bidding timeline', 'deadline_days_published': 'the bidding timeline',
    'span__deadline_date': 'the bidding timeline',
    'span__bid_opening_date': 'the procedural timetable',
    'span__question_deadline_date': 'the procedural timetable',
    'question_window_days': 'the procedural timetable',
    'opening_lag_days': 'the procedural timetable',
    'n_lots': 'how the project is split into lots',
    'n_doc_references': 'the documentation volume',
}
WHY_PREFIXES = [
    (('cpv_main__', 'cpv_additional__'), 'the specialised type of work'),
    (('place_nuts3__', 'place_postal_zone__'), 'the location — fewer firms bid there'),
    (('buyer_nuts__', 'buyer_postal_zone__'), 'where the buyer sits'),
]


def _why_phrase(feat):
    if feat in WHY_PHRASES:
        return WHY_PHRASES[feat]
    for prefixes, phrase in WHY_PREFIXES:
        if feat.startswith(prefixes):
            return phrase
    return None


def explain_rows(model, X, cat_cols, k=3):
    """Per-row top-k plain-language reasons the model leans lonely (positive
    SHAP) and crowded (negative), deduped through the phrase book."""
    from catboost import Pool
    shap = model.get_feature_importance(type='ShapValues',
                                        data=Pool(X, cat_features=cat_cols))
    feats = list(X.columns)

    def collect(contrib, order, sign):
        phrases = []
        for j in order:
            if sign * contrib[j] <= 0:
                break
            p = _why_phrase(feats[j])
            if p and p not in phrases:
                phrases.append(p)
            if len(phrases) == k:
                break
        return phrases

    lonely, crowded = [], []
    for row in shap:
        contrib = row[:-1]
        order = np.argsort(-contrib)
        lonely.append(collect(contrib, order, 1))
        crowded.append(collect(contrib, order[::-1], -1))
    return lonely, crowded


# -------------------------------------------------------------- step 4: predict

def predict_open(paths, tenders, roles, aw, args):
    """Score every open lot with the champion; append new rows to the ledger.
    Returns (new_ledger_rows, all_scores_this_cycle, scored) — the full score
    array feeds the score-distribution drift monitor, and `scored` (one dict
    per open lot, ledger-row shaped, dedup or not) feeds the subscription
    renderer even on cycles where dedup writes no new ledger rows."""
    champ = current_champion(paths)
    if champ is None:
        print('[predict] no champion model — nothing to score')
        return [], np.array([]), []
    from catboost import CatBoostClassifier
    model = CatBoostClassifier()
    model.load_model(str(paths.models / champ['model_id'] / 'model.cbm'))

    open_t = sb.open_tenders(tenders, aw)
    today = now_utc().date().isoformat()
    deadline = pd.to_datetime(open_t.get('deadline_date'), errors='coerce')
    open_t = open_t[(deadline.isna()) | (deadline.dt.date.astype(str) >= today)]
    if open_t.empty:
        print('[predict] no open lots')
        return [], np.array([]), []
    X_open, cats_open, _, _ = sb.build_features(open_t, roles, list_frame=tenders)
    assert list(X_open.columns) == list(model.feature_names_), \
        'store schema no longer matches the champion model — retrain required'
    scores = sb.predict(model, X_open)
    why_lonely, why_crowded = explain_rows(model, X_open, cats_open)

    # rank-based tiers ("is it a good one?"), never probabilities: HIGH = top
    # tier_high share of this batch's ranking, MEDIUM = next tier_medium share,
    # LOW = the rest. Their real-world meaning comes from the graded track
    # record ("HIGH picks ended lonely X in 100"), not from the score values.
    n = len(scores)
    ranks = np.empty(n, dtype=int)
    ranks[np.argsort(-scores)] = np.arange(n)
    n_high = max(1, round(n * args.tier_high))
    n_med = max(1, round(n * args.tier_medium))
    tiers = np.where(ranks < n_high, 'HIGH', np.where(ranks < n_high + n_med, 'MEDIUM', 'LOW'))

    seen = {(r['procedure_id'], r['lot_id'], r.get('notice_id'), r['model'])
            for r in read_jsonl(paths.predictions)}
    ts = now_utc().isoformat(timespec='seconds')
    scored, rows = [], []
    for (idx, t), score, tier, w_l, w_c in zip(open_t.iterrows(), scores, tiers,
                                               why_lonely, why_crowded):
        cpv = t.get('cpv_main')
        row = {
            'ts': ts, 'model': champ['model_id'],
            'procedure_id': t['procedure_id'], 'lot_id': t['lot_id'],
            'notice_id': t.get('notice_id'),
            'publication_date': str(t.get('publication_date')),
            'deadline_date': str(t.get('deadline_date')),
            'score': float(score), 'threshold': args.threshold,
            'flag': bool(score >= args.threshold), 'tier': str(tier),
            # slicing keys + audit link + rendering columns, stamped at write
            # time (SUBSCRIPTIONS.md) — rendering columns never feed features
            'cpv3': str(cpv)[:3] if pd.notna(cpv) else None,
            'place_nuts3': stamp(t.get('place_nuts3')),
            'publication_number': stamp(t.get('publication_number')),
            'buyer_name': stamp(t.get('buyer_name')),
            'est_value_lot': stamp(t.get('est_value_lot')),
            'title': stamp(t.get('title')),
            'why_lonely': w_l, 'why_crowded': w_c,
        }
        scored.append(row)
        key = (t['procedure_id'], t['lot_id'], t.get('notice_id'), champ['model_id'])
        if key not in seen:  # idempotent re-runs: same notice + same model scored once
            rows.append(row)
    append_jsonl(paths.predictions, rows)
    print(f'[predict] {len(rows)} new ledger rows ({len(open_t)} open rows scored, '
          f'model {champ["model_id"]})')
    return rows, scores, scored


# --------------------------------------------------- step 4b: deliver to subs

def load_subscriptions(path, today):
    """Active subscription versions in force on `today` (SUBSCRIPTIONS.md:
    versioned, never edited — the newest version with effective_from <= today
    speaks for the subscription; active: false deactivates)."""
    in_force = {}
    for row in read_jsonl(path):
        if str(row.get('effective_from', '')) > today:
            continue
        key = (str(row.get('effective_from', '')), int(row.get('version', 1)))
        cur = in_force.get(row['sub_id'])
        if cur is None or key >= cur[0]:
            in_force[row['sub_id']] = (key, row)
    return [row for _, row in in_force.values() if row.get('active', True)]


def _matches(sub, row, today, min_days):
    if sub.get('cpv_prefixes'):
        if not row['cpv3'] or not any(str(row['cpv3']).startswith(str(p))
                                      for p in sub['cpv_prefixes']):
            return False
    if sub.get('nuts_prefixes'):
        if not row['place_nuts3'] or not any(str(row['place_nuts3']).startswith(str(p))
                                             for p in sub['nuts_prefixes']):
            return False
    if min_days > 0:
        # a deadline promise cannot be honored for an unknown deadline
        deadline = pd.to_datetime(row.get('deadline_date'), errors='coerce')
        if pd.isna(deadline) or (deadline.date() - today).days < min_days:
            return False
    return True


MAX_RECEIPTS = 15  # itemized reviewed picks shown per report; the rest is counted

def receipt_lines(grades_recent, sub_deliveries, pred_info, kind='pick'):
    """Receipts before rates (SUBSCRIPTIONS.md): one line per delivered pick
    (or, kind='avoid', per warning) whose outcome has arrived — what we said,
    the bids that came, TED link as proof. Misses render exactly like hits;
    a warning is right when the lot ended contested."""
    grade_by_lot = {(g['procedure_id'], g['lot_id']): g for g in grades_recent}
    by_lot = {}
    for d in sorted(sub_deliveries, key=lambda d: str(d['ts'])):
        by_lot.setdefault((d['procedure_id'], d['lot_id']), []).append(d)
    items = []
    for lot, ds in by_lot.items():
        g = grade_by_lot.get(lot)
        if g is None:
            continue
        before = [d for d in ds if str(d['ts'])[:10] <= str(g['award_pub'])[:10]]
        d = (before or ds)[-1]
        if d.get('kind', 'pick') == kind:
            items.append((d, g))
    if not items:
        if kind == 'avoid':
            return []  # section renders only once a warning has its outcome
        n_open = sum(1 for ds in by_lot.values()
                     if ds[-1].get('kind', 'pick') == 'pick')
        return [(f'All {n_open} picks we have sent you are on the record; none has '
                 'its outcome yet — award notices trail deadlines by roughly three '
                 'months. Meanwhile, the numbers below cover the wider market.')
                if n_open else
                'Your first picks are below — each one goes on the record, and its '
                'outcome will be reviewed here when the award is published.', '']
    items.sort(key=lambda ig: str(ig[1]['award_pub']), reverse=True)
    lines = []
    for d, g in items[:MAX_RECEIPTS]:
        info = pred_info.get((g['procedure_id'], g['lot_id']), {})
        title = clean_cell(d.get('title') or info.get('title') or f"lot {g['lot_id']}", 60)
        buyer = d.get('buyer_name') or info.get('buyer_name')
        n = g.get('n_tenders')
        outcome = (f"{int(n)} bid{'s' if n != 1 else ''}" if n is not None
                   else ('0–1 bids' if g['label'] else 'more than 1 bid'))
        right = (not g['label']) if kind == 'avoid' else bool(g['label'])
        if kind == 'avoid':
            verdict = ('crowded, as we warned — bid money saved' if right
                       else 'ended quiet after all — this warning cost you a chance')
            said = 'warning'
        else:
            verdict = 'almost nobody competed' if right else 'competitive after all'
            said = 'pick'
        nr = g.get('award_publication_number')
        link = (f' · [TED {nr}](https://ted.europa.eu/en/notice/-/detail/{nr})'
                if nr else '')
        lines.append(f"- {'✓' if right else '✗'} {said}, {str(g['award_pub'])[:10]} — "
                     f"{title}{f' ({clean_cell(buyer, 40)})' if buyer else ''}: "
                     f'**{outcome}** — {verdict}{link}')
    if len(items) > MAX_RECEIPTS:
        lines.append(f'- …and {len(items) - MAX_RECEIPTS} more graded '
                     f"{'warnings' if kind == 'avoid' else 'picks'} in your "
                     'delivery ledger.')
    if kind == 'avoid':
        n_right = sum(1 for d, g in items if not g['label'])
        lines.append('')
        lines.append(f'So far {n_right} of {len(items)} warnings proved right.')
    return lines + ['']


def slice_record_lines(grades_recent, sub, args, today):
    """The customer report's track-record header: verified numbers for the
    subscription's slice, or — when the slice is too thin — the fallback
    ladder (slice -> industry Germany-wide -> whole graded market), always
    labeling the rung it stands on. Silence and unlabeled broad numbers are
    both non-options (SUBSCRIPTIONS.md)."""
    if not grades_recent:
        return [f'No graded outcomes in the trailing {args.track_window} yet — '
                'grading starts as awards arrive.', '']
    rungs = [
        ('your market', sub),
        ('your industry Germany-wide', {**sub, 'nuts_prefixes': None}),
        ('the whole graded market', {**sub, 'nuts_prefixes': None, 'cpv_prefixes': None}),
    ]
    n_slice = None
    for i, (desc, s) in enumerate(rungs):
        rows = [g for g in grades_recent if _matches(s, g, today, 0)]
        if n_slice is None:
            n_slice = len(rows)
        if len(rows) < args.min_slice_grades and not (i == 2 and rows):
            continue
        stats = _top_slice_stats(rows, args.top_slice)
        lift = f"{stats['lift']:.1f}x" if stats['lift'] is not None else '—'
        scope = 'your market' if i == 0 else desc
        explain = ([f'*What "lift {lift}" means: pick a tender blindly from {scope} and '
                    f'{stats["base"]*100:.0f} in 100 end with 0–1 bids; pick from the top '
                    f'of our ranking and {stats["hit"]*100:.0f} in 100 do. With us, your '
                    f'odds of facing (almost) no competition are {lift} better.*', '']
                   if stats['lift'] is not None else [])
        if i == 0:
            lines = [f"In your market over the trailing {args.track_window}: "
                     f"**{stats['n']}** of our predictions got their outcome. Of the "
                     f"**top {args.top_slice:.0%} of our ranking for you** ({stats['k']} lots), "
                     f"**{stats['hit']*100:.0f} in 100 ended with 0–1 bids**, vs "
                     f"{stats['base']*100:.0f} in 100 by chance — **lift {lift}**.", ''] + explain
            return lines
        return [f'Your market has {n_slice} graded outcomes so far — too few to quote '
                f'honestly. Across **{desc}** ({stats["n"]} outcomes): the top '
                f'{args.top_slice:.0%} of our ranking hit {stats["hit"]*100:.0f} in 100, '
                f'chance {stats["base"]*100:.0f} — lift {lift}.', ''] + explain
    return [f'No graded outcomes in the trailing {args.track_window} yet — '
            'grading starts as awards arrive.', '']


def deliver(paths, scored, args):
    """The dispatcher: one run, many views. Filter this cycle's scored open
    lots per subscription, re-rank and re-tier WITHIN the slice, write the
    customer's report, append delivery-ledger rows (the frozen record of what
    this customer actually saw). Never a model call, never a store join."""
    today = now_utc().date()
    subs = load_subscriptions(paths.subscriptions, today.isoformat())
    if not subs:
        print('[deliver] no active subscriptions — skipped')
        return 0
    # latest revision per lot: a customer sees each lot once, as last published
    latest = {}
    for row in scored:
        key = (row['procedure_id'], row['lot_id'])
        if key not in latest or str(row['publication_date']) >= str(latest[key]['publication_date']):
            latest[key] = row
    past = read_jsonl(paths.deliveries)
    already = {(d['sub_id'], d['procedure_id'], d['lot_id'], str(d['ts'])[:10])
               for d in past}
    by_sub = {}
    for d in past:
        by_sub.setdefault(d['sub_id'], []).append(d)
    cutoff = (now_utc() - parse_window(args.track_window)).date().isoformat()
    grades_recent = [g for g in read_jsonl(paths.grades)
                     if str(g['award_pub'])[:10] >= cutoff]
    pred_info = {}  # receipt fallback for pre-stamp delivery rows (append-only file, safe to join)
    for p in read_jsonl(paths.predictions):
        if p.get('title') or p.get('buyer_name'):
            pred_info[(p['procedure_id'], p['lot_id'])] = p
    ts = now_utc().isoformat(timespec='seconds')
    n_rows = 0
    for sub in subs:
        min_days = int(sub.get('min_deadline_days', 0) or 0)
        rows = sorted((r for r in latest.values() if _matches(sub, r, today, min_days)),
                      key=lambda r: -r['score'])
        n_high = max(1, round(len(rows) * args.tier_high))
        n_med = max(1, round(len(rows) * args.tier_medium))
        # a pick must be flagged (the quality floor); max_picks caps the list —
        # a short actionable list beats homework, and zero picks is a message
        max_picks = int(sub.get('max_picks', 5) or 0)
        top = [r for r in rows if r.get('flag')][:max_picks]

        def tender_cell(r):
            title = clean_cell(r.get('title') or f"lot {r['lot_id']}", 60)
            nr = r.get('publication_number')
            return (f'[{title}](https://ted.europa.eu/en/notice/-/detail/{nr})'
                    if nr else title)

        lines = [f"# {sub.get('name', sub['sub_id'])} — TenderMining picks — {today.isoformat()}", '',
                 'Your market: CPV ' + '|'.join(sub.get('cpv_prefixes') or ['all'])
                 + ', region ' + '|'.join(sub.get('nuts_prefixes') or ['all'])
                 + (f', ≥{min_days} days to deadline' if min_days else '') + '.', '',
                 'Every week we read every public construction tender in Germany — this '
                 f'week, **{len(rows)} in your market** — and rank them by the one '
                 'question no one can answer by reading a single notice: **how many '
                 'competitors will show up?** A **pick** is a tender where we expect few '
                 'or none. Your team still decides what you can build and what it should '
                 'cost. We add the missing piece: where bidding is worth the effort — as '
                 'in poker, knowing which hands *not* to play is where the money is. '
                 f'You get at most {max_picks} picks a week; when nothing qualifies, we '
                 'say so instead of padding the list.', '',
                 '## Your picks, reviewed', '']
        lines += receipt_lines(grades_recent, by_sub.get(sub['sub_id'], []), pred_info)
        warn_receipts = receipt_lines(grades_recent, by_sub.get(sub['sub_id'], []),
                                      pred_info, kind='avoid')
        if warn_receipts:
            lines += ['## Our warnings, reviewed', ''] + warn_receipts
        lines += ['## The numbers behind it', '']
        lines += slice_record_lines(grades_recent, sub, args, today)
        lines += ["## This week's picks", '']
        if not rows:
            lines += ['**0 open tenders matched your filters this week.**', '']
        elif not top:
            lines += [f'**No pick this week.** None of the {len(rows)} open tenders in '
                      'your market looks lonely enough to be worth your bid budget. Not '
                      'bidding into a crowd is the product working, not failing.', '']
        else:
            lines += [f'Out of {len(rows)} open tenders in your market, these '
                      f'{len(top)} look the loneliest. Click a tender to read the '
                      'official notice and get the documents; mind the deadline.', '',
                      '| deadline | buyer | tender | why we expect few bidders |',
                      '|---|---|---|---|']
        deliveries = []
        for i, r in enumerate(top):
            tier = 'HIGH' if i < n_high else ('MEDIUM' if i < n_high + n_med else 'LOW')
            lines.append(f"| {str(r.get('deadline_date'))[:10]} "
                         f"| {clean_cell(r.get('buyer_name') or '', 40)} | {tender_cell(r)} "
                         f"| {', '.join((r.get('why_lonely') or [])[:2])} |")
            if (sub['sub_id'], r['procedure_id'], r['lot_id'], ts[:10]) not in already:
                deliveries.append({
                    'ts': ts, 'sub_id': sub['sub_id'], 'sub_version': sub.get('version', 1),
                    'procedure_id': r['procedure_id'], 'lot_id': r['lot_id'],
                    'notice_id': r.get('notice_id'), 'model': r['model'],
                    'score': r['score'], 'slice_rank': i + 1,
                    'slice_size': len(rows), 'slice_tier': tier,
                    'publication_number': r.get('publication_number'),
                    'buyer_name': r.get('buyer_name'), 'title': r.get('title'),
                    'kind': 'pick',
                })
        avoid_n = int(sub.get('avoid_n', 5) or 0)
        avoid = (list(reversed(rows[-avoid_n:]))
                 if avoid_n and len(rows) > len(top) + avoid_n else [])
        if avoid:
            lines += ['', "## Don't go there this week", '',
                      f'The {len(avoid)} most contested-looking lots in your market — '
                      'our model expects a crowd. The entry fee (your bid preparation) '
                      'is the same as anywhere; the odds are not. We put these warnings '
                      'on the record and grade them when the outcome is published, '
                      'exactly like the picks.', '',
                      '| deadline | buyer | tender | why we expect a crowd |',
                      '|---|---|---|---|']
            for i, r in enumerate(avoid):
                lines.append(f"| {str(r.get('deadline_date'))[:10]} "
                             f"| {clean_cell(r.get('buyer_name') or '', 40)} | {tender_cell(r)} "
                             f"| {', '.join((r.get('why_crowded') or [])[:2])} |")
                if (sub['sub_id'], r['procedure_id'], r['lot_id'], ts[:10]) not in already:
                    deliveries.append({
                        'ts': ts, 'sub_id': sub['sub_id'], 'sub_version': sub.get('version', 1),
                        'procedure_id': r['procedure_id'], 'lot_id': r['lot_id'],
                        'notice_id': r.get('notice_id'), 'model': r['model'],
                        'score': r['score'], 'slice_rank': i + 1,
                        'slice_size': len(rows), 'slice_tier': 'AVOID',
                        'publication_number': r.get('publication_number'),
                        'buyer_name': r.get('buyer_name'), 'title': r.get('title'),
                        'kind': 'avoid',
                    })
        # the annex: every open tender in the slice (deadline filter ignored —
        # a candidate with 10 days left still deserves its verdict), so the
        # customer can check THEIR candidates, not just ours
        annex_rows = sorted((r for r in latest.values() if _matches(sub, r, today, 0)),
                            key=lambda r: -r['score'])
        n_crowd = max(1, round(len(annex_rows) * args.top_slice))
        verdicts = {}
        for rank, r in enumerate(annex_rows):
            if r.get('flag'):
                verdicts[id(r)] = ('few bidders likely', (r.get('why_lonely') or [])[:2])
            elif rank >= len(annex_rows) - n_crowd:
                verdicts[id(r)] = ('expect a crowd', (r.get('why_crowded') or [])[:2])
            else:
                verdicts[id(r)] = ('average odds', [])
        annex_name = f'annex_{today.isoformat()}.md'
        lines += ['', '## Check any tender before you bid', '',
                  f'The [annex]({annex_name}) lists **all {len(annex_rows)} open '
                  'tenders in your market** with our verdict on each. Before any '
                  'bid/no-bid decision, find your tender there — the money-saving '
                  'moment is before the calculation starts, not after. Considering '
                  'a tender outside your market filters? Reply with its TED number.']
        annex = [f"# {sub.get('name', sub['sub_id'])} — full market annex — "
                 f'{today.isoformat()}', '',
                 f'All {len(annex_rows)} open tenders in your market '
                 '(CPV ' + '|'.join(sub.get('cpv_prefixes') or ['all'])
                 + ', region ' + '|'.join(sub.get('nuts_prefixes') or ['all'])
                 + '), sorted by deadline. Look up any tender you are considering; '
                 'the verdict is the same model that produces your weekly picks, '
                 'verified in your report.', '',
                 '| verdict | deadline | buyer | tender | why |', '|---|---|---|---|---|']
        for r in sorted(annex_rows, key=lambda r: str(r.get('deadline_date'))):
            verdict, why = verdicts[id(r)]
            annex.append(f"| {verdict} | {str(r.get('deadline_date'))[:10]} "
                         f"| {clean_cell(r.get('buyer_name') or '', 40)} | {tender_cell(r)} "
                         f"| {', '.join(why)} |")
        out = paths.reports / 'subscriptions' / sub['sub_id'] / f'report_{today.isoformat()}.md'
        out.parent.mkdir(parents=True, exist_ok=True)
        (out.parent / annex_name).write_text('\n'.join(annex) + '\n', encoding='utf-8')
        out.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        append_jsonl(paths.deliveries, deliveries)
        n_rows += len(deliveries)
        print(f"[deliver] {sub['sub_id']}: {len(top)} lots delivered "
              f'({len(rows)} matched, {len(deliveries)} new delivery rows)')
    return n_rows


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


def drift_monitors(tenders, aw, prior_predictions, scores_now, args):
    """The four every-cycle drift monitors from ONLINE_LEARNING.md — pure
    reads that WARN in the report footer, never block promotion. They are what
    says "the market moved" before the track record sours.

    Recent = the trailing --drift-window; each monitor skips itself (and says
    so) when either side has too few rows to mean anything."""
    checks, warnings = {}, []
    cutoff = now_utc() - parse_window(args.drift_window)

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
    ledger_cut = (now_utc() - timedelta(days=35)).isoformat(timespec='seconds')
    hist_scores = np.array([r['score'] for r in prior_predictions if str(r['ts']) >= ledger_cut])
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

def report(paths, tenders, args, record, gate, drift, model_id, n_graded, n_predicted):
    predictions = read_jsonl(paths.predictions)
    latest_model = {}
    for r in predictions:
        latest_model[(r['procedure_id'], r['lot_id'])] = r  # last write wins (sorted by append order)
    open_rows = sorted(latest_model.values(), key=lambda r: -r['score'])

    info = {}
    for t in tenders.itertuples():
        info[(t.procedure_id, t.lot_id)] = t
    lines = [f'# TenderMining weekly report — {now_utc().date().isoformat()}', '']
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
        if record['flags']:
            fr = record['flags_right']
            lines += [f"Secondary (flag view @ cut-off): of {record['flags']} flags, {fr*100:.0f} of 100 right; "
                      f"caught {record['coverage']*100:.0f} of 100 single-bid lots." if fr is not None else '',
                      '']
    else:
        lines += ['## Verified track record', '',
                  'No graded outcomes in the window yet — grading starts as awards arrive.', '']

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

    paths.reports.mkdir(parents=True, exist_ok=True)
    out = paths.reports / f'report_{now_utc().date().isoformat()}.md'
    out.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'[report] {out}')
    return out


# ----------------------------------------------------------------------- main

def cmd_run(args):
    paths = Paths(args.data_dir, args.models_dir)
    checkpoint = read_json(paths.checkpoint, {})

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

    new_grades = grade(paths, tenders, aw, args)
    record = track_record(paths, args)
    model_id, gate = learn(paths, tenders, roles, data, aw, args, checkpoint)
    prior_predictions = read_jsonl(paths.predictions)  # snapshot before this cycle appends
    rows, scores_now, scored = predict_open(paths, tenders, roles, aw, args)
    drift = drift_monitors(tenders, aw, prior_predictions, scores_now, args)
    # persisted so the dashboard can show the monitors (SUBSCRIPTIONS.md phase 5)
    write_json(paths.drift, {'at': now_utc().isoformat(timespec='seconds'), **drift})
    report(paths, tenders, args, record, gate, drift, model_id, len(new_grades), len(rows))
    deliver(paths, scored, args)

    try:
        import render_dashboard
        render_dashboard.main(data_dir=paths.data, models_dir=paths.models)
    except Exception as e:  # the dashboard is a convenience; never fail the cycle over it
        print(f'[dashboard] rendering failed: {e}')

    checkpoint['last_success_at'] = now_utc().isoformat(timespec='seconds')
    if date_to:
        checkpoint['last_success_to'] = date_to
    write_json(paths.checkpoint, checkpoint)
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
    run.add_argument('--data-dir', default='data', dest='data_dir')
    run.add_argument('--models-dir', default='models', dest='models_dir')
    run.add_argument('--skip-download', action='store_true', dest='skip_download',
                     help='reuse the existing store (offline run)')
    run.set_defaults(func=cmd_run)
    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
