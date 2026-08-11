# HANDOFF — the public trade pages and the forecast figure

Written 2026-08-11 at the end of a long session, for whoever picks this up.
The goal in one line:

> **A visitor on a trade page should see how well we predict a
> non-contested tender — in his trade, honestly, including when we are bad
> at it.**

The machinery for that is built and merged. What is missing is the *data*,
and one claim of mine that was never verified.

## 1. Where things stand

**Built and on master:**

- `site/` — the hand-written public pages (landing, Impressum, Datenschutz,
  `style.css`, `robots.txt`). Brand is **Murara**, contact `info@murara.eu`,
  domain `murara.eu`. Edit these by hand; there is no build step for them.
- `trade_pages.py` — builds the whole site into **`<data-dir>/public/`**
  (never into `site/`, which in the container is the read-only image). 32 of
  47 trades qualify; the other 15 are named at every run.
- The forecast section on every trade page — [`TRADE_PAGES.md`](TRADE_PAGES.md)
  §6c has the full design, four states, and why the unflattering one prints.
- `backtest.py` writes `data/reports/backtest_lots.json` — the per-lot receipt
  the section reads. **Run the replay once, slice it many times.**
- `doc/METHODS.md` §0 — which program answers which question. Read it before
  quoting any precision or recall.

**Every page today says „noch nicht genug ausgewertete Hinweise".** That is
correct behaviour, not a bug: no receipt exists in the live `data/` yet.

## 2. The one thing that makes it real

```
docker compose run --rm tm python backtest.py
```

That writes the receipt. The next `trade_pages.py` run then fills the section
in. Nothing else is needed.

**Expect most trades still to say nothing.** A synthetic dry run over the real
6,685 resolved lots put 22 of 32 trades in "too few checked alarms" and 10 in
"beats chance" — and that used a deliberately generous flagging rule. The
floor is `market.SMALL_SAMPLE` = 30 *checked alarms*, which is the right bar
and will exclude most trades for a while.

## 3. Open question I could not settle — and where I was probably wrong

The operator asked, and I did not get to answer: **why can the backtest not
just run weekly?**

I said repeatedly that it takes hours. **I never measured it.** That claim
came from a line in `STORAGE.md` ("a backtest can run for hours") written
about a different configuration, and I repeated it as if it were a
measurement. The operator pushed back and may well be right.

What is actually true: `backtest.py` replays every weekly cutoff since the
awards begin, and retrains the champion at each one. Whether that is minutes
or hours on this store **is an empirical question nobody has answered.**

So: **time it.** `--step 28` replays monthly instead of weekly and should be
roughly a quarter of the cost. If a full run is tolerable, put it in the
weekly cycle and delete the "not in the cycle" caveat from
[`TRADE_PAGES.md`](TRADE_PAGES.md) §6c. If it is not, keep the receipt and
refresh it quarterly. Either way the page prints the receipt's own date, so a
stale figure ages visibly.

## 4. Rules this work must keep

Four, all learned the hard way in this session:

1. **Trades are matched by words in the lot title, never by CPV.** Buyers
   enter CPV wrongly; `market.py` never consults it. `backtest.py`'s own prose
   table still groups by CPV3 — that table is therefore **not quotable on a
   public page**, and the per-trade slice deliberately re-does the grouping.
2. **Two different things are called precision/recall.** *Forecast* (did a
   flagged lot really end with 0-1 bids — `backtest.py`) and *gate* (is this
   lot in the customer's trade — `calibrate.py`). Never quote one in the
   other's sentence. `METHODS.md` §0.
3. **No figure without its denominator and its date.** A share over 8 alarms
   is noise; a market page with no „Stand" silently ages into a lie.
4. **An unflattering measured result gets printed, not dropped.** Operator's
   call. A silently missing section is the version a reader cannot audit.

## 5. Housekeeping left over

- **`murara:latest` is an orphan image** — from a rename that was reverted.
  `docker rmi murara:latest`, nothing references it.
- **The running scheduler is on a stale image** (`39059efcd2db`, ~9 h old, now
  untagged). Docker never swaps a running container's image on rebuild, so
  next Monday it would run pre-forecast code. Recreate it:
  `docker compose --profile scheduler up -d scheduler`, then the old layer is
  collectable.
- **Blocking the first letter**, none of it code: the Impressum text
  (`TM_IMPRESSUM`, § 5 TMG — the HTML comment in
  `site/impressum/index.html` lists what is required), `info@murara.eu`
  receiving mail, and the Resend sending domain verified.
- The VPS is still unchosen; [`HOSTING.md`](HOSTING.md) has the measurements
  and the comparison.
