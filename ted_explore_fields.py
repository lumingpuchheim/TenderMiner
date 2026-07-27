"""Survey a TED sample (ted_awards_sample.jsonl) and report what can be mined:
money coverage, bid counts, winner countries/size, CPV categories, notice types."""

import json
import sys
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # TED data is multilingual UTF-8

SAMPLE = Path(__file__).parent / "data" / "ted_awards_sample.jsonl"


def first(v):
    """TED values are often lists or {lang: [..]} maps; pull one readable value."""
    if isinstance(v, dict):
        v = next(iter(v.values()), None)
    if isinstance(v, list):
        v = v[0] if v else None
    return v


def main() -> None:
    if not SAMPLE.exists():
        print(f"No sample found at {SAMPLE}. Run:  python ted_download_sample.py")
        return
    rows = [json.loads(line) for line in SAMPLE.read_text(encoding="utf-8").splitlines()]
    n = len(rows)
    print(f"Surveyed {n} TED notices\n")

    print("=== NOTICE TYPES ===")
    for t, c in Counter(first(r.get("notice-type")) for r in rows).most_common():
        print(f"  {t}: {c}")

    print("=== TEXT for ML (description-proc / title) ===")
    with_desc = sum(1 for r in rows if first(r.get("description-proc")) or first(r.get("description-lot")))
    with_title = sum(1 for r in rows if first(r.get("title-proc")) or first(r.get("notice-title")))
    print(f"  notices with a description: {with_desc}/{n} ({with_desc/n:.0%})")
    print(f"  notices with a title:       {with_title}/{n} ({with_title/n:.0%})")
    # one description -> value training pair, to show the data is ML-ready
    for r in rows:
        desc = first(r.get("description-proc")) or first(r.get("description-lot"))
        val = first(r.get("total-value"))
        if desc and val:
            print("  example (description -> value):")
            print(f"    text:  {str(desc)[:120]}...")
            print(f"    value: {val} {first(r.get('total-value-cur'))}")
            break

    print("\n=== MONEY (total-value) ===")
    vals = []
    for r in rows:
        v = first(r.get("total-value"))
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            pass
    print(f"  notices with a value: {len(vals)}/{n}  ({len(vals)/n:.0%})")
    if vals:
        vals.sort()
        print(f"  min {min(vals):,.0f}   median {vals[len(vals)//2]:,.0f}   max {max(vals):,.0f} EUR")

    print("\n=== COMPETITION (received-submissions-type-val = tenders received) ===")
    counts = []
    for r in rows:
        v = r.get("received-submissions-type-val")
        if isinstance(v, list) and v:
            try:
                counts.append(max(int(x) for x in v))  # the total is the largest of the breakdown
            except ValueError:
                pass
    print(f"  notices reporting a bid count: {len(counts)}/{n}")
    if counts:
        single = sum(1 for c in counts if c == 1)
        print(f"  min {min(counts)}  median {sorted(counts)[len(counts)//2]}  max {max(counts)} bids")
        print(f"  single-bidder awards: {single}/{len(counts)} ({single/len(counts):.0%})")

    print("\n=== WINNER SIZE (SME vs large) ===")
    for s, c in Counter(first(r.get("winner-size")) for r in rows if r.get("winner-size")).most_common():
        print(f"  {s}: {c}")

    print("\n=== TOP WINNER COUNTRIES ===")
    for c, k in Counter(first(r.get("winner-country")) for r in rows if r.get("winner-country")).most_common(8):
        print(f"  {c}: {k}")

    print("\n=== TOP CPV DIVISIONS ===")
    cpv = Counter()
    for r in rows:
        v = r.get("classification-cpv")
        if isinstance(v, list):
            for code in set(v):
                cpv[str(code)[:2]] += 1
    for div, c in cpv.most_common(8):
        print(f"  {div}xx: {c}")


if __name__ == "__main__":
    main()
