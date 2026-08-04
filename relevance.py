"""TenderMining relevance gate — RELEVANCE.md phase 3 runtime.

Decides, per subscription, whether an open lot is the customer's business at
all, before the competition model decides whether it is worth bidding on. A
customer is defined by reference tenders (`profile_refs`: publication numbers
of their wins, resolved against the embedding sidecar) plus optional
`profile_texts`; a candidate passes if its text reads like any reference
(text channel) or its deep code names a trade in the profile's fingerprint
(code channel). A code can add evidence, never veto the text.

The gate is enabled per subscription by `min_relevance` (plus a profile);
subscriptions without it behave exactly as before — no flag-day. Thresholds
default to the committed calibration receipt for the active MODEL_TAG.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

from calibrate import code_score, fingerprint, is_deep, pseudo_refs
from embed import KEY, MODEL_TAG, load_label_sidecar, load_sidecar

# Defaults from calibration_<MODEL_TAG>.md (configuration D); a subscription
# may override min_relevance per line. Revisit on every model_tag flip.
DEFAULT_MIN_RELEVANCE = 0.521
DEFAULT_MIN_CODE_RELEVANCE = 0.750
BORDERLINE_MARGIN = 0.05  # near-misses below the gate render as "knapp aussortiert"

TRUSTED_CODES = Path(__file__).resolve().parent / f'trusted_codes_{MODEL_TAG}.json'


class Gate:
    """Sidecars + trust list loaded once per cycle; profiles built per sub."""

    def __init__(self, data_dir):
        self.rows, self.mat = load_sidecar(data_dir)
        self.lpos, self.lrows, self.lmat = load_label_sidecar(data_dir)
        trust = json.loads(TRUSTED_CODES.read_text(encoding='utf-8'))
        self.baseline = trust['baseline']
        self.cohesion = {c: v for c, v in trust['codes'].items()}
        self.trusted = {c for c, v in trust['codes'].items() if v['trusted']}
        self.by_pub = {}
        for i, r in enumerate(self.rows):
            self.by_pub.setdefault(r['publication_number'], i)
        self.by_key = {(r['procedure_id'], r['lot_id']): i
                       for i, r in enumerate(self.rows)}
        # cpv per sidecar row for the pseudo-reference pools (store read at
        # load time — profile building is model-side runtime, not a customer-
        # number reconstruction)
        lots = pd.read_parquet(Path(data_dir) / 'store' / 'tenders.parquet'
                               ).drop_duplicates(subset=KEY)
        key_cpv = dict(zip(zip(lots['procedure_id'], lots['lot_id']),
                           lots['cpv_main']))
        self.all_cpv = np.array(
            [key_cpv.get((r['procedure_id'], r['lot_id'])) for r in self.rows],
            dtype=object)
        self._pools = None

    @property
    def pools(self):
        if self._pools is None:
            self._pools = pseudo_refs(self.mat, self.all_cpv, self.trusted,
                                      self.cohesion, self.baseline)
        return self._pools


def wants_gate(sub):
    return bool(sub.get('min_relevance') is not None
                and (sub.get('profile_refs') or sub.get('profile_texts')))


def build_profile(gate, sub):
    """Resolve a subscription's profile against the sidecars. An unresolvable
    profile_ref is an error (RELEVANCE.md: never a silent skip)."""
    ref_rows = []
    for pub in sub.get('profile_refs') or []:
        if pub not in gate.by_pub:
            raise ValueError(f'profile_ref {pub!r} not in embedding sidecar')
        ref_rows.append(gate.by_pub[pub])
    ref_rows = np.array(sorted(set(ref_rows)), dtype=int)
    ref_vecs = gate.mat[ref_rows] if len(ref_rows) else np.empty((0, gate.mat.shape[1]))
    if sub.get('profile_texts'):
        from embed import embed_texts  # loads the model lazily, only when needed
        ref_vecs = np.vstack([ref_vecs, embed_texts(list(sub['profile_texts']))])
    if not len(ref_vecs):
        raise ValueError('gated subscription has an empty profile')

    ref_codes = [gate.all_cpv[i] for i in ref_rows]
    expanded = [ref_vecs]
    for c in {c for c in ref_codes if c in gate.trusted}:
        pool = gate.pools[c][~np.isin(gate.pools[c], ref_rows)]
        if len(pool):
            expanded.append(gate.mat[pool])
    # fingerprint over the reference vectors + trusted reference codes
    sims = (gate.lmat @ ref_vecs.T).max(axis=1)
    fp = set(np.argsort(-sims)[:8].tolist())
    for c in ref_codes:
        if c in gate.trusted and c in gate.lpos:
            fp.add(gate.lpos[c])
    return {
        'ref_matrix': np.vstack(expanded),
        'fp_rows': np.fromiter(fp, int, len(fp)),
        'min_relevance': float(sub.get('min_relevance', DEFAULT_MIN_RELEVANCE)),
        'min_code_relevance': float(sub.get('min_code_relevance',
                                            DEFAULT_MIN_CODE_RELEVANCE)),
        'version': sub.get('version', 1),
    }


def judge(gate, profile, scored_row):
    """(passed, borderline, text_score, code_score) for one scored open lot.

    Fail-open: a lot missing from the sidecar (embedding step behind the
    store) passes ungated with scores None — the gate must never hide a
    tender because of an infrastructure gap."""
    i = gate.by_key.get((scored_row['procedure_id'], scored_row['lot_id']))
    if i is None:
        return True, False, None, None
    text = float((profile['ref_matrix'] @ gate.mat[i]).max())
    code = scored_row.get('cpv_main')
    c_score = (code_score(code, profile['fp_rows'], gate.lmat, gate.lpos)
               if isinstance(code, str) and is_deep(code) else 0.0)
    passed = (text >= profile['min_relevance']
              or c_score >= profile['min_code_relevance'])
    borderline = (not passed
                  and text >= profile['min_relevance'] - BORDERLINE_MARGIN)
    return passed, borderline, text, c_score
