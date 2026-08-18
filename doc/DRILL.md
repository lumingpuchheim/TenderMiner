# DRILL — proving the backup restores

Written 2026-08-15, from the operator's question "what is the restore drill,
what is missing". This is the **spec**; the first drill has not run.
Companions: [`OPERATIONS.md`](OPERATIONS.md) §3 (the backup this proves)
and §4 (the runbook this times), [`SECRETS.md`](SECRETS.md) (where the
restic password comes from), [`RUNBOOK.md`](RUNBOOK.md) (where the result
is written).

## 0. The question the drill answers

*If the VPS and the OVH account were gone at 09:00, could reports be served
again by lunch, from nothing but the bucket, git and the password manager?*

"The backup ran" does not answer it. Three failures only a restore reveals:

1. the bucket has data but the password in the manager is wrong or stale —
   the copy is unreadable;
2. the export restores but `db.py --migrate` refuses it (schema drift, or a
   ledger file holding more rows than its table — the guard `ledger.read`
   enforces) — the record exists, the code will not load it;
3. the app serves but the next cycle cannot run — `raw/` incomplete, or the
   rebuild needs something the backup does not carry.

Sunday's `restic check --read-data-subset=5%` ([`docker/nightly.sh`](../docker/nightly.sh))
catches bit-rot in the bucket. It catches none of these.

## 1. Rules

- **Not on the server, not touching the server.** Laptop or a throwaway
  VM. The bucket is read only; nothing is written back.
- **The password comes from the password manager**, never from the
  server's `.env` — "the manager's copy is right" is one of the things
  under test ([`SECRETS.md`](SECRETS.md) §0).
- **`--network none` for the cycle.** A restore that quietly fetches
  something from the live server or the internet has proven less than it
  looks. (Exception, deliberate: the embedding model comes from a local
  cache seeded beforehand — it is a public download, not state.)
- **Timed, and the times are the deliverable.** Two clocks: *serving*
  (app answers with the restored record) and *rebuilt* (a full cycle has
  run). OPERATIONS.md §4 promises the first in half a working day.

## 2. Preconditions

| # | needed | today (2026-08-15) |
| --- | --- | --- |
| 1 | a bucket with ≥1 nightly push | not yet — restic keys not in `.env` |
| 2 | ≥1 subscription, so a database exists to restore | 0 subscriptions; a test subscription for the operator suffices |
| 3 | the restic password readable from the manager | operator |
| 4 | Docker + a clone of master on the drill machine; ~6 GB free | laptop |
| 5 | the jina model in a local `tm-model-cache` volume (RUNBOOK §1b seeding) | laptop has it |

## 3. The steps

Numbered as OPERATIONS.md §4 numbers them, so a gap found here maps onto
the runbook line it belongs to. `DRILL=…` is an empty directory outside any
checkout.

```
# 1–2. restore
export RESTIC_REPOSITORY=…  RESTIC_PASSWORD=…  AWS_…      # from the manager
restic snapshots                                            # proves password + reachability
restic restore latest --target "$DRILL"                     # raw/ export/<date>/ logs/
   ─ clock A starts ─
# 2. the record
python db.py --migrate --data-dir "$DRILL/export/<date>"    # → tendermining.db in $DRILL
python subscriptions.py --data-dir "$DRILL"                 # customers present, versions valid
# 4. no secrets restored: expected. .env comes from the manager (SECRETS.md).
# 5a. serving
TM_STATE="$DRILL" docker compose up -d app
curl -s localhost:8000/healthz                              # cycle_age_days from the restored checkpoint
   → open one token link taken from the restored deliveries table
   ─ clock A stops: "serving" ─
# 5b. rebuilt (unattended; hours)
TM_STATE="$DRILL" docker compose run --rm --network none tm \
    python cycle.py run --last 7d                            # store, embeddings, champion, picks
   ─ clock B stops: "rebuilt" ─
diff <(picks from the drill) <(picks the server made the same week)   # same inputs → same list
```

The exact `db.py --migrate` invocation against an export directory is
whatever `db.py --help` says the day of the drill; if it takes more than
one flag to point it at `export/<date>`, that is a finding (§5).

## 4. What is written down

One block appended to [`RUNBOOK.md`](RUNBOOK.md):

```
Restore drill YYYY-MM-DD — snapshot <id> of YYYY-MM-DD, drill machine <what>
  restore:   N min, X GB
  serving:   N min from restore start      (target: ≤ 4 h incl. renting a VM)
  rebuilt:   N h  (embedding N lots)       (measured 2026-08-14 on the VPS: ~10.5 h)
  picks match server: yes / no (diff)
  by hand, not in OPERATIONS.md §4: …      (each line becomes a commit)
```

Cadence after the first: **quarterly**, and after any change to `db.py`
schema, `nightly.sh`, or the restic retention.

## 5. What a finding turns into

A step that needed a human guess, a flag not in the docs, a file that had
to be fetched from somewhere other than the bucket, a mismatch in the pick
diff — each is a commit against the runbook or the code, never a note in
someone's head. The drill is the "did we forget something" test; its
output is the list of forgotten things.

## 6. Order of work

1. Restic keys into `.env` (operator; the night that happens, layer 2 starts).
2. One subscription — the operator's own address is fine — so tomorrow's
   export carries a database.
3. Drill steps 1–2 and 5a the day after: proves the irreplaceable part,
   ~1 h attended.
4. Step 5b overnight for the second number.
5. RUNBOOK entry; findings → commits; OPERATIONS.md §7 item 5 struck.
