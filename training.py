"""Step 3 of the cycle: train a candidate and gate it — PARAMETERS.md 9.

`learn()` builds the training frame, trains one CatBoost model, runs the
tripwires (TRAINING.md: leakage guards, the monthly shuffled-label check, the
too-good-ROC bar) and the promotion gate against the arm's own champion, then
writes the model, its `meta.json` and the registry row. `current_champion()`
resolves `models/CURRENT` — or an arm's own pointer — to that record.

Promotion is the one decision software still takes on its own here
(PARAMETERS.md 8.3 names it); which arm *delivers* is the operator's, in
`experiments.py`.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

import experiments
import single_bidder as sb
import util


def current_champion(paths, arm=None):
    """The champion: models/CURRENT, or the arm's own pointer during a trial
    (doc/EXPERIMENTS.md §4 — each arm is gated against ITS OWN champion)."""
    pointer = paths.current if arm is None else experiments.arm_current_path(paths.models, arm.id)
    if not pointer.exists():
        return None
    model_id = pointer.read_text(encoding='utf-8').strip()
    meta = util.read_json(paths.models / model_id / 'meta.json', None)
    return {'model_id': model_id, 'meta': meta}


def learn(paths, tenders, roles, data, aw, args, checkpoint, arm=None, plan=None):
    """Train a candidate on v1 notice-only features, run the trust checks, gate
    against the champion, persist to the registry. Returns (model_id, gate).

    A failed trust check BLOCKS PROMOTION and keeps the champion — it never
    aborts the cycle (ONLINE_LEARNING.md: "blocks keep the champion and
    notify; nothing fails silently").

    arm/plan (doc/EXPERIMENTS.md §8): during a trial this runs once per arm
    with the arm's feature build, CatBoost overrides and guard exemptions,
    gated against the arm's own champion; the delivering arm's promotion also
    rewrites models/CURRENT. Without an arm it is exactly the single-arm cycle."""
    feature_build = arm.feature_build if arm else sb.FEATURE_BUILD
    exempt = arm.guard_exempt if arm else ()
    tag = f'[learn:{arm.id}]' if arm else '[learn]'
    # the multi-hot vocabulary is fitted once, on the full tenders frame, and
    # travels with the candidate (meta.json) so predict_open scores with the
    # columns the model was trained on — not with whatever the archive holds
    # the week it scores (doc/EXPERIMENTS.md §3)
    multihot = sb.fit_multihot(tenders, roles, feature_build=feature_build)
    X, cat_cols, num_cols, _ = sb.build_features(data, roles, list_frame=tenders,
                                                 multihot=multihot, feature_build=feature_build)
    features = cat_cols + num_cols
    gate = {'val_window': args.val_window, 'checks': {}, 'warnings': [], 'failures': []}
    if arm:
        gate['arm'] = arm.id

    try:
        card = sb.assert_pure_one_hot(X, cat_cols, exempt=exempt)
        ctr = sb.ctr_columns(card, exempt)
        checked_max = int(card.drop(labels=[c for c in exempt if c in card.index]).max())
        gate['checks']['pure_one_hot'] = (
            f'passed (max cardinality {checked_max}'
            + (f'; CTR columns: ' + ', '.join(f'{c} {n}' for c, n in ctr.items()) if ctr else '')
            + ')')
    except AssertionError as e:
        # cannot train a trustworthy candidate at all — keep champion, skip training
        gate['failures'].append(f'pure_one_hot: {e}')
        print(f'{tag} TRUST CHECK FAILED: {e} — no candidate this cycle, champion kept')
        return None, gate

    pub = pd.to_datetime(data['publication_date'])
    val_threshold = pub.max() - util.parse_window(args.val_window)
    split = sb.temporal_split(data, X, threshold=val_threshold)
    gate['val_threshold'] = str(val_threshold.date())
    gate['n_val_lots'] = split.n_test_lots

    overrides = {'iterations': args.iterations} if args.iterations else {}
    if arm:
        overrides.update(arm.overrides)
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
        elif not last_shuffled or (util.now_utc().date() - datetime.strptime(last_shuffled, '%Y-%m-%d').date()).days >= 30:
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
                checkpoint['last_shuffled_check'] = util.now_utc().date().isoformat()
        else:
            gate['checks']['shuffled_label'] = f'skipped (last run {last_shuffled})'

    # tripwire: computability on open lots (production dry-run)
    open_t = sb.open_tenders(tenders, aw)
    X_open, cats_o, nums_o, _ = sb.build_features(open_t, roles, list_frame=tenders,
                                                  multihot=multihot, feature_build=feature_build)
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
    champ = current_champion(paths, arm)
    # A champion trained on a different feature schema cannot score today's
    # build AT ALL (predict_open compares the column list), so comparing PR-AUC
    # to it decides nothing: the candidate is the only usable model. A feature
    # change is therefore a forced, announced flag day — never a promotion rule
    # that keeps a champion the next step cannot call.
    champ_meta = (champ or {}).get('meta') or {}
    champ_features = champ_meta.get('features')
    incompatible = bool(champ and champ_features is not None
                        and list(champ_features) != list(features))
    if gate['failures']:
        promote = False
        gate['warnings'].append('champion kept: trust check failed')
        if incompatible:
            gate['warnings'].append(
                'AND the kept champion cannot score the current feature build — '
                'this cycle will not predict until the failure above is fixed')
    elif incompatible:
        promote = True
        added = [f for f in features if f not in set(champ_features)]
        dropped = [f for f in champ_features if f not in set(features)]
        gate['feature_schema_change'] = {'added': added, 'dropped': dropped,
                                         'champion': champ['model_id']}
        gate['warnings'].append(
            f"feature schema changed vs champion {champ['model_id']} "
            f'(+{len(added)}: {added or "-"}; -{len(dropped)}: {dropped or "-"}) '
            '— candidate promoted unconditionally: the champion can no longer '
            'score this build, so a PR-AUC comparison is not defined')
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

    model_id = 'm' + util.now_utc().strftime('%Y-%m-%d-%H%M%S') + (arm.suffix if arm else '')
    mdir = paths.models / model_id
    mdir.mkdir(parents=True, exist_ok=True)
    deploy.save_model(str(mdir / 'model.cbm'))
    meta = {
        'model_id': model_id,
        'trained_at': util.now_utc().isoformat(timespec='seconds'),
        'n_train_rows': len(data),
        'n_train_lots': int(data.groupby(sb.KEY).ngroups),
        'features': features, 'n_features': len(features),
        'max_cardinality': int(card.max()),
        'multihot': multihot,
        'feature_build': feature_build,
        'val_pr_auc': None if val_metrics is None else val_metrics['pr_auc'],
        'val_roc_auc': None if val_metrics is None else val_metrics['roc_auc'],
        'val_top_hit': None if val_metrics is None else val_metrics.get('top_slice_hit'),
        'val_top_lift': None if val_metrics is None else val_metrics.get('top_slice_lift'),
        'gate': gate, 'promoted': promote,
        'threshold': args.threshold,
    }
    if arm:
        meta.update({'experiment': plan.experiment.id, 'arm': arm.id, 'label': arm.label,
                     'guard_exempt': list(arm.guard_exempt), 'catboost': dict(arm.catboost)})
    util.write_json(mdir / 'meta.json', meta)
    util.append_jsonl(paths.registry, [{'model_id': model_id, 'promoted': promote,
                                   'val_pr_auc': meta['val_pr_auc'],
                                   'val_top_hit': meta['val_top_hit'],
                                   'val_top_lift': meta['val_top_lift'],
                                   'trained_at': meta['trained_at'],
                                   **({'arm': arm.id, 'experiment': plan.experiment.id} if arm else {})}])
    if promote:
        # the arm's own pointer during a trial; models/CURRENT only for the
        # delivering arm — a shadow's promotion never reaches a customer
        if arm:
            ap = experiments.arm_current_path(paths.models, arm.id)
            ap.parent.mkdir(parents=True, exist_ok=True)
            ap.write_text(model_id + '\n', encoding='utf-8')
        if arm is None or plan.is_delivering(arm):
            paths.current.parent.mkdir(parents=True, exist_ok=True)
            paths.current.write_text(model_id + '\n', encoding='utf-8')
    lift = f" | top-{args.top_slice:.0%} val hit {meta['val_top_hit']:.2f} (lift {meta['val_top_lift']:.1f}x)" \
        if meta['val_top_hit'] is not None else ''
    print(f'{tag} candidate {model_id} '
          f"val PR-AUC {meta['val_pr_auc']}{lift} -> {'PROMOTED' if promote else 'champion kept'}")
    for wmsg in gate['warnings']:
        print(f'{tag} warning: {wmsg}')
    return model_id, gate


