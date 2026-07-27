"""Download ONE day of notices from oeffentlichevergabe.de and report what's in it.

Self-contained: download (OCDS/JSON ZIP) -> extract plain .json files -> survey
which fields are populated, with money / region / bidder aggregations so you can
compare a recent day against the early-2023 sample.

There is no artificial timeout: a recent day is tens of MB and streams slowly,
so the script just waits (printing progress) until the download finishes.

Usage:
    python analyze_day.py                # defaults to the recent day below
    python analyze_day.py 2026-07-15     # any day >= 2022-12-01 and < today

Notes:
  - The API only serves a whole day/month as a ZIP; there is no server-side
    filter and no single-notice download. This script unpacks the ZIP for you.
  - A day must be at least the day-before-yesterday to be fully available.
"""

import glob
import io
import json
import sys
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # avoid cp1252 mojibake on Windows

BASE_URL = "https://oeffentlichevergabe.de/api/notice-exports"
DEFAULT_DAY = "2026-07-15"
OUT_DIR = Path(__file__).parent / "data"

# NUTS-1 code -> German federal state, for readable region output.
NUTS1 = {
    "DE1": "Baden-Württemberg", "DE2": "Bayern", "DE3": "Berlin",
    "DE4": "Brandenburg", "DE5": "Bremen", "DE6": "Hamburg", "DE7": "Hessen",
    "DE8": "Mecklenburg-Vorpommern", "DE9": "Niedersachsen",
    "DEA": "Nordrhein-Westfalen", "DEB": "Rheinland-Pfalz", "DEC": "Saarland",
    "DED": "Sachsen", "DEE": "Sachsen-Anhalt", "DEF": "Schleswig-Holstein",
    "DEG": "Thüringen",
}


def download(pub_day: str) -> bytes:
    url = f"{BASE_URL}?pubDay={pub_day}&format=ocds.zip"
    print(f"Downloading {url}\n(no timeout - a recent day is large, please wait)")
    req = urllib.request.Request(url, headers={"User-Agent": "TenderMining/0.1"})
    buf = io.BytesIO()
    with urllib.request.urlopen(req, timeout=None) as resp:  # timeout=None -> wait forever
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            buf.write(chunk)
            print(f"\r  received {buf.tell():,} bytes...", end="", flush=True)
    print("\nDownload complete.")
    return buf.getvalue()


def extract(zip_bytes: bytes, pub_day: str) -> Path:
    dest = OUT_DIR / f"notices_{pub_day}"
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        if not names:
            print(f"\nArchive for {pub_day} is empty - no notices that day. Pick another date.")
            return dest
        for name in names:
            (dest / Path(name).name).write_bytes(zf.read(name))
    print(f"Extracted {len(names)} JSON notice file(s) to {dest}/")
    return dest


def rget(d, *keys):
    for k in keys:
        d = d.get(k) if isinstance(d, dict) else None
    return d


def survey(dest: Path, pub_day: str) -> None:
    files = sorted(glob.glob(str(dest / "*.json")))
    rels = [json.load(open(f, encoding="utf-8")).get("releases", [{}])[0] for f in files]
    n = len(rels)
    if not n:
        return
    print(f"\n================  {pub_day}: {n} notices  ================")

    # ---- money ----
    vals = [rget(r, "tender", "value", "amount") for r in rels]
    vals = sorted(v for v in vals if isinstance(v, (int, float)))
    print("\nMONEY (tender.value.amount)")
    print(f"  notices with a value: {len(vals)}/{n}")
    if vals:
        print(f"  min {min(vals):,.0f}   median {vals[len(vals)//2]:,.0f}   max {max(vals):,.0f}")

    # ---- awards / bidders ----
    awards_block = sum(1 for r in rels if r.get("awards"))
    tenderers = [rget(r, "tender", "numberOfTenderers") for r in rels]
    tenderers = [t for t in tenderers if isinstance(t, int) and t > 0]
    bids = sum(1 for r in rels if r.get("bids"))
    print("\nCOMPETITION")
    print(f"  notices with awards[] block: {awards_block}/{n}")
    print(f"  notices with bids block: {bids}/{n}")
    print(f"  notices with numberOfTenderers > 0: {len(tenderers)}/{n}"
          + (f"  (min {min(tenderers)} max {max(tenderers)})" if tenderers else ""))

    # ---- region ----
    reg = Counter()
    for r in rels:
        for it in (rget(r, "tender", "items") or []):
            rr = (it.get("deliveryAddress") or {}).get("region")
            if rr:
                reg[rr[:3]] += 1
    print("\nREGION (NUTS-1 of place of performance)")
    for code, c in reg.most_common(8):
        print(f"  {code} {NUTS1.get(code, '?'):22} {c}")

    # ---- category ----
    cpv = Counter()
    for r in rels:
        for it in (rget(r, "tender", "items") or []):
            c = rget(it, "classification", "id")
            if c:
                cpv[str(c)[:2]] += 1
    print("\nCATEGORY (CPV division)")
    for div, c in cpv.most_common(8):
        print(f"  {div}xx  {c}")

    # ---- lifecycle ----
    tags = Counter(tuple(r.get("tag") or []) for r in rels)
    print("\nLIFECYCLE (release tag)")
    for t, c in tags.most_common():
        print(f"  {t or '(none)'}: {c}")


def main() -> None:
    pub_day = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DAY
    survey(extract(download(pub_day), pub_day), pub_day)


if __name__ == "__main__":
    main()
