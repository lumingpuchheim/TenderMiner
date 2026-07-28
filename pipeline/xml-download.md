# Component: XML download

First component of the data pipeline
([overview](../DATA_PIPELINE.md): `[XML download] ──► [GAEB download] ──► [Extractor]`).
Downloads the official eForms XML of every in-scope notice — the single source of
notice truth, including lot structure.

## Input

| Input | Role |
| --- | --- |
| TED search API (`POST /v3/notices/search`) | **discovery only** — yields the publication numbers matching the scope (date window, country, CPV, `cn-standard` + `can-standard`); the response is **never persisted** |
| `data/logs/checkpoint.json` | last covered publication date from the previous run → incremental operation |

## Output

| Output | Content |
| --- | --- |
| `data/raw/xml/<publication-number>.xml` | one file per notice, stored byte-for-byte as received, never edited |
| `data/logs/ingest_log.jsonl` | one line per run: query, notices fetched, **single-lot vs multi-lot counts, lot histogram** |
| `data/logs/checkpoint.json` | updated last covered date |

## Behaviour

1. **Discover:** query the search API for the scope; keep only the publication numbers.
2. **Fetch:** for each number not yet in `data/raw/xml/`, GET
   `https://ted.europa.eu/en/notice/<number>/xml` (verified: no login, one request,
   ~26 KB) and store it unmodified.
3. **Checkpoint:** record the last covered publication date; the next run continues
   from there. Nothing is ever re-downloaded or overwritten (append-only principle).

## Lot handling

Per-lot data comes from the XML's `ProcurementProjectLot` / `LotResult` blocks — never
from search-response arrays (their flat lists lose lot alignment and can even fake
multi-lot on single-lot notices; see MODELING.md §2.3). Route 1 (time decision) trains
on single-lot notices first; Route 2 parses multi-lot XMLs later. Since the XML is
fetched for every notice anyway, Route 2 is purely an Extractor upgrade, not a new
download.

## Required protocol

Each run appends one line to `data/logs/ingest_log.jsonl`:

```jsonc
{ "run": "2026-07-30T06:00Z", "query": "…", "notices_fetched": 412,
  "single_lot": 371, "multi_lot": 41, "lots_histogram": {"1": 371, "2": 18, "3": 9, "4+": 14} }
```

The single-vs-multi-lot count is data, not debug output: it sizes the Route 1 training
set and tells us when the Route 2 lot parser pays off.
