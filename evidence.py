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
DICT_MIN_IN = 0.10    # word must appear in >= 10% of the trade's lots
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
MIN_BUYERS = 2             # refs from fewer buyers cannot define a trade
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
# rollback switch for phase 8f (A) and (B); env var overrides per run so the
# A/B needs no edit (BUYER_DIVERSITY=0 reproduces the phase-8e lexicons)
BUYER_DIVERSITY = os.environ.get('BUYER_DIVERSITY', '1') != '0'
DICT_CACHE_V = 2           # bumped by (A): older caches are not comparable
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


def stem(word):
    """Light German plural/ending trim so a plural-only keyword still
    substring-matches the singular in a candidate text."""
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


def names_trade(w):
    """Does this word name a trade or a material? Substring against the
    roots, so plurals and compounds need no entry of their own — minus the
    listed collisions (Klassen|zimmer is a room, Hain|holz is a village)."""
    if not TRADE_ROOTS:
        return True
    roots, nots = trade_roots()
    f = fold(str(w).casefold())
    if any(x in f for x in nots):
        return False
    return any(r in f for r in roots)


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


def derive_keywords(refs, docfreq, label_texts=()):
    """The TF-IDF sieve over (text, buyer) references: a stem survives when
    it appears in enough references, FROM MORE THAN ONE BUYER (trade words
    travel across buyers; a buyer's template words don't — the same-buyer
    lesson applied to the lexicon), and in almost none of the store. Label
    words (from the profile's trusted CPV labels) pass the same rarity
    filter. The final list keeps the shortest stem of every family and is
    small enough to read aloud.

    Phase 8h: single-buyer profiles contribute their own words again —
    the trade vocabulary vets each word directly, so the blanket rule of
    phase 8f (B) was redundant and cost recall (see the constants block)."""
    n_docs, df = docfreq['n'], docfreq['df']
    in_refs, buyers_of = Counter(), {}
    n_buyers = len({b for _, b in refs}) or 1
    # buyer-name words describe WHO, not WHAT — "hamburg" from
    # "Schulbau Hamburg" is geography, never trade evidence
    buyer_words = {w for _, b in refs for w in tokens(str(b or ''))}
    for t, b in refs:
        for w in set(tokens(t)):
            in_refs[w] += 1
            buyers_of.setdefault(w, set()).add(b)
    min_refs = min(MIN_WITNESSES, len(refs)) or 1
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
    min_buyers = min(MIN_BUYERS, n_buyers)
    cands = {}
    for w, c in in_refs.items():
        if len(w) < MIN_STEM_LEN or c < min_refs or w in buyer_words:
            continue
        if len(buyers_of[w]) < min_buyers:
            continue
        if df.get(w, 0) / n_docs > MAX_DOC_FREQ:
            continue
        # phase 8g: rarity is not meaning. 'derzeit' sits in 0.97% of lots
        # and 'estrich' in 1.88%, so the rarity sieve rates the filler word
        # ABOVE the trade word; only the vocabulary can tell them apart.
        if not names_trade(w):
            continue
        s = stem(w)
        if len(s) >= MIN_STEM_LEN:
            best = cands.get(s)
            if best is None or (c, -df.get(w, 0)) > best:
                cands[s] = (c, -df.get(w, 0))
    for lt in label_texts:
        for w in tokens(lt):
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
                s = stem(w)
                cands.setdefault(s, (len(refs), 0))
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
    from calibrate import lot_codes
    lots = tenders.drop_duplicates(subset=KEY)
    if cache_path and Path(cache_path).exists():
        d = json.loads(Path(cache_path).read_text(encoding='utf-8'))
        if (d.get('n') == int(len(lots)) and d.get('v') == DICT_CACHE_V
                and bool(d.get('bd')) == bool(BUYER_DIVERSITY)):
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
            if c in trusted:
                by_code.setdefault(c, []).append(idx)
    dicts = {}
    for c, idxs in by_code.items():
        if len(idxs) < DICT_MIN_LOTS:
            continue
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
            s = stem(w)
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
                        'bd': bool(BUYER_DIVERSITY), 'dicts': dicts}),
            encoding='utf-8')
    _TRADE_DICTS = dicts
    return dicts


def firm_keywords(refs, docfreq, label_texts, trusted_codes, dicts):
    """The profile lexicon (phase 8c): the firm's own derived words plus
    the dictionaries of its trusted trades. Substring-redundant words are
    collapsed to the shortest stem so one text occurrence can never count
    as two witnesses (phase 8b)."""
    kws = derive_keywords(refs, docfreq, label_texts)
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
                               Path(data_dir) / 'trade_dicts.json')

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
            tc_i = {c for j, cs in enumerate(codes) if j != i
                    for c in cs if c in trusted}
            kws = firm_keywords(others, docfreq, lbl, tc_i, dicts)
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
                            {c for cs in codes for c in cs if c in trusted},
                            dicts)
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
    from calibrate import lot_codes
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
                               Path(data_dir) / 'trade_dicts.json')

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
                             if c in trusted}
            firms[firm] = firm_keywords(
                [(texts[k], raw[k][4]) for k in keys], docfreq, lbl,
                firm_tc[firm], dicts)
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
                                      firm_tc[firm], dicts)
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
    gate = rel.Gate(data_dir)
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
        rel.GATE_MODE = mode
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
                    'buyer_name': raw[k][4]})
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
                    'buyer_name': raw[k][4]})
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
                        'buyer_name': raw[k][4]})
                    n_neg += 1
                    neg_pass += bool(ok)
            for pi in rng.choice(len(all_keys), VOL_PER_FIRM, replace=False):
                k = all_keys[pi]
                ok, *_ = rel.judge(gate, profile, {
                    'procedure_id': k[0], 'lot_id': k[1],
                    'buyer_name': raw[k][4]})
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
    rel.GATE_MODE = 'evidence'  # profiles carry the lexicon; the embedding
    #                             ladder ignores it, so one build serves both
    gate = rel.Gate(data_dir)
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
        rel.GATE_MODE = 'embedding'
        emb_ok, emb_bord, *_ = rel.judge(gate, profile, row)
        rel.GATE_MODE = 'evidence'
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
    rel.GATE_MODE = 'evidence'  # profiles carry the lexicon; embedding ignores it
    gate = rel.Gate(data_dir)
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
            rel.GATE_MODE = 'evidence'
            profile = rel.build_profile(gate, {
                'sub_id': 'judge-benchmark', 'version': 0, 'name': firm,
                'profile_refs': [gate.rows[gate.by_key[r]]
                                 ['publication_number'] for r in use],
                'min_relevance': rel.DEFAULT_MIN_RELEVANCE})
            row = {'procedure_id': k[0], 'lot_id': k[1],
                   'buyer_name': raw[k][4]}
            got = {}
            for mode in ('embedding', 'evidence'):
                rel.GATE_MODE = mode
                ok, *_ = rel.judge(gate, profile, row)
                got[mode] = 'in' if ok else 'out'
                fails[mode] += got[mode] != case['expect']
            rel.GATE_MODE = 'evidence'
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
    ap.add_argument('--data-dir', default='data')
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
    ap.add_argument('--judge-benchmark', action='store_true',
                    help='only the benchmark cases through the real judge(), '
                         'both gate modes — seconds, for benchmark growth')
    args = ap.parse_args()
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
                                   Path(args.data_dir) / 'trade_dicts.json')
        keys = firm_profile_texts(awards, texts, args.keywords)
        aw = awards[awards['winner_names'].apply(
            lambda x: x is not None and args.keywords in list(x))]
        aw = aw.merge(lots[KEY + ['cpv_main', 'cpv_additional']], on=KEY)
        tc = {c for _, r in aw.iterrows()
              for c in lot_codes(r['cpv_main'], r['cpv_additional'])
              if c in trusted}
        lbl = [labels[c] for c in tc if c in labels]
        print(f'[evidence] {args.keywords}: {len(keys)} wins, '
              f'trusted trades {sorted(tc)}')
        print(firm_keywords([(texts[k], raw[k][4]) for k in keys],
                            docfreq, lbl, tc, dicts))
        return
    if args.benchmark:
        sys.exit(1 if run_benchmark(args.data_dir, args.tier3) else 0)
    if args.receipt:
        receipt(args.data_dir, args.tier3)


if __name__ == '__main__':
    main()
