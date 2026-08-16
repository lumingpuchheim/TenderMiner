"""Step 4 of the cycle: score every open lot — PARAMETERS.md 9.

`predict_open()` loads the arm's champion, scores the lots whose deadline has
not passed, assigns the rank-based tiers (HIGH/MEDIUM/LOW — never
probabilities; their meaning comes from the graded track record) and appends
one prediction row per lot, stamped with the model id that made it. That
stamp is what every later comparison rests on (PARAMETERS.md 3).

The phrase book above it is the customer boundary for model internals
(SUBSCRIPTIONS.md): SHAP moves a feature, the phrase book decides whether a
human ever sees it and in what words. `None` means technical — never
surfaced.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import ledger
import single_bidder as sb
import training
import util


# The customer boundary for model internals (SUBSCRIPTIONS.md): feature
# groups -> plain phrases; None = technical, never surfaces.
WHY_PHRASES = {
    'notice_kind': None, 'notice_subtype': None, 'quality_flags': None,
    'est_value_lot_currency': None, 'est_value_procedure_currency': None,
    'span__issue_date': None,
    'procedure_type': 'das Vergabeverfahren', 'accelerated': 'das Vergabeverfahren',
    'award_criterion_kind': 'die Zuschlagskriterien', 'price_weight_pct': 'die Zuschlagskriterien',
    'contract_type': 'die Vertragsart',
    'buyer_legal_type': 'die Art des Auftraggebers', 'buyer_activity': 'die Art des Auftraggebers',
    'buyer_is_cpb_awarding': 'die Art des Auftraggebers',
    'buyer_is_cpb_acquiring': 'die Art des Auftraggebers',
    'service_provider_type': 'die Art des Auftraggebers',
    'buyer_country': 'der Sitz des Auftraggebers',
    'eu_funded': 'die Finanzierung', 'funding_programs': 'die Finanzierung',
    'duration_source': 'die Vertragslaufzeit', 'duration_unit_raw': 'die Vertragslaufzeit',
    'duration_measure_raw': 'die Vertragslaufzeit', 'duration_days': 'die Vertragslaufzeit',
    'span__period_start': 'die Vertragslaufzeit', 'span__period_end': 'die Vertragslaufzeit',
    'est_value_lot': 'das Auftragsvolumen', 'est_value_procedure': 'das Auftragsvolumen',
    'selection_criteria_types': 'die Eignungsanforderungen',
    'n_selection_criteria': 'die Eignungsanforderungen',
    'n_criteria_suitability': 'die Eignungsanforderungen',
    'n_criteria_financial': 'die Eignungsanforderungen',
    'n_criteria_technical': 'die Eignungsanforderungen',
    'n_criteria_other': 'die Eignungsanforderungen',
    'bid_bond_required': 'die geforderte Bietersicherheit',
    'bid_validity_unit': 'die geforderte Bindefrist',
    'bid_validity_days': 'die geforderte Bindefrist',
    'bid_validity_raw': 'die geforderte Bindefrist',
    'exclusion_grounds': 'die Ausschlussgründe', 'n_exclusion_grounds': 'die Ausschlussgründe',
    'cv_required': 'die geforderten Qualifikationsnachweise',
    'legal_form_required': 'die geforderte Rechtsform',
    'security_clearance_required': 'die geforderte Sicherheitsüberprüfung',
    'docs_restricted': 'der beschränkte Zugang zu den Unterlagen',
    'variants': 'die Vorgaben zur Angebotsform', 'multiple_bids': 'die Vorgaben zur Angebotsform',
    'esubmission': 'der Einreichungsweg', 'sme_suitable': 'die Eignung für kleinere Betriebe',
    'framework_type': 'die Rahmenvereinbarung', 'is_framework': 'die Rahmenvereinbarung',
    'procurement_additional_types': 'besondere Vergabebedingungen',
    'is_strategic': 'besondere Vergabebedingungen',
    'procedure_languages': 'die Sprachanforderungen',
    'gpa_covered': 'die internationale Ausschreibungspflicht',
    'recurring': 'die regelmäßige Wiederkehr', 'eauction': 'das E-Auktions-Format',
    'is_corrigendum': 'die bisherigen Korrekturen', 'change_reasons': 'die bisherigen Korrekturen',
    'n_corrections_so_far': 'die bisherigen Korrekturen',
    'platform_name': 'die genutzte Vergabeplattform',
    'deadline_days': 'die Angebotsfrist', 'deadline_days_published': 'die Angebotsfrist',
    'span__deadline_date': 'die Angebotsfrist',
    'span__bid_opening_date': 'der Verfahrenszeitplan',
    'span__question_deadline_date': 'der Verfahrenszeitplan',
    'question_window_days': 'der Verfahrenszeitplan',
    'opening_lag_days': 'der Verfahrenszeitplan',
    'n_lots': 'der Loszuschnitt',
    'n_doc_references': 'der Umfang der Unterlagen',
}
WHY_PREFIXES = [
    (('cpv_main__', 'cpv_additional__'), 'das spezialisierte Gewerk'),
    (('place_nuts3__', 'place_postal_zone__'), 'der Standort — dort bieten weniger Firmen'),
    (('buyer_nuts__', 'buyer_postal_zone__'), 'der Sitz des Auftraggebers'),
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

def predict_open(paths, tenders, roles, aw, args, arm=None, plan=None):
    """Score every open lot with the champion; append new rows to the ledger.
    Returns (new_ledger_rows, all_scores_this_cycle, scored) — the full score
    array feeds the score-distribution drift monitor, and `scored` (one dict
    per open lot, ledger-row shaped, dedup or not) feeds the subscription
    renderer even on cycles where dedup writes no new ledger rows.

    arm/plan (doc/EXPERIMENTS.md §8): during a trial this runs once per arm
    with the arm's own champion; every row is stamped with `experiment` and
    `arm` so the arm-vs-arm grading can find it. Only the delivering arm's
    return value reaches deliver(), the drift monitors and the simulation."""
    tag = f'[predict:{arm.id}]' if arm else '[predict]'
    champ = training.current_champion(paths, arm)
    if champ is None:
        print(f'{tag} no champion model — nothing to score')
        return [], np.array([]), []
    from catboost import CatBoostClassifier
    model = CatBoostClassifier()
    model.load_model(str(paths.models / champ['model_id'] / 'model.cbm'))

    open_t = sb.open_tenders(tenders, aw)
    today = util.now_utc().date().isoformat()
    deadline = pd.to_datetime(open_t.get('deadline_date'), errors='coerce')
    open_t = open_t[(deadline.isna()) | (deadline.dt.date.astype(str) >= today)]
    if open_t.empty:
        print(f'{tag} no open lots')
        return [], np.array([]), []
    # the champion's own multi-hot vocabulary and feature build: the columns
    # it was trained on. A champion from before the vocabulary existed has
    # none; the default fit is then the best guess and the schema check below
    # still decides.
    meta = champ.get('meta') or {}
    multihot = meta.get('multihot')
    feature_build = meta.get('feature_build', 'default')
    X_open, cats_open, _, _ = sb.build_features(open_t, roles, list_frame=tenders,
                                                multihot=multihot, feature_build=feature_build)
    if list(X_open.columns) != list(model.feature_names_):
        # Reachable only when learn() could not promote a candidate (a trust
        # check blocked it) across a feature change: the champion predates the
        # current build and cannot score it. Loud skip, not a stack trace —
        # ONLINE_LEARNING.md: "blocks keep the champion and notify; nothing
        # fails silently". No scores this cycle means no picks this cycle, which
        # the report and every subscription already state as an outcome.
        now = set(X_open.columns)
        had = set(model.feature_names_)
        print(f'{tag} SCHEMA MISMATCH — champion {champ["model_id"]} was '
              f'trained on a different feature set, so it cannot score this '
              f'build. Not scoring this cycle. '
              f'new: {sorted(now - had) or "-"}; '
              f'gone: {sorted(had - now) or "-"}. '
              f'Fix the blocked trust check in [learn] and rerun.')
        return [], np.array([]), []
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

    seen = ledger.prediction_keys(paths.ledger_home)
    ts = util.now_utc().isoformat(timespec='seconds')
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
            'cpv_main': util.stamp(cpv),  # full code for the relevance code channel
            'place_nuts3': util.stamp(t.get('place_nuts3')),
            'publication_number': util.stamp(t.get('publication_number')),
            'buyer_name': util.stamp(t.get('buyer_name')),
            'est_value_lot': util.stamp(t.get('est_value_lot')),
            'title': util.stamp(t.get('title')),
            'why_lonely': w_l, 'why_crowded': w_c,
        }
        if arm:
            # the arm-vs-arm grading finds an arm's rows by these two stamps;
            # rows from before the experiment have none and never count
            row['experiment'] = plan.experiment.id
            row['arm'] = arm.id
        scored.append(row)
        key = (t['procedure_id'], t['lot_id'], t.get('notice_id'), champ['model_id'])
        if key not in seen:  # idempotent re-runs: same notice + same model scored once
            rows.append(row)
    ledger.append(paths.ledger_home, 'predictions', rows)
    print(f'{tag} {len(rows)} new ledger rows ({len(open_t)} open rows scored, '
          f'model {champ["model_id"]})')
    return rows, scores, scored


