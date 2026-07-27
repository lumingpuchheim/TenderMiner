"""Survey every downloaded OCDS notice and report which fields are actually
populated, plus a few quick aggregations to show what could be mined."""

import glob
import io
import json
import sys
from collections import Counter

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

FILES = sorted(glob.glob("data/notices_2023-01-16/*.json"))


def walk(node, prefix, paths):
    """Record the set of leaf/branch paths present in one document."""
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{prefix}.{k}" if prefix else k
            paths.add(p)
            walk(v, p, paths)
    elif isinstance(node, list) and node:
        walk(node[0], f"{prefix}[]", paths)


field_counts = Counter()
releases = []
for f in FILES:
    doc = json.load(open(f, encoding="utf-8"))
    rel = doc["releases"][0] if doc.get("releases") else {}
    releases.append(rel)
    paths = set()
    walk(rel, "", paths)
    field_counts.update(paths)

n = len(releases)
print(f"Surveyed {n} notices from 2023-01-16\n")

print("=== Field coverage (share of notices where the path exists) ===")
for path, c in sorted(field_counts.items(), key=lambda kv: (-kv[1], kv[0])):
    if "." not in path.strip("[]") or c >= n * 0.05:  # top-level + anything >=5%
        print(f"  {c/n:5.0%}  {path}")


def rget(d, *keys):
    for k in keys:
        d = d.get(k) if isinstance(d, dict) else None
    return d


print("\n=== MONEY: tender.value.amount ===")
vals = [rget(r, "tender", "value", "amount") for r in releases]
vals = [v for v in vals if isinstance(v, (int, float))]
print(f"  notices with a value: {len(vals)}/{n}")
if vals:
    vals.sort()
    print(f"  min {min(vals):,.0f}  median {vals[len(vals)//2]:,.0f}  max {max(vals):,.0f}")
    curr = Counter(rget(r, "tender", "value", "currency") for r in releases if rget(r, "tender", "value", "amount"))
    print(f"  currencies: {dict(curr)}")

print("\n=== CATEGORY: CPV classification of items ===")
cpv = Counter()
for r in releases:
    for it in (rget(r, "tender", "items") or []):
        c = rget(it, "classification", "id")
        if c:
            cpv[c[:2]] += 1  # first 2 digits = CPV division
print(f"  notices/items carrying a CPV code: {sum(cpv.values())}")
for div, c in cpv.most_common(8):
    print(f"    CPV {div}xx: {c}")

print("\n=== REGION: buyer / place-of-performance location ===")
regions = Counter()
countries = Counter()
for r in releases:
    for party in (r.get("parties") or []):
        addr = party.get("address") or {}
        if addr.get("region"):
            regions[addr["region"]] += 1
        if addr.get("countryName") or addr.get("country"):
            countries[addr.get("countryName") or addr.get("country")] += 1
    for it in (rget(r, "tender", "items") or []):
        loc = rget(it, "deliveryAddresses") or it.get("deliveryLocation")
print(f"  parties with a region: {sum(regions.values())}")
for reg, c in regions.most_common(8):
    print(f"    {reg}: {c}")
print(f"  countries: {dict(countries.most_common(5))}")

print("\n=== BIDDERS / AWARDS: numberOfTenderers, awards, suppliers ===")
with_awards = sum(1 for r in releases if r.get("awards"))
tenderer_counts = [rget(r, "tender", "numberOfTenderers") for r in releases]
tenderer_counts = [t for t in tenderer_counts if isinstance(t, int)]
statmap = Counter(rget(r, "tender", "status") for r in releases)
print(f"  notices with an awards[] block: {with_awards}/{n}")
print(f"  notices with tender.numberOfTenderers: {len(tenderer_counts)}")
if tenderer_counts:
    print(f"    values: min {min(tenderer_counts)} max {max(tenderer_counts)}")
print(f"  tender.status distribution: {dict(statmap)}")

print("\n=== PROCEDURE: tender.procurementMethod / tag ===")
methods = Counter(rget(r, "tender", "procurementMethod") for r in releases)
tags = Counter(tuple(r.get("tag") or []) for r in releases)
print(f"  procurementMethod: {dict(methods)}")
print(f"  release tags: {dict(tags)}")
