# TRADE_PAGES — market pages per trade, for search

Written 2026-08-11, operator's proposal. Specification only; nothing here is
built yet. Companions: [`LAUNCH.md`](LAUNCH.md) §4 (the public/personal rule
and why the main page carries no figures), [`ONBOARDING.md`](ONBOARDING.md) §0
(the funnel these pages feed), `market.py` (every number below already exists
there).

## 1. What this is, and the one job it has

One page per trade — `www.<domain>/gewerke/maler` — carrying the market
figures for that trade and nothing else. Their job is **to be found in Google
by a contractor searching for his own trade**, and to be worth his time when
he arrives.

They are not a browsing surface. A Maler never sees anything about Elektro:
the pages do not link to each other, and the main page does not advertise
them.

**Why they can work at all:** these figures exist nowhere else. Competitors
sell access to tenders; nobody publishes how big a trade's public market is,
what a lot is worth, or how often almost nobody bids. Unique data is the only
SEO asset that cannot be out-spent.

**What they are not for:** the launch. A new domain needs six to twelve months
to rank for anything contested, and the head terms belong to DTAD and ibau.
The letters are the launch (`ONBOARDING.md`); this is a slow channel that
should be built cheaply and then left alone.

## 2. What is on a page

Exactly this, in this order. No filler — a short page of true figures ranks
safely, a padded one does not (§5).

| block | content | source |
| --- | --- | --- |
| headline | „Öffentliche Ausschreibungen für **<Gewerk>**" | — |
| lede | one sentence: what this page is (market figures for this trade, from the official EU procurement record) | — |
| figures | lots per month · median award · rough €/year in scope · 0/1-bid share **with its denominator** · share closed without any award | `market.py trade` |
| method | 3–4 sentences: source is TED, a trade is matched on the lot title by a maintained word list, only lots with a published award count toward the bid share, results appear ~90 days after the deadline so recent months are excluded | — |
| freshness | „Stand: <Monat> <Jahr>, berechnet über <n> vollständig erfasste Monate" | `market.coverage` |
| CTA | the same offer as the main page: send your firm name, we look up what you won and what matched this week | `site/index.html` |

**No forecast language** unless the trade is CPV 452, the only trade with
measured lift (`LAUNCH.md` §4.1 claim rules). Simplest and safest: leave the
forecast off these pages entirely and keep it for the letter and the app.

**No lot listings, no award listings, no firm names.** A published award with
its bid count is fact, but printed beside our forecast a reader cannot tell
the two apart — the reason it came off the main page, and the same reason
applies here.

## 3. Which trades get a page

**The floor is `market.SMALL_SAMPLE` = 30 awarded lots in mature months** —
the project's existing line for "below this, a share is indicative, not a
rate". A page whose headline figure cannot honestly be quoted should not
exist.

Measured by the generator against the live store (2026-08-11, 12 covered
months): **32 of 47 trades clear it.** The fifteen that do not — Sportstätten
(27 awarded lots), Küchen-/Bühnentechnik (26), Photovoltaik (22),
Sicherheits- und Meldeanlagen (21), Reinigung (17), Möbel (16),
Industrieboden (15), Schwimmbad (11), Feuerlöschanlagen (10), Kanalbau (8),
Schließanlagen (7), Brückenbau (6), Wasser- und Abwassertechnik (5), Zaunbau
(3), Modulbau (2) — get no page until the data arrives. The floor is
re-checked at every build, so a trade appears on its own the month it
qualifies, and disappears again if it stops qualifying.

`python trade_pages.py --dry-run` prints exactly this list without writing
anything.

**Where the trade list comes from.** Today `trades.txt`, hand-owned, one
word list per trade. The operator's note (2026-08-11) is that an agent can
derive trades from the datasets instead — worth doing when the list starts
costing maintenance, and it changes nothing here: this spec needs *a* list of
trades with a way to select their lots, not a particular way of producing it.
Whatever produces it, the same floor applies.

## 4. Linking

Three links, and no more:

- **Main page → an index**, as one item in the footer line
  („Marktzahlen nach Gewerk"). A visitor reading the page does not notice a
  footer link; a crawler always does. This is the only reason to link at all:
  Google finds pages through links, and a page nothing links to is crawled
  rarely and ranks worse — building the asset and then hiding it from the
  mechanism that makes it work.
- **Index → each trade page.** A plain list, nothing else on it.
- **Each trade page → the main page**, in its footer.

**Never trade page → trade page.** That is the rule the whole design exists to
protect.

## 5. Staying inside Google's rules

The pattern Google demotes is the *doorway page*: many near-identical pages
made to catch searches and funnel people onward, worthless in themselves. The
test is whether a page has value on its own or whether the visitor must click
further to get anything.

Four properties keep these pages on the right side, and each is a build rule
rather than an intention:

1. **The content genuinely differs.** Different figures computed from real
   awards, not one text with the trade name swapped. Painting and
   bridge-building do not resemble each other on these numbers.
2. **The page answers the query it ranks for.** Someone searching
   „Ausschreibungen Maler" can read it, learn something true and unavailable
   elsewhere, and leave without contacting us. That is exactly the property a
   doorway page lacks.
3. **No mass permutation.** ~28 pages, one per trade with real data. The
   dangerous version is trade × Bundesland — 16 × 28 = 448 near-identical
   pages, which is what gets a whole domain demoted. **Rejected**, and the
   earlier implementation of it was removed (`LAUNCH.md` §4.1).
4. **No padding.** No text written to make a page look substantial. If a trade
   has four true numbers, the page is four numbers long.

## 6. Freshness, and why this one needs a build step

The figures move every week and there are 28 of them: no one maintains that by
hand, and a market page carrying last quarter's numbers is worse than no page.

So the rule the operator set on 2026-08-11 splits cleanly:
**hand-write what has no data in it, generate what does.** `site/index.html`
and the legal pages stay files you edit. These pages are generated from the
store into `site/gewerke/` by a small program run from the cycle — the same
place `render_dashboard` runs, non-fatal in the same way.

**DECIDED (operator, 2026-08-11): not committed.** The generated pages were
briefly committed and that was wrong for a reason beyond the weekly diff
noise: `site/` is inside the code checkout, which in the container is `/app` —
the image. A build writing there is discarded when the container exits, and
fails outright under the read-only root filesystem the cycle is proven to run
with (STORAGE.md 6.5).

So the split is by **source vs. output**, not by hand-written vs. generated:

| | what | where |
| --- | --- | --- |
| source | `site/` — landing page, legal pages, `style.css`, `robots.txt` | committed, hand-edited |
| output | `<data-dir>/public/` — the source copied in, plus `gewerke/` and `sitemap.xml` | gitignored, on the mounted volume, rebuilt weekly |

`trade_pages.publish()` copies the hand-written half; `build()` adds the
generated half. **Upload `<data-dir>/public/`**, never `site/`. The sitemap is
generated rather than committed because it is the one file that has to know
both halves.

*Receipt (2026-08-11):* built inside a container with `--read-only --tmpfs
/tmp` against the mounted volume — 32 trade pages plus the five hand-written
files landed in `/data/public/`, and nothing was written to the image.

## 6b. Built — 2026-08-11

[`trade_pages.py`](../trade_pages.py), run from the cycle, output committed in
`site/gewerke/`. 32 pages; the 15 trades below the floor are named at every
run so the gap is visible rather than silent.

Every figure comes from `market.py`'s own loader, coverage rule and
`SMALL_SAMPLE` — the public number and the operator's number cannot drift
apart, because there is only one of them.

Two things the build got wrong first, both now tested:

- **Money was printed in the console's format** — `204 k €`, `34.08 M €`.
  Right for a terminal, wrong for a German page, which wants `204.075 €` and
  `34,1 Mio. €`. Note the naive fix is also wrong: swapping `,`→`.` and then
  `.`→`,` turns the thousands separator into a decimal comma and yields
  `34,1 Mio,`. Both spellings are asserted.
- **Relative depth.** These pages sit two levels down, so the stylesheet is
  `../../style.css`. A test now walks *every* HTML file under `site/` and
  resolves every `href` and `src` against the file naming it — a generator
  with the wrong depth breaks 32 pages at once, and the earlier absolute-path
  bug proved this is not hypothetical.

Also enforced by test, not by intention: no page links sideways to another
trade, every page links up to the index and home, every page states its
denominator and its date, names TED and the ~3-month award lag, carries no
forecast language, no firm name and no lot listing.

## 6c. The forecast section — built 2026-08-11

Each page answers "wie gut trifft unsere Einschätzung?" for its own trade.
This is the **forecast** precision/recall of [`METHODS.md`](METHODS.md) §0 —
did a lot we flagged really end with 0-1 bids — never the gate's, and it is
sliced **by title word-match like everything else**, not by CPV3 as
`backtest.py`'s own prose table does, because buyers enter CPV wrongly.

**Where the number comes from.** Only the as-of replay can answer it: a live
award publishes a median 84 days after its tender, so the live grade ledger
holds 18 rows and will for months. `backtest.py` now writes
`data/reports/backtest_lots.json` — three facts per checkable lot (examined,
flagged, final bid count) and nothing else. Deliberately no title, trade or
CPV in it: whoever slices joins to the store, so `trades.txt` can change
without invalidating a receipt.

That receipt is the reason the replay is worth its hours. It used to keep
per-lot facts in memory and write only prose, which made every new question a
fresh multi-hour run. **Run the replay once, slice it many times.**

**Not in the weekly cycle**, and it must not be: the replay retrains the
champion at every cutoff. Refresh it when the model changes or quarterly; the
page prints the receipt's own date, so a figure ages visibly instead of
silently.

**Three states, and the page always says which one it is in:**

| state | what the page says |
| --- | --- |
| no receipt | „…noch nicht genug ausgewertete Hinweise… behaupten wir dazu nichts" |
| fewer than `SMALL_SAMPLE` checked alarms | names the count and refuses to quote a rate |
| enough, and it beats the base rate | both denominators, the factor, and the recall line |
| enough, and it does **not** | **says so plainly** |

The last row is the operator's call (2026-08-11), and it is the one worth
defending: a page that shows the market and then admits the forecast is not
beating chance in this trade is auditable, and a silently missing section is
not. It also says something true — there the value is the coverage, not the
forecast. The recall line always accompanies a quoted precision, because
precision alone cannot be wrong in an interesting way: flag one lot a year,
get it right, claim 100 %.

`loop.flag_stats` computes it — the same function the weekly report and the
backtest use, so the replayed number and the live number stay one statistic
rather than two implementations that agree by coincidence.

*Receipt:* proven end-to-end against a synthetic replay over the real 6,685
resolved lots (a rule, not a model — plumbing only, and written to the scratch
copy so it could never be mistaken for a measurement). 22 of 32 trades landed
in "too few checked", 10 in "beats chance", which is the shape to expect: **on
real data most trades will not have enough checked alarms to say anything.**

## 7. Out of scope

No Bundesland or city pages (§5.3). No lot or award listings. No firm names.
No blog, no guides, no glossary — every one of those is a page nobody
searches for and nobody maintains. The single-bidder report of `LAUNCH.md`
§4.1 is a separate asset and not part of this.

## 8. What would tell us it worked

One number, checked in Search Console once the pages have been live a
quarter: **impressions on trade-name queries**, and whether any page reaches
the first two result pages. If after six months no trade page ranks, the
channel is wrong and the pages should be deleted rather than expanded — the
same rule `LAUNCH.md` §4.1 applies to new public sections: query evidence,
not a story about what contractors probably google.
