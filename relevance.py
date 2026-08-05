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

from calibrate import is_deep, pseudo_refs
from embed import KEY, MODEL_TAG, load_label_sidecar, load_sidecar

# Defaults from calibration_<MODEL_TAG>.md (configuration H under
# jina-v2-base-de, 2026-08-05); a subscription may override min_relevance
# per line. Revisit on every model_tag flip.
DEFAULT_MIN_RELEVANCE = 0.700
# The fingerprint splits by origin (configuration F). HARD codes are facts —
# trusted codes on the customer's actual won lots. SOFT labels are our own
# guesses — dictionary labels near the reference texts, kept when at least
# SOFT_CONSENSUS references sit above SOFT_FLOOR. With phase-5 corroboration
# guarding soft-only passes, the searched optimum relaxed membership to
# floor .45 / consensus 2 (the corroboration contains what loose membership
# lets through — receipt: leakage 2.1% -> 1.5%, recall 58.4% -> 60.3%).
DEFAULT_MIN_CODE_HARD = 0.825
DEFAULT_MIN_CODE_SOFT = 0.725
SOFT_FLOOR = 0.45
SOFT_CONSENSUS = 2
# ONE bar (decision 2026-08-05, configuration G): a lot that passes the gate
# is recommendable, full stop — there is no separate "pick confidence"; the
# earlier margin was the only uncalibrated number in the system and cost a
# documented false rejection. The bar is precision-first: calibrated by
# minimising wrong-trade leakage (2.1% in the receipt) subject only to the
# volume floor (a typical week must still have candidates); the resulting
# recall (58% of a firm's own wins) is reported, never promised.
BORDERLINE_MARGIN = 0.05  # near-misses below the gate render as "knapp aussortiert"
# Profile expansion via trusted-code pools measurably HURTS under
# jina-v2-base-de (config C/D vs E in the receipt: 41.3% vs 26.5% leakage) —
# in a sharp embedding space, pseudo-references widen a profile more than
# they help. Off until a calibration proves otherwise for a future model.
USE_EXPANSION = False
# Phase 5 (RELEVANCE.md): trade-read corroboration of soft-only passes. A
# lot passing on the soft channel alone (a guess matched against a guess —
# the Rettungswache leak) must also READ as one of the profile's hard trade
# labels, else it demotes to the borderline band. H1 = absolute floor on
# sim(candidate text, best hard label); H2 = the hard label must come
# within PARAM of the candidate's best read anywhere. Receipt 2026-08-05:
# H2 @ 0.000 — the hard label must BE the candidate's top read.
TRADE_READ_FORM = 'H2'
TRADE_READ_PARAM = 0.0

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
        key_add = dict(zip(zip(lots['procedure_id'], lots['lot_id']),
                           lots['cpv_additional']))
        key_ctype = dict(zip(zip(lots['procedure_id'], lots['lot_id']),
                             lots['contract_type']))
        self.all_cpv = np.array(
            [key_cpv.get((r['procedure_id'], r['lot_id'])) for r in self.rows],
            dtype=object)
        self.all_cpv_add = np.array(
            [key_add.get((r['procedure_id'], r['lot_id'])) for r in self.rows],
            dtype=object)
        self.all_ctype = np.array(
            [key_ctype.get((r['procedure_id'], r['lot_id'])) for r in self.rows],
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


def _codes_of(gate, i):
    """ALL of a lot's CPV codes — main plus additional. Buyers often put the
    real trade in the additional codes (Lübeck: Gleisbau main,
    Baustellenüberwachung additional); the channel must see both."""
    out = []
    if isinstance(gate.all_cpv[i], str):
        out.append(gate.all_cpv[i])
    add = gate.all_cpv_add[i]
    if add is not None and not isinstance(add, float):
        out.extend(str(a) for a in add)
    return out


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

    ref_codes = [c for i in ref_rows for c in _codes_of(gate, i)]
    expanded = [ref_vecs]
    if USE_EXPANSION:
        for c in {c for c in ref_codes if c in gate.trusted}:
            pool = gate.pools[c][~np.isin(gate.pools[c], ref_rows)]
            if len(pool):
                expanded.append(gate.mat[pool])
    # the two-tier fingerprint (configuration F): hard = facts, soft = guesses
    hard_rows = sorted({gate.lpos[c] for c in ref_codes
                        if c in gate.trusted and c in gate.lpos})
    R = gate.lmat @ ref_vecs.T
    ok = np.flatnonzero((R >= SOFT_FLOOR).sum(axis=1) >= SOFT_CONSENSUS)
    soft_rows = ok[np.argsort(-R.max(axis=1)[ok])][:8]
    # titles aligned with the leading rows of ref_matrix (references, then
    # free texts); pseudo-reference rows beyond them have no title and fall
    # back to a generic "why" — they exist only when USE_EXPANSION is on
    ref_titles = ([str(gate.all_title[i] or '') for i in ref_rows]
                  + [str(t) for t in (sub.get('profile_texts') or [])])
    return {
        'ref_matrix': np.vstack(expanded),
        'ref_titles': ref_titles,
        'hard_rows': np.array(hard_rows, dtype=int),
        'soft_rows': soft_rows.astype(int),
        'ref_buyers': {b for b in (_norm_buyer(gate.all_buyer[i])
                                   for i in ref_rows) if b},
        'ref_types': {t for t in (gate.all_ctype[i] for i in ref_rows)
                      if isinstance(t, str) and t},
        'min_relevance': float(sub.get('min_relevance', DEFAULT_MIN_RELEVANCE)),
        'min_code_hard': float(sub.get('min_code_hard', DEFAULT_MIN_CODE_HARD)),
        'min_code_soft': float(sub.get('min_code_soft', DEFAULT_MIN_CODE_SOFT)),
        'version': sub.get('version', 1),
    }


def trade_read(gate, profile, i):
    """Phase-5 signal: what the candidate's OWN text reads as, in official
    vocabulary. Returns (best sim to the profile's hard trade labels, best
    sim to any label in the dictionary)."""
    reads = gate.lmat @ gate.mat[i]
    world = float(reads.max())
    hard = (float(reads[profile['hard_rows']].max())
            if len(profile['hard_rows']) else 0.0)
    return hard, world


def corroborated(gate, profile, i):
    """Phase 5: may a soft-only code pass stand? Only if the candidate's own
    text reads as one of the profile's hard trade labels. Without hard labels
    (cold start) there is no fact to corroborate against — rule inactive."""
    if TRADE_READ_FORM == 'off' or not len(profile['hard_rows']):
        return True
    tread, world = trade_read(gate, profile, i)
    if TRADE_READ_FORM == 'H1':
        return tread >= TRADE_READ_PARAM
    return tread >= world - TRADE_READ_PARAM


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
      2. Contract type (works/services/supplies) known on both sides and
         matching NO reference -> borderline band. Coarse but hard
         information; borderline and not out, because adjacent types can
         be real business (works-profile firm, services-coded maintenance).
      3. Same buyer as a profile reference -> text ABSTAINS (similarity
         between two documents the same office wrote is self-plagiarism,
         not evidence) and the code decides alone; if the code cannot
         speak (shallow / not in the dictionary) -> borderline band,
         visibly undecided rather than decided by a meaningless signal.
      4. Independent buyer -> text decides; a code may add a pass (OR),
         never veto — codes lie in both directions (Speyer), text is what
         the bidder actually reads.
      5. (phase 5, across rules 3 and 4) A pass carried by the SOFT channel
         alone is a guess matched against a guess (the Rettungswache leak:
         a building label entered the fingerprint via one reference text,
         then matched the candidate's building code). It stands only if the
         candidate's own text also reads as one of the profile's hard trade
         labels (see corroborated()); otherwise borderline band. Text
         passes and hard-code passes are untouched — a derived signal may
         demote a guess, never override a fact.

    The code channel reads ALL the lot's codes (main + additional), scored
    separately against the HARD fingerprint (trusted codes on actual wins —
    facts) and the SOFT one (labels inferred from reference texts — guesses,
    higher bar via calibration, and never pick-confident). A code is
    "silent" only if none is deep and known.

    Returns (passed, borderline, text, code, why, code_hard) — code is the
    stamped best of both tiers, code_hard feeds is_confident.
    """
    i = gate.by_key.get((scored_row['procedure_id'], scored_row['lot_id']))
    if i is None:
        return True, False, None, None, None, 0.0  # rule 1: fail-open
    sims = profile['ref_matrix'] @ gate.mat[i]
    text = float(sims.max())
    cand_rows = [gate.lpos[c] for c in _codes_of(gate, i)
                 if is_deep(c) and c in gate.lpos]
    c_hard = c_soft = 0.0
    hard_label = soft_label = None
    for cr in cand_rows:
        if len(profile['hard_rows']):
            s = gate.lmat[profile['hard_rows']] @ gate.lmat[cr]
            j = int(np.argmax(s))
            if s[j] > c_hard:
                c_hard, hard_label = float(s[j]), int(profile['hard_rows'][j])
        if len(profile['soft_rows']):
            s = gate.lmat[profile['soft_rows']] @ gate.lmat[cr]
            j = int(np.argmax(s))
            if s[j] > c_soft:
                c_soft, soft_label = float(s[j]), int(profile['soft_rows'][j])
    silent = not cand_rows
    hard_pass = c_hard >= profile['min_code_hard']
    soft_pass = c_soft >= profile['min_code_soft']
    code_pass = hard_pass or soft_pass
    c_score = max(c_hard, c_soft)

    def why_text():
        j = int(np.argmax(sims))
        titles = profile['ref_titles']
        return ('ref', titles[j]) if j < len(titles) and titles[j] else ('ref', None)

    def why_code():
        row = hard_label if hard_pass else soft_label
        return ('code', gate.lrows[row]['label_de'])

    ctype = gate.all_ctype[i]
    if (profile['ref_types'] and isinstance(ctype, str) and ctype
            and ctype not in profile['ref_types']):
        return False, True, text, c_score, None, c_hard  # rule 2

    soft_only = code_pass and not hard_pass

    if _norm_buyer(scored_row.get('buyer_name')) in profile['ref_buyers']:
        # rule 3: text score is still computed and stamped for audit, but it
        # must not decide — only the code may
        if silent:
            return False, True, text, 0.0, None, 0.0
        if soft_only and not corroborated(gate, profile, i):
            return False, True, text, c_score, None, c_hard  # rule 5
        return code_pass, False, text, c_score, (why_code() if code_pass else None), c_hard

    # rule 4
    passed = text >= profile['min_relevance'] or code_pass
    if (passed and text < profile['min_relevance'] and soft_only
            and not corroborated(gate, profile, i)):
        return False, True, text, c_score, None, c_hard  # rule 5
    borderline = (not passed
                  and text >= profile['min_relevance'] - BORDERLINE_MARGIN)
    why = (why_text() if text >= profile['min_relevance']
           else why_code() if code_pass else None)
    return passed, borderline, text, c_score, why, c_hard


