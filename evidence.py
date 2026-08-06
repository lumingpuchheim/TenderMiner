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
    small enough to read aloud."""
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
    min_buyers = min(2, n_buyers)
    cands = {}
    for w, c in in_refs.items():
        if len(w) < MIN_STEM_LEN or c < min_refs or w in buyer_words:
            continue
        if len(buyers_of[w]) < min_buyers:
            continue
        if df.get(w, 0) / n_docs > MAX_DOC_FREQ:
            continue
        s = stem(w)
        if len(s) >= MIN_STEM_LEN:
            best = cands.get(s)
            if best is None or (c, -df.get(w, 0)) > best:
                cands[s] = (c, -df.get(w, 0))
    for lt in label_texts:
        for w in tokens(lt):
            if (len(w) >= MIN_STEM_LEN
                    and df.get(w, 0) / n_docs <= MAX_DOC_FREQ):
                s = stem(w)
                cands.setdefault(s, (len(refs), 0))
    kept = []
    for s in sorted(cands, key=len):  # shortest stem of a family wins
        if not any(k in s for k in kept):
            kept.append(s)
    kept.sort(key=lambda s: (-cands[s][0], cands[s][1]))
    return kept[:MAX_KEYWORDS]


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
            kws = derive_keywords(others, docfreq, lbl)
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
        kws = derive_keywords([(texts[k], raw[k][4]) for k in keys],
                              docfreq, label_texts)
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

    firms = {}
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
            firms[firm] = derive_keywords(
                [(texts[k], raw[k][4]) for k in keys], docfreq, lbl)
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
                kws_k = derive_keywords(others, docfreq)
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
    args = ap.parse_args()
    if args.judge:
        judge_run(args.data_dir)
        return
    if args.keywords:
        tenders, awards, lots, texts, raw, docfreq = load_world(args.data_dir)
        keys = firm_profile_texts(awards, texts, args.keywords)
        print(f'[evidence] {args.keywords}: {len(keys)} wins')
        print(derive_keywords([(texts[k], raw[k][4]) for k in keys], docfreq))
        return
    if args.benchmark:
        sys.exit(1 if run_benchmark(args.data_dir, args.tier3) else 0)
    if args.receipt:
        receipt(args.data_dir, args.tier3)


if __name__ == '__main__':
    main()
