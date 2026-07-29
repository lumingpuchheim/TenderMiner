# Component: Download job

First of the two pipeline components ([overview](../DATA_PIPELINE.md)):

```
[Download job] ──► [Extractor]
```

One program that talks to the network. For every in-scope notice it downloads the
official eForms **XML** (the single source of notice truth, including lot structure)
and — immediately, while the link is alive — follows the document link inside that XML
to retrieve the **GAEB** bill of quantities. Two payload formats, one job.

## Invocation and arguments

The job is a **finite run**: start → work → exit. No permanently-running process. Run
it daily (manually or via a scheduler); each run picks up exactly where the previous
one stopped. Missing a day (or a week) is harmless — the next run fetches the backlog.

```bash
python download.py --from 20240101 --to 20251231 --country DEU --cpv 45   # backfill a window, once
python download.py --country DEU --cpv 45                                  # continue from checkpoint
python download.py --country DEU --cpv-re "^452(1|2)"                      # regex CPV scope
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `--from` / `--to` (YYYYMMDD) | **Backfill mode:** fetch this publication-date window, then exit. | unset |
| *(no dates)* | **Continuation mode:** fetch everything new since the checkpoint, then exit. | — |
| `--country ISO3` | buyer-country fetch filter (e.g. `DEU`) | required |
| `--cpv CODES` | comma-separated; short code = prefix (server-side `45*`), 8 digits = exact | all CPVs |
| `--cpv-re REGEX` | true regular expression over CPV codes, applied **client-side** during discovery (the TED server only supports exact + prefix; the job queries the broadest safe prefix server-side, then filters the returned CPV lists with the regex **before** fetching any XML) | unset |
| `--limit N` | stop after N newly fetched notices (testing aid) | unlimited |

A notice is in scope if **any** of its CPV codes matches (trade lots inside a larger
project count — see MODELING.md §2.1).

**Warning (recorded advice):** a narrow scope at download time forfeits GAEB files
forever for everything it excludes (links die after tender deadlines). Filter
**coarse** here (country + CPV division, e.g. `--cpv 45`); express fine concepts like
"Sporthalle" as queries on the extracted data, not as download filters — fine CPV codes
are unreliable anyway (buyers tag by trade and misclassify).

## Scope decisions (recorded)

1. **Country filters fetching; language does not.** `--country` decides which notices
   are fetched. Language is an Extractor rule (keep the buyer's language text, e.g.
   German); the raw XML keeps whatever languages the buyer submitted.
2. **Discovery searches tenders + awards only** (`cn-standard`, `can-standard`).
3. **Procedure completion is automatic:** whenever a notice is fetched, the job also
   fetches **all other notices of the same procedure** (by `procedure-identifier` —
   the search API supports this query directly), even outside the date window. This is
   what makes tender→award training pairs complete: an award from June pulls in its
   tender from January.
4. **No planning notices** (`pin*`) — not searched, and skipped during completion.
   Corrigenda/change notices and contract modifications of in-scope procedures **are**
   fetched: a correction is a complete republished notice, and only the version chain
   tells us which is the latest. The Extractor uses only each procedure's **latest
   version**; earlier versions are history (and extend the GAEB retry window when a
   deadline was moved).
5. **GAEB retries ride along inside each finite run:** after fetching new notices, the
   run re-attempts GAEB links of still-live tenders whose previous attempts failed,
   then exits. A retry is a line in the next run's to-do list, not a waiting process.

## Input

| Input | Role |
| --- | --- |
| TED search API (`POST /v3/notices/search`) | **discovery only** — yields the publication numbers matching the scope, plus the completion lookups by `procedure-identifier`; responses are **never persisted** |
| e-procurement platforms (evergabe etc.) | reached via the document links found inside the fetched XMLs; source of the GAEB packages |
| `data/logs/checkpoint.json` | last covered publication date from the previous run → continuation mode |

## Output

| Output | Content |
| --- | --- |
| `data/raw/xml/<publication-number>.xml` | one file per notice, stored byte-for-byte as received, never edited |
| `data/raw/gaeb/<procedure-identifier>/…` | retrieved GAEB files; everything else in a package (PDF etc.) is discarded |
| `data/logs/manifest.jsonl` | one line per stored GAEB file — the authoritative GAEB↔tender/lot mapping |
| `data/logs/gaeb_outcomes.jsonl` | one line per notice per attempt — feeds the retry pass and measures link rot |
| `data/logs/ingest_log.jsonl` | one line per run: query, notices fetched, **single-/multi-lot counts, lot histogram**, GAEB outcome counts, per-platform breakdown |
| `data/logs/platform_report.json` | regenerated each run from the full outcome history: per platform → handled?, attempts, ok-rate, last success. **The file to read when yield drops** ("RIB: 0 % since <date>") |
| `data/logs/checkpoint.json` | updated last covered date |

## Behaviour

Per run:

1. **Discover:** query the search API for the scope (window from `--from/--to` or the
   checkpoint); apply `--cpv-re` client-side if given; keep only the publication
   numbers + procedure identifiers.
2. **Fetch XML:** for each number not yet in `data/raw/xml/`, GET
   `https://ted.europa.eu/en/notice/<number>/xml` (verified: no login, one request,
   ~26 KB) and store it unmodified.
3. **Complete procedures:** for every procedure touched in step 2, look up its other
   notices by `procedure-identifier` (skipping `pin*`) and fetch any missing XMLs.
4. **Fetch GAEB (immediately, best-effort):** read the document links from each newly
   fetched tender XML and attempt retrieval — links rot after award, so
   fetch-at-publication beats backfill. Policy below.
5. **Retry pass:** re-attempt GAEB for previously failed, still-live tenders.
6. **Checkpoint:** record the last covered publication date; append the run line to
   `ingest_log.jsonl`; exit.

Nothing is ever re-downloaded or overwritten (append-only principle): existing XML
files are skipped, manifest and logs only grow.

## XML part — lot handling

Per-lot data comes from the XML's `ProcurementProjectLot` / `LotResult` blocks — never
from search-response arrays (their flat lists lose lot alignment and can even fake
multi-lot on single-lot notices; see MODELING.md §2.3). Route 1 (time decision) trains
on single-lot notices first; Route 2 parses multi-lot XMLs later. Since the XML is
fetched for every notice anyway, Route 2 is purely an Extractor upgrade — the Download
job is identical in both routes.

## GAEB part — what and why

The notice is only the advertisement. The full scope — the complete bill of
quantities — lives in the procurement documents behind the notice's document link
(eForms BT-15, per **lot**; platforms often serve one package for the whole procedure).
In Germany that is usually a **GAEB file**: standardized, machine-readable, every
position with code, description, quantity, unit. **PDFs are ignored** — no PDF parsing
in this pipeline.

Accepted GAEB types: GAEB DA XML `.x81`–`.x86` (tender phase: `.x83`) and legacy
GAEB90/2000 `.d81`–`.d86` / `.p81`–`.p86`.

## GAEB part — the platform layer

Notices link to hundreds of portals, but those portals run on a **handful of software
products**. Handlers are therefore written per *product*, not per website, and product
signatures in the URL path beat host names — the same software is white-labelled across
many domains (`/VMPSatellite/` on every state Vergabemarktplatz, `evergabe.bieter` on
Deutsche Bahn's portal). Two components:

- **`PLATFORM_HOSTS` + `detect_platform()`** — maps a document URL to a product name;
  unrecognised hosts surface as `other:<host>` so they are visible, never silently
  lumped together.
- **`HANDLERS`** — product → handler function. Products without a handler fall back to
  the generic strategy (visible file links only), which fails on JavaScript-rendered
  pages; that is expected and recorded, not an error.

Every attempt records its platform, so `data/logs/platform_report.json` can answer
"which platform stopped working, and since when".

## GAEB part — best-effort policy (required behaviour)

Document access is legally guaranteed only for **open procedures** and only **while the
tender is live**. The survival rate is unknown — so the job must **try its best on
every notice and record the outcome instead of failing**:

1. Follow the link; use only the direct/no-registration path (the legally required
   "ohne Anmeldung" access). **Never create accounts, never bypass a registration
   wall, never solve a CAPTCHA** — record and move on.
2. Keep only GAEB files; store under `data/raw/gaeb/<procedure-identifier>/` (the
   procedure, not the notice, owns the documents — corrigenda create new
   publication-numbers for the same documents, and award notices carry no link).
3. Write one manifest entry per stored file (below).
4. Log one outcome per notice:

| outcome | meaning |
| --- | --- |
| `ok` | ≥1 GAEB file retrieved |
| `no_url` | notice has no document link |
| `no_gaeb` | documents reached, but none is a GAEB file (e.g. the LV is published as PDF) |
| `registration_wall` | documents exist but require an account — **a policy limit, permanent** |
| `needs_javascript` | the page renders its file list client-side and no handler exists yet — **a tooling gap, fixable** |
| `dead_link` | URL unreachable / tender taken down / deadline passed (expected for old notices) |
| `error` | anything else (recorded with detail, non-fatal) |

`registration_wall` and `needs_javascript` must not be conflated: the first is a
decision we will not reverse, the second is a to-do item. Misfiling one as the other
hides work (or invents a wall that is not there).

5. Retries while the tender is live (each run's retry pass); after the award, one
   final attempt, then stop — `dead_link` becomes a permanent, expected state.

No outcome blocks the pipeline: lots without GAEB proceed through the Extractor with
description-only features (the fallback path METHODS.md §5a.4 is designed for).

## GAEB part — download efficiency (required behaviour)

Handlers fall into two kinds:

- **Selective** (rib, evergabe.de, evergabe-online, deutsche-evergabe, staatsanzeiger):
  the platform exposes per-file URLs, so only GAEB/LV-named files are fetched.
- **Bulk-only** (cosinex-satellite, cosinex-netserver, aumass): the platform offers no
  per-file access — the whole package is the only door. Observed sizes: 39 MB (a DTVP
  archive containing **no** GAEB at all) and 174 MB (aumass). Roughly 37 % of tenders
  land on a bulk-only platform, and most of those bytes are drawings we discard.

Bulk downloads are therefore the run's bottleneck, and the job **must avoid them when
the package cannot contain a GAEB file**:

1. **Listing pre-check.** If the platform prints filenames before download (cosinex
   Satellite does), scan the listing first and skip the archive when no name matches a
   GAEB/LV pattern.
2. **ZIP central-directory peek.** A ZIP stores its index at the *end* of the file. Where
   the server supports HTTP `Range` requests, fetch only the last ~64 KB, read the entry
   names from the central directory, and download the full archive **only if** a GAEB
   file is actually inside. Fall back to a full download when ranges are unsupported.
3. **Caps remain the backstop:** `FILE_CAP_BYTES` / `FILE_TIME_BUDGET_S` for per-file
   downloads, and a larger `ARCHIVE_CAP_BYTES` / `ARCHIVE_TIME_BUDGET_S` for bulk-only
   platforms, so no single tender can stall a run.

## GAEB part — the manifest

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

## Required protocol

Each run appends one line to `data/logs/ingest_log.jsonl`:

```jsonc
{ "run": "2026-07-30T06:00Z", "query": "…", "notices_fetched": 412,
  "single_lot": 371, "multi_lot": 41, "lots_histogram": {"1": 371, "2": 18, "3": 9, "4+": 14},
  "gaeb": {"ok": 118, "no_url": 61, "no_gaeb": 155, "registration_wall": 40, "dead_link": 30, "error": 8},
  "gaeb_by_platform": {"cosinex-satellite": {"ok": 41, "no_gaeb": 30}, "subreport": {"registration_wall": 40}} }
```

The single-vs-multi-lot count sizes the Route 1 training set and tells us when the
Route 2 lot parser pays off; the GAEB outcome counts measure link rot, and the
per-platform breakdown feeds `platform_report.json`, which ranks unhandled platforms by
volume — the to-do list for the next handler.

## Measured platform distribution (German construction, July 2026)

From 1 988 `cn-standard` notices with a document link (CPV 45\*, `buyer-country=DEU`).
Handlers are written against this ranking, not guesses:

| Platform | Share | Status |
| --- | --- | --- |
| cosinex-satellite (DTVP, VMPSatellite) | 18.9 % | handled (bulk) |
| cosinex-netserver | 15.7 % | handled (bulk) |
| rib | 11.9 % | handled (selective) |
| subreport | 10.0 % | **registration wall** — documents require an account |
| deutsche-evergabe (incl. DB `evergabe.bieter`) | 7.2 %+ | handled (selective) |
| evergabe.de | 4.9 % | handled (selective) |
| evergabe-online | 3.4 % | handled (selective) |
| aumass | 2.9 % | handled (bulk) |
| vergabe24 | 2.1 % | partial — expiry detected; download path unobserved |
| staatsanzeiger evoportal | <1 % | handled (selective) |
| unhandled long tail | ~9 % | `staatsanzeiger-eservices` 3.2 %, `deutsches-ausschreibungsblatt` 2.3 %, rest <1 % each |

**Attemptable coverage: ~81 %** of tenders with a document link; ~10 % walled, ~9 %
not yet implemented.
