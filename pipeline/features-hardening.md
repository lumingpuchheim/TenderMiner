# features.py — hardening & completeness specification

Status: **draft, awaiting approval — no implementation yet.**
Scope: `features.py` (XML → `tenders.parquet` / `awards.parquet`). The download job and
the source XML are out of scope; the extractor never edits source data.

Origin: code review of 2026-07-30 (error handling + XML-coverage audit over the
~4,500-notice corpus). Findings are numbered R1–R7 and specified in full below; the
build order at the end phases the work without cutting any of it.

## Design principles (unchanged)

1. **Export what the notice said.** Contradictions in the source are surfaced, never
   silently "fixed".
2. **Null means unknown, never guessed.** Coercion failures stay null — but they must
   now be *counted*, not silent.
3. Post-outcome data stays in `awards.parquet`; call-time data in `tenders.parquet`.

---

## R1 — Coercion telemetry (silent nulls become countable)

**Problem.** Every malformed value (bad date, bad float, unknown duration unit,
inverted period, org referenced but missing) becomes a silent null. A sender that
systematically emits a malformed field drains a column to null with no signal.

**Spec.**
- A module-level `collections.Counter` records one event per *suppressed* value, keyed
  by `(context, reason)` — e.g. `('duration_measure', 'unknown_unit:HUR')`,
  `('issue_date', 'bad_date')`, `('bid.tenderer', 'org_missing')`.
- Counted: any case where a **non-empty** input produced null. Absent elements are NOT
  counted (absence is normal in eForms).
- At end of run, non-zero counters print to stdout and are written to
  `data/logs/extract_report.json` together with run stats (files, rows, failures).
- No behavior change to the values themselves.

## R2 — Per-file fault isolation

**Problem.** Only `ET.ParseError` is caught per file; any other exception from one
pathological notice (e.g. `OverflowError` via `int(float('inf'))` in `_measure_days`)
aborts the whole run. Failures are printed (first five) and discarded.

**Spec.**
- The per-file loop catches `Exception`, not just `ET.ParseError`. One bad file costs
  its rows, never the run.
- Every failure is appended to `data/logs/extract_failures.jsonl` as
  `{file, error_type, error, timestamp}` and counted in the run report (R1).
- New flag `--strict`: re-raise on first failure (for debugging).
- `_measure_days` additionally catches `OverflowError` alongside `ValueError`.

## R3 — Cross-field quality flags

**Problem.** The extractor exports self-contradictory notices without any marker
(observed in corpus: `result_code='selec-w'` with `n_tenders=0` and a named winner;
`question_window_days=-11`). Every analysis has to rediscover these checks.

**Spec.** New column `quality_flags: list<string>` on **both** tables. Flags never
modify values. Defined flags:

| Table | Flag | Condition |
| --- | --- | --- |
| awards | `selected_but_zero_tenders` | `result_code == 'selec-w'` and `n_tenders == 0` |
| awards | `winner_but_zero_tenders` | `n_winners > 0` and `n_tenders == 0` |
| awards | `tender_band_inverted` | `lowest_tender_amount > highest_tender_amount` (both present) |
| awards | `winning_bid_outside_band` | any bid amount outside `[lowest, highest]` (band present) |
| tenders | `deadline_before_issue` | `deadline_days < 0` |
| tenders | `question_deadline_before_issue` | `question_window_days < 0` |
| tenders | `opening_before_deadline` | `opening_lag_days < 0` |
| tenders | `period_inverted` | `period_start > period_end` |
| tenders | `duration_out_of_range` | duration rejected by the (0, 20y] sanity cap |

Flag counts appear in the run report (R1). The set is append-only; removing a flag is
a breaking change.

## R4 — Withheld sentinel on estimated values

**Problem.** `est_value_lot` / `est_value_procedure` are read with `_float`, so a
withheld value (`-1`) would be exported as −1.0 EUR. The guard exists only on award
amounts. Not triggered in the current corpus; latent one-word bug.

**Spec.** Both estimated-value fields go through `_number` (withheld → null), counted
under R1 as `('est_value', 'withheld')`.

## R5 — Honest winner/bid semantics

**Problem (verified empirically).** German notices publish `LotTender` detail only for
the winning tender: among 800 lots with ≥2 tenders, `n_bids_detailed ≤ 1` in 797. The
docstring claims "individual bids nested underneath", and `winner_names` collects
names from **all** referenced tenders without checking rank — correct today only
because referenced tenders happen to be winners.

**Spec.**
- Rename columns: `bids` → `winning_bids`, `n_bids_detailed` → `n_winning_bids`.
  (Downstream impact: `eda_single_bidder.ipynb` §8 only; update alongside.)
- `winner_names` derivation: from referenced tenders, filtered to `rank == 1` **when
  any rank is present**; unranked referenced tenders count as winners (status quo).
- If a lot references more than one tender and none carries a rank, flag
  `multiple_unranked_tenders` (R3 mechanism).
- Module docstring and column comments updated to state that losing bids are not
  published in this corpus.

## R6 — All document references, not the first

**Problem.** `docs_restricted` / `docs_url` read only the first
`CallForTendersDocumentReference`; lots routinely carry several. A lot whose first
reference is open and second restricted is exported `docs_restricted=False`.

**Spec.**
- `docs_restricted = True` if **any** reference is `restricted-document`; `False` if
  references exist and none is restricted; null if no references.
- `docs_url` = first non-restricted reference URI, else first URI.
- New column `n_doc_references: int32`.

## R7 — New fields from the XML-coverage audit

Fields present in the corpus but absent from the parquet exports. All are additive.

### tenders.parquet

| Column | Type | Source (local-name path) | Notes |
| --- | --- | --- | --- |
| `award_criteria` | `list<struct{type, name, description, weight, weight_code}>` | `AwardingTerms/AwardingCriterion/SubordinateAwardingCriterion` (+ `Name`, `Description`, parameter ext.) | Replaces the parallel lists `award_criterion_types` / `award_criterion_weights` (derivable; parallel lists are drop-prone). `award_criterion_kind` / `price_weight_pct` stay as derived columns. |
| `award_criterion_kind` fallback | — | criterion `Name`/`Description` text | When every `TypeCode` is absent, classify `price-only` from text (case-insensitive: `preis.*(einzige\|alleinige)s? zuschlagskriterium`, `niedrigster preis`, `100 ?% preis`). Rows classified this way get flag `criterion_kind_from_text`. Recovers kind for ~27% of joined lots currently null. |
| `execution_requirements` | `list<struct{list_name, code, description}>` | `TenderingTerms/ContractExecutionRequirement` (+ `@listName` of the code) | Carries performance-guarantee and e-invoicing conditions; `@listName` disambiguates the overloaded code values (`performance`, `required`, `not-allowed`, …). |
| `exclusion_grounds` | `list<string>` | `TendererQualificationRequest/SpecificTendererRequirement/TendererRequirementTypeCode` (lot, falling back to root) | Codes as published (`exg-*`, `late-all`, `none`, `epo-*`). |
| `n_exclusion_grounds` | `int32` | derived | Count of `exg-*` codes only (boilerplate `epo-*` excluded from the count, kept in the list). |
| `strategic_procurement` | `list<struct{code, value}>` | `TenderingTerms/StrategicProcurement` descendants (`ApplicableLegalBasis`, inner codes/measures) | Raw pairs; `is_strategic` unchanged. |
| `publication_number` | `string` | `Publication/NoticePublicationID` | TED publication number = raw filename stem; the public join key. |
| `publication_date` | `date32` | `Publication/PublicationDate` | |
| `gazette_id` | `string` | `Publication/GazetteID` | |
| `deadline_days_published` | `int32` | derived: `publication_date` → `deadline_date` | Complements `deadline_days` (issue-based); issue can precede publication. |
| `procedure_languages` | `list<string>` | `TenderingTerms/Language/ID` (lot, fallback root) | |

### awards.parquet

| Column | Type | Source | Notes |
| --- | --- | --- | --- |
| `publication_number`, `publication_date`, `gazette_id` | as above | `Publication/*` | Same keys on the award side. |
| `winning_bids[].tenderer_sizes` | `list<string>` per bid | `Organizations/Organization/Company/CompanySizeCode` via org id | micro/small/medium/large/sme. |
| `winner_size` | `string` | derived | Size of the single winner; null when 0 or ≥2 winners. |
| `winning_bids[].tender_reference` | `string` | `LotTender/TenderReference/ID` | |
| `n_beneficial_owners` | `int32` | count of `UltimateBeneficialOwner` records linked to winner orgs | Count only; no personal data is exported. |

Explicitly **not** extracted (reviewed and rejected as plumbing/free text with no
modeling value): contact details (phone/fax/email/names), street addresses, appeal-body
references, `AdditionalInformationParty`, `OpenTenderEvent` location/description,
submission `EndTime` fields, `ProcurementProject/Note`, UBL/customization version ids.
Revisit `Note` if description-text features ever underperform.

---

## Non-goals

- No re-shaping of the two-table split or the join contract
  (`procedure_id`, `lot_id`) — unchanged.
- No imputation, no dropping of contradictory rows (flags only).
- No changes to download.md / extractor.md pipeline specs; this hardens the existing
  `features.py` step.

## Build order (phases sequence the work; nothing is cut)

1. **Correctness guards** — R2 (fault isolation), R4 (withheld sentinel), R6 (all doc
   references). Small diffs, immediately safer output.
2. **Observability** — R1 (telemetry + run report), R3 (quality flags). Requires 1
   (report file shares the failure log plumbing).
3. **Semantics** — R5 (winner/bid rename + rank filter) with the notebook update in
   the same change.
4. **New fields** — R7, one commit per table is acceptable; ends with a full re-extract
   over the then-current corpus and a `--coverage` run committed to the run report.

## Acceptance criteria

- A run over the full corpus completes with ≥1 non-zero telemetry counter printed and
  `data/logs/extract_report.json` written (R1).
- A deliberately corrupted XML file costs exactly its own rows; the run exits 0 and the
  file appears in `extract_failures.jsonl` (R2); with `--strict` it raises.
- The known contradictory notice (selec-w, 0 tenders, named winner) carries
  `selected_but_zero_tenders` and `winner_but_zero_tenders` (R3).
- `est_value_lot == -1` in a synthetic notice exports as null (R4).
- Renamed columns present; `winner_names` unchanged on the current corpus (R5 —
  regression: byte-identical winner_names column before/after on the same corpus).
- A synthetic lot with [open, restricted] document references exports
  `docs_restricted=True` (R6).
- Every R7 column appears in `--coverage` output with a plausible fill rate; the
  `award_criterion_kind` null rate on the joined set drops materially (~27% → single
  digits expected).
