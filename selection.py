"""The selection: slice -> gate -> rank -> cap. One copy, for every caller.

(REFACTOR.md phase 4 calls this module `select.py`. That name cannot be
used: `select` is a standard-library module, and a repo-root `select.py`
shadows it for the whole process — `import subprocess` then dies inside
`selectors.py` with `module 'select' has no attribute 'select'`. Receipt,
in the deployed image: `docker run tendermining:latest python -c "import
subprocess"` with this file named `select.py` fails, i.e. the scheduler
would not start at all.)

`delivering.deliver` decides what a customer sees this week; `rewind_all.replay`
measures how good those decisions were. Until this module existed they were
two implementations of the same four steps, and they had already drifted
(REFACTOR.md defect 1) — so the backtest measured a selection that never
shipped. Both now call `for_sub`, which makes the measurement true by
construction rather than by review.

No I/O, no HTML, no clock of its own: `today` is passed in, because the
rewind's "today" is a past cutoff. What stays outside is profile
*construction* (the caller knows whether it wants the customer's live
profile or their as-of one) and rendering.

A row is any mapping with `procedure_id`, `lot_id`, `score` and the market
fields (`cpv_main`/`cpv3`, `place_nuts3`, `deadline_date`); `flag` carries
the competition verdict, which is the model's output and not this module's
business. Prediction-ledger dicts and rows built from the store both
qualify.
"""

from dataclasses import dataclass, field

import subscriptions


def lot_key(row):
    """The lot's identity — the key of every per-lot side table below."""
    return row['procedure_id'], row['lot_id']


@dataclass
class SliceResult:
    """What one subscription gets out of one cycle's scored lots.

    `market` is the annex and `picks` is the report, and the difference
    between them is deliberate: a lot too close to its deadline to bid on
    still deserves a printed verdict, so the deadline promise narrows the
    recommendation, never the market view.
    """

    market: list = field(default_factory=list)      # in slice + gate-passed
    ranked: list = field(default_factory=list)      # market ∩ deadline promise
    picks: list = field(default_factory=list)       # ranked ∩ flag, capped
    borderline: list = field(default_factory=list)  # near-misses under the gate
    judged: dict = field(default_factory=dict)      # lot_key -> (text, code, why, hard)


def for_sub(sub, rows, today, gate=None, profile=None):
    """Everything one subscription selects out of `rows`, as of `today`.

    Without a profile the gate step is skipped and the slice is the market
    filter alone — the ungated delivery path, which must stay byte-identical
    for customers who never asked for a profile.
    """
    res = SliceResult()
    for row in rows:
        if not subscriptions.in_market(sub, row):
            continue
        if profile is None:
            res.market.append(row)
            continue
        import relevance as rel
        ok, near, text, code, why, hard = rel.judge(gate, profile, row)
        res.judged[lot_key(row)] = (text, code, why, hard)
        if ok:
            res.market.append(row)
        elif near:
            res.borderline.append(row)
    res.ranked = sorted(
        (r for r in res.market if subscriptions.deadline_ok(sub, r, today)),
        key=lambda r: -r['score'])
    # ONE bar (RELEVANCE.md decision 2026-08-05): passing the gate means
    # recommendable — a pick just needs the competition flag on top
    res.picks = [r for r in res.ranked
                 if r.get('flag')][:subscriptions.max_picks(sub)]
    return res
