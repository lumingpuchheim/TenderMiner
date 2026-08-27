# TenderMining

Predicts, on the day a German construction tender is published, whether it will
end with **0 or 1 bids** — a low-competition lot a contractor can win without a
price war. Data comes from **TED** (*Tenders Electronic Daily*, the EU-wide
publication service); the German *Bekanntmachungsservice*
(oeffentlichevergabe.de) was explored as an alternative source.

Key documents: [`ONLINE_LEARNING.md`](doc/ONLINE_LEARNING.md) (the running system),
[`TRAINING.md`](doc/TRAINING.md) (model recipe + leakage rules),
[`FIELDS.md`](FIELDS.md) (every data field and its role),
[`DATA_PIPELINE.md`](doc/DATA_PIPELINE.md) (architecture),
[`BUSINESS_CASE.md`](doc/BUSINESS_CASE.md) (value case),
[`MARKET_AND_COMPETITORS.md`](doc/MARKET_AND_COMPETITORS.md) (the market by
trade, what competitors sell and charge, what we may claim),
[`ONBOARDING.md`](doc/ONBOARDING.md) (target list → letter → free weeks →
subscription, with every open decision marked),
[`train_single_bidder.ipynb`](train_single_bidder.ipynb) (the original
experiment, fully explained in plain language).

## The production pipeline — what runs and how

```
cycle.py    (the update — any day, mails nobody)
   ├─ calls bulk.py       download new notices from TED packages
   ├─ calls features.py   parse notice XML -> the two parquet tables
   └─ imports single_bidder.py   train / evaluate / predict functions
deliver.py  (the sending — once a week, from what the cycle wrote)
```

### cycle.py — the update

One command executes the whole predict → grade → retrain cycle described in
[`ONLINE_LEARNING.md`](doc/ONLINE_LEARNING.md):

```bash
python cycle.py run --last 7d
```

What one run does, in order:

1. **Download** the last X days of notices (`--last 7d/2w/6m`; the window widens
   automatically to cover any gap since the previous successful run).
2. **Rebuild the store** — `data/store/tenders.parquet` + `awards.parquet` from
   the raw XML archive.
3. **Grade**: every past prediction whose award has now been published is marked
   right/wrong in `data/ledger/grades.jsonl` — the verified track record.
4. **Learn**: retrain a candidate model; it replaces the current champion only
   if it passes the trust checks (leakage tripwires) *and* matches or beats the
   champion's validation score. Models live in `models/<id>/`;
   `models/CURRENT` names the champion.
5. **Predict**: score every open lot (deadline not passed) with the champion;
   append to `data/ledger/predictions.jsonl` (append-only, never rewritten).
6. **Report**: write `data/reports/report_<date>.md` — track record first, then
   the ranked list of open lots, then a health footer.

Nothing in it mails a customer. That is the second command:

### deliver.py — the sending

```bash
python deliver.py run
```

Reads the delivering model's latest prediction per open lot from the ledger,
renders every active customer's report, mails it, and records what each
customer saw. It trains nothing; if the newest prediction is older than
`--max-age` (default 1d) it refuses and says which cycle to run first.
Idempotent per day. Until 2026-08-18 the two were one command, `loop.py`.

Useful options (all windows are parameters, nothing is hard-coded):
`--cpv 45` scope (default construction) · `--country DEU` · `--threshold 0.5`
flag cut-off · `--val-window 8w` promotion-gate window · `--track-window 12w`
track-record window · `--skip-download` offline run on the existing store ·
`--data-dir` / `--models-dir` alternative locations.

Scheduled on the server by cron ([`docker/crontab`](docker/crontab)): the
cycle Mondays 07:00, the delivery Mondays 11:00 — [`RUNBOOK.md`](doc/RUNBOOK.md) §1.

### bulk.py — bulk notice download from TED packages

Fetches every notice in a date window by downloading TED's prebuilt packages
and filtering locally (country / CPV / date) — complete by construction, unlike
the Search API, which silently truncates large result sets.

```bash
python bulk.py --from 20260101 --to 20260630 --country DEU --cpv 45
```

- Finished months come from **monthly packages**; the **running month** (whose
  monthly package TED has not published yet) is covered by **daily packages**
  automatically. Re-runs skip finished months/days (state in
  `data/logs/bulk_state.json`); re-downloaded notices dedup by ID.
- Output: one XML per notice in `data/raw/xml/` — the raw archive, the one
  thing that must be backed up.
- `--redo` reprocesses done months; `--keep-archives` keeps the tar.gz files;
  `--cpv-re` regex scope instead of `--cpv`.

### features.py — notice XML → the two parquet tables

Parses the raw archive into the tables everything downstream reads. Each column
carries a `role` tag in the parquet metadata (see [`FIELDS.md`](FIELDS.md));
feature selection is driven by these roles, never by hand-picked lists.

```bash
python features.py --xml-dir data/raw/xml --cpv 45 \
    --tenders-out data/store/tenders.parquet --awards-out data/store/awards.parquet
```

- `tenders.parquet` — one row per (procedure, lot) *per notice version*, call
  time only. `awards.parquet` — one row per lot result (outcome + bid count).
- `--from/--to/--nuts` narrow the scope; `--coverage` prints fill rates;
  `--fields-doc` regenerates FIELDS.md.
- **Do not use `--deduplicate` for training data** — every revision must stay a
  row (TRAINING.md, leakage rule 3).

### single_bidder.py — the model logic (importable module, no CLI)

The training/estimation/prediction functions used by both the notebook and
cycle.py: `load_with_roles`, `assemble` (label + source firewall),
`build_features` (role-driven), `temporal_split` (group-aware, 1/k weights),
`train` / `predict` / `metrics`, `cpv4_baseline`, the tripwire helpers, and
`open_tenders`. Import it; don't run it:

```python
import single_bidder as sb
tenders, roles = sb.load_with_roles('data/store/tenders.parquet')
```

(`buyer_history` exists as a documented experiment only — deliberately not used
by the pipeline.)

### download.py — Search-API download job (alternative to bulk.py)

The older network component (spec: `pipeline/download.md`): discovers notices
via the TED Search API, fetches their XML, and attempts to retrieve GAEB
bill-of-quantities files from the buyer platforms linked inside each notice.
Use it when GAEB retrieval matters; **the loop uses bulk.py**, which is
complete by construction and needs no discovery step.

### extractor.py — quick stats on the raw archive

```bash
python extractor.py stats data/raw/xml     # per-lot bidder counts, summary
```

### market.py — the business-developer view

Answers the three questions behind [`doc/GO_TO_MARKET.md`](doc/GO_TO_MARKET.md):
what is a trade worth, who wins it, and which trade to sell into next. Prints
to stdout and writes nothing.

```bash
python market.py next                   # THE decision: which market first,
                                        # who is contactable, what to charge
python market.py pitch Jebsen           # one firm's letter numbers
python market.py trade Blitzschutz      # lots/month, value, 0/1-bidder share,
                                        # buyers, regions, how it is bought
python market.py firms Blitzschutz      # who wins it — the prospect list
python market.py rank                   # browse all trades on one sort key
python market.py suggest Blitzschutz    # words this trade may be missing
python market.py trades                 # what the trade list claims
```

`next` decides instead of sorting: a firm is **contactable** when it is
small/micro, has won ≥2 lots in the trade, and its own regions yield ≥12
picks a year (so "we will definitely recommend you tenders" is always true);
the per-firm price is a share of the value the subscription plausibly creates
(uncontested lots in reach × median award × uplift × margin), floored at
€75/month, flipping to a percentage-of-deal pitch where one median award is
large. Trades are ranked by total chargeable revenue, rejected trades are
printed with the failing gate, and the uplift/margin constants are labelled
GUESSES in every output until a customer's answers replace them.

A trade is **defined by words, not by CPV**: of the store lots whose title says
Blitzschutz, only 64% carry `45312310` as their main code, so a code filter
misses a third of the market and admits refurbishments filed under the same
code. The word lists live in [`trades.txt`](trades.txt) — hand-written and
hand-owned, like [`cpv_trade_roots.txt`](cpv_trade_roots.txt); `suggest`
proposes vocabulary but never adds it. Rates only count publication months the
store actually covers, and the 0/1-bidder share is quoted over months mature
enough to have awards (median award lag ~83 days).

Options: `--scope core|mentioned|both` (title names the trade / only the body
does), `--region DE2`, `--since 2026-01`, `--sort`, `--min-lots`, `--top`.

## Requirements

- **Python 3** (tested on 3.13).
- The **exploration scripts** below need only the standard library.
- The **production pipeline** (`cycle.py`, `deliver.py`, `features.py`, `single_bidder.py`)
  additionally needs `pip install -r requirements.txt` — the exact versions the
  cycle runs on, so a rebuild months from now is the same system.
- Internet access; all TED endpoints are public, **no API key**.
- Run every command from the repository root. Downloaded data lands in `data/`
  (git-ignored). `data/raw/` and `data/ledger/` are the two things worth backing
  up — everything else is rebuildable.

**Or skip all of that** — [`Dockerfile`](Dockerfile) and
[`docker-compose.yml`](docker-compose.yml) build an image with the whole stack
in it, and the cycle runs against a mounted state directory:

```bash
docker compose build && docker compose run --rm tm python cycle.py run --last 7d
```

See [`RUNBOOK.md`](doc/RUNBOOK.md) §1b for the mounts and the plain `docker run`
equivalent, and [`STORAGE.md`](doc/STORAGE.md) 6.5 for why the image ships code
only and every writable path is configuration.

```bash
cd C:\Users\user\workspace\TenderMining
```

---

## Exploration scripts (one-off schema surveys, stdlib only)

Findings are written up in [`FINDINGS_oeffentlichevergabe.md`](doc/FINDINGS_oeffentlichevergabe.md)
and [`FINDINGS_ted.md`](doc/FINDINGS_ted.md).

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
