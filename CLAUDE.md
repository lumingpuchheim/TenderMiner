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
The ledgers (`deliveries`, `learned_refs`, `predictions`, `grades`) are moving
too; read them through `loop.py` / `feedback.py`, which already take a data
directory rather than a filename.
