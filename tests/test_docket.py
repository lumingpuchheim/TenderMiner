"""PARAMETERS.md 11: the docket — the program files the questions, backplay
measures them at night, the weekly line reads the measurements, and a
question closes itself and hands the bucket to the next knob.

No harness runs here: `backplay.measure` is stubbed to return judge-shaped
payloads, so what is tested is the rotation, the ledger round-trip, the
change detector and the closing rules.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backplay                                                   # noqa: E402
import knobs                                                      # noqa: E402
import ledger                                                     # noqa: E402
import util                                                       # noqa: E402

T_A = knobs.Tunable('mod.A', 'gate', 1, 4, 1, 'Is A right?')
T_B = knobs.Tunable('mod.B', 'gate', 0.775, 0.875, 0.025, 'Is B right?')
T_C = knobs.Tunable('mod.C', 'delivery', 5, 7, 1, 'Is C right?')
TUNABLES = (T_A, T_B, T_C)
CURRENT = {'mod.A': 2, 'mod.B': 0.825, 'mod.C': 5}


def payload(recall, leakage, n_pos=400, fingerprint='cand000000'):
    return {'gate_fingerprint': fingerprint, 'counts': {'n_pos': n_pos, 'n_neg': 900},
            'configurations': [{'name': 'evidence + K>=2 + band p=0.0 (committed)',
                                'recall': recall, 'leakage': leakage}]}


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        import gc
        self.addCleanup(gc.collect)
        self.paths = util.Paths(self.tmp.name, Path(self.tmp.name) / 'models')
        # the tunables read their current value from a module; here from a dict
        p = mock.patch.object(knobs.Tunable, 'current', lambda t: CURRENT[t.knob])
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(knobs, 'TUNABLES', TUNABLES)
        p2.start()
        self.addCleanup(p2.stop)
        p3 = mock.patch.object(backplay, 'evidence_stamp', lambda paths: 'stamp-1')
        p3.start()
        self.addCleanup(p3.stop)

    def docket(self, today='2026-08-16'):
        return knobs.docket(self.paths, today, TUNABLES, filed=())


class TheLadder(unittest.TestCase):
    def test_float_ladder_contains_the_codes_value_exactly(self):
        self.assertEqual(T_B.grid(), (0.775, 0.8, 0.825, 0.85, 0.875))
        self.assertIn(0.825, T_B.grid())

    def test_int_ladder(self):
        self.assertEqual(T_A.grid(), (1, 2, 3, 4))

    def test_a_value_off_the_ladder_refuses(self):
        with self.assertRaises(ValueError):
            knobs.question_from(T_A, '2026-08-16', current=9)

    def test_the_real_tunables_hold_values_on_their_ladders(self):
        """The one test that imports the real modules: the code's constants
        must sit on the program's ladders, or the docket cannot open them."""
        for t in knobs.TUNABLES:
            q = knobs.question_from(t, '2026-08-16')
            self.assertIn(q.current, q.grid)
            self.assertTrue(q.neighbours(), t.knob)


class TheRotation(Sandbox):
    def test_one_open_question_per_bucket_the_first_of_the_rotation(self):
        qs = self.docket()
        self.assertEqual([q.knob for q in qs], ['mod.A', 'mod.C'])
        self.assertEqual(qs[0].current, 2)
        self.assertEqual(qs[0].grid, (1, 2, 3, 4))
        self.assertEqual(qs[0].stop, '2026-10-11')       # opened + STOP_WEEKS

    def test_the_docket_is_persisted_and_stable_across_calls(self):
        first = self.docket('2026-08-16')
        second = self.docket('2026-08-23')
        self.assertEqual([q.opened for q in second], [q.opened for q in first])

    def test_a_hand_filed_question_takes_its_buckets_slot(self):
        filed = knobs.Question(id='hand', knob='x.Y', bucket='gate', question='Y?',
                               metric='recall', benchmark='b', grid=(1, 2), current=1,
                               opened='2026-08-01', stop='2026-12-01')
        qs = knobs.docket(self.paths, '2026-08-16', TUNABLES, filed=(filed,))
        self.assertEqual([q.knob for q in qs], ['x.Y', 'mod.C'])

    def test_closing_hands_the_bucket_to_the_next_knob_and_wraps(self):
        q = self.docket()[0]
        knobs.close_question(self.paths, q, 'flat twice', 'nothing better', '2026-08-30')
        qs = self.docket('2026-08-31')
        self.assertEqual(qs[0].knob, 'mod.B')
        self.assertEqual(qs[0].opened, '2026-08-31')
        knobs.close_question(self.paths, qs[0], 'stop date reached', '-', '2026-10-30')
        self.assertEqual(self.docket('2026-10-31')[0].knob, 'mod.A')   # wraps
        self.assertEqual(len(knobs.read_docket(self.paths)['closed']), 2)


class BackplayMeasuresTheDocket(Sandbox):
    def _measure(self, table):
        def fake(paths, value, harness='judge', knob=None, timeout=0):
            self.calls.append((value, knob))
            return table[value]
        self.calls = []
        return mock.patch.object(backplay, 'measure', fake)

    def test_baseline_and_candidates_are_recorded_with_their_numbers(self):
        table = {2: payload(.80, .010), 1: payload(.90, .040), 3: payload(.70, .005)}
        with self._measure(table):
            lines = backplay.run(self.paths, self.docket()[:1], today='2026-08-16')
        self.assertEqual(self.calls, [(2, None), (1, 'A'), (3, 'A')])
        rows = ledger.read(self.paths.ledger_home, 'backplays')
        self.assertEqual([r['role'] for r in rows], ['current', 'candidate', 'candidate'])
        by = {r['value']: r for r in rows}
        self.assertEqual(by[2]['metric'], .80)
        self.assertTrue(by[1]['rejected'])                # leaks 4% > 2.2%
        self.assertFalse(by[3]['rejected'])
        self.assertEqual(by[3]['stamp'], 'stamp-1')
        self.assertIn('REJECTED', '\n'.join(lines))

    def test_the_weekly_line_reads_what_backplay_measured(self):
        table = {2: payload(.80, .010), 1: payload(.90, .040), 3: payload(.95, .005)}
        with self._measure(table):
            backplay.run(self.paths, self.docket()[:1], today='2026-08-16')
        line = knobs.weekly(self.paths, '2026-08-17')[0]
        self.assertIn('move up', line)                    # 3's lower bound clears 2's upper
        self.assertIn('backplay rejected 1', line)
        self.assertIn('[2] 0.800', line)
        self.assertIn('3 ok 0.950', line)

    def test_nothing_moved_means_nothing_re_measured(self):
        table = {2: payload(.80, .010), 1: payload(.90, .040), 3: payload(.95, .005)}
        with self._measure(table):
            backplay.run(self.paths, self.docket()[:1], today='2026-08-16')
            n = len(self.calls)
            lines = backplay.run(self.paths, self.docket()[:1], today='2026-08-17')
        self.assertEqual(len(self.calls), n)
        self.assertIn('nothing moved', lines[0])

    def test_a_moved_stamp_or_force_re_measures(self):
        table = {2: payload(.80, .010), 1: payload(.90, .040), 3: payload(.95, .005)}
        with self._measure(table):
            backplay.run(self.paths, self.docket()[:1], today='2026-08-16')
            n = len(self.calls)
            backplay.run(self.paths, self.docket()[:1], today='2026-08-17', force=True)
            self.assertEqual(len(self.calls), 2 * n)
            with mock.patch.object(backplay, 'evidence_stamp', lambda p: 'stamp-2'):
                backplay.run(self.paths, self.docket()[:1], today='2026-08-18')
            self.assertEqual(len(self.calls), 3 * n)

    def test_a_later_survival_lifts_an_earlier_rejection(self):
        with self._measure({2: payload(.80, .010), 1: payload(.90, .040), 3: payload(.7, .01)}):
            backplay.run(self.paths, self.docket()[:1], today='2026-08-16')
        self.assertIn(1, backplay.rejected_values(self.paths, 'auto:mod.A', '2026-08-17'))
        with self._measure({2: payload(.80, .010), 1: payload(.90, .010), 3: payload(.7, .01)}):
            backplay.run(self.paths, self.docket()[:1], today='2026-08-23', force=True)
        self.assertNotIn(1, backplay.rejected_values(self.paths, 'auto:mod.A', '2026-08-24'))

    def test_every_neighbour_rejected_closes_the_question(self):
        table = {2: payload(.80, .010), 1: payload(.90, .040), 3: payload(.95, .050)}
        with self._measure(table):
            backplay.run(self.paths, self.docket()[:1], today='2026-08-16')
        line = knobs.weekly(self.paths, '2026-08-17')[0]
        self.assertIn('closed', line)
        self.assertIn('every neighbour rejected', line)
        self.assertEqual(self.docket('2026-08-24')[0].knob, 'mod.B')

    def test_flat_twice_closes_and_the_show_command_tells_the_story(self):
        table = {2: payload(.80, .010), 1: payload(.79, .010), 3: payload(.81, .010)}
        with self._measure(table):
            backplay.run(self.paths, self.docket()[:1], today='2026-08-16')
        knobs.weekly(self.paths, '2026-08-17')
        line = knobs.weekly(self.paths, '2026-08-24')[0]
        self.assertIn('flat twice', line)
        body = '\n'.join(backplay.show(self.paths, '2026-08-25'))
        self.assertIn('mod.B live since 2026-08-25', body)
        self.assertIn('closed:', body)
        self.assertIn('mod.A 2026-08-16..2026-08-24', body)
        self.assertIn('current', body)


if __name__ == '__main__':
    unittest.main()
