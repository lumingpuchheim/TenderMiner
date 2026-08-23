"""TenderMining market view — the business-developer's three questions.

    python market.py next                      THE decision: which market first,
                                               who is contactable there, and
                                               what to charge them
    python market.py pitch "Jebsen"            one firm's letter numbers
    python market.py trade  "Blitzschutz"      what is this market worth, and
                                               how contested is it?
    python market.py firms  "Blitzschutz"      who wins it — who do I write to?
    python market.py rank                      browse all trades, one sort key
    python market.py suggest "Blitzschutz"     what words is this trade missing?
    python market.py trades                    what the trade list claims

Prints to stdout. Writes nothing, anywhere — see doc/GO_TO_MARKET.md for the
play these numbers feed and `outreach.py` for the contact list they hand off
to.

WHY A TRADE IS FOUND BY WORDS, NOT BY CPV
A CPV filter would be one line and would be wrong. CPV names one code per
trade and mixes two axes — what is BUILT versus what WORK is done (the
argument is written out in `cpv_trade_roots.txt`) — so `45312310`
(Blitzschutz) misses every Blitzschutz lot a buyer filed under `45311200`,
`45310000` or plain `45000000`, which in this store is a third of them, while
a whole school refurbishment filed under `45312310` walks in. The title is
the buyer's own description of the work; the code is a clerk's filing
decision. So trades are defined in `trades.txt`, by words, and matched as
substrings against the folded notice text (`evidence.fold`, so Fussboden and
Fußboden are one word).

Nothing here reads a CPV column, and nothing assumes one. Today's store
happens to hold only division 45; `store_profile()` reports what is actually
in it rather than asserting what ought to be.

TWO SCOPES, BECAUSE THEY ARE TWO MARKETS
  core       the TITLE names the trade — the lot IS this work, biddable as a
             main contractor. The addressable market, and the default.
  mentioned  only the body names it — a line item inside a bigger package. A
             subcontract lead, not a tender you win.

WHAT EVERY NUMBER HERE IS HONEST ABOUT
* **Coverage.** The store is not a continuous archive; whole months are
  missing where a download never ran. A per-month rate over calendar months
  would divide by months holding no data, so only months clearing
  `--coverage-floor` of the busiest month count, and the skipped ones are
  printed by name.
* **Censoring.** An award publishes a median ~83 days after the call, so
  recent lots have no result and the ones that do resolve early are the
  simple ones. The 0/1-bidder share is therefore given twice: over every
  resolved lot, and over the "mature" months whose store-wide resolution rate
  clears `--mature-floor`. The second is the one to quote.
* **Value.** `est_value_lot` is filled on 8% of lots — too thin to average.
  The headline is what the winner was actually paid (the winning bids on the
  award notice, ~74% filled where there is an award); the estimate sits
  beside it with its own denominator.
* **Sample size.** Every rate prints what it is out of. Below
  `SMALL_SAMPLE` lots it says "indicative" instead of pretending to be a rate.
* **Firm identity is the registration number on the award notice** (firms.py),
  not the name a clerk typed. `SVA GmbH` and `SVA System Vertrieb Alexander
  GmbH` share a VAT number and are one company; `Siemens AG` and `Siemens
  Healthineers AG` do not and are two. A group filing under one VAT number
  (Bechtle) therefore arrives as one company, parent and subsidiaries
  together — what the notices say, listed for a person in the receipt.
  Where no usable number exists, an identical name with nothing contradicting
  it still merges, and the postcode may support a merge but never block one. `firms` still PRINTS one row per spelling, because a
  person reading it wants to see what buyers actually typed; the numbers a
  letter quotes are the company's.
"""

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

import config
import evidence as ev

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

TRADES_FILE = Path(__file__).with_name('trades.txt')
MIN_TERM_LEN = 5        # shorter substrings hit inside unrelated compounds
SMALL_SAMPLE = 30       # below this, a share is indicative, not a rate
COVERAGE_FLOOR = 0.25   # a month is covered at >= this share of the busiest
MATURE_FLOOR = 0.35     # ... and mature at >= this store-wide resolution rate
TOP_N = 12
VALUE_BANDS = [(0, 100_000), (100_000, 250_000),
               (250_000, 1_000_000), (1_000_000, float('inf'))]
SMALL_SIZES = ('small', 'micro')

# --- the economics behind `next` and `pitch` ---------------------------
# Everything below the line is DERIVED from the store. The three constants
# above it are GUESSES, printed as such in every output that uses them, and
# they stay guesses until a customer answers the three questions in
# GO_TO_MARKET.md (hours spent reading portals, cost of one bid, hit rate).
# Change them here, in one place, when an answer arrives.
WIN_UPLIFT = 0.05        # GUESS: flagged uncontested lots a firm converts
MARGIN = 0.08            # GUESS: contractor margin on contract value
PRICE_SHARE = 0.20       # GUESS: the share of created value we charge
FLAT_FLOOR = 75.0        # EUR/month — the GO_TO_MARKET.md price, the minimum
PCT_DEAL_MIN = 500_000   # median award above this: pitch a % of the deal,
                         # because a flat fee looks absurd next to one win
MIN_RECS_YEAR = 12       # a contactable firm must be owed >= one pick a
                         # month from their own region — the "we will
                         # definitely recommend you tenders" guarantee
MIN_PROSPECT_WINS = 2    # ... and have proven twice that they win tenders

TENDER_COLS = ['procedure_id', 'lot_id', 'notice_version', 'publication_date',
               'title', 'description', 'est_value_lot', 'est_value_procedure',
               'place_nuts3', 'buyer_name', 'procedure_type', 'is_framework',
               'n_lots', 'deadline_days', 'bid_bond_required', 'cpv_main',
               'n_selection_criteria', 'publication_number']
AWARD_COLS = ['procedure_id', 'lot_id', 'publication_date', 'n_tenders',
              'result_code', 'winner_names', 'winner_size', 'winning_bids']


# ------------------------------------------------------------- trade list

def load_trades(path=TRADES_FILE):
    """trades.txt -> {name: {'terms': [...], 'exclude': [...]}}, folded.

    Owned by a person (see the file's own header). Parsing is deliberately
    dumb: a line is a word, "-" makes it an exclusion, "=" starts a trade.
    A word too short to be a safe substring is a hard error, not a warning —
    it would quietly contaminate every number downstream."""
    trades, name, bad = {}, None, []
    for lineno, raw in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        line = raw.split('#')[0].strip()
        if not line:
            continue
        if line.startswith('='):
            name = line.lstrip('= ').strip()
            trades[name] = {'terms': [], 'exclude': []}
        elif name is None:
            bad.append(f'{path.name}:{lineno}: word before any "=" trade header')
        else:
            key = 'exclude' if line.startswith('-') else 'terms'
            word = ev.fold(line.lstrip('-').strip().casefold())
            if len(word) < MIN_TERM_LEN:
                bad.append(f'{path.name}:{lineno}: "{word}" is under '
                           f'{MIN_TERM_LEN} characters')
            trades[name][key].append(word)
    empty = [n for n, t in trades.items() if not t['terms']]
    bad += [f'{path.name}: trade "{n}" has no match words' for n in empty]
    if bad:
        raise SystemExit('trade list rejected:\n  ' + '\n  '.join(bad))
    return trades


def resolve(trades, wanted):
    """A trade name the way a person types it: exact, else unique
    case-insensitive substring, else treat it as an ad-hoc word so
    `market.py trade Blitzschutz` works before anyone edits trades.txt."""
    if wanted in trades:
        return wanted, trades[wanted]
    hits = [n for n in trades if wanted.casefold() in n.casefold()]
    if len(hits) == 1:
        return hits[0], trades[hits[0]]
    if len(hits) > 1:
        raise SystemExit(f'"{wanted}" matches {len(hits)} trades: '
                         + ', '.join(hits) + ' — name one exactly')
    word = ev.fold(wanted.casefold())
    if len(word) < MIN_TERM_LEN:
        raise SystemExit(f'"{wanted}" is not in {TRADES_FILE.name} and is too '
                         f'short ({MIN_TERM_LEN} characters minimum) to use as '
                         f'a match word on its own')
    return (f'{wanted} (ad-hoc — not in {TRADES_FILE.name})',
            {'terms': [word], 'exclude': []})


# --------------------------------------------------------------- loading

def load_lots(data_dir):
    """One row per (procedure, lot): the newest notice version's fields, but
    the FIRST publication date — a corrigendum re-dates the notice, not the
    moment the work reached the market — joined to its award if one exists."""
    store = Path(data_dir) / 'store'
    t = pd.read_parquet(store / 'tenders.parquet', columns=TENDER_COLS)
    first_pub = t.groupby(['procedure_id', 'lot_id'])['publication_date'].min()
    lots = (t.sort_values(['notice_version', 'publication_date'])
             .drop_duplicates(['procedure_id', 'lot_id'], keep='last')
             .set_index(['procedure_id', 'lot_id']))
    lots['publication_date'] = first_pub
    lots = lots.reset_index()
    lots['month'] = pd.PeriodIndex(pd.to_datetime(lots.publication_date),
                                   freq='M')

    a = pd.read_parquet(store / 'awards.parquet', columns=AWARD_COLS)
    a = (a.sort_values('publication_date')
          .drop_duplicates(['procedure_id', 'lot_id'], keep='last'))
    a['award_value'] = a.winning_bids.map(bid_sum)
    a = a.rename(columns={'publication_date': 'award_date'})
    lots = lots.merge(a.drop(columns=['winning_bids']),
                      on=['procedure_id', 'lot_id'], how='left')
    lots['resolved'] = lots.n_tenders.notna()
    lots['nuts2'] = lots.place_nuts3.fillna('').str[:3].replace('', '?')
    return lots


def bid_sum(bids):
    """What the winner(s) were actually paid on this lot, in EUR. Other
    currencies are dropped rather than converted — there is no rate table
    here, and a silently mixed unit is worse than a missing value."""
    if bids is None or len(bids) == 0:
        return None
    amounts = [b['amount'] for b in bids
               if b is not None and b.get('amount') is not None
               and (b.get('currency') or 'EUR') == 'EUR']
    total = float(np.sum(amounts)) if amounts else None
    # 441 award rows carry 0.00 — a withheld figure, not free work. Averaging
    # them in would drag every median down.
    return total if total else None


def add_text(lots):
    """The matchable text, folded once: the title, and title + Leistung body."""
    lots['t_title'] = [ev.fold(str(x or '').casefold()) for x in lots.title]
    lots['t_full'] = [ev.fold(ev.leistung_text(a, b))
                      for a, b in zip(lots.title, lots.description)]
    return lots


def store_profile(lots):
    """What this store actually holds — asserted by counting, not assumed.
    The CPV divisions present are reported so a reader knows the scope of
    every number below; no filter is applied on them."""
    div = Counter(str(c)[:2] for c in lots.cpv_main.dropna())
    span = f'{lots.publication_date.min()} to {lots.publication_date.max()}'
    parts = ', '.join(f'CPV {d} {n:,}' for d, n in div.most_common(6))
    return f'{len(lots):,} lots, published {span} ({parts})'


# --------------------------------------------------------------- coverage

def coverage(lots, floor=COVERAGE_FLOOR, mature=MATURE_FLOOR):
    """Which publication months does the store really hold, and which of them
    can carry a competition rate?

    A month counts as covered when its STORE-WIDE lot count clears `floor` of
    the busiest month: the store is assembled from downloaded packages, and a
    package that never landed leaves a month that looks like a quiet market
    instead of like absent data. Mature adds that enough of the month's lots
    have an award published to quote a bidder count from them."""
    per = lots.groupby('month').agg(lots=('lot_id', 'size'),
                                    resolved=('resolved', 'sum'))
    per['resolution'] = per.resolved / per.lots
    per['covered'] = per.lots >= floor * per.lots.max()
    per['mature'] = per.covered & (per.resolution >= mature)
    return list(per.index[per.covered]), list(per.index[per.mature]), per


# How many of the newest mature months carry the public bidder figures.
# The archive now reaches into 2023, and the 2023/24 market was much
# lonelier than today's — a rate over ALL mature months describes a market
# no reader is bidding into (operator, 2026-08-20: "when a market becomes
# more competitive, we may underestimate"). Twelve keeps a full year of
# seasonality; the trade pages quote every bidder number over this window
# and say so. The operator console keeps printing the full set.
RECENT_MONTHS = 12


def recent_mature(mature, n=RECENT_MONTHS):
    """The newest `n` of the mature months — the window every public bidder
    figure is computed over. All of them when there are fewer."""
    return list(mature)[-n:]


def low_bid_rate(lots, mature, sel=None):
    """The share of awarded lots in mature months that ended with 0 or 1
    bids — over the whole store, or over `sel` (a boolean mask: one trade).
    -> (rate, n_awarded); (None, 0) when nothing is awarded.

    This is THE denominator (operator, 2026-08-19): the figure a trade page
    prints as its tile, and the base every forecast claim is measured
    against. The replay scores its own pool of lots — those open for a week
    or more at a cutoff — and that pool is more contested than the trade
    (Heizung: 7 % there against 10 % here), so a lift quoted against it
    flatters the forecast. There is one rate per trade, it is this one, and
    a reader can check any claim on the page against the tile."""
    sub = lots if sel is None else lots[sel]
    mat = sub[sub.month.isin(mature) & sub.resolved]
    if not len(mat):
        return None, 0
    return float((mat.n_tenders <= 1).mean()), int(len(mat))


# --------------------------------------------------------------- matching

def match(lots, trade, scope='core'):
    """-> boolean Series of the lots in this trade, under this scope.

    One regex per trade rather than one pass per word: 40 trades x 600 words
    over 23k lots is otherwise a minute of substring scans."""
    pat = '|'.join(re.escape(t) for t in trade['terms'])
    keep = pd.Series(True, index=lots.index)
    if trade['exclude']:
        ex = '|'.join(re.escape(t) for t in trade['exclude'])
        keep = ~lots.t_full.str.contains(ex, regex=True)
    core = lots.t_title.str.contains(pat, regex=True) & keep
    if scope == 'core':
        return core
    body = lots.t_full.str.contains(pat, regex=True) & keep
    return body if scope == 'both' else body & ~core


def term_hits(lots, trade):
    """Per word: the lots it pulled in by title. The operator's check that one
    loose word is not carrying the whole trade."""
    return Counter({t: int(lots.t_title.str.contains(t, regex=False).sum())
                    for t in trade['terms']})


# --------------------------------------------------------------- formatting

def money(x):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return '—'
    if x >= 1_000_000:
        return f'{x / 1e6:.2f} M'
    if x >= 10_000:
        return f'{x / 1e3:.0f} k'
    if x >= 1_000:
        return f'{x / 1e3:.1f} k'
    return f'{x:.0f}'      # a four-figure award is real; "0 k" is not a number


def pct(part, whole):
    """Percent, with a decimal only where rounding to a whole one would
    print "1%" for everything from 0.5% to 1.4% — a trade's share of the
    store lives in exactly that range."""
    if not whole:
        return '—'
    v = part / whole
    return f'{v:.1%}' if 0 < v < 0.05 else f'{v:.0%}'


def table(rows, headers):
    """Markdown table, sized to the console rather than to a browser."""
    if not rows:
        return '  (nothing to show)\n'
    keys = list(headers)
    width = {k: max(len(headers[k]), *(len(str(r.get(k, ''))) for r in rows))
             for k in keys}
    out = ['  ' + '  '.join(headers[k].ljust(width[k]) for k in keys),
           '  ' + '  '.join('-' * width[k] for k in keys)]
    for r in rows:
        out.append('  ' + '  '.join(str(r.get(k, '')).ljust(width[k])
                                    for k in keys))
    return '\n'.join(out) + '\n'


def months_str(months):
    return ', '.join(str(m) for m in months) if len(months) else 'none'


def low_bid_line(sub, label):
    """The 0/1-bidder share with its denominator — or a refusal to quote one."""
    res = sub[sub.resolved]
    n = len(res)
    if n == 0:
        return f'{label}: no awarded lot yet'
    zero, one = int((res.n_tenders == 0).sum()), int((res.n_tenders == 1).sum())
    tail = '  <- small sample, indicative only' if n < SMALL_SAMPLE else ''
    return (f'{label}: {pct(zero + one, n)} of {n} awarded lots '
            f'({zero} with no bid, {one} with one), '
            f'median {res.n_tenders.median():.0f} bidders{tail}')


# --------------------------------------------------------------- trade page

def cmd_trade(lots, trades, args):
    name, trade = resolve(trades, args.trade)
    covered, mature, _ = coverage(lots)
    sel = match(lots, trade, args.scope)
    sub = lots[sel].copy()

    print(f'# {name}')
    print(f'store: {store_profile(lots)}')
    print(f'match: {len(trade["terms"])} words, scope "{args.scope}"'
          + (f', excluding {", ".join(trade["exclude"])}'
             if trade['exclude'] else ''))
    core_n = int(match(lots, trade, 'core').sum())
    ment_n = int(match(lots, trade, 'mentioned').sum())
    print(f'  title names the trade (biddable):        {core_n}')
    print(f'  only the body does (subcontract lead):   {ment_n}')
    hits = term_hits(lots, trade)
    print('  words that pulled their weight: '
          + ', '.join(f'{w} {n}' for w, n in hits.most_common(8) if n))
    dead = [w for w, n in hits.items() if n == 0]
    if dead:
        print(f'  words matching nothing by title ({len(dead)}): '
              + ', '.join(dead[:12]))

    if args.region:
        sub = sub[sub.place_nuts3.fillna('').str.startswith(tuple(args.region))]
        print(f'  region {", ".join(args.region)}: {len(sub)} lots')
    if args.since:
        sub = sub[sub.month >= pd.Period(args.since, freq='M')]
        print(f'  from {args.since}: {len(sub)} lots')
    print()

    if sub.empty:
        print('No lot matches. Try `market.py suggest` for words this trade '
              'may be missing, or a shorter root of the name '
              '(Blitzschutzanlage -> blitzschutz).')
        return

    print('## Store coverage the rates are computed over')
    print(f'  covered months ({len(covered)}): {months_str(covered)}')
    print(f'  mature enough for a bidder rate ({len(mature)}): '
          f'{months_str(mature)}')
    print(f'  months skipped as download gaps or as the running month: '
          f'{len(lots.month.unique()) - len(covered)}')
    print()

    cov = sub[sub.month.isin(covered)]
    all_cov = lots[lots.month.isin(covered)]
    n_months = len(covered)
    print('## How much work comes to market')
    print(f'  {len(cov) / n_months:5.1f} lots per month  '
          f'({len(cov)} lots over {n_months} covered months)')
    print(f'  {pct(len(cov), len(all_cov)):>5}  of everything in the store '
          f'over the same months ({len(all_cov):,} lots)')
    series = cov.groupby('month').size().reindex(covered, fill_value=0)
    peak = max(series.max(), 1)
    for m, n in series.items():
        print(f'    {m}  {n:4d}  {"#" * int(round(24 * n / peak))}')
    print()

    aw = sub.award_value.dropna()
    est = sub.est_value_lot.dropna()
    per_year = len(cov) / n_months * 12
    print('## What a lot is worth')
    if len(aw):
        print(f'  median award {money(aw.median())} EUR, '
              f'mean {money(aw.mean())} EUR '
              f'({len(aw)} of {len(sub)} lots have a published award sum)')
        print(f'  quartiles: {money(aw.quantile(.25))} / '
              f'{money(aw.median())} / {money(aw.quantile(.75))} EUR')
        print(f'  ~{per_year:.0f} lots a year x {money(aw.median())} median '
              f'= roughly {money(aw.median() * per_year)} EUR a year in scope')
    else:
        print('  no published award sum on any matched lot yet')
    print(f'  published estimate before the call: '
          f'{money(est.median()) if len(est) else "—"} EUR median, on '
          f'{len(est)} lots only — the field is mostly empty, which is why '
          f'the award sum leads')
    print()

    print('## How contested it is')
    print('  ' + low_bid_line(sub, 'all resolved lots      '))
    print('  ' + low_bid_line(sub[sub.month.isin(mature)],
                              'mature months (quote me)'))
    print('  ' + low_bid_line(lots[lots.month.isin(mature)],
                              'whole store, same months'))
    res = sub[sub.resolved]
    if len(res):
        dist = res.n_tenders.value_counts().sort_index()
        print('  bidders: ' + ', '.join(f'{int(k)}->{int(v)}'
                                        for k, v in dist.head(12).items()))
        closed = int((res.result_code == 'clos-nw').sum())
        print(f'  {closed} of {len(res)} resolved lots closed with no award at '
              f'all ({pct(closed, len(res))}) — a procedure that found nobody '
              f'is the strongest thin-competition signal there is')
    print(f'  award published for {int(sub.resolved.sum())} of {len(sub)} '
          f'matched lots ({pct(int(sub.resolved.sum()), len(sub))}); the rest '
          f'are too recent to have a result')
    print()

    print('## Where the thin competition sits')
    print(table(group_rows(sub, sub.award_value.map(band_of), top=6),
                {'key': 'value band (EUR)', 'lots': 'lots', 'value': 'median',
                 'resolved': 'awarded', 'low': '0/1 bids'}))
    print(table(group_rows(sub, sub.nuts2),
                {'key': 'region', 'lots': 'lots', 'value': 'median award',
                 'resolved': 'awarded', 'low': '0/1 bids'}))

    print('## Who buys it')
    print(table(group_rows(sub, sub.buyer_name, top=TOP_N, trim=42),
                {'key': 'buyer', 'lots': 'lots', 'value': 'median award',
                 'resolved': 'awarded', 'low': '0/1 bids'}))
    repeat = sub.buyer_name.value_counts()
    print(f'  {int((repeat > 1).sum())} of {len(repeat)} buyers tendered this '
          f'trade more than once — a repeat buyer is a pipeline, not a one-off')
    print()

    print('## How it is bought')
    for col in ('procedure_type', 'is_framework'):
        vc = sub[col].value_counts(dropna=False).head(4)
        print(f'  {col:16s} ' + ', '.join(f'{k}: {v}' for k, v in vc.items()))
    multi = int((sub.n_lots.fillna(1) > 1).sum())
    print(f'  {"deadline":16s} median {sub.deadline_days.median():.0f} days')
    print(f'  {"bundling":16s} {pct(multi, len(sub))} of lots sit inside a '
          f'multi-lot procedure (median {sub.n_lots.median():.0f} lots)')
    bond = sub.bid_bond_required.eq(True)
    print(f'  {"entry cost":16s} bid bond on {pct(int(bond.sum()), len(sub))}, '
          f'median {sub.n_selection_criteria.median():.0f} selection criteria')
    print()

    print(f'## {min(args.sample, len(sub))} matched lots — read these, they '
          f'are the proof the match is the right trade')
    for _, r in sub.head(args.sample).iterrows():
        bids = '—' if pd.isna(r.n_tenders) else f'{int(r.n_tenders)} bids'
        print(f'  {r.publication_date}  {str(r.title)[:74]:74s}  '
              f'{money(r.award_value):>8}  {bids}')


def band_of(value):
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    for lo, hi in VALUE_BANDS:
        if lo <= value < hi:
            return (f'{money(lo)} - {money(hi)}' if hi < float('inf')
                    else f'{money(lo)} and up')
    return None


def group_rows(sub, by, top=TOP_N, trim=None):
    rows = []
    for key, g in sub.groupby(by, dropna=True):
        res, val = g[g.resolved], g.award_value.dropna()
        rows.append({'key': str(key)[:trim] if trim else key,
                     'lots': len(g),
                     'value': money(val.median()) if len(val) else '—',
                     'resolved': len(res),
                     'low': pct(int((res.n_tenders <= 1).sum()), len(res))
                            + ('' if len(res) >= SMALL_SAMPLE else ' ?')})
    rows.sort(key=lambda r: -r['lots'])
    return rows[:top]


# --------------------------------------------------------------- firms

def firm_rows(lots, sel, customers=()):
    """One row per exact winner-name string among the trade's awarded lots.

    Identity is the string, per SIMULATION.md: "Doser GmbH" and "Doser GmbH &
    Co. KG" stay two rows and are flagged as probably-one-firm, because
    merging them is a judgement about a company, and getting it wrong means
    writing to the wrong address."""
    sub = lots[sel]
    awarded = sub[sub.winner_names.notna()]
    if awarded.empty:
        return [], None
    dates = pd.to_datetime(awarded.award_date)
    split = dates.median()
    trade_regions = sub.nuts2.nunique()

    acc = {}
    for (_, r), when in zip(awarded.iterrows(), dates):
        names = r.winner_names
        for raw in (names if not isinstance(names, str) else [names]):
            if not raw:
                continue
            f = acc.setdefault(str(raw).strip(), {
                'wins': 0, 'low': 0, 'value': [], 'regions': set(),
                'buyers': set(), 'early': 0, 'late': 0, 'sizes': Counter()})
            if isinstance(r.winner_size, str) and r.winner_size:
                f['sizes'][r.winner_size] += 1
            f['wins'] += 1
            f['low'] += int(r.n_tenders is not None
                            and not pd.isna(r.n_tenders) and r.n_tenders <= 1)
            if not pd.isna(r.award_value):
                f['value'].append(r.award_value)
            f['regions'].add(r.nuts2)
            f['buyers'].add(r.buyer_name)
            f['early' if when <= split else 'late'] += 1

    cust = {str(c).casefold() for c in customers}
    rows = []
    for firm, f in acc.items():
        rows.append({
            'firm': firm,
            'size': modal_size(f['sizes']),
            'wins': f['wins'],
            'low': f['low'],
            'lowpct': pct(f['low'], f['wins']),
            'value': money(float(np.median(f['value']))) if f['value'] else '—',
            'regions': f'{len(f["regions"])}/{trade_regions}',
            'buyers': len(f['buyers']),
            'trend': f'{f["early"]}->{f["late"]}',
            'falling': f['early'] - f['late'],
            'narrow': len(f['regions']) / max(trade_regions, 1),
            'flag': 'customer' if firm.casefold() in cust else '',
        })
    return rows, split


def modal_size(sizes):
    """The size a firm declares most often, marked "*" when it does not
    declare the same one twice running.

    `winner_size` is filled in per award notice by whoever typed it, and the
    identical string "Kieback&Peter GmbH & Co. KG" arrives as large, medium
    AND small on different notices. Taking whichever row happened to be read
    last put a 1,400-person company in a prospect pool defined as small firms.
    The mode is not truth either — it is a vote — so the disagreement is
    printed rather than hidden."""
    if not sizes:
        return '?'
    top, _ = sizes.most_common(1)[0]
    return top + ('*' if len(sizes) > 1 else '')


def _alias_key(name, companies=None):
    """The company a spelling belongs to (firms.py) — a fact off the award
    notice, a matching VAT or Handelsregister number, rather than the guess
    this used to make from the first two words of a name.

    The guess merged what it should not ("Bechtle GmbH" with "Bechtle AG", two
    companies) and split what it should not ("SVA GmbH" from "SVA System
    Vertrieb Alexander GmbH", one company, because a three-letter key fell back
    to the exact name). It stays as the fallback for a store without the
    identity columns, which is every store written before 2026-08.

    The first two significant words of a firm name, folded — "NDB
    ELEKTROTECHNIK GmbH & Co. KG, NL Berlin" and "NDB Elektrotechnik GmbH &
    Co. KG" reduce to the same key. Too-short keys fall back to the exact
    name so "BAU GmbH" never swallows "Baumann GmbH"."""
    if companies:
        found = companies.get(name) or companies.get(str(name).strip())
        if found:
            return found
    n = re.sub(r'\b(gmbh|co|kg|ag|mbh|se|ohg|e\.?k|und|&|\+)\b', ' ',
               name.casefold())
    key = ' '.join(re.sub(r'[^a-zäöüß ]', ' ', n).split()[:2])
    return key if len(key) >= 6 else name.casefold()


def _companies(data_dir):
    """{spelling: company} from the operator index, or None when there is no
    index yet — then every firm number in this view is per spelling, exactly
    as it was before firms.py."""
    try:
        import firms
        return firms.groups(data_dir)
    except Exception as e:                                     # noqa: BLE001
        print(f'# firm identity unavailable ({e}) — counts are per spelling')
        return None


def alias_groups(rows, companies=None):
    """Rows that belong to one company, flagged and listed, never merged HERE.

    `cmd_firms` prints one row per spelling on purpose — a person reading it
    wants to see what the buyers actually typed. The numbers a LETTER quotes
    are merged (`prospect_rows`), and since firms.py both the flag and the
    merge follow a registration number rather than a resemblance. The footer
    prints the combined total so a person can see what the firm is worth."""
    seen = {}
    for r in rows:
        seen.setdefault(_alias_key(r['firm'], companies), []).append(r)
    groups = [g for g in seen.values() if len(g) > 1]
    for group in groups:
        for r in group:
            r['flag'] = (r['flag'] + ' alias?').strip()
    return rows, groups


def cmd_firms(lots, trades, args):
    name, trade = resolve(trades, args.trade)
    sel = match(lots, trade, args.scope)
    rows, split = firm_rows(lots, sel, customer_names(args.data_dir))
    if not rows:
        print(f'# {name}\nNo awarded lot with a named winner yet.')
        return
    rows, groups = alias_groups(rows, _companies(args.data_dir))
    if args.size:
        rows = [r for r in rows if r['size'].rstrip('*') in args.size]
    if args.min_wins:
        rows = [r for r in rows if r['wins'] >= args.min_wins]

    order = {'wins': lambda r: (-r['wins'], -r['low']),
             'single': lambda r: (-r['low'], -r['wins']),
             'falling': lambda r: (-r['falling'], -r['wins']),
             'narrow': lambda r: (r['narrow'], -r['wins'])}[args.sort]
    rows.sort(key=order)

    total = sum(r['wins'] for r in rows)
    print(f'# {name} — who wins it')
    print(f'sorted by "{args.sort}"; {len(rows)} firms, {total} awarded lots')
    print(f'  wins     lots won in this trade')
    print(f'  0/1      of those, won against 0 or 1 bidder — the product\'s '
          f'promise, already happening to them')
    print(f'  regions  NUTS2 regions they win in, out of the regions the '
          f'trade tenders in; a low number is a firm missing its own market')
    print(f'  trend    wins before -> after {split.date() if split is not None else "?"}'
          f' (award dates); recent awards are under-published, so read it '
          f'as a comparison between firms, not as a decline')
    print()
    print(table(rows[:args.top],
                {'firm': 'firm', 'size': 'size', 'wins': 'wins',
                 'lowpct': '0/1', 'value': 'median award', 'regions': 'regions',
                 'buyers': 'buyers', 'trend': 'trend', 'flag': ''}))
    small = [r for r in rows if r['size'].rstrip('*') in SMALL_SIZES
             and r['wins'] >= 2]
    print(f'  prospect pool per GO_TO_MARKET.md (small/micro, >=2 wins): '
          f'{len(small)} firms')
    if groups:
        print(f'\n  probably one firm each, spelled several ways — the split '
              f'understates them, so merge by eye before writing:')
        for g in sorted(groups, key=lambda g: -sum(r['wins'] for r in g)):
            total = sum(r['wins'] for r in g)
            print(f'    {total} wins combined ('
                  + ' + '.join(str(r['wins']) for r in g) + '): '
                  + '  |  '.join(r['firm'] for r in g))


def customer_names(data_dir):
    """Winner-name spellings that are already customers, so the prospect list
    can flag them. Read through subscriptions.py — never off disk (CLAUDE.md).
    A deployment with no subscriptions is normal, not an error."""
    try:
        import subscriptions
        subs = subscriptions.load(config.data_root(data_dir), None)
    except Exception:
        return ()
    names = []
    for s in subs:
        names += list(s.get('award_names') or [])
        if s.get('name'):
            names.append(s['name'])
    return names


# ----------------------------------------------------- next / pitch

def firm_value(uncontested_year, median_award):
    """-> (value EUR/year, price EUR/month, model) for one firm.

    value = uncontested lots a year in their reach x the trade's median award
    x WIN_UPLIFT x MARGIN — what the subscription plausibly adds to their
    bottom line. The price is PRICE_SHARE of that, floored at the
    GO_TO_MARKET.md flat fee; where one median award clears PCT_DEAL_MIN the
    model flips to a percentage of the deal, because next to a 2 M EUR win a
    flat fee prices the product as noise."""
    value = uncontested_year * median_award * WIN_UPLIFT * MARGIN
    price = max(FLAT_FLOOR, PRICE_SHARE * value / 12)
    model = '% of deal' if median_award >= PCT_DEAL_MIN else 'flat'
    return value, price, model


def trade_economics(lots, sel, covered, mature):
    """The per-trade quantities the pricing needs: lots per year by region
    (covered months only), the mature-month 0/1 rate with its denominator,
    and the median award."""
    sub = lots[sel]
    months = max(len(covered), 1)
    cov = sub[sub.month.isin(covered)]
    region_year = (cov.groupby('nuts2').size() * 12.0 / months).to_dict()
    mat = sub[sub.month.isin(mature) & sub.resolved]
    rate = float((mat.n_tenders <= 1).mean()) if len(mat) else None
    val = sub.award_value.dropna()
    med_award = float(val.median()) if len(val) else None
    return region_year, rate, len(mat), med_award


def prospect_rows(lots, sel, region_year, rate, med_award, companies=None):
    """One row per prospect FIRM (alias-merged), with the letter numbers.

    This is the one place spellings ARE merged — by alias key, for the maths
    only, because a prospect's win count and price must not depend on how a
    clerk typed the name. Every spelling is kept in `names` so the letter can
    still quote the exact strings TED published.

    recs_year is what we would actually deliver: the trade's yearly lot count
    in the regions THE FIRM wins in. contactable means the GO_TO_MARKET.md
    segment (small/micro, proven winner) AND recs_year clears MIN_RECS_YEAR —
    the guarantee that we never contact a firm we cannot deliver to."""
    sub = lots[sel]
    awarded = sub[sub.winner_names.notna()]
    per = {}
    for _, r in awarded.iterrows():
        names = r.winner_names if not isinstance(r.winner_names, str) \
            else [r.winner_names]
        for raw in names:
            if not raw:
                continue
            name = str(raw).strip()
            f = per.setdefault(_alias_key(name, companies), {
                'names': set(), 'wins': 0, 'low': 0, 'regions': set(),
                'sizes': Counter(), 'values': []})
            f['names'].add(name)
            f['wins'] += 1
            if not pd.isna(r.n_tenders) and r.n_tenders <= 1:
                f['low'] += 1
            f['regions'].add(r.nuts2)
            if isinstance(r.winner_size, str) and r.winner_size:
                f['sizes'][r.winner_size] += 1
            if not pd.isna(r.award_value):
                f['values'].append(r.award_value)

    rows = []
    for f in per.values():
        recs_year = sum(region_year.get(rg, 0.0) for rg in f['regions'])
        uncontested_year = recs_year * (rate or 0.0)
        value, price, model = firm_value(uncontested_year, med_award or 0.0)
        size = modal_size(f['sizes'])
        why_not = []
        if size.rstrip('*') not in SMALL_SIZES:
            why_not.append(f'size {size}')
        if f['wins'] < MIN_PROSPECT_WINS:
            why_not.append(f'{f["wins"]} win')
        if recs_year < MIN_RECS_YEAR:
            why_not.append(f'{recs_year:.0f} picks/yr < {MIN_RECS_YEAR}')
        rows.append({
            'firm': sorted(f['names'], key=len)[0], 'names': sorted(f['names']),
            'size': size, 'wins': f['wins'], 'low': f['low'],
            'regions': sorted(f['regions']), 'recs_year': recs_year,
            'uncontested_year': uncontested_year, 'value': value,
            'price': price, 'model': model,
            'contactable': not why_not, 'why_not': ', '.join(why_not)})
    rows.sort(key=lambda r: -r['price'])
    return rows


def print_assumptions():
    print(f'  assumptions (GUESSES until a customer answers — one place to '
          f'change them, market.py):')
    print(f'    WIN_UPLIFT {WIN_UPLIFT:.0%} of flagged uncontested lots '
          f'become wins · MARGIN {MARGIN:.0%} of contract value '
          f'· PRICE_SHARE {PRICE_SHARE:.0%} of created value '
          f'· floor {FLAT_FLOOR:.0f} EUR/month')


def cmd_next(lots, trades, args):
    covered, mature, _ = coverage(lots)
    base = lots[lots.month.isin(mature) & lots.resolved]
    baseline = float((base.n_tenders <= 1).mean()) if len(base) else 0.0

    rows, rejected = [], []
    for name, trade in trades.items():
        sel = match(lots, trade, args.scope)
        region_year, rate, awarded, med_award = \
            trade_economics(lots, sel, covered, mature)
        if rate is None:
            rejected.append((name, 'no awarded lot in a mature month — '
                                   'nothing to rate yet'))
            continue
        if med_award is None:
            rejected.append((name, 'no published award sum — '
                                   'nothing to price'))
            continue
        pros = prospect_rows(lots, sel, region_year, rate, med_award,
                             _companies(args.data_dir))
        contact = [p for p in pros if p['contactable']]
        if not contact:
            rejected.append(
                (name, f'{len(pros)} winning firms, none contactable '
                       f'(small/micro, >={MIN_PROSPECT_WINS} wins, '
                       f'>={MIN_RECS_YEAR} picks/yr)'))
            continue
        def med(key):
            vals = sorted(p[key] for p in contact)
            return vals[len(vals) // 2]
        prices = [p['price'] for p in contact]
        rows.append({
            'trade': name[:34],
            'firms': len(contact),
            'recs': f'{med("recs_year"):.0f}',
            'low': f'{rate:.0%}' + ('' if awarded >= SMALL_SAMPLE else ' ?'),
            'uncon': f'{med("uncontested_year"):.1f}',
            'value': money(med('value')),
            'price': f'{med("price"):.0f}',
            'model': '% of deal' if med_award >= PCT_DEAL_MIN else 'flat',
            'revenue': sum(prices),
            'rev': f'{sum(prices):.0f}'})
    rows.sort(key=lambda r: -r['revenue'])

    print('# Which market first — contactable firms x what to charge them')
    print(f'store: {store_profile(lots)}')
    print(f'coverage: {len(covered)} covered months, {len(mature)} mature; '
          f'store-wide 0/1 baseline {baseline:.0%}')
    print_assumptions()
    print(f'  a firm is contactable when it is small/micro, has won '
          f'>={MIN_PROSPECT_WINS} lots in the trade, and its own regions '
          f'yield >={MIN_RECS_YEAR} picks a year — the "we will definitely '
          f'recommend tenders" guarantee.')
    print(f'  per-firm columns are the median contactable firm; '
          f'rev/mo is the sum over all of them — the sort key, because the '
          f'question is where a sales week earns most.')
    print()
    print(table(rows, {'trade': 'trade', 'firms': 'firms', 'recs': 'picks/yr',
                       'low': '0/1 bids', 'uncon': 'uncon/yr',
                       'value': 'value/yr', 'price': 'EUR/mo',
                       'model': 'model', 'rev': 'rev/mo'}))
    print('## Rejected, and why (the reasoning, not just the survivors)')
    for name, why in rejected:
        print(f'  {name}: {why}')
    print()
    print(f'next step: python market.py pitch "<firm>" for any firm in a '
          f'winning trade — the letter numbers, checked per firm.')


def cmd_pitch(lots, trades, args):
    want = args.firm.casefold()
    covered, mature, _ = coverage(lots)
    hits = []
    for name, trade in trades.items():
        sel = match(lots, trade, args.scope)
        region_year, rate, awarded, med_award = \
            trade_economics(lots, sel, covered, mature)
        if rate is None or med_award is None:
            continue
        for p in prospect_rows(lots, sel, region_year, rate, med_award,
                               _companies(args.data_dir)):
            if any(want in s.casefold() for s in p['names']):
                hits.append((name, sel, rate, awarded, med_award, p))
    if not hits:
        print(f'no winning firm matches "{args.firm}" in any trade — '
              f'`market.py firms <trade>` lists who does win.')
        return
    hits.sort(key=lambda h: -h[5]['wins'])
    name, sel, rate, awarded, med_award, p = hits[0]

    print(f'# {p["firm"]} — the letter numbers')
    if len(p['names']) > 1:
        print(f'  published under {len(p["names"])} spellings: '
              + '  |  '.join(p['names']))
    if len(hits) > 1:
        print(f'  also wins in: '
              + ', '.join(f'{h[0]} ({h[5]["wins"]})' for h in hits[1:4]))
    print(f'  trade: {name} · size {p["size"]} · '
          f'regions {", ".join(p["regions"])}')
    print()
    print(f'## What the letter can say')
    print(f'  - you won {p["wins"]} tenders in this trade that TED published'
          + (f', {p["low"]} of them against 0 or 1 competitors' if p['low']
             else ''))
    print(f'  - your regions see ~{p["recs_year"]:.0f} {name} lots a year — '
          f'that is what we would send you')
    print(f'  - ~{p["uncontested_year"]:.1f} of those a year will end with 0 '
          f'or 1 bidders (trade rate {rate:.0%} on {awarded} awarded lots'
          + ('' if awarded >= SMALL_SAMPLE else ', small sample') + ')')
    print(f'  - median award in the trade: {money(med_award)} EUR')
    print()
    print(f'## What to charge')
    print(f'  value basis ~{money(p["value"])} EUR/year -> '
          f'{p["price"]:.0f} EUR/month, model: {p["model"]}')
    print_assumptions()
    print()
    print(f'## Their wins on record (the proof)')
    sub = lots[sel]
    mask = sub.winner_names.map(
        lambda ns: not isinstance(ns, (type(None), float))   # None/NaN
        and any(str(n).strip() in p['names'] for n in ns))
    for _, r in sub[mask].sort_values('award_date').iterrows():
        bids = '—' if pd.isna(r.n_tenders) else f'{int(r.n_tenders)} bids'
        print(f'  {r.award_date}  {str(r.title)[:64]:64s}  '
              f'{money(r.award_value):>8}  {bids}  {r.publication_number}')
    if not p['contactable']:
        print(f'\n  NOT in the contactable pool: {p["why_not"]} — '
              f'write anyway only if you know something the data does not.')


# --------------------------------------------------------------- rank

def cmd_rank(lots, trades, args):
    covered, mature, _ = coverage(lots)
    n_months = len(covered)
    claimed = pd.Series(False, index=lots.index)
    rows = []
    for name, trade in trades.items():
        sel = match(lots, trade, args.scope)
        claimed |= sel
        sub = lots[sel]
        cov = sub[sub.month.isin(covered)]
        if len(cov) < args.min_lots:
            continue
        mat = sub[sub.month.isin(mature) & sub.resolved]
        val = sub.award_value.dropna()
        firms, _ = firm_rows(lots, sel)
        pool = [f for f in firms if f['size'].rstrip('*') in SMALL_SIZES
                and f['wins'] >= 2]
        top3 = sum(sorted((f['wins'] for f in firms), reverse=True)[:3])
        wins = sum(f['wins'] for f in firms) or 1
        rows.append({
            'trade': name[:38],
            'permonth': round(len(cov) / n_months, 1),
            'lots': len(cov),
            'value': money(val.median()) if len(val) else '—',
            'year': money(val.median() * len(cov) / n_months * 12)
                    if len(val) else '—',
            'low': pct(int((mat.n_tenders <= 1).sum()), len(mat))
                   + ('' if len(mat) >= SMALL_SAMPLE else ' ?'),
            'lowsort': (mat.n_tenders <= 1).mean() if len(mat) else -1,
            'awarded': len(mat),
            'firms': len(firms),
            'top3': pct(top3, wins),
            'pool': len(pool),
            'yearsort': (val.median() * len(cov) / n_months * 12)
                        if len(val) else -1,
        })

    order = {'pool': lambda r: -r['pool'], 'lots': lambda r: -r['permonth'],
             'single': lambda r: -r['lowsort'], 'value': lambda r: -r['yearsort']}
    rows.sort(key=order[args.sort])

    print('# Which trade to sell into next')
    print(f'store: {store_profile(lots)}')
    print(f'trades: {len(trades)} in {TRADES_FILE.name}, '
          f'{len(rows)} with at least {args.min_lots} lots in covered months')
    print(f'coverage: {n_months} covered months, {len(mature)} of them mature '
          f'enough for a bidder rate')
    print(f'the trade list claims {pct(int(claimed.sum()), len(lots))} of all '
          f'store lots by title; the rest is vocabulary nobody has written yet')
    print(f'sorted by "{args.sort}".  pool = small/micro firms with >=2 wins '
          f'(the GO_TO_MARKET.md segment).  "?" marks a rate under '
          f'{SMALL_SAMPLE} awarded lots.')
    print()
    print(table(rows[:args.top],
                {'trade': 'trade', 'permonth': 'lots/mo', 'value': 'median',
                 'year': 'EUR/year', 'low': '0/1 bids', 'awarded': 'awarded',
                 'firms': 'firms', 'top3': 'top3', 'pool': 'pool'}))
    if args.by == 'trade-region':
        cmd_rank_region(lots, trades, args, covered, mature, n_months)


def cmd_rank_region(lots, trades, args, covered, mature, n_months):
    rows = []
    for name, trade in trades.items():
        sub = lots[match(lots, trade, args.scope)]
        for region, g in sub.groupby('nuts2'):
            cov = g[g.month.isin(covered)]
            if len(cov) < args.min_lots:
                continue
            mat = g[g.month.isin(mature) & g.resolved]
            val = g.award_value.dropna()
            rows.append({
                'cell': f'{name[:30]} / {region}',
                'permonth': round(len(cov) / n_months, 1),
                'value': money(val.median()) if len(val) else '—',
                'low': pct(int((mat.n_tenders <= 1).sum()), len(mat))
                       + ('' if len(mat) >= SMALL_SAMPLE else ' ?'),
                'lowsort': (mat.n_tenders <= 1).mean() if len(mat) else -1,
                'awarded': len(mat)})
    rows.sort(key=lambda r: -r['lowsort'] if args.sort == 'single'
              else -r['permonth'])
    print(f'\n## By trade and region (cells under {args.min_lots} lots '
          f'suppressed — {n_months} months split across regions runs out of '
          f'data fast)')
    print(table(rows[:args.top],
                {'cell': 'trade / region', 'permonth': 'lots/mo',
                 'value': 'median', 'low': '0/1 bids', 'awarded': 'awarded'}))


# --------------------------------------------------------------- suggest

def cmd_suggest(lots, trades, args):
    name, trade = resolve(trades, args.trade)
    sel = match(lots, trade, 'core')
    inside, outside = lots[sel], lots[~sel]
    if inside.empty:
        print(f'# {name}\nNothing matches yet — no lots to learn words from.')
        return

    have = set(trade['terms'])
    known = set(ev.trade_roots()[0])
    reviewed = []
    counted = Counter()
    for text in inside.t_full:
        for w in set(ev.tokens(text)):
            for r in ev.roots_in(w):
                counted[r] += 1
    unlisted = Counter()
    for text in inside.t_full:
        for w in set(ev.tokens(text)):
            fw = ev.fold(w)
            if len(fw) >= MIN_TERM_LEN and not ev.roots_in(w):
                unlisted[fw] += 1

    def score(word):
        n_in = int(inside.t_full.str.contains(word, regex=False).sum())
        share_in = n_in / len(inside)
        share_out = float(outside.t_full.str.contains(word, regex=False).mean())
        return n_in, share_in, share_out, share_in / max(share_out, 1e-4)

    for root in counted:
        if root in have:
            continue
        n_in, s_in, s_out, lift = score(root)
        if n_in >= args.min_lots and lift >= 3:
            reviewed.append({'word': root, 'lots': n_in,
                             'inside': f'{s_in:.0%}', 'outside': f'{s_out:.2%}',
                             'lift': f'{lift:.0f}x', 'sort': lift})
    reviewed.sort(key=lambda r: -r['sort'])

    fresh = []
    for word, n in unlisted.most_common(400):
        if n < args.min_lots or word in have:
            continue
        n_in, s_in, s_out, lift = score(word)
        if lift >= 8:
            fresh.append({'word': word, 'lots': n_in, 'inside': f'{s_in:.0%}',
                          'outside': f'{s_out:.2%}', 'lift': f'{lift:.0f}x',
                          'sort': lift})
    fresh.sort(key=lambda r: -r['sort'])

    print(f'# {name} — vocabulary proposals')
    print(f'from the {int(sel.sum())} lots whose title already matches. '
          f'Proposals only: paste the ones that name THIS trade into '
          f'{TRADES_FILE.name}.')
    print()
    print(f'## From the reviewed vocabulary ({Path(ev.ROOTS_FILE).name}) — '
          f'these already passed a human read as trade words')
    print(table([{k: v for k, v in r.items() if k != 'sort'}
                 for r in reviewed[:args.top]],
                {'word': 'word', 'lots': 'lots', 'inside': 'inside',
                 'outside': 'outside', 'lift': 'lift'}))
    print(f'## Not in the reviewed vocabulary — unread, and this is where '
          f'boilerplate and place names live. Judge every one.')
    print(table([{k: v for k, v in r.items() if k != 'sort'}
                 for r in fresh[:args.top]],
                {'word': 'word', 'lots': 'lots', 'inside': 'inside',
                 'outside': 'outside', 'lift': 'lift'}))
    print(f'  ({len(known)} words in the reviewed vocabulary; a word absent '
          f'from it may still be right — it just has not been read yet)')


# --------------------------------------------------------------- trades

def cmd_trades(lots, trades, args):
    covered, _, _ = coverage(lots)
    sel = {name: match(lots, trade, 'core') for name, trade in trades.items()}
    hits = sum(s.astype(int) for s in sel.values())   # trades per lot
    claimed = hits > 0
    rows = []
    for name, trade in trades.items():
        core = sel[name]
        rows.append({'trade': name[:40], 'words': len(trade['terms']),
                     'core': int(core.sum()),
                     'mentioned': int(match(lots, trade, 'mentioned').sum()),
                     'permonth': round(int(core[lots.month.isin(covered)].sum())
                                       / max(len(covered), 1), 1),
                     'shared': pct(int((core & (hits > 1)).sum()),
                                   int(core.sum()))})
    rows.sort(key=lambda r: -r['core'])
    print(f'# {TRADES_FILE.name} against the store')
    print(f'store: {store_profile(lots)}')
    print(f'"shared" is the share of a trade\'s lots that another trade also '
          f'claims. Overlap is real — "Elektro, Blitzschutz, Datennetz" is one '
          f'lot in three trades — but a trade over ~50% shared is probably '
          f'carrying a word that belongs to its neighbour.')
    print()
    print(table(rows, {'trade': 'trade', 'words': 'words', 'core': 'by title',
                       'mentioned': 'body only', 'permonth': 'lots/mo',
                       'shared': 'shared'}))
    print(f'  the list claims {pct(int(claimed.sum()), len(lots))} of store '
          f'lots by title ({int(claimed.sum()):,} of {len(lots):,})')
    print(f'  {int((~claimed).sum()):,} lots match no trade — either general '
          f'contracting, or vocabulary still missing. A few of them:')
    for t in lots[~claimed].title.dropna().head(8):
        print(f'    {str(t)[:96]}')


# --------------------------------------------------------------- cli

def main():
    p = argparse.ArgumentParser(
        description='Market size, competition and prospects for one trade — '
                    'matched by the words in the notice, never by CPV code. '
                    'Prints to stdout; writes nothing.')
    p.add_argument('--data-dir', help='state directory (see config.py)')
    p.add_argument('--scope', choices=['core', 'mentioned', 'both'],
                   default='core',
                   help='core (default) = the title names the trade')
    p.add_argument('--top', type=int, default=20, help='rows per table')

    # The same three options again, accepted AFTER the subcommand as well.
    # SUPPRESS is what makes both orders work: an option the user did not type
    # is absent from the namespace instead of overwriting the parent's value
    # with a default.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--data-dir', default=argparse.SUPPRESS)
    common.add_argument('--scope', default=argparse.SUPPRESS,
                        choices=['core', 'mentioned', 'both'])
    common.add_argument('--top', type=int, default=argparse.SUPPRESS)
    sub = p.add_subparsers(dest='cmd', required=True, parser_class=(
        lambda **kw: argparse.ArgumentParser(parents=[common], **kw)))

    sub.add_parser('next', help='THE decision: which market first, who is '
                                'contactable there, what to charge')

    pi = sub.add_parser('pitch', help='one firm\'s letter numbers')
    pi.add_argument('firm', help='firm name or a piece of it, e.g. Jebsen')

    t = sub.add_parser('trade', help='the market page for one trade')
    t.add_argument('trade')
    t.add_argument('--region', help='NUTS prefixes, comma separated (DE2,DE1)')
    t.add_argument('--since', help='earliest publication month, YYYY-MM')
    t.add_argument('--sample', type=int, default=12,
                   help='matched lots printed as the match receipt')

    f = sub.add_parser('firms', help='who wins this trade — the prospect list')
    f.add_argument('trade')
    f.add_argument('--sort', choices=['wins', 'single', 'falling', 'narrow'],
                   default='wins')
    f.add_argument('--size', help='winner_size filter, e.g. small,micro')
    f.add_argument('--min-wins', type=int, default=1)

    r = sub.add_parser('rank', help='which trade to sell into next')
    r.add_argument('--by', choices=['trade', 'trade-region'], default='trade')
    r.add_argument('--sort', choices=['pool', 'lots', 'single', 'value'],
                   default='pool')
    r.add_argument('--min-lots', type=int, default=30,
                   help='suppress trades (or cells) thinner than this')

    s = sub.add_parser('suggest', help='vocabulary proposals for a trade')
    s.add_argument('trade')
    s.add_argument('--min-lots', type=int, default=3)

    sub.add_parser('trades', help='what the trade list claims, trade by trade')

    args = p.parse_args()
    if getattr(args, 'region', None):
        args.region = [x.strip().upper() for x in args.region.split(',') if x.strip()]
    if getattr(args, 'size', None):
        args.size = [x.strip() for x in args.size.split(',') if x.strip()]

    trades = load_trades()
    lots = add_text(load_lots(config.data_root(args.data_dir)))
    {'next': cmd_next, 'pitch': cmd_pitch, 'trade': cmd_trade,
     'firms': cmd_firms, 'rank': cmd_rank, 'suggest': cmd_suggest,
     'trades': cmd_trades}[args.cmd](lots, trades, args)


if __name__ == '__main__':
    main()
