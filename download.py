"""TenderMining Download job — the network component of the data pipeline.

See pipeline/download.md for the full specification. In short, per finite run:
  1. discover in-scope notice numbers via the TED search API (never persisted)
  2. fetch each notice's eForms XML -> data/raw/xml/
  3. complete procedures: fetch sibling notices by procedure-identifier (no pin*)
  4. attempt GAEB retrieval from links inside newly fetched tender XMLs
  5. retry GAEB for still-live tenders that failed before
  6. write ingest-log line + checkpoint, exit

Usage:
    python download.py --from 20240101 --to 20251231 --country DEU --cpv 45   # backfill
    python download.py --country DEU --cpv 45                                  # continue from checkpoint
    python download.py --country DEU --cpv-re "^452(1|2)" --limit 20           # regex scope, testing
"""

import argparse
import datetime as dt
import hashlib
import html
import http.cookiejar
import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
XML_URL = "https://ted.europa.eu/en/notice/{pn}/xml"
USER_AGENT = "TenderMining/0.1 (research; contact: repo lumingpuchheim/TenderMiner)"
# e-procurement platforms often reject unknown agents outright; a plain browser UA is
# required just to receive the public landing page (no wall is bypassed by this).
BROWSER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

ROOT = Path(__file__).parent
XML_DIR = ROOT / "data" / "raw" / "xml"
GAEB_DIR = ROOT / "data" / "raw" / "gaeb"
LOG_DIR = ROOT / "data" / "logs"
CHECKPOINT = LOG_DIR / "checkpoint.json"
MANIFEST = LOG_DIR / "manifest.jsonl"
GAEB_OUTCOMES = LOG_DIR / "gaeb_outcomes.jsonl"
INGEST_LOG = LOG_DIR / "ingest_log.jsonl"

GAEB_EXT = re.compile(r"\.(x8[1-6]|d8[1-6]|p8[1-6])$", re.I)
RETRY_MAX_AGE_DAYS = 60  # tenders older than this are treated as no longer live
DISCOVERY_FIELDS = ["publication-number", "procedure-identifier", "notice-type",
                    "classification-cpv"]
PLATFORM_REPORT = LOG_DIR / "platform_report.json"

# Download budget per file: GAEB files are small; LV zips are a few MB. Anything
# bigger is drawings we do not want. Platforms can be very slow (~45 KB/s observed).
FILE_CAP_BYTES = 50 * 1024 * 1024
FILE_TIME_BUDGET_S = 420

# ------------------------------------------------------------ platform registry
# host fragment -> platform product name. Notices link to hundreds of portals but
# these run on a handful of software products; handlers are written per PRODUCT.
PLATFORM_HOSTS = [
    ("rib",              ("meinauftrag.rib.de", ".rib.de", "vergabe.bayern.de")),
    ("cosinex/dtvp",     ("dtvp.de", "cosinex", "vergabemarktplatz")),
    ("evergabe-online",  ("evergabe-online.de",)),
    ("subreport",        ("subreport.de",)),
    ("deutsche-evergabe",("deutsche-evergabe.de",)),
    ("vergabe24",        ("vergabe24.de",)),
    ("evergabe.de",      ("evergabe.de",)),
    ("aumass",           ("aumass.de",)),
    ("staatsanzeiger",   ("vergabe.staatsanzeiger",)),
]


def detect_platform(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    for name, fragments in PLATFORM_HOSTS:
        if any(f in host for f in fragments):
            return name
    return f"other:{host}"


# ---------------------------------------------------------------- HTTP helpers

def http_get(url: str, timeout: int = 60, browser: bool = False) -> tuple[int, bytes, str]:
    """GET a URL. Returns (status, body, content_type). Raises on network failure."""
    agent = BROWSER_AGENT if browser else USER_AGENT
    req = urllib.request.Request(url, headers={"User-Agent": agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read(), resp.headers.get("Content-Type", "")


def search(body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        SEARCH_URL, data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": USER_AGENT})
    last_err: Exception | None = None
    for _ in range(2):  # one retry on slow/dropped reads
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                return json.loads(resp.read())
        except (TimeoutError, OSError) as e:
            last_err = e
    raise last_err


def search_all(query: str, limit_cap: int | None = None) -> list[dict]:
    """Iterate the search API until exhausted (or limit_cap notices)."""
    out: list[dict] = []
    token = None
    while True:
        page_size = min(100, (limit_cap - len(out)) if limit_cap else 100)
        body = {"query": query, "fields": DISCOVERY_FIELDS, "limit": page_size,
                "scope": "ALL", "paginationMode": "ITERATION"}
        if token:
            body["iterationNextToken"] = token
        page = search(body)
        batch = page.get("notices", [])
        out.extend(batch)
        token = page.get("iterationNextToken")
        if not batch or not token or (limit_cap and len(out) >= limit_cap):
            return out[:limit_cap] if limit_cap else out


# ---------------------------------------------------------------- scope/query

def cpv_server_clause(cpvs: list[str]) -> str:
    terms = []
    for c in cpvs:
        c = c.strip()
        if c:
            terms.append(f"classification-cpv={c}" if len(c) >= 8
                         else f"classification-cpv={c}*")
    if not terms:
        return ""
    return terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")"


def regex_prefix(pattern: str) -> str | None:
    """Longest literal digit prefix of a regex, for the broadest safe server-side cut.
    '^452(1|2).*' -> '452'. Returns None if the regex has no usable literal prefix."""
    m = re.match(r"\^?(\d+)", pattern)
    return m.group(1) if m else None


def build_query(date_from: str, date_to: str | None, country: str,
                cpvs: list[str] | None, cpv_re: str | None) -> str:
    parts = ["(notice-type=cn-standard OR notice-type=can-standard)",
             f"publication-date>={date_from}",
             f"buyer-country={country}"]
    if date_to:
        parts.append(f"publication-date<={date_to}")
    if cpvs:
        clause = cpv_server_clause(cpvs)
        if clause:
            parts.append(clause)
    elif cpv_re:
        prefix = regex_prefix(cpv_re)
        if prefix:
            parts.append(f"classification-cpv={prefix}*")
    return " AND ".join(parts) + " SORT BY publication-date"  # ascending; 'ASC' keyword is invalid


def first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


# ---------------------------------------------------------------- XML fetching

def fetch_xml(pn: str) -> Path | None:
    """Fetch one notice XML unless already archived. Returns path or None on failure."""
    path = XML_DIR / f"{pn}.xml"
    if path.exists():
        return path
    try:
        status, body, _ = http_get(XML_URL.format(pn=pn))
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        print(f"  ! xml fetch failed {pn}: {e}")
        return None
    if status != 200 or not body.lstrip().startswith(b"<?xml"):
        print(f"  ! xml fetch {pn}: HTTP {status}, not XML")
        return None
    path.write_bytes(body)
    return path


def count_lots(xml_text: str) -> int:
    return len(re.findall(r"<cac:ProcurementProjectLot\b", xml_text))


def is_tender(notice_type: str | None) -> bool:
    return bool(notice_type) and notice_type.startswith("cn")


def is_planning(notice_type: str | None) -> bool:
    return bool(notice_type) and notice_type.startswith("pin")


# ---------------------------------------------------------------- GAEB fetching

def make_browser_opener() -> urllib.request.OpenerDirector:
    """Fresh opener with its own cookie jar (platforms bind download tokens to the
    session that viewed the page) and a browser User-Agent."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.addheaders = [("User-Agent", BROWSER_AGENT)]
    return opener


def fetch_capped(opener: urllib.request.OpenerDirector, url: str,
                 cap: int = FILE_CAP_BYTES, budget_s: int = FILE_TIME_BUDGET_S) -> bytes | None:
    """Chunked download with a size cap and a wall-clock budget (platforms stream
    slowly; drawings are large and unwanted). Returns None on any failure/overrun."""
    start = dt.datetime.now()
    try:
        with opener.open(url, timeout=90) as resp:
            if resp.status != 200:
                return None
            buf = io.BytesIO()
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    return buf.getvalue()
                buf.write(chunk)
                if buf.tell() > cap:
                    return None
                if (dt.datetime.now() - start).total_seconds() > budget_s:
                    return None
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None


LV_NAME = re.compile(r"(^|[^a-z])(lv|leistungsverzeichnis|gaeb)([^a-z]|$)", re.I)
SKIP_NAME = re.compile(r"plananlage|zeichnung|grundriss|schnitt|ansicht", re.I)


def handle_rib(doc_url: str, pid: str, pn: str, known_hashes: set[str]) -> str:
    """RIB (meinauftrag.rib.de / my.vergabe.bayern.de family).

    The public publication page embeds the complete document tree with direct
    tokenized links (…/remote/download.php?k=<hash>). Selective download only:
    GAEB-named files always; ZIPs only when the name suggests a Leistungsverzeichnis
    and not drawings. Requires the page's session cookie; no account involved."""
    opener = make_browser_opener()
    try:
        with opener.open(doc_url, timeout=60) as resp:
            page = resp.read().decode("utf-8", "replace")
            if resp.status != 200:
                return "dead_link"
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return "dead_link"

    page = page.replace("\\/", "/")
    pairs = re.findall(
        r'(https?://[^"\'<>\s]+/remote/download\.php\?k=[a-f0-9]+)[^>]*>([^<]{2,120})<', page)
    if not pairs:
        return "registration_wall"  # page reachable but no public document tree

    best: dict[str, str] = {}
    for u, n in pairs:
        n = html.unescape(n).strip()
        if u not in best or len(n) > len(best[u]):
            best[u] = n

    stored = 0
    for url, name in best.items():
        if GAEB_EXT.search(name):
            blob = fetch_capped(opener, url + "&down")
            if blob:
                stored += store_gaeb([(Path(name).name, blob)], pid, pn, url, known_hashes)
        elif name.lower().endswith(".zip") and LV_NAME.search(name) and not SKIP_NAME.search(name):
            blob = fetch_capped(opener, url + "&down")
            if blob:
                files = extract_gaeb_from_zip(blob)
                if files:
                    stored += store_gaeb(files, pid, pn, url, known_hashes)
    return "ok" if stored else "no_gaeb"


# handler registry: platform product -> handler. Platforms without a handler fall
# back to the generic strategy (visible file links). Extend guided by the report.
HANDLERS = {"rib": handle_rib}


def document_urls(xml_text: str) -> list[str]:
    """Candidate procurement-document URLs from a notice XML: URIs inside document
    references, excluding TED itself."""
    urls: list[str] = []
    for block in re.findall(
            r"<cac:CallForTendersDocumentReference\b.*?</cac:CallForTendersDocumentReference>",
            xml_text, re.S):
        urls += re.findall(r"<cbc:URI>\s*([^<\s]+)\s*</cbc:URI>", block)
    # fallback: any URI element outside TED (some schemas place the link elsewhere)
    if not urls:
        urls = re.findall(r"<cbc:URI>\s*(https?://[^<\s]+)\s*</cbc:URI>", xml_text)
    seen, out = set(), []
    for u in urls:
        u = u.strip()
        if u.startswith("http") and "ted.europa.eu" not in u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def extract_gaeb_from_zip(blob: bytes) -> list[tuple[str, bytes]]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        return []
    files = []
    for name in zf.namelist():
        if GAEB_EXT.search(name):
            files.append((Path(name).name, zf.read(name)))
    return files


def html_gaeb_links(html: str, base_url: str) -> list[str]:
    """Direct .zip/.x8x/… hrefs on a platform landing page (one level deep)."""
    links = re.findall(r'href=["\']([^"\']+)["\']', html, re.I)
    out = []
    for l in links:
        if GAEB_EXT.search(l) or l.lower().endswith(".zip"):
            out.append(urllib.parse.urljoin(base_url, l))
    return out[:10]


def store_gaeb(files: list[tuple[str, bytes]], pid: str, pn: str, url: str,
               known_hashes: set[str]) -> int:
    stored = 0
    dest = GAEB_DIR / (pid or pn)
    for name, blob in files:
        sha = hashlib.sha256(blob).hexdigest()
        if sha in known_hashes:
            continue
        known_hashes.add(sha)
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / name
        if path.exists():  # same name, different content: disambiguate
            path = dest / f"{sha[:8]}_{name}"
        path.write_bytes(blob)
        append_jsonl(MANIFEST, {
            "procedure-identifier": pid, "publication-number": pn, "lot": None,
            "source_url": url, "stored_path": str(path.relative_to(ROOT)).replace("\\", "/"),
            "sha256": sha, "fetched_at": now_iso(),
            "gaeb_kind": path.suffix.lstrip(".").lower()})
        stored += 1
    return stored


def attempt_gaeb(xml_text: str, pid: str, pn: str, known_hashes: set[str]) -> tuple[str, str | None]:
    """Try to retrieve GAEB files for one tender notice.
    Returns (outcome, platform). Dispatches to a platform handler when one exists,
    else falls back to the generic visible-file-link strategy."""
    urls = document_urls(xml_text)
    if not urls:
        return "no_url", None
    platform = detect_platform(urls[0])
    handler = HANDLERS.get(platform)
    if handler:
        return handler(urls[0], pid, pn, known_hashes), platform
    return attempt_generic(urls, pid, pn, known_hashes), platform


def attempt_generic(urls: list[str], pid: str, pn: str, known_hashes: set[str]) -> str:
    """Generic strategy for unhandled platforms: direct file / ZIP links only."""
    outcome = "dead_link"  # until any URL responds
    for url in urls:
        try:
            status, body, ctype = http_get(url, timeout=45, browser=True)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            continue
        if status != 200:
            continue
        # direct GAEB file?
        if GAEB_EXT.search(url):
            if store_gaeb([(Path(url).name.split("?")[0], body)], pid, pn, url, known_hashes):
                return "ok"
        # ZIP package?
        if body[:2] == b"PK":
            files = extract_gaeb_from_zip(body)
            if files and store_gaeb(files, pid, pn, url, known_hashes):
                return "ok"
            outcome = "no_gaeb"
            continue
        # HTML landing page: look one level deep for direct file links
        if "html" in ctype.lower() or body.lstrip()[:1] == b"<":
            deep = html_gaeb_links(body.decode("utf-8", "replace"), url)
            got = False
            for d in deep:
                try:
                    s2, b2, _ = http_get(d, timeout=45, browser=True)
                except (urllib.error.URLError, OSError, TimeoutError, ValueError):
                    continue
                if s2 != 200:
                    continue
                if b2[:2] == b"PK":
                    files = extract_gaeb_from_zip(b2)
                    if files and store_gaeb(files, pid, pn, d, known_hashes):
                        return "ok"
                elif GAEB_EXT.search(d) and store_gaeb(
                        [(Path(d).name.split("?")[0], b2)], pid, pn, d, known_hashes):
                    return "ok"
                got = True
            outcome = "no_gaeb" if got else "registration_wall"
        else:
            outcome = "no_gaeb"  # reachable, but not GAEB/ZIP/HTML we can use
    return outcome


# ---------------------------------------------------------------- bookkeeping

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def load_manifest_hashes() -> set[str]:
    if not MANIFEST.exists():
        return set()
    return {json.loads(l)["sha256"] for l in MANIFEST.read_text(encoding="utf-8").splitlines() if l}


def load_last_outcomes() -> dict[str, dict]:
    """publication-number -> latest outcome record."""
    out: dict[str, dict] = {}
    if GAEB_OUTCOMES.exists():
        for l in GAEB_OUTCOMES.read_text(encoding="utf-8").splitlines():
            if l:
                rec = json.loads(l)
                out[rec["publication-number"]] = rec
    return out


# ---------------------------------------------------------------- platform report

def write_platform_report() -> None:
    """Aggregate the full outcome history into data/logs/platform_report.json:
    per platform — handled?, cumulative outcomes, ok-rate, last success, last attempt.
    This is the file to look at when yield drops: 'RIB: 0% since <date>' = RIB broke."""
    agg: dict[str, dict] = {}
    if GAEB_OUTCOMES.exists():
        for line in GAEB_OUTCOMES.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            rec = json.loads(line)
            p = rec.get("platform") or "unknown"
            a = agg.setdefault(p, {"outcomes": {}, "last_ok": None, "last_attempt": None})
            a["outcomes"][rec["outcome"]] = a["outcomes"].get(rec["outcome"], 0) + 1
            a["last_attempt"] = max(a["last_attempt"] or "", rec["at"])
            if rec["outcome"] == "ok":
                a["last_ok"] = max(a["last_ok"] or "", rec["at"])
    for p, a in agg.items():
        total = sum(a["outcomes"].values())
        a["handled"] = p in HANDLERS
        a["attempts"] = total
        a["ok_rate"] = round(a["outcomes"].get("ok", 0) / total, 3) if total else 0.0
    PLATFORM_REPORT.parent.mkdir(parents=True, exist_ok=True)
    PLATFORM_REPORT.write_text(json.dumps(
        {"generated": now_iso(), "platforms": agg}, indent=2, ensure_ascii=False))


def print_platform_report() -> None:
    if not PLATFORM_REPORT.exists():
        return
    data = json.loads(PLATFORM_REPORT.read_text(encoding="utf-8"))
    rows = sorted(data["platforms"].items(), key=lambda kv: -kv[1]["attempts"])
    print("\nPLATFORM OVERVIEW (cumulative, see data/logs/platform_report.json)")
    print(f"  {'platform':22} {'handled':8} {'attempts':>8} {'ok-rate':>8}  last_ok")
    for name, a in rows:
        print(f"  {name[:22]:22} {'yes' if a['handled'] else 'NO':8} "
              f"{a['attempts']:>8} {a['ok_rate']:>8.0%}  {a['last_ok'] or '-'}")


# ---------------------------------------------------------------- main run

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TenderMining download job (finite run).")
    p.add_argument("--from", dest="date_from", metavar="YYYYMMDD", default=None)
    p.add_argument("--to", dest="date_to", metavar="YYYYMMDD", default=None)
    p.add_argument("--country", required=True, metavar="ISO3")
    p.add_argument("--cpv", default=None, metavar="CODES")
    p.add_argument("--cpv-re", dest="cpv_re", default=None, metavar="REGEX")
    p.add_argument("--limit", type=int, default=None, metavar="N")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    for label, val in (("--from", args.date_from), ("--to", args.date_to)):
        if val and not (val.isdigit() and len(val) == 8):
            raise SystemExit(f"{label} must be YYYYMMDD, got {val!r}")
    if args.cpv and args.cpv_re:
        raise SystemExit("use either --cpv or --cpv-re, not both")

    # mode: backfill (dates given) vs continuation (checkpoint)
    if args.date_from:
        date_from, date_to = args.date_from, args.date_to
        mode = "backfill"
    else:
        cp = json.loads(CHECKPOINT.read_text()) if CHECKPOINT.exists() else {}
        date_from = cp.get("last_covered", (dt.date.today() - dt.timedelta(days=7)).strftime("%Y%m%d"))
        date_to = None
        mode = "continuation"

    cpvs = args.cpv.split(",") if args.cpv else None
    query = build_query(date_from, date_to, args.country, cpvs, args.cpv_re)
    print(f"[{mode}] {query}")

    # 1. discover
    hits = search_all(query, args.limit)
    if args.cpv_re:
        rx = re.compile(args.cpv_re)
        hits = [h for h in hits
                if any(rx.search(str(c)) for c in (h.get("classification-cpv") or []))]
    print(f"discovered {len(hits)} notices in scope")

    # 2. fetch XML + 3. complete procedures
    XML_DIR.mkdir(parents=True, exist_ok=True)
    fetched: list[tuple[str, str, str]] = []  # (pn, pid, notice-type)
    done_pids: set[str] = set()
    todo = [(str(h.get("publication-number")), first(h.get("procedure-identifier")) or "",
             str(h.get("notice-type") or "")) for h in hits]
    n_new = 0
    while todo:
        pn, pid, ntype = todo.pop(0)
        if is_planning(ntype):
            continue
        was_new = not (XML_DIR / f"{pn}.xml").exists()
        if fetch_xml(pn) is None:
            continue
        if was_new:
            n_new += 1
            fetched.append((pn, pid, ntype))
            print(f"  xml {pn} ({ntype})")
        if pid and pid not in done_pids:
            done_pids.add(pid)
            try:
                sibs = search_all(f"procedure-identifier={pid}")
            except Exception as e:
                print(f"  ! completion lookup failed {pid}: {e}")
                continue
            for s in sibs:
                spn = str(s.get("publication-number"))
                stype = str(s.get("notice-type") or "")
                if spn != pn and not is_planning(stype) and not (XML_DIR / f"{spn}.xml").exists():
                    todo.append((spn, pid, stype))
        if args.limit and n_new >= args.limit:
            break

    # lot statistics over newly fetched XMLs
    hist: Counter = Counter()
    for pn, _, _ in fetched:
        text = (XML_DIR / f"{pn}.xml").read_text(encoding="utf-8", errors="replace")
        n = count_lots(text)
        hist[str(n) if n < 4 else "4+"] += 1
    single = hist.get("1", 0)
    multi = sum(v for k, v in hist.items() if k != "1")

    # 4. GAEB for newly fetched tender notices
    known_hashes = load_manifest_hashes()
    gaeb_counts: Counter = Counter()
    by_platform: dict[str, Counter] = {}
    for pn, pid, ntype in fetched:
        if not is_tender(ntype):
            continue
        text = (XML_DIR / f"{pn}.xml").read_text(encoding="utf-8", errors="replace")
        outcome, platform = attempt_gaeb(text, pid, pn, known_hashes)
        gaeb_counts[outcome] += 1
        by_platform.setdefault(platform or "none", Counter())[outcome] += 1
        append_jsonl(GAEB_OUTCOMES, {"publication-number": pn,
                                     "procedure-identifier": pid,
                                     "platform": platform,
                                     "outcome": outcome, "at": now_iso()})
        print(f"  gaeb {pn} [{platform}]: {outcome}")

    # 5. retry pass: previously failed, still-live tenders (not fetched this run)
    just_done = {pn for pn, _, _ in fetched}
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=RETRY_MAX_AGE_DAYS)
    for pn, rec in load_last_outcomes().items():
        if pn in just_done or rec["outcome"] == "ok":
            continue
        at = dt.datetime.strptime(rec["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        if at < cutoff:
            continue
        path = XML_DIR / f"{pn}.xml"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        outcome, platform = attempt_gaeb(text, rec.get("procedure-identifier") or "", pn, known_hashes)
        gaeb_counts[f"retry_{outcome}"] += 1
        by_platform.setdefault(platform or "none", Counter())[f"retry_{outcome}"] += 1
        append_jsonl(GAEB_OUTCOMES, {"publication-number": pn,
                                     "procedure-identifier": rec.get("procedure-identifier"),
                                     "platform": platform,
                                     "outcome": outcome, "at": now_iso()})
        if outcome == "ok":
            print(f"  gaeb retry {pn} [{platform}]: ok")

    # 6. checkpoint + ingest log + platform report
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if mode == "continuation" or not args.date_to:
        CHECKPOINT.write_text(json.dumps(
            {"last_covered": dt.date.today().strftime("%Y%m%d"), "updated": now_iso()}))
    append_jsonl(INGEST_LOG, {
        "run": now_iso(), "mode": mode, "query": query,
        "notices_fetched": len(fetched), "single_lot": single, "multi_lot": multi,
        "lots_histogram": dict(hist), "gaeb": dict(gaeb_counts),
        "gaeb_by_platform": {p: dict(c) for p, c in by_platform.items()}})
    write_platform_report()
    print(f"\nrun complete: {len(fetched)} new notices "
          f"({single} single-lot, {multi} multi-lot); gaeb: {dict(gaeb_counts)}")
    print_platform_report()


if __name__ == "__main__":
    main()
