"""PARAMETERS.md 8: the knob protocol's two halves — propose, and block.

The verdict grid is the substance here, so it is tested against synthetic
sweeps rather than a real one: a sweep that needs the embedding model is not
a unit test, and the point of these cases is which verdict a shape of numbers
earns.
"""
import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import knobs                                                     # noqa: E402
import ledger                                                    # noqa: E402
import util                                                      # noqa: E402

Q = knobs.Question(
    id='nomination-bar', knob='evidence.NOMINATION_BAR', bucket='gate',
    question='Does a lower nomination bar buy recall without breaching the leakage bar?',
    metric='recall', benchmark='abc123abc123',
    grid=(0.50, 0.55, 0.60), current=0.55,
    opened='2026-08-01', stop='2026-11-30')


def sweep(**by_value):
    """{value: (metric, n[, leakage])} -> the rows a question's `run` returns."""
    rows = []
    for value, spec in by_value.items():
        metric, n = spec[0], spec[1]
        row = {'value': float(value.replace('_', '.').lstrip('v')),
               'metric': metric, 'n': n}
        if len(spec) > 2:
            row['leakage'] = spec[2]
        rows.append(row)
    return rows


class Declaration(unittest.TestCase):
    def test_the_shipped_declaration_is_empty_and_valid(self):
        """No filed question today — every knob is frozen (§8.1). If this ever
        fails because LIVE grew, the register's §2.7 count moved with it."""
        self.assertEqual(knobs.LIVE, ())
        self.assertTrue(knobs._validate())

    def test_two_live_questions_in_one_bucket_are_refused(self):
        """§8.1's one-live-knob-per-bucket rule, which is Google's layering
        idea at this scale: never move two knobs of one model at once."""
        with self.assertRaises(ValueError):
            knobs._validate([Q, dataclasses.replace(Q, id='second')])

    def test_a_current_value_off_the_grid_is_refused(self):
        bad = dataclasses.replace(Q, current=0.575)
        with self.assertRaises(ValueError):
            knobs._validate([bad])

    def test_a_stop_date_before_opening_is_refused(self):
        bad = dataclasses.replace(Q, stop='2026-07-01')
        with self.assertRaises(ValueError):
            knobs._validate([bad])

    def test_a_question_must_be_a_question(self):
        bad = dataclasses.replace(Q, question='lower the bar')
        with self.assertRaises(ValueError):
            knobs._validate([bad])

    def test_only_the_neighbours_are_candidates(self):
        """§8.2: one grid step per cycle, never a jump to the sweep optimum."""
        self.assertEqual(Q.neighbours(), [0.50, 0.60])
        edge = dataclasses.replace(Q, current=0.50)
        self.assertEqual(edge.neighbours(), [0.55])


class Verdicts(unittest.TestCase):
    def test_a_clearly_better_neighbour_earns_one_step(self):
        rows = sweep(v0_50=(0.40, 400), v0_55=(0.50, 400), v0_60=(0.75, 400))
        v, detail = knobs.verdict(Q, rows, '2026-08-16')
        self.assertEqual(v, 'move up')
        self.assertIn('0.55 -> 0.6', detail)

    def test_the_step_can_go_down(self):
        rows = sweep(v0_50=(0.75, 400), v0_55=(0.50, 400), v0_60=(0.40, 400))
        v, _ = knobs.verdict(Q, rows, '2026-08-16')
        self.assertEqual(v, 'move down')

    def test_overlapping_intervals_are_flat_not_a_move(self):
        """The whole point of quoting an interval: 0.52 vs 0.50 on 400 cases
        is not a finding, and a protocol that moved on it would wander."""
        rows = sweep(v0_50=(0.49, 400), v0_55=(0.50, 400), v0_60=(0.52, 400))
        v, detail = knobs.verdict(Q, rows, '2026-08-16')
        self.assertEqual(v, 'flat')
        self.assertIn('no neighbour', detail)

    def test_a_thin_denominator_holds_however_good_it_looks(self):
        rows = sweep(v0_50=(0.10, 12), v0_55=(0.50, 12), v0_60=(0.95, 12))
        v, detail = knobs.verdict(Q, rows, '2026-08-16')
        self.assertEqual(v, 'hold (underpowered)')
        self.assertIn('need 30', detail)

    def test_the_hard_bar_bars_a_winner(self):
        """Precision over recall (the operator's standing rule): a candidate
        above 2.2% leakage is not proposed, whatever recall it buys."""
        rows = sweep(v0_50=(0.40, 400, 0.01), v0_55=(0.50, 400, 0.015),
                     v0_60=(0.95, 400, 0.031))
        v, detail = knobs.verdict(Q, rows, '2026-08-16')
        self.assertEqual(v, 'flat')
        self.assertIn('barred: 0.6 leaks 3.1%', detail)

    def test_a_candidate_at_the_bar_is_still_allowed(self):
        rows = sweep(v0_50=(0.40, 400, 0.01), v0_55=(0.50, 400, 0.01),
                     v0_60=(0.75, 400, 0.022))
        v, _ = knobs.verdict(Q, rows, '2026-08-16')
        self.assertEqual(v, 'move up')

    def test_the_stop_date_ends_the_question_whatever_the_numbers_say(self):
        rows = sweep(v0_50=(0.40, 400), v0_55=(0.50, 400), v0_60=(0.99, 400))
        v, detail = knobs.verdict(Q, rows, '2026-12-01')
        self.assertEqual(v, 'stop date reached')
        self.assertIn('write the receipt', detail)

    def test_flat_twice_running_says_close_the_question(self):
        rows = sweep(v0_50=(0.49, 400), v0_55=(0.50, 400), v0_60=(0.52, 400))
        _, first = knobs.verdict(Q, rows, '2026-08-16', flat_streak=0)
        self.assertNotIn('close the question', first)
        _, second = knobs.verdict(Q, rows, '2026-08-16', flat_streak=1)
        self.assertIn('close the question', second)


class Weekly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = util.Paths(self.tmp.name, Path(self.tmp.name) / 'models')

    def test_no_question_at_all_is_one_quiet_line(self):
        lines = knobs.weekly(self.paths, '2026-08-16', [])
        self.assertEqual(len(lines), 1)
        self.assertIn('no live question', lines[0])

    def test_with_nothing_filed_the_queue_supplies_the_questions(self):
        """PARAMETERS.md 11: the program files its own — one per bucket,
        the first knob of the rotation, not yet measured."""
        lines = knobs.weekly(self.paths, '2026-08-16')
        self.assertEqual(len(lines), 1)
        self.assertIn(knobs.KNOBS[0].knob, lines[0])
        self.assertIn('not measured yet', lines[0])
        self.assertIn('grid', lines[0])

    def test_the_flat_streak_survives_between_cycles(self):
        rows = sweep(v0_50=(0.49, 400), v0_55=(0.50, 400), v0_60=(0.52, 400))
        q = dataclasses.replace(Q, run=lambda: rows)
        first = knobs.weekly(self.paths, '2026-08-16', [q])
        self.assertIn('flat', first[0])
        self.assertNotIn('close the question', first[0])
        second = knobs.weekly(self.paths, '2026-08-23', [q])
        self.assertIn('close the question', second[0])
        # and a move resets it
        moved = sweep(v0_50=(0.40, 400), v0_55=(0.50, 400), v0_60=(0.75, 400))
        third = knobs.weekly(self.paths, '2026-08-30',
                             [dataclasses.replace(Q, run=lambda: moved)])
        self.assertIn('move up', third[0])
        fourth = knobs.weekly(self.paths, '2026-09-06', [q])
        self.assertNotIn('close the question', fourth[0])

    def test_a_failing_sweep_never_fails_the_cycle(self):
        def boom():
            raise RuntimeError('sidecar missing')
        q = dataclasses.replace(Q, run=boom)
        lines = knobs.weekly(self.paths, '2026-08-16', [q])
        self.assertIn('sweep skipped (sidecar missing)', lines[0])

    def test_the_line_carries_the_stop_date_so_an_extension_cannot_be_silent(self):
        rows = sweep(v0_50=(0.49, 400), v0_55=(0.50, 400), v0_60=(0.52, 400))
        q = dataclasses.replace(Q, run=lambda: rows)
        line = knobs.weekly(self.paths, '2026-08-16', [q])[0]
        self.assertIn('live 2w', line)
        self.assertIn('stop 2026-11-30', line)


class GateGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        import gc
        self.addCleanup(gc.collect)
        self.paths = util.Paths(self.tmp.name, Path(self.tmp.name) / 'models')

    def test_the_shipped_gate_matches_the_register(self):
        ok, lines = knobs.gate_guard(self.paths)
        self.assertTrue(ok, lines)
        self.assertEqual(lines, [])

    def test_a_moved_knob_blocks_delivery_and_names_itself(self):
        """What an edited constant without the ritual looks like from the
        outside — and the message has to be actionable, not two hashes."""
        import relevance as rel
        import evidence as evd
        with mock.patch.object(evd, 'SYN_THRESHOLD', 0.99):
            moved = rel.GateConfig()
        ledger.append(self.paths.deliveries_home, 'gate_configs',
                      [{'fingerprint': knobs.EXPECTED_GATE_FINGERPRINT,
                        'first_seen': '2026-08-16T00:00:00+00:00',
                        **rel.DEFAULT_CONFIG.as_dict()}])
        ok, lines = knobs.gate_guard(self.paths, moved)
        self.assertFalse(ok)
        body = '\n'.join(lines)
        self.assertIn('GATE MISMATCH', body)
        self.assertIn('evidence.SYN_THRESHOLD: 0.8 -> 0.99', body)
        self.assertIn('delivery skipped', body)

    def test_an_unrecorded_expected_config_still_blocks_and_says_why(self):
        import relevance as rel
        import evidence as evd
        with mock.patch.object(evd, 'SYN_THRESHOLD', 0.99):
            moved = rel.GateConfig()
        ok, lines = knobs.gate_guard(self.paths, moved)
        self.assertFalse(ok)
        self.assertIn('never recorded here', '\n'.join(lines))


if __name__ == '__main__':
    unittest.main()
