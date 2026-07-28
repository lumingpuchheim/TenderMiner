# TenderMining — Data Retrieval Spec

How raw tender data is acquired and turned into the **feature file** that the modeling
stage ([`MODELING.md`](MODELING.md), [`METHODS.md`](METHODS.md)) consumes.

## 0. The overview: three roles, two payload formats

Every file in the pipeline has exactly one of three roles:

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

**The download job persists exactly two kinds of payload:**

1. **Notice XML** — one file per notice (tenders *and* awards). The complete, official
   truth about a notice, including its lot structure.
2. **GAEB files** — bills of quantities, reached via document links found inside the
   notice XML. Everything else in a document package (PDF etc.) is discarded.

**The TED search API is discovery only.** It is used to find *which* notice numbers to
fetch (filtered by date, country, CPV, notice-type) — its response is **never
persisted**. The XML is the single source of notice truth. (The trimmed-JSONL output of
`ted_download_sample.py` is an exploration convenience, not part of this pipeline.)

Directory layout:

```
data/
  raw/
    xml/<publication-number>.xml          # one per notice, as received
    gaeb/<procedure-identifier>/…         # GAEB files, grouped by procedure
  features.jsonl                          # Role 2 — one line per LOT (see §3)
  embeddings/                             # vector sidecar files
  logs/
    manifest.jsonl                        # GAEB file ↔ tender/lot mapping (§2)
    ingest_log.jsonl                      # per-run stats incl. lot counts (§1)
    checkpoint.json                       # last fetched date → incremental runs
```

Pipeline flow:

```
TED search (discovery only, nothing kept)
   │  list of publication numbers
   ▼
fetch notice XML ────────────► data/raw/xml/            Stage 1
   │  follow document links found in the XML
   ▼
fetch GAEB packages ─────────► data/raw/gaeb/ + manifest  Stage 2 (best-effort)
   │
   ▼
EXTRACT (XML + GAEB + Method E on text)                  Stage 3
   │
   ▼
data/features.jsonl  ◄─── the ONLY thing models ever read
```

## 1. Stage 1 — Notice download (XML)

1. **Discover:** query the TED search API for the scope (date window, country, CPV,
   both `cn-standard` and `can-standard`). Keep only the publication numbers.
2. **Fetch:** for each number not yet in `data/raw/xml/`, GET
   `https://ted.europa.eu/en/notice/<number>/xml` (verified: no login, one request,
   ~26 KB) and store it unmodified.
3. **Checkpoint:** record the last covered publication date in
   `data/logs/checkpoint.json`; the next run continues from there. Nothing is ever
   re-downloaded or overwritten (append-only principle).

**Lot handling (MODELING.md §2.3):** per-lot data comes from the XML's
`ProcurementProjectLot` / `LotResult` blocks — never from search-response arrays (their
flat lists lose lot alignment and can even fake multi-lot on single-lot notices).
Route 1 trains on single-lot notices; Route 2 parses multi-lot XMLs. Since the XML is
fetched for every notice anyway, Route 2 is purely an extraction upgrade, not a new
download.

**Required protocol:** each run appends one line to `data/logs/ingest_log.jsonl`:

```jsonc
{ "run": "2026-07-30T06:00Z", "query": "…", "notices_fetched": 412,
  "single_lot": 371, "multi_lot": 41, "lots_histogram": {"1": 371, "2": 18, "3": 9, "4+": 14} }
```

The single-vs-multi-lot count is data, not debug output: it sizes the Route 1 training
set and tells us when the Route 2 parser pays off.

## 2. Stage 2 — GAEB download (best-effort)

### What and why

The notice is only the advertisement. The full scope — the complete bill of quantities —
lives behind the document link in the notice XML (eForms BT-15, per **lot**; platforms
often serve one package for the whole procedure). In Germany the bill of quantities is
usually a **GAEB file**: standardized, machine-readable, every position with code,
description, quantity, unit. **PDFs are ignored** — no PDF parsing in this pipeline.

Accepted GAEB types: GAEB DA XML `.x81`–`.x86` (tender phase: `.x83`) and legacy
GAEB90/2000 `.d81`–`.d86` / `.p81`–`.p86`. Everything else in a package is discarded.

### Best-effort policy (required behaviour)

Document access is legally guaranteed only for **open procedures** and only **while the
tender is live**; links rot after award. The survival rate is unknown — so the job must
**try its best on every notice and record the outcome instead of failing**:

1. Attempt retrieval **as soon as the tender notice is ingested** (fetch-at-publication
   beats backfill).
2. Follow the link; use only the direct/no-registration path (the legally required
   "ohne Anmeldung" access). **Never create accounts, never bypass a registration
   wall** — record and move on.
3. Keep only GAEB files; store under `data/raw/gaeb/<procedure-identifier>/` (the
   procedure, not the notice, owns the documents — corrigenda create new
   publication-numbers for the same documents, and award notices carry no link).
4. **Write one manifest entry per stored file** (below) — the manifest, not the folder
   layout, is the authoritative link between a GAEB file and its tender.
5. **Log one outcome per notice** (measures link rot):

| outcome | meaning |
| --- | --- |
| `ok` | ≥1 GAEB file retrieved |
| `no_url` | notice has no document link |
| `no_gaeb` | package retrieved but contains no GAEB file (e.g. PDF-only) |
| `registration_wall` | no registration-free path found |
| `dead_link` | URL unreachable / tender taken down (expected for old/awarded notices) |
| `error` | anything else (recorded with detail, non-fatal) |

6. Retries while the tender is live (e.g. daily until deadline); after the award, one
   final attempt, then stop — `dead_link` becomes a permanent, expected state.

No outcome blocks the pipeline: lots without GAEB proceed to Stage 3 with
description-only features (the fallback path A/B are designed for — METHODS.md §5a.4).

### The manifest — how downstream knows which GAEB file belongs to which tender

GAEB files carry **no TED identity inside** (a file is named e.g. `LV_Los2.x83`; its
header has project text but no publication-number). The association exists only at
download time and must be recorded then. `data/logs/manifest.jsonl`, append-only, one
line per stored file:

```jsonc
{
  "procedure-identifier": "3cee752d-ada8-42e9-a786-8837ea478ba6",   // primary join key
  "publication-number": "367898-2026",       // the notice whose link was followed
  "lot": "LOT-0001",                          // when the URL was lot-specific
  "source_url": "https://…/dl/Vergabeunterlagen.zip",
  "stored_path": "data/raw/gaeb/3cee752d-…/LV_Los2.x83",
  "sha256": "…",                              // dedup + integrity
  "fetched_at": "2026-07-30T06:12:00Z",
  "gaeb_kind": "x83"
}
```

Rules:

- **Downstream joins via the manifest only**, never by parsing folder names. Folder
  layout is a storage detail.
- Join chain: GAEB file → manifest → `procedure-identifier` (+ `lot`) → all notices of
  that procedure (tender *and* award). Survives corrigenda/republication; attaches
  documents to award records that never carried a link.
- `sha256` dedups identical packages fetched via different notices/lots.
- Re-fetches append new lines (new `fetched_at`); latest per `sha256` wins. Nothing is
  overwritten.

## 3. Stage 3 — Feature extraction → the feature file

Reads **only** the raw archive (XML + GAEB via manifest) — never the network. Fully
re-runnable: deleting `features.jsonl` and re-extracting from `data/raw/` must
reproduce it.

### Sources per lot, in priority order

1. **GAEB** (when the manifest has files for the lot's procedure): parse positions →
   structured quantities (m², m³, m, t, Stück) with what each quantity refers to.
   Registry-grade input — no LLM needed, it is a defined format.
2. **Description text** (from the XML): Method E extraction (METHODS.md §5a) fills the
   fixed per-domain schema; the text embedding is computed here too.
3. **Notice fields** (from the XML): CPV, NUTS, procedure-type, contract-nature, GPA,
   framework, buyer, duration, estimated value.

### Output: the feature file (the contract with the modeling stage)

Append-only JSONL, **one record per LOT** (the unit of observation — MODELING.md §2.1),
keyed by `(procedure-identifier, lot-id)`:

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
  "embedding_ref": "embeddings/518801-2026_LOT-0001.npy",
  "targets": { "value_eur": null, "n_bids": null }   // filled from the award's lot result
}
```

- `source_level` lets the modeling stage measure the three data ladders separately
  (fields-only vs description vs GAEB).
- Targets stay `null` on tender records until the matching award arrives; the award
  join updates the record's targets (the only permitted mutation).

## Open questions (to resolve during implementation)

- Actual GAEB survival rate — measured by the Stage 2 outcome log, not assumed.
- Per-platform download quirks (dozens of e-procurement platforms; handle the common
  ones first, `error`-log the rest).
- GAEB parser choice (format is documented; Python parsing is feasible — evaluate
  existing libraries before writing one).
