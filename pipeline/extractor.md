# Component: Extractor

Second of the two pipeline components ([overview](../DATA_PIPELINE.md)):

```
[Download job] ──► [Extractor]
```

Takes the Download job's raw archive — the notice XMLs plus the GAEB files with their
manifest — and generates `data/features.jsonl`, the one table the modeling stage reads.
**Touches no network.** Fully re-runnable: deleting `features.jsonl` and re-extracting
from `data/raw/` must reproduce it.

## Input

| Input | Role |
| --- | --- |
| `data/raw/xml/` | notice truth: lots, structured fields, description text |
| `data/raw/gaeb/` via `data/logs/manifest.jsonl` | bills of quantities per procedure/lot (the manifest, never folder names, provides the mapping) |

## Output

| Output | Content |
| --- | --- |
| `data/features.jsonl` | **one line per LOT** (unit of observation — MODELING.md §2.1), keyed by `(procedure-identifier, lot-id)`; the only file the modeling stage reads |
| `data/embeddings/*.npy` | text-embedding vectors, one sidecar file per lot |

## Two rules inherited from the download discussion

- **Language:** country is a fetch filter; language is enforced *here*. When a field
  exists in several languages in the XML, keep the buyer's language (e.g. `deu`),
  drop the rest — no foreign-language text ever reaches `features.jsonl`.
- **Latest version wins:** corrigenda are complete republished notices. Per procedure,
  extract from the **latest version only**; earlier versions are history and are not
  merged.

## The four workers

1. **XML parser** — reads each notice XML, resolves lots from
   `ProcurementProjectLot`/`LotResult` blocks, pulls the structured fields per lot
   (CPV, NUTS, procedure-type, contract-nature, GPA, framework, buyer, duration,
   estimated value). Route 1 (time decision, MODELING.md §2.3): initially only
   single-lot notices; the multi-lot parser is the Route 2 upgrade.
2. **GAEB parser** — for lots whose procedure has manifest entries: parse positions →
   structured quantities (m², m³, m, t, Stück) with what each quantity refers to.
   Registry-grade input — no LLM needed, GAEB is a defined format.
3. **Method E / embedding step** — runs the fixed extraction schema (METHODS.md §5a)
   over the lot's description text and computes the text embedding. The only step
   involving a model; offline and cached.
4. **Award joiner** — when an award XML exists for the procedure, writes the lot's
   actual value and bid count into the matching record's `targets` — the only mutation
   `features.jsonl` ever sees.

### Bidder-count label (implemented — `extractor.py`)

The bid count **must** be parsed from the award XML, never from the search API's
`received-submissions-type-val` (GitHub issue #1). That flat array mixes per-lot totals
(multi-lot notices) with a per-*type* breakdown (single-lot notices) and the two are
indistinguishable without the XML.

`parse_lot_results(xml_text)` returns one record per `<efac:LotResult>`:

| Field | Meaning |
| --- | --- |
| `lot_id` | the lot the result belongs to (`<efac:TenderLot><cbc:ID>`) |
| `n_bids` | headline bid count |
| `n_bids_source` | `tenders` (the explicit total, preferred) / `t-esubm` (fallback, a **lower bound**) / `None` (missing) |
| `n_bids_sme`, `n_bids_electronic`, `n_bids_other_eea`, `n_bids_non_eea`, … | subset counts, captured free from the same block |

Two data realities it handles, both observed live:

- **~18 % of lot results publish no `tenders` total**, only `t-esubm` (electronic
  submissions). Since electronic submissions are a subset of all tenders, the fallback
  is a lower bound — hence `n_bids_source`, so downstream can filter or flag it.
- **`StatisticsNumeric` can be negative** (`-1` = not disclosed). Negative values are
  treated as missing, never as counts.

Measured on 196 German construction award notices (223 lot results, July 2026):
median 5 bids, mean 6.0, max 26, 12 % single-bidder, 1 % zero-bid.

## Source priority per lot

1. **GAEB** (worker 2) — best: complete quantities.
2. **Description text** (worker 3) — Method E schema + embedding.
3. **Notice fields** (worker 1) — always present.

`source_level` records the best source that contributed (`gaeb` / `description` /
`fields_only`) so the modeling stage can measure the three data ladders separately.

## Output record format

```jsonc
// data/features.jsonl — one line per lot
{
  "procedure-identifier": "…",
  "lot-id": "LOT-0001",
  "publication-number": "518801-2026",   // provenance: notice the lot was read from
  "notice-type": "cn-standard",
  "source_level": "gaeb" | "description" | "fields_only",
  "gaeb_outcome": "ok" | "no_url" | … ,   // Download job's GAEB outcome, carried along
  "fields": { "cpv_division": "45", "nuts": "DEA2C", "procedure": "open", … },
  "quantities": [
    { "what": "asphalt_surface", "value": 45000, "unit": "m2", "source": "gaeb" },
    { "what": "earthworks",      "value": 2500,  "unit": "m3", "source": "description" }
  ],
  "embedding_ref": "embeddings/518801-2026_LOT-0001.npy",
  "targets": { "value_eur": null, "n_bids": null }   // filled by the award joiner
}
```

- Append-only: new records only; the award joiner's target update is the single
  permitted mutation.
- Targets stay `null` on tender records until the matching award arrives.
