# TenderMining — Data Source Findings

Exploration of the German public-procurement open data at
[oeffentlichevergabe.de](https://oeffentlichevergabe.de) (the *Bekanntmachungsservice*,
"announcement service"). Goal: understand what data is available and what can be mined.

## 1. The data source

- **One public endpoint only:** `GET /api/notice-exports?pubDay=YYYY-MM-DD&format=ocds.zip`
  (a `pubMonth=YYYY-MM` variant also exists).
- **Coverage:** notices from `2022-12-01` up to the day before yesterday (~3 years).
- **Licence:** Creative Commons **CC0** (public domain).
- **Formats:** `eforms.zip` (original XML), `ocds.zip` (JSON), `csv.zip` (flat tables).
  We use **OCDS / JSON** (Open Contracting Data Standard v1.1) — self-describing, one
  JSON object per notice, trivial to parse.

### Two hard limitations

1. **ZIP-only, no single-file download.** The API only delivers a whole day (or month)
   as a ZIP bundle. There is no per-notice download. Our scripts unpack the ZIP into
   plain `.json` files immediately.
2. **No server-side filter.** The only query knobs are `pubDay` / `pubMonth` / `format`.
   There is **no** filter for region, CPV category, buyer, or keyword. You download the
   whole day and filter locally. A recent day is tens of MB and streams slowly; early-2023
   days are ~0.7 MB and download in seconds — good for sampling the schema.

## 2. What a notice looks like

Each file is one OCDS **release package**: `releases[0]` holds the notice. Key structure:

```
releases[0]
├── ocid, id, date            identifiers + publication date
├── tag: ["tender"|"award"|"planning"]   lifecycle stage  <-- most important classifier
├── buyer / parties[]         the public authority (name, address, contact email)
├── tender
│   ├── title, description     free text (subject; on awards, sometimes the winner)
│   ├── items[].classification.id   CPV code (what is being bought)
│   ├── items[].deliveryAddress.region   NUTS code (where)
│   ├── lots[]                 sub-packages
│   ├── value.amount           estimated value (rarely present)
│   ├── numberOfTenderers      bid count (0/placeholder unless an EU award)
│   └── tenderers[]            bidding companies (award notices only)
├── awards[]                   present only on award notices
└── contracts[].value.amount   the signed contract value (award notices only)
```

## 3. Tender vs. Award — how to tell them apart

The single classifier is **`releases[0].tag`**:

| You want… | Look for |
| --- | --- |
| Is it an award? | `tag` contains `"award"` |
| Does it name a winner? | `awards[]` block exists (or `tender.tenderers[]`) |
| Does it have money? | `contracts[].value.amount` is set |
| Just a call for bids? | `tag == ["tender"]`, and **no** `awards`/`contracts` |

## 4. Two days compared

| Signal | 2023-01-16 (538 notices) | 2026-07-20 (105 notices) |
| --- | --- | --- |
| Lifecycle mix | 425 tender / 102 award | 74 tender / 17 award / 3 planning |
| `tender.value.amount` | **0 / 538** | 3 / 105 |
| `contracts[].value` (signed €) | none | ~7 / 105 |
| `tender.tenderers[]` (winners) | none | ~11 % |
| `numberOfTenderers > 0` | none | 12 |
| Region (NUTS) | 83 % populated | ~50 % |
| CPV category | 98 % | 79 % |
| Buyer contact email | — | 74 % |

The **2023 national feed carries no structured money or bidders**; the **2026 day does**,
because it includes **EU-threshold award notices** (see §6).

## 5. What can be mined, and how easily

| Signal | Field(s) | Ease | Notes |
| --- | --- | --- | --- |
| **Buyer** | `buyer.name`, `buyer.address` | 🟢 Trivial | who is buying |
| **Category** | `tender.items[].classification.id` (CPV) | 🟢 Trivial | what, as an EU code |
| **Location** | delivery `region`/`locality`, `buyer.address` | 🟢 Easy | NUTS region |
| **Lifecycle** | `tag` | 🟢 Trivial | tender / award / planning |
| **Contact** | `buyer.contactPoint.email` | 🟢 Easy | lead-gen |
| **Documents** | `tender.documents[].url` | 🟢 Easy | links to tender PDFs |
| **Money (structured)** | `contracts[].value`, `tender.value` | 🟡 Medium | **award notices only**, ~10–20 % |
| **Bidders / winners** | `tenderers[]`, `numberOfTenderers` | 🟡 Medium | award notices only |
| **Money (free text)** | inside `tender.description` | 🔴 Hard | rare, and error-prone (see §7) |

**~90 % of the value** (buyer, category, region, lifecycle, contacts, doc links) is
structured and mineable with plain JSON parsing — **no ML required**. Money and
competition are structured too, but only on the award subset.

## 6. EU-threshold award notices (why money appears in a field)

German procurement runs on two tracks, split by contract value (the *Schwellenwert*):

- **Above threshold** (≈ €221k supplies/services, ≈ €5.5M works; revised biennially) →
  EU-wide rules → mandatory **eForms** template → **award value is a required structured
  field**.
- **Below threshold** → lighter national forms → value often **absent** or only in prose.

So the trustworthy money dataset is: **`tag == "award"` AND `contracts[].value.amount`
present.** That combination produced the clean figures below.

Real examples from 2026-07-20:

| Contract | Value | Bidders |
| --- | --- | --- |
| School catering (4 lots) | €1.9M / €138k–€547k | 1 (RWS Cateringservice) |
| Briefbeförderung (mail) | €2.1M + 3 more | **4** (Deutsche Post InHaus, REGIO, Pin Mail, Turbo Post) |
| Vermessungsleistungen DB | €313,600 | 1 (DB Engineering & Consulting) |

**Notable:** 11 of 12 award notices with a bidder count had **only 1 bidder** — a strong
competition-concentration signal worth mining.

## 7. Free-text money extraction — possible, but a trap

Some notices mention euros only in prose, e.g. notice *"0051-84 Losübergreifendes
Projektmanagement"* (`tender.value.amount = None`):

> "…das im Hauptvertrag vereinbarte **Honorar von 5.995.442,50 EUR** vergleichsweise günstig…"

A German-number regex (`\d{1,3}(?:\.\d{3})+,\d{2}\s*EUR`) extracts `5.995.442,50` instantly.
**But that figure is wrong for this award** — it refers to a *different, referenced*
contract. The actual awarded value is the **structured** `contracts[].value` = **€85,232**.

**Lesson:** regex easily *finds* euro amounts; the hard part is deciding *which* number is
the contract value vs. a deadline, quantity, penalty, or an unrelated reference. That
classification is the real ML task (NER / a text classifier). Engineering rule: **prefer
the structured field; fall back to text only when it is absent.**

## 8. Recommended next steps

1. **Flatten to a table.** Build a CSV/Parquet extractor: one row per notice
   (buyer, CPV, region, tag, value, n_bidders, winner) across a whole month.
2. **Trustworthy money dataset.** Filter `tag == award` + populated `contracts[].value`.
3. **Analyses:** demand map (CPV × region × time), buyer/winner networks, single-bidder
   concentration, live open-tender lead feed (CPV filter + `documents[].url` + email).

## Scripts

- `download_sample.py` — download + unpack **one small early day** (fast schema sample).
- `analyze_day.py` — download + unpack **any day** (no timeout) and print a field survey.
- `explore_fields.py` — field-coverage survey over an already-downloaded folder.

Data lands in `data/` (git-ignored — re-download with the scripts above).
