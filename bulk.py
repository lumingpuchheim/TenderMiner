"""TenderMining bulk ingest — the same corpus as download.py, via TED's XML packages.

TED publishes every notice as a monthly (or daily) tar.gz. Fetching those and filtering
locally sidesteps the Search API entirely, which matters for correctness rather than
just speed: the API's ITERATION mode silently stops part-way through a large result set
(36,214 of 56,573 notices for a 12-month German construction scope, 250 of 273 for a
single busy day) and the pagination token restarts rather than continues. A package is
complete by construction — there is no result set to page through.

Same inputs and outputs as download.py: a country, a CPV scope and a date range in,
eForms XML named by publication number into --out-dir (default data/raw/xml/).

    python bulk.py --from 20250601 --to 20260531 --country DEU --cpv 45
    python bulk.py --from 20260401 --to 20260630 --country DEU --cpv 45 \
        --out-dir data/jobs/de-construction-q2/xml

The cost is bandwidth. Packages contain every notice published EU-wide — roughly 890k a
year, of which a German construction scope is about 6% — and there is no server-side
filter, because package filenames carry only the notice id. You transfer everything and
discard locally. Archives are deleted after processing unless --keep-archives.
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import os
import pathlib
import re
import tarfile
import time
import urllib.error
import urllib.request

from download import (XML_DIR, LOG_DIR, append_jsonl, elapsed, log, now_iso)

MONTHLY_URL = "https://ted.europa.eu/packages/monthly/{year}-{month}"
USER_AGENT = "TenderMining/0.1 (research; contact: repo lumingpuchheim/TenderMiner)"
ARCHIVE_DIR = LOG_DIR.parent / "raw" / "packages"
BULK_STATE = LOG_DIR / "bulk_state.json"

# Notice types download.py's query selects: (notice-type=cn-standard OR can-standard).
NOTICE_TYPES = {"cn-standard", "can-standard"}

# Cheap byte tests run before any XML parsing. A monthly package holds ~65k notices and
# parsing all of them costs far more than scanning for a country code first; only ~6%
# survive to the parse step. Both patterns are deliberately loose — they may admit a
# notice that the real check then rejects, but must never exclude one it would accept.
COUNTRY_HINT = re.compile(rb">([A-Z]{3})<")
CPV_RE = re.compile(r"<cbc:ItemClassificationCode[^>]*>(\d+)<")
NOTICE_TYPE_RE = re.compile(r"<cbc:NoticeTypeCode[^>]*>([^<]+)<")
PUBLICATION_ID_RE = re.compile(r"<efbc:NoticePublicationID[^>]*>([^<]+)<")
PUBLICATION_DATE_RE = re.compile(r"<efbc:PublicationDate[^>]*>([^<]+)<")
BUYER_ORG_RE = re.compile(
    r"<cac:ContractingParty>.*?<cac:PartyIdentification>\s*<cbc:ID[^>]*>([^<]+)<", re.S)


def month_range(date_from: str, date_to: str) -> list[tuple[int, int]]:
    start = dt.date(int(date_from[:4]), int(date_from[4:6]), 1)
    end = dt.date(int(date_to[:4]), int(date_to[4:6]), 1)
    out = []
    while start <= end:
        out.append((start.year, start.month))
        start = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return out


def publication_number(member_name: str) -> str | None:
    """Archive member '20250604_106/00361433_2025.xml' -> '361433-2025'.

    download.py names files by publication number without the zero padding that
    NoticePublicationID carries, so the two ingests must agree or every notice would be
    fetched twice under two names.
    """
    base = os.path.basename(member_name)
    if not base.endswith(".xml"):
        return None
    stem = base[:-4]
    if "_" not in stem:
        return None
    left, _, right = stem.partition("_")
    left = left.lstrip("0") or "0"
    return f"{left}-{right}"


def cpv_matches(codes: list[str], cpvs: list[str] | None, cpv_re: re.Pattern | None) -> bool:
    """Mirror download.py's query: classification-cpv=45* matches if ANY code matches."""
    if not cpvs and not cpv_re:
        return True
    for code in codes:
        if cpv_re and cpv_re.search(code):
            return True
        for want in cpvs or ():
            if len(want) >= 8 and code == want:
                return True
            if len(want) < 8 and code.startswith(want):
                return True
    return False


def buyer_country(text: str) -> str | None:
    """Country of the buyer organisation, resolved through the ORG reference.

    cac:ContractingParty holds only a pointer; the address lives in the extension block.
    Matching any DEU anywhere in the document would also catch a French buyer with a
    German winner or appeal body.
    """
    ref = BUYER_ORG_RE.search(text)
    if not ref:
        return None
    org_id = ref.group(1).strip()
    block = re.search(
        r"<efac:Organization>(?:(?!</efac:Organization>).)*?"
        + re.escape(org_id)
        + r"(?:(?!</efac:Organization>).)*?</efac:Organization>", text, re.S)
    if not block:
        return None
    country = re.search(r"<cbc:IdentificationCode[^>]*>([A-Z]{3})<", block.group(0))
    return country.group(1) if country else None


def wanted(text: str, country: str, cpvs, cpv_re, date_from: str, date_to: str):
    """Decide whether one notice belongs in the corpus. Returns (pn, reason_rejected)."""
    kind = NOTICE_TYPE_RE.search(text)
    if not kind or kind.group(1).strip() not in NOTICE_TYPES:
        return None, "notice-type"
    if not cpv_matches(CPV_RE.findall(text), cpvs, cpv_re):
        return None, "cpv"
    if buyer_country(text) != country:
        return None, "country"
    published = PUBLICATION_DATE_RE.search(text)
    if published:
        day = published.group(1)[:10].replace("-", "")
        # A monthly package spans the whole month; the requested range may start or end
        # mid-month, and download.py's query bounded publication-date exactly.
        if day < date_from or day > date_to:
            return None, "date"
    pub = PUBLICATION_ID_RE.search(text)
    if not pub:
        return None, "no-publication-id"
    return f"{pub.group(1).strip().lstrip('0')}", None


def download_package(url: str, dest, label: str) -> bool:
    """Stream a package to disk with progress. Returns False if TED has no such package."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        resp = urllib.request.urlopen(req, timeout=300)
    except urllib.error.HTTPError as e:
        log(f"{label} no package: HTTP {e.code} {e.reason}")
        return False
    total = int(resp.headers.get("Content-Length") or 0)
    tmp = dest.with_suffix(dest.suffix + ".part")
    got = 0
    started = time.monotonic()
    last = 0.0
    with resp, tmp.open("wb") as fh:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            got += len(chunk)
            now = time.monotonic()
            if now - last > 10:
                last = now
                rate = got / max(now - started, 0.001) / 1e6
                pct = f" ({100 * got / total:.0f}%)" if total else ""
                log(f"{label}   {got / 1e6:.0f} MB{pct} at {rate:.1f} MB/s")
    tmp.replace(dest)
    log(f"{label} downloaded {got / 1e6:.0f} MB in {time.monotonic() - started:.0f}s")
    return True


def process_package(path, out_dir, country, cpvs, cpv_re, date_from, date_to, label):
    """Scan one archive, writing matching notices into out_dir."""
    scanned = matched = written = 0
    # 'r|gz' streams: a monthly package holds ~65k members and never needs random access.
    with tarfile.open(path, mode="r|gz") as tf:
        for member in tf:
            if not member.isfile() or not member.name.endswith(".xml"):
                continue
            scanned += 1
            pn_guess = publication_number(member.name)
            if pn_guess and (out_dir / f"{pn_guess}.xml").exists():
                matched += 1          # already have it; do not re-read or re-write
                continue
            fh = tf.extractfile(member)
            if fh is None:
                continue
            raw = fh.read()
            if country.encode() not in raw:
                continue              # cheap reject before paying for a decode
            text = raw.decode("utf-8", "replace")
            pn, _ = wanted(text, country, cpvs, cpv_re, date_from, date_to)
            if not pn:
                continue
            matched += 1
            out = out_dir / f"{pn}.xml"
            if out.exists():
                continue
            out.write_bytes(raw)
            written += 1
            if written % 500 == 0:
                log(f"{label}   scanned {scanned} · matched {matched} · written {written}")
    return scanned, matched, written


def load_state() -> dict:
    if BULK_STATE.exists():
        try:
            return json.loads(BULK_STATE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state: dict) -> None:
    BULK_STATE.parent.mkdir(parents=True, exist_ok=True)
    BULK_STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="date_from", required=True, metavar="YYYYMMDD")
    ap.add_argument("--to", dest="date_to", required=True, metavar="YYYYMMDD")
    ap.add_argument("--country", required=True, metavar="ISO3")
    ap.add_argument("--cpv", default=None, metavar="CODES")
    ap.add_argument("--cpv-re", dest="cpv_re", default=None, metavar="REGEX")
    ap.add_argument("--out-dir", default=str(XML_DIR), metavar="DIR",
                    help=f"where notice XML is written (default: {XML_DIR}). Use a "
                         "separate folder to keep a job's corpus apart from others; the "
                         "done-marker is per destination, so the same scope can be "
                         "written to two folders independently.")
    ap.add_argument("--keep-archives", action="store_true",
                    help="keep the downloaded tar.gz files (default: delete after use)")
    ap.add_argument("--redo", action="store_true",
                    help="re-process months already recorded as done")
    args = ap.parse_args()

    for label, val in (("--from", args.date_from), ("--to", args.date_to)):
        if not (val.isdigit() and len(val) == 8):
            raise SystemExit(f"{label} must be YYYYMMDD, got {val!r}")
    if args.cpv and args.cpv_re:
        raise SystemExit("use either --cpv or --cpv-re, not both")

    cpvs = [c.strip() for c in args.cpv.split(",")] if args.cpv else None
    cpv_re = re.compile(args.cpv_re) if args.cpv_re else None
    months = month_range(args.date_from, args.date_to)
    scope = args.cpv or args.cpv_re or "all CPV"

    out_dir = pathlib.Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    key_prefix = f"{out_dir}|{args.country}|{scope}|{args.date_from}-{args.date_to}"

    log(f"[bulk] {args.country} {scope} {args.date_from}..{args.date_to} "
        f"-> {len(months)} monthly packages")
    append_jsonl(LOG_DIR / "ingest_log.jsonl", {
        "run": now_iso(), "event": "start", "mode": "bulk", "country": args.country,
        "cpv": args.cpv, "cpv_re": args.cpv_re,
        "from": args.date_from, "to": args.date_to, "months": len(months)})

    totals = {"scanned": 0, "matched": 0, "written": 0}
    for i, (year, month) in enumerate(months, 1):
        label = f"  [{i}/{len(months)}] {year}-{month:02d}"
        key = f"{key_prefix}|{year}-{month:02d}"
        if state.get(key, {}).get("done") and not args.redo:
            log(f"{label} already done ({state[key]['written']} written) — skipping")
            continue
        archive = ARCHIVE_DIR / f"{year}-{month:02d}.tar.gz"
        if not archive.exists():
            if not download_package(MONTHLY_URL.format(year=year, month=month),
                                    archive, label):
                continue
        else:
            log(f"{label} using existing archive ({archive.stat().st_size / 1e6:.0f} MB)")
        try:
            scanned, matched, written = process_package(
                archive, out_dir, args.country, cpvs, cpv_re,
                args.date_from, args.date_to, label)
        except (tarfile.TarError, EOFError) as e:
            # A truncated download reads as a broken archive; drop it so the next run
            # re-fetches rather than skipping the month for good.
            log(f"{label} ! corrupt archive ({e}) — deleting, re-run to retry")
            archive.unlink(missing_ok=True)
            continue
        for k, v in zip(totals, (scanned, matched, written)):
            totals[k] += v
        state[key] = {"done": True, "scanned": scanned, "matched": matched,
                      "written": written, "at": now_iso()}
        save_state(state)
        log(f"{label} scanned {scanned} · matched {matched} · written {written} "
            f"[{elapsed()}]")
        if not args.keep_archives:
            archive.unlink(missing_ok=True)

    log(f"\nbulk complete: {totals['written']} new notices written to {out_dir} "
        f"({totals['matched']} in scope, {totals['scanned']} scanned) [{elapsed()}]")
    append_jsonl(LOG_DIR / "ingest_log.jsonl", {
        "run": now_iso(), "event": "bulk_complete", "out_dir": str(out_dir), **totals})


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\ninterrupted — completed months are recorded; re-run to continue")
        raise SystemExit(130)
