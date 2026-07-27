"""Download a sample of EU procurement notices from TED (Tenders Electronic Daily).

TED is the EU-wide publication service for ABOVE-threshold procurement (the
"Supplement to the Official Journal"). Unlike oeffentlichevergabe.de, TED offers
a real search API with SERVER-SIDE FILTERING - you send an expert-search query
and pick exactly which fields to return, so there is no need to download whole
days and filter locally.

Endpoint (read-only, NO API key needed for searching published notices):
    POST https://api.ted.europa.eu/v3/notices/search
    body: {"query": <expert query>, "fields": [...], "limit": N, "page": P,
           "scope": "ALL", "paginationMode": "ITERATION", "iterationNextToken": ...}

Response JSON: {"notices": [ {<requested fields>, "links": {...}}, ... ],
                "totalNoticeCount": N, "iterationNextToken": "..."}

An API key IS required only to SUBMIT / modify notices, not to search.

This script pulls contract-AWARD notices (which carry money + winners + bid
counts) for a chosen date range and saves them as one JSON object per line (.jsonl).

Usage (all arguments are optional):
    python ted_download_sample.py                              # newest 50 awards since 2026-07-01
    python ted_download_sample.py --max 2000                   # newest 2000 in the default window
    python ted_download_sample.py --from 20250101 --to 20251231 --max 2000
    python ted_download_sample.py --from 20250101 --country DEU # German buyers only (one language)
    python ted_download_sample.py --type cn-standard           # calls for bids instead of awards

Arguments:
    --from YYYYMMDD   start publication date (inclusive). Default 20260701.
    --to   YYYYMMDD   end publication date (inclusive). Default: today (no upper bound).
    --max  N          maximum notices to download, newest first. Default 50.
    --type CODE       notice-type: can-standard (award, has money) | cn-standard (call) | pin ...
    --country ISO3    restrict to a buyer country, e.g. DEU, FRA (handy to fix one language).
    --out  PATH       output .jsonl path. Default data/ted_awards_sample.jsonl.
"""

import argparse
import json
import urllib.request
from pathlib import Path

SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
OUT_DIR = Path(__file__).parent / "data"


def build_query(date_from: str, date_to: str | None, notice_type: str, country: str | None) -> str:
    """Assemble a TED expert-search query. Operators: =, >=, <=, AND, SORT BY.
    This server-side filter is the advantage TED has over the German whole-day feed."""
    parts = [f"notice-type={notice_type}", f"publication-date>={date_from}"]
    if date_to:
        parts.append(f"publication-date<={date_to}")
    if country:
        parts.append(f"buyer-country={country}")
    return " AND ".join(parts) + " SORT BY publication-date DESC"

# eForms "business term" fields to return (there are ~1830 to choose from).
FIELDS = [
    "publication-number",              # TED notice id, e.g. 516182-2026
    "notice-type",                     # cn-standard (call) / can-standard (award) / pin ...
    "publication-date",
    # --- TEXT (for ML on description -> value etc.) ---
    "notice-title",                    # short title
    "title-proc",                      # procedure title
    "description-proc",                # MAIN free-text description of what is procured
    "description-lot",                 # per-lot descriptions (fallback / more detail)
    # --- LABELS / features ---
    "organisation-name-buyer",         # the public buyer (multilingual: {"eng": [...]})
    "buyer-country",
    "classification-cpv",              # what is bought (CPV codes)
    "total-value", "total-value-cur",  # MONEY: awarded value + currency (the ML target)
    "winner-country",                  # where the winner is based
    "winner-size",                     # large / sme  <-- SME participation signal
    "received-submissions-type-val",   # NUMBER OF TENDERS received (bid count)
    "links",                           # per-language XML + PDF of the full notice
]

# Note: text fields are multilingual and currencies vary (EUR, PLN, ...). For ML you
# will likely normalise language (e.g. filter buyer-country, or translate) and convert
# all values to one currency.


def post(body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        SEARCH_URL, data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "TenderMining/0.1"},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read())


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Download a sample of TED procurement notices.")
    # 'from'/'to' are Python keywords, so store them under different attribute names.
    p.add_argument("--from", dest="date_from", default="20260701",
                   metavar="YYYYMMDD", help="start publication date, inclusive (default 20260701)")
    p.add_argument("--to", dest="date_to", default=None,
                   metavar="YYYYMMDD", help="end publication date, inclusive (default: today)")
    p.add_argument("--max", dest="max_notices", type=int, default=50,
                   metavar="N", help="max notices to download, newest first (default 50)")
    p.add_argument("--type", dest="notice_type", default="can-standard",
                   help="notice-type: can-standard (award) | cn-standard (call) | pin ...")
    p.add_argument("--country", default=None, metavar="ISO3",
                   help="restrict to a buyer country, e.g. DEU, FRA")
    p.add_argument("--out", default=None, metavar="PATH",
                   help="output .jsonl path (default data/ted_awards_sample.jsonl)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    for label, val in (("--from", args.date_from), ("--to", args.date_to)):
        if val is not None and not (val.isdigit() and len(val) == 8):
            raise SystemExit(f"{label} must be YYYYMMDD (8 digits), got {val!r}")

    query = build_query(args.date_from, args.date_to, args.notice_type, args.country)
    target = args.max_notices
    out = Path(args.out) if args.out else OUT_DIR / "ted_awards_sample.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"Query: {query}")
    print(f"Fetching up to {target} notices -> {out}\n")

    notices, token = [], None
    while len(notices) < target:
        body = {
            "query": query, "fields": FIELDS,
            "limit": min(100, target - len(notices)),
            "scope": "ALL", "paginationMode": "ITERATION",
        }
        if token:
            body["iterationNextToken"] = token
        page = post(body)
        batch = page.get("notices", [])
        notices.extend(batch)
        token = page.get("iterationNextToken")
        print(f"  fetched {len(notices)}/{target} "
              f"(of {page.get('totalNoticeCount', '?')} matching notices total)")
        if not batch or not token:
            break

    with out.open("w", encoding="utf-8") as f:
        for n in notices:
            f.write(json.dumps(n, ensure_ascii=False) + "\n")
    print(f"\nSaved {len(notices)} notices to {out}")
    print("Inspect fields with:  python ted_explore_fields.py")


if __name__ == "__main__":
    main()
