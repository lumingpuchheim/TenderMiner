"""Fill the synonym tier's word cache ahead of time — so a replay never
loads the embedding model.

Evidence tier 3 embeds single words (`evidence.SynonymTier`) and caches each
one forever. The cache is nearly complete, and that is exactly the problem:
a run that meets a *handful* of uncached words pays the full ~650 MB of ONNX
runtime and jina weights to embed them, then carries that for the rest of the
process. Measured on 2026-08-13, the store's whole title+description
vocabulary was 360 words short of the cache — 1 MB of vectors holding 650 MB
of memory hostage, every run, forever, because `rewind_all.py` never saved
what it embedded either.

This tool embeds the gap once. Afterwards `rewind_all.py` runs with the model
never loaded (`--check` verifies that), and its peak drops by ~650 MB. See
`doc/MEMORY_BUDGET.md`.

It is also the converter to the memory-mapped cache format: whatever it reads
(legacy `word_vecs.npz`, or the current `word_vecs.npy` + `word_vecs_words.json`),
it writes the new pair.

The vocabulary is the union of every word tier 3 can be handed:

* every token of the store's `leistung_text` — what the tier tokenises;
* every token of the whole title+description — the superset, so a change of
  section splitter cannot open a new gap;
* every trade root in `cpv_trade_roots.txt` — these enter a lexicon
  canonically, bypassing MIN_STEM_LEN, so length is not a filter here;
* every word of the CPV German labels — label words enter lexicons too;
* every key of the cached `evidence_df.json` — `evidence.stem()` only ever
  returns a trimmed form that is a key of the document-frequency map, and that
  map is a cache which can outlive the text it was built from;
* every word of every subscription's `profile_texts` — the cold-start path,
  where a customer describes their business in prose the store never saw.

**The last two are belt-and-braces and currently add nothing** (measured
2026-08-13: the store's text is a superset of both, as it must be while the
store only grows). They are here for the day it is not, and they are not the
guarantee. The guarantee is that `rewind_all.py` saves what it had to embed
and prints the count: a first replay on a fresh store may still meet a word no
enumeration here predicted — five got through on 2026-08-13, source
undiagnosed — and the run after it will not.

Usage:
    python embed_vocab.py                 # fill the gap, save
    python embed_vocab.py --check         # report the gap, embed nothing
    python embed_vocab.py --data-dir data
"""

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

import config
import evidence as ev
import heavy_lock
import subscriptions
from embed import read_cpv_labels

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

CACHE = 'word_vecs.npz'   # the name; SynonymTier resolves the real files
BATCH = 2000              # words per embed call — bounds the ORT arena


def store_vocabulary(data_dir, progress=True):
    """Every word tier 3 can be handed, from the store this data_dir holds."""
    lots = pd.read_parquet(
        Path(data_dir) / 'store' / 'tenders.parquet',
        columns=ev.KEY + ['title', 'description']).drop_duplicates(subset=ev.KEY)
    vocab = set()
    for n, r in enumerate(lots.itertuples(index=False)):
        vocab.update(
            w for w in ev.tokens(ev.leistung_text(r.title, r.description))
            if len(w) >= ev.MIN_STEM_LEN)
        vocab.update(
            w for w in ev.tokens(ev.fix_text(
                f'{r.title or ""} {r.description or ""}'))
            if len(w) >= ev.MIN_STEM_LEN)
        if progress and (n + 1) % 5000 == 0:
            print(f'[vocab] {n + 1}/{len(lots)} lots', flush=True)
    roots, _ = ev.trade_roots()
    vocab.update(roots)
    for label in read_cpv_labels().values():
        vocab.update(w for w in ev.tokens(label) if len(w) >= ev.MIN_STEM_LEN)
    # the document-frequency cache: `evidence.stem()` will only ever return a
    # trimmed form that is a key of it, and it can be staler than the store
    df = Path(data_dir) / 'evidence_df.json'
    if df.exists():
        vocab.update(w for w in json.loads(df.read_text(encoding='utf-8'))['df']
                     if len(w) >= ev.MIN_STEM_LEN)
    # customer prose, which the store has never seen
    for sub in subscriptions.load(data_dir, date.today().isoformat()):
        for text in (sub.get('profile_texts') or []):
            vocab.update(w for w in ev.tokens(text) if len(w) >= ev.MIN_STEM_LEN)
    return vocab, len(lots)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', default=config.data_root())
    ap.add_argument('--check', action='store_true',
                    help='report the gap and exit; embeds nothing, writes '
                         'nothing. Exit 1 when the cache is short — the form '
                         'to run before a replay on a memory-tight box.')
    args = ap.parse_args()

    syn = ev.SynonymTier(Path(args.data_dir) / 'embeddings' / CACHE)
    print(f'[vocab] cache holds {len(syn)} words')
    vocab, n_lots = store_vocabulary(args.data_dir)
    missing = sorted(w for w in vocab if w not in syn)
    print(f'[vocab] {len(vocab)} words over {n_lots} lots; '
          f'{len(missing)} not cached')
    if not missing:
        print('[vocab] cache is complete — a replay will not load the model')
        return 0
    if args.check:
        print('[vocab] sample: ' + ', '.join(missing[:12]))
        print(f'[vocab] run `python embed_vocab.py --data-dir {args.data_dir}` '
              f'to embed them ({len(missing) * 768 * 4 / 1e6:.1f} MB)')
        return 1
    # Only the embedding path takes the heavy-job lock. `--check` returned
    # above: it loads no model and allocates nothing worth serialising, and it
    # is exactly what an operator wants to run *while* a cycle is going to
    # find out whether a backfill is needed afterwards.
    #
    # Fail fast rather than wait, like the replay (heavy_lock, property 4):
    # this is manual, cheap to repeat, and its operator is watching.
    #
    # `top_up` is the same work without this lock, because its caller (the
    # weekly cycle) already holds it. Nothing in this file locks twice.
    try:
        with heavy_lock.held(args.data_dir, 'the vocabulary backfill'):
            _embed_missing(syn, missing)
    except heavy_lock.Busy as e:
        print(f'[vocab] {e}', file=sys.stderr)
        return 2
    return 0


def _embed_missing(syn, missing, log=print):
    t0 = time.time()
    for a in range(0, len(missing), BATCH):
        syn._embed(missing[a:a + BATCH])
        log(f'[vocab] embedded {min(a + BATCH, len(missing))}/{len(missing)}')
    syn.save()
    log(f'[vocab] {len(missing)} words embedded in {time.time() - t0:.0f}s; '
        f'cache now {len(syn)} words at {syn.vec_path}')
    return len(missing)


def top_up(data_dir, progress=True, log=print):
    """Embed the words the store has and the cache lacks — for the cycle.

    Called from `cycle.py` immediately after the lots are embedded, and that
    timing is the whole point. **Opening the model costs ~1.2 GB whether it is
    then handed 278 tender texts or one word.** The cycle already opens it for
    the lots; the words tier 3 falls back on ride along in the same open.

    Left to itself the model is opened up to three times a week for the same
    1.2 GB: once here for the lots, again during delivery the moment a keyword
    has to be compared against a word nobody has written before, and a third
    time in the next replay that meets one. Delivery is the expensive place
    for it to happen — that is the stage writing customer reports, and on a
    4 GB machine a surprise gigabyte there is how a Monday ends with no report
    rather than with a worse one.

    Takes no lock: the cycle holds it already (see `main`). Returns the number
    of words embedded, and never raises for lack of work.
    """
    syn = ev.SynonymTier(Path(data_dir) / 'embeddings' / CACHE)
    vocab, _ = store_vocabulary(data_dir, progress=progress)
    missing = sorted(w for w in vocab if w not in syn)
    if not missing:
        log('[vocab] cache is current — no words to embed')
        return 0
    log(f'[vocab] {len(missing)} new words, embedded while the model is open')
    return _embed_missing(syn, missing, log)


if __name__ == '__main__':
    sys.exit(main())
