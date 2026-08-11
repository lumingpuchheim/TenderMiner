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

Measured against the live store (2026-08-11, 12 covered months): **28 of 40
trades clear it.** The twelve that do not — Modulbau (2 awarded lots),
Zaunbau (3), Wasser- und Abwassertechnik (5), Brückenbau (6), Schließanlagen
(7), Kanalbau (8), Feuerlöschanlagen (10), Schwimmbad (11), Industrieboden
(15), Möbel (16), Reinigung (17), Sicherheits- und Meldeanlagen (21) — get no
page until the data arrives. The floor is re-checked at every build, so a
trade appears on its own the month it qualifies.

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

**[CLARIFY]** whether the generated pages are committed to git (a visible
diff each week, and the site remains uploadable from a clean checkout) or left
as a build artifact (no weekly noise, but the upload must run after a build).
Recommendation: **committed**, because it keeps "upload the `site/` folder"
true, and because a diff is how you notice a number moving in a way it should
not.

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
