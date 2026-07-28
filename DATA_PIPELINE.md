# TenderMining — Data Retrieval Spec

How raw tender data is acquired and turned into the **feature file** that the modeling
stage ([`MODELING.md`](MODELING.md), [`METHODS.md`](METHODS.md)) consumes. Three stages,
each strictly richer than the last:

```
Stage 1: TED notices  ──►  Stage 2: GAEB documents  ──►  Stage 3: feature extraction
        (metadata + text)      (best-effort, quantities)      (one feature record per LOT)
```

## Stage 1 — Notice ingestion (exists)

`ted_download_sample.py` pulls notices from the TED search API with server-side filters
(date, country, CPV, notice-type) and saves trimmed JSONL (no links block, one language,
deduped CPV — see README). Both `cn-standard` (tenders) and `can-standard` (awards) are
ingested; they join on `procedure-identifier`.

**Lot handling (MODELING.md §2.3):** the search API cannot deliver reliable per-lot
data for multi-lot notices, so ingestion follows the two-route policy — Route 1 uses
single-lot notices directly; Route 2 fetches each multi-lot notice's eForms XML
(`…/notice/<number>/xml`, one GET, no login) and reads the lots from its
`ProcurementProjectLot`/`LotResult` blocks. Lot count is determined from the XML, never
inferred from search-response array lengths (those can fake multi-lot on single-lot
notices).

**Required protocol:** the download job must count and report, per run and cumulatively,
how many ingested notices are **single-lot** vs **multi-lot** (and the lot-count
distribution). This statistic sizes the Route 1 training set and tells us when building
the Route 2 parser pays off — it is data, not debug output: append one line per run to
`data/ingest_log.jsonl` (run date, query, notices fetched, single-lot count, multi-lot
count, lots histogram).

## Stage 2 — GAEB document acquisition (best-effort)

### What and why

The notice is only the advertisement. The full scope — the complete bill of quantities —
lives in the procurement documents behind the notice's **`document-url-lot`** (eForms
BT-15). In Germany the bill of quantities is usually a **GAEB file**: a standardized,
machine-readable format in which every position has a code, description, quantity, and
unit. GAEB + description together carry all the features the model needs; **PDF
documents are ignored** (out of scope — no PDF parsing in this pipeline).

GAEB file types to accept: GAEB DA XML `.x81`–`.x86` (tender phase is `.x83`) and legacy
GAEB90/2000 `.d81`–`.d86` / `.p81`–`.p86`. Everything else in a document package is
discarded.

### Best-effort policy (required behaviour)

Access to the documents is legally guaranteed only for **open procedures** and only
**while the tender is live**; links are frequently abandoned after award. How many
survive is unknown — therefore the retriever must **try its best on every notice and
record the outcome instead of failing**:

1. For every ingested tender notice with a `document-url-lot`, attempt retrieval **as
   soon as the notice is ingested** (links rot after the result is in — fetch-at-
   publication beats backfill).
2. Follow the URL; if it lands on a platform page, look for the direct/no-registration
   download path (the legally required "ohne Anmeldung" access). **Never create
   accounts, never bypass a registration wall** — if a wall blocks the direct path,
   record it and move on.
3. Download the document package; keep only GAEB files; store them under
   `data/gaeb/<procedure-identifier>/` (the procedure, not the notice, owns the
   documents — corrigenda create new publication-numbers for the same documents, and
   the award notice has no document link at all).
4. **Write one manifest entry per stored file** — see "The manifest" below. The
   manifest, not the folder layout, is the authoritative link between a GAEB file and
   its tender.
5. **Log one outcome per notice** (this log is itself data — it measures link rot):

| outcome | meaning |
| --- | --- |
| `ok` | ≥1 GAEB file retrieved |
| `no_url` | notice has no document link |
| `no_gaeb` | package retrieved but contains no GAEB file (e.g. PDF-only) |
| `registration_wall` | no registration-free path found |
| `dead_link` | URL unreachable / tender taken down (expected for old/awarded notices) |
| `error` | anything else (recorded with detail, non-fatal) |

6. Retries are allowed while a tender is live (e.g. daily until deadline); after the
   award is published, one final attempt, then stop — `dead_link` is then a permanent,
   expected state, not an error.

### The manifest — how downstream knows which GAEB file belongs to which tender

GAEB files carry **no TED identity inside** (a file is named e.g. `LV_Los2.x83`; its
header has project text but no publication-number). The association therefore exists
only at download time — and must be recorded then, explicitly. The download job's
output is files **plus** `data/gaeb/manifest.jsonl` (append-only, one line per stored
file):

```jsonc
{
  "procedure-identifier": "3cee752d-ada8-42e9-a786-8837ea478ba6",   // primary join key
  "publication-number": "367898-2026",       // the notice whose link was followed
  "lot": "LOT-0001",                          // when the URL was lot-specific
  "source_url": "https://…/dl/Vergabeunterlagen.zip",
  "stored_path": "data/gaeb/3cee752d-…/LV_Los2.x83",
  "sha256": "…",                              // dedup + integrity
  "fetched_at": "2026-07-30T06:12:00Z",
  "gaeb_kind": "x83"
}
```

Rules:

- **Downstream joins via the manifest only** (`features.jsonl` ← manifest ←
  notices), never by parsing folder names. Folder layout is a storage detail.
- The **join chain** is: GAEB file → manifest → `procedure-identifier` → all notices of
  that procedure (tender *and* award). This survives corrigenda/republication and
  attaches documents to award records that never carried a link themselves.
- `sha256` dedups identical packages fetched via different notices/lots of the same
  procedure.
- Re-fetches append new manifest lines (new `fetched_at`); the latest line per
  `sha256` wins. Nothing is overwritten — consistent with the append-only principle.

No outcome blocks the pipeline: notices without GAEB simply proceed to Stage 3 with
description-only features (the fallback path A/B are designed for — METHODS.md §5a.4).

## Stage 3 — Feature extraction → the feature file

### Sources, in priority order

1. **GAEB file** (when Stage 2 yielded one): located via the **manifest join**
   (`procedure-identifier` → manifest → stored files, never via folder names); parse
   positions → structured quantities (m², m³, m, t, Stück) with what each quantity
   refers to. This is registry-grade input — no LLM needed, it is a defined format.
2. **Description text** (always): Method E extraction (METHODS.md §5a) fills the fixed
   per-domain schema; embedding of the text is computed here too.
3. **Notice fields** (always): CPV, NUTS, procedure-type, contract-nature, GPA,
   framework, buyer, duration, estimated value.

### Output: the feature file (the contract with the next stage)

Append-only JSONL, **one record per LOT** (the unit of observation — see MODELING.md
§2.1; a single-lot notice yields one record, a 14-lot project up to 14), keyed by
`(procedure-identifier, lot-id)` — never overwritten, new records only (consistent with
the append-only ingestion principle):

```jsonc
// data/features.jsonl — one line per lot
{
  "procedure-identifier": "…",
  "lot-id": "LOT-0001",
  "publication-number": "518801-2026",   // provenance: notice the lot was read from
  "notice-type": "cn-standard",
  "source_level": "gaeb" | "description" | "fields_only",   // best source that contributed
  "gaeb_outcome": "ok" | "no_url" | … ,                      // Stage 2 log, carried along
  "fields": { "cpv_division": "45", "nuts": "DEA2C", "procedure": "open", … },
  "quantities": [                                            // from GAEB and/or Method E
    { "what": "asphalt_surface", "value": 45000, "unit": "m2", "source": "gaeb" },
    { "what": "earthworks",      "value": 2500,  "unit": "m3", "source": "description" }
  ],
  "embedding_ref": "embeddings/518801-2026.npy",             // vectors stored separately
  "targets": { "total_value_eur": null, "n_bids": null }     // filled from the joined award
}
```

- `source_level` lets the modeling stage measure the three data ladders separately
  (fields-only vs description vs GAEB) — the accuracy comparison in METHODS.md §8.6.
- Targets stay `null` on tender records until the matching award arrives; the join
  updates the record's targets (the only permitted mutation).

## Open questions (to resolve during implementation)

- Actual GAEB survival rate — measured by the Stage 2 outcome log, not assumed.
- Per-platform download quirks (dozens of e-procurement platforms; handle the common
  ones first, `error`-log the rest).
- GAEB parser choice (format is documented; Python parsing is feasible — evaluate
  existing libraries before writing one).
