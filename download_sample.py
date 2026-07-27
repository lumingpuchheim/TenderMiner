"""Download a sample of German public-procurement notices from oeffentlichevergabe.de.

The Bekanntmachungsservice ("announcement service") has exactly ONE public
OpenData endpoint that exports every notice published in a given month or day:

    GET /api/notice-exports?pubDay=YYYY-MM-DD&format=ocds.zip

Important: the source ONLY delivers notices as a ZIP bundle (a whole day or a
whole month at a time) - there is no single-file / single-notice download in
any format. So we download the ZIP, immediately unpack the JSON notices out of
it, and delete the ZIP. You are left with plain .json files on disk.

Formats offered by the API:
    - eforms.zip : original eForms XML (one file per notice)
    - ocds.zip   : Open Contracting Data Standard, JSON (one file per notice)  <-- we use this
    - csv.zip    : several flat CSV tables joined by noticeIdentifier

We pick OCDS/JSON: self-describing, one JSON object per notice, trivial to parse.
Data spans ~the last three years (from 2022-12-01). Licence: CC0.

Early-2023 days hold only a handful of notices (~30 KB) and download instantly,
which is why the default below is an early date - a fast, small sample.

Usage:
    python download_sample.py                # default sample day below
    python download_sample.py 2023-01-16     # any day >= 2022-12-01 and < today
"""

import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

# Windows consoles default to cp1252 and would mangle German umlauts on print.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = "https://oeffentlichevergabe.de/api/notice-exports"
DEFAULT_DAY = "2023-01-16"  # small early day -> quick sample
OUT_DIR = Path(__file__).parent / "data"


def download(pub_day: str) -> bytes:
    """Fetch one day of notices as an OCDS ZIP, streaming with live progress."""
    url = f"{BASE_URL}?pubDay={pub_day}&format=ocds.zip"
    print(f"Downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "TenderMining/0.1 sample script"})
    buf = io.BytesIO()
    with urllib.request.urlopen(req, timeout=90) as resp:
        while True:
            chunk = resp.read(16384)
            if not chunk:
                break
            buf.write(chunk)
            print(f"\r  received {buf.tell():,} bytes...", end="", flush=True)
    print()
    return buf.getvalue()


def unpack(zip_bytes: bytes, pub_day: str) -> None:
    """Extract every JSON notice out of the ZIP into a folder of plain files."""
    dest = OUT_DIR / f"notices_{pub_day}"
    dest.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        if not names:
            print(f"\nThe archive for {pub_day} is empty - no notices were published that day.")
            print("Try another date, e.g.  python download_sample.py 2023-01-16")
            return
        print(f"\nArchive holds {len(names)} notice file(s). Extracting to {dest}/")
        for name in names:
            (dest / Path(name).name).write_bytes(zf.read(name))

    files = sorted(dest.glob("*.json"))
    print(f"Extracted {len(files)} plain JSON file(s). (ZIP discarded - not kept on disk.)")
    if files:
        inspect(files[0])


def inspect(path: Path) -> None:
    """Print the field structure of one notice so we can see what's inside."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    print(f"\n=== Sample notice: {path.name} ===")
    print("Top-level keys:")
    for k, v in doc.items():
        print(f"  {k}: {type(v).__name__}")

    # An OCDS release package wraps a list of releases; unwrap if present.
    rel = doc["releases"][0] if isinstance(doc.get("releases"), list) and doc["releases"] else doc
    print("\nHighlights from the first release:")
    _show(rel, "ocid")
    _show(rel, "date")
    _show(rel, ("tender", "title"))
    _show(rel, ("tender", "mainProcurementCategory"))
    _show(rel, ("tender", "value", "amount"))
    _show(rel, ("tender", "value", "currency"))
    _show(rel, ("buyer", "name"))
    parties = rel.get("parties") or []
    print(f"  parties: {len(parties)} organisation(s)")
    tags = doc.get("releases", [{}])[0].get("tag") if doc.get("releases") else None
    print(f"  release tag(s): {tags}")


def _show(obj, path) -> None:
    keys = (path,) if isinstance(path, str) else path
    cur = obj
    for k in keys:
        cur = cur.get(k) if isinstance(cur, dict) else None
    print(f"  {'.'.join(keys)}: {cur!r}")


def main() -> None:
    pub_day = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DAY
    unpack(download(pub_day), pub_day)


if __name__ == "__main__":
    main()
