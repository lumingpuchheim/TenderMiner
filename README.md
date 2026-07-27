# TenderMining

Scripts for exploring public-procurement (tender) open data from two sources:

- **oeffentlichevergabe.de** — the German *Bekanntmachungsservice* (national + EU notices).
- **TED** — *Tenders Electronic Daily*, the EU-wide service (above-threshold notices).

Findings are written up in [`FINDINGS_oeffentlichevergabe.md`](FINDINGS_oeffentlichevergabe.md)
and [`FINDINGS_ted.md`](FINDINGS_ted.md).

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

Queries the TED search API for recent contract-award notices (which carry money, winners,
and bid counts) and saves them as JSON Lines. Optional argument = how many to fetch
(default 50).

```bash
python ted_download_sample.py        # up to 50 notices
python ted_download_sample.py 200    # up to 200 notices
```

Output: `data/ted_awards_sample.jsonl` (one notice per line).

To change **what** is fetched, edit the `QUERY` and `FIELDS` constants near the top of
`ted_download_sample.py` (e.g. filter by country, CPV, or value range).

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
python ted_download_sample.py 200
python ted_explore_fields.py
```

## Notes

- `data/` is git-ignored — nothing downloaded is committed. Delete it anytime and re-run.
- On Windows, the scripts force UTF-8 console output so German/EU characters display correctly.
