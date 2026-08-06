"""Sentence-level template stripping — RELEVANCE.md phase 6.

STATUS: OFF (decision 2026-08-06, receipt calibration_jina-v2-base-de-strip.md).
Measured end-to-end and lost to the shipping gate: stripping alone 1.9%
wrong-trade leakage / 61.4% recall vs phase-5 corroboration's 1.5% / 60.3%,
and the combined configuration collapsed (the search turned corroboration
off on stripped vectors) — the two mechanisms neutralise the same template
noise, so they are substitutes, and corroboration is the sharper one. This
module stays as inert infrastructure behind the 'jina-v2-base-de-strip'
embed tag, which nothing reads unless EMBED_MODEL selects it; re-measure at
every model_tag flip (the substitute relationship is a property of the
model). Full record in RELEVANCE.md, phase 6.

Tender prose describes the project around the trade, and whole-document
embeddings average the two. This module separates them: descriptions are
split into sentences, and a sentence is **boilerplate** when it recurs
across many procedures AND its host lots span many trades (the second test
is the guard that keeps standard trade phrases — "Blitzschutzanlage nach
DIN EN 62305" recurs too, but its hosts are one trade, so it stays). What
survives is the lot's *distinctive text*; titles are never stripped.

The boilerplate ledger is derived data, built from the store and frozen
next to the stripped sidecar (data/embeddings/<tag>/strip_ledger.json) so
already-embedded texts stay byte-stable across incremental runs; rebuild it
deliberately with --rebuild when the store has grown a lot.

Usage (diagnostics; embedding integration runs via embed.py):
    python strip.py --stats                       # ledger + removal statistics
    python strip.py --stats --pub 00493570-2026   # kept/dropped per sentence
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import pandas as pd
from ftfy import fix_text

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

KEY = ['procedure_id', 'lot_id']
STRIP_MIN_PROCS = 5    # a sentence recurring in fewer procedures is never boilerplate
STRIP_MIN_TRADES = 3   # ...and its hosts must span at least this many cpv3 trades
STRIP_MIN_CHARS = 15   # fragments below this never count (nor get stripped)

# Sentence boundary: newline, or terminal punctuation followed by whitespace
# and an upper-case/numeric start. Abbreviation false-splits only shorten the
# units — a shorter unit must still recur across trades to be stripped.
_SPLIT = re.compile(r'\n+|(?<=[.!?;:])\s+(?=[A-ZÄÖÜ0-9])')


def split_sentences(text):
    return [s.strip() for s in _SPLIT.split(text or '') if s.strip()]


def norm_key(sentence):
    """Identity of a sentence for the ledger: case/whitespace-folded, digit
    runs masked (templates embed per-notice dates, sums and addresses)."""
    s = re.sub(r'\d+', '0', ' '.join(sentence.casefold().split()))
    return hashlib.sha1(s.encode('utf-8')).hexdigest()[:16]


def build_ledger(tenders):
    """Boilerplate hash set from the full store, per the two-test rule."""
    lots = tenders.drop_duplicates(subset=KEY)
    hosts = {}
    for r in lots.itertuples(index=False):
        cpv3 = str(r.cpv_main)[:3] if pd.notna(r.cpv_main) else ''
        for s in split_sentences(fix_text(str(r.description or ''))):
            if len(s) < STRIP_MIN_CHARS:
                continue
            h = norm_key(s)
            e = hosts.setdefault(h, [set(), set()])
            e[0].add(r.procedure_id)
            e[1].add(cpv3)
    boiler = {h for h, (procs, trades) in hosts.items()
              if len(procs) >= STRIP_MIN_PROCS and len(trades) >= STRIP_MIN_TRADES}
    return boiler, hosts


_boiler = None


def load_or_build(ledger_path, tenders=None, rebuild=False):
    """Load the frozen boilerplate set, building it once from the store when
    absent. Freezing keeps stripped texts byte-stable across incremental
    embed runs; pass rebuild=True to refresh after substantial store growth."""
    global _boiler
    p = Path(ledger_path)
    if p.exists() and not rebuild:
        meta = json.loads(p.read_text(encoding='utf-8'))
        _boiler = set(meta['boilerplate'])
        return _boiler
    if tenders is None:
        raise RuntimeError(f'strip ledger missing at {p} and no store given')
    boiler, hosts = build_ledger(tenders)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(
        {'min_procs': STRIP_MIN_PROCS, 'min_trades': STRIP_MIN_TRADES,
         'min_chars': STRIP_MIN_CHARS, 'n_sentences': len(hosts),
         'boilerplate': sorted(boiler)}), encoding='utf-8')
    _boiler = boiler
    return _boiler


def distinctive(description):
    """The description minus boilerplate sentences (the ledger must be loaded
    via load_or_build first — failing loud beats stripping inconsistently)."""
    if _boiler is None:
        raise RuntimeError('strip ledger not loaded — call load_or_build first')
    kept = [s for s in split_sentences(fix_text(str(description or '')))
            if len(s) < STRIP_MIN_CHARS or norm_key(s) not in _boiler]
    return '\n'.join(kept)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', default='data')
    ap.add_argument('--stats', action='store_true')
    ap.add_argument('--rebuild', action='store_true',
                    help='rebuild the frozen ledger from the current store')
    ap.add_argument('--pub', action='append', default=[],
                    help='show kept/dropped sentences for this publication number')
    args = ap.parse_args()
    tenders = pd.read_parquet(Path(args.data_dir) / 'store' / 'tenders.parquet')
    boiler, hosts = build_ledger(tenders)
    if args.rebuild or args.stats:
        global _boiler
        _boiler = boiler
    lots = tenders.drop_duplicates(subset=KEY)
    if args.stats:
        total = removed = 0
        for r in lots.itertuples(index=False):
            desc = fix_text(str(r.description or ''))
            total += len(desc)
            removed += len(desc) - len(distinctive(r.description))
        print(f'[strip] {len(hosts)} distinct sentences, {len(boiler)} boilerplate '
              f'({len(boiler) / max(len(hosts), 1):.1%})')
        print(f'[strip] characters removed store-wide: {removed / max(total, 1):.1%}')
        top = sorted(((len(v[0]), len(v[1]), h) for h, v in hosts.items()
                      if h in boiler), reverse=True)[:12]
        by_hash = {}
        for r in lots.itertuples(index=False):
            for s in split_sentences(fix_text(str(r.description or ''))):
                if len(s) >= STRIP_MIN_CHARS:
                    by_hash.setdefault(norm_key(s), s)
        print('[strip] widest-spread boilerplate:')
        for n_procs, n_trades, h in top:
            print(f'   {n_procs} procs / {n_trades} trades: '
                  f'{by_hash.get(h, "?")[:90]!r}')
    for pub in args.pub:
        sel = lots[lots['publication_number'] == pub]
        for r in sel.itertuples(index=False):
            print(f'\n[strip] {pub} {str(r.title)[:60]!r}:')
            for s in split_sentences(fix_text(str(r.description or ''))):
                mark = ('DROP' if len(s) >= STRIP_MIN_CHARS
                        and norm_key(s) in boiler else 'keep')
                print(f'   [{mark}] {s[:100]!r}')


if __name__ == '__main__':
    main()
