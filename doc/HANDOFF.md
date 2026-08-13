# HANDOFF — the public trade pages and the forecast figure

Rewritten 2026-08-12. The 2026-08-11 version of this file described a
half-built pipeline and one unverified claim; both are resolved, and the
open question it could not settle is answered with a measurement below.

The goal, unchanged:

> **A visitor on a trade page should see how well we predict a
> non-contested tender — in his trade, honestly, including when we are bad
> at it.**

## 1. How the pieces fit now

The replay produces **one JSON document on stdout** and owns no path; every
readable form is a renderer over that document ([`TRADE_PAGES.md`](TRADE_PAGES.md)
§6d). The rewind machinery lives in `asof.py`, shared by all three rewind
programs (`REFACTOR.md` phase 5 — implemented, receipts in the log). The
programs were renamed 2026-08-12; [`METHODS.md`](METHODS.md) §0 has the
question × direction grid.

```
python rewind_all.py > run-<date>.json        # the replay, ~33 min
python rewind_all.py --render run-<date>.json # operator prose, seconds
python trade_pages.py --replay run-<date>.json # the pages, with the figure
```

No `--replay` → every page says „…behaupten wir dazu nichts", which is
correct, not a bug.

## 2. Measurements that used to be guesses

- **The full weekly replay takes ~33 minutes** (measured 2026-08-11: 79
  cutoffs, 46 trained, 33 min 26 s; seeded end to end, so a rerun is
  byte-identical). The old "it takes hours" claim was never a measurement.
  It *could* run weekly; there is currently no reason to spend that, since
  the figure moves quarterly at best and the page prints the document's
  date.
- **Forecast quality over the whole store** (runs of 2026-08-11 and
  2026-08-13, 5,994 checkable lots): precision 17% (CI 15–20%), recall 31%
  (CI 28–35%), base rate 9%, 1.83× chance. Per CPV3 (internal targeting only,
  never public): 452 at 2.01×, 453 at 1.34×, 454 at 1.18×, 450 and 451
  **below chance** — no lift number in their letters.

  The two runs agree to the digit across every cell of both tables. That is
  the receipt that `REFACTOR.md` phase 4a (the selection extraction) left the
  lot-level path alone: the statistic is computed from flagged and scored
  lots, which have no subscription in them. It is also evidence that the
  store has not moved since 2026-08-10, so the two runs are a clean A/B —
  the trap phase 3 fell into once and documented.

## 3. What remains before the figure is live

1. **Produce the document from the current store** and keep it wherever
   run artifacts should live (operator's naming — the program will not
   choose).
2. **Build and upload the site** with `--replay` pointing at it. Expect
   most trades to say "too few checked alarms" — the floor is
   `market.SMALL_SAMPLE` = 30 checked alarms, and that is the right bar.

## 4. Rules this work must keep

1. **Trades are matched by words in the lot title, never by CPV.** The
   document carries `cpv3` (raw store field) but no trade; consumers join
   to the store. `rewind_all.py`'s own CPV3 table is not quotable publicly.
2. **Two different things are called precision/recall** — *forecast*
   (`rewind_all.py`) vs *gate* (`calibrate.py`). Never quote one in the
   other's sentence. `METHODS.md` §0.
3. **No figure without its denominator and its date.**
4. **An unflattering measured result gets printed, not dropped.**

## 5. Still open, none of it code

- The Impressum text (`TM_IMPRESSUM`), `info@murara.eu` receiving mail,
  and the Resend sending domain — blocking the first letter.
- The VPS is unchosen; [`HOSTING.md`](HOSTING.md) has the comparison.
- `REFACTOR.md` phase 4b (`render.py`) is the next refactor due. Phase 4a
  (the selection, now `selection.py` — not `select.py`, which collides with
  the standard library) landed 2026-08-13: the global figure above is
  unchanged, the per-subscription pick lists are not, because the rewind used
  to impose a 14-day deadline horizon that six of the eight live
  subscriptions never promised.
