# TenderMining — Data Pipeline Overview

How raw tender data is acquired and turned into the **feature file** that the modeling
stage ([`MODELING.md`](MODELING.md), [`METHODS.md`](METHODS.md)) consumes.

## Vocabulary (to avoid ambiguity)

- **Component** = a box in the architecture below. All components run in every pipeline
  run once built; "downstream" means data flow, **not** calendar time.
- **Route / build order / phase** = decisions in **time** (what we build or use first),
  e.g. Route 1 "single-lot tenders first" (MODELING.md §2.3). A route never removes a
  component; it only narrows what a component processes for now.

## The architecture

```
                 xml files
[XML download] ─────────────────────────────► [Extractor] ──► features.jsonl
      │                                           ▲
      │ document links                            │ gaeb files + manifest
      ▼                                           │
[GAEB download] ──────────────────────────────────┘
```

Two edges feed the Extractor: the notice XMLs (from XML download) and the GAEB files
with their manifest (from GAEB download). GAEB download itself consumes the XMLs only
to find the document links.

Three components, each with its own specification document:

| Component | Document | Input | Output |
| --- | --- | --- | --- |
| **XML download** | [`pipeline/xml-download.md`](pipeline/xml-download.md) | TED search API (discovery only, response never persisted); `data/logs/checkpoint.json` from the previous run | `data/raw/xml/<publication-number>.xml` (one per notice); `data/logs/ingest_log.jsonl` (run stats incl. single-/multi-lot counts); updated `checkpoint.json` |
| **GAEB download** | [`pipeline/gaeb-download.md`](pipeline/gaeb-download.md) | `data/raw/xml/` (document links inside the notice XMLs) | `data/raw/gaeb/<procedure-identifier>/…` (GAEB files only); `data/logs/manifest.jsonl` (file↔tender/lot mapping); per-notice outcome log |
| **Extractor** | [`pipeline/extractor.md`](pipeline/extractor.md) | `data/raw/xml/`; `data/raw/gaeb/` via `manifest.jsonl` — **no network** | `data/features.jsonl` (one line per LOT — the only file the modeling stage reads); `data/embeddings/*.npy` |

The download job = XML download + GAEB download (the two components that touch the
network). The Extractor is a separate offline program, fully re-runnable from the raw
archive.

## Three roles of files on disk

```
ROLE 1: RAW ARCHIVE            ROLE 2: THE ONE TABLE            ROLE 3: LOGS
what the download job          the only file downstream         what happened while
fetched, byte-for-byte,        ever reads                       downloading
never edited, never read
by models

data/raw/xml/                  data/features.jsonl              data/logs/
data/raw/gaeb/                 (+ embeddings/ sidecar)            manifest.jsonl
                                                                  ingest_log.jsonl
                                                                  checkpoint.json
```

**The download job persists exactly two kinds of payload:** notice XML (the complete,
official truth about a notice, including its lot structure) and GAEB files (bills of
quantities; PDFs and everything else in a package are discarded).

**The TED search API is discovery only** — used to find which notice numbers to fetch;
its response is never persisted. The XML is the single source of notice truth. (The
trimmed-JSONL output of `ted_download_sample.py` is an exploration convenience, not part
of this pipeline.)

Directory layout:

```
data/
  raw/
    xml/<publication-number>.xml          # one per notice, as received
    gaeb/<procedure-identifier>/…         # GAEB files, grouped by procedure
  features.jsonl                          # Role 2 — one line per LOT
  embeddings/                             # vector sidecar files
  logs/
    manifest.jsonl                        # GAEB file ↔ tender/lot mapping
    ingest_log.jsonl                      # per-run stats incl. lot counts
    checkpoint.json                       # last fetched date → incremental runs
```

## Build status

| Component | Status |
| --- | --- |
| XML download | spec only ([`pipeline/xml-download.md`](pipeline/xml-download.md)) — `ted_download_sample.py` is the exploration sampler, not this job |
| GAEB download | spec only ([`pipeline/gaeb-download.md`](pipeline/gaeb-download.md)) |
| Extractor | spec only ([`pipeline/extractor.md`](pipeline/extractor.md)) |

## Open questions (to resolve during implementation)

- Actual GAEB survival rate — measured by the GAEB download outcome log, not assumed.
- Per-platform download quirks (dozens of e-procurement platforms; handle the common
  ones first, `error`-log the rest).
- GAEB parser choice (format is documented; Python parsing is feasible — evaluate
  existing libraries before writing one).
