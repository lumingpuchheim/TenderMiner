# Component: One store for all trades — IT first

Decided with the operator 2026-08-22. The store widens from German construction
(CPV 45) to construction + software + IT services (**45, 48, 72**), so IT firms
can be customers of the same weekly product. Hardware (302) is explicitly out
for now; archives are kept from this migration on, so adding a division later
is a local re-scan, not a re-download.

## What changes, and what deliberately does not

Two code changes:

1. **`cycle.py`**: the default `--cpv` becomes `'45,48,72'`.
2. **`training.py`**: every candidate's `meta.json` gains `val_by_division` —
   the validation grades (PR-AUC, ROC-AUC, base rate, within-division top-slice
   hit/lift, n) computed per CPV division beside the pooled numbers. The pooled
   PR-AUC the promotion gate compares can hide a slide in one trade once the
   store spans several; the per-division rows make that visible every cycle.
   Recorded evidence only — promotion still compares the pooled number.

Nothing else moves. Delivery, subscriptions, the mailer and `features.py` are
already trade-agnostic; a customer's market is their `cpv_prefixes`, so
construction customers cannot receive an IT lot regardless of store contents.

## The flag day, and the receipts that gate it

The first cycle at the widened scope changes the feature schema (new 48/72
vocabulary columns). `training.py` promotes a schema-changed candidate
**unconditionally** — the old champion cannot score the new columns, so no
comparison is defined. That is the designed flag-day rule, and it means the
gate protects nothing on exactly the cycle that widens the store. Therefore
two receipts run first, against real data, before the scope change deploys:

- **Receipt A — is there a product?** The single-bid base rate among IT lots
  (48/72) and whether a champion-recipe model ranks IT lots above chance
  (within-division top-slice lift on IT-only validation lots).
- **Receipt B — does construction survive?** Train twice with identical
  settings: once on the 45-only store, once on 45+48+72. Grade both on the
  **identical construction-only validation window** — same lots, same
  denominator, directly comparable. Pass: the widened model's construction
  PR-AUC holds within the promotion epsilon (0.005).

A failed Receipt B blocks the flag day. Receipt A is the operator's go/no-go
on the product itself.

## Migration mechanics (2026-08-22)

The backfill ran **on the server** (operator: no backfill on the laptop),
as a detached container on the deployed image, writing only new XML into
`/data/raw/xml` — publication numbers not already on disk — plus kept monthly
archives in `/data/raw/packages`. It adds files and touches nothing else: not
the store, not `models/`, not the database. Scheduled service (Monday cycle
and delivery, nightly backup and backplay, the website) is unaffected; the
job runs outside cron hours and holds no lock it would contend on.

After the receipts pass and the operator says go: deploy the new tag, hand-run
one mid-week cycle (mails nobody), expect the announced schema-change warning
and `val_by_division` in the new champion's meta, and time the cycle — the
Monday delivery at 08:30 waits on the heavy lock rather than skipping, but a
cycle that outgrows its 90-minute head start should be known, not discovered.

**Rollback**: redeploy the previous tag. Its next cycle rebuilds the store at
CPV 45 from the same XML (deterministic, minutes) and the flag-day rule
promotes a 45-schema candidate; every earlier champion remains on disk in
`models/<id>/`. The predictions ledger keeps its IT rows — append-only by
design; they are record, not damage.

## IT customers and the relevance gate

The relevance gate's benchmark is operator-labeled construction cases, and the
trusted-codes lists predate the widened store. Until the operator labels IT
cases and `calibrate.py` re-runs over the widened store, IT subscriptions run
**without** the gate: `cpv_prefixes` (e.g. `["72", "48"]`) + `profile_texts`,
market filter only — exactly how early construction customers ran.
