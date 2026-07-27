# TenderMining — TED Data Source Findings

Exploration of **TED — Tenders Electronic Daily** ([ted.europa.eu](https://ted.europa.eu)),
the EU-wide publication service for public procurement (the *Supplement to the Official
Journal of the EU*). Companion to [`FINDINGS_oeffentlichevergabe.md`](FINDINGS_oeffentlichevergabe.md),
which covers the German national source.

## 1. The data source

- **Real search API with a query language** — the big difference from the German feed:
  `POST https://api.ted.europa.eu/v3/notices/search`
- **Read-only, NO API key** needed to search published notices. (A key is required only to
  *submit* or modify notices.)
- **Server-side filtering:** you send an "expert search" query (`=`, `>=`, `IN`, `~` contains,
  `AND/OR/NOT`, `SORT BY`) and choose exactly which fields to return. No downloading whole
  days and filtering locally.
- **Coverage:** EU/EEA-wide, **above-threshold** procurement, in all 24 EU languages, with
  decades of archives (daily OJ S editions).
- **Swagger / spec:** `https://api.ted.europa.eu/swagger`, spec at `https://api.ted.europa.eu/api-v3.yaml`.

### Request / response shape

```
POST /v3/notices/search
{
  "query": "notice-type=can-standard AND publication-date>=20260701 SORT BY publication-date DESC",
  "fields": ["publication-number","total-value","winner-country", ...],
  "limit": 100, "scope": "ALL",
  "paginationMode": "ITERATION", "iterationNextToken": "<from previous call>"
}
-> { "notices": [ { <requested fields>, "links": {"xml": {...}, "pdf": {...per language}} } ],
     "totalNoticeCount": 29207, "iterationNextToken": "..." }
```

- **Fields:** ~**1830** selectable eForms "business term" fields (e.g. `total-value`,
  `winner-size`, `received-submissions-type-val`). Cryptic but exhaustive.
- **Pagination:** `PAGE_NUMBER` (up to 15 000 results) or `ITERATION` (token-based, for more).
- **Per-notice full document:** each result's `links` give XML (eForms) + PDF in every language.
- **Bulk alternative:** daily XML packages are also published on `data.europa.eu` for
  whole-corpus work.

## 2. What can be mined — and it's richer than the German feed

Measured on a live sample of **120 recent contract-award notices** (`can-standard`),
July 2026:

| Signal | Field | Coverage | Notes |
| --- | --- | --- | --- |
| **Description (text)** | `description-proc` / `description-lot` | **100 %** | free-text object of the contract — the ML input |
| **Title** | `title-proc` / `notice-title` | **100 %** | short title |
| **Money** | `total-value` (+`-cur`) | **89 %** | median **€417k**; structured, not free text |
| **Competition** | `received-submissions-type-val` | **99 %** | tenders received; median **2**, max 82 |
| **Single-bidder rate** | derived | — | **27 %** of awards had just 1 bid |
| **Winner size (SME)** | `winner-size` | high | large / medium / small / micro / sme |
| **Winner country** | `winner-country` | high | DEU 21, FRA 15, CZE 12, POL 10… |
| **Category** | `classification-cpv` | ~100 % | 45xx works, 71xx eng., 72xx IT, 33xx medical |
| **Buyer** | `organisation-name-buyer` | ~100 % | multilingual `{lang: [...]}` |
| **Full notice** | `links.xml` / `links.pdf` | 100 % | eForms XML + PDF, all EU languages |

**Key contrast with the German source:** money and bidder counts here are *first-class,
near-complete structured fields*, because every TED notice is an above-threshold EU eForms
notice (see §6 of the German findings). No free-text extraction and no ML needed for money.

### Ready-made ML dataset: description → value

Because **description is 100 %** and **value is 89 %**, TED directly yields
`(text, value)` pairs — e.g. predict/estimate contract value or CPV category from the
description, or model bid count vs. description/CPV/country. A real pair from the sample:

> text: *"Suministro de tiras reactivas y cesión de equipos… determinación de glucosa en sangre…"*
> value: **141 900 EUR** (CPV 33696, medical)

**Caveats that matter for training:**
- **Multilingual** — descriptions are in the buyer's own language (Spanish, French, Polish,
  German…). Either filter by `buyer-country`/language, or translate, or use a multilingual model.
- **Mixed currencies** — `total-value-cur` is not always EUR (saw PLN); convert to one currency
  before using value as a target.
- **Outliers** — a few values are framework ceilings / unit artefacts (one read €36 bn); clip or
  winsorise, and prefer the median for summaries.
- **Award-only** — value exists on award (`can-standard`) notices, not on the initial call.

## 3. How easy is it to mine?

**Easier than the German feed in the ways that matter:**

- 🟢 **Server-side filter** — ask for exactly the notices you want (country, CPV, value range,
  date, notice type) in one query. No bulk download.
- 🟢 **Field selection** — return only the columns you need; response is already tabular-ish.
- 🟢 **Money / bids / winner / SME** — structured and near-complete on award notices.
- 🟢 **No auth** for reading.

**Harder in a few ways:**

- 🟡 **Cryptic field names** — 1830 eForms business terms (`BT-759`, `received-submissions-type-val`);
  you must look up the right one. Invalid names return HTTP 400 with the full supported list.
- 🟡 **Multilingual nested values** — many fields are `{lang: [values]}` or arrays; needs flattening.
- 🟡 **Data-quality outliers** — e.g. one sample value read €36 billion (a framework ceiling /
  unit artefact). Use the **median**, and sanity-check extreme values.
- 🟡 **Pagination for bulk** — 15 000-per-query cap; use the iteration token for larger pulls.

## 4. TED vs. oeffentlichevergabe.de — when to use which

| | **oeffentlichevergabe.de** | **TED** |
| --- | --- | --- |
| Scope | Germany, incl. **below-threshold** national notices | **EU-wide**, above-threshold only |
| Filtering | none (whole day/month ZIP) | **full server-side query** |
| Delivery | ZIP bundles (OCDS/eForms/CSV) | JSON search + per-notice XML/PDF |
| Money | only ~10–20 % (EU-threshold awards) | **~89 %** of award notices |
| Bidder counts | rare | **~99 %** of award notices |
| Winner / SME | partial | structured (`winner-size`, country) |
| Auth | none | none (for search) |
| Granularity | whole day | single notice / arbitrary query |

**Overlap:** German *above-threshold* notices are forwarded to TED, so they appear in both.
German *below-threshold* notices appear **only** on oeffentlichevergabe.de.

**Rule of thumb:**
- Want **German coverage including small/national** contracts → oeffentlichevergabe.de.
- Want **EU-wide, clean structured money/bidders/winners** → TED.
- Want the **fullest German picture** → combine: TED for above-threshold richness,
  the national feed for the below-threshold long tail.

## 5. Recommended next steps

1. **Flatten to a table** — one row per notice (buyer, country, CPV, value, currency,
   n_bidders, winner-country, winner-size), straight from the selected fields.
2. **Cross-border & SME analysis** — TED uniquely supports "who wins across borders" and
   "SME win rate by sector/country".
3. **Competition monitoring** — single-bidder rate by CPV/country over time (27 % in-sample).
4. **De-duplicate against the German feed** on above-threshold notices when combining sources.

## Scripts

- `ted_download_sample.py` — query the TED search API (server-side filter) and save a sample
  of award notices as JSON Lines (`data/ted_awards_sample.jsonl`).
- `ted_explore_fields.py` — survey that sample: money, bid counts, winner size/country, CPV.

Data lands in `data/` (git-ignored — re-fetch with the scripts above).
