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
    python ted_download_sample.py --cpv 33,72                   # only medical (33) or IT (72) tenders
    python ted_download_sample.py --type cn-standard           # calls for bids instead of awards

Arguments:
    --from YYYYMMDD   start publication date (inclusive). Default 20260701.
    --to   YYYYMMDD   end publication date (inclusive). Default: today (no upper bound).
    --max  N          maximum notices to download, newest first. Default 50.
    --type CODE       notice-type: can-standard (award, has money) | cn-standard (call) | pin ...
    --country ISO3    restrict to a buyer country, e.g. DEU, FRA (handy to fix one language).
    --cpv  CODES      CPV filter, comma-separated. Short code = prefix (33=medical, 72=IT);
                      8-digit code = exact. Example: --cpv 33,72 or --cpv 33696000.
    --out  PATH       output .jsonl path. Default data/ted_awards_sample.jsonl.
    --lang LANG       force a text language (3-letter, e.g. deu). Default Rule A below.
    --raw             keep full untrimmed notices (skip the shrink step).

Saved notices are TRIMMED by default to keep the file small and single-language:
    - drop the `links` block (all per-language PDF/HTML URLs are rebuildable from
      publication-number, so no information is lost);
    - collapse every multilingual field to ONE language -- Rule A: the buyer's own
      language (from buyer-country), falling back to English;
    - de-duplicate code lists such as classification-cpv.
Pass --raw to keep everything. Trimming is lossless in practice: any dropped detail is
reproducible from TED via the publication-number.
"""

import argparse
import json
import urllib.request
from pathlib import Path

SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
OUT_DIR = Path(__file__).parent / "data"

# TED 3-letter language codes. Used to detect language-keyed fields like
# {"deu": "..."} so we can collapse them to a single language (see trim_notice).
LANGS = {
    "bul", "ces", "dan", "deu", "ell", "eng", "est", "fin", "fra", "gle", "hrv",
    "hun", "ita", "lav", "lit", "mlt", "nld", "pol", "por", "ron", "slk", "slv",
    "spa", "swe", "nor", "isl",
}

# Buyer country (ISO3) -> its main TED language. Rule A: keep the buyer's own
# language, then fall back to English. Unmapped countries just fall back to English
# (and then to whatever the field actually contains).
COUNTRY_LANG = {
    "DEU": "deu", "AUT": "deu", "CHE": "deu", "FRA": "fra", "LUX": "fra", "BEL": "fra",
    "ESP": "spa", "ITA": "ita", "NLD": "nld", "POL": "pol", "PRT": "por", "ROU": "ron",
    "CZE": "ces", "SVK": "slk", "SVN": "slv", "HRV": "hrv", "HUN": "hun", "GRC": "ell",
    "CYP": "ell", "BGR": "bul", "DNK": "dan", "SWE": "swe", "FIN": "fin", "EST": "est",
    "LVA": "lav", "LTU": "lit", "MLT": "mlt", "IRL": "eng", "NOR": "nor", "ISL": "isl",
}

# Array fields whose duplicate entries are noise (safe to de-duplicate). We do NOT
# dedup value-bearing lists like received-submissions-type-val where order/repeats matter.
DEDUP_FIELDS = {"classification-cpv", "contract-nature"}


def preferred_langs(notice: dict, override: str | None) -> list[str]:
    """Rule A: [buyer's own language, English]. --lang overrides the first choice."""
    if override:
        first = override
    else:
        country = notice.get("buyer-country")
        country = country[0] if isinstance(country, list) and country else country
        first = COUNTRY_LANG.get(country)
    order = [first, "eng"]
    return [x for i, x in enumerate(order) if x and x not in order[:i]]


def _unwrap(v):
    """Single-element lists -> the element, so text is a plain string not ["str"]."""
    return v[0] if isinstance(v, list) and len(v) == 1 else v


def _collapse_lang(value: dict, pref: list[str]):
    """Pick one language from a {lang: value} map: preferred langs first, else any."""
    for lang in pref:
        if lang in value:
            return _unwrap(value[lang])
    return _unwrap(next(iter(value.values())))  # fallback: first available language


def _is_lang_map(v) -> bool:
    return isinstance(v, dict) and bool(v) and all(k in LANGS for k in v)


def trim_notice(notice: dict, lang_override: str | None) -> dict:
    """Shrink one notice for local storage:
      - drop `links` (all per-language PDF/HTML URLs are rebuildable from publication-number)
      - collapse each multilingual field to one language (Rule A)
      - de-duplicate code-list fields (e.g. classification-cpv)
    Everything is reproducible from TED, so this is lossless for practical purposes."""
    pref = preferred_langs(notice, lang_override)
    out = {}
    for k, v in notice.items():
        if k == "links":
            continue
        if _is_lang_map(v):
            out[k] = _collapse_lang(v, pref)
        elif k in DEDUP_FIELDS and isinstance(v, list):
            out[k] = list(dict.fromkeys(v))  # order-preserving unique
        else:
            out[k] = v
    return out


def cpv_clause(cpvs: list[str]) -> str:
    """Build a CPV filter. A code shorter than 8 digits becomes a prefix match
    (e.g. '33' -> classification-cpv=33*, matching the whole CPV division); a full
    8-digit code is matched exactly. Multiple codes are OR-ed together."""
    terms = []
    for c in cpvs:
        c = c.strip()
        if not c:
            continue
        terms.append(f"classification-cpv={c}" if len(c) >= 8 else f"classification-cpv={c}*")
    if not terms:
        return ""
    return terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")"


def build_query(date_from: str, date_to: str | None, notice_type: str,
                country: str | None, cpvs: list[str] | None) -> str:
    """Assemble a TED expert-search query. Operators: =, >=, <=, OR, AND, SORT BY.
    This server-side filter is the advantage TED has over the German whole-day feed."""
    parts = [f"notice-type={notice_type}", f"publication-date>={date_from}"]
    if date_to:
        parts.append(f"publication-date<={date_to}")
    if country:
        parts.append(f"buyer-country={country}")
    if cpvs:
        clause = cpv_clause(cpvs)
        if clause:
            parts.append(clause)
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
]
# Note: TED always returns a big `links` block (per-language PDF/HTML URLs) regardless
# of FIELDS; trim_notice() strips it. Text fields are multilingual and currencies vary
# (EUR, PLN, ...); trim_notice() collapses text to one language, you still convert value.


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
    p.add_argument("--cpv", default=None, metavar="CODES",
                   help="CPV filter, comma-separated. A short code is a prefix "
                        "(e.g. 33=medical, 72=IT); an 8-digit code is exact. "
                        "Example: --cpv 33,72 or --cpv 33696000")
    p.add_argument("--out", default=None, metavar="PATH",
                   help="output .jsonl path (default data/ted_awards_sample.jsonl)")
    p.add_argument("--lang", default=None, metavar="LANG",
                   help="force a text language (3-letter, e.g. deu). Default: the buyer's "
                        "own language with English fallback (Rule A).")
    p.add_argument("--raw", action="store_true",
                   help="keep the full untrimmed notices (do not drop links / collapse "
                        "languages / dedup CPV).")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    for label, val in (("--from", args.date_from), ("--to", args.date_to)):
        if val is not None and not (val.isdigit() and len(val) == 8):
            raise SystemExit(f"{label} must be YYYYMMDD (8 digits), got {val!r}")

    cpvs = args.cpv.split(",") if args.cpv else None
    query = build_query(args.date_from, args.date_to, args.notice_type, args.country, cpvs)
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

    if not args.raw:
        notices = [trim_notice(n, args.lang) for n in notices]

    with out.open("w", encoding="utf-8") as f:
        for n in notices:
            f.write(json.dumps(n, ensure_ascii=False) + "\n")
    print(f"\nSaved {len(notices)} notices to {out}"
          + ("  (raw, untrimmed)" if args.raw else "  (trimmed: no links, one language, deduped CPV)"))
    print("Inspect fields with:  python ted_explore_fields.py")


if __name__ == "__main__":
    main()
