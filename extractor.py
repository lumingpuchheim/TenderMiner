"""TenderMining Extractor — the offline component of the data pipeline.

See pipeline/extractor.md. Reads only the raw archive (no network) and produces the
per-lot feature records the modelling stage consumes.

This module currently implements the notice-XML parsing needed for the LOT-level
bidder-count label (GitHub issue #1): the search API's `received-submissions-type-val`
is an unlabeled flat array that cannot be interpreted, so the label must come from the
XML's labeled per-lot statistics.

Usage:
    python extractor.py stats data/raw/xml            # per-lot bidder counts, summary
    python extractor.py stats data/raw/xml --json     # machine-readable
"""

import argparse
import json
import statistics
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# eForms statistics codes (listName="received-submission-type"). `tenders` is the
# total bid count; the rest are subsets of it and are kept as free extra features.
CODE_TENDERS = "tenders"
SUBSET_CODES = {
    "t-esubm": "n_bids_electronic",     # submitted electronically
    "t-sme": "n_bids_sme",              # from SMEs
    "t-oth-eea": "n_bids_other_eea",    # from other EEA countries
    "t-no-eea": "n_bids_non_eea",       # from outside the EEA
    "t-med": "n_bids_medium",
    "t-small": "n_bids_small",
    "t-micro": "n_bids_micro",
    "t-verif-inad": "n_bids_inadmissible",
    "t-verif-inad-low": "n_bids_abnormally_low",
}


def _tag(el: ET.Element) -> str:
    """Local tag name without the namespace."""
    return el.tag.rsplit("}", 1)[-1]


def _find_all(root: ET.Element, name: str):
    """Namespace-agnostic descendant search by local tag name."""
    return (el for el in root.iter() if _tag(el) == name)


def _first_text(el: ET.Element, name: str) -> str | None:
    for child in _find_all(el, name):
        return (child.text or "").strip() or None
    return None


def parse_lot_results(xml_text: str) -> list[dict]:
    """Per-lot results from an award notice XML.

    Each `<efac:LotResult>` names its lot (`<efac:TenderLot><cbc:ID>`) and carries
    `<efac:ReceivedSubmissionsStatistics>` entries as (StatisticsCode, StatisticsNumeric)
    pairs — labeled, unlike the search API's flat array.

    Returns one dict per lot result with:
      lot_id, n_bids, n_bids_source, plus any subset counts that were present.

    `n_bids_source` records where the headline count came from:
      "tenders"  — the explicit total (preferred)
      "t-esubm"  — fallback: some buyers publish only the electronic-submission count;
                   observed on 2 of 8 award notices in the local sample. Electronic
                   submissions are a subset of all tenders, so this is a lower bound.
      None       — no usable statistic; n_bids is None (missing, not zero)
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    out: list[dict] = []
    for lr in _find_all(root, "LotResult"):
        lot_id = None
        for tl in _find_all(lr, "TenderLot"):
            lot_id = _first_text(tl, "ID")
            break

        counts: dict[str, int] = {}
        for st in _find_all(lr, "ReceivedSubmissionsStatistics"):
            code = _first_text(st, "StatisticsCode")
            num = _first_text(st, "StatisticsNumeric")
            if not code or num is None:
                continue
            try:
                value = int(float(num))
            except ValueError:
                continue
            # Negative values are eForms' "not disclosed" marker, not a count.
            # Observed live: StatisticsNumeric = -1. Treat as missing.
            if value < 0:
                continue
            counts[code] = value

        if CODE_TENDERS in counts:
            n_bids, source = counts[CODE_TENDERS], CODE_TENDERS
        elif "t-esubm" in counts:
            n_bids, source = counts["t-esubm"], "t-esubm"
        else:
            n_bids, source = None, None

        rec = {"lot_id": lot_id, "n_bids": n_bids, "n_bids_source": source}
        for code, field in SUBSET_CODES.items():
            if code in counts:
                rec[field] = counts[code]
        out.append(rec)
    return out


def parse_notice(path: Path) -> dict:
    """Notice-level summary: id, type, lot count, per-lot results."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lots = sum(1 for _ in _find_all(ET.fromstring(text), "ProcurementProjectLot")) \
        if text.lstrip().startswith("<") else 0
    return {"publication_number": path.stem,
            "n_lots": lots,
            "lot_results": parse_lot_results(text)}


def cmd_stats(xml_dir: Path, as_json: bool) -> None:
    """Recompute the competition statistics from XML (issue #1, second task).

    The figures previously published in FINDINGS_ted.md came from the search API's
    flat array, which mixes per-type subsets (including structural zeros such as
    t-oth-eea / t-no-eea) into the same list — polluting median and single-bidder rate.
    """
    notices = [parse_notice(p) for p in sorted(xml_dir.glob("*.xml"))]
    with_results = [n for n in notices if n["lot_results"]]
    rows = [(n["publication_number"], lr)
            for n in with_results for lr in n["lot_results"]]
    counted = [(pn, lr) for pn, lr in rows if lr["n_bids"] is not None]
    values = [lr["n_bids"] for _, lr in counted]

    if as_json:
        print(json.dumps({"notices": notices}, ensure_ascii=False, indent=2))
        return

    print(f"XML files scanned:            {len(notices)}")
    print(f"award notices (with results): {len(with_results)}")
    print(f"lot results:                  {len(rows)}")
    print(f"lot results with a bid count: {len(counted)}")
    if not values:
        print("\nno bid counts found — nothing to summarise")
        return

    src = Counter(lr["n_bids_source"] for _, lr in counted)
    print(f"\nlabel source: {dict(src)}"
          f"   (t-esubm = electronic-submission fallback, a lower bound)")
    values_sorted = sorted(values)
    single = sum(1 for v in values if v == 1)
    zero = sum(1 for v in values if v == 0)
    print(f"\nbids per lot: min {min(values)}  median {statistics.median(values):g}  "
          f"mean {statistics.fmean(values):.1f}  max {max(values)}")
    print(f"  distribution: {dict(sorted(Counter(values).items()))}")
    print(f"  single-bidder lots: {single}/{len(values)} ({single/len(values):.0%})")
    if zero:
        print(f"  zero-bid lots:      {zero}/{len(values)} ({zero/len(values):.0%})"
              f"  (procedure ran but no admissible tender)")
    sme = [lr.get("n_bids_sme") for _, lr in counted if lr.get("n_bids_sme") is not None]
    if sme:
        print(f"\nSME split available on {len(sme)}/{len(counted)} lot results")


def main() -> None:
    p = argparse.ArgumentParser(description="TenderMining extractor")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stats", help="per-lot bidder counts parsed from notice XML")
    s.add_argument("xml_dir", type=Path, nargs="?", default=Path("data/raw/xml"))
    s.add_argument("--json", action="store_true", dest="as_json")
    args = p.parse_args()
    if args.cmd == "stats":
        cmd_stats(args.xml_dir, args.as_json)


if __name__ == "__main__":
    main()
