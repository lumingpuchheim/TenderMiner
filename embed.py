"""TenderMining embedding sidecar — RELEVANCE.md phase 1.

One vector per stored lot, computed once, cached forever. The sidecar lives at
data/embeddings/<MODEL_TAG>/ as two aligned files: lots.npy (float32, L2-normalised,
row i) and lots_index.jsonl (line i — procedure_id, lot_id, notice_id,
publication_number, text_hash). Switching embedding models means a new MODEL_TAG
directory and a full re-embed, never an in-place mutation.

The store preserves the official record byte-for-byte; only the embedding text is
repaired (ftfy) — see the encoding decision in RELEVANCE.md.

Usage:
    python embed.py                       # incremental (first run = full backfill)
    python embed.py --data-dir data      # explicit root
"""

import argparse
import gc
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from ftfy import fix_text

import config

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# Known embedding models. The active one is picked by the EMBED_MODEL env var
# (sidecar tag); the default flips only by an explicit commit, never as a side
# effect of a backfill in progress.
MODELS = {
    'minilm-l12-v2': {
        'name': 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2',
        'dim': 384},
    'jina-v2-base-de': {
        'name': 'jinaai/jina-embeddings-v2-base-de',
        'dim': 768},
    # phase 6 (RELEVANCE.md): same model, but descriptions lose their
    # boilerplate sentences before embedding (strip.py); own sidecar dir,
    # own calibration receipt, flips in like any model change
    'jina-v2-base-de-strip': {
        'name': 'jinaai/jina-embeddings-v2-base-de',
        'dim': 768, 'strip': True},
}
MODEL_TAG = os.environ.get('EMBED_MODEL', 'jina-v2-base-de')
MODEL_NAME = MODELS[MODEL_TAG]['name']
DIM = MODELS[MODEL_TAG]['dim']
STRIP = bool(MODELS[MODEL_TAG].get('strip'))
# The model attends to ~128 tokens; the trade-defining vocabulary sits in the title
# and the opening of the description, so a generous character cut loses nothing.
MAX_CHARS = 2000

KEY = ['procedure_id', 'lot_id']


def sidecar_dir(data_dir):
    return Path(data_dir) / 'embeddings' / MODEL_TAG


def prep_text(title, description):
    """The exact text a lot is embedded as — one definition for store rows and,
    later, for profile_texts (RELEVANCE.md: same model, same space). Under a
    strip tag the description loses its boilerplate sentences first (phase 6);
    the title is never stripped."""
    if STRIP:
        import strip
        description = strip.distinctive(description)
    parts = [p for p in ((title or '').strip(), (description or '').strip()) if p]
    return fix_text('\n'.join(parts))[:MAX_CHARS]


def text_hash(text):
    return hashlib.sha1(text.encode('utf-8')).hexdigest()[:16]


_model = None


def unload_model():
    """Drop the embedding session and hand its arenas back.

    The model is the single largest thing the cycle holds, and it is needed for
    about five minutes of a twenty-minute run. Caching it in a module global is
    right *within* the embed stage — reloading per chunk would be absurd — but
    it used to stay resident through grading, training, delivery and the
    simulation too, none of which embed anything. That held the floor at
    2.4 GB and pushed delivery past a 3 GB budget (doc/HOSTING.md 0a).

    Safe to call at any point: `embed_texts` reloads lazily, at a cost of about
    three seconds, so a later caller is slower rather than broken.
    """
    global _model
    if _model is None:
        return
    _model = None
    gc.collect()


def embed_texts(texts, batch_size=16):
    """L2-normalised float32 vectors, one per text. Model is loaded lazily so
    importing this module stays free for callers that only read the sidecar.

    `batch_size` is a memory dial, not a speed one. `prep_text` truncates every
    lot to 2,000 characters, so a batch is that many *full-length* sequences in
    flight at once and the activations dominate the process. Measured in the
    container on 278 real lots (doc/HOSTING.md 0a):

        batch  peak RSS   throughput
           64   4,913 MB    0.7 lots/s
           16   2,149 MB    0.7 lots/s
            8   2,064 MB    0.9 lots/s

    Throughput is flat because the model, not the batching, is the bottleneck —
    so the old default of 64 bought nothing and cost 2.8 GB. It is what made
    the weekly cycle need more RAM than a 4 GB VPS has: the cycle was
    OOM-killed inside this call under a 3 GB cap. Below 16 the curve flattens,
    so 16 is the knee rather than the floor.
    """
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        t0 = time.time()
        _model = TextEmbedding(MODEL_NAME)
        print(f'[embed] model {MODEL_NAME} ready in {time.time() - t0:.1f}s')
    vecs = np.array(list(_model.embed(list(texts), batch_size=batch_size)),
                    dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / np.maximum(norms, 1e-12)


CPV_CSV = Path(__file__).resolve().parent / 'cpv_2008_de.csv'


def read_cpv_labels():
    """code -> German label from the committed CPV 2008 codelist."""
    import csv
    with open(CPV_CSV, encoding='utf-8') as f:
        rows = csv.reader(line for line in f if not line.startswith('#'))
        header = next(rows)
        return {code: label for code, label in rows}


def build_label_sidecar(data_dir):
    """Embed every CPV label into the lot space (RELEVANCE.md code-label
    channel). Idempotent: skips when the sidecar matches the codelist."""
    d = sidecar_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    labels = read_cpv_labels()
    npy, idx = d / 'cpv_labels.npy', d / 'cpv_labels_index.jsonl'
    if npy.exists() and idx.exists():
        with open(idx, encoding='utf-8') as f:
            n = sum(1 for line in f if line.strip())
        if n == len(labels):
            print(f'[embed] label sidecar current: {n} codes')
            return 0
    codes = sorted(labels)
    t0 = time.time()
    vecs = embed_texts([fix_text(labels[c]) for c in codes])
    np.save(d / 'cpv_labels.npy', vecs)
    with open(idx, 'w', encoding='utf-8') as f:
        for c in codes:
            f.write(json.dumps({'code': c, 'label_de': labels[c]},
                               ensure_ascii=False) + '\n')
    print(f'[embed] label sidecar: {len(codes)} codes in {time.time() - t0:.1f}s')
    return len(codes)


def load_label_sidecar(data_dir):
    """(code -> row position, label rows, matrix); build first via --labels."""
    d = sidecar_dir(data_dir)
    with open(d / 'cpv_labels_index.jsonl', encoding='utf-8') as f:
        rows = [json.loads(line) for line in f if line.strip()]
    mat = np.load(d / 'cpv_labels.npy')
    if len(rows) != len(mat):
        raise RuntimeError(f'label sidecar misaligned — delete {d} and re-run')
    return {r['code']: i for i, r in enumerate(rows)}, rows, mat


def load_sidecar(data_dir):
    """(index rows, matrix) — aligned by position; ([], empty) before first run."""
    d = sidecar_dir(data_dir)
    npy, idx = d / 'lots.npy', d / 'lots_index.jsonl'
    if not npy.exists() or not idx.exists():
        return [], np.empty((0, DIM), dtype=np.float32)
    with open(idx, encoding='utf-8') as f:
        rows = [json.loads(line) for line in f if line.strip()]
    mat = np.load(npy)
    if len(mat) > len(rows):
        # Crash between matrix replace and index append: the tail vectors have no
        # index lines. Drop them — their lots re-embed as new on the next run.
        mat = mat[:len(rows)]
    elif len(mat) < len(rows):
        raise RuntimeError(f'sidecar corrupt: {len(rows)} index rows vs only '
                           f'{len(mat)} vectors — delete {d} and re-run')
    return rows, mat


def ensure_embeddings(data_dir, tenders):
    """Embed every store lot not yet in the sidecar. Cached vectors are never
    recomputed; a changed text under a known key is counted and reported, not
    silently re-embedded (append-only discipline — the drift is detectable)."""
    d = sidecar_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    if STRIP:
        # frozen next to the sidecar so already-embedded texts stay
        # byte-stable across incremental runs (strip.py)
        import strip
        strip.load_or_build(d / 'strip_ledger.json', tenders)
    rows, mat = load_sidecar(data_dir)
    known = {(r['procedure_id'], r['lot_id']): r['text_hash'] for r in rows}

    lots = tenders.drop_duplicates(subset=KEY)
    todo, drifted = [], 0
    for r in lots.itertuples(index=False):
        key = (r.procedure_id, r.lot_id)
        text = prep_text(r.title, r.description)
        h = text_hash(text)
        if key in known:
            drifted += known[key] != h
            continue
        todo.append({'procedure_id': r.procedure_id, 'lot_id': r.lot_id,
                     'notice_id': r.notice_id, 'publication_number': r.publication_number,
                     'text_hash': h, 'text': text})
    if drifted:
        print(f'[embed] warning: {drifted} known lots have changed text '
              f'(store rebuilt with different extraction?) — cached vectors kept')
    if not todo:
        print(f'[embed] sidecar current: {len(rows)} lots, nothing new')
        return 0

    # Checkpointed in chunks: a multi-hour backfill interrupted at hour 5 keeps
    # everything flushed so far, and the next run resumes with the remainder.
    # Within a chunk, lots of one procedure often share their text — each
    # distinct text is embedded once (cross-chunk duplicates re-embed; rare and
    # harmless). Row i of lots.npy is line i of lots_index.jsonl — the matrix is
    # written first via atomic replace: a crash before the index append leaves
    # an over-long matrix, which load_sidecar truncates (a short one would be
    # corruption).
    print(f'[embed] {len(todo)} new lots to embed ({MODEL_TAG})')
    t0, step = time.time(), 1000
    for k in range(0, len(todo), step):
        chunk = todo[k:k + step]
        by_hash = {}
        for t in chunk:
            by_hash.setdefault(t['text_hash'], t['text'])
        hashes = list(by_hash)
        vec_of = dict(zip(hashes, embed_texts([by_hash[h] for h in hashes])))
        new_mat = np.stack([vec_of[t['text_hash']] for t in chunk])
        _, mat_now = load_sidecar(data_dir)
        full = np.vstack([mat_now, new_mat]) if len(mat_now) else new_mat
        tmp = d / 'lots.tmp.npy'  # np.save appends .npy to names not ending in it
        np.save(tmp, full)
        tmp.replace(d / 'lots.npy')
        with open(d / 'lots_index.jsonl', 'a', encoding='utf-8') as f:
            for t in chunk:
                f.write(json.dumps({key: t[key] for key in
                                    ('procedure_id', 'lot_id', 'notice_id',
                                     'publication_number', 'text_hash')},
                                   ensure_ascii=False) + '\n')
        done = k + len(chunk)
        rate = done / (time.time() - t0)
        print(f'[embed] {done}/{len(todo)} lots ({done / len(todo):.0%}, '
              f'~{(len(todo) - done) / rate / 60:.0f} min left)', flush=True)
    # Deliberately does NOT unload here. The caller decides, because the
    # weekly cycle has a second job for the open model straight after this one
    # — the single words of `embed_vocab.top_up` — and opening the model is
    # the ~1.2 GB, not using it. `cycle.py` unloads once both are done.
    return len(todo)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', default=config.data_root())
    ap.add_argument('--labels', action='store_true',
                    help='also (re)build the CPV label sidecar')
    args = ap.parse_args()
    if args.labels:
        build_label_sidecar(args.data_dir)
    tenders = pd.read_parquet(Path(args.data_dir) / 'store' / 'tenders.parquet')
    n = ensure_embeddings(args.data_dir, tenders)
    rows, mat = load_sidecar(args.data_dir)
    print(f'[embed] sidecar: {len(rows)} lots, {mat.shape[1]} dims, '
          f'{mat.nbytes / 1e6:.1f} MB ({n} added this run)')


if __name__ == '__main__':
    main()
