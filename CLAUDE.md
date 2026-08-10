# TenderMining — working rules

## Never work directly on master

All work happens in a git worktree, never in the primary checkout on
`master`. Create one before the first edit — not after, and not "just for
this small change".

- Start with the `EnterWorktree` tool (or `git worktree add`); worktrees
  live under `.claude/worktrees/`.
- Make every commit on the worktree's branch, then merge or open a PR from
  there. `master` in the primary checkout stays clean and buildable.
- This applies to documentation-only and one-line changes too. The rule
  exists so an in-progress edit never blocks a scheduled `loop.py` run
  reading the working tree, and so a half-finished change is never what
  `master` has.

If you notice you have already edited the primary checkout, stop and move
the change to a worktree before committing.

## Inside the worktree you have full access

Once the session is in a worktree under `.claude/worktrees/`, run whatever the
work needs — scripts, the test suite, a receipt harness, a throwaway
`python -c` — without stopping to ask each time. The worktree *is* the safety
margin: the files are a private copy on a private branch, `master` is
untouched, and the worst outcome is one `git worktree remove`. Asking for
permission to run `python foo.py` there buys nothing and costs a round trip.

Two things are not covered by that, because they reach outside the worktree:

- **The real `data/`.** A fresh worktree has no `data/` at all (it is
  gitignored), so anything touching real records has to be pointed at the
  primary checkout's copy explicitly — and that copy is shared with scheduled
  `loop.py` runs. Reading it is fine; writing to `data/tendermining.db` or a
  live ledger is still a question first.
- **`git push`, and the deny-list.** Pushing, `reset --hard`, force-push and
  the rest stay gated wherever you are.

This rule is about *asking*, not about the permission prompt itself — that one
is decided by the allow-list in `~/.claude/settings.json`, which is user-level
precisely so a newly created worktree inherits it instead of re-asking from
scratch. If a routine command prompts every time, the fix is a line there, not
a paragraph here.

## Merging and pushing is yours to do

You may merge a worktree branch into `master` and push it to `origin`
yourself, without asking each time. Fast-forward when the branch allows it.

This does not loosen the rule above: the editing still happens on the
worktree branch, and `master` only ever receives work that is finished —
spec, implementation and passing checks all done, never a half-landed
change pushed "so it is saved".

## Subscriptions: go through `subscriptions.py`, always

Several agents work on this repo at once, and subscription storage is moving
from a JSONL file into SQLite (`REFACTOR.md` phase 2). Two rules keep that
migration invisible to everyone else:

1. **Never open subscription storage yourself.** No
   `open('data/subscriptions.jsonl')`, no `json.loads` over its lines, and do
   not construct that path. Read with `subscriptions.load(data_dir, as_of)` or
   `subscriptions.one(data_dir, as_of, sub_id)`; create a throwaway set with
   `subscriptions.write_sandbox(dir, [sub])`. Every function takes the
   **directory** subscriptions live in, never a file — which is why the format
   can change under you without a single call site moving. Passing something
   that looks like a storage file raises rather than half-working.

2. **A new subscription field must be added to `KNOWN` in
   `subscriptions.py`, in the same commit that starts using it.** Validation
   rejects unknown fields on purpose (a silently ignored field is discovered
   from a wrong report weeks later) — but the rejection is not scoped to the
   line that carries it: an unknown field stops delivery for *every*
   customer. So a per-customer gate knob is a two-file change, and the
   `KNOWN` half comes first.

In-process subscription *dicts* are unaffected: `relevance.build_profile`
takes a mapping, so the receipt harnesses that synthesise
`{'sub_id': 'judge-run', 'profile_refs': [...], ...}` keep working untouched.

## The JSONL ledger files are NOT authoritative any more

As of 2026-08-08 the database is the record. `data/tendermining.db` is what the
cycle reads and writes; the files beside it are frozen snapshots from before the
migration and they fall further behind with every cycle.

**`data/ledger/predictions.jsonl` is the clearest trap.** It stopped growing and
is thousands of rows behind — do not read it, do not join against it, and above
all do not restore it from a backup expecting it to be true. Same for
`grades.jsonl`, `deliveries.jsonl`, `learned_refs.jsonl`, `gate_configs.jsonl`
and `subscriptions.jsonl`.

Read every one of them through [`ledger.py`](ledger.py), which takes the
**directory** and the ledger's name and decides where the records actually are:

```python
rows = ledger.read(data_dir, 'predictions')      # or 'grades', 'deliveries', ...
ledger.append(data_dir, 'deliveries', new_rows)
```

For predictions specifically, prefer the targeted queries over reading 98,000
rows to answer a narrow question: `ledger.prediction_keys`,
`ledger.predictions_by_lot(lots=…)`, `ledger.prediction_titles`,
`ledger.prediction_scores_since`.

If a ledger file ever holds more rows than its table, `ledger.read` **raises**
rather than serving stale records, and the message names the fix
(`python db.py --migrate`). That guard is the only thing standing between a
restored backup and a customer being served last month's market.

To get readable, greppable, diffable text back at any time:
`python db.py --export-jsonl DIR`. That is also the tested rollback path —
export, delete the database, and the previous code runs from files again.
