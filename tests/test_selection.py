"""Selection tests — the four steps every customer's week goes through.

    python -m unittest discover -t . -s tests        # from the repository root
    python tests/test_selection.py                   # or directly

`selection.for_sub` is the one copy of slice -> gate -> rank -> cap
(REFACTOR.md phase 4a). It is called by `delivering.deliver`, which ships, and by
`rewind_all.replay`, which measures — so these assertions are what makes the
backtest's number a statement about the shipped system rather than about a
replica of it.

Same properties as the other test files: no real data, stdlib `unittest`, and
behaviours rather than implementations. The gate is a stub here on purpose —
what `relevance.judge` decides is `relevance.py`'s business and has its own
tests; what selection owes is that a pass, a near-miss and a rejection each
land in the right list.

The assertions worth naming, because each one is a way a customer gets the
wrong report:

* the deadline promise narrows the RECOMMENDATION but never the market view
  (the annex must still print a verdict for a lot that is too close to bid on),
* a lot the gate rejected is out of the market entirely — it must not reappear
  in the annex,
* `judged` is keyed by lot identity, not object identity, so a verdict cannot
  land next to another lot's pick,
* an ungated subscription never touches the gate at all.
"""

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import selection

TODAY = date(2026, 8, 12)


def row(procedure='p1', lot_id='L1', score=0.9, flag=True,
        cpv='45000000', nuts='DE211', deadline='2026-09-30', **over):
    r = {'procedure_id': procedure, 'lot_id': lot_id, 'score': score,
         'flag': flag, 'cpv_main': cpv, 'place_nuts3': nuts,
         'deadline_date': deadline, 'title': 'Estricharbeiten',
         'buyer_name': 'Stadt X'}
    r.update(over)
    return r


class StubGate:
    """A gate whose verdict is dictated per lot: 'pass', 'near' or 'out'."""

    def __init__(self, verdicts=None, default='pass'):
        self.verdicts = verdicts or {}
        self.default = default
        self.asked = []

    def verdict_for(self, r):
        return self.verdicts.get((r['procedure_id'], r['lot_id']), self.default)


def stub_judge(gate, profile, scored_row, config=None):
    gate.asked.append((scored_row['procedure_id'], scored_row['lot_id']))
    v = gate.verdict_for(scored_row)
    text = 0.7 if v == 'pass' else 0.4
    return (v == 'pass', v == 'near', text, 0.5, ('ref', 'ein Auftrag'), 0.5)


class SelectionTest(unittest.TestCase):

    def setUp(self):
        import relevance
        self._real_judge = relevance.judge
        relevance.judge = stub_judge
        self.addCleanup(setattr, relevance, 'judge', self._real_judge)

    # ---------------------------------------------------------- the slice

    def test_out_of_market_lots_never_appear(self):
        sub = {'sub_id': 's', 'cpv_prefixes': ['45'], 'nuts_prefixes': ['DE2']}
        rows = [row(lot_id='in'),
                row(lot_id='wrong-cpv', cpv='79000000'),
                row(lot_id='wrong-nuts', nuts='DE500')]
        res = selection.for_sub(sub, rows, TODAY)
        self.assertEqual([r['lot_id'] for r in res.market], ['in'])
        self.assertEqual([r['lot_id'] for r in res.picks], ['in'])

    def test_ungated_subscription_never_consults_the_gate(self):
        gate = StubGate()
        sub = {'sub_id': 's', 'cpv_prefixes': ['45']}
        res = selection.for_sub(sub, [row()], TODAY, gate=gate, profile=None)
        self.assertEqual(gate.asked, [])
        self.assertEqual(res.judged, {})
        self.assertEqual(len(res.picks), 1)

    # ----------------------------------------------------------- the gate

    def test_gate_verdicts_land_in_the_right_lists(self):
        gate = StubGate({('p1', 'yes'): 'pass', ('p1', 'maybe'): 'near',
                         ('p1', 'no'): 'out'})
        rows = [row(lot_id='yes'), row(lot_id='maybe'), row(lot_id='no')]
        res = selection.for_sub({'sub_id': 's'}, rows, TODAY, gate=gate,
                                profile={'any': 'profile'})
        self.assertEqual([r['lot_id'] for r in res.market], ['yes'])
        self.assertEqual([r['lot_id'] for r in res.borderline], ['maybe'])
        self.assertEqual([r['lot_id'] for r in res.picks], ['yes'])

    def test_a_rejected_lot_is_out_of_the_annex_too(self):
        gate = StubGate(default='out')
        res = selection.for_sub({'sub_id': 's'}, [row()], TODAY, gate=gate,
                                profile={'any': 'profile'})
        self.assertEqual(res.market, [])

    def test_judged_is_keyed_by_lot_identity(self):
        """A side table keyed by object identity puts one lot's reason next to
        another lot's pick as soon as a row is copied or re-read."""
        gate = StubGate()
        rows = [row(lot_id='A'), row(lot_id='B')]
        res = selection.for_sub({'sub_id': 's'}, rows, TODAY, gate=gate,
                                profile={'any': 'profile'})
        self.assertEqual(set(res.judged), {('p1', 'A'), ('p1', 'B')})
        # a fresh copy of the same lot still finds its own verdict
        self.assertIn(selection.lot_key(dict(rows[0])), res.judged)

    # -------------------------------------------------- deadline and rank

    def test_deadline_promise_narrows_picks_but_not_the_market(self):
        """The annex needs a verdict for a lot too close to bid on; the
        recommendation must not name it."""
        sub = {'sub_id': 's', 'min_deadline_days': 14}
        rows = [row(lot_id='roomy', deadline='2026-09-30'),
                row(lot_id='tight', deadline='2026-08-14')]
        res = selection.for_sub(sub, rows, TODAY)
        self.assertEqual({r['lot_id'] for r in res.market}, {'roomy', 'tight'})
        self.assertEqual([r['lot_id'] for r in res.picks], ['roomy'])

    def test_no_deadline_promise_keeps_every_lot_recommendable(self):
        """Six of eight live subscriptions promise nothing; the rewind used to
        impose 14 days on them anyway (REFACTOR.md phase 4a)."""
        rows = [row(lot_id='tight', deadline='2026-08-13')]
        self.assertEqual(len(selection.for_sub({'sub_id': 's'}, rows, TODAY).picks), 1)
        self.assertEqual(len(selection.for_sub(
            {'sub_id': 's', 'min_deadline_days': 14}, rows, TODAY).picks), 0)

    def test_unknown_deadline_fails_a_promise_that_was_made(self):
        rows = [row(lot_id='mystery', deadline=None)]
        self.assertEqual(selection.for_sub(
            {'sub_id': 's', 'min_deadline_days': 14}, rows, TODAY).picks, [])
        self.assertEqual(len(selection.for_sub({'sub_id': 's'}, rows, TODAY).picks), 1)

    def test_picks_are_ranked_by_score(self):
        rows = [row(lot_id='low', score=0.51), row(lot_id='high', score=0.99),
                row(lot_id='mid', score=0.75)]
        res = selection.for_sub({'sub_id': 's'}, rows, TODAY)
        self.assertEqual([r['lot_id'] for r in res.picks], ['high', 'mid', 'low'])

    # ------------------------------------------------------------ the cap

    def test_max_picks_caps_the_recommendation(self):
        rows = [row(lot_id=f'L{i}', score=0.9 - i / 100) for i in range(9)]
        res = selection.for_sub({'sub_id': 's', 'max_picks': 3}, rows, TODAY)
        self.assertEqual([r['lot_id'] for r in res.picks], ['L0', 'L1', 'L2'])
        self.assertEqual(len(res.ranked), 9, 'the cap must not shrink the slice')

    def test_only_flagged_lots_are_recommended(self):
        """Passing the gate means relevant; a pick needs the competition
        verdict on top (RELEVANCE.md decision 2026-08-05)."""
        rows = [row(lot_id='crowded', flag=False, score=0.99),
                row(lot_id='lonely', flag=True, score=0.60)]
        res = selection.for_sub({'sub_id': 's'}, rows, TODAY)
        self.assertEqual([r['lot_id'] for r in res.picks], ['lonely'])
        self.assertEqual(len(res.ranked), 2, 'the annex still ranks both')


if __name__ == '__main__':
    unittest.main()
