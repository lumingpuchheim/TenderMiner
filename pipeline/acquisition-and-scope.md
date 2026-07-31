# Component: Acquisition and scope

Supersedes the acquisition half of [`DATA_PIPELINE.md`](../DATA_PIPELINE.md) and adds a
scope contract to the extractor. Two changes, one motivating finding.

## 1. Why this exists

The TED Search API silently returns part of a result set and stops. Measured:

| Query scope | TED reports matching | Iteration returned |
| --- | --- | --- |
| 12 months, DEU, CPV 45 | 56,573 | 36,214 (64%) |
| 1 month (2025-06), same | 4,709 | 3,000 (64%) |
| 1 day (2025-06-06), same | 273 | 250 |

Monthly match counts sum exactly to the annual figure, so the shortfall is real and not
a counting artefact. The loss is not random: results are sorted by publication date, so
truncation drops the **later** part of every range.

Three properties make this unfixable from the client side:

- The `iterationNextToken` does not continue past the stop — reusing it **restarts** the
  scan. A one-day query of 237 notices looped to 33,654 rows of which 237 were distinct.
- Splitting the date range narrows the result set, but a single day is the smallest slice
  a date filter can express. Days holding more notices than iteration will return are
  unreachable by any amount of splitting.
- The documentation states ITERATION has *no* cap on retrievable notices (unlike PAGE
  mode's 15,000), and does not document a termination condition at all. The behaviour is
  undocumented, so it cannot be relied on or worked around by contract.

**TED publishes the same notices as complete archives.** One `tar.gz` per month, no
authentication, containing every notice published EU-wide:

```
https://ted.europa.eu/packages/monthly/2025-6    → 2025-06.tar.gz   ~350 MB
https://ted.europa.eu/packages/daily/202500106   → 20250604_2025106.tar.gz  ~20 MB
```

A package has no result set to page through, so completeness stops being something the
code must verify and becomes a property of the file. This is the primary acquisition
route; the Search API is retained only for incremental updates.

## 2. Architecture

```
                       BROAD                          SCOPED
[bulk.py]      ──┐
 (packages)      ├──► data/raw/xml/ ──► [features.py --scope] ──► tenders_<scope>.parquet
[download.py]  ──┘                                                awards_<scope>.parquet
 (incremental)
```

### The principle: broad raw, scoped refine

Acquisition filters only on what the **source** can express — country, CPV, publication
date. Everything else is refinement and happens at feature-build time.

The reason is that the interesting questions are not expressible at acquisition. "Bridges
in Hesse" needs a region, and neither the API nor the packages carry a region filter.
Which CPV subdivision matters is usually decided after looking at the data. If those
choices were made during download, changing your mind would mean re-downloading — hours,
and 4.2 GB, to answer a question you could answer from files already on disk.

So the raw layer is deliberately **broader than any single analysis**, and every analysis
states its own scope.

## 3. Components

### 3.1 `bulk.py` — primary acquisition

| | |
| --- | --- |
| Input | TED monthly packages; a country, CPV scope and date range |
| Output | `<out-dir>/<publication-number>.xml`, one per in-scope notice |
| Network | 12 requests per year of data |
| Completeness | By construction |

```bash
python bulk.py --from 20250601 --to 20260531 --country DEU --cpv 45
```

| Argument | Default | Meaning |
| --- | --- | --- |
| `--from`, `--to` | required | publication date bounds, `YYYYMMDD` |
| `--country` | required | ISO3 buyer country |
| `--cpv` / `--cpv-re` | none | CPV prefixes or regex; matches **any** CPV on the notice |
| `--out-dir` | `data/raw/xml` | where XML is written |
| `--packages-dir` | `data/raw/packages` | where archives are kept |
| `--discard-archives` | off | delete archives after processing |

**Archives are kept by default.** They are the only thing that makes a change of scope
cheap: re-selecting for a different country or CPV becomes a local re-scan instead of a
re-download. Keeping extracted XML for everything instead would cost ~59 GB a year
against ~4.2 GB compressed for the same information.

Filenames must match `download.py`'s convention exactly, or the two routes fetch the same
notice twice under two names. The archive member `00361433_2025.xml` carries publication
id `00361433-2025`; the file is written as `361433-2025.xml`, zero padding stripped.

Acquisition-time filtering is exact, not approximate:

- **Notice type** — `cbc:NoticeTypeCode` in `{cn-standard, can-standard}`.
- **CPV** — any `ItemClassificationCode` matching, mirroring the API query semantics.
- **Country** — the **buyer** organisation's country, resolved through the ORG reference
  in `cac:ContractingParty`. Matching `DEU` anywhere in the document would wrongly admit
  Austrian, Swiss and French notices that merely name a German winner or appeal body —
  29 such cases in a single day's package.
- **Date** — a monthly package spans the whole month; notices outside the requested range
  are dropped so the corpus matches the stated bounds.

Verified against a real package: 3,555 members scanned, 237 accepted — the same count the
Search API reports for that day — and the written files are byte-identical to those the
API route produced.

### 3.2 `download.py` — incremental acquisition

Unchanged in role, demoted in importance. Suited to keeping a corpus current (a handful
of notices a day, where per-notice fetching is cheap and truncation cannot bite), not to
backfilling a year.

Gains `--out-dir` (default `data/raw/xml`) so it can target the same folders as `bulk.py`.

### 3.3 `features.py` — refine

| | |
| --- | --- |
| Input | `--xml-dir` (default `data/raw/xml`), no network |
| Output | `tenders_<scope>.parquet`, `awards_<scope>.parquet` |
| Scope | **mandatory** |

```bash
python features.py --cpv 45221 --nuts DE7      # bridges in Hesse
python features.py --all                       # everything, deliberately
```

## 4. Scope contract (normative)

**Running with no scope is an error.** With a broad raw folder, forgetting a filter is
the most likely mistake and the hardest to notice: nothing crashes, and a parquet mixing
IT projects into a construction analysis looks entirely normal. The failure is silent and
may survive weeks.

```
error: no scope given. Say what you want to analyse:
  --cpv 45221      bridges
  --nuts DE7       Hesse
  --all            everything in the folder, deliberately
```

`--all` exists so that analysing everything remains possible — but as something typed,
not something obtained by forgetting. Same output, but one is a decision and the other an
accident.

| Filter | Matches against | Semantics |
| --- | --- | --- |
| `--cpv` | `cpv_main` | prefix, comma-separated, **main CPV only** |
| `--nuts` | `place_nuts3`, else `buyer_nuts` | prefix, comma-separated |
| `--from`, `--to` | `issue_date` | inclusive bounds |

**CPV matches the main classification only.** The additional codes list secondary trades,
so matching them would pull a school building into a bridge analysis because it includes
a footbridge. Acquisition already did the loose match; refinement is precise.

**NUTS matches the place of performance**, not the buyer's address — a federal buyer in
Berlin can procure a bridge in Hesse. It falls back to the buyer's region for the ~1% of
lots stating no place.

**Awards follow their tenders.** After filtering tenders, the award table is restricted to
the surviving `(procedure_id, lot_id)` keys. An award for an out-of-scope lot is not part
of the analysis, and keeping it would leave rows that join to nothing. Consequence: an
award whose call was never acquired is dropped even if the award itself is in scope.

### Output naming

Filenames are derived from the scope, so two analyses cannot overwrite each other:

```
tenders_cpv45221_nutsDE7.parquet
awards_cpv45221_nutsDE7.parquet
tenders_all.parquet
```

`--tenders-out` / `--awards-out` still override explicitly.

## 5. Storage

Per year of the German construction scope:

| Stage | Location | Size |
| --- | --- | --- |
| Packages (all EU, kept) | `data/raw/packages/` | ~4.2 GB |
| Selected XML | `data/raw/xml/` | ~1.8 GB |
| Feature tables | `data/*.parquet` | ~5 MB |

A German construction scope is about 6% of what a package contains (56,573 of roughly
890,000 EU notices a year). Uncompressed, one day is 240 MB across 3,555 notices.

## 6. What changes from today

1. New `bulk.py`; `download.py` keeps its role but stops being the backfill route.
2. `bulk.py` keeps archives by default.
3. `download.py` and `bulk.py` gain `--out-dir`.
4. `features.py` gains `--cpv`, `--nuts`, `--from`, `--to`, `--all`, and **refuses to run
   without one of them**.
5. `features.py` output filenames derive from the scope.
6. The existing 12,824-notice corpus was acquired through the truncated route and is
   ~64% of its nominal scope, skewed to earlier dates. It needs re-acquiring via `bulk.py`
   before any result is trusted. Nothing needs deleting — files are keyed by publication
   number and the missing ones are simply added.

## 7. Open questions

- **Package download rate.** One 20 MB package took 103 s (~200 KB/s) in testing. If that
  rate holds, a year is ~6 hours; at a few MB/s it is under an hour. Unknown whether the
  limit is TED, the test environment, or the connection. Measure before committing to a
  full backfill.
- **Whether `download.py`'s Search API path is worth keeping at all.** It is currently the
  only incremental route, but daily packages (~20 MB) would serve the same purpose with
  the same completeness guarantee, and would let the search-API code, its retry logic and
  its checkpointing be deleted.
- **The 5,000-odd notices already fetched outside any recorded scope.** They are German
  construction as far as we know, but nothing in the repo asserts it. Under a broad-raw
  design this matters less, since scope is applied at build time.
