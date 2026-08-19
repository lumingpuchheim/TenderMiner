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
| figures | lots per month · median award · rough €/year in scope · 0/1-bid share **with its denominator** · bidder distribution. Not the "closed without award" share: to a reader it means the same as "kein Angebot", and the difference is too small and too subtle to explain on the page (operator decision 2026-08-15) | `market.py trade` |
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
| output | `<data-dir>/public/current/` — the source copied in, plus `gewerke/` and `sitemap.xml` | gitignored, on the mounted volume, rebuilt on every deploy and every Monday |

`trade_pages.publish()` copies the hand-written half; `build()` adds the
generated half. The edge serves **`<data-dir>/public/current/`**, never
`site/`. The sitemap is generated rather than committed because it is the one
file that has to know both halves.

**How a rebuild reaches the visitor (operator decisions, 2026-08-15).**
`current` is a symlink to the one complete build beside it
(`public/site-XXXX/`). `trade_pages.release` writes a new build into a fresh
directory, repoints `current` in a single rename, then deletes the directory
it pointed at before — and any half-build a crash left. Nothing is kept: at
rest `public/` holds `current` and one directory. A visitor sees the old site
or the new one, never a half-written or empty one; if the build dies, the old
site keeps serving. The edge bind-mounts `public/` itself and that directory
is never deleted or recreated — the earlier `rmtree(out)` did exactly that,
and a bind mount follows the inode, so the container would have gone on
serving a deleted directory.

Who runs the build: `docker/deploy.sh` after every proved image (so a page
template change is live with the push), and the Monday cycle (so the figures
follow the data). Same function, same output.

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
`rewind_all.py`'s own prose table does, because buyers enter CPV wrongly.

**Where the number comes from.** Only the as-of replay can answer it: a live
award publishes a median 84 days after its tender, so the live grade ledger
holds 18 rows and will for months. The replay is `rewind_all.py`, and how it
hands its results over is settled in §6d below — a JSON document on stdout,
which this program is given the path to:

```
python rewind_all.py > run-2026-08-11.json
python trade_pages.py --replay run-2026-08-11.json
```

Without a document there is no forecast claim and every page says so. That
is the state on a fresh checkout, and it is the correct one.

**The conventional file (2026-08-18).** The deploy and the cycle build the
site without flags, and until then no live page had ever carried the
forecast section — the operator noticed on the Elektroinstallation page
("doesn't mention the lift"). So `trade_pages.py` now looks in one place
when `--replay` is not given: **`<data>/replay/latest.json`**
(`trade_pages.REPLAY_FILE`). Refreshing the number is therefore:

```
python rewind_all.py --out data/replay/latest.json     # ~35 min, laptop store
scp data/replay/latest.json <server>:tm-state/replay/latest.json
```

and the next deploy or Monday cycle prints it on every page, with the
document's own date. `--replay PATH` still overrides it. Nothing but the
operator's replay run writes that file.

**Run the replay once, slice it many times.** The document is the reason the
replay is worth its half hour: it used to keep per-lot facts in memory and
print only prose, which made every new question ("how does the forecast do in
*this* trade?") a fresh 33-minute run.

**Not in the weekly cycle**, and it must not be: the replay retrains the
champion at every cutoff. Refresh it when the model changes or quarterly; the
page prints the document's own date, so a figure ages visibly instead of
silently. It *could* run weekly — measured 2026-08-11 at 33 minutes for 46
trained cutoffs, not the "hours" this file used to claim — but there is no
reason to spend that when the figure moves quarterly at best.

**Three states, and the page always says which one it is in:**

| state | what the page says |
| --- | --- |
| no `--replay` (or an unreadable one) | „…noch nicht genug ausgewertete Hinweise… behaupten wir dazu nichts" |
| fewer than `SMALL_SAMPLE` checked alarms | names the count and refuses to quote a rate |
| enough, and it beats the base rate | both denominators, the factor, and the recall line — **and a fifth figure tile at the top, „1,2-fach — so oft trifft unser Hinweis, verglichen mit Zufall"** (2026-08-18: „show the level when our prediction is better than guessing") |
| enough, and it does **not** | **says so plainly**; no tile |

The same verdict, per trade, goes to `<data>/trade_forecast.json` for the
operator page and the invitation message (`trade_pages.level`,
`trade_pages.forecasts`; ADMIN.md 3b). First real replay, 2026-08-18 over the
2026-08-10 store (46 cutoffs, 1 042 checked alarms, 18 % vs 9 % overall):
9 of 32 pages have ≥30 checked alarms; 7 of those beat chance (Elektro
1,2×, Heizung 1,5×, Lüftung 1,6×, Gleisbau 1,2×, Tischler 1,2×,
Wärmedämmung 1,1×, Stahlbau 1,0×), Aufzüge and Fenster do not; the other
23 pages say „zu wenige". The per-trade slices are thin — 5 of 43 is what
Elektro's 12 % rests on — and the page prints the count so a reader can
weigh it.

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

**The denominator is the trade's own rate — the tile — never the replay
pool's (operator, 2026-08-19).** `flag_stats` scores a flag against the pool
it was raised in: the lots the replay scored, i.e. open for a week or more at
a weekly cutoff. That pool is more contested than the trade (Heizung, server
store: 7 % there, 10 % in the trade), so the page printed "10 % in the tile,
7 % without our forecast, 1,5-fach" — a product scoring itself against a
smaller number than the one it shows three screens up. A reader who can
divide sees it; the operator did ("self-cheating and then customer-cheating
are not acceptable"). Now `market.low_bid_rate` is the one rate per trade:
it is the tile, and `trade_pages.against` replaces `flag_stats`' base with it
for the sentence, the verdict, the factor, the fifth tile, the overall line
(store-wide rate) and `trade_forecast.json` — so the operator page and the
invitation message carry the same verdict as the public page. The pool's own
rate stays in the verdict as `pool_base` for the operator. Recall stays the
pool's (it needs to know which lonely lots we did *not* flag, known only for
scored lots) and the sentence names that pool instead of "all lots of the
trade". **Precision and base can never be over the same lots** — one is "of
our flags", the other "of the trade"; that difference *is* the lift. What
this fixes is that the base is now the number the reader sees.

The live measurement, once the grade ledger is deep enough to carry it, gets
the same treatment: precision from the grades, base from the market, and the
switch from replay to live moves nothing but the precision source.

**A lift that prints as „1,0-fach" is no advantage** (`MIN_FACTOR` = 1.05):
against the trade's rate Heizung is 10,3 % vs 9,9 %, strictly above and
visibly nothing. The page says "nicht besser", no tile, no message. A display
floor, not a significance test — the count stands beside every rate.

*Receipt:* proven end-to-end against a synthetic replay over the real 6,685
resolved lots (a rule, not a model — plumbing only, and written to the scratch
copy so it could never be mistaken for a measurement). 22 of 32 trades landed
in "too few checked", 10 in "beats chance", which is the shape to expect: **on
real data most trades will not have enough checked alarms to say anything.**

## 6d. How `rewind_all.py` hands its results over — decided 2026-08-11

**The rule: `rewind_all.py` produces data, and every readable form is a renderer
over that data.** Implement the replay once; extend the display as often as
you like. Operator's decision, and it is the reason this section exists rather
than a paragraph in a commit message.

Concretely:

- **One output: a JSON document on stdout.** The program writes no file and
  owns no path — no `RECEIPT_NAME`, no `data/reports/` convention, no dated
  filename. The operator names the file (`> run-2026-08-11.json`) or pipes it.
- **Progress goes to stderr**, so the stream is clean whichever you do.
- **The prose is a renderer, not a side effect.** `rewind_all.py --render PATH`
  (or `-` for stdin) prints the operator's report from a document, in a
  second, without replaying. `report()` is a pure function of the payload.
- **`trade_pages.py --replay PATH`** slices the same document by trade into
  HTML. No argument, no forecast claim.

**What the document carries.** Everything any renderer needs, so that no
consumer ever re-derives a fact the replay already knew: per-lot rows
(`procedure_id`, `lot_id`, `cpv3`, `flag`, `n_tenders`), per-subscription
picks and own-win rows, the per-firm target rows when `--targets` was given,
and run metadata (`generated`, `model_tag`, `step_days`, `cutoffs_trained`).
`n_tenders` is `null` when no award has published yet, which is how one list
carries both the "examined" and the "results known" denominators.

**`cpv3` is in a lot row; `trade` deliberately is not.** CPV3 is a raw store
field and cannot drift. A trade is a title word-match that `trades.txt`
redefines, so a document carrying trades would silently disagree with
`trades.txt` the day it changed. Consumers that group by trade join to the
store themselves — which is exactly what `trade_pages.py` does, and why the
public slice is by title words while the backtest's own table is by CPV3.

**Rejected alternatives**, recorded so they are not re-proposed:

- *A default-named file* (`data/reports/backtest_lots.json`, what this was
  until 2026-08-11). Rejected by the operator: the program should not own a
  path. It also invited the failure this file used to have — a dated prose
  `.md` beside it that nothing read and that could go stale against the data.
- *A pipe as the only channel* (`rewind_all.py | trade_pages.py`). Rejected on
  lifetimes: the producer costs 33 minutes and refreshes quarterly, the site
  rebuilds far more often. A live pipe would re-replay on every rebuild. The
  document is what lets the two cadences differ; `-` still gives the pipe to
  anyone who wants it.
- *A table in `tendermining.db`.* Rejected for scope, not taste: a replay is a
  research artifact stamped with a model tag and a step size, not a cycle
  record, and putting it in the database invites the weekly cycle to depend on
  a run this expensive — which §6c forbids. Revisit if cross-run queries ever
  matter; loading the JSONs is easy.

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
