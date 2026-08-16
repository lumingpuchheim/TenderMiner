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

import dataclasses
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from calibrate import is_deep, pseudo_refs
import config
import util
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
# Phase 7 (RELEVANCE.md): trade-talk contradiction on HARD passes — a wrong
# code pointing toward the customer's own trade (the Trafostation case)
# passes the hard channel unopposed otherwise. Leniency for project talk,
# scrutiny for trade talk: a hard pass demotes to borderline when the
# candidate's best FOREIGN trade reading (CPV groups 453/454, no
# ancestor/descendant relation to a profile hard code — the hierarchy is
# the compatibility rule) beats its profile-trade reading by this margin.
# Value from the calibration receipt (configuration K, 2026-08-06):
# margin 0.225 = largest catch whose recall price stays under 0.5pt
# (recall 60.3% -> 60.0%, 5.9% of hard-code admissions contested; both
# known wrong picks carried +0.28/+0.29). None disables.
TRADE_TALK_MARGIN = 0.225
TRADE_BRANCHES = ('453', '454')
# Phase 8 (RELEVANCE.md): the gate switch. 'embedding' = the phase-5/7
# ladder exactly as shipped (tag gate-v1-embedding). 'evidence' = the
# evidence gate — trade keywords convict, similarity and codes only
# nominate. FLIPPED TO 'evidence' by explicit operator decision
# (2026-08-06) so the scheduled loop.py run uses it with no change to how
# cron calls it. Live configuration: bar 0.55 + K>=2 witnesses, band
# guess OFF. Receipt (evidence.py --judge, end to end, vs the embedding
# gate it replaces): recall 51.5% (was 44.5%), leakage 2.7% (was 1.9%),
# volume 4.4% (was 4.6% — report size unchanged), hand-labeled hard set
# 19/19 (was 18/19), grown benchmark 61/103 (was 47/103).
# Rollback: set this back to 'embedding' (tag gate-v1-embedding) — no
# retraining, no rebuild. The env var still overrides per run.
GATE_MODE = os.environ.get('GATE_MODE', 'evidence')
# Phase 8 nomination bar — the text-similarity level at which a lot enters
# the evidence test. Distinct from the embedding gate's conviction bar
# (min_relevance, 0.700): nomination convicts nothing, so it can afford
# recall (the spec predicted it must come down; the first implementation
# reused 0.700 unexamined and cost the gate half its recall). Receipt
# (evidence.py --sweep, 2026-08-06, real judge() components, 2473 pos /
# 25600 neg / 102400 vol): 0.55 is the ONLY grid point with benchmark
# 19/19 (Kölln-Reisiek flips to correct) AND leakage below the embedding
# gate's 1.9% — recall 23.8% -> 35.9%, leakage 1.6%, volume 2.4%. Bars
# <= 0.50 leak >= 3.0%; bars >= 0.60 lose Kölln-Reisiek (18/19). Full
# table in RELEVANCE.md phase 8.
NOMINATION_BAR = 0.550
# Phase 8i (operator decision 2026-08-07): SIMILARITY NO LONGER NOMINATES.
# Route 2 -- "this lot's text resembles the customer's past tenders" -- is
# off. Four reasons, in order of weight:
#
# 1. The test cannot see it. Positive cases are the firm's OWN past wins,
#    which come from buyers already in its profile, so same_buyer is true
#    and route 2 is MUTED for most of them. Production is the mirror
#    image: open lots come overwhelmingly from buyers the firm never won
#    from, so route 2 is ON. We measured a gate with this route disabled
#    and shipped a gate with it enabled -- including when we chose
#    NOMINATION_BAR itself, which is the knob that controls it.
# 2. Its safety catch is a string comparison. same_buyer is buyer-NAME
#    equality (_norm_buyer: casefold + whitespace). "SBH | Schulbau
#    Hamburg" and "GMH | Gebaeudemanagement Hamburg GmbH" are different
#    strings, so a shared municipal template sails through: the
#    Norderschulweg heating lot (GMH) was nominated for a FLOORING firm
#    whose six references are all SBH. The guard protects against one
#    buyer repeating itself, not against a family of authorities sharing
#    a document template -- which is the common case in German public
#    procurement (a city and its Eigenbetrieb, a Landkreis and its Aemter).
# 3. It contradicts phase 8's founding decision. Similarity was demoted
#    because it is not trustworthy enough to CONVICT. But nomination is
#    the door: whatever passes it need only clear the evidence test, and
#    that test is satisfiable by coincidence (two filler words before the
#    phase-8g vocabulary; 'dehnfug' in a water-reservoir spec after). A
#    signal we do not trust to decide should not be trusted to decide who
#    gets considered.
# 4. It is the leakage source. The bar sweep moved leakage 0.4% -> 1.6%
#    -> 6.0% as the bar dropped 0.70 -> 0.55 -> 0.40; that range is
#    similarity admitting lots.
#
# What remains: the CPV hard-code match and the trade-evidence witnesses.
# Both are buyer-independent, both mean the same thing in the test and in
# production, and both are readable. With route 2 off, no part of the
# evidence ladder consults the buyer name at all.
#
# KNOWN COST: route 2 uniquely rescued the lot whose TITLE names the trade
# with a single keyword, since the witness rule demands
# EVIDENCE_NOMINATION_MIN of them. The direct repair is to let a title
# witness nominate (conviction already treats it as sufficient) -- measured
# separately, not folded into this decision.
# NOMINATION_BAR is inert while this is False; it stays because the
# embedding ladder (GATE_MODE='embedding') and the sweep still use it.
SIMILARITY_NOMINATES = os.environ.get('SIMILARITY_NOMINATES', '0') != '0'
# Phase 8k (experiment 2026-08-07): CONVICTION-STRENGTH EVIDENCE NOMINATES
# ITSELF. The gate already holds that a trade keyword in the TITLE convicts
# on its own (evidence.convicts, the title-or-two rule) — yet nothing let
# that same fact open the door, so a lot could be convictable and never
# considered. Concrete case, 00367721-2025 'Estricharbeiten' (Thüringer
# Landesamt für Bau) against the screed firm N3Bau: filed under CPV
# 45216111 'Bau von Polizeirevieren', so hard=0.135 and route 1 fails; the
# description is an address, so evidence is estrich(t1) alone and the
# witness rule (which wants EVIDENCE_NOMINATION_MIN) fails; route 2 is gone
# since phase 8i. Verdict ok=False — a tender whose title IS the firm's
# trade. Unlike route 2 this cannot reopen the boilerplate hole: whole-
# document similarity is dominated by a buyer's standard paragraphs, while
# the title is the one field that names the actual work.
# SHIPPED ON (receipt: IN 26/74 -> 34/74, OUT unchanged at 45/52, total
# 71/126 -> 79/126). Eight recall cases for no measured precision cost --
# the only free move found in this whole sequence, because it admits on the
# field that names the work rather than on document resemblance.
# Rollback: CONVICTION_NOMINATES=0.
CONVICTION_NOMINATES = os.environ.get('CONVICTION_NOMINATES', '1') != '0'
# Phase 8b, the witness rule (operator design, 2026-08-06): "one
# coincidence is coincidence, multiple coincidences are a conviction."
# Evidence alone may NOMINATE a lot (no similarity, no code — deliberately
# buyer-independent, which rescues the same-buyer LOO misses) when at
# least this many DISTINCT lexicon keywords are found by the exact/typo
# tiers; synonym hits are weaker witnesses and do not count toward
# nomination. Any evidence still convicts a nominated lot; 0 disables.
# SHIPPED AT 2 (decision 2026-08-06, phase 8e — the operator asked for a
# K>=2 vs K>=3 call). Receipt (--sweep, 2473/25600/102400): K>=2 = 51.5%
# recall / 2.7% leakage / 4.4% volume / 19/19 / 61-63 of 103; K>=3 =
# 45.0% / 1.6% / 2.9% / 19/19 / 56/103. The decisive evidence is not the
# aggregate but WHICH cases separate them: all 5 differing benchmark
# cases are true wins that K>=2 catches and K>=3 rejects, and the two
# are IDENTICAL on every wrong-trade case (28/32 each) — so K>=2's extra
# recall costs nothing in hand-read precision. All 5 are same-buyer
# template wins (similarity 0.86-1.00) carrying exactly 2 witnesses:
# precisely the starvation class the witness rule exists to rescue, which
# K>=3 re-starves. The +1.1pt leakage appears only against synthetic
# off-class CPV negatives, not against operator judgment. K>=3 stays the
# fallback if leakage ever binds in production; K>=1 (55.3% / 3.7%) is
# the max-recall variant. The title-or-two conviction rule
# (evidence.convicts) keeps every K at 19/19. Tables in RELEVANCE.md.
EVIDENCE_NOMINATION_MIN = 2
# Phase 8d (operator decision 2026-08-06): the borderline band — the
# gate's honest "signals conflict" cases — is partially admitted with a
# fixed probability instead of being uniformly rejected. Band composition
# (8c receipt): 22.7pt of true wins, 15.6pt of wrong-trade lots; admit
# probability p gives recall 51.5%+p*22.7, leakage 2.7%+p*15.6, volume
# 4.4%+p*15.7. p = 0.375 is the smallest p clearing the operator's 60%
# recall bar (projected 60.0% / 8.5% / 10.2%). The draw is a
# DETERMINISTIC hash of the lot identity — same lot, same verdict, every
# run — so reports and receipts stay reproducible.
# SET TO 0 (OFF) FOR THE LIVE FLIP, 2026-08-06. Measured at p=0.375 the
# coin delivered the 60% recall target (60.4% / 8.6% / 10.3%) but tripled
# leakage, doubled report size, and overturned FOUR hand-labeled operator
# rejections (hard set 19/19 -> 15/19), including the Kreishaus lot that
# founded this phase. For a product whose value is a short trustworthy
# list, that trade is backwards: a missed lot is invisible, a wrong-trade
# recommendation is not. The band stays a flagged NO.
# The coin was always an interim PLACEHOLDER for an LLM judge that reads
# the borderline lot's title+Leistung and decides like the operator
# ("guess with new information"); the receipts show reading dominates
# guessing on BOTH axes (a 90%-accurate reader: ~71.9% recall / ~4.3%
# leakage on the same band, vs the coin's 60.4% / 8.6%) — the only
# measured move that improves recall and precision at once. Restoring a
# nonzero p re-enables the guess; see RELEVANCE.md phase 8d.
BORDERLINE_ADMIT_P = 0.0


def _band_draw(procedure_id, lot_id):
    """Deterministic uniform [0,1) per lot for the phase-8d band admit."""
    h = hashlib.md5(f'band:{procedure_id}|{lot_id}'.encode()).digest()
    return int.from_bytes(h[:8], 'big') / 2.0 ** 64

TRUSTED_CODES = Path(__file__).resolve().parent / f'trusted_codes_{MODEL_TAG}.json'


# --------------------------------------------------------------- the config

# A candidate value from TM_GATE_OVERRIDE for THIS module's constants
# (PARAMETERS.md 10) — applied before GateConfig is defined, because the
# dataclass takes them as its field defaults.
_OVERRIDDEN = util.apply_override(globals())
if _OVERRIDDEN:
    print(f'[relevance] gate override: '
          + ', '.join(f'{k}={v!r}' for k, v in sorted(_OVERRIDDEN.items())))


def _evidence_rules_snapshot():
    """`evidence.rules()` as a hashable tuple of (name, value) pairs. Lazy
    import: `evidence` pulls ftfy and the lexicon machinery, which the
    embedding-only callers of this module never need.

    Also the one choke point where an override key that NO module claimed is
    caught: by the time a gate configuration is built both modules have been
    imported, so a leftover key is a typo, not a timing artefact. Raising is
    the point — a run under an ignored override measures the champion while
    reporting the candidate's name.
    """
    import evidence as evd
    left = util.unconsumed_override()
    if left:
        raise SystemExit(
            f'TM_GATE_OVERRIDE names {", ".join(left)}, which no constant in '
            'evidence.py or relevance.py answers to. Check the spelling '
            '(names are the constants\' own, upper case) - an override that '
            'silently does nothing would measure the champion and call it '
            'the candidate.')
    return tuple(evd.rules().items())


@dataclass(frozen=True)
class GateConfig:
    """Every tunable that changes a verdict, as one value (REFACTOR.md phase 3).

    The constants above stay where they are — they carry the decision history
    that gives each number its meaning — and this dataclass takes them as its
    defaults. What changes is that the gate READS a config instead of reading
    module state, which buys two things the constants could not give:

      * **the ledger can say which rules picked a lot.** `fingerprint` is
        stamped on every delivery row, so the flip from the embedding ladder
        to the evidence gate (2026-08-06) stops being invisible in a
        customer's retrospective. The competition model was always stamped;
        the gate that decided the lot was theirs at all was not.
      * **two configurations can coexist in one process.** The receipt
        harnesses used to assign to `rel.GATE_MODE` / `rel.SOFT_FLOOR` /
        `rel.TRUSTED_CODES` and hope about ordering; they now pass configs.

    Frozen: a config is a value, not a settings object. `replace()` makes a
    variant, which is what a sweep wants anyway.
    """

    mode: str = GATE_MODE
    # bars (a subscription line may still override the three min_* per customer)
    min_relevance: float = DEFAULT_MIN_RELEVANCE
    min_code_hard: float = DEFAULT_MIN_CODE_HARD
    min_code_soft: float = DEFAULT_MIN_CODE_SOFT
    # soft-fingerprint membership
    soft_floor: float = SOFT_FLOOR
    soft_consensus: int = SOFT_CONSENSUS
    use_expansion: bool = USE_EXPANSION
    # embedding ladder
    borderline_margin: float = BORDERLINE_MARGIN
    trade_read_form: str = TRADE_READ_FORM
    trade_read_param: float = TRADE_READ_PARAM
    trade_talk_margin: float = TRADE_TALK_MARGIN
    trade_branches: tuple = TRADE_BRANCHES
    # evidence gate
    nomination_bar: float = NOMINATION_BAR
    similarity_nominates: bool = SIMILARITY_NOMINATES
    conviction_nominates: bool = CONVICTION_NOMINATES
    evidence_nomination_min: int = EVIDENCE_NOMINATION_MIN
    borderline_admit_p: float = BORDERLINE_ADMIT_P
    # world
    model_tag: str = MODEL_TAG
    trusted_codes: Path = TRUSTED_CODES
    # the evidence gate's own rules (PARAMETERS.md 4.1) — every
    # `evidence.py` constant that decides which words are witnesses and what
    # convicts, snapshotted at construction as (name, value) pairs. Before
    # this field the fingerprint stayed 7d29fa0dce whatever those constants
    # (or their env-var overrides) said; now moving any of them moves the
    # stamp on every delivery row written afterwards. Snapshot, not a
    # reference: `evidence.py` still reads its module state, so two configs
    # differing here cannot coexist in one process — the honest stamp is
    # what this buys, not that.
    evidence_rules: tuple = dataclasses.field(
        default_factory=lambda: _evidence_rules_snapshot())

    def replace(self, **fields):
        return dataclasses.replace(self, **fields)

    def as_dict(self):
        """Canonical, JSON-safe. The trust list enters by FILE NAME, not by
        path: the same list under a different absolute path is the same
        configuration, and `rewind_all.py`'s as-of list is a different one."""
        d = dataclasses.asdict(self)
        d['trusted_codes'] = Path(self.trusted_codes).name
        d['trade_branches'] = list(self.trade_branches)
        d['evidence_rules'] = dict(self.evidence_rules)
        return d

    @property
    def fingerprint(self):
        """Short stable hash of everything above — the ledger stamp.

        Covers the tunables, the embedding model tag and the trust list's
        file name. It does NOT cover the trust list's CONTENTS: those are a
        committed receipt (`trusted_codes_<tag>.json`) regenerated by
        calibrate.py, so a recalibration that leaves every tunable alone
        keeps the same fingerprint. Git history is the record for that; this
        hash answers "which rules", not "which trust list revision".
        """
        blob = json.dumps(self.as_dict(), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:10]

    @property
    def rules_fingerprint(self):
        """Hash of the evidence rules alone (matches `evidence.rules_fingerprint()`
        when the snapshot is current), so a describe() line says which
        witness rules were in force separately from the gate's own bars."""
        blob = json.dumps(dict(self.evidence_rules), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:10]

    def describe(self):
        bits = [f'mode={self.mode}']
        if self.mode == 'evidence':
            bits += [f'K>={self.evidence_nomination_min}',
                     f'sim_nom={int(self.similarity_nominates)}',
                     f'conv_nom={int(self.conviction_nominates)}',
                     f'band_p={self.borderline_admit_p}',
                     f'rules={self.rules_fingerprint}']
            if self.similarity_nominates:
                bits.insert(0, f'bar={self.nomination_bar}')
        else:
            bits += [f'min_relevance={self.min_relevance}',
                     f'trade_talk={self.trade_talk_margin}']
        return f"{self.fingerprint} ({', '.join(bits)})"


DEFAULT_CONFIG = GateConfig()


def mute_reason(profile, config=None):
    """Can this profile EVER convict a lot? -> None if yes, else why not.

    Under the evidence gate a lot passes on one of two things: a word from
    the profile's lexicon found in the tender, or a core trade root found in
    its title. A profile holding neither cannot pass ANY lot in the market —
    not a low score, an impossibility. The customer then receives an empty
    report every week and nothing anywhere says why; the operator sees
    silence and reasonably concludes the market was quiet.

    Measured 2026-08-07: 43 of the 512 firms with >= 3 wins were in this
    state (fewer since the dictionaries opened, but not zero). The gate has
    always known it at build time — it just never said so.
    """
    cfg = config or DEFAULT_CONFIG
    if cfg.mode != 'evidence':
        return None          # the embedding ladder does not read the lexicon
    if profile.get('keywords') or profile.get('keywords_core'):
        return None
    n_refs = len(profile.get('ref_titles') or ())
    return (f'no lexicon and no core trade root from {n_refs} reference(s) — '
            f'this profile cannot pass any lot')


def _check_profiles(data_dir, as_of):
    """`python relevance.py --check-profiles` — every live subscription's
    lexicon, and a loud line for the ones that can never recommend
    anything."""
    import subscriptions as subs_mod
    # CLAUDE.md: read through the sanctioned API, never the storage — it is
    # moving into SQLite and `load` takes the directory for exactly that
    # reason
    live = subs_mod.load(data_dir, as_of)
    gate = Gate(data_dir, as_of=as_of)
    print(f'[check] {len(live)} subscription(s) active on {as_of}, '
          f'gate {gate.config.describe()}')
    n_mute = 0
    for sub in sorted(live, key=lambda s: s['sub_id']):
        sid = sub['sub_id']
        if not wants_gate(sub):
            print(f'  {sid:24s} ungated — every matching lot is delivered')
            continue
        try:
            profile = build_profile(gate, sub)
        except Exception as e:
            print(f'  {sid:24s} PROFILE ERROR: {e}')
            n_mute += 1
            continue
        kw = profile.get('keywords') or []
        core = profile.get('keywords_core') or []
        why = mute_reason(profile, gate.config)
        if why:
            n_mute += 1
            print(f'  {sid:24s} ** MUTE ** {why}')
        else:
            print(f'  {sid:24s} {len(kw)} keyword(s), {len(core)} core root(s)')
            print(f'  {"":24s}   lexicon: {" ".join(kw[:12]) or "-"}')
            print(f'  {"":24s}   core:    {" ".join(core[:12]) or "-"}')
    print(f'[check] {n_mute} subscription(s) cannot recommend anything'
          + (' — these deliver an empty report every cycle' if n_mute else ''))
    return n_mute


def _print_config():
    """`python relevance.py` — the rules this checkout would judge under,
    with the fingerprint that appears on every delivery row. The answer to
    "which configuration produced this pick": match this against
    `gate_config` in the delivery ledger, or look the hash up in
    `data/ledger/gate_configs.jsonl`."""
    import sys
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    cfg = DEFAULT_CONFIG
    print(f'[gate] {cfg.describe()}')
    for k, v in cfg.as_dict().items():
        env = ''
        if k == 'mode' and 'GATE_MODE' in os.environ:
            env = '   <- from $GATE_MODE'
        elif k == 'similarity_nominates' and 'SIMILARITY_NOMINATES' in os.environ:
            env = '   <- from $SIMILARITY_NOMINATES'
        elif k == 'conviction_nominates' and 'CONVICTION_NOMINATES' in os.environ:
            env = '   <- from $CONVICTION_NOMINATES'
        print(f'  {k:24s} {v}{env}')


class Gate:
    """Sidecars + trust list loaded once per cycle; profiles built per sub.

    `as_of` (RELEVANCE.md phase 9) is the date whose world this gate
    reconstructs, and it governs learned references — a customer's own
    wins, recorded by feedback.py. **It has no default on purpose.**
    Without it no learned reference is loaded and every profile is exactly
    what the subscription line says, which is the behaviour every caller
    had before phase 9. A replay path that forgets to pass its cutoff
    therefore *understates* the profile; it can never pull a future win
    into a historical world, which is the failure that would silently
    flatter every backtest.
    """

    def __init__(self, data_dir, as_of=None, config=None):
        self.data_dir = data_dir
        self.as_of = str(as_of) if as_of else None
        # the configuration this gate judges under unless a caller passes
        # another to build_profile()/judge() — see GateConfig
        self.config = config or DEFAULT_CONFIG
        self._learned = None
        self.rows, self.mat = load_sidecar(data_dir)
        self.lpos, self.lrows, self.lmat = load_label_sidecar(data_dir)
        trust = json.loads(Path(self.config.trusted_codes).read_text(
            encoding='utf-8'))
        self.baseline = trust['baseline']
        self.cohesion = {c: v for c, v in trust['codes'].items()}
        self.trusted = {c for c, v in trust['codes'].items() if v['trusted']}
        self.by_pub = {}
        for i, r in enumerate(self.rows):
            self.by_pub.setdefault(r['publication_number'], i)
        # trade-label rows for the phase-7 contradiction (labels naming a
        # Gewerk, not a project object): CPV groups 453/454
        self.trade_rows = np.array(
            [j for j, lr in enumerate(self.lrows)
             if lr['code'][:3] in self.config.trade_branches], dtype=int)
        self.trade_trim = [self.lrows[j]['code'].rstrip('0')
                           for j in self.trade_rows]
        self.by_key = {(r['procedure_id'], r['lot_id']): i
                       for i, r in enumerate(self.rows)}
        # cpv + buyer per sidecar row (store read at load time — profile
        # building is model-side runtime, not a customer-number reconstruction)
        # The seven fields below plus KEY are everything a gate reads off the
        # store: the per-row arrays built here, and (evidence mode) the
        # lexicon derivation, which takes `_lots` and touches title,
        # description, buyer_name and the two code columns. Naming them keeps
        # ~130 MB of unrelated object columns out of every gate — and a
        # replay builds one gate per cutoff. doc/MEMORY_BUDGET.md.
        lots = pd.read_parquet(
            Path(data_dir) / 'store' / 'tenders.parquet',
            columns=KEY + ['cpv_main', 'cpv_additional', 'contract_type',
                           'buyer_name', 'publication_date', 'title',
                           'description']).drop_duplicates(subset=KEY)
        key_cpv = dict(zip(zip(lots['procedure_id'], lots['lot_id']),
                           lots['cpv_main']))
        key_buyer = dict(zip(zip(lots['procedure_id'], lots['lot_id']),
                             lots['buyer_name']))
        key_title = dict(zip(zip(lots['procedure_id'], lots['lot_id']),
                             lots['title']))
        key_desc = dict(zip(zip(lots['procedure_id'], lots['lot_id']),
                            lots['description']))
        key_add = dict(zip(zip(lots['procedure_id'], lots['lot_id']),
                           lots['cpv_additional']))
        key_ctype = dict(zip(zip(lots['procedure_id'], lots['lot_id']),
                             lots['contract_type']))
        key_date = dict(zip(zip(lots['procedure_id'], lots['lot_id']),
                            lots['publication_date']))
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
        # phase 9f: hysteresis replays a profile's references in the order
        # they were published, so the core needs each reference's date
        self.all_date = np.array(
            [key_date.get((r['procedure_id'], r['lot_id'])) for r in self.rows],
            dtype=object)
        self.all_title = np.array(
            [key_title.get((r['procedure_id'], r['lot_id'])) for r in self.rows],
            dtype=object)
        self.all_desc = np.array(
            [key_desc.get((r['procedure_id'], r['lot_id'])) for r in self.rows],
            dtype=object)
        self._lots = lots  # evidence-mode lexicon derivation reads the store
        self._pools = None

    def learned_refs(self, sub_id):
        """Publication numbers learned for this subscription on or before
        `as_of`; empty without an `as_of` (see the class docstring)."""
        if not self.as_of:
            return []
        if self._learned is None:
            try:
                import feedback
                self._learned = feedback.read_learned(self.data_dir)
            except Exception:  # a missing/broken ledger is not a cycle failure
                self._learned = []
        import feedback
        return feedback.refs_for(self._learned, sub_id, self.as_of)

    def reload_learned(self):
        """Drop the cached ledger — call after appending within a cycle."""
        self._learned = None

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


def build_profile(gate, sub, config=None):
    """Resolve a subscription's profile against the sidecars. An unresolvable
    profile_ref is an error (RELEVANCE.md: never a silent skip).

    Phase 9: the profile is the subscription's own references UNIONED with
    the customer's learned references — their own wins, recorded by
    feedback.py — as of `gate.as_of`. Without an `as_of` that union is
    empty and this behaves exactly as before. The asymmetry in error
    handling is deliberate: an operator-written ref that does not resolve
    is a mistake and raises, while a learned ref that does not resolve
    (the store was rebuilt, the sidecar lags) is derived data and is
    skipped — a feedback record must never break a customer's delivery."""
    cfg = config or gate.config
    ref_rows = []
    for pub in sub.get('profile_refs') or []:
        if pub not in gate.by_pub:
            raise ValueError(f'profile_ref {pub!r} not in embedding sidecar')
        ref_rows.append(gate.by_pub[pub])
    for pub in gate.learned_refs(sub.get('sub_id')):
        if pub in gate.by_pub:
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
    if cfg.use_expansion:
        for c in {c for c in ref_codes if c in gate.trusted}:
            pool = gate.pools[c][~np.isin(gate.pools[c], ref_rows)]
            if len(pool):
                expanded.append(gate.mat[pool])
    # the two-tier fingerprint (configuration F): hard = facts, soft = guesses
    hard_rows = sorted({gate.lpos[c] for c in ref_codes
                        if c in gate.trusted and c in gate.lpos})
    R = gate.lmat @ ref_vecs.T
    ok = np.flatnonzero((R >= cfg.soft_floor).sum(axis=1) >= cfg.soft_consensus)
    soft_rows = ok[np.argsort(-R.max(axis=1)[ok])][:8]
    # titles aligned with the leading rows of ref_matrix (references, then
    # free texts); pseudo-reference rows beyond them have no title and fall
    # back to a generic "why" — they exist only when USE_EXPANSION is on
    ref_titles = ([str(gate.all_title[i] or '') for i in ref_rows]
                  + [str(t) for t in (sub.get('profile_texts') or [])])
    # phase 7: trade labels with NO ancestor/descendant relation to any hard
    # code — readings there are foreign trade talk and may contradict a
    # hard pass (RELEVANCE.md, the Trafostation case)
    hard_trim = [gate.lrows[r]['code'].rstrip('0') for r in hard_rows]
    foreign = [int(r) for r, t in zip(gate.trade_rows, gate.trade_trim)
               if not any(t.startswith(h) or h.startswith(t)
                          for h in hard_trim)] if hard_trim else []
    keywords, wide, core = None, [], []
    if cfg.mode == 'evidence':
        # phase 8: the profile's trade lexicon (TF-IDF over the reference
        # texts + trusted-label words; buyer-diverse, buyer-name-free),
        # phase 8c: unioned with the store-wide dictionaries of the
        # profile's trusted trades
        import evidence as evd
        docfreq = evd.store_doc_freq(
            gate._lots, Path(gate.data_dir) / 'evidence_df.json')
        refs = [(evd.leistung_text(gate.all_title[i], gate.all_desc[i]),
                 gate.all_buyer[i]) for i in ref_rows]
        refs += [(str(t).casefold(), None)
                 for t in (sub.get('profile_texts') or [])]
        dicts = evd.trade_dictionaries(
            gate._lots, gate.trusted, docfreq,
            Path(gate.data_dir))
        # phase 8r: `name` is the customer's own company name, which states
        # the trade on its letterhead — see evidence.name_keywords()
        keywords = evd.firm_keywords(
            refs, docfreq, [gate.lrows[r]['label_de'] for r in hard_rows],
            # phase 8t: dictionaries are looked up by the profile's DEEP
            # codes, not its trusted ones — trust gates the code-similarity
            # nomination above, never the vocabulary
            # phase 8w: only the codes that RECUR across the firm's wins —
            # cpv_additional names every trade of a procurement, not this
            # firm's part of it
            evd.firm_codes([_codes_of(gate, i) for i in ref_rows]), dicts,
            firm=sub.get('name'))
        # phase 8n: the wide root lexicon — nomination only, never conviction
        wide = evd.wide_keywords(refs)
        # phase 8o: the roots that RECUR across the firm's wins — its trade
        # rather than its context. A core root in the TITLE convicts.
        # ref_titles is aligned with refs (both are ref_rows then
        # profile_texts), which is what lets a ONE-reference profile read
        # its trade off the title instead of the whole document.
        # dates aligned like ref_titles: references then free texts; free
        # texts are operator-written and timeless, so they carry None and
        # sort to the front of the phase-9f replay
        ref_dates = ([gate.all_date[i] for i in ref_rows]
                     + [None] * len(sub.get('profile_texts') or []))
        core = evd.core_keywords(refs, firm=sub.get('name'),
                                 titles=ref_titles, dates=ref_dates)
    return {
        'ref_matrix': np.vstack(expanded),
        'ref_titles': ref_titles,
        'hard_rows': np.array(hard_rows, dtype=int),
        'foreign_trade_rows': np.array(foreign, dtype=int),
        'soft_rows': soft_rows.astype(int),
        'ref_buyers': {b for b in (_norm_buyer(gate.all_buyer[i])
                                   for i in ref_rows) if b},
        'ref_types': {t for t in (gate.all_ctype[i] for i in ref_rows)
                      if isinstance(t, str) and t},
        'min_relevance': float(sub.get('min_relevance', cfg.min_relevance)),
        'min_code_hard': float(sub.get('min_code_hard', cfg.min_code_hard)),
        'min_code_soft': float(sub.get('min_code_soft', cfg.min_code_soft)),
        'version': sub.get('version', 1),
        'keywords': keywords,
        'keywords_wide': wide,
        'keywords_core': core,
        # the configuration this profile was DERIVED under (its lexicon and
        # soft fingerprint depend on it); judge() may still be asked to decide
        # under another, which is exactly what the nomination-bar sweep does
        'config': cfg,
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


def corroborated(gate, profile, i, config=None):
    """Phase 5: may a soft-only code pass stand? Only if the candidate's own
    text reads as one of the profile's hard trade labels. Without hard labels
    (cold start) there is no fact to corroborate against — rule inactive."""
    cfg = config or gate.config
    if cfg.trade_read_form == 'off' or not len(profile['hard_rows']):
        return True
    tread, world = trade_read(gate, profile, i)
    if cfg.trade_read_form == 'H1':
        return tread >= cfg.trade_read_param
    return tread >= world - cfg.trade_read_param


_SYN = None


def _syn_tier(gate):
    """Lazy word-vector synonym matcher (evidence tier 3), sharing the
    persistent vocabulary cache with the receipt harness."""
    global _SYN
    if _SYN is None:
        import evidence as evd
        _SYN = evd.SynonymTier(
            Path(gate.data_dir) / 'embeddings' / 'word_vecs.npz')
    return _SYN


def evidence_for(gate, profile, i):
    """Phase 8: the (keyword, found_word, tier) matches of a lot's
    title+Leistung against the profile lexicon; [] without a lexicon."""
    import evidence as evd
    return evd.match_evidence(
        evd.leistung_text(gate.all_title[i], gate.all_desc[i]),
        profile.get('keywords') or [], syn=_syn_tier(gate))


def evidence_components(gate, profile, scored_row, i):
    """The raw inputs of the phase-8 decision for one lot, computed by the
    same code the runtime ladder runs — exposed so the nomination-bar sweep
    (evidence.py --sweep) measures the shipped code, not a replica.
    -> (text, c_hard, type_mismatch, same_buyer, ev)."""
    sims = profile['ref_matrix'] @ gate.mat[i]
    text = float(sims.max())
    c_hard = 0.0
    if len(profile['hard_rows']):
        for cr in [gate.lpos[c] for c in _codes_of(gate, i)
                   if is_deep(c) and c in gate.lpos]:
            s = float((gate.lmat[profile['hard_rows']] @ gate.lmat[cr]).max())
            c_hard = max(c_hard, s)
    ctype = gate.all_ctype[i]
    mismatch = bool(profile['ref_types'] and isinstance(ctype, str) and ctype
                    and ctype not in profile['ref_types'])
    same_buyer = _norm_buyer(scored_row.get('buyer_name')) in profile['ref_buyers']
    # a type-mismatched lot is borderline before evidence is consulted —
    # skip the (comparatively costly) evidence match
    ev = [] if mismatch else evidence_for(gate, profile, i)
    return text, c_hard, mismatch, same_buyer, ev


def _evidence_verdict(profile, text, c_hard, same_buyer, has_evidence,
                      bar=None, nomination_min=None, witnesses=0,
                      convicting=None, wide_witnesses=0, config=None):
    """The phase-8 decision itself, pure arithmetic on the components —
    shared by the runtime ladder and the sweep. NOMINATE by a hard
    trusted-code match, or by the evidence itself when it carries enough
    distinct witnesses (phase 8b, EVIDENCE_NOMINATION_MIN) — and, only
    when SIMILARITY_NOMINATES is set back on, by text similarity against
    NOMINATION_BAR (phase 8i removed that route: it was invisible to the
    test and guarded by a buyer-name string match); CONVICT by
    conviction-strength evidence (phase 8c(3): title witness or multiple
    distinct keywords; `convicting=None` falls back to any-evidence).
    Nominated with sub-conviction evidence, nominated without evidence,
    or evidence without nomination: borderline, visibly undecided.
    -> (passed, borderline)."""
    cfg = config or profile.get('config') or DEFAULT_CONFIG
    if bar is None:
        bar = cfg.nomination_bar
    if nomination_min is None:
        nomination_min = cfg.evidence_nomination_min
    if convicting is None:
        convicting = has_evidence
    nominated = (c_hard >= profile['min_code_hard']
                 # phase 8i: route 2 (similarity) is off by default — see
                 # SIMILARITY_NOMINATES. `same_buyer` survives only to keep
                 # the rollback arm honest; nothing else reads the buyer.
                 or (cfg.similarity_nominates and not same_buyer and text >= bar)
                 or (nomination_min > 0 and witnesses >= nomination_min)
                 # phase 8k: what convicts may also nominate. NB when the
                 # caller passes convicting=None this falls back to plain
                 # has_evidence, i.e. the any-evidence-nominates diagnostic
                 # (8.7% leakage) — the runtime always passes the real
                 # title-or-two value, the sweep must do so too.
                 or (cfg.conviction_nominates and convicting)
                 # phase 8n: the WIDE root lexicon may nominate but never
                 # convict. A guardrail firm's texts carry 'beton' because
                 # the posts are set in it, so a concrete tender becomes a
                 # candidate — and then has to convict on the firm's own
                 # narrow vocabulary, which it will not carry.
                 or (nomination_min > 0 and wide_witnesses >= nomination_min))
    if nominated and convicting:
        return True, False
    if nominated or has_evidence:
        return False, True
    return False, False


def evidence_witnesses(ev):
    """Distinct keywords found by the exact/typo tiers — the witness count
    of the phase-8b nomination rule (synonym hits don't nominate)."""
    return len({kw for kw, _, t in ev if t <= 2})


def _judge_evidence(gate, profile, scored_row, i, config=None):
    """Phase-8 ladder (GATE_MODE='evidence'): components + verdict, see
    evidence_components() / _evidence_verdict(). Same 6-tuple as the
    embedding ladder."""
    import evidence as evd
    cfg = config or gate.config
    text, c_hard, mismatch, same_buyer, ev = evidence_components(
        gate, profile, scored_row, i)
    if mismatch:
        passed, borderline = False, True
    else:
        # phase 8n: the wide lexicon is matched separately and feeds ONLY
        # the nomination count — `ev` (narrow) still decides conviction
        wide = profile.get('keywords_wide') or []
        ev_wide = evd.match_evidence(
            evd.leistung_text(gate.all_title[i], gate.all_desc[i]),
            wide) if wide else []
        # phase 8o: a recurring trade root in the TITLE convicts too
        core = profile.get('keywords_core') or []
        title_f = evd.fold(str(gate.all_title[i] or '').casefold())
        core_title = any(r in title_f for r in core)
        convicting = evd.convicts(gate.all_title[i], ev) or core_title
        # phase 9g: a lone NON-core keyword in the title is a coin flip
        # (census: 18 in / 16 out on the hand-read cases), so alone it no
        # longer convicts — it needs the hard code channel to agree. A core
        # root in the title (88% right) and a two-keyword body convict
        # exactly as before.
        if (evd.LONE_TITLE_NEEDS_CODE and convicting and not core_title
                and evidence_witnesses(ev) < evd.CONVICT_BODY_MIN
                and not c_hard >= profile['min_code_hard']):
            convicting = False
        # phase 9i: a body conviction under a title that names a trade the
        # profile does not know is an inventory lot mentioning the firm's
        # words. A lexicon keyword in the title or the hard code channel
        # lifts the veto (adjacency — Rohbau firms really win Abbruch lots).
        if (evd.TITLE_CONTRADICTS_BODY and convicting and not core_title
                and not evd.title_witness(gate.all_title[i], ev)
                and not c_hard >= profile['min_code_hard']
                and any(evd.roots_in(w)
                        for w in set(evd.tokens(title_f)))):
            # phase 9j: the embedding may lift the veto where the lexicon
            # cannot — a lot whose own text READS as the profile's hard
            # trade (within the phase-7 margin of its best reading
            # anywhere) is that trade despite the foreign title. A profile
            # without hard labels has tread 0 and is never forgiven.
            tread, world = trade_read(gate, profile, i)
            if not (evd.TRADE_READ_FORGIVES
                    and cfg.trade_talk_margin is not None
                    and world - tread < cfg.trade_talk_margin):
                convicting = False
        passed, borderline = _evidence_verdict(
            profile, text, c_hard, same_buyer, bool(ev) or core_title,
            witnesses=evidence_witnesses(ev),
            convicting=convicting,
            wide_witnesses=evidence_witnesses(ev_wide), config=cfg)
    # phase 8d: deterministic partial admit of the borderline band
    if (not passed and borderline and cfg.borderline_admit_p > 0
            and _band_draw(scored_row['procedure_id'],
                           scored_row['lot_id']) < cfg.borderline_admit_p):
        passed, borderline = True, False
    why = None
    if passed and ev:
        words = ', '.join(dict.fromkeys(w for _, w, _ in ev))
        why = ('evidence', words[:80])
    return passed, borderline, text, c_hard, why, c_hard


def trade_talk_contradicted(gate, profile, i, config=None):
    """Phase 7: does the candidate's own text confidently name a DIFFERENT
    trade than the hard code claims? Project talk (object labels) and
    same-family trade talk (ancestors/siblings by code prefix — the CPV
    hierarchy) never contradict; only a foreign trade reading beating the
    profile-trade reading by TRADE_TALK_MARGIN does."""
    cfg = config or gate.config
    if (cfg.trade_talk_margin is None or not len(profile['hard_rows'])
            or not len(profile.get('foreign_trade_rows', ()))):
        return False
    reads = gate.lmat @ gate.mat[i]
    foreign = float(reads[profile['foreign_trade_rows']].max())
    tread = float(reads[profile['hard_rows']].max())
    return foreign - tread >= cfg.trade_talk_margin


def judge(gate, profile, scored_row, config=None):
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
         passes are untouched — a derived signal may demote a guess, never
         override the text the bidder actually reads.
      6. (phase 7, across rules 3 and 4) A HARD pass whose text confidently
         talks about a foreign trade demotes to the borderline band (the
         Trafostation case: a transformer-station lot wearing the
         customer's Blitzschutz code). Project talk and same-family trade
         talk never trigger it (see trade_talk_contradicted()); a text
         pass still overrides — the amended asymmetry: a fact contradicted
         by the lot's own confident testimony no longer stands unopposed.

    The code channel reads ALL the lot's codes (main + additional), scored
    separately against the HARD fingerprint (trusted codes on actual wins —
    facts) and the SOFT one (labels inferred from reference texts — guesses,
    higher bar via calibration, and never pick-confident). A code is
    "silent" only if none is deep and known.

    Returns (passed, borderline, text, code, why, code_hard) — code is the
    stamped best of both tiers, code_hard feeds is_confident.
    """
    cfg = config or gate.config
    i = gate.by_key.get((scored_row['procedure_id'], scored_row['lot_id']))
    if i is None:
        return True, False, None, None, None, 0.0  # rule 1: fail-open
    if cfg.mode == 'evidence':
        return _judge_evidence(gate, profile, scored_row, i, config=cfg)
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
        if soft_only and not corroborated(gate, profile, i, cfg):
            return False, True, text, c_score, None, c_hard  # rule 5
        if hard_pass and trade_talk_contradicted(gate, profile, i, cfg):
            return False, True, text, c_score, None, c_hard  # rule 6
        return code_pass, False, text, c_score, (why_code() if code_pass else None), c_hard

    # rule 4
    passed = text >= profile['min_relevance'] or code_pass
    if (passed and text < profile['min_relevance'] and soft_only
            and not corroborated(gate, profile, i, cfg)):
        return False, True, text, c_score, None, c_hard  # rule 5
    if (passed and text < profile['min_relevance'] and hard_pass
            and trade_talk_contradicted(gate, profile, i, cfg)):
        return False, True, text, c_score, None, c_hard  # rule 6
    borderline = (not passed
                  and text >= profile['min_relevance'] - cfg.borderline_margin)
    why = (why_text() if text >= profile['min_relevance']
           else why_code() if code_pass else None)
    return passed, borderline, text, c_score, why, c_hard


if __name__ == '__main__':
    import argparse as _ap
    from datetime import date as _date
    _p = _ap.ArgumentParser(description=_print_config.__doc__)
    _p.add_argument('--check-profiles', action='store_true',
                    help='build the profile of every live subscription and '
                         'name the ones that can never recommend anything')
    _p.add_argument('--data-dir', default=config.data_root())
    _p.add_argument('--as-of', default=_date.today().isoformat())
    _a = _p.parse_args()
    if _a.check_profiles:
        _check_profiles(_a.data_dir, _a.as_of)
    else:
        _print_config()
