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

This script pulls recent contract-AWARD notices (which carry money + winners +
bid counts) and saves them as one JSON object per line (.jsonl).

Usage:
    python ted_download_sample.py                 # default: recent award notices
    python ted_download_sample.py 200             # fetch up to 200 notices
"""

import json
import sys
import urllib.request
from pathlib import Path

SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
OUT_DIR = Path(__file__).parent / "data"

# Expert-search query: contract-award notices published since July 2026,
# newest first. The query language supports =, >=, IN, ~ (contains),
# AND/OR/NOT and "SORT BY". This is the server-side filter the German feed lacks.
QUERY = "notice-type=can-standard AND publication-date>=20260701 SORT BY publication-date DESC"

# eForms "business term" fields to return (there are ~1830 to choose from).
FIELDS = [
    "publication-number",              # TED notice id, e.g. 516182-2026
    "notice-type",                     # cn-standard (call) / can-standard (award) / pin ...
    "publication-date",
    "organisation-name-buyer",         # the public buyer (multilingual: {"eng": [...]})
    "buyer-country",
    "classification-cpv",              # what is bought (CPV codes)
    "total-value", "total-value-cur",  # MONEY: awarded value + currency
    "winner-country",                  # where the winner is based
    "winner-size",                     # large / sme  <-- SME participation signal
    "received-submissions-type-val",   # NUMBER OF TENDERS received (bid count)
    "links",                           # per-language XML + PDF of the full notice
]


def post(body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        SEARCH_URL, data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "TenderMining/0.1"},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read())


def main() -> None:
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / "ted_awards_sample.jsonl"

    notices, token = [], None
    while len(notices) < target:
        body = {
            "query": QUERY, "fields": FIELDS,
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
