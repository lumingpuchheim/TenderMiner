# MEMORY BUDGET — what `rewind_all.py` holds, and why

Status: measured 2026-08-13 over the 2026-08-11 store (28,973 tender rows,
24,023 embedded lots, 9,454 CPV labels); fixes below implemented in the same
pass. The question that prompted it: **does the replay fit in a 4 GB VM?**

> **2026-08-24: the store is 4.1× this document's baseline and the answer to
> its question is now NO.** See "The 4× store" at the end — every absolute
> number below is a 29k-row figure; the proportions still hold, the totals do
> not. The measurement is repeatable with `memory_receipt.py` (committed that
> day, so the next re-measure is one command instead of an archaeology dig).

## The result

Peak working set of `python rewind_all.py --step 21`, sampled at 4 Hz, both
runs in the same checkout against the same store:

| | peak |
|---|---|
| before | 2943 MB |
| after | 1436 MB |

(Measured against master at phase 4a, both sides in the same worktree.
Repeated runs of the after-code landed at 1432/1436/1443 MB; the spread is
allocator noise, not a live-set difference. Phase 4a moved neither number:
the selection is not where the memory was.)

The replay documents are **byte-identical** — same 18,891 lot rows, same
picks, same own-win rows. That equality is the acceptance test for everything
below, and it is the only reason these changes are worth having: a memory
saving that moves a verdict is not a saving, it is a different product.

**Measure both sides in the same working tree.** The first attempt at this
table compared the branch against the primary checkout, which was carrying
uncommitted phase-4 work (`selection.py`); the replay's pick count differs
between those two by design, and it cost five bisection runs to notice that
the "regression" was someone else's feature. A replay is a five-minute run
whose output depends on every file in the tree — baseline it in the tree you
are changing, never against another directory.

The replay is the largest single thing this repository runs. Its peak is not
a fact about the model and not a fact about the store's size — it is mostly
the same 29k rows held three and four times at once, plus an embedding model
that loads for want of a dozen cached words. Both are avoidable, and both
were avoided without changing a single number the replay produces.

## The rule this document exists to state

**Nothing here may be "fixed" by making the replay compute something else.**
Every change below is byte-identical in output: narrower reads of columns
nobody used, freeing a value before allocating its replacement, handing a
frame to a function instead of letting it re-read the file, and a similarity
table built over the rows that can actually be indexed instead of the full
square. The one change that *would* have altered verdicts — switching evidence
tier 3 off, worth 646 MB in a single line — is deliberately not taken. See
"What was not done".

## Where the peak came from

Resident simultaneously at a late cutoff, before the fixes:

| MB | what | where |
|---|---|---|
| 646 | fastembed + ONNX runtime + jina weights | `embed.py:90` |
| 352 | `Gate` — sidecar matrices, `_lots`, seven per-lot object arrays | `relevance.py:417` |
| 329 | `tenders_full`, 28,973 rows × 103 mostly-object columns | `rewind_all.py:124` |
| 310 | the World's own copy of the same store | `asof.py:133` |
| 295 | synonym word vectors, 93,201 × 768 float32 | `evidence.py:1446` |
| 190 | interpreter + imports (catboost alone 106) | `rewind_all.py:42` |
| 82 | training frame, features, model, scores | `asof.py:236` |

and the transients that actually set the high-water mark, each on top of all
of the above:

- **+354 MB** — `LL = lmat @ lmat.T`, the full 9,454² label-similarity square
  (`calibrate.py:268`)
- **+460 MB** — the calibration re-reading both store parquets and both
  sidecars that its caller was already holding (`calibrate.py:219`)
- **+291 MB** — last cutoff's `Gate` still bound to its name while
  `world.gate(cfg)` built the next one (`rewind_all.py:181`)

Row counts are not the problem and never were: the `prediction` table's 99,174
rows are never read by this program, and 29k lots is small. Duplication is the
problem.

## The embedding model, which should not have been loading at all

`RELEVANCE.md` is explicit that lot vectors are "computed once, cached
forever" and that scoring is numpy dot products over the sidecar. `embed.py`
loads the model lazily for exactly that reason — its docstring says importing
the module "stays free for callers that only read the sidecar".

The replay defeated this by a hair. Evidence tier 3 embeds single *words*
against a persistent vocabulary cache of 93,201 of them, and the store's
vocabulary had drifted **6,878 words** past it — 21 MB of vectors that pulled
in 646 MB of runtime and weights, at the first cutoff, every run. Worse,
`rewind_all.py` never called `SynonymTier.save()` (only the two receipt
harnesses did), so the words it embedded were discarded at exit and the same
6,878 were embedded again on the next run. Permanently.

Three changes close it:

- **`embed_vocab.py`** (new) embeds the gap once, ahead of the run. Its
  vocabulary is the union of what tier 3 can ever be handed: the store's
  `leistung_text` tokens, the whole title+description tokens as a superset,
  every trade root in `cpv_trade_roots.txt`, and every word of the German CPV
  labels. `--check` reports the gap and exits non-zero without embedding —
  the form to run before a replay on a memory-tight box.
- **The cache is memory-mapped.** It moved from one compressed `word_vecs.npz`
  to `word_vecs.npy` + `word_vecs_words.json`, because `np.load` cannot mmap a
  compressed archive. A run touches a few thousand of 100k rows; it now pays
  for those rows and not for the matrix. The `.npz` still loads, so an
  unconverted checkout works and the old file is the rollback.
- **`rewind_all.py` saves what it embedded**, and reports the miss count to
  stderr. A non-zero count is the signal to run `embed_vocab.py` again.

After the backfill the cache holds 100,079 words and the store's vocabulary is
fully covered — `embed_vocab.py --check` reports 0 missing, and the replay
runs with the model never loaded.

## The duplicated store

- `rewind_all.py` read all 103 columns of `tenders.parquet` to use four
  (`procedure_id`, `lot_id`, `publication_date`, `deadline_date`). It now
  names them, derives `lot_dates` before the loop instead of in the return
  statement, and drops the frame before the first cutoff.
- `relevance.Gate` read all 103 columns to build seven per-row arrays. It now
  names the nine fields a gate actually reads — the seven plus the key — which
  is also everything the evidence-mode lexicon derivation takes off `_lots`.
- `asof.World.calibrate()` called `run_calibration(str(self.work))`, a path,
  so the calibration opened both parquets a second time. It now passes the
  frames the World already holds. `calibrate()` keeps the path form for every
  other caller.
- `rewind_all.py` now clears `gate` at the top of each cutoff rather than
  letting the assignment do it, so the old gate is gone before the rewind,
  the calibration and the training run — not merely before the new gate is
  bound.

## The label-similarity square

`calibrate()` built `lmat @ lmat.T`: every CPV label against every CPV label,
9,454² float32, 354 MB, to answer `best_ll(cand_rows, fp_rows)` lookups. But
the two axes are not the same population. A **candidate** row is always a code
some store lot actually carries; a **fingerprint** row can be any label in the
dictionary. The table is now (carried × all): `label_rows` returns positions
into it, columns stay raw label rows, and the result of every lookup is
unchanged. It prints its own size at run time.

This keeps the lookup a lookup. Recomputing `lmat[cand] @ lmat[fp].T` per call
would have saved the whole table and cost hours — `best_ll` runs on the order
of 10⁵ times per calibration.

## What was not done

**Evidence tier 3 stays on.** Turning it off (`use_tier3=False`, which already
exists in the receipt harnesses) removes the 646 MB in one line, and it is the
wrong trade: `RELEVANCE.md` records tiers 1–2 alone at 51.7% recall against
the embedding gate's 60.0%, with the rich-lexicon misses "dominated by
trade-name synonymy inside a firm's own wins (Holzbau↔Holzarbeit,
Schlosser↔Stahlbau, Parkett↔Bodenbelag) — precisely tier 3's job". How much of
that gap tier 3 actually closes is still an open measurement. Trading an
unmeasured amount of recall for memory obtainable by fixing a cache is not a
trade; it is a shortcut with a receipt nobody has run.

## Second pass — 2026-08-24, after the archive tripled

The prediction above ("2× the store adds ~1 GB") came true and then some. The
store went from 28,973 lots to 92,839 — the 2023-24 backfill of construction,
which is 52,000 of those lots, plus 11,828 IT lots when the scope widened to
CPV 48 and 72. On the server the replay was killed twice by the kernel:

| | |
| --- | --- |
| 2026-08-23, cutoff 89 of 94 | `Out of memory: Killed process`, anon-rss **4.28 GB**, sharing a 7.6 GB box with another session's calibration |
| 2026-08-23, cutoff 93 of 94 | box to itself, killed against a `--memory 5g` container cap |

Both died near the END of the window, which is the shape to expect: the peak
is one cutoff's world, and the last cutoff has the biggest one (43,130
training lots against 6,551 on the laptop store).

**Three changes, all of them the same kind as the first pass — the document
they produce is byte-identical (3,930,390 bytes, 18,891 lot rows).**

1. **The sidecar matrices are mapped, not copied** (`load_sidecar(mmap=True)`,
   `load_label_sidecar(mmap=True)`, taken by `relevance.Gate` and
   `calibrate`). 285 MB of lot vectors and 29 MB of label vectors are read and
   never written. Mapped, they are file-backed pages: the kernel can drop them
   under pressure instead of killing the process. Writers keep the copy —
   `ensure_embeddings` replaces the file underneath, and a map of a replaced
   file is a map of the old bytes.
2. **The embedding model is cached out of the loop** (`embed_texts_cached`,
   `embed.unload_model()` once per cutoff). A subscription with
   `profile_texts` used to load 646 MB of ONNX to embed the same dozen strings
   at every one of 94 cutoffs, and leave it resident through the next cutoff's
   calibration and training — which is exactly where the peak is. The section
   above had already named this the largest avoidable item.
3. **The rewind streams instead of materialising** (`asof.World.rewind`). It
   read the entire store — 92,839 rows × 103 mostly-text columns — into Arrow
   and then held the filtered copy beside it, 94 times. A dataset scanner
   pushes the date predicate down and yields one batch at a time, so the peak
   is a batch and stays a batch however deep the archive gets.

**Measured on the laptop store, same method as above, same tree rule:
1461 MB → 1410 MB.** That number understates the changes and is reported
anyway, because it is what was measured: at 24,023 embedded lots the mapped
matrix is 74 MB rather than 285 MB, the rewind moves 29k rows rather than 93k,
and the model never loads at all (the word cache covers it), so two of the
three changes have almost nothing to bite on. The server-side figure is the
one that matters and is measured on the next server run.

## Reading this on a VM

Two properties do not shrink and should be planned around.

**The ONNX arena does not give memory back.** Once the runtime has embedded a
large batch it keeps the arena. `rewind_all.py` embeds single words, so the
arena stays small — but any path that embeds 2,000-character lot texts at
`batch_size=64` (`embed.py:85`, i.e. a sidecar backfill) will exceed 4 GB on
its own. **Do not run `embed.py` and the replay on the same 4 GB box**, and do
not size the box on this document's numbers if the plan includes a re-embed.

**Everything except the model weights scales with the store.** The frames, the
sidecar matrices, the per-lot arrays and the label table all grow with lot
count. Roughly 2× today's store adds ~1 GB.

`peak_wset` is a high-water mark over a run that allocates and frees ~15 times
at this size, so it also carries allocator fragmentation. It is the right
number to size a VM with and the wrong number to read as a live set.

**So: 4 GB fits, with about 2.3 GB of headroom** at 1436 MB, and it now
survives the store roughly doubling. Before these changes the same box had
~850 MB of headroom and would not have survived one year of growth. Run
`python embed_vocab.py --check` before a replay on a memory-tight box: it
exits non-zero exactly when the run would otherwise load the model and add
646 MB to the numbers above.

## The 4× store (2026-08-24)

The bulk backfill to 2024-11 (landed 2026-08-19..22) took the store from
28,973 to **118,573 tender rows** in ten days. The paragraph above promised
survival for a *doubling*; nobody re-read it before quadrupling, and the OOM
killer delivered the news instead:

- **2026-08-23 19:23** — global OOM, a replay's python at 4.3 GB anon,
  killed while a manual calibration ran beside it (the calibration harnesses
  do not take the heavy lock — only the cycle, the delivery, the replay, the
  backfill and backplay do). The kernel picked the victim; it could as
  easily have picked `app.py`.
- **2026-08-23 23:26** — memcg OOM at 5.2 GB, the full weekly replay killed
  by its container limit **one cutoff from the end of a five-hour run**.

Re-measured 2026-08-24 on the server, in a container capped at 6 GB, with
the sampler now committed as `memory_receipt.py`:

```
docker run --rm --memory 6g -e TM_DATA_DIR=/data \
  -v /home/debian/tm-state:/data "tendermining:$TAG" \
  python memory_receipt.py --label replay-late -- \
  python rewind_all.py --from 2026-05-01 --sub nobody --out /tmp/measure.json
```

**Peak 4,835 MB over 77 minutes** (17 cutoffs, 2026-05-01..2026-08-21,
exit 0). The late window is the honest short form: the as-of world grows
monotonically, so the peak lives in the last cutoffs and a measurement that
starts in May answers "does it fit" in a quarter of the full run's time. The
full weekly run from 2024-11 peaks slightly higher (more allocate/free
rounds, more fragmentation — its kill came at 5.2 GB) and takes ~5 hours.
The model never loaded (no tier-3 miss line): this is pure store scaling,
exactly the "roughly 2× adds ~1 GB" rule above, applied twice.

What stands as of that day, in order of preference when memory is tight:

- The box has **4 GB of swap** (`docker/swap.sh`, swappiness 10) — a
  transient peak now means a slow tail, not a dead five-hour run.
- Run a full replay under `--memory 6g` (as above), so a regression dies
  alone in its cgroup instead of handing the kernel a free choice of victim.
- Backplay starts no measurement within six hours of the Monday cycle
  (`backplay.CYCLE_CLEARANCE`) — on 2026-08-24 a 04:00 backplay held the
  heavy lock past 09:30 and the Monday mail was never sent.
- The duplication pass was partially repeated the same night. The full
  weekly replay (95 cutoffs, 2024-11..2026-08) peaked at **5,230 MB over
  3 h 36 m** — the exact number the 2026-08-23 memcg kill hit, confirming
  that run died at its natural peak. Narrowing `awards_full` to the six
  columns the replay reads (of 48; the nested `submission_statistics` and
  `winning_bids` stay on disk) and dropping the second awards copy after
  the outcome dict re-ran **byte-identical** (same store, same night, only
  the `generated` stamp excluded) at **5,117 MB** — ~200 MB lower through
  the middle of the run, ~113 MB at the peak. The peak lives in the Gate,
  the World's store copy and the label-table transients; those are the
  remaining candidates, and they are real surgery, not a column list.

`doc/HOSTING.md` §0's per-stage table and its "fits a 4 GB machine" claims
are 2026-08-13 numbers over the 29k store — read them as proportions, not
totals, until they are re-measured.
