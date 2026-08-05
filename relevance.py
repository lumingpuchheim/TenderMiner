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

# Defaults from calibration_<MODEL_TAG>.md (best two-channel configuration —
# E under jina-v2-base-de); a subscription may override min_relevance per
# line. Revisit on every model_tag flip.
DEFAULT_MIN_RELEVANCE = 0.482
DEFAULT_MIN_CODE_RELEVANCE = 0.700
BORDERLINE_MARGIN = 0.05  # near-misses below the gate render as "knapp aussortiert"
# A pass is not a pick: recommendations must clear the gate with room to
# spare (a 0.007-margin pass looks identical to a 0.3-margin pass once
# scores are hidden, so confidence must be enforced structurally). Code
# passes need no margin — a code is precise whenever it speaks at all.
PICK_MARGIN = 0.05
# Profile expansion via trusted-code pools measurably HURTS under
# jina-v2-base-de (config C/D vs E in the receipt: 41.3% vs 26.5% leakage) —
# in a sharp embedding space, pseudo-references widen a profile more than
# they help. Off until a calibration proves otherwise for a future model.
USE_EXPANSION = False

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
        # cpv + buyer per sidecar row (store read at load time — profile
        # building is model-side runtime, not a customer-number reconstruction)
        lots = pd.read_parquet(Path(data_dir) / 'store' / 'tenders.parquet'
                               ).drop_duplicates(subset=KEY)
        key_cpv = dict(zip(zip(lots['procedure_id'], lots['lot_id']),
                           lots['cpv_main']))
        key_buyer = dict(zip(zip(lots['procedure_id'], lots['lot_id']),
                             lots['buyer_name']))
        key_title = dict(zip(zip(lots['procedure_id'], lots['lot_id']),
                             lots['title']))
        self.all_cpv = np.array(
            [key_cpv.get((r['procedure_id'], r['lot_id'])) for r in self.rows],
            dtype=object)
        self.all_buyer = np.array(
            [key_buyer.get((r['procedure_id'], r['lot_id'])) for r in self.rows],
            dtype=object)
        self.all_title = np.array(
            [key_title.get((r['procedure_id'], r['lot_id'])) for r in self.rows],
            dtype=object)
        self._pools = None

    @property
    def pools(self):
        if self._pools is None:
            self._pools = pseudo_refs(self.mat, self.all_cpv, self.trusted,
                                      self.cohesion, self.baseline)
        return self._pools


def _norm_buyer(name):
    """Buyer identity for the same-buyer guard. Name equality after whitespace/
    case normalisation — deliberately simple; a buyer using two spellings
    merely loses the guard for one of them (falls back to the normal path),
    never the other way around."""
    if name is None or (isinstance(name, float) and np.isnan(name)):
        return None
    return ' '.join(str(name).casefold().split()) or None


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
    if USE_EXPANSION:
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
    # titles aligned with the leading rows of ref_matrix (references, then
    # free texts); pseudo-reference rows beyond them have no title and fall
    # back to a generic "why" — they exist only when USE_EXPANSION is on
    ref_titles = ([str(gate.all_title[i] or '') for i in ref_rows]
                  + [str(t) for t in (sub.get('profile_texts') or [])])
    return {
        'ref_matrix': np.vstack(expanded),
        'ref_titles': ref_titles,
        'fp_rows': np.fromiter(fp, int, len(fp)),
        'ref_buyers': {b for b in (_norm_buyer(gate.all_buyer[i])
                                   for i in ref_rows) if b},
        'min_relevance': float(sub.get('min_relevance', DEFAULT_MIN_RELEVANCE)),
        'min_code_relevance': float(sub.get('min_code_relevance',
                                            DEFAULT_MIN_CODE_RELEVANCE)),
        'version': sub.get('version', 1),
    }


def judge(gate, profile, scored_row):
    """(passed, borderline, text_score, code_score) for one scored open lot.

    The gate juggles two imperfect signals, and every rule here says when to
    trust which:

      TEXT — rich, but worthless when the buyer copy-pasted one template
             across all their lots (measured: 311 heavy-templater buyers,
             14% of stored lots, incl. the biggest serial builders).
      CODE — precise, but only when the clerk coded honestly and deeply
             (measured per code via cohesion; nonsense codes exist).

    Ladder, top rule wins:
      1. Lot not in the sidecar yet -> pass ungated (fail-open: an
         infrastructure gap must never hide a tender).
      2. Same buyer as a profile reference -> text ABSTAINS (similarity
         between two documents the same office wrote is self-plagiarism,
         not evidence) and the code decides alone; if the code cannot
         speak (shallow / not in the dictionary) -> borderline band,
         visibly undecided rather than decided by a meaningless signal.
      3. Independent buyer -> text decides; a code may add a pass (OR),
         never veto — codes lie in both directions (Speyer), text is what
         the bidder actually reads.
    """
    i = gate.by_key.get((scored_row['procedure_id'], scored_row['lot_id']))
    if i is None:
        return True, False, None, None, None  # rule 1: fail-open
    sims = profile['ref_matrix'] @ gate.mat[i]
    text = float(sims.max())
    code = scored_row.get('cpv_main')
    # None = the code cannot speak; 0.0 would conflate "silent" with "far"
    c_score = (code_score(code, profile['fp_rows'], gate.lmat, gate.lpos)
               if isinstance(code, str) and is_deep(code) else None)

    def why_text():
        j = int(np.argmax(sims))
        titles = profile['ref_titles']
        return ('ref', titles[j]) if j < len(titles) and titles[j] else ('ref', None)

    def why_code():
        best = profile['fp_rows'][int(np.argmax(gate.lmat[profile['fp_rows']]
                                                @ gate.lmat[gate.lpos[code]]))]
        return ('code', gate.lrows[best]['label_de'])

    if _norm_buyer(scored_row.get('buyer_name')) in profile['ref_buyers']:
        # rule 2: text score is still computed and stamped for audit, but it
        # must not decide — only the code may
        if c_score is None:
            return False, True, text, 0.0, None
        passed = c_score >= profile['min_code_relevance']
        return passed, False, text, c_score, (why_code() if passed else None)

    # rule 3
    code_pass = (c_score or 0.0) >= profile['min_code_relevance']
    passed = text >= profile['min_relevance'] or code_pass
    borderline = (not passed
                  and text >= profile['min_relevance'] - BORDERLINE_MARGIN)
    why = (why_text() if text >= profile['min_relevance']
           else why_code() if code_pass else None)
    return passed, borderline, text, c_score or 0.0, why


def is_confident(profile, text, c_score):
    """Pick-worthy (PICK_MARGIN above the gate, or a code pass, or fail-open).
    A pass below this stays in the market view but never becomes a pick."""
    if text is None:
        return True  # fail-open rows carry no scores to be confident about
    return (text >= profile['min_relevance'] + PICK_MARGIN
            or (c_score or 0.0) >= profile['min_code_relevance'])
