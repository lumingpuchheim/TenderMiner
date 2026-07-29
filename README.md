# TenderMining

Scripts for exploring public-procurement (tender) open data from two sources:

- **oeffentlichevergabe.de** — the German *Bekanntmachungsservice* (national + EU notices).
- **TED** — *Tenders Electronic Daily*, the EU-wide service (above-threshold notices).

Findings are written up in [`FINDINGS_oeffentlichevergabe.md`](FINDINGS_oeffentlichevergabe.md)
and [`FINDINGS_ted.md`](FINDINGS_ted.md). The sourced value case for the project is in
[`BUSINESS_CASE.md`](BUSINESS_CASE.md).

## Requirements

- **Python 3** (tested on 3.13). **No third-party packages** — standard library only
  (`urllib`, `json`, `zipfile`). Nothing to `pip install`.
- Internet access. Both APIs are public and need **no API key**.
- Run every command from the repository root. Downloaded data lands in `data/`
  (git-ignored; safe to delete and re-fetch).

```bash
cd C:\Users\user\workspace\TenderMining
```

---

## oeffentlichevergabe.de (German source)

The API only serves a **whole day** (or month) as a ZIP; there is no server-side filter.
These scripts download a day, unpack the JSON notices, and survey the fields.

### 1. Quick schema sample (small & fast)

Downloads one low-volume early day (~0.7 MB), unpacks it, prints a sample notice.

```bash
python oeffentlichevergabe_download_sample.py
```

Pick a specific day (any date from `2022-12-01` to the day before yesterday):

```bash
python oeffentlichevergabe_download_sample.py 2023-01-16
```

Output: plain JSON files in `data/notices_<date>/`.

### 2. Analyze any day (field survey)

Downloads a day and prints coverage of money / region / bidders / CPV / lifecycle.
**No timeout** — a recent day is tens of MB and streams slowly; it waits and shows a
byte counter. Defaults to a recent day.

```bash
python oeffentlichevergabe_analyze_day.py            # default recent day
python oeffentlichevergabe_analyze_day.py 2026-07-20 # a specific day
```

Output: JSON files in `data/notices_<date>/` plus a printed survey.

### 3. Field-coverage survey of already-downloaded data

Surveys a folder you already downloaded. **Note:** the folder date is set inside the
script (`explore_fields` glob) — edit the `data/notices_<date>` path near the top to
point at the folder you want.

```bash
python oeffentlichevergabe_explore_fields.py
```

---

## TED (EU source)

TED has a real search API with **server-side filtering** — you send a query and get back
only the fields you ask for. No whole-day downloads.

### 1. Download a sample of award notices

Queries the TED search API for contract-award notices (which carry money, winners, and bid
counts) and saves them as JSON Lines. All arguments are optional:

| Argument | Meaning | Default |
| --- | --- | --- |
| `--from YYYYMMDD` | start publication date (inclusive) | `20260701` |
| `--to YYYYMMDD` | end publication date (inclusive) | today (no upper bound) |
| `--max N` | max notices to download, **newest first** | `50` |
| `--type CODE` | notice-type: `can-standard` (award, has money) / `cn-standard` (call) / `pin` … | `can-standard` |
| `--country ISO3` | restrict to a buyer country (e.g. `DEU`, `FRA`) — handy to fix one language | all |
| `--cpv CODES` | CPV filter, comma-separated. Short code = prefix (`33`=medical, `72`=IT); 8-digit = exact | all |
| `--out PATH` | output `.jsonl` path | `data/ted_awards_sample.jsonl` |
| `--lang LANG` | force a text language (3-letter, e.g. `deu`) | buyer's language + English fallback |
| `--raw` | keep full untrimmed notices (skip the shrink step) | off (trimmed) |

**Saved notices are trimmed by default** (~80% smaller) to keep the file lean and
single-language:

- drops the `links` block — all per-language PDF/HTML URLs are rebuildable from
  `publication-number`, so nothing is lost;
- collapses every multilingual field to **one** language — Rule A: the buyer's own
  language (from `buyer-country`), falling back to English;
- de-duplicates code lists such as `classification-cpv`.

Trimming is lossless in practice — any dropped detail is reproducible from TED via the
`publication-number`. Use `--raw` to keep everything, or `--lang deu` to force a language.

```bash
python ted_download_sample.py                                   # newest 50 awards since 2026-07-01
python ted_download_sample.py --max 2000                        # newest 2000 in the default window
python ted_download_sample.py --from 20250101 --to 20251231 --max 2000   # all of 2025
python ted_download_sample.py --from 20250101 --country DEU     # German buyers only (one language)
python ted_download_sample.py --cpv 33,72                       # only medical (33) or IT (72) tenders
python ted_download_sample.py --type cn-standard               # calls for bids instead of awards
python ted_download_sample.py --help                           # full argument list
```

**`--max` is a count of notices, not a time span.** The time span is `--from` / `--to`;
`--max` just limits how many of the matching notices you pull (newest first). The script
prints how many notices match your window, e.g. `fetched 2000/2000 (of 29207 ... total)`.

Output: `data/ted_awards_sample.jsonl` (one notice per line). To change **which fields**
are returned, edit the `FIELDS` list near the top of `ted_download_sample.py`.

### 2. Survey the sample

Reads `data/ted_awards_sample.jsonl` and prints money / bid-count / winner-size /
winner-country / CPV summaries. Run it after step 1.

```bash
python ted_explore_fields.py
```

---

## Typical workflows

**Understand the German schema, fast:**

```bash
python oeffentlichevergabe_download_sample.py     # small sample -> data/notices_2023-01-16/
```

**Compare a recent German day (money/bidders appear on EU-threshold awards):**

```bash
python oeffentlichevergabe_analyze_day.py 2026-07-20
```

**Get clean EU-wide award data (structured money + bidders):**

```bash
python ted_download_sample.py --max 200
python ted_explore_fields.py
```

## Notes

- `data/` is git-ignored — nothing downloaded is committed. Delete it anytime and re-run.
- On Windows, the scripts force UTF-8 console output so German/EU characters display correctly.
