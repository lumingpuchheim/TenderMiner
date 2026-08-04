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
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from ftfy import fix_text

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

MODEL_NAME = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
MODEL_TAG = 'minilm-l12-v2'  # names the sidecar directory; new model = new tag
DIM = 384
# The model attends to ~128 tokens; the trade-defining vocabulary sits in the title
# and the opening of the description, so a generous character cut loses nothing.
MAX_CHARS = 2000

KEY = ['procedure_id', 'lot_id']


def sidecar_dir(data_dir):
    return Path(data_dir) / 'embeddings' / MODEL_TAG


def prep_text(title, description):
    """The exact text a lot is embedded as — one definition for store rows and,
    later, for profile_texts (RELEVANCE.md: same model, same space)."""
    parts = [p for p in ((title or '').strip(), (description or '').strip()) if p]
    return fix_text('\n'.join(parts))[:MAX_CHARS]


def text_hash(text):
    return hashlib.sha1(text.encode('utf-8')).hexdigest()[:16]


_model = None


def embed_texts(texts, batch_size=64):
    """L2-normalised float32 vectors, one per text. Model is loaded lazily so
    importing this module stays free for callers that only read the sidecar."""
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

    # Lots of one procedure often share their text; embed each distinct text once.
    by_hash = {}
    for t in todo:
        by_hash.setdefault(t['text_hash'], t['text'])
    hashes = list(by_hash)
    t0 = time.time()
    chunks, step = [], 2000
    for i in range(0, len(hashes), step):
        chunks.append(embed_texts([by_hash[h] for h in hashes[i:i + step]]))
        done = min(i + step, len(hashes))
        if len(hashes) > step:
            rate = done / (time.time() - t0)
            print(f'[embed] {done}/{len(hashes)} texts '
                  f'({done / len(hashes):.0%}, ~{(len(hashes) - done) / rate:.0f}s left)')
    unique_vecs = np.vstack(chunks)
    vec_of = dict(zip(hashes, unique_vecs))
    new_mat = np.stack([vec_of[t['text_hash']] for t in todo])
    print(f'[embed] {len(todo)} new lots ({len(hashes)} distinct texts) '
          f'in {time.time() - t0:.1f}s')

    # Row i of lots.npy is line i of lots_index.jsonl — write the matrix first
    # via atomic replace: a crash before the index append leaves an over-long
    # matrix, which load_sidecar truncates (a short one would be corruption).
    full = np.vstack([mat, new_mat]) if len(mat) else new_mat
    tmp = d / 'lots.tmp.npy'  # np.save appends .npy to names not ending in it
    np.save(tmp, full)
    tmp.replace(d / 'lots.npy')
    with open(d / 'lots_index.jsonl', 'a', encoding='utf-8') as f:
        for t in todo:
            f.write(json.dumps({k: t[k] for k in
                                ('procedure_id', 'lot_id', 'notice_id',
                                 'publication_number', 'text_hash')},
                               ensure_ascii=False) + '\n')
    return len(todo)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', default='data')
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
