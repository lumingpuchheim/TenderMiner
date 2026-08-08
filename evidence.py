"""TenderMining evidence gate — RELEVANCE.md phase 8 runtime and receipts.

TF-IDF derives each profile's trade keywords from the texts of the
customer's won tenders (frequent in their wins, rare in the store), plus
the words of their trusted CPV labels. A candidate is convicted
(recommendable) only when its title + Leistung section carries evidence of
the trade. Matching is three-tiered, cheapest first, every match quotable:

  1. exact  — case-folded substring; German compounding makes stems strong
              ("blitzschutz" hits Blitzschutzanlage, Gebäudeblitzschutz)
  2. typo   — bounded edit distance (<=1) against same-length tokens
  3. synonym — the keyword's embedding vs embeddings of the description's
              WORDS (word granularity carries no project frame)

Usage:
    python evidence.py --benchmark            # the committed labeled cases
    python evidence.py --receipt              # LOO recall / leakage / volume
    python evidence.py --receipt --tier3      # + synonym tier on failures
    python evidence.py --keywords "Jebsen GmbH"   # show a firm's derived list
"""

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
from ftfy import fix_text

import config

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

KEY = ['procedure_id', 'lot_id']
MIN_STEM_LEN = 6      # shorter stems risk false substring hits
MAX_KEYWORDS = 25
MAX_DOC_FREQ = 0.02   # keyword must appear in <= 2% of store lots
# References that must contain a keyword. 1 (decision 2026-08-06, operator):
# the two-witness rule starved 2-wins-per-trade profiles (the Ahle carpentry
# case — 'zimmer' had one witness and died); receipts: recall 51.7% -> 54.8%
# (59.0% with the synonym tier) for +1.1pt conviction-only leakage, benchmark
# untouched at 19/19. The buyer-diversity rule still guards multi-buyer
# profiles against template words.
MIN_WITNESSES = 1
TYPO_MIN_LEN = 8      # typo tier only for long keywords
SYN_THRESHOLD = 0.80  # word-embedding cosine for the synonym tier
# Phase 8c (operator decision 2026-08-06: recall first, no more threshold
# tinkering — fix the lexicon). Two structural changes:
# (1) DEFINITIONAL WAIVER: the store-rarity sieve (MAX_DOC_FREQ) deletes
#     the trade's own name for exactly the biggest trades (receipt:
#     malerarbeiten 2.0%, sanitär 2.6%, heizung 2.6%, lüftung 2.1%,
#     abbruch 4.8% — all killed; a trade's name is common in proportion
#     to its market share). A word from the profile's own trusted-code
#     labels that names FEW trades (label-space rarity, <= LABEL_DF_MAX
#     of the CPV dictionary's labels) is definitional, not distinctive,
#     and enters regardless of store frequency.
# (2) TRADE DICTIONARIES: each trusted trade's vocabulary derived from
#     ALL store lots carrying the code — frequent inside the trade
#     (>= DICT_MIN_IN of its lots), rare outside (ratio >= DICT_MIN_RATIO)
#     — so a 3-win firm inherits the vocabulary of hundreds of lots
#     instead of five documents. The two-sided in/out test replaces the
#     crude store-wide cutoff, which cannot tell 'neubau' (common
#     everywhere) from 'malerarbeiten' (common only inside painting).
# (3) TITLE-OR-TWO CONVICTION: richer lexicons surfaced the sub-scope
#     coincidence the spec left open as risk (b) — the Blitzschutz
#     dictionary word 'ableitung' convicted the Kreishaus-Starkstrom
#     benchmark lot via "rauchableitung" in the LV fine print. The
#     operator principle, extended: one coincidence in the fine print is
#     a coincidence; the TITLE naming the trade (exact tier), or >=
#     CONVICT_BODY_MIN distinct keywords, is a conviction. Evidence
#     below conviction strength still lands the lot in the borderline
#     band (visible), never a pick.
TRADE_DICTS = True    # rollback switch for (2)
CONVICT_BODY_MIN = 2  # body-only conviction needs this many distinct kws
LABEL_DF_MAX = 5      # definitional = names <= this many CPV labels
DICT_MIN_LOTS = 20    # trades with fewer coded lots keep no dictionary
# Phase 8t (2026-08-07). A dictionary used to be built only for a TRUSTED
# code. Trust means the code's lot texts sit close together in embedding
# space -- a test invented for a different job (certifying clean negatives
# when calibrating the threshold) and reused here as a gate on vocabulary,
# which it says nothing about. A trade whose projects genuinely vary
# (Landschaftsbau: parks, schools, roadsides) scores low on cohesion while
# its vocabulary -- pflanz, gehoelz, entwicklungspflege -- is perfectly
# sharp. Measured cost of the borrowed gate: 212 deep codes exist, 56 are
# trusted, 47 dictionaries get built; 135 codes carry >= DICT_MIN_LOTS lots
# but only 33 of those are trusted. The 102 excluded are the major trades --
# Metallbau 702 lots, HLK 579, Rohbau 513, Elektroinstallation 499, Fassade
# 443, Landschaftsbau 421, Zimmer/Schreiner 351, Bahnbau 347, Aufzuege 245.
# And the effect is total: of 261 firms holding a trusted code NONE had an
# empty lexicon; of the 251 without, 101 did.
# The dictionary's OWN tests are the ones that matter and they stay:
# DICT_MIN_IN, DICT_MIN_RATIO, the buyer-diversity pair, and names_trade().
# Rollback: DICT_TRUSTED_ONLY=1.
#
# SHIPS DORMANT (2026-08-07). Removing the gate is right on the merits and
# wrong as it stands, because a firm's code set has the SAME defect the
# pool had: a lot carries every trade of its procurement in cpv_additional,
# so Heberger -- three won tenders in wastewater plants -- inherits seven
# dictionaries spanning fire alarms, building automation, lightning
# protection and structural concrete, 24 words, and convicts on all of
# them. Measured cost, voting held on in both arms: OUT 45/52 -> 36/52.
# Turn this on once a firm's trade is taken from the cpv_main of the lots
# it actually won, not from every code listed on the procurement.
DICT_TRUSTED_ONLY = os.environ.get('DICT_TRUSTED_ONLY', '0') != '0'
# Phase 8w — A CODE MUST RECUR ACROSS THE FIRM'S WINS. The third and last
# place the same principle was missing. A word must recur across the firm's
# references (8q); a lot must agree with its code's majority (8u); but a
# firm inherited a dictionary for EVERY code appearing on any lot it won a
# part of. Those codes come from cpv_additional, which lists every trade in
# the procurement -- so Heberger, three won tenders all in wastewater
# plants, carried seven dictionaries spanning fire alarms, building
# automation, lightning protection and structural concrete. Its 24 words
# were every one of them CORRECT for their trades; the error was that six
# of those trades are not Heberger's. No threshold on word quality can see
# that, which is why tightening DICT_MIN_IN could not fix it.
# Its wastewater code is on 3 of 3 wins; its fire-alarm code on 1. One
# recurs, the other is context.
# 0 disables (every code counts, the phase-8t behaviour).
DICT_CODE_SHARE = float(os.environ.get('DICT_CODE_SHARE', '0.34'))


def firm_codes(code_lists, share=None):
    """Phase 8w: which of a firm's CPV codes are its trade rather than the
    context of somebody else's procurement. `code_lists` is one list of
    codes per won lot. A code survives if it appears on at least `share` of
    them (always at least 2 lots, and a 1-win firm keeps everything it
    has)."""
    from calibrate import is_deep
    share = DICT_CODE_SHARE if share is None else share
    n = len(code_lists)
    seen = Counter()
    for cs in code_lists:
        seen.update({c for c in cs if is_deep(c)})
    if not share or n < 2:
        return set(seen)
    need = max(2, int(n * share + 0.999))
    return {c for c, k in seen.items() if k >= need}
# Phase 8u (operator idea 2026-08-07): THE POOL VOTES. A dictionary is only
# as good as the lots filed under its code, and most of them are not filed
# under it deliberately -- measured on 45261420 (Abdichtungsarbeiten gegen
# Wasser): 17 lots arrive via cpv_main and 88 via cpv_additional, 84% of the
# pool, carrying titles like "Rohbauarbeiten 3. Bauabschnitt" and
# "Spraypark". A procurement lists every trade it contains as an additional
# code on each of its lots, so the same lots land in a dozen trades' pools
# at once. That is CORRELATED miscoding: DICT_MIN_IN and DICT_MIN_RATIO are
# built for noise and this is not noise.
#
# The operator's observation is the fix: wrongly-filed lots are wrong in
# every direction at once (rohbau, tiefbau, spraypark) while correctly-filed
# ones all say the same thing, so the right trade OUTVOTES the wrong ones.
# Each lot votes with the trade roots in its text; the roots that carry the
# vote are the code's signature; a lot holding none of them is not this
# trade and leaves the pool before any word is counted. Same recurrence
# principle as core_keywords(), one level up: the trade recurs across the
# code's lots, the miscoded ones scatter.
# Rollback: DICT_VOTE=0.
DICT_VOTE = os.environ.get('DICT_VOTE', '1') != '0'
DICT_VOTE_MARGIN = 0.5   # signature = roots polling >= this share of the top
DICT_VOTE_MAX = 6        # ... at most this many, so a signature stays a signature
# Phase 8v (operator decision 2026-08-07): a dictionary word must appear in
# ALL of the trade's surviving lots, not 10% of them. The 10% bar was set
# when the pool was up to 84% miscoded; phase 8u's vote now removes the lots
# that disagree, and the operator's rule for what remains is stricter than
# frequency: "what if someone sneaks a trade that is not relevant to the
# code. he just makes a mistake. we respect the most of his work, but not
# all" -- the vote keeps a lot whose trade agrees, intersection then drops
# the off-trade words that lot happened to carry.
# Env-overridable so the share can be swept without an edit.
DICT_MIN_IN = float(os.environ.get('DICT_MIN_IN', '0.10'))
DICT_MIN_RATIO = 8.0  # ... and >= 8x as often inside as outside
# Phase 8f (2026-08-07) — ONE principle, applied in the two places it was
# missing: **a trade word travels between buyers; an office's house style
# does not**. Stated in derive_keywords since phase 8, but enforced in
# neither of the two paths that actually build a lexicon.
#
# (A) TRADE DICTIONARIES counted lots, so one buyer's template could carry
#     a word past DICT_MIN_IN on its own. Measured on the live store: in
#     45233280 (Leitplanken, 30 lots / 13 buyers) 'kurzfristig',
#     'einwirkung' and 'leichtigkeit' each sit in 26.7% of LOTS but come
#     from ONE buyer (7.7%), while the real trade word 'anpralldämpfer'
#     spans 3 buyers; in 45432100 (Bodenbelag) 'behörde'/'berufsbildung'
#     are one buyer's boilerplate at 3.0% of lots, against 'bodenbelag'
#     80 buyers, 'linoleum' 56, 'kautschuk' 41. Buyer share separates the
#     two populations where lot share cannot, so a dictionary word must
#     now also clear DICT_MIN_BUYER_SHARE of the trade's buyers and come
#     from at least DICT_MIN_BUYERS of them — one buyer is not evidence.
# (B) was a companion rule for single-buyer profiles, WITHDRAWN in phase
#     8h -- the trade vocabulary subsumes it. See derive_keywords.
#
# Whether (A) still earns its place is an open question of the same kind:
# every dictionary word it removes (kurzfristig, erfahrungswert,
# fabrikneu, mengenansaetz, beschleunigt) ALSO fails names_trade(), so the
# vocabulary may subsume it too. Kept for now because it costs almost
# nothing, and measurable either way: BUYER_DIVERSITY=0 with TRADE_ROOTS=1
# is the arm that answers it (lexicon_receipt.py runs it as 'roots only').
DICT_MIN_BUYERS = 2        # a dictionary word needs >= this many buyers
DICT_MIN_BUYER_SHARE = 0.10  # ... and >= this share of the trade's buyers
# Phase 8q — RECURRENCE, stated directly. The share of a firm's references
# a word must appear in to enter its lexicon; 0 leaves MIN_WITNESSES alone.
# This replaces the withdrawn word-level buyer rule (see derive_keywords),
# which was recurrence measured through buyers instead of through
# references and cost every multi-buyer firm its single-witness words.
# Receipt (lexicon_receipt.py --config both, 122 cases):
#   buyer rule (withdrawn)   IN 44/74  OUT 45/52
#   share 0     (no rule)    IN 49/74  OUT 38/52
#   share 0.34  (shipped)    IN 44/74  OUT 47/52
#   share 0.5                IN 44/74  OUT 47/52
#   share 0.75               IN 42/74  OUT 47/52
# 0.34 and 0.5 tie on the benchmark because they differ only above four
# references (both demand 2 of 3 and 2 of 4); 0.34 is chosen for leaving
# fewer firms empty (103 against 125). Rollback: WORD_MIN_REF_SHARE=0.
WORD_MIN_REF_SHARE = float(os.environ.get('WORD_MIN_REF_SHARE', '0.34'))
# Phase 8g (2026-08-07) — THE TRADE-ROOT VOCABULARY. A lexicon word must
# name a Gewerk or a material. CPV-45 cannot supply that list directly: 484
# of its 822 labels name what is BUILT (Schulgebaeude, Polizeireviere) and
# only 330 name the WORK, so a vocabulary taken from the whole division
# admits 'schulen' and 'behoerde' alongside 'estricharbeiten'. The trade
# side was separated by reading, then reduced to ROOTS, because German
# carries the trade in the compound's modifier: Holz|bau, Holz|arbeiten and
# Brettsperr|holz share only 'holz'. Generic heads (bau, arbeiten, anlagen,
# installation) are absent by design. See cpv_trade_roots.txt for the list
# and its rationale; a candidate survives if it CONTAINS a root, folded for
# umlauts so Tuer/Tür and Fussboden/Fußboden need one entry each.
TRADE_ROOTS = os.environ.get('TRADE_ROOTS', '1') != '0'
ROOTS_FILE = Path(__file__).with_name('cpv_trade_roots.txt')
# Phase 8n: a SECOND, wider lexicon of trade roots, used only to NOMINATE.
# Conviction keeps the narrow firm_keywords() lexicon. See wide_keywords().
# Rollback: WIDE_NOMINATION=0.
WIDE_NOMINATION = os.environ.get('WIDE_NOMINATION', '1') != '0'
# Phase 8o: a recurring trade root in the TITLE convicts. Since phase 8k
# conviction is the only stage that decides anything (convicting implies
# nominated), so it is the only place a change can move the receipt.
# CORE_SHARE = the share of the firm's references a root must appear in to
# count as its trade rather than context. Rollback: CORE_TITLE_CONVICTS=0.
# Phase 8r: the firm's own name is a lexicon source. See name_keywords().
# Rollback: NAME_KEYWORDS=0.
NAME_KEYWORDS = os.environ.get('NAME_KEYWORDS', '1') != '0'
# Phase 8s: the narrow lexicon counts ROOTS, not surface forms — German
# writes one trade three ways across three tenders. See derive_keywords.
# Rollback: ROOT_LEXICON=0.
ROOT_LEXICON = os.environ.get('ROOT_LEXICON', '1') != '0'
CORE_TITLE_CONVICTS = os.environ.get('CORE_TITLE_CONVICTS', '1') != '0'
CORE_SHARE = float(os.environ.get('CORE_SHARE', '0.5'))
# rollback switch for phase 8f (A) and (B); env var overrides per run so the
# A/B needs no edit (BUYER_DIVERSITY=0 reproduces the phase-8e lexicons)
BUYER_DIVERSITY = os.environ.get('BUYER_DIVERSITY', '1') != '0'
# Bumped by phase 8f (A), then by 8t. A NEW KEY FIELD IS NOT ENOUGH on its
# own: `bool(d.get('to'))` is False for a cache written before the field
# existed, which is exactly the value the new default carries, so a stale
# trusted-only cache matched the key and was silently reused. Any change to
# what the dictionaries contain must bump this version too.
DICT_CACHE_V = 6
DICT_MAX_WORDS = 30   # per-trade cap, keeps lexicons readable
SEED = 7              # mirrors calibrate.py sampling
NEG_PER_FIRM = 50
VOL_PER_FIRM = 200
MIN_WINS = 3

WORD_RE = re.compile(r'[a-zäöüß]{3,}')
LEISTUNG_RE = re.compile(
    r'(?:kurz)?beschreibung der (?:hier )?ausgeschriebenen leistung\s*:?'
    r'|beschreibung der leistung(?:en)?\s*:?'
    r'|art und umfang der leistung(?:en)?\s*:?'
    r'|leistungsumfang\s*:',
    re.IGNORECASE)


def leistung_text(title, description):
    """Title + the Leistung section of the description; the whole
    description when no section structure is detectable (fail-open).
    Case-folded — ready for matching."""
    desc = fix_text(str(description or ''))
    m = None
    for m in LEISTUNG_RE.finditer(desc):
        pass  # the LAST marker wins: project prose comes first
    body = desc[m.end():] if m else desc
    return (fix_text(str(title or '')) + '\n' + body).casefold()


def tokens(text):
    return WORD_RE.findall(str(text).casefold())


def stem(word, known=None):
    """Light German plural/ending trim so a plural-only keyword still
    substring-matches the singular in a candidate text.

    Phase 8m — `known` (the store's token -> count map) makes the trim
    EVIDENCE-BASED. Without it this is a length-gated blind chop of a
    trailing e/s/n that knows nothing about German: Beton -> beto,
    Fassade -> fassad, Kabelrinne -> kabelrinn, Balkon -> balko. In each
    the letter is part of the root, not a suffix. The length gate makes it
    incoherent as well, leaving 'fliese' (6) alone while cutting
    'bodenfliese' (11) to 'bodenflies' — one word stemmed two ways
    depending on the compound it sits in, which is the opposite of what a
    family collapse is for.

    With `known`, a suffix comes off only when what remains is itself a
    token the store actually uses: 'bodenfliesen' -> 'bodenfliese'
    survives because that word exists, while 'ortbeton' -> 'ortbeto' does
    not, so 'ortbeton' stays whole and still contains the root 'beton'.
    No language model and no dependency — the corpus decides.

    The trim is only ever needed in one direction: matching is substring,
    so a stored singular already matches a plural in the candidate text.
    """
    if known is not None:
        for cut in (2, 1):
            if len(word) - cut < MIN_STEM_LEN:
                continue
            if cut == 2 and not word.endswith('en'):
                continue
            if cut == 1 and not word.endswith(('e', 's', 'n')):
                continue
            if word[:-cut] in known:
                return word[:-cut]
        return word
    # legacy blind chop, kept for callers with no corpus at hand
    if len(word) > 8 and word.endswith('en'):
        return word[:-2]
    if len(word) > 7 and word.endswith(('e', 's', 'n')):
        return word[:-1]
    return word


def fold(w):
    """Umlaut/eszett folding so one root entry covers both spellings —
    the corpus writes Fußboden and Fussboden, Tür and Tuer."""
    return (str(w).replace('ä', 'ae').replace('ö', 'oe').replace('ü', 'ue')
            .replace('ß', 'ss'))


_ROOTS = None


def trade_roots():
    """The committed trade-root vocabulary (phase 8g), folded.
    -> (roots, exceptions); "-" lines are compound collisions to reject."""
    global _ROOTS
    if _ROOTS is None:
        roots, nots = set(), set()
        for ln in ROOTS_FILE.read_text(encoding='utf-8').splitlines():
            ln = ln.strip().casefold()
            if not ln or ln.startswith('#'):
                continue
            (nots if ln.startswith('-') else roots).add(fold(ln.lstrip('-')))
        _ROOTS = (tuple(sorted(roots, key=len)), tuple(sorted(nots, key=len)))
    return _ROOTS


def roots_in(w):
    """Which trade roots does this word contain? Substring against the
    committed list, so plurals and compounds need no entry of their own —
    minus the listed collisions (Klassen|zimmer is a room, Hain|holz is a
    village). Holzbauarbeiten -> ['holz'], Ortbeton -> ['beton']."""
    roots, nots = trade_roots()
    f = fold(str(w).casefold())
    if any(x in f for x in nots):
        return []
    return [r for r in roots if r in f]


def names_trade(w):
    """Does this word name a trade or a material?"""
    if not TRADE_ROOTS:
        return True
    return bool(roots_in(w))


def store_doc_freq(tenders, cache_path=None):
    """token -> number of store lots containing it (plus lot count).
    Derived data; cached because it only changes when the store grows."""
    if cache_path and Path(cache_path).exists():
        return json.loads(Path(cache_path).read_text(encoding='utf-8'))
    lots = tenders.drop_duplicates(subset=KEY)
    df = Counter()
    for r in lots.itertuples(index=False):
        df.update(set(tokens(fix_text(
            str(r.title or '') + ' ' + str(r.description or '')))))
    out = {'n': int(len(lots)), 'df': dict(df)}
    if cache_path:
        Path(cache_path).write_text(json.dumps(out), encoding='utf-8')
    return out


def derive_keywords(refs, docfreq, label_texts=(), reasons=None,
                    sources=None):
    """The TF-IDF sieve over (text, buyer) references: a stem survives when
    it appears in enough references (WORD_MIN_REF_SHARE — the trade recurs
    across a firm's wins, the context of any one project does not), is not
    a word of the buyer's own NAME, and is rare in the store. Label words
    (from the profile's trusted CPV labels) pass the same rarity filter.
    The final list keeps the shortest stem of every family and is small
    enough to read aloud.

    Phase 8h: single-buyer profiles contribute their own words again —
    the trade vocabulary vets each word directly, so the blanket rule of
    phase 8f (B) was redundant and cost recall (see the constants block).

    `reasons` (phase 8p diagnostic): an optional dict that receives
    word -> the name of the FIRST filter that rejected it. A side channel
    only — it changes nothing about what is returned, so the attribution
    is of the shipped sieve rather than a replica of it. Written because
    an empty lexicon says which words survived and never which filter
    killed the rest, and 45 firms were empty before the vocabulary
    existed at all."""
    n_docs, df = docfreq['n'], docfreq['df']
    in_refs = Counter()
    # buyer-name words describe WHO, not WHAT — "hamburg" from
    # "Schulbau Hamburg" is geography, never trade evidence
    buyer_words = {w for _, b in refs for w in tokens(str(b or ''))}
    # Phase 8s — COUNT ROOTS, NOT SURFACE FORMS (operator decision
    # 2026-08-07: "i dont like matching raw token at all ... people write in
    # different ways and you miss all of them").
    #
    # Braun GmbH is the case. Its four references say Wärmepumpenanlage,
    # Wärmepumpen and Wärmepumpe — one trade word, three surface forms,
    # because German compounds it differently every time. Counted as
    # tokens that is three words with one mention each, and the recurrence
    # rule refused all three; counted as ROOTS it is `pump` in three
    # references out of four. The proof was already in the code: Braun's
    # core list (root-counted) reads `pump daemm leitung` while its narrow
    # lexicon (token-counted) was empty. Every other list in the system —
    # core, wide, the dictionaries — is already canonical; derive_keywords
    # was the last one measuring the surface.
    #
    # Nothing is lost by canonicalising: a token carrying no root fails
    # names_trade() anyway, so it could never have entered the lexicon.
    if ROOT_LEXICON and TRADE_ROOTS:
        for t, _b in refs:
            in_refs.update({r for w in set(tokens(t)) for r in roots_in(w)})
    else:
        for t, _b in refs:
            in_refs.update(set(tokens(t)))
    min_refs = min(MIN_WITNESSES, len(refs)) or 1
    if WORD_MIN_REF_SHARE:
        min_refs = max(min_refs, min(len(refs), int(
            len(refs) * WORD_MIN_REF_SHARE + 0.999)))
    # Phase 8h (2026-08-07): the single-buyer rule of phase 8f (B) is
    # WITHDRAWN. It stripped a one-buyer profile of its own text entirely,
    # on the grounds that nothing there could be told apart from the
    # buyer's template. The trade vocabulary (phase 8g) answers that
    # question directly instead: every word polat-real-estate's SBH
    # boilerplate contributed -- behoerde, berufsbildung, landesbetrieb,
    # nachstehend, bewirtschaft, hansestadt, schulen -- fails names_trade()
    # on its own, so the rule was solving a problem the word list already
    # solves, while discarding real trade words from any firm that wins
    # mostly from one authority. Buyer diversity was a PROXY for "is this a
    # trade word"; with the direct answer available the proxy gives way.
    #
    # Phase 8q (operator decision 2026-08-07): the WORD-level buyer rule is
    # withdrawn too, on the rule's own terms rather than on a receipt. It
    # required each word to appear under two of the firm's buyers, which
    # (a) silently overrode MIN_WITNESSES = 1, so the phase-8b decision to
    # let a single witness count was never in force for any multi-buyer
    # firm, and (b) measured, by attribution, as the single largest cause
    # of empty lexicons: 3,262 of 4,691 rejected words across the 45 firms
    # that were empty before the vocabulary existed. What it was really
    # doing is RECURRENCE -- a word under two buyers is a word that recurs
    # across projects -- which is phase 8o's distinction (the trade recurs,
    # the context varies) wearing a buyer's name. So recurrence is stated
    # directly, over references, in min_refs above.
    cands = {}
    def reject(w, why):
        if reasons is not None:
            reasons[w] = why
        return True
    rooted = bool(ROOT_LEXICON and TRADE_ROOTS)
    for w, c in in_refs.items():
        # the conditions are spelled out one per line so `reasons` can name
        # the first one that fired; the conjunction is unchanged
        if c < min_refs and reject(w, 'too few references'):
            continue
        if w in buyer_words and reject(w, 'buyer name'):
            continue
        if rooted:
            # a root needs none of the surface-form guards. MIN_STEM_LEN
            # protects substring matching against accidental hits
            # ('glas' in Glasgow) and the roots file carries that duty
            # itself, by hand, in its exception lines — which is why it
            # can hold `dach` and `holz` at all. MAX_DOC_FREQ is a rarity
            # proxy for "is this a trade word" and the root IS the direct
            # answer (phase 8g: 'derzeit' 0.97%, 'estrich' 1.88% — rarity
            # rated the filler above the trade). stem() is meaningless on
            # a canonical form.
            cands[w] = (c, 0)
            if sources is not None:
                sources.setdefault(w, 'own text')
            continue
        if len(w) < MIN_STEM_LEN and reject(w, 'too short'):
            continue
        if df.get(w, 0) / n_docs > MAX_DOC_FREQ and reject(w, 'store-common'):
            continue
        # phase 8g: rarity is not meaning. 'derzeit' sits in 0.97% of lots
        # and 'estrich' in 1.88%, so the rarity sieve rates the filler word
        # ABOVE the trade word; only the vocabulary can tell them apart.
        if not names_trade(w) and reject(w, 'not a trade word'):
            continue
        s = stem(w, df)
        if len(s) < MIN_STEM_LEN:
            reject(w, 'stem too short')
        if len(s) >= MIN_STEM_LEN:
            best = cands.get(s)
            if best is None or (c, -df.get(w, 0)) > best:
                cands[s] = (c, -df.get(w, 0))
                if sources is not None:
                    sources.setdefault(s, 'own text')
    for lt in label_texts:
        for w in tokens(lt):
            if rooted:
                # a CPV label names the trade by definition, so its roots
                # enter outright — same canonical form as everything else
                for r in roots_in(w):
                    cands.setdefault(r, (len(refs), 0))
                    if sources is not None:
                        sources.setdefault(r, 'CPV label')
                continue
            if len(w) < MIN_STEM_LEN:
                continue
            # phase 8g: label words are definitional only if they NAME the
            # trade — "Anbringen von Leitplanken" contributes 'leitplank',
            # not 'anbring', which is a verb that names no trade at all
            if not names_trade(w):
                continue
            # phase 8c (1): definitional waiver — see constants block
            definitional = label_doc_freq().get(w, 0) <= LABEL_DF_MAX
            if definitional or df.get(w, 0) / n_docs <= MAX_DOC_FREQ:
                s = stem(w, df)
                cands.setdefault(s, (len(refs), 0))
                if sources is not None:
                    sources.setdefault(s, 'CPV label')
    kept = []
    for s in sorted(cands, key=len):  # shortest stem of a family wins
        if not any(k in s for k in kept):
            kept.append(s)
    kept.sort(key=lambda s: (-cands[s][0], cands[s][1]))
    return kept[:MAX_KEYWORDS]


_LABEL_DF = None


def label_doc_freq():
    """token -> number of distinct CPV labels containing it. Label-space
    rarity: 'estricharbeiten' names one trade, 'installation' names dozens
    — only the former is definitional (phase 8c waiver)."""
    global _LABEL_DF
    if _LABEL_DF is None:
        from embed import read_cpv_labels
        c = Counter()
        for lab in read_cpv_labels().values():
            c.update(set(tokens(lab)))
        _LABEL_DF = dict(c)
    return _LABEL_DF


_TRADE_DICTS = None


def dict_cache_path(data_dir, n_lots):
    """Where this configuration's dictionaries live: the config IS the
    filename.

    The cache used to be one `trade_dicts.json` guarded by a hand-written
    comparison of key fields. That failed three times on 2026-08-07, always
    the same way: adding a switch means adding a field AND bumping the
    version, and a field missing from an older file reads as False —
    exactly what a new default carries — so a stale entry matched the key
    and was silently reused. Wrong measurements, invisible.

    Hashing every input into the name removes the class of error. A stale
    cache cannot be *found* rather than being wrongly matched, adding a
    switch needs no bookkeeping, and A/B arms stop evicting each other —
    each configuration keeps its own file, so a sweep rebuilds once and
    then reuses.

    cpv_trade_roots.txt is hashed too, and was never in the old key at all
    even though the dictionaries are derived through names_trade() and the
    phase-8u vote signature. Editing a root silently left the dictionaries
    stale.
    """
    import hashlib
    cfg = repr([
        n_lots, DICT_CACHE_V, bool(BUYER_DIVERSITY), bool(TRADE_ROOTS),
        bool(DICT_TRUSTED_ONLY), bool(DICT_VOTE), float(DICT_MIN_IN),
        float(DICT_VOTE_MARGIN), int(DICT_VOTE_MAX), int(DICT_MIN_LOTS),
        float(DICT_MIN_RATIO), int(DICT_MIN_BUYERS),
        float(DICT_MIN_BUYER_SHARE), int(DICT_MAX_WORDS), int(MIN_STEM_LEN),
        ROOTS_FILE.read_bytes() if ROOTS_FILE.exists() else b'',
    ])
    h = hashlib.sha1(cfg.encode('utf-8')).hexdigest()[:12]
    return Path(data_dir) / f'trade_dicts_{h}.json'


def trade_dictionaries(tenders, trusted, docfreq, cache_path=None):
    """code -> [stems]: each trusted trade's vocabulary, derived from ALL
    store lots carrying the code (main or additional): frequent inside the
    trade, rare outside it, and (phase 8f (A)) used by SEVERAL of the
    trade's buyers rather than concentrated in one procuring office's
    template. Cached on disk (rebuilt when the store grows or when
    DICT_CACHE_V changes) and memoized per process."""
    global _TRADE_DICTS
    if _TRADE_DICTS is not None:
        return _TRADE_DICTS
    from calibrate import is_deep, lot_codes
    lots = tenders.drop_duplicates(subset=KEY)
    # phase 8y: the configuration is the FILENAME (see dict_cache_path), so
    # finding the file is the whole check — there is no key left to forget
    # a field from. `cache_path` may be a directory (preferred) or a file.
    if cache_path is not None and Path(cache_path).is_dir():
        cache_path = dict_cache_path(cache_path, int(len(lots)))
    if cache_path and Path(cache_path).exists():
        d = json.loads(Path(cache_path).read_text(encoding='utf-8'))
        _TRADE_DICTS = d['dicts']
        return _TRADE_DICTS
    n, df = docfreq['n'], docfreq['df']
    by_code = {}
    toks, buyers = [], []
    for idx, r in enumerate(lots.itertuples(index=False)):
        toks.append(set(tokens(fix_text(
            str(r.title or '') + ' ' + str(r.description or '')))))
        b = ' '.join(str(getattr(r, 'buyer_name', '') or '').casefold().split())
        buyers.append(b or None)
        for c in lot_codes(r.cpv_main, r.cpv_additional):
            # phase 8t: any DEEP code with enough lots earns a dictionary;
            # `trusted` is consulted only under the rollback switch
            if (c in trusted) if DICT_TRUSTED_ONLY else is_deep(c):
                by_code.setdefault(c, []).append(idx)
    dicts = {}
    for c, idxs in by_code.items():
        if len(idxs) < DICT_MIN_LOTS:
            continue
        if DICT_VOTE and TRADE_ROOTS:
            poll = Counter()
            lot_roots = {}
            for i in idxs:
                rs = {r for w in toks[i] for r in roots_in(w)}
                lot_roots[i] = rs
                poll.update(rs)
            if poll:
                top = poll.most_common(1)[0][1]
                signature = {r for r, n in poll.most_common(DICT_VOTE_MAX)
                             if n >= top * DICT_VOTE_MARGIN}
                voted = [i for i in idxs if lot_roots[i] & signature]
                # a pool that loses its quorum keeps no dictionary at all:
                # too few agreeing lots is exactly the case where the words
                # would be the miscoded minority's
                if len(voted) < DICT_MIN_LOTS:
                    continue
                idxs = voted
        cnt = Counter()
        # phase 8f (A): distinct buyers per word, alongside the lot count
        w_buyers = {}
        for i in idxs:
            cnt.update(toks[i])
            if buyers[i] is not None:
                for w in toks[i]:
                    w_buyers.setdefault(w, set()).add(buyers[i])
        n_in = len(idxs)
        n_buyers_in = len({buyers[i] for i in idxs if buyers[i] is not None})
        cands = {}
        for w, k in cnt.items():
            f_in = k / n_in
            if len(w) < MIN_STEM_LEN or f_in < DICT_MIN_IN:
                continue
            if BUYER_DIVERSITY:
                nb = len(w_buyers.get(w, ()))
                if nb < DICT_MIN_BUYERS:
                    continue
                if n_buyers_in and nb / n_buyers_in < DICT_MIN_BUYER_SHARE:
                    continue
            f_out = (df.get(w, 0) - k) / max(n - n_in, 1)
            if f_in / max(f_out, 1e-9) < DICT_MIN_RATIO:
                continue
            # phase 8l: vet the RAW word, before stem() truncates it. A root
            # sitting at the TAIL of a compound is cut off by stemming —
            # Ortbeton -> ortbeto, Stahlzargen -> stahlzarg, Bodenfliesen ->
            # bodenflies — so a check applied after stemming rejects genuine
            # trade words and would need dozens of truncated roots to
            # compensate. Checking here needs none.
            if not names_trade(w):
                continue
            s = stem(w, df)
            score = f_in * min(f_in / max(f_out, 1e-9), 100.0)
            if score > cands.get(s, 0.0):
                cands[s] = score
        kept = []
        for s in sorted(cands, key=len):  # shortest stem of a family wins
            if not any(k in s for k in kept):
                kept.append(s)
        kept.sort(key=lambda s: -cands[s])
        dicts[c] = kept[:DICT_MAX_WORDS]
    if cache_path:
        Path(cache_path).write_text(
            json.dumps({'n': int(len(lots)), 'v': DICT_CACHE_V,
                        'dicts': dicts}),
            encoding='utf-8')
    _TRADE_DICTS = dicts
    return dicts


def wide_keywords(refs):
    """Phase 8n — the NOMINATION lexicon: every committed trade root found
    in the firm's reference texts, ordered by how many references carry it.

    Deliberately broader than firm_keywords(). A won tender describes the
    work AND its context — what it sits on, connects to, replaces — so a
    guardrail firm's texts carry 'beton' (the posts are set in it), 'bohr'
    (the holes) and 'rueckbau' (the old barrier). Those are trades
    MENTIONED, not the trade the firm IS, and a lexicon built from them
    matches far too much: measured alone it moved the receipt to IN 60/74
    but OUT 27/52.

    So it is used for one thing only: deciding a lot is worth CONSIDERING.
    Conviction still runs against firm_keywords(), the firm's own narrow
    vocabulary, which a merely-concrete tender will not carry. The lot gets
    considered and then rejected, rather than never considered (nomination
    too tight) or wrongly picked (conviction too loose).

    No stem() anywhere here: the roots are the canonical short forms, hand
    written, so nothing is truncated and every inflection matches — 'holz'
    is inside Holzbau, Holzbauarbeiten and Brettsperrholz alike.
    """
    if not (TRADE_ROOTS and WIDE_NOMINATION):
        return []
    found = Counter()
    for t, _b in refs:
        for r in {r for w in set(tokens(t)) for r in roots_in(w)}:
            found[r] += 1
    kept = []
    for r in sorted(found, key=len):  # shortest root of a family wins
        if not any(k in r for k in kept):
            kept.append(r)
    kept.sort(key=lambda r: -found[r])
    return kept[:MAX_KEYWORDS]


def name_keywords(firm):
    """Phase 8r — the trade roots in the FIRM'S OWN NAME.

    A German contractor says what it is on its letterhead: Tischlerei
    Fischer, Metallbau Politz, Elektro Böhe, Reutlinger Abbruch, KSG
    Kabel-Signal-Gleisbau. That is the one statement of what the firm IS
    rather than what one project happened to contain — self-declared,
    identical across every reference, and immune to the buyer's house
    style that makes tender prose so hard to read. The BUYER's name has
    been read since phase 8 (and discarded, as geography); the winner's
    never was.

    Deliberately ungated (operator decision 2026-08-07): *"a broad
    business is better than no business mentioned at all"*. Where the name
    says nothing — Braun GmbH, PIK AG — this contributes nothing and costs
    nothing, so there is no case to filter for. Where it says something
    broad, broad is what the firm published about itself.

    Measured before shipping: a trade root appears in 140 of the 512 firms
    with >= 3 wins, in 20 of the 102 with no narrow lexicon, and in 6 of
    the 42 that are FULLY MUTE — no narrow, no core, no wide, so no lot
    could ever convict for them (KÖNIGBAU -> tiefbau, Metallbau Politz ->
    metallbau, RIWAtec-Elektro and Elektro Böhe -> elektro, Tischlerei
    Fischer -> tischler, KSG Kabel-Signal-Gleisbau -> kabel, gleis).
    """
    if not (TRADE_ROOTS and NAME_KEYWORDS) or not firm:
        return []
    found = [r for w in tokens(str(firm)) for r in roots_in(w)]
    kept = []
    for r in sorted(dict.fromkeys(found), key=len):  # shortest of a family
        if not any(k in r for k in kept):
            kept.append(r)
    return kept


def root_share(refs):
    """root -> how many of the firm's references carry it. The raw counts
    behind core_keywords(), exposed so the CORE_SHARE rule can be argued
    about with numbers instead of intuition."""
    found = Counter()
    for t, _b in refs:
        for r in {r for w in set(tokens(t)) for r in roots_in(w)}:
            found[r] += 1
    return dict(found)


def core_keywords(refs, firm=None):
    """Phase 8o — the roots that RECUR across the firm's wins: present in
    at least CORE_SHARE of its reference texts.

    Phase 8r: a root from the firm's own NAME joins them unconditionally.
    Recurrence is a way of asking "is this the trade or the context"; a
    name answers that question directly, and a name recurs by definition —
    it is on every reference the firm has ever had.

    This is the distinction the wide lexicon could not make. A won tender
    describes the work and its context, so a guardrail firm's texts all
    carry 'leitplank' while only some carry 'beton' (the posts are set in
    it). The trade recurs; the context varies. Recurrence separates them
    without any judgement about what the words mean.

    Used for one thing: a core root in the TITLE convicts. The title is the
    field that names the work — boilerplate lives in the description — so
    it is the safe place to admit evidence the firm's narrow lexicon
    happens to lack.
    """
    if not (TRADE_ROOTS and CORE_TITLE_CONVICTS):
        return []
    found = Counter()
    for t, _b in refs:
        for r in {r for w in set(tokens(t)) for r in roots_in(w)}:
            found[r] += 1
    need = max(2, int(len(refs) * CORE_SHARE + 0.999)) if len(refs) > 1 else 1
    core = [r for r, n in found.items() if n >= need]
    # the name is on every reference the firm has, so it recurs by
    # definition — scored above any word that merely recurs often
    for r in name_keywords(firm):
        found[r] = max(found.get(r, 0), len(refs) + 1)
        core.append(r)
    kept = []
    for r in sorted(dict.fromkeys(core), key=len):
        if not any(k in r for k in kept):
            kept.append(r)
    return sorted(kept, key=lambda r: -found[r])


def firm_keywords(refs, docfreq, label_texts, trusted_codes, dicts,
                  reasons=None, firm=None, sources=None):
    """The profile lexicon (phase 8c): the firm's own derived words plus
    the dictionaries of its trusted trades, and (phase 8r) the trade roots
    in the firm's own name. Substring-redundant words are collapsed to the
    shortest stem so one text occurrence can never count as two witnesses
    (phase 8b)."""
    kws = derive_keywords(refs, docfreq, label_texts, reasons, sources)
    # the name goes in FIRST so it survives the subsumption pass below as
    # the shortest form of its family: 'elektro' should absorb a dictionary
    # entry like 'elektroinstallation', not the other way round
    for r in name_keywords(firm):
        if not any(k in r for k in kws):
            kws = [k for k in kws if r not in k]
            kws.append(r)
            if sources is not None:
                sources[r] = 'firm name'
    if TRADE_DICTS and dicts:
        for c in trusted_codes:
            for w in dicts.get(c, []):
                # phase 8l: NOT vetted here — dictionary entries are already
                # stemmed, and stemming can cut the very root that would
                # match. trade_dictionaries() vets the raw word instead.
                if any(k in w for k in kws):
                    continue  # an existing (shorter) stem already hits w
                kws = [k for k in kws if w not in k]  # w subsumes longer kws
                kws.append(w)
                if sources is not None:
                    sources[w] = f'dictionary {c}'
    return kws


def _ed_le1(a, b):
    """Edit distance <= 1, two-pointer, no allocation."""
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    i = j = diff = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        diff += 1
        if diff > 1:
            return False
        if la == lb:
            i += 1
            j += 1
        elif la > lb:
            i += 1
        else:
            j += 1
    return diff + (la - i) + (lb - j) <= 1


# Phase 8s diagnostic. The vocabulary header records what the embedder
# scores on SYNONYMS (anstreicher/maler 0.289 ...) and concludes it cannot
# supply the trade list. That measurement is about words that share a
# MEANING and no letters. Grouping a firm's own surface forms is the
# opposite task -- words that share a stem -- and it was never measured.
# These pairs are the real ones, taken from firms in the --empty dump.
INFLECTION_PAIRS = [
    ('wärmepumpe', 'wärmepumpenanlage'),
    ('wärmepumpe', 'wärmepumpen'),
    ('betonarbeiten', 'ortbeton'),
    ('betonarbeiten', 'stahlbeton'),
    ('leitung', 'kanalleitungen'),
    ('dämmarbeiten', 'dämmung'),
    ('kältemaschinen', 'kältetechnische'),
    ('bodenbelag', 'bodenbelagsarbeiten'),
    ('estrich', 'estricharbeiten'),
    ('trennwand', 'trennwände'),
    ('aufzug', 'aufzüge'),
    ('tür', 'türen'),
]
# the control: pairs the header already measured, so the two tasks are
# comparable in one run
SYNONYM_PAIRS = [
    ('anstreicher', 'maler'),
    ('spengler', 'klempner'),
    ('schreiner', 'tischler'),
    ('linoleum', 'bodenbelag'),
    ('dehnfuge', 'estrich'),
]


def collide(data_dir, candidates):
    """Which store words would each candidate root match? The check the
    roots file describes ("the store was checked" — it is how `stei` and
    `gla` were refused) and which has never been a tool. Every root added
    by hand, or proposed by a reader, has to pass it: a root is only as
    good as the words it actually hits.

    Two things are printed beside each word, both learned from writing the
    43 roots of the blind-lot pass. `NEW` marks a word no committed root
    reaches yet -- the part of the list the candidate is actually buying,
    as against the part some other root already covers. `-excepted` marks a
    word an existing "-" line already rejects, so an exception written for
    one root is not mistaken for evidence about another.

    The whole list is printed, never a head of it. The collisions that
    refused `sportgeraet` (transportgeraete) and `toranlag`
    (raffstoranlagen, monitoranlage) all sat in the one-lot tail: a
    truncated listing reads as clean and is the failure this tool exists
    to prevent.
    """
    tenders = pd.read_parquet(Path(data_dir) / 'store' / 'tenders.parquet')
    df = store_doc_freq(tenders, Path(data_dir) / 'evidence_df.json')
    counts, n = df['df'], df['n']
    _roots, nots = trade_roots()
    for cand in candidates:
        c = fold(str(cand).casefold().lstrip('-'))
        hits = sorted(((k, w) for w, k in counts.items() if c in fold(w)),
                      reverse=True)
        tot = sum(k for k, _ in hits)
        new = [(k, w) for k, w in hits
               if not roots_in(w) and not any(x in fold(w) for x in nots)]
        print(f'\n[collide] {cand}: {len(hits)} distinct words, '
              f'{tot} lot-occurrences ({tot / n:.1%} of the store); '
              f'{len(new)} words / {sum(k for k, _ in new)} occurrences '
              f'reach no current root')
        for k, w in hits:
            if any(x in fold(w) for x in nots):
                mark = '  -excepted'
            elif not roots_in(w):
                mark = '  NEW'
            else:
                mark = ''
            print(f'   {k:6d}  {w}{mark}')


def roots_audit(data_dir, limit=40):
    """Which roots in cpv_trade_roots.txt are AMBIGUOUS? Three were found by
    accident this session -- `pump` (Waermepumpe / Abwasserpumpwerk),
    `leitung` (Rohrleitung / Bauleitung) and `schal` (Schalung /
    Schaltschrank / Schallschutz) -- each one a string short enough to sit
    inside words from unrelated trades. Finding them by tripping over them
    is not a search.

    THE RANKING DOES NOT WORK -- read this as an INVENTORY, not a detector.
    Two scores were built and measured, both against the CPV code:

      (1) how concentrated a root's lots are in one code. Surfaces daemm,
          holz, dach, stein -- sound roots whose words are one family. It
          measures UBIQUITY: insulation appears in roofing, facade and HVAC
          lots and means insulation in all of them.
      (2) whether a root's own WORDS agree on a trade. Surfaces stein, putz,
          verkleid, pfahl -- splits that are real but are project
          differences, not meaning differences. Plaster is plaster whether
          the lot is filed under painting, structural or insulation.

    Both ask the CPV code what a word MEANS, and phase 8u established that
    the code frequently does not know (84% of one pool arrived via
    cpv_additional; a tender titled Estricharbeiten sits under "Bau von
    Polizeirevieren"). A noisy label cannot be ground truth for meaning.

    What survives is the listing itself: every root with the words it
    actually matches in the store, grouped by the trades those words fall
    in. That is worth READING -- all three ambiguous roots found so far
    (pump, leitung, schal) were caught by a person noticing a word that did
    not belong in one firm's lexicon, and this shows the same thing store-
    wide. The ordering is not a signal; do not treat the top of it as a
    list of suspects.
    """
    from calibrate import is_deep
    from embed import read_cpv_labels
    tenders = pd.read_parquet(Path(data_dir) / 'store' / 'tenders.parquet')
    lots = tenders.drop_duplicates(subset=KEY)
    labels = read_cpv_labels()
    roots, _nots = trade_roots()
    print(f'[roots] {len(roots)} roots over {len(lots)} store lots', flush=True)

    memo = {}
    def rts(w):
        r = memo.get(w)
        if r is None:
            r = memo[w] = tuple(roots_in(w))
        return r

    per_root_codes = {}
    per_root_words = {}
    per_word_codes = {}
    for r in lots.itertuples(index=False):
        code = str(r.cpv_main or '')
        if not is_deep(code):
            continue
        text = fix_text(str(r.title or '') + ' ' + str(r.description or ''))
        hits = {}
        for w in set(tokens(text)):
            for root in rts(w):
                hits.setdefault(root, set()).add(w)
        for root, words in hits.items():
            per_root_codes.setdefault(root, Counter())[code] += 1
            wc = per_root_words.setdefault(root, Counter())
            for w in words:
                wc[w] += 1
                per_word_codes.setdefault(w, Counter())[code] += 1

    # A root's lots spreading over many codes is UBIQUITY, not ambiguity:
    # `daemm` reaches roofing, facade and HVAC lots and means insulation in
    # every one of them. Ambiguity lives in the WORDS -- `schal` matched
    # schalung, schaltschrank and schallschutz, which are unrelated to each
    # other. So ask whether the root's own words agree: give each matched
    # word the trade its lots concentrate in, then score the root by how
    # much of its weight sits in the majority trade.
    rows = []
    for root, wc in per_root_words.items():
        fam = {}
        for w, k in wc.items():
            codes = per_word_codes.get(w)
            if not codes:
                continue
            fam.setdefault(codes.most_common(1)[0][0][:4], []).append((w, k))
        if len(fam) < 2:
            continue
        total = sum(k for ws in fam.values() for _, k in ws)
        groups = sorted(fam.items(), key=lambda kv: -sum(k for _, k in kv[1]))
        agree = sum(k for _, k in groups[0][1]) / total
        rows.append((agree, total, len(fam), root, groups))
    rows.sort()
    print(f'{"agree":>6s} {"words":>6s} {"trades":>7s}  root')
    for agree, total, nfam, root, groups in rows[:limit]:
        print(f'{agree:6.2f} {total:6d} {nfam:7d}  {root}')
        for pre, ws in groups[:3]:
            top = ' '.join(w for w, _ in sorted(ws, key=lambda x: -x[1])[:4])
            lab = next((labels[c] for c in labels if c.startswith(pre)), '?')
            print(f'{"":16s}{pre}xxxx {lab[:30]:32s} <- {top}')
    return rows


def dict_pool(data_dir, code):
    """Read the lots a trade dictionary is built from, split by HOW the code
    reached them. cpv_main is the buyer's statement of what this lot is;
    cpv_additional is often every trade in the whole procurement, copied
    onto each lot, which puts the same lots into a dozen trades' pools
    systematically. The 10%/8x/two-buyer filters catch random miscoding;
    correlated miscoding is not noise and passes straight through."""
    from calibrate import lot_codes
    tenders = pd.read_parquet(Path(data_dir) / 'store' / 'tenders.parquet')
    lots = tenders.drop_duplicates(subset=KEY)
    docfreq = store_doc_freq(tenders, Path(data_dir) / 'evidence_df.json')
    main, addl = [], []
    for r in lots.itertuples(index=False):
        if str(r.cpv_main or '') == code:
            main.append(r)
        elif code in lot_codes(r.cpv_main, r.cpv_additional):
            addl.append(r)
    from embed import read_cpv_labels
    print(f'[dict] {code} {read_cpv_labels().get(code, "?")}')
    print(f'[dict] pool: {len(main)} lots via cpv_main, '
          f'{len(addl)} via cpv_additional '
          f'({len(addl) / max(len(main) + len(addl), 1):.0%} of the pool)')
    for label, rows in (('cpv_main', main), ('cpv_additional', addl)):
        print(f'\n[dict] {label} sample:')
        for r in rows[:12]:
            print(f'   {str(r.title)[:74]!r}')
    dicts = trade_dictionaries(tenders, set(), docfreq,
                               Path(data_dir))
    print(f'\n[dict] derived words: {dicts.get(code, [])}')


def pair_receipt(data_dir):
    """Cosine for word pairs through the SHIPPED tier-3 matcher, against a
    noise floor drawn from the store's own vocabulary. Answers whether the
    embedder can group a firm's surface forms (Wärmepumpe /
    Wärmepumpenanlage) even though it cannot supply synonyms."""
    tenders = pd.read_parquet(Path(data_dir) / 'store' / 'tenders.parquet')
    docfreq = store_doc_freq(tenders, Path(data_dir) / 'evidence_df.json')
    syn = SynonymTier(Path(data_dir) / 'embeddings' / 'word_vecs.npz')
    rng = np.random.default_rng(SEED)
    vocab = [w for w, c in docfreq['df'].items()
             if c >= 20 and len(w) >= MIN_STEM_LEN]
    pick = rng.choice(len(vocab), 400, replace=False)
    noise_words = [vocab[i] for i in pick]

    def cos(a, b):
        V = syn._embed([a, b])
        return float(V[0] @ V[1])

    N = syn._embed(noise_words)
    S = N @ N.T
    off = S[~np.eye(len(N), dtype=bool)]
    print(f'[pairs] noise floor over {len(N)} store words: '
          f'mean {off.mean():.3f}, p95 {np.percentile(off, 95):.3f}, '
          f'max {off.max():.3f}')
    print(f'[pairs] tier-3 bar SYN_THRESHOLD = {SYN_THRESHOLD}')
    for label, pairs in (('inflection/compound', INFLECTION_PAIRS),
                         ('synonym (control)', SYNONYM_PAIRS)):
        scores = []
        print(f'\n[pairs] {label}:')
        for a, b in pairs:
            s = cos(a, b)
            scores.append(s)
            print(f'   {s:.3f} {"PASS" if s >= SYN_THRESHOLD else "    "}  '
                  f'{a} / {b}')
        arr = np.array(scores)
        print(f'   -> mean {arr.mean():.3f}, '
              f'{int((arr >= SYN_THRESHOLD).sum())}/{len(arr)} clear the bar')

    # WHY THE BAR CANNOT SIMPLY BE LOWERED. match_evidence takes the
    # ARGMAX over every word of the candidate text, so a keyword is not
    # compared with one unrelated word — it is compared with hundreds, and
    # the bar has to beat the MAXIMUM of that pile, not its average. The
    # noise floor's mean (0.185) is therefore the wrong statistic entirely;
    # this is the right one. Each trade keyword is scored against a
    # document-sized bag of unrelated store words, exactly as the tier
    # would score it in production.
    kws = sorted({a for a, _ in INFLECTION_PAIRS})
    K = syn._embed(kws)
    S_kn = K @ N.T
    best = S_kn.max(axis=1)
    print(f'\n[pairs] each keyword vs a {len(N)}-word bag of store words — '
          f'the MAX, which is what the tier actually tests.')
    print('        READ THE MATCHED WORD: a "false" hit that turns out to '
          'be a\n        real variant is not noise, and inverts the '
          'conclusion.')
    order = sorted(range(len(kws)), key=lambda i: -best[i])
    for i in order:
        top = np.argsort(-S_kn[i])[:3]
        quoted = ', '.join(f'{noise_words[j]} {S_kn[i, j]:.3f}' for j in top)
        print(f'   {best[i]:.3f}  {kws[i]:16s} <- {quoted}')
    true_scores = np.array([cos(a, b) for a, b in INFLECTION_PAIRS])
    print(f'\n[pairs] {"bar":>5s} {"inflection kept":>16s} '
          f'{"keywords firing on noise":>26s}')
    for bar in (0.60, 0.65, 0.70, 0.73, 0.75, 0.80, 0.85, 0.90):
        print(f'   {bar:5.2f} {int((true_scores >= bar).sum()):8d}/'
              f'{len(true_scores):<7d} {int((best >= bar).sum()):14d}/'
              f'{len(kws):<11d}')
    syn.save()


def match_evidence(text, keywords, syn=None):
    """-> list of (keyword, found_word, tier). `text` from leistung_text().
    `syn` (optional): callable(unmatched_tokens, keywords) -> list of
    (keyword, token) pairs — the tier-3 synonym hook."""
    found, missed = [], []
    toks = None
    for kw in keywords:
        if kw in text:
            found.append((kw, kw, 1))
            continue
        if len(kw) >= TYPO_MIN_LEN:
            if toks is None:
                toks = list(dict.fromkeys(tokens(text)))
            hit = next((t for t in toks
                        if abs(len(t) - len(kw)) <= 1 and _ed_le1(kw, t)),
                       None)
            if hit:
                found.append((kw, hit, 2))
                continue
        missed.append(kw)
    if syn is not None and missed and not found:
        if toks is None:
            toks = list(dict.fromkeys(tokens(text)))
        for kw, tok in syn([t for t in toks if len(t) >= MIN_STEM_LEN],
                           missed):
            found.append((kw, tok, 3))
    return found


def title_witness(title, ev):
    """Does any exact-tier keyword hit the lot TITLE? A German lot title
    names the procured Gewerk; a single body mention can be sub-scope
    fine print (phase 8c(3): the Rauchableitung lesson)."""
    t = fix_text(str(title or '')).casefold()
    return any(kw in t for kw, _, tier in ev if tier == 1)


def convicts(title, ev):
    """Phase 8c(3) conviction strength: title witness, or >=
    CONVICT_BODY_MIN distinct keywords anywhere."""
    return (title_witness(title, ev)
            or len({kw for kw, _, _ in ev}) >= CONVICT_BODY_MIN)


class SynonymTier:
    """Word-level embedding matcher with a persistent vocabulary cache —
    each unique word is embedded once, ever."""

    def __init__(self, cache_path):
        self.cache_path = Path(cache_path)
        self.vecs = {}
        if self.cache_path.exists():
            d = np.load(self.cache_path, allow_pickle=True)
            self.vecs = dict(zip(d['words'].tolist(), d['vecs']))

    def _embed(self, words):
        todo = [w for w in words if w not in self.vecs]
        if todo:
            from embed import embed_texts
            for w, v in zip(todo, embed_texts(todo)):
                self.vecs[w] = v
        return np.stack([self.vecs[w] for w in words])

    def save(self):
        words = list(self.vecs)
        np.savez_compressed(self.cache_path, words=np.array(words),
                            vecs=np.stack([self.vecs[w] for w in words]))

    def __call__(self, toks, keywords):
        if not toks or not keywords:
            return []
        T = self._embed(toks)
        K = self._embed(keywords)
        S = K @ T.T
        out = []
        for ki, kw in enumerate(keywords):
            j = int(np.argmax(S[ki]))
            if S[ki, j] >= SYN_THRESHOLD:
                out.append((kw, toks[j]))
        return out


# ---------------------------------------------------------------- receipts

def load_world(data_dir):
    tenders = pd.read_parquet(Path(data_dir) / 'store' / 'tenders.parquet')
    awards = pd.read_parquet(Path(data_dir) / 'store' / 'awards.parquet')
    lots = tenders.drop_duplicates(subset=KEY)
    texts = {(r.procedure_id, r.lot_id): leistung_text(r.title, r.description)
             for r in lots.itertuples(index=False)}
    raw = {(r.procedure_id, r.lot_id): (r.title, r.description, r.cpv_main,
                                        r.publication_number, r.buyer_name)
           for r in lots.itertuples(index=False)}
    docfreq = store_doc_freq(tenders, Path(data_dir) / 'evidence_df.json')
    return tenders, awards, lots, texts, raw, docfreq


def firm_profile_texts(awards, texts, firm):
    keys = [k for k in ((p, l) for p, l in zip(
        awards[awards['winner_names'].apply(
            lambda x: x is not None and firm in list(x))]['procedure_id'],
        awards[awards['winner_names'].apply(
            lambda x: x is not None and firm in list(x))]['lot_id']))
        if k in texts]
    return list(dict.fromkeys(keys))


def receipt(data_dir, use_tier3):
    from calibrate import firm_win_rows, is_deep, lot_codes
    from embed import MODEL_TAG, read_cpv_labels
    tenders, awards, lots, texts, raw, docfreq = load_world(data_dir)
    labels = read_cpv_labels()
    trust = json.loads(Path(f'trusted_codes_{MODEL_TAG}.json').read_text(
        encoding='utf-8'))
    trusted = {c for c, v in trust['codes'].items() if v['trusted']}

    wins = firm_win_rows(awards, tenders)
    wins = wins[[k in texts for k in zip(wins['procedure_id'], wins['lot_id'])]]
    wins = wins.drop_duplicates(subset=['winner_names'] + KEY)
    by_firm = {name: g for name, g in wins.groupby('winner_names')
               if len(g) >= MIN_WINS}
    all_keys = list(texts)
    all_class = {k: str(raw[k][2] or '')[:4] for k in all_keys}
    deep_trusted = {k for k in all_keys
                    if is_deep(str(raw[k][2] or '')) and str(raw[k][2]) in trusted}
    rng = np.random.default_rng(SEED)
    syn = SynonymTier(Path(data_dir) / 'embeddings' / 'word_vecs.npz') \
        if use_tier3 else None
    dicts = trade_dictionaries(tenders, trusted, docfreq,
                               Path(data_dir))

    n_pos = hits = 0
    tier_hits = Counter()
    n_neg = neg_pass = 0
    n_vol = vol_pass = 0
    kw_counts = []
    empty_miss = poor_miss = rich_miss = 0
    miss_examples = []
    print(f'[evidence] {len(by_firm)} firms with >= {MIN_WINS} wins')
    for firm, g in by_firm.items():
        keys = [(p, l) for p, l in zip(g['procedure_id'], g['lot_id'])]
        codes = [lot_codes(m, a) for m, a in zip(g['cpv_main'],
                                                 g['cpv_additional'])]
        firm_classes = {c[:4] for cs in codes for c in cs}
        label_texts = [labels[c] for cs in codes for c in cs
                       if c in trusted and c in labels]
        # positives: leave-one-out
        for i, k in enumerate(keys):
            others = [(texts[k2], raw[k2][4]) for j, k2 in enumerate(keys)
                      if j != i]
            lbl = [labels[c] for j, cs in enumerate(codes) if j != i
                   for c in cs if c in trusted and c in labels]
            tc_i = firm_codes([cs for j, cs in enumerate(codes) if j != i])
            kws = firm_keywords(others, docfreq, lbl, tc_i, dicts,
                                firm=firm)
            kw_counts.append(len(kws))
            n_pos += 1
            ev = match_evidence(texts[k], kws, syn)
            if ev:
                hits += 1
                tier_hits[min(t for _, _, t in ev)] += 1
            else:
                if not kws:
                    empty_miss += 1
                elif len(kws) < 3:
                    poor_miss += 1
                else:
                    rich_miss += 1
                if len(miss_examples) < 8 and rng.random() < 0.1:
                    miss_examples.append((firm, raw[k][0], kws[:6]))
        # negatives (clean, off-class trusted) and volume — firm-level keywords
        kws = firm_keywords([(texts[k], raw[k][4]) for k in keys],
                            docfreq, label_texts,
                            firm_codes(codes), dicts, firm=firm)
        neg_pool = [k for k in deep_trusted
                    if all_class[k] not in firm_classes and k not in keys]
        if neg_pool:
            pick = rng.choice(len(neg_pool), min(NEG_PER_FIRM, len(neg_pool)),
                              replace=False)
            for pi in pick:
                n_neg += 1
                neg_pass += bool(match_evidence(texts[neg_pool[pi]], kws, syn))
        pick = rng.choice(len(all_keys), VOL_PER_FIRM, replace=False)
        for pi in pick:
            n_vol += 1
            vol_pass += bool(match_evidence(texts[all_keys[pi]], kws, syn))
    if syn is not None:
        syn.save()
    r = {'recall': hits / n_pos, 'leakage': neg_pass / n_neg,
         'volume': vol_pass / n_vol, 'n_pos': n_pos, 'n_neg': n_neg,
         'n_vol': n_vol, 'tiers': dict(tier_hits), 'tier3': use_tier3}
    print(f"[evidence] recall {r['recall']:.1%} ({hits}/{n_pos}; "
          f"by tier {dict(tier_hits)})")
    print(f"[evidence] leakage {r['leakage']:.1%} of {n_neg} clean negatives")
    print(f"[evidence] volume  {r['volume']:.1%} of {n_vol} random lots")
    kw_a = np.array(kw_counts)
    print(f'[evidence] lexicon sizes: median {int(np.median(kw_a))}, '
          f'{(kw_a == 0).mean():.1%} empty, {(kw_a < 3).mean():.1%} under 3')
    print(f'[evidence] misses: {empty_miss} empty lexicon, {poor_miss} poor '
          f'(<3 kws), {rich_miss} rich lexicon but no match')
    for firm, title, kws in miss_examples:
        print(f'   miss: {str(title)[:48]!r} [{str(firm)[:28]}] kws={kws}')
    return r


def run_benchmark(data_dir, use_tier3):
    tenders, awards, lots, texts, raw, docfreq = load_world(data_dir)
    from calibrate import is_deep, lot_codes
    from embed import MODEL_TAG, read_cpv_labels
    labels = read_cpv_labels()
    trust = json.loads(Path(f'trusted_codes_{MODEL_TAG}.json').read_text(
        encoding='utf-8'))
    trusted = {c for c, v in trust['codes'].items() if v['trusted']}
    cases = [json.loads(line) for line in
             Path('benchmark_relevance.jsonl').read_text(
                 encoding='utf-8').splitlines() if line.strip()
             and not line.startswith('#')]
    syn = SynonymTier(Path(data_dir) / 'embeddings' / 'word_vecs.npz') \
        if use_tier3 else None
    dicts = trade_dictionaries(tenders, trusted, docfreq,
                               Path(data_dir))

    firms = {}
    firm_tc = {}
    fails = 0
    for case in cases:
        firm = case['firm']
        if firm not in firms:
            keys = firm_profile_texts(awards, texts, firm)
            aw = awards[awards['winner_names'].apply(
                lambda x: x is not None and firm in list(x))]
            aw = aw.merge(lots[KEY + ['cpv_main', 'cpv_additional']], on=KEY)
            lbl = [labels[c] for _, r in aw.iterrows()
                   for c in lot_codes(r['cpv_main'], r['cpv_additional'])
                   if c in trusted and c in labels]
            firm_tc[firm] = {c for _, r in aw.iterrows()
                             for c in lot_codes(r['cpv_main'],
                                                r['cpv_additional'])
                             if is_deep(c)}
            firms[firm] = firm_keywords(
                [(texts[k], raw[k][4]) for k in keys], docfreq, lbl,
                firm_tc[firm], dicts, firm=firm)
            print(f'[benchmark] {firm}: keywords = {firms[firm]}')
        kws = firms[firm]
        sel = [k for k in texts if raw[k][3] == case['pub']
               and case.get('title_contains', '') in str(raw[k][0])]
        if not sel:
            print(f"  ?? {case['pub']} not found");  fails += 1
            continue
        for k in sel:
            ref_keys = firm_profile_texts(awards, texts, firm)
            is_ref = k in ref_keys
            # a reference judged against itself is trivially 'in'; judge
            # references leave-one-out like the receipt does
            if is_ref:
                others = [(texts[k2], raw[k2][4]) for k2 in ref_keys
                          if k2 != k]
                kws_k = firm_keywords(others, docfreq, (),
                                      firm_tc[firm], dicts, firm=firm)
            else:
                kws_k = kws
            ev = match_evidence(texts[k], kws_k, syn)
            got = 'in' if ev else 'out'
            ok = got == case['expect']
            fails += not ok
            quote = ', '.join(f'{w}(t{t})' for _, w, t in ev[:4])
            print(f"  {'OK  ' if ok else 'FAIL'} [{case['expect']:>3} -> "
                  f"{got:>3}] {str(raw[k][0])[:44]!r} "
                  f"{('— ' + quote) if quote else ''}")
    if syn is not None:
        syn.save()
    print(f'[benchmark] {len(cases) - fails}/{len(cases)} correct')
    return fails


def judge_run(data_dir):
    """THE WHOLE RUN (operator request 2026-08-06): benchmark cases, clean
    negatives, leave-one-out recall and volume — all through the REAL
    relevance.judge() code path, under BOTH gate modes, same seed, same
    lots. This is the only apples-to-apples table; the calibration numbers
    are arithmetic replicas of the gate, this executes the gate."""
    import relevance as rel
    from calibrate import is_deep
    # profiles carry the evidence lexicon; the embedding ladder ignores it,
    # so ONE gate serves both modes and each judgment names its own config
    # (REFACTOR.md phase 3 -- this used to assign to rel.GATE_MODE)
    CFG = {m: rel.DEFAULT_CONFIG.replace(mode=m)
           for m in ('embedding', 'evidence')}
    gate = rel.Gate(data_dir, config=CFG['evidence'])
    tenders, awards, lots, texts, raw, docfreq = load_world(data_dir)
    from embed import MODEL_TAG
    trust = json.loads(Path(f'trusted_codes_{MODEL_TAG}.json').read_text(
        encoding='utf-8'))
    trusted = {c for c, v in trust['codes'].items() if v['trusted']}
    all_keys = [k for k in texts if k in gate.by_key]
    all_class = {k: str(raw[k][2] or '')[:4] for k in all_keys}
    deep_trusted = [k for k in all_keys
                    if is_deep(str(raw[k][2] or '')) and str(raw[k][2]) in trusted]

    def firm_sub(firm, refs):
        return {'sub_id': 'judge-run', 'version': 0, 'name': firm,
                'profile_refs': refs,
                'min_relevance': rel.DEFAULT_MIN_RELEVANCE}

    def resolvable(firm):
        keys = firm_profile_texts(awards, texts, firm)
        return [k for k in keys if k in gate.by_key]

    def pub_of(k):
        return gate.rows[gate.by_key[k]]['publication_number']

    wins_by_firm = {}
    aw = awards[awards['winner_names'].apply(
        lambda x: x is not None and len(x) > 0)].explode('winner_names')
    for firm, g in aw.groupby('winner_names'):
        keys = [k for k in dict.fromkeys(zip(g['procedure_id'], g['lot_id']))
                if k in gate.by_key and k in texts]
        if len(keys) >= MIN_WINS:
            wins_by_firm[firm] = keys
    print(f'[judge-run] {len(wins_by_firm)} firms through the real judge()')

    cases = [json.loads(line) for line in
             Path('benchmark_relevance.jsonl').read_text(
                 encoding='utf-8').splitlines()
             if line.strip() and not line.startswith('#')]
    results = {}
    for mode in ('embedding', 'evidence'):
        cfg = CFG[mode]
        # --- benchmark through judge() ---
        fails = 0
        for case in cases:
            firm = case['firm']
            refs = resolvable(firm)
            sel = [k for k in all_keys if raw[k][3] == case['pub']
                   and case.get('title_contains', '') in str(raw[k][0])]
            for k in sel:
                use = [r for r in refs if r != k]
                profile = rel.build_profile(gate, firm_sub(firm,
                                                           [pub_of(r) for r in use]))
                ok, *_ = rel.judge(gate, profile, {
                    'procedure_id': k[0], 'lot_id': k[1],
                    'buyer_name': raw[k][4]}, config=cfg)
                got = 'in' if ok else 'out'
                if got != case['expect']:
                    fails += 1
                    print(f"  [{mode}] FAIL [{case['expect']} -> {got}] "
                          f'{str(raw[k][0])[:50]!r}')
        bench = f'{len(cases) - fails}/{len(cases)}'
        # --- recall (leave-one-out), leakage, volume through judge() ---
        rng = np.random.default_rng(SEED)
        n_pos = hits = n_neg = neg_pass = n_vol = vol_pass = 0
        for firm, keys in wins_by_firm.items():
            for i, k in enumerate(keys):
                use = [pub_of(r) for j, r in enumerate(keys) if j != i]
                profile = rel.build_profile(gate, firm_sub(firm, use))
                ok, *_ = rel.judge(gate, profile, {
                    'procedure_id': k[0], 'lot_id': k[1],
                    'buyer_name': raw[k][4]}, config=cfg)
                n_pos += 1
                hits += bool(ok)
            profile = rel.build_profile(gate, firm_sub(
                firm, [pub_of(r) for r in keys]))
            firm_classes = {str(raw[k][2] or '')[:4] for k in keys}
            neg_pool = [k for k in deep_trusted
                        if all_class[k] not in firm_classes and k not in keys]
            if neg_pool:
                for pi in rng.choice(len(neg_pool),
                                     min(NEG_PER_FIRM, len(neg_pool)),
                                     replace=False):
                    k = neg_pool[pi]
                    ok, *_ = rel.judge(gate, profile, {
                        'procedure_id': k[0], 'lot_id': k[1],
                        'buyer_name': raw[k][4]}, config=cfg)
                    n_neg += 1
                    neg_pass += bool(ok)
            for pi in rng.choice(len(all_keys), VOL_PER_FIRM, replace=False):
                k = all_keys[pi]
                ok, *_ = rel.judge(gate, profile, {
                    'procedure_id': k[0], 'lot_id': k[1],
                    'buyer_name': raw[k][4]}, config=cfg)
                n_vol += 1
                vol_pass += bool(ok)
        results[mode] = {'benchmark': bench, 'recall': hits / n_pos,
                         'leakage': neg_pass / n_neg, 'volume': vol_pass / n_vol}
        print(f'[judge-run] {mode:9s}: benchmark {bench}, '
              f'recall {hits / n_pos:.1%} ({hits}/{n_pos}), '
              f'leakage {neg_pass / n_neg:.1%} ({n_neg} negatives), '
              f'volume {vol_pass / n_vol:.1%}', flush=True)
    if rel._SYN is not None:
        rel._SYN.save()
    return results


# the grid: the task's 0.40-0.65 ladder plus 0.70 — the old (bugged) value,
# kept as the continuity anchor: its row must reproduce the committed
# --judge receipt (18/19, 23.8%, 0.4%, 0.9%) or the refactor changed
# behaviour
SWEEP_BARS = (0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70)


def judge_sweep(data_dir, limit=None):
    """Re-search the evidence gate's nomination bar (RELEVANCE.md phase 8,
    open step). ONE pass through the real code path — profiles built once,
    per-judgment components (text sim, hard-code sim, same-buyer,
    contract-type, evidence) collected via relevance.evidence_components();
    then every bar on the grid, plus the evidence-may-nominate variant, is
    applied through relevance._evidence_verdict — the shipped decision
    function, executed, not replicated. The embedding gate is judged through
    the real judge() inside the same loops as the reference row."""
    import relevance as rel
    from calibrate import is_deep
    # profiles carry the lexicon; the embedding ladder ignores it, so one
    # build serves both (REFACTOR.md phase 3: a config, not a global)
    CFG = {m: rel.DEFAULT_CONFIG.replace(mode=m)
           for m in ('embedding', 'evidence')}
    gate = rel.Gate(data_dir, config=CFG['evidence'])
    tenders, awards, lots, texts, raw, docfreq = load_world(data_dir)
    from embed import MODEL_TAG
    trust = json.loads(Path(f'trusted_codes_{MODEL_TAG}.json').read_text(
        encoding='utf-8'))
    trusted = {c for c, v in trust['codes'].items() if v['trusted']}
    all_keys = [k for k in texts if k in gate.by_key]
    all_class = {k: str(raw[k][2] or '')[:4] for k in all_keys}
    deep_trusted = [k for k in all_keys
                    if is_deep(str(raw[k][2] or '')) and str(raw[k][2]) in trusted]

    def firm_sub(firm, refs):
        return {'sub_id': 'judge-sweep', 'version': 0, 'name': firm,
                'profile_refs': refs,
                'min_relevance': rel.DEFAULT_MIN_RELEVANCE}

    def resolvable(firm):
        keys = firm_profile_texts(awards, texts, firm)
        return [k for k in keys if k in gate.by_key]

    def pub_of(k):
        return gate.rows[gate.by_key[k]]['publication_number']

    def observe(profile, k):
        """(embedding verdict through the real judge(), evidence components
        incl. the phase-8b witness counts under both definitions)"""
        row = {'procedure_id': k[0], 'lot_id': k[1], 'buyer_name': raw[k][4]}
        emb_ok, emb_bord, *_ = rel.judge(gate, profile, row,
                                         config=CFG['embedding'])
        text, c_hard, mismatch, same_buyer, ev = rel.evidence_components(
            gate, profile, row, gate.by_key[k])
        return (bool(emb_ok), bool(emb_bord), text, c_hard, mismatch,
                same_buyer, bool(ev), rel.evidence_witnesses(ev),
                len({kw for kw, _, _ in ev}), convicts(raw[k][0], ev),
                profile['min_code_hard'], rel._band_draw(k[0], k[1]))

    def verdict(obs, bar, kmin=0, all_tiers=False, no_guard=False,
                visible=False, any_ev_convicts=False, band_p=0.0):
        (emb_ok, emb_bord, text, c_hard, mismatch, same_buyer, ev,
         n12, nall, conv, mch, draw) = obs
        if mismatch:
            passed, borderline = False, True
        else:
            passed, borderline = rel._evidence_verdict(
                {'min_code_hard': mch}, text, c_hard,
                False if no_guard else same_buyer, ev,
                bar=bar, nomination_min=kmin,
                witnesses=(nall if all_tiers else n12),
                convicting=(ev if any_ev_convicts else conv))
        if not passed and borderline and draw < band_p:  # phase 8d
            passed, borderline = True, False
        return (passed or borderline) if visible else passed

    wins_by_firm = {}
    aw = awards[awards['winner_names'].apply(
        lambda x: x is not None and len(x) > 0)].explode('winner_names')
    for firm, g in aw.groupby('winner_names'):
        keys = [k for k in dict.fromkeys(zip(g['procedure_id'], g['lot_id']))
                if k in gate.by_key and k in texts]
        if len(keys) >= MIN_WINS:
            wins_by_firm[firm] = keys
    if limit:  # smoke-test cap; a receipt run never sets it
        wins_by_firm = dict(list(wins_by_firm.items())[:limit])
    print(f'[sweep] {len(wins_by_firm)} firms, one component pass', flush=True)

    cases = [json.loads(line) for line in
             Path('benchmark_relevance.jsonl').read_text(
                 encoding='utf-8').splitlines()
             if line.strip() and not line.startswith('#')]
    n_hard = min(19, len(cases))  # the original hand-labeled hard set
    bench_obs = []  # (case, is_hard, lot title, observation)
    for ci, case in enumerate(cases):
        firm = case['firm']
        refs = resolvable(firm)
        sel = [k for k in all_keys if raw[k][3] == case['pub']
               and case.get('title_contains', '') in str(raw[k][0])]
        for k in sel:
            use = [r for r in refs if r != k]
            profile = rel.build_profile(gate, firm_sub(
                firm, [pub_of(r) for r in use]))
            bench_obs.append((case, ci < n_hard, str(raw[k][0]),
                              observe(profile, k)))

    rng = np.random.default_rng(SEED)
    pos_obs, neg_obs, vol_obs = [], [], []
    for n_firm, (firm, keys) in enumerate(wins_by_firm.items()):
        for i, k in enumerate(keys):
            use = [pub_of(r) for j, r in enumerate(keys) if j != i]
            profile = rel.build_profile(gate, firm_sub(firm, use))
            pos_obs.append(observe(profile, k))
        profile = rel.build_profile(gate, firm_sub(
            firm, [pub_of(r) for r in keys]))
        firm_classes = {str(raw[k][2] or '')[:4] for k in keys}
        neg_pool = [k for k in deep_trusted
                    if all_class[k] not in firm_classes and k not in keys]
        if neg_pool:
            for pi in rng.choice(len(neg_pool),
                                 min(NEG_PER_FIRM, len(neg_pool)),
                                 replace=False):
                neg_obs.append(observe(profile, neg_pool[pi]))
        for pi in rng.choice(len(all_keys), VOL_PER_FIRM, replace=False):
            vol_obs.append(observe(profile, all_keys[pi]))
        if (n_firm + 1) % 25 == 0:
            print(f'[sweep] {n_firm + 1}/{len(wins_by_firm)} firms', flush=True)
    if rel._SYN is not None:
        rel._SYN.save()

    # --- the table: embedding reference row, then the grids ---
    def bench_line(passes):
        n_h = h_ok = n_a = a_ok = 0
        hard_fails = []
        for (c, hard, t, _), p in zip(bench_obs, passes):
            ok = ('in' if p else 'out') == c['expect']
            n_a += 1
            a_ok += ok
            if hard:
                n_h += 1
                h_ok += ok
                if not ok:
                    hard_fails.append((c, t))
        return f'{h_ok}/{n_h}', f'{a_ok}/{n_a}', hard_fails

    def row(name, fn):
        b19, ball, hard_fails = bench_line(
            [fn(o) for _, _, _, o in bench_obs])
        return (name, b19, ball, hard_fails,
                np.mean([fn(o) for o in pos_obs]),
                np.mean([fn(o) for o in neg_obs]),
                np.mean([fn(o) for o in vol_obs]))

    rows = [row('embedding gate (live)', lambda o: o[0])]
    for bar in SWEEP_BARS:
        rows.append(row(f'evidence, bar {bar:.2f}',
                        lambda o, b=bar: verdict(o, b)))
    # honesty rows (phase 8c): what a customer SEES (pass or borderline),
    # and recall without the same-buyer muting that LOO overstates — the
    # live-world proxy (a live candidate rarely shares a buyer with the
    # profile references)
    rows.append(row('embedding, visible (pass|bord.)',
                    lambda o: o[0] or o[1]))
    rows.append(row(f'evidence {rel.NOMINATION_BAR:.2f}, visible',
                    lambda o: verdict(o, rel.NOMINATION_BAR, visible=True)))
    rows.append(row(f'evidence {rel.NOMINATION_BAR:.2f}, live proxy '
                    '(no guard)',
                    lambda o: verdict(o, rel.NOMINATION_BAR, no_guard=True)))
    rows.append(row(f'evidence {rel.NOMINATION_BAR:.2f}, any-ev convicts',
                    lambda o: verdict(o, rel.NOMINATION_BAR,
                                      any_ev_convicts=True)))
    # phase 8d: the committed configuration — witness rule + deterministic
    # band admit at BORDERLINE_ADMIT_P (rule-only rows above stay at p=0)
    rows.append(row(f'evidence + K>=2 + band p={rel.BORDERLINE_ADMIT_P}'
                    ' (committed)',
                    lambda o: verdict(o, rel.NOMINATION_BAR, kmin=2,
                                      band_p=rel.BORDERLINE_ADMIT_P)))
    # phase 8b: the witness grid at the committed bar; K=1/all-tiers is
    # the rejected evidence-nominates variant, kept as the anchor
    for kmin in (1, 2, 3):
        for all_tiers in (False, True):
            name = (f'evidence, bar {rel.NOMINATION_BAR:.2f} '
                    f'+ K>={kmin} {"all" if all_tiers else "t12"}')
            rows.append(row(name, lambda o, k=kmin, a=all_tiers:
                            verdict(o, rel.NOMINATION_BAR, k, a)))
    print(f'\n[sweep] n_pos {len(pos_obs)}, n_neg {len(neg_obs)}, '
          f'n_vol {len(vol_obs)}')
    print(f"{'configuration':36s} {'hard19':>7s} {'bench':>8s} "
          f"{'recall':>7s} {'leakage':>8s} {'volume':>7s}")
    for name, b19, ball, hard_fails, recall, leakage, volume in rows:
        diagnostic = 'visible' in name or 'proxy' in name
        print(f'{name:36s} {b19 if not diagnostic else "-":>7s} '
              f'{ball if not diagnostic else "-":>8s} {recall:7.1%} '
              f'{leakage:8.1%} {volume:7.1%}')
        if diagnostic:
            continue  # not a pick rule — benchmark columns undefined
        for c, t in hard_fails:
            print(f"    FAIL(hard) [{c['expect']}] {t[:56]!r} "
                  f"({c['note'][:36]})")
    return rows


def judge_benchmark(data_dir):
    """The committed benchmark cases through the REAL judge(), BOTH gate
    modes — the seconds-fast per-case receipt (the store-wide loops live in
    --judge/--sweep). References are judged leave-one-out as everywhere."""
    import relevance as rel
    # profiles carry the lexicon; embedding ignores it (REFACTOR.md phase 3)
    CFG = {m: rel.DEFAULT_CONFIG.replace(mode=m)
           for m in ('embedding', 'evidence')}
    gate = rel.Gate(data_dir, config=CFG['evidence'])
    tenders, awards, lots, texts, raw, docfreq = load_world(data_dir)
    all_keys = [k for k in texts if k in gate.by_key]
    cases = [json.loads(line) for line in
             Path('benchmark_relevance.jsonl').read_text(
                 encoding='utf-8').splitlines()
             if line.strip() and not line.startswith('#')]
    fails = {'embedding': 0, 'evidence': 0}
    for case in cases:
        firm = case['firm']
        refs = [k for k in firm_profile_texts(awards, texts, firm)
                if k in gate.by_key]
        sel = [k for k in all_keys if raw[k][3] == case['pub']
               and case.get('title_contains', '') in str(raw[k][0])]
        if not sel:
            print(f"  ?? {case['pub']} ({firm}) not found")
            fails['embedding'] += 1
            fails['evidence'] += 1
            continue
        for k in sel:
            use = [r for r in refs if r != k]
            profile = rel.build_profile(gate, {
                'sub_id': 'judge-benchmark', 'version': 0, 'name': firm,
                'profile_refs': [gate.rows[gate.by_key[r]]
                                 ['publication_number'] for r in use],
                'min_relevance': rel.DEFAULT_MIN_RELEVANCE})
            row = {'procedure_id': k[0], 'lot_id': k[1],
                   'buyer_name': raw[k][4]}
            got = {}
            for mode in ('embedding', 'evidence'):
                ok, *_ = rel.judge(gate, profile, row, config=CFG[mode])
                got[mode] = 'in' if ok else 'out'
                fails[mode] += got[mode] != case['expect']
            marks = ' '.join(
                f"{m}:{'OK' if got[m] == case['expect'] else 'FAIL(' + got[m] + ')'}"
                for m in ('embedding', 'evidence'))
            print(f"  [{case['expect']:>3}] {marks:34s} "
                  f'{str(raw[k][0])[:44]!r} [{str(firm)[:24]}]')
    for m in ('embedding', 'evidence'):
        print(f'[judge-benchmark] {m}: {len(cases) - fails[m]}/{len(cases)} correct')
    return fails


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', default=config.data_root())
    ap.add_argument('--benchmark', action='store_true')
    ap.add_argument('--receipt', action='store_true')
    ap.add_argument('--tier3', action='store_true',
                    help='enable the word-embedding synonym tier')
    ap.add_argument('--keywords', metavar='FIRM',
                    help='print the derived keyword list for a firm')
    ap.add_argument('--judge', action='store_true',
                    help='THE WHOLE RUN: benchmark + recall/leakage/volume '
                         'through the real relevance.judge(), both gate modes')
    ap.add_argument('--sweep', action='store_true',
                    help='re-search the nomination bar: one real-code pass, '
                         'all bars + the evidence-nominates variant')
    ap.add_argument('--limit', type=int,
                    help='(--sweep smoke test only) cap the firm count')
    ap.add_argument('--collide', nargs='+', metavar='ROOT',
                    help='which store words would these candidate roots '
                         'match? Run before adding any root by hand — it is '
                         'how `stei` and `gla` were refused.')
    ap.add_argument('--roots', action='store_true',
                    help='rank every trade root by how CONCENTRATED its '
                         'store lots are in one trade — the ambiguous ones '
                         '(pump, leitung, schal) sort to the top for reading')
    ap.add_argument('--dict', metavar='CODE',
                    help='READ the pool a trade dictionary is built from: '
                         'how many lots reach it via cpv_main vs '
                         'cpv_additional, sample titles of each, and the '
                         'words derived. A code is only as good as the '
                         'lots filed under it.')
    ap.add_argument('--pairs', action='store_true',
                    help='what the embedder scores on INFLECTION vs on '
                         'synonyms, against a store noise floor — the '
                         'measurement behind grouping a firm\'s surface forms')
    ap.add_argument('--judge-benchmark', action='store_true',
                    help='only the benchmark cases through the real judge(), '
                         'both gate modes — seconds, for benchmark growth')
    args = ap.parse_args()
    if args.collide:
        collide(args.data_dir, args.collide)
        return
    if args.roots:
        roots_audit(args.data_dir, args.limit or 40)
        return
    if args.dict:
        dict_pool(args.data_dir, args.dict)
        return
    if args.pairs:
        pair_receipt(args.data_dir)
        return
    if args.judge_benchmark:
        judge_benchmark(args.data_dir)
        return
    if args.sweep:
        judge_sweep(args.data_dir, args.limit)
        return
    if args.judge:
        judge_run(args.data_dir)
        return
    if args.keywords:
        from calibrate import lot_codes
        from embed import MODEL_TAG, read_cpv_labels
        tenders, awards, lots, texts, raw, docfreq = load_world(args.data_dir)
        labels = read_cpv_labels()
        trust = json.loads(Path(f'trusted_codes_{MODEL_TAG}.json').read_text(
            encoding='utf-8'))
        trusted = {c for c, v in trust['codes'].items() if v['trusted']}
        dicts = trade_dictionaries(tenders, trusted, docfreq,
                                   Path(args.data_dir))
        keys = firm_profile_texts(awards, texts, args.keywords)
        aw = awards[awards['winner_names'].apply(
            lambda x: x is not None and args.keywords in list(x))]
        aw = aw.merge(lots[KEY + ['cpv_main', 'cpv_additional']], on=KEY)
        tc = {c for _, r in aw.iterrows()
              for c in lot_codes(r['cpv_main'], r['cpv_additional'])
              if c in trusted}
        deep = firm_codes([lot_codes(r['cpv_main'], r['cpv_additional'])
                           for _, r in aw.iterrows()])
        lbl = [labels[c] for c in tc if c in labels]
        print(f'[evidence] {args.keywords}: {len(keys)} wins, '
              f'trusted trades {sorted(tc)}')
        # phase 8s: WHERE EACH WORD CAME FROM. The merged list hides the
        # one thing worth reading -- a lexicon of dictionary words is the
        # trade's vocabulary, a lexicon of own-text words is five
        # documents, and the operator's test for a lexicon is reading it.
        src = {}
        kws = firm_keywords([(texts[k], raw[k][4]) for k in keys],
                            docfreq, lbl, deep, dicts, firm=args.keywords,
                            sources=src)
        print(kws)
        by = {}
        for w in kws:
            by.setdefault(src.get(w, '?'), []).append(w)
        for where in sorted(by):
            print(f'   {where:20s} ({len(by[where])}): '
                  f'{" ".join(by[where])}')
        print(f'   {"core (title convicts)":20s} '
              f'({len(core_keywords([(texts[k], raw[k][4]) for k in keys], firm=args.keywords))}): '
              f'{" ".join(core_keywords([(texts[k], raw[k][4]) for k in keys], firm=args.keywords))}')
        return
    if args.benchmark:
        sys.exit(1 if run_benchmark(args.data_dir, args.tier3) else 0)
    if args.receipt:
        receipt(args.data_dir, args.tier3)


if __name__ == '__main__':
    main()
