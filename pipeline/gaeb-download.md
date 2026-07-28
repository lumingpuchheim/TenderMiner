# Component: GAEB download

Second component of the data pipeline
([overview](../DATA_PIPELINE.md): `[XML download] ──► [GAEB download] ──► [Extractor]`).
Follows the document links found inside the notice XMLs and retrieves the GAEB bills of
quantities — best-effort, with every outcome recorded.

## Input

| Input | Role |
| --- | --- |
| `data/raw/xml/` | source of the document links (eForms BT-15, per **lot**; platforms often serve one package for the whole procedure) |

## Output

| Output | Content |
| --- | --- |
| `data/raw/gaeb/<procedure-identifier>/…` | retrieved GAEB files, stored as received; everything else in a package (PDF etc.) is discarded |
| `data/logs/manifest.jsonl` | **one line per stored file** — the authoritative GAEB↔tender/lot mapping (see below) |
| per-notice outcome log | `ok` / `no_url` / `no_gaeb` / `registration_wall` / `dead_link` / `error` — measures link rot |

## What and why

The notice is only the advertisement. The full scope — the complete bill of
quantities — lives in the procurement documents behind the notice's document link. In
Germany that is usually a **GAEB file**: standardized, machine-readable, every position
with code, description, quantity, unit. **PDFs are ignored** — no PDF parsing in this
pipeline.

Accepted GAEB types: GAEB DA XML `.x81`–`.x86` (tender phase: `.x83`) and legacy
GAEB90/2000 `.d81`–`.d86` / `.p81`–`.p86`.

## Best-effort policy (required behaviour)

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
4. Write one manifest entry per stored file (below).
5. Log one outcome per notice:

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

No outcome blocks the pipeline: lots without GAEB proceed through the Extractor with
description-only features (the fallback path METHODS.md §5a.4 is designed for).

## The manifest — how downstream knows which GAEB file belongs to which tender

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
