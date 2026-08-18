"""TenderMining delivery — the sending: once a week, from what the last
cycle wrote. The one command that reaches a person.

    python deliver.py run                 # mail every active customer
    python deliver.py run --no-mail       # write every report to disk, mail nobody
    python deliver.py run --max-age 3d    # accept predictions up to 3 days old

What it does, in order (RUNBOOK 1): read the delivering champion's latest
prediction per lot still open from the ledger (`predicting.open_scored`) ->
refuse if the newest is older than --max-age -> refuse if the gate guard
says the resolved gate is not the registered one (PARAMETERS.md 8.3) ->
learn each customer's own wins as references (feedback) -> slice per
subscription, gate, render, mail, append the delivery rows, turn the
trial-ask clock (`delivering.deliver`).

What it does NOT do: download, embed, grade, train, predict, simulate. It
holds no model in memory and never will; a delivery that finds its
predictions stale says so and stops, and the fix is `python cycle.py run`,
never something this file does on its own. That is the whole point of the
split (2026-08-18): the update runs any day; the sending runs on the
schedule; neither can be mistaken for the other.

Idempotent per day: delivery rows dedup by (customer, lot, day), so a second
run the same day finds everything already on record and sends nothing.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

import config
import cycle
import delivering
import heavy_lock
import knobs
import predicting
import single_bidder as sb
import util


class Stale(RuntimeError):
    """The newest prediction is older than the delivery accepts."""


def check_fresh(newest_ts, max_age, now=None):
    """Raise Stale unless `newest_ts` (ISO, from a prediction row) is within
    `max_age` (Nd/Nw/Nm) of now. A missing timestamp is stale by definition —
    there is nothing to deliver from."""
    now = now or util.now_utc()
    if not newest_ts:
        raise Stale('no prediction rows for the delivering model — '
                    'run `python cycle.py run` first')
    ts = datetime.fromisoformat(str(newest_ts))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = now - ts
    limit = util.parse_window(max_age)
    if age > limit:
        raise Stale(f'newest prediction is {age.days}d {age.seconds // 3600}h old '
                    f'(written {newest_ts}); --max-age is {max_age}. '
                    f'Run `python cycle.py run` first, or pass a larger --max-age '
                    f'if these predictions are what you mean to send.')
    return age


def run(paths, args):
    print(f'[config] data root: {config.describe(paths.data)}')
    import relevance as rel
    print(f'[config] gate: {rel.DEFAULT_CONFIG.describe()}')

    tenders, _ = sb.load_with_roles(paths.store_tenders)
    awards, _ = sb.load_with_roles(paths.store_awards)
    _, aw, _ = sb.assemble(tenders, awards)

    scored, newest = predicting.open_scored(paths, tenders, aw)
    age = check_fresh(newest, args.max_age)
    print(f'[deliver] predictions are {age.days}d {age.seconds // 3600}h old — fresh enough '
          f'(--max-age {args.max_age})')

    # The gate guard (PARAMETERS.md 8.3): a resolved gate configuration that
    # is not the registered one stops delivery. The cycle printed the same
    # lines into the report; this is the process that actually sends, so it
    # decides again for itself.
    gate_ok, guard_lines = knobs.gate_guard(paths)
    for line in guard_lines:
        print(line)
    if not gate_ok:
        print('[deliver] GATE MISMATCH — nothing sent (see above)')
        return 1

    delivering.learn_references(paths, tenders, awards, args)
    delivering.deliver(paths, scored, args)
    print('[done]')
    return 0


def cmd_run(args):
    paths = util.Paths(args.data_dir, args.models_dir)
    # Behind the heavy lock, WAITING: a delivery scheduled 90 minutes after
    # the cycle must not read predictions the cycle is still writing, and a
    # cycle that overran is a reason to send later, not to send from last
    # week's rows. Bounded — heavy_lock raises rather than hanging.
    with heavy_lock.held(paths.data, 'the delivery', wait=3600):
        return run(paths, args)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    run_ = sub.add_parser('run', help='render and send every active customer')
    cycle.add_common_args(run_)
    run_.add_argument('--max-age', default='1d', dest='max_age', metavar='NdNwNm',
                      help='refuse when the newest prediction is older than this (default 1d)')
    run_.add_argument('--no-mail', dest='mail', action='store_false',
                      help='write the reports, mail nobody (a dry run; what '
                           'preview_report.py wants)')
    run_.set_defaults(func=cmd_run)
    args = ap.parse_args(argv)
    try:
        return args.func(args) or 0
    except Stale as e:
        print(f'[deliver] STALE — nothing sent: {e}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
