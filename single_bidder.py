"""Single-bidder classifier: the training / evaluation / prediction logic from
train_single_bidder.ipynb as importable functions (recipe: TRAINING.md).

Used by the notebook today and by the online-learning loop (ONLINE_LEARNING.md)
next. Unlike the exploration scripts, this module needs third-party packages:
pandas, pyarrow, catboost, scikit-learn.

Every function is deterministic for a fixed seed; the notebook's numbers
reproduce exactly.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from catboost import CatBoostClassifier, Pool
from sklearn.metrics import (
    average_precision_score,
    precision_score,
    recall_score,
    roc_auc_score,
)

KEY = ['procedure_id', 'lot_id']
NA = '__NA__'
ONE_HOT_MAX_SIZE = 1024  # must exceed every categorical cardinality: pure one-hot, no CTR
SEED = 42
LABEL_MAX_TENDERS = 1  # insufficient competition = 0 or 1 bids
TOO_GOOD_ROC = 0.85  # tripwire 2: literature tops out ~0.7


# ---------------------------------------------------------------- data loading

def load_with_roles(path):
    """Read a parquet plus the `role` tag embedded in each column's metadata."""
    schema = pq.read_schema(path)
    roles = {}
    for name in schema.names:
        md = schema.field(name).metadata or {}
        roles[name] = md.get(b'role', b'').decode() or None
    return pd.read_parquet(path), roles


def _has_flag(flags, name):
    if flags is None:
        return False
    try:
        return name in list(flags)
    except TypeError:
        return False


def latest_awards(awards):
    """Latest award revision per lot, reporting errors dropped, label attached.

    Returns (aw, n_dropped): aw keeps ALL award columns (needed for buyer
    history) — it must never feed features directly (leakage rule 1).
    """
    aw = awards.sort_values('publication_date').groupby(KEY, as_index=False).tail(1).copy()
    bad = aw['quality_flags'].apply(lambda f: _has_flag(f, 'winner_but_zero_tenders'))
    aw = aw[~bad]
    aw = aw[aw['n_tenders'].notna()].copy()
    aw['label'] = (aw['n_tenders'] <= LABEL_MAX_TENDERS).astype(int)
    return aw, int(bad.sum())


def assemble(tenders, awards):
    """Labeled dataset: every tender revision joined to its lot's eventual label.

    Source firewall (leakage rule 1): from awards only KEY + label survive into
    the joined frame; an assertion fails if any other award column slips in.
    Returns (data, aw, n_dropped).
    """
    aw, n_dropped = latest_awards(awards)
    data = tenders.merge(aw[KEY + ['label']], on=KEY, how='inner')
    leaked = [c for c in data.columns
              if c in set(awards.columns) - set(tenders.columns) - {'label'}]
    assert leaked == [], f'awards columns leaked into features: {leaked}'
    return data, aw, n_dropped


# ---------------------------------------------------------- feature engineering

def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, np.ndarray)):
        return list(v)
    if isinstance(v, float) and np.isnan(v):
        return []
    return [v]


def _cat_str(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return NA
    return str(v)


def _hier_levels(col):
    if col == 'cpv_main':
        # Depth decision 2026-08-06 (TRAINING.md): the main code is expanded to
        # its FULL 8 digits. cpv2 is dropped — under a CPV-45 scope it has one
        # category, i.e. a constant column (measured: dropping it moves val
        # PR-AUC by +0.0000). Digits 5-8 were previously discarded on 75.7% of
        # lots, and they carry signal cpv4 cannot express: inside one cpv4
        # bucket, cpv6 sub-buckets spread 13.2pt against a 10.3% base rate
        # (permutation null 6.8pt, p ~ 0.000). A/B on the temporal holdout:
        # PR-AUC 0.2766 -> 0.3071, +9.6% relative, positive in 4/4 split
        # dates. Receipt: python cpv_depth_receipt.py
        return [('cpv3', 3), ('cpv4', 4), ('cpv6', 6), ('cpv8', 8)]
    if 'cpv' in col:
        # cpv_additional stays shallow. It is a list column; since 2026-08-16
        # it is encoded multi-hot per level (one 0/1 column per code, see
        # fit_multihot / _apply_multihot) instead of one combination string per
        # level — the combination string had 1,767 distinct values at cpv4 on
        # the server, over one_hot_max_size=1024, and the leakage-rule-4 guard
        # rightly refused every candidate (doc/EXPERIMENTS.md §1). Depth stays
        # at cpv4: deeper levels multiply the vocabulary for no measured gain.
        return [('cpv2', 2), ('cpv3', 3), ('cpv4', 4)]
    if 'nuts' in col:
        return [('nuts1', 3), ('nuts2', 4), ('nuts3', 5)]
    if 'postal' in col:
        return [('zone1', 1)]
    return None


# Multi-hot support (TRAINING.md "List columns are multi-hot", scaled
# 2026-08-17): a value gets its own 0/1 column when at least MULTIHOT_MIN_SHARE
# of the distinct lots in the training frame carry it — a SHARE, so the rule
# follows the store instead of decaying into a formality as it grows (the
# operator: "today is good enough is a time bomb"). MULTIHOT_MIN_SUPPORT is
# the FLOOR under that share: the statistical minimum below which a column
# is noise whatever the store size. It stops mattering past ~20,000 lots.
MULTIHOT_MIN_SHARE = 0.0015    # 0.15 %: 30 of today's ~20,000 lots — the first day changes nothing
MULTIHOT_MIN_SUPPORT = 30      # the floor, in lots
# The feature build the cycle runs (FEATURE_BUILDS below). 'default': list
# columns multi-hot, single-valued categoricals one-hot under the 1024 cap.
# 'multihot': EVERY categorical column multi-hot — no cap anywhere, the
# encoding an all-trades store needs (TRAINING.md, 2026-08-17). A knob: the
# replay measures it under the lever, an arm confirms it forward.
FEATURE_BUILD = 'default'
# The flagging cut-off: score >= THRESHOLD is "low competition expected". The
# cycle's `--threshold` defaults to this; the replay reads it here too — one
# value, so the queue's lever (below) reaches both.
THRESHOLD = 0.5

# A candidate value from TM_GATE_OVERRIDE lands here, exactly as in
# evidence.py / relevance.py (PARAMETERS.md 10.1, 13): the two competitiveness
# knobs are measured by the replay harness under their own value without
# anybody editing a constant. Placed before `fit_multihot`, whose default
# argument binds MULTIHOT_MIN_SUPPORT at definition time.
import util  # noqa: E402  (after the constants on purpose)
_OVERRIDDEN = util.apply_override(globals())
if _OVERRIDDEN:
    print('[single_bidder] override: '
          + ', '.join(f'{k}={v!r}' for k, v in sorted(_OVERRIDDEN.items())))

# Named feature builds (doc/EXPERIMENTS.md §3). `default` is the build the
# cycle runs; the others exist so an A/B arm can differ from it in exactly one
# named way. `cpv_additional_combination`: the additional-CPV codes stay one
# combination string per level (the pre-2026-08-16 encoding, the "target
# statistics" arm) while every other list column is multi-hot as in `default`.
# `multihot`: every categorical column — list or single-valued, categorical,
# hierarchical (cpv_main at cpv3/4/6/8) or bool — becomes 0/1 columns for the
# values with support, plus n_rare and n; no CatBoost categorical feature is
# left, so `assert_pure_one_hot` has nothing to refuse and ONE_HOT_MAX_SIZE
# is not a wall (1,323 cpv4 classes across all trades would breach it).
FEATURE_BUILDS = ('default', 'cpv_additional_combination', 'multihot')


def effective_support(n_lots, share=None, floor=None):
    """The support in lots for a frame of `n_lots` distinct lots:
    max(floor, ceil(share * n_lots)). Both default to the module's values, so
    an override of either reaches here."""
    share = MULTIHOT_MIN_SHARE if share is None else share
    floor = MULTIHOT_MIN_SUPPORT if floor is None else floor
    return int(max(int(floor), int(np.ceil(share * int(n_lots)))))


def _check_build(feature_build):
    if feature_build not in FEATURE_BUILDS:
        raise ValueError(f'unknown feature_build {feature_build!r}; '
                         f'known: {", ".join(FEATURE_BUILDS)}')


def _is_list_col(frame, col):
    return col in frame.columns and frame[col].map(
        lambda v: isinstance(v, (list, np.ndarray))).any()


def _multihot_levels(roles, frame, feature_build=None):
    """The columns encoded multi-hot and their levels: every LIST column whose
    role is categorical (one level, the full value) or hierarchical with a
    known scheme (one level per truncation depth). A list never becomes a
    combination string: 32 criterion types made 2,102 combinations, 1,514
    additional CPV codes made 5,325 — the combinations are what outgrew the
    one-hot cap, the values themselves never do. Non-list columns are
    untouched: one value per row is one-hot safe as it is."""
    feature_build = FEATURE_BUILD if feature_build is None else feature_build
    _check_build(feature_build)
    out = {}
    for col, role in roles.items():
        if feature_build == 'multihot':
            # every categorical column, whatever its shape (TRAINING.md)
            if role in ('categorical', 'bool'):
                out[col] = [(None, None)]
            elif role == 'hierarchical' and _hier_levels(col) is not None:
                out[col] = _hier_levels(col)
            continue
        if not _is_list_col(frame, col):
            continue
        if feature_build == 'cpv_additional_combination' and col == 'cpv_additional':
            continue  # stays a combination string per level (build_features)
        if role == 'categorical':
            out[col] = [(None, None)]
        elif role == 'hierarchical' and _hier_levels(col) is not None:
            out[col] = _hier_levels(col)
    return out


def _codes_at(values, n):
    """The distinct codes of one list value, truncated to n digits (None: whole)."""
    return {str(x)[:n] if n else str(x) for x in _as_list(values)}


def _level_key(lname):
    return lname if lname is not None else '*'


def _colname(col, lname, tail):
    return f'{col}__{lname}__{tail}' if lname is not None else f'{col}__{tail}'


def fit_multihot(frame, roles, min_support=None, feature_build=None):
    """The multi-hot vocabulary: per list column and level, the values present
    in at least `min_support` distinct lots of `frame`, sorted.

    Fit on the FULL tenders frame (the same `list_frame` build_features takes),
    never on the labeled subset or the open subset alone: both are then
    transformed with the same columns. No label is consulted, so this is not
    target leakage — it is a fact about the notice population, like a role.

    The result is plain JSON (dict of dict of list) so training.learn can store it
    in the champion's meta.json and predicting.predict_open can rebuild the very
    same columns weeks later, whatever the archive has grown to since.
    Flat categorical lists use the level key '*'.
    """
    feature_build = FEATURE_BUILD if feature_build is None else feature_build
    vocab = {}
    lots = frame.drop_duplicates(subset=KEY) if all(k in frame.columns for k in KEY) else frame
    # the support is a SHARE of the lots with a floor (effective_support);
    # `min_support` given explicitly wins — a stored champion's own number
    if min_support is None:
        min_support = effective_support(len(lots))
    for col, levels in _multihot_levels(roles, frame, feature_build).items():
        vocab[col] = {}
        for lname, n in levels:
            counts = {}
            for v in lots[col]:
                for code in _codes_at(v, n):
                    counts[code] = counts.get(code, 0) + 1
            vocab[col][_level_key(lname)] = sorted(
                c for c, k in counts.items() if k >= min_support)
    return {'min_support': int(min_support), 'min_share': MULTIHOT_MIN_SHARE,
            'n_lots': int(len(lots)), 'vocab': vocab, 'feature_build': feature_build}


def _apply_multihot(df, col, levels, vocab):
    """Numeric columns for one list column: per level, has_<value> for every
    vocabulary value, n_rare (values outside the vocabulary), n (distinct
    values, 0 for an empty list — so 'no additional codes' is a value of its
    own, not a row of zeros that looks like 'only rare codes'). Returns one
    DataFrame block, built in a single piece (hundreds of columns inserted one
    by one fragment the frame and pandas says so on every insert)."""
    block = {}
    for lname, n in levels:
        codes_at = df[col].map(lambda vs, n=n: _codes_at(vs, n))
        known = vocab.get(_level_key(lname), [])
        known_set = set(known)
        for code in known:
            block[_colname(col, lname, f'has_{code}')] = codes_at.map(
                lambda s, code=code: int(code in s))
        block[_colname(col, lname, 'n_rare')] = codes_at.map(
            lambda s: sum(1 for c in s if c not in known_set))
        block[_colname(col, lname, 'n')] = codes_at.map(len)
    return pd.DataFrame(block, index=df.index, dtype='int64')


def build_features(df, roles, list_frame=None, multihot=None, feature_build=None):
    """Mechanical role-driven feature build (leakage rule 2).

    list_frame: frame used to detect list-typed columns (which are encoded
    multi-hot, see fit_multihot) — pass the FULL tenders frame so any subset
    (e.g. open lots) transforms identically.
    multihot: the vocabulary from fit_multihot; None fits it on list_frame,
    which gives identical columns to every caller passing the same frame.
    A stored champion passes its own (meta.json) so scoring uses the columns
    it was trained on, whatever the archive holds today.
    feature_build: one of FEATURE_BUILDS; must match the vocabulary's.
    Returns (X, cat_cols, num_cols, excluded).
    """
    feature_build = FEATURE_BUILD if feature_build is None else feature_build
    if list_frame is None:
        list_frame = df
    if multihot is None:
        multihot = fit_multihot(list_frame, roles, feature_build=feature_build)
    if multihot.get('feature_build', 'default') != feature_build:
        raise ValueError(f'multihot vocabulary was fitted for feature_build '
                         f'{multihot.get("feature_build", "default")!r}, '
                         f'not {feature_build!r}')
    multi = _multihot_levels(roles, list_frame, feature_build)
    is_list = {c: _is_list_col(list_frame, c) for c in roles}
    X = pd.DataFrame(index=df.index)
    cats, nums, excl, blocks = [], [], [], []
    for col, role in roles.items():
        s = df[col]
        if col in multi:
            # a LIST column, categorical or hierarchical: multi-hot numeric
            # 0/1 flags per value (per level), never one combination string
            # — no categorical column, so leakage rule 4 holds structurally
            block = _apply_multihot(df, col, multi[col], multihot['vocab'].get(col, {}))
            blocks.append(block)
            nums += list(block.columns)
        elif role == 'numeric':
            X[col] = pd.to_numeric(s, errors='coerce')
            nums.append(col)
        elif role == 'bool':
            # 3-way categorical: missingness is informative (null != False)
            X[col] = s.map(lambda v: NA if v is None or (isinstance(v, float) and np.isnan(v))
                           else str(bool(v)))
            cats.append(col)
        elif role == 'categorical':
            X[col] = s.map(_cat_str)
            cats.append(col)
        elif role == 'hierarchical':
            levels = _hier_levels(col)
            if levels is None:
                excl.append((col, 'hierarchical-unknown-scheme'))
                continue
            for lname, n in levels:
                new = f'{col}__{lname}'
                if is_list[col]:
                    # only under feature_build='cpv_additional_combination':
                    # one combination string per level, the encoding the
                    # rule-4 guard outgrew — kept as the "target statistics"
                    # arm of doc/EXPERIMENTS.md, never the default
                    X[new] = s.map(lambda v, n=n: '|'.join(sorted(_codes_at(v, n))) or NA)
                else:
                    X[new] = s.map(lambda v, n=n: NA if v is None or (isinstance(v, float) and np.isnan(v))
                                   else str(v)[:n])
                cats.append(new)
        elif role == 'date':
            if col == 'publication_date':
                continue  # the reference point, not a feature
            # errors='coerce': a buyer's typo (a deadline in the year 3032,
            # 2026-08-17) is NaN, not a failed Monday — the model handles
            # missing values, and one lot's bad date must never stop delivery
            X[f'span__{col}'] = (pd.to_datetime(s, errors='coerce')
                                 - pd.to_datetime(df['publication_date'], errors='coerce')).dt.days
            nums.append(f'span__{col}')
        else:
            excl.append((col, role or 'MISSING'))
    assert 'buyer_name' in [c for c, _ in excl], 'buyer_name must be excluded (rule 4)'
    if blocks:
        # the multi-hot blocks go to the end, in role order; the column
        # order is deterministic either way, which is all CatBoost needs
        X = pd.concat([X] + blocks, axis=1)
    return X, cats, nums, excl


def assert_pure_one_hot(X, cat_cols, one_hot_max_size=ONE_HOT_MAX_SIZE, exempt=()):
    """Leakage rule 4 guard: every categorical must fit under one_hot_max_size,
    otherwise CatBoost silently switches that column to target statistics.
    Returns the cardinality Series (descending, ALL categoricals) for display.

    exempt: columns allowed above the cap — the caller has decided, out loud,
    that CatBoost's ordered target statistics are wanted for exactly those
    (doc/EXPERIMENTS.md §3, the "target statistics" arm). They are left out of
    the max but stay in the returned Series, so the gate check can name them:
    `ctr_columns(card, exempt, cap)` gives the ones that will actually take it.
    """
    card = pd.Series({c: X[c].nunique() for c in cat_cols}).sort_values(ascending=False)
    checked = card.drop(labels=[c for c in exempt if c in card.index])
    if len(checked):
        assert checked.max() <= one_hot_max_size, (
            f'{checked.idxmax()} has {checked.max()} categories > one_hot_max_size='
            f'{one_hot_max_size} -> CatBoost would use target statistics')
    return card


def ctr_columns(card, exempt, one_hot_max_size=ONE_HOT_MAX_SIZE):
    """{column: cardinality} of the exempt columns that DO exceed the cap —
    the ones CatBoost will encode with target statistics."""
    return {c: int(card[c]) for c in exempt
            if c in card.index and card[c] > one_hot_max_size}


# -------------------------------------------------------------- split / weights

@dataclass
class Split:
    Xtr: pd.DataFrame
    ytr: np.ndarray
    wtr: np.ndarray
    Xte: pd.DataFrame
    yte: np.ndarray
    wte: np.ndarray
    is_train: np.ndarray  # row mask over the full data/X frames
    threshold: pd.Timestamp
    n_train_lots: int
    n_test_lots: int

    @property
    def base_rate_train(self):
        return float(np.average(self.ytr, weights=self.wtr))

    @property
    def base_rate_test(self):
        return float(np.average(self.yte, weights=self.wte))


def temporal_split(data, X, quantile=0.8, threshold=None):
    """Temporal group-aware split (leakage rule 3): a lot goes wholly to train or
    test by its FIRST publication_date; rows weighted 1/k (k = revision count).
    Asserts no lot straddles the boundary.

    threshold: explicit boundary date (lots first published after it go to test);
    when omitted, the quantile of lot first-publication dates is used."""
    pub = pd.to_datetime(data['publication_date'])
    first_pub = pub.groupby([data[k] for k in KEY]).transform('min')
    lot_first = pub.groupby([data[k] for k in KEY]).min()
    if threshold is None:
        threshold = lot_first.quantile(quantile)
    threshold = pd.Timestamp(threshold)

    is_train = (first_pub <= threshold).to_numpy()
    train_lots = set(map(tuple, data.loc[is_train, KEY].drop_duplicates().values))
    test_lots = set(map(tuple, data.loc[~is_train, KEY].drop_duplicates().values))
    assert not (train_lots & test_lots), 'a (procedure_id, lot_id) straddles the split boundary'

    k = data.groupby(KEY)['label'].transform('size')
    w = (1.0 / k).to_numpy()
    y = data['label'].to_numpy()
    return Split(
        Xtr=X[is_train], ytr=y[is_train], wtr=w[is_train],
        Xte=X[~is_train], yte=y[~is_train], wte=w[~is_train],
        is_train=is_train, threshold=threshold,
        n_train_lots=len(train_lots), n_test_lots=len(test_lots),
    )


# ------------------------------------------------------------ train / evaluate

def make_model(cat_cols, one_hot_max_size=ONE_HOT_MAX_SIZE, seed=SEED, **overrides):
    """TRAINING.md model block; overrides forwarded to CatBoost (e.g. iterations)."""
    params = dict(
        cat_features=cat_cols,
        one_hot_max_size=one_hot_max_size,  # pure one-hot, no target statistics
        auto_class_weights='Balanced',
        eval_metric='PRAUC',
        random_seed=seed,
        verbose=False,
        # CatBoost otherwise drops a `catboost_info/` scratch directory into
        # the working directory on every fit — per-iteration tsv logs nobody
        # reads, and the last thing in this program that wrote to the code
        # checkout (doc/STORAGE.md 6.5). It affects logging only, never the
        # fitted model.
        allow_writing_files=False,
    )
    params.update(overrides)
    return CatBoostClassifier(**params)


def train(Xtr, ytr, wtr, cat_cols, **overrides):
    model = make_model(cat_cols, **overrides)
    model.fit(Pool(Xtr, ytr, weight=wtr, cat_features=cat_cols))
    return model


def predict(model, X):
    """Score rows: probability of insufficient competition (0-1 bids)."""
    return model.predict_proba(X)[:, 1]


def metrics(y, p, w, threshold=0.5):
    """Weighted metrics dict. precision = of flagged lots, share truly single-bid
    (the 'flags right'); recall = of single-bid lots, share caught ('coverage')."""
    yhat = (p >= threshold).astype(int)
    return {
        'pr_auc': float(average_precision_score(y, p, sample_weight=w)),
        'roc_auc': float(roc_auc_score(y, p, sample_weight=w)),
        'precision': float(precision_score(y, yhat, sample_weight=w)),
        'recall': float(recall_score(y, yhat, sample_weight=w)),
        'base_rate': float(np.average(y, weights=w)),
    }


def cpv4_baseline(split):
    """Single-feature baseline: per-cpv4 weighted single-bid rate learned on
    train, applied to test; unseen cpv4 falls back to the train base rate."""
    tr = pd.DataFrame({'cpv4': split.Xtr['cpv_main__cpv4'], 'y': split.ytr, 'w': split.wtr})
    rate = tr.groupby('cpv4').apply(lambda g: np.average(g['y'], weights=g['w']),
                                    include_groups=False)
    return split.Xte['cpv_main__cpv4'].map(rate).fillna(split.base_rate_train).to_numpy()


def feature_importance(model, X, y, w, cat_cols):
    """Importance indexed by the model's own feature order (never a hand list)."""
    pool = Pool(X, y, weight=w, cat_features=cat_cols)
    return pd.Series(model.get_feature_importance(pool),
                     index=model.feature_names_).sort_values(ascending=False)


# ------------------------------------------------------------------- tripwires

def permuted_lot_labels(frame, mask=None, seed=SEED):
    """Lot-level label permutation for the scrambled-answers tripwire: all rows
    of a lot keep a common, but randomly reassigned, label.
    frame needs KEY columns + 'label'. Returns {(procedure_id, lot_id): label}."""
    rows = frame if mask is None else frame.loc[mask]
    lot_ids = rows[KEY].apply(tuple, axis=1)
    uniq = lot_ids.drop_duplicates().tolist()
    lot2label = dict(zip(frame[KEY].apply(tuple, axis=1), frame['label']))
    orig = np.array([lot2label[l] for l in uniq])
    rng = np.random.default_rng(seed)
    return dict(zip(uniq, rng.permutation(orig)))


def labels_from_mapping(frame, mask, mapping, default=0):
    """Row labels for frame.loc[mask] looked up from a lot->label mapping."""
    rows = frame if mask is None else frame.loc[mask]
    return rows[KEY].apply(tuple, axis=1).map(mapping).fillna(default).astype(int).to_numpy()


def assert_shuffled_collapsed(pr_auc_shuffled, base_rate, factor=1.5):
    assert pr_auc_shuffled < base_rate * factor, (
        'TRIPWIRE: shuffled-label score did not collapse — pipeline leaks answers')


def assert_not_too_good(roc_auc, limit=TOO_GOOD_ROC):
    assert roc_auc < limit, 'TRIPWIRE: score too good to be true — hunt the leaking feature'


def single_feature_audit(split, features, cat_cols, iterations=200, seed=SEED):
    """Train on each feature alone (constant-in-train features skipped).
    Returns (PR-AUC Series descending, skipped list)."""
    skipped = [c for c in features if split.Xtr[c].nunique(dropna=False) <= 1]
    scores = {}
    for col in features:
        if col in skipped:
            continue
        is_cat = col in cat_cols
        m = train(split.Xtr[[col]], split.ytr, split.wtr,
                  [col] if is_cat else [], iterations=iterations, seed=seed)
        scores[col] = metrics(split.yte, predict(m, split.Xte[[col]]), split.wte)['pr_auc']
    return pd.Series(scores).sort_values(ascending=False), skipped


def open_tenders(tenders, aw):
    """Production dry-run population: tender rows whose lot has no award yet."""
    awarded = set(map(tuple, aw[KEY].values))
    mask = ~tenders[KEY].apply(tuple, axis=1).isin(awarded)
    return tenders[mask].copy()


# ------------------------------------------------------- v2: buyer track record

def award_history(aw):
    """Per-award frame for the expanding-window buyer encoder (rule 5)."""
    h = aw[KEY + ['buyer_name', 'publication_date', 'label']].dropna(subset=['buyer_name']).copy()
    h['award_pub'] = pd.to_datetime(h['publication_date'])
    return h.sort_values('award_pub')


def buyer_history(df, aw_hist, lot_labels=None):
    """Outcome-availability-aware buyer features (leakage rule 5): for each row,
    aggregate ONLY awards published strictly before the row's publication_date,
    never the row's own lot. lot_labels overrides history labels (for the
    scrambled-answers check of this encoder).
    Returns DataFrame[buyer_hist_n, buyer_hist_rate] aligned to df."""
    hist = aw_hist
    if lot_labels is not None:
        hist = aw_hist.copy()
        hist['label'] = hist[KEY].apply(tuple, axis=1).map(lot_labels)
    by_buyer = {b: (g['award_pub'].values, g['label'].to_numpy().cumsum())
                for b, g in hist.groupby('buyer_name')}
    own = {(r.procedure_id, r.lot_id): (r.award_pub, r.label) for r in hist.itertuples()}

    pubs = pd.to_datetime(df['publication_date']).values
    ns, rates = np.zeros(len(df)), np.full(len(df), np.nan)
    for i, (b, pub, p_id, l_id) in enumerate(
            zip(df['buyer_name'], pubs, df['procedure_id'], df['lot_id'])):
        if b is None or b not in by_buyer:
            continue
        dates, cum = by_buyer[b]
        idx = np.searchsorted(dates, pub, side='left')  # strictly earlier awards only
        n, pos = idx, (cum[idx - 1] if idx > 0 else 0)
        o = own.get((p_id, l_id))
        if o is not None and o[0] < pub:  # own award must never feed own feature
            n, pos = n - 1, pos - o[1]
        if n > 0:
            ns[i], rates[i] = n, pos / n
    return pd.DataFrame({'buyer_hist_n': ns, 'buyer_hist_rate': rates}, index=df.index)
