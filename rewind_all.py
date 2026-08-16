"""TenderMining forward backtest — replay history as the loop would have run it.

For every weekly cutoff D, an as-of world is materialised (only notices
published before D), the champion is retrained inside it, open lots are
scored, gated subscriptions get their as-of profiles, and the week's picks
are recorded. Afterwards every pick is graded against the outcomes the full
store later learned. The result is the live track record, bootstrapped
backwards: "across the replayed period, picks ended with 0-1 bids X in 100,
base rate Y" — plus, per gated subscription, whether the customer's own
eventual wins appeared in their replayed market. Picks are graded on two
axes: did the lot end with 0-1 bids (the competition claim), and did the
firm the pick was handed to eventually win it themselves, regardless of
bidder count (the customer-outcome claim). The axes have different
denominators: the first needs a countable bid field, the second only a
named winner on the latest award revision.

Time isolation lives in `asof.py` (REFACTOR.md phase 5): the as-of world,
the calibration inside it, the pre-cutoff profile references and the pre-
cutoff champion are the engine's guarantees, shared with the other rewind
programs and documented there — including the embedding-sidecar residual.
One residual is this program's own: trust/thresholds are recalibrated every
RECAL_EVERY cutoffs, not weekly (cost) — which uses staler thresholds, never
later ones.

**This program produces one thing: a JSON document on stdout** (see
`build_payload`). It writes no file and owns no path — the operator names the
file or pipes it, and progress goes to stderr so the stream stays clean. Every
human-readable form is a renderer over that document, never a second
computation: `--render` prints the operator's prose, `trade_pages.py --replay`
slices the same document by trade into HTML. Replay once (33 minutes over the
2026-08-11 store), render as often as you like.

Usage:
    python rewind_all.py > run.json             # weekly, whole feasible range
    python rewind_all.py --out run.json         # same, without the redirect
    python rewind_all.py --step 14 --sub jebsen-blitzschutz > run.json
    python rewind_all.py --render run.json      # the report, without replaying
    python rewind_all.py --render -             # ... from a pipe
    python trade_pages.py --replay run.json
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import asof
import config
import heavy_lock
import loop
import relevance as rel
import selection
import single_bidder as sb
import subscriptions
from selection import lot_key
from calibrate import WorldTooThin
from embed import MODEL_TAG
import grading

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

MIN_TRAIN_LOTS = 300   # first cutoff needs this many labeled lots
RECAL_EVERY = 8        # cutoffs between trust/threshold recalibrations
FLAG_THRESHOLD = 0.5   # mirrors the loop's --threshold default
# The market-wide target simulation has no subscription behind it, so these
# two are its own horizon and its own cap. A CUSTOMER's deadline promise and
# pick cap are never these: they come off the subscription line, inside
# selection.py — which is the drift this phase removed.
MIN_DEADLINE_DAYS = 14
MAX_PICKS = 5


def winner_map(awards_full):
    """lot -> set of stripped winner names, from the latest award revision.

    Deliberately unfiltered (no n_tenders / quality gate): winning does not
    require a countable bid field, so this axis has its own denominator.
    """
    latest = (awards_full.sort_values('publication_date')
              .groupby(sb.KEY, as_index=False).tail(1))
    winners = {}
    for _, a in latest.iterrows():
        names = a['winner_names']
        if names is None:
            continue
        clean = {str(n).strip() for n in list(names) if str(n).strip()}
        if clean:
            winners[(a['procedure_id'], a['lot_id'])] = clean
    return winners


CALIBRATED_BARS = ('min_relevance', 'min_code_hard', 'min_code_soft')


def as_of_profile(gate, sub, awards_asof):
    """The subscription's profile as it would have stood: wins awarded (i.e.
    award notice published) before the cutoff. None when the customer had no
    resolvable win yet.

    The customer's OWN three bars are dropped, not overridden, so the gate
    falls through to the as-of calibration carried on the config (see
    `replay`). Two reasons, and only the first is about leakage:

    * a bar a customer carries today was chosen with the whole history in
      view, so a replayed week must not judge with it;
    * a search that switches a channel off says so out of range — text off is
      `inf` (configuration F, when the code channel already meets the recall
      target) and soft off is 2.0 (SOFT_GRID's sentinel). Neither can sit on a
      subscription line, because `subscriptions.validate` confines a
      customer's bars to [0, 1] — rightly: out there such a value is a typo.
      A config field is not customer input and takes them as written.
    """
    refs = asof.refs_for_firm(gate, awards_asof, sub.get('name'))
    if not refs:
        return None
    spec = subscriptions.override(
        {k: v for k, v in sub.items() if k not in CALIBRATED_BARS},
        profile_refs=refs)
    return rel.build_profile(gate, spec)


def replay(data_dir, step_days, sub_ids):
    full_store = Path(data_dir) / 'store'
    world = asof.World(data_dir, Path(data_dir) / 'asof' / 'all')

    # FOUR COLUMNS, not the whole table. The full frame is 103 mostly-object
    # columns over ~29k lots -- 329 MB that stays resident for the whole
    # replay beside the World's own copy of the same store, to answer two
    # questions (the last publication date, and the dates per lot below).
    # Read narrow, answer both here, drop it before the first cutoff.
    # doc/MEMORY_BUDGET.md has the measurement.
    tenders_full = pd.read_parquet(
        full_store / 'tenders.parquet',
        columns=['procedure_id', 'lot_id', 'publication_date', 'deadline_date'])
    awards_full = pd.read_parquet(full_store / 'awards.parquet')
    aw_latest, _ = sb.latest_awards(awards_full)
    outcome = {(a['procedure_id'], a['lot_id']): int(a['n_tenders'])
               for _, a in aw_latest.iterrows()}

    # The subscription DEFINITION is taken as it stands now (the newest
    # version in force), not as of each cutoff: as_of_profile() is what
    # rewinds a customer to their historical wins, while their market
    # filters are the thing under test. Resolving per cutoff would be a
    # different experiment -- deliberately not this one.
    subs = {s0['sub_id']: s0 for s0
            in subscriptions.load(data_dir, date.today().isoformat())
            if s0['sub_id'] in sub_ids}

    pub_a = pd.to_datetime(awards_full['publication_date'])
    first = pub_a.min() + pd.Timedelta(days=1)
    last = pd.to_datetime(tenders_full['publication_date']).max()
    cutoffs = pd.date_range(first, last, freq=f'{step_days}D')

    # call publication + deadline per lot, for the own-win fairness split: a
    # win whose call closed before the firm had a single published reference
    # was never judged at all (profile None). Derived here rather than in the
    # return statement so the frame it comes from does not have to outlive the
    # loop.
    lot_dates = {
        (r.procedure_id, r.lot_id): (str(r.publication_date or ''),
                                     str(r.deadline_date or ''))
        for r in tenders_full.drop_duplicates(
            subset=['procedure_id', 'lot_id']).itertuples(index=False)}
    del tenders_full

    flagged = {}          # lot -> first week's score (global record, dedup)
    scored_lonely = {}    # lot -> ever scored while open (base-rate pool)
    week_flags = {}       # cutoff -> [(lot, score, cpv3, nuts1)] for target sim
    sub_picks = {s: {} for s in subs}
    sub_market = {s: set() for s in subs}
    cfg, n_run, gate = None, 0, None
    for D in cutoffs:
        # Drop last week's gate BEFORE this week's rewind, calibration and
        # training run. It holds the sidecar matrices and seven per-lot object
        # arrays (~350 MB); left bound to the name it would stay alive until
        # `world.gate()` below had finished building its replacement, so the
        # two coexisted at the exact moment the calibration matrices were also
        # up. Nothing outside one iteration reads it. doc/MEMORY_BUDGET.md.
        gate = None
        world.rewind(D)
        n_lots = world.data.groupby(sb.KEY).ngroups
        if n_lots < MIN_TRAIN_LOTS:
            continue
        if cfg is None or n_run % RECAL_EVERY == 0:
            try:
                world.calibrate()
            except WorldTooThin as e:
                # Early cutoffs only: the world grows monotonically, so this
                # never strands a later week. Printed, not swallowed — a
                # skipped cutoff must not read as a cutoff with no picks.
                print(f'[rewind_all] {D.date()}: skipped — {e}', flush=True,
                      file=sys.stderr)
                continue
            # Recipe F: all three bars ride on the config, and as_of_profile
            # drops the customer's own — see its docstring for why.
            cfg = world.calibrated_config('F')
        n_run += 1

        model = world.model()

        open_t = sb.open_tenders(world.tenders, world.aw)
        deadline = pd.to_datetime(open_t.get('deadline_date'), errors='coerce')
        open_t = open_t[deadline >= D]
        if open_t.empty:
            continue
        Xo, _, _, _ = sb.build_features(open_t, world.roles,
                                        list_frame=world.tenders)
        scores = sb.predict(model, Xo)

        gate = world.gate(cfg)
        profiles = {s: as_of_profile(gate, subs[s], world.awards)
                    for s in subs}
        # the target simulation is a market-wide question with no subscription
        # behind it, so it keeps its own fixed horizon; a customer's own
        # deadline promise is applied per subscription, inside selection.py
        dl_ok = (deadline.loc[open_t.index]
                 >= D + pd.Timedelta(days=MIN_DEADLINE_DAYS))
        n_flagged = 0
        candidates = []
        wf = week_flags.setdefault(str(D.date()), [])
        for (i, row), s in zip(open_t.iterrows(), scores):
            lot = (row['procedure_id'], row['lot_id'])
            cpv3 = str(row.get('cpv_main') or '')[:3]
            scored_lonely.setdefault(lot, cpv3)
            flag = bool(s >= FLAG_THRESHOLD)
            if flag:
                n_flagged += 1
                flagged.setdefault(lot, (str(D.date()), float(s), cpv3))
                nuts1 = (str(row['place_nuts3'])[:3]
                         if pd.notna(row.get('place_nuts3')) else None)
                if bool(dl_ok.loc[i]):
                    wf.append((lot, float(s), cpv3, nuts1,
                               str(row.get('title'))[:60],
                               str(row.get('buyer_name'))[:40]))
            # the row as the shipped selection expects it: the market fields it
            # slices on, the deadline it promises against, and the competition
            # verdict — the same keys a prediction-ledger row carries
            candidates.append({
                'procedure_id': lot[0], 'lot_id': lot[1],
                'buyer_name': row.get('buyer_name'), 'title': row.get('title'),
                'score': float(s), 'flag': flag,
                'cpv_main': row.get('cpv_main'), 'cpv3': cpv3 or None,
                'place_nuts3': row.get('place_nuts3'),
                'deadline_date': row.get('deadline_date'),
            })
        for s, profile in profiles.items():
            if profile is None:
                continue
            # The shipped selection, not a replica of it (REFACTOR.md phase 4):
            # slice, gate, deadline, rank and cap all come from selection.py,
            # which is what loop.deliver runs on Monday. This is the whole
            # point of the measurement — the drifted second copy that used to
            # live here is what defect 1 was.
            sel = selection.for_sub(subs[s], candidates, D.date(),
                                    gate=gate, profile=profile)
            sub_market[s].update(lot_key(d) for d in sel.market)
            for d in sel.picks:
                sub_picks[s].setdefault(
                    lot_key(d),
                    {'week': str(D.date()), **{k2: d[k2] for k2 in
                                               ('title', 'buyer_name', 'score')}})
        print(f'[rewind_all] {D.date()}: {n_lots} train lots, '
              f'{len(open_t)} open, {n_flagged} flagged',
              flush=True, file=sys.stderr)

    # Keep whatever tier 3 had to embed. Without this the same words are
    # re-embedded on every replay and the ONNX model is loaded to do it --
    # ~650 MB resident for the rest of the process. A run that reports misses
    # here wants `python embed_vocab.py` before the next one.
    if rel._SYN is not None:
        if rel._SYN.misses:
            print(f'[rewind_all] tier 3 embedded {rel._SYN.misses} uncached '
                  f'words (the model was loaded for them) — run '
                  f'`python embed_vocab.py` to keep the next replay off it',
                  flush=True, file=sys.stderr)
        rel._SYN.save()

    return {'flagged': flagged, 'scored': scored_lonely, 'sub_picks': sub_picks,
            'sub_market': sub_market, 'outcome': outcome, 'subs': subs,
            'awards_full': awards_full, 'gate_dir': str(world.work),
            'step_days': step_days, 'n_cutoffs': n_run,
            'lot_dates': lot_dates,
            'week_flags': week_flags, 'winners': winner_map(awards_full)}


def cpv3_labels():
    """cpv3 prefix -> German group label, from the committed CPV dictionary."""
    labels = {}
    for line in Path('cpv_2008_de.csv').read_text(encoding='utf-8').splitlines():
        if line.startswith('#') or line.startswith('code,'):
            continue
        code, _, label = line.partition(',')
        if code.endswith('00000'):
            labels[code[:3]] = label.strip()
    return labels


def flag_matrix(payload):
    """**Forecast** precision AND recall for the binary alarm, across the
    whole replay.

    "Forecast", not just "precision": this project measures two unrelated
    things with those words, and reading one for the other is the easiest
    mistake to make in it.

      * **forecast** precision/recall — HERE and nowhere else. Did a lot we
        flagged really end with 0-1 bids? Ground truth is `n_tenders` from a
        published award, so it can only be answered by an as-of replay.
      * **gate** precision/recall — `calibrate.py`. Is a lot in this
        customer's trade at all? Ground truth is the hand-labelled benchmark,
        and it has nothing to do with how many people bid.

    A number from one is meaningless in the other's sentence. `doc/METHODS.md`
    has the table.

    The summary above this section reports precision only ("alarms right, N x
    better than chance"). Precision alone cannot be wrong in an interesting
    way: a model that raises one alarm a year and gets it right scores 100%.
    Recall is the half that says what the alarm walked past, and the flag
    view is where a customer's actual question lives — "if I act on every
    alarm, what do I get, and what do I miss?"

    Scored with grading.flag_stats, the same function the weekly report uses, so
    the replayed number and the live number are comparable statistics rather
    than two implementations that happen to agree. Population: every tender
    examined while open whose award has since published. Alarms are deduped
    per lot by the replay (first week's score), so this is one row per lot.
    """
    rows = [{'flag': r['flag'], 'label': int(r['n_tenders'] <= 1)}
            for r in payload['lots'] if r['n_tenders'] is not None]
    f = grading.flag_stats(rows)
    if not f:
        return ['## The flag: forecast precision and recall', '',
                'No examined tender has a published result yet.', '']
    lines = ['## The flag: forecast precision and recall', '',
             'The same statistic the weekly report prints, over the replay '
             'instead of over live', 'grading — which is the only reason it '
             'can be read today: a live award publishes a', 'median 84 days '
             'after its tender, so the loop\'s own version of this table is '
             'still', 'counting in single digits.', '',
             f"Of {f['n']} examined tenders with a published result, the model "
             f"raised {f['flagged']} alarms; {f['positives']} really ended with "
             '0-1 bids.', '',
             '| | alarm raised | no alarm | total |',
             '|---|---|---|---|',
             f"| **ended 0-1 bids** | {f['tp']} | {f['fn']} | {f['positives']} |",
             f"| **ended 2+ bids** | {f['fp']} | {f['tn']} | {f['n'] - f['positives']} |",
             f"| total | {f['flagged']} | {f['n'] - f['flagged']} | {f['n']} |", '']
    prec = (f"{f['precision']:.0%}" if f['precision'] is not None else '—')
    rec = (f"{f['recall']:.0%}" if f['recall'] is not None else '—')
    ci_p, ci_r = f['precision_ci'], f['recall_ci']
    lines += [f"- **forecast precision** (alarms right): {prec}"
              + (f" (95% CI {ci_p[0]:.0%}-{ci_p[1]:.0%})" if ci_p else ''),
              f"- **forecast recall** (0-1-bid tenders caught): {rec}"
              + (f" (95% CI {ci_r[0]:.0%}-{ci_r[1]:.0%})" if ci_r else ''),
              f"- **F1**: {f['f1']:.2f}" if f['f1'] is not None else '- **F1**: —',
              '']
    if f['positives']:
        lines += ['Against raising an alarm on **every** tender: forecast precision '
                  f"{f['base']:.0%}, recall 100%"
                  + (f", F1 {f['base_f1']:.2f}." if f['base_f1'] is not None else '.'),
                  '']
        if f['precision'] is not None and not f['beats_base']:
            lines += ['**At this cut-off the alarm is not paying for itself** — its '
                      f"precision ({f['precision']:.0%}) is at or below the "
                      f"{f['base']:.0%} that alarming on everything gives, and it "
                      'gives up recall to get there.', '']
    return lines


def trade_table(payload):
    """Per-cpv3 slice of the global replay — the branch-ranking table.

    Sorted by graded flag count (evidence weight), not by hit rate: a thin
    slice's 100% is an anecdote, not a branch decision.

    CPV3 is an INTERNAL grouping and this table is not quotable on a public
    page: buyers enter CPV wrongly, which is why `market.py` never consults it
    and why `trade_pages.py` re-slices the same payload by title words. It is
    the reason `cpv3` rides on a lot row while `trade` deliberately does not —
    a raw store field cannot drift, a derived grouping can.
    """
    labels = cpv3_labels()
    trades = {}
    for r in payload['lots']:
        t = trades.setdefault(r['cpv3'], {'scored': 0, 'graded': [], 'flags': 0,
                                          'graded_flags': []})
        t['scored'] += 1
        if r['n_tenders'] is not None:
            t['graded'].append(r['n_tenders'])
        if r['flag']:
            t['flags'] += 1
            if r['n_tenders'] is not None:
                t['graded_flags'].append(r['n_tenders'])
    lines = ['## Per trade — where the alarms were right', '',
             'Every rate is printed as its fraction: "115/480 (24%)" means '
             '480 alarms could',
             'be checked against a published result and 115 of them really '
             'ended with 0-1 bids.', '',
             '| code | trade | examined | results known | chance (0-1 bids anyway) | alarms | alarms right / checked | vs chance |',
             '|---|---|---|---|---|---|---|---|']
    for cpv3, t in sorted(trades.items(),
                          key=lambda kv: (-len(kv[1]['graded_flags']), kv[0])):
        if not t['flags']:
            continue
        n_base = sum(1 for n in t['graded'] if n <= 1)
        n_hit = sum(1 for n in t['graded_flags'] if n <= 1)
        base = n_base / len(t['graded']) if t['graded'] else float('nan')
        hit = (n_hit / len(t['graded_flags'])
               if t['graded_flags'] else float('nan'))
        lift = hit / base if t['graded_flags'] and base else float('nan')
        thin = ' (thin)' if 0 < len(t['graded_flags']) < 10 else ''
        chance = (f'{n_base}/{len(t["graded"])} ({base:.0%})'
                  if t['graded'] else '—')
        right = (f'{n_hit}/{len(t["graded_flags"])} ({hit:.0%}){thin}'
                 if t['graded_flags'] else f'0 checked of {t["flags"]}')
        vs = (f'{lift:.2f}x' if t['graded_flags'] and base else '—')
        lines.append(
            f'| {cpv3} | {labels.get(cpv3, "?")[:45]} | {t["scored"]} '
            f'| {len(t["graded"])} | {chance} | {t["flags"]} | {right} | {vs} |')
    return lines + ['']


def target_rows(res, targets_csv, max_picks=MAX_PICKS):
    """Deliver weekly picks to every firm in the outreach target list across
    the replay, and grade them — SIMULATION.md's market rule (the trades and
    NUTS-1 regions of the firm's wins; no regions = nationwide), run
    backwards through time. One pick row per (firm, lot) ever, like the live
    simulation ledger.

    Returns the per-firm rows for the payload. This runs at REPLAY time
    because it needs the targets CSV and the winner map; `target_lines` then
    renders them, so the prose is a view of the payload like everything else.
    """
    targets = pd.read_csv(targets_csv, encoding='utf-8-sig')
    outcome = res['outcome']
    markets = {}
    for _, t in targets.iterrows():
        trades = set(str(t['trades']).split(';')) - {'', 'nan'}
        regions = set(str(t['regions']).split(';')) - {'', 'nan'}
        markets[t['company']] = (trades, regions)
    picks = {c: {} for c in markets}
    for week in sorted(res['week_flags']):
        for company, (trades, regions) in markets.items():
            cand = [(lot, s) for lot, s, cpv3, nuts1, _, _
                    in res['week_flags'][week]
                    if cpv3 in trades and (not regions or nuts1 is None
                                           or nuts1 in regions)]
            cand.sort(key=lambda x: -x[1])
            taken = 0
            for lot, s in cand:
                if taken >= max_picks:
                    break
                if lot not in picks[company]:
                    picks[company][lot] = week
                    taken += 1
    winners = res['winners']
    rows = []
    for _, t in targets.iterrows():
        c = t['company']
        graded = [outcome[lot] for lot in picks[c] if lot in outcome]
        known = [lot for lot in picks[c] if lot in winners]
        rows.append({
            'company': c, 'trade_match': t.get('trade_match'),
            'primary_trade': str(t['trades']).split(';')[0],
            'picks': len(picks[c]), 'graded': len(graded),
            'lonely': sum(1 for n in graded if n <= 1),
            'winner_known': len(known),
            'won': sum(1 for lot in known
                       if str(c).strip() in winners[lot]),
        })
    return rows


def target_lines(payload):
    """Render the per-firm target rows. Returns [] when the run had no
    `--targets`, which is the usual case."""
    rows = payload.get('targets')
    if not rows:
        return []
    per_firm = pd.DataFrame(rows)
    lines = ['## Target firms — picks replayed and checked', '',
             'A pick is counted per firm: the same tender handed to 20 firms '
             'counts 20',
             'times here (this measures what each firm saw, not distinct '
             'tenders).', '']
    seg = per_firm[(per_firm['trade_match'] == True)      # noqa: E712
                   & (per_firm['primary_trade'] == '452')]
    for label, g in (('all targets', per_firm),
                     ('text-confirmed 452 (the campaign batch)', seg)):
        got = g[g['picks'] > 0]
        gr = g[g['graded'] > 0]
        hit = g['lonely'].sum() / g['graded'].sum() if g['graded'].sum() else 0
        majority = int(((gr['lonely'] * 2) > gr['graded']).sum())
        gw = g[g['winner_known'] > 0]
        won_rate = (g['won'].sum() / g['winner_known'].sum()
                    if g['winner_known'].sum() else 0)
        lines += [
            f'### {label}: {len(g)} firms',
            f'- {len(got)} firms received picks; {g["picks"].sum()} picks '
            f'handed out (top 5 per firm and week), results known for '
            f'{g["graded"].sum()}',
            f'- **{g["lonely"].sum()} of the {g["graded"].sum()} checked '
            f'picks really ended with 0-1 bids ({hit:.0%})**',
            f'- {int((gr["lonely"] >= 1).sum())} of the {len(gr)} firms with '
            f'checked picks got at least one 0-1-bid pick; {majority} were '
            f'right on the majority of their picks',
            f'- **{g["won"].sum()} of the {g["winner_known"].sum()} picks '
            f'with a winner on record were eventually won by the very firm '
            f'they were handed to ({won_rate:.0%}, any bidder count)**; '
            f'{int((gw["won"] >= 1).sum())} of the {len(gw)} firms with '
            f'winner-checked picks won at least one',
            '']
    return lines


SCHEMA = 1


def build_payload(res, targets_csv=None):
    """The replay's results as ONE serialisable document — the single thing
    this program produces.

    The replay is the expensive step in this repository (33 minutes over the
    2026-08-11 store, 46 trained cutoffs), so it runs once and is rendered
    many times: `report` prints the operator's prose from this document,
    `trade_pages.py` slices the same document by trade into HTML, and anything
    later is another renderer rather than another replay. Nothing downstream
    re-derives a fact the replay knew; nothing upstream formats.

    What a lot row carries: was it examined while open (every row was), did we
    raise an alarm, its `cpv3`, and how many bids it eventually got —
    `n_tenders: null` when no award has published, so the "examined" and
    "results known" denominators are both readable off this list.

    `cpv3` is here and `trade` is NOT, deliberately: cpv3 is a raw store
    field, while a trade is a title word-match that `trades.txt` redefines. A
    payload carrying trades would silently disagree with `trades.txt` the day
    it changed; carrying cpv3 cannot. Consumers that group by trade join to
    the store themselves, which is what `trade_pages.py` does.
    """
    outcome, winners = res['outcome'], res['winners']
    lots = [{'procedure_id': lot[0], 'lot_id': lot[1], 'cpv3': cpv3,
             'flag': bool(lot in res['flagged']),
             'n_tenders': (int(outcome[lot]) if lot in outcome else None)}
            for lot, cpv3 in res['scored'].items()]

    subs = []
    for s, picks in res['sub_picks'].items():
        firm = res['subs'][s].get('name', s)
        f_name = str(firm).strip()
        subs.append({
            'sub_id': s, 'name': firm,
            'picks': [{'procedure_id': lot[0], 'lot_id': lot[1],
                       'week': p['week'], 'title': p['title'],
                       'buyer_name': p['buyer_name'], 'score': p['score'],
                       'n_tenders': (int(outcome[lot]) if lot in outcome
                                     else None),
                       'winner_known': lot in winners,
                       'won': f_name in winners.get(lot, ())}
                      for lot, p in sorted(picks.items(),
                                           key=lambda kv: kv[1]['week'])],
            'wins': own_win_rows(res, s, firm, picks),
        })
    payload = {
        'schema': SCHEMA,
        'generated': date.today().isoformat(),
        'model_tag': MODEL_TAG,
        'step_days': res['step_days'],
        'cutoffs_trained': res['n_cutoffs'],
        'n_lots': len(lots),
        'lots': lots,
        'subs': subs,
    }
    if targets_csv:
        payload['targets'] = target_rows(res, targets_csv)
    return payload


def own_win_rows(res, s, firm, picks):
    """Each of the firm's eventual wins, and whether the replayed market saw
    it — computed HERE, at replay time, because it needs the full awards frame
    that never enters the payload.

    FAIRNESS SPLIT (2026-08-10): a profile is built from awards published
    before each cutoff (as_of_profile), and an award publishes a median 84
    days after its tender. A pilot onboarded from its recent wins therefore
    replays its FIRST wins against a profile that does not exist yet — VLE and
    Polat read 0/8 and 0/6 here while today's gate admits 7/8 and 5/6 of the
    same wins. A win whose tender closed before the firm's first OTHER
    reference was published was never judged at all; counting it against the
    gate is grading chronology, not relevance. `refs_at_close` is what lets a
    renderer separate the two without re-reading the store.
    """
    aw = res['awards_full']
    wins = aw[aw['winner_names'].apply(
        lambda x: x is not None and firm in list(x))]
    # when did each of the firm's references become citable? — the
    # publication date of the AWARD notice that creates it
    ref_avail = sorted((str(w['publication_date'] or ''), str(w['procedure_id']))
                       for _, w in wins.iterrows())
    rows = []
    for _, w in wins.iterrows():
        lot = (w['procedure_id'], w['lot_id'])
        call_pub, deadline = res.get('lot_dates', {}).get(lot, ('', ''))
        anchor = deadline or call_pub
        rows.append({
            'awarded': str(w['publication_date'])[:10],
            'procedure_id': lot[0], 'lot_id': lot[1],
            'in_market': lot in res['sub_market'][s],
            'pick': lot in picks,
            'n_tenders': (int(w['n_tenders']) if pd.notna(w['n_tenders'])
                          else None),
            'refs_at_close': sum(1 for d, p in ref_avail
                                 if d and anchor and d <= anchor
                                 and p != str(w['procedure_id'])),
        })
    return sorted(rows, key=lambda r: (r['awarded'], r['procedure_id'],
                                       str(r['lot_id'])))


def report(payload):
    """Print the operator's prose. A pure function of the payload — every
    number here is also derivable by any other renderer, which is the point of
    the payload existing.
    """
    lots = payload['lots']
    graded = [r['n_tenders'] for r in lots if r['n_tenders'] is not None]
    base = np.mean([n <= 1 for n in graded]) if graded else 0
    graded_flags = [r['n_tenders'] for r in lots
                    if r['flag'] and r['n_tenders'] is not None]
    hit = (np.mean([n <= 1 for n in graded_flags])
           if graded_flags else float('nan'))
    lines = [f'# Forward backtest — {payload["model_tag"]} — generated '
             f'{payload["generated"]} '
             f'({payload["cutoffs_trained"]} cutoffs, '
             f'step {payload["step_days"]}d)',
             '',
             'Words used here: **examined** = the model looked at the tender while it was',
             'open · **alarm** = the model said "few bidders likely" · **pick** = one of',
             'the top-5 alarms handed to one firm for its market · **results known** =',
             'the award notice is published, so the true bid count is on public record ·',
             '**chance** = how often tenders end with 0-1 bids anyway, with no model ·',
             '**right** = share of checked alarms/picks that really ended with 0-1 bids ·',
             '**vs chance** = right ÷ chance ·',
             '**won** = the award notice names the very firm the pick was handed to as',
             'a winner, no matter how many bidders there were (own denominator: the',
             'award must name a winner, which is not the same set as "bid count known").',
             '',
             f'- Tenders examined while open: {len(lots)} '
             f'(results known for {len(graded)}; '
             f'{sum(1 for n in graded if n <= 1)} of those '
             f'ended with 0-1 bids anyway — chance {base:.0%})',
             f'- **Alarms raised on {sum(1 for r in lots if r["flag"])} '
             f'tenders; {sum(1 for n in graded_flags if n <= 1)} of the '
             f'{len(graded_flags)} checked alarms really ended with 0-1 bids '
             f'({hit:.0%}), {hit / base:.2f}x better than chance**'
             if graded_flags else '- no checked alarms yet',
             '']
    lines += flag_matrix(payload)
    lines += trade_table(payload)
    lines += target_lines(payload)
    for sub in payload['subs']:
        firm, picks = sub['name'], sub['picks']
        lines += [f'## {firm}', '']
        n_graded = sum(1 for p in picks if p['n_tenders'] is not None)
        n_lonely = sum(1 for p in picks
                       if p['n_tenders'] is not None and p['n_tenders'] <= 1)
        won = sum(1 for p in picks if p['won'])
        n_winner_known = sum(1 for p in picks if p['winner_known'])
        lines += [f'- Picks across the replay: {len(picks)} (results known '
                  f'for {n_graded}, of which {n_lonely} ended with 0-1 bids)',
                  f'- Picks eventually won by {firm} themselves: {won} '
                  f'of {n_winner_known} with a winner on record '
                  f'(any bidder count)', '']
        for p in picks:
            res_s = ('outcome pending' if p['n_tenders'] is None
                     else f'{p["n_tenders"]} bid(s)')
            if p['won']:
                res_s += ', WON by them'
            lines.append(f'  - {p["week"]}: {str(p["title"])[:60]!r} '
                         f'[{str(p["buyer_name"])[:35]}] -> {res_s}')
        # recall of the customer's own eventual wins; see own_win_rows for the
        # fairness split the `refs_at_close` column carries
        rows = sub['wins']
        vis = sum(1 for r in rows if r['in_market'])
        starved = sum(1 for r in rows
                      if not r['in_market'] and r['refs_at_close'] == 0)
        lines += ['', f'- Own wins visible in the replayed market: '
                  f'{vis}/{len(rows)} (as picks: '
                  f'{sum(1 for r in rows if r["pick"])})']
        if starved:
            gateable = len(rows) - starved
            lines.append(
                f'- {starved} of the misses closed before the firm had a '
                f'single published reference (new-customer bootstrap: the '
                f'gate never ran) — gate-attributable: {vis}/{gateable}')
        for r in rows:
            nb = '?' if r['n_tenders'] is None else r['n_tenders']
            lines.append(f'  - win awarded {r["awarded"]} ({nb} bids): '
                         f'in market={r["in_market"]}, pick={r["pick"]}, '
                         f'refs at close={r["refs_at_close"]}')
        lines.append('')
    print('\n'.join(lines))


class BadDocument(Exception):
    """A replay document a renderer cannot use. The message says why."""


LOT_FIELDS = ('procedure_id', 'lot_id', 'cpv3', 'flag', 'n_tenders')


def validate(payload):
    """Check a document before any renderer touches it. -> payload, or raise
    `BadDocument` with a sentence a person can act on.

    Valid JSON is not a valid document, and that is the case worth guarding:
    an older run, a truncated file, or somebody's unrelated `.json` all parse
    fine and then raise `KeyError` in the middle of rendering — which on the
    public-site path used to mean a whole build died over an optional section.
    Fail here, once, with a reason, and let each caller decide what to do
    about it (`TRADE_PAGES.md` §6d): the site downgrades to no claim and says
    so, `--render` stops.

    Only what a renderer actually indexes is checked. This is not a schema
    language and should not grow into one.
    """
    if not isinstance(payload, dict):
        raise BadDocument(f'not a replay document: expected a JSON object, '
                          f'got {type(payload).__name__}')
    schema = payload.get('schema')
    if schema is None:
        raise BadDocument('not a replay document: no "schema" field. A file '
                          'written before 2026-08-11 (or by something else) '
                          'cannot be rendered — re-run `python rewind_all.py`.')
    if not isinstance(schema, int) or schema > SCHEMA:
        raise BadDocument(f'document schema {schema!r} is newer than this '
                          f'rewind_all.py understands (schema {SCHEMA}) — '
                          f'update the code, or re-run the replay with it.')
    lots = payload.get('lots')
    if not isinstance(lots, list):
        raise BadDocument('document has no "lots" list — nothing to render.')
    for i, r in enumerate(lots):
        if not isinstance(r, dict):
            raise BadDocument(f'lot row {i} is not an object')
        missing = [k for k in LOT_FIELDS if k not in r]
        if missing:
            raise BadDocument(
                f'lot row {i} is missing {", ".join(missing)} — this document '
                f'was written by an older rewind_all.py; re-run the replay.')
    for k in ('generated', 'model_tag', 'step_days', 'cutoffs_trained'):
        if k not in payload:
            raise BadDocument(f'document is missing "{k}" — every figure has '
                              f'to be able to state which run it came from.')
    if not isinstance(payload.get('subs'), list):
        raise BadDocument('document has no "subs" list')
    return payload


def read_payload(path):
    """Load and validate a replay document from a path, or stdin for `-`."""
    try:
        text = (sys.stdin.read() if path == '-'
                else Path(path).read_text(encoding='utf-8'))
    except OSError as e:
        raise BadDocument(f'cannot read {path}: {e}') from e
    try:
        payload = json.loads(text)
    except ValueError as e:
        raise BadDocument(f'{path} is not valid JSON: {e}') from e
    return validate(payload)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', default=config.data_root())
    ap.add_argument('--step', type=int, default=7, help='days between cutoffs')
    ap.add_argument('--sub', action='append', default=None,
                    help='subscription id(s) to replay (default: all gated)')
    ap.add_argument('--targets', default=None,
                    help='outreach targets.csv: deliver + grade weekly picks '
                         'for every firm in it across the replay')
    ap.add_argument('--render', metavar='PATH', default=None,
                    help='do not replay: read a previous run\'s document '
                         '(`-` for stdin) and print the report from it')
    ap.add_argument('--out', metavar='PATH', default='-',
                    help='where the document goes; `-` (the default) is '
                         'stdout. No conventional filename either way — the '
                         'caller names it.')
    args = ap.parse_args()
    if args.render:
        # The operator asked to render THIS document. A bad one is an error
        # with a reason, never an empty report — unlike the site build, which
        # has market pages to protect and downgrades instead.
        try:
            report(read_payload(args.render))
        except BadDocument as e:
            print(f'[rewind_all] {e}', file=sys.stderr)
            return 2
        return
    # Fail fast rather than wait: this is a manual run with its operator
    # watching, and the alternative is a replay that silently queues behind a
    # 28-minute cycle and then competes with the next thing. The cycle waits
    # for us; we never wait for it (heavy_lock, property 4).
    try:
        with heavy_lock.held(args.data_dir, 'the replay'):
            return _replay_to_document(args)
    except heavy_lock.Busy as e:
        print(f'[rewind_all] {e}', file=sys.stderr)
        return 2


def _replay_to_document(args):
    # STDOUT IS THE DOCUMENT, so nothing else may write to it. `calibrate`,
    # `subscriptions` and `embed` all print progress with a bare `print`, and
    # one such line in the middle of the stream is a corrupt JSON file that
    # took half an hour to produce. Rather than police every module (and every
    # module added later), everything from here to the dump runs with
    # `sys.stdout` bound to stderr: `print` resolves it per call, so every
    # stray diagnostic lands with the deliberate ones and the real stream
    # stays untouched until the dump. The guard starts BEFORE the default
    # `--sub` resolution — `subscriptions.load` prints retired-field warnings,
    # and that exact line corrupted a receipt run on 2026-08-12.
    doc_stream = sys.stdout
    try:
        sys.stdout = sys.stderr
        sub_ids = args.sub
        if sub_ids is None:
            sub_ids = [s0['sub_id'] for s0
                       in subscriptions.load(args.data_dir,
                                             date.today().isoformat())
                       if s0.get('profile_refs')]
        res = replay(args.data_dir, args.step, set(sub_ids))
        payload = build_payload(res, targets_csv=args.targets)
    finally:
        sys.stdout = doc_stream
    if args.out == '-':
        json.dump(payload, doc_stream)
        doc_stream.flush()
    else:
        Path(args.out).write_text(json.dumps(payload), encoding='utf-8')
    print(f'[rewind_all] {payload["n_lots"]} lots examined, '
          f'{sum(1 for r in payload["lots"] if r["n_tenders"] is not None)} '
          f'with a published result'
          + ('' if args.out == '-' else f' -> {args.out}'), file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
