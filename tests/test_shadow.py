"""PARAMETERS.md 12: the gate's forward channel — standing proposals judged
beside the champion on live lots, disagreements recorded, read blind, and a
verdict that can say "ready", "bar breached" or "loses" by itself.

The judge subprocess is stubbed: what is tested is the diff, the record, the
blindness of the reading, and the verdict ladder.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import knobs                                                      # noqa: E402
import ledger                                                     # noqa: E402
import shadow                                                     # noqa: E402
import util                                                       # noqa: E402

KNOB = 'relevance.DEFAULT_MIN_CODE_HARD'
T = knobs.Knob(KNOB, 'gate', 0.775, 0.875, 0.025, 'Where?')


def doc(fp, verdicts):
    return {'fingerprint': fp, 'override': {}, 'n_subs': 1,
            'verdicts': [{'sub_id': 'acme', 'sub_name': 'ACME Bau', 'procedure_id': p,
                          'lot_id': 'LOT-0001', 'verdict': v, 'title': f'Los {p}',
                          'buyer_name': 'Stadt X', 'cpv_main': '45000000',
                          'publication_number': f'{p}-2026', 'desc': 'Estrich…'}
                         for p, v in verdicts.items()]}


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        import gc
        self.addCleanup(gc.collect)
        self.paths = util.Paths(self.tmp.name, Path(self.tmp.name) / 'models')
        for p in (mock.patch.object(knobs, 'KNOBS', (T,)),
                  mock.patch.object(knobs.Knob, 'current', lambda t: 0.825)):
            p.start()
            self.addCleanup(p.stop)
        # a standing proposal: the queue found 0.85 better on the benchmark
        knobs.queue(self.paths, '2026-08-10', (T,), filed=())
        q = knobs.question_from(T, '2026-08-10')
        knobs.close_question(self.paths, q, 'proposal: move up',
                             '0.825 -> 0.85: recall 0.70 (lower bound 0.68) clears 0.825\'s 0.66',
                             '2026-08-10', proposed=0.85)

    def _measure(self, champ, cand):
        calls = []

        def fake(paths, lots_path, override=None, as_of=None, timeout=0):
            calls.append(override)
            return cand if override else champ
        return mock.patch.object(shadow, 'measure', fake), calls


class TheCycle(Sandbox):
    def test_disagreements_and_a_summary_are_recorded_and_the_line_says_so(self):
        champ = doc('7931c8e9cd', {'p1': 'in', 'p2': 'out', 'p3': 'in', 'p4': 'near'})
        cand = doc('cand000001', {'p1': 'in', 'p2': 'in', 'p3': 'out', 'p4': 'near'})
        patch, calls = self._measure(champ, cand)
        with patch:
            lines = shadow.run(self.paths, [{'procedure_id': 'p1'}], today='2026-08-17')
        self.assertEqual(calls, [None, {'DEFAULT_MIN_CODE_HARD': 0.85}])
        rows = ledger.read(self.paths.ledger_home, 'gate_shadows')
        diffs = [r for r in rows if r['role'] == 'diff']
        self.assertEqual({(r['procedure_id'], r['champion'], r['challenger']) for r in diffs},
                         {('p2', 'out', 'in'), ('p3', 'in', 'out')})
        summary = next(r for r in rows if r['role'] == 'summary')
        self.assertEqual((summary['champion_in'], summary['challenger_in'], summary['n_diff']),
                         (2, 2, 2))
        self.assertIn('2 disagree', lines[0])
        self.assertIn('--label', lines[0])

    def test_no_proposal_means_no_subprocess(self):
        with mock.patch.object(knobs, 'standing_proposals', lambda *a, **k: []):
            with mock.patch.object(shadow, 'measure', side_effect=AssertionError('must not run')):
                lines = shadow.run(self.paths, [], today='2026-08-17')
        self.assertIn('no standing proposal', lines[0])

    def test_the_champion_subprocess_runs_without_the_override_and_the_challenger_with_it(self):
        seen = []

        def fake_run(argv, env=None, **kw):
            seen.append(env.get('TM_GATE_OVERRIDE'))
            Path(argv[argv.index('--out') + 1]).write_text(json.dumps(doc('x', {})), encoding='utf-8')
            return subprocess.CompletedProcess(argv, 0, '', '')
        with mock.patch.object(subprocess, 'run', fake_run), \
             mock.patch.dict(os.environ, {'TM_GATE_OVERRIDE': '{"stale": 1}'}):
            shadow.measure(self.paths, 'lots.json')
            shadow.measure(self.paths, 'lots.json', override={'DEFAULT_MIN_CODE_HARD': 0.85})
        self.assertEqual(seen, [None, '{"DEFAULT_MIN_CODE_HARD": 0.85}'])


class TheReadingIsBlind(Sandbox):
    def setUp(self):
        super().setUp()
        champ = doc('7931c8e9cd', {'p1': 'in', 'p2': 'out', 'p3': 'in'})
        cand = doc('cand000001', {'p1': 'in', 'p2': 'in', 'p3': 'out'})
        patch, _ = self._measure(champ, cand)
        with patch:
            shadow.run(self.paths, [], today='2026-08-17')

    def test_unread_carries_no_verdict(self):
        for d in shadow.unread(self.paths):
            self.assertNotIn('champion', d)
            self.assertNotIn('challenger', d)
            self.assertIn('title', d)

    def test_answers_are_recorded_and_leave_the_unread_list(self):
        self.assertEqual(len(shadow.unread(self.paths)), 2)          # p2 and p3
        with mock.patch('builtins.print'):
            n = shadow.label(self.paths, answers=['i', 's'])
        self.assertEqual(n, 1)
        self.assertEqual(len(shadow.unread(self.paths)), 1)          # the skipped one
        with mock.patch('builtins.print'):
            shadow.label(self.paths, answers=['o'])
        self.assertEqual(shadow.unread(self.paths), [])
        labels = ledger.read(self.paths.ledger_home, 'gate_labels')
        self.assertEqual(sorted(l['expect'] for l in labels), ['in', 'out'])

    def test_quit_stops_without_recording_the_rest(self):
        with mock.patch('builtins.print'):
            n = shadow.label(self.paths, answers=['q'])
        self.assertEqual(n, 0)


class TheVerdict(Sandbox):
    def _cycle(self, day, champ, cand):
        patch, _ = self._measure(doc('7931c8e9cd', champ), doc('cand000001', cand))
        with patch:
            shadow.run(self.paths, [], today=day)

    def _label(self, expects):
        ledger.append(self.paths.ledger_home, 'gate_labels', [
            {'ts': f'2026-08-20T00:00:0{i % 10}+00:00', 'sub_id': 'acme', 'procedure_id': p,
             'lot_id': 'LOT-0001', 'expect': e, 'note': ''} for i, (p, e) in enumerate(expects.items())])

    def test_no_cycle_yet(self):
        status, _, _ = shadow.verdict(self.paths, KNOB, 0.85)
        self.assertEqual(status, 'no cycle yet')

    def test_collecting_until_enough_are_read(self):
        self._cycle('2026-08-17', {'p1': 'in', 'p2': 'out'}, {'p1': 'out', 'p2': 'in'})
        status, detail, _ = shadow.verdict(self.paths, KNOB, 0.85)
        self.assertEqual(status, 'collecting')
        self.assertIn('0 read', detail)

    def test_ready_when_the_challenger_wins_the_read_disagreements(self):
        champ = {f'p{i}': 'out' for i in range(30)}
        cand = {f'p{i}': 'in' for i in range(30)}
        self._cycle('2026-08-17', champ, cand)
        self._label({f'p{i}': 'in' for i in range(25)})       # 25 read, all say the challenger was right
        status, detail, st = shadow.verdict(self.paths, KNOB, 0.85)
        self.assertEqual(status, 'ready to promote', detail)
        self.assertEqual(st['challenger_right'], 25)

    def test_challenger_loses_when_the_champion_wins_them(self):
        champ = {f'p{i}': 'out' for i in range(30)}
        cand = {f'p{i}': 'in' for i in range(30)}
        self._cycle('2026-08-17', champ, cand)
        # 25 read: 3 in, 22 out — but 22 wrong admissions of 30 breaches the bar first
        self._label({f'p{i}': ('in' if i < 3 else 'out') for i in range(25)})
        status, _, _ = shadow.verdict(self.paths, KNOB, 0.85)
        self.assertEqual(status, 'bar breached')

    def test_bar_breached_is_certain_even_with_few_read(self):
        """One wrong admission of thirty is 3.3 % added leakage: a breach on
        the lots read, whatever the unread ones say."""
        champ = {f'p{i}': 'out' for i in range(30)}
        cand = {f'p{i}': 'in' for i in range(30)}
        self._cycle('2026-08-17', champ, cand)
        self._label({'p0': 'out'})
        status, detail, _ = shadow.verdict(self.paths, KNOB, 0.85)
        self.assertEqual(status, 'bar breached', detail)
        self.assertEqual(shadow.challengers(self.paths), [])          # dropped from the shadow

    def test_challenger_loses_on_dropped_lots(self):
        """The other direction: the challenger DROPS lots the champion admits;
        no added leakage, but the reading says the champion was right."""
        champ = {f'p{i}': 'in' for i in range(30)}
        cand = {f'p{i}': 'out' for i in range(30)}
        self._cycle('2026-08-17', champ, cand)
        self._label({f'p{i}': 'in' for i in range(25)})
        status, _, _ = shadow.verdict(self.paths, KNOB, 0.85)
        self.assertEqual(status, 'challenger loses')
        self.assertEqual(shadow.challengers(self.paths), [])

    def test_the_weekly_proposal_line_carries_the_forward_status(self):
        self._cycle('2026-08-17', {'p1': 'in'}, {'p1': 'out'})
        report = '\n'.join(knobs.weekly(self.paths, '2026-08-18'))
        self.assertIn('PROPOSAL standing', report)
        self.assertIn('forward: **collecting**', report)


class TheGuardrail(Sandbox):
    """PARAMETERS.md 14: delivered lots join the blind reading; their reading
    is the champion's live wrong-trade share."""

    def _deliver(self, n, gate_config='7931c8e9cd', day='2026-08-10'):
        ledger.append(self.paths.deliveries_home, 'deliveries', [
            {'ts': f'{day}T05:00:0{i % 10}+00:00', 'sub_id': 'acme', 'sub_version': 1,
             'procedure_id': f'd{i}', 'lot_id': 'LOT-0001', 'model': 'm', 'score': 0.7,
             'kind': 'pick', 'title': f'Delivered {i}', 'buyer_name': 'Stadt Y',
             'gate_config': gate_config} for i in range(n)])

    def _read_guards(self, expects):
        guards = [r for r in ledger.read(self.paths.ledger_home, 'gate_shadows') if r['role'] == 'guard']
        ledger.append(self.paths.ledger_home, 'gate_labels', [
            {'ts': f'2026-08-20T01:00:{i % 60:02d}+00:00', 'sub_id': g['sub_id'],
             'procedure_id': g['procedure_id'], 'lot_id': g['lot_id'], 'expect': e, 'note': ''}
            for i, (g, e) in enumerate(zip(guards, expects))])

    def test_a_cycle_queues_a_sample_of_delivered_lots_at_most_guard_sample(self):
        self._deliver(25)
        with mock.patch.object(knobs, 'standing_proposals', lambda *a, **k: []):
            lines = shadow.run(self.paths, [], today='2026-08-17')
        guards = [r for r in ledger.read(self.paths.ledger_home, 'gate_shadows') if r['role'] == 'guard']
        self.assertEqual(len(guards), shadow.GUARD_SAMPLE)
        self.assertIn('10 delivered lots queued', lines[0])
        self.assertIn('gate guardrail: **no reading yet**', lines[1])
        # the next cycle queues the NEXT ten, never the same lot twice
        with mock.patch.object(knobs, 'standing_proposals', lambda *a, **k: []):
            shadow.run(self.paths, [], today='2026-08-24')
        guards = [r for r in ledger.read(self.paths.ledger_home, 'gate_shadows') if r['role'] == 'guard']
        self.assertEqual(len({g['procedure_id'] for g in guards}), 20)

    def test_guard_lots_look_like_any_other_lot_in_the_reading_list(self):
        self._deliver(3)
        shadow.guard_sample(self.paths, '2026-08-17')
        for d in shadow.unread(self.paths):
            self.assertEqual(set(d), {'sub_id', 'sub_name', 'procedure_id', 'lot_id', 'title',
                                      'buyer_name', 'cpv_main', 'desc', 'publication_number'})

    def test_within_the_bar_and_breached(self):
        self._deliver(40)
        shadow.guard_sample(self.paths, '2026-08-17', n=40)
        self._read_guards(['in'] * 40)
        status, detail, st = shadow.guardrail(self.paths)
        self.assertEqual(status, 'within the bar', detail)
        self.assertEqual(st['read'], 40)
        # a second reading of two lots as out: 2/40 = 5% > 2.2%
        guards = [r for r in ledger.read(self.paths.ledger_home, 'gate_shadows') if r['role'] == 'guard']
        ledger.append(self.paths.ledger_home, 'gate_labels', [
            {'ts': '2026-08-21T00:00:00+00:00', 'sub_id': 'acme', 'procedure_id': g['procedure_id'],
             'lot_id': g['lot_id'], 'expect': 'out', 'note': ''} for g in guards[:2]])
        status, detail, _ = shadow.guardrail(self.paths)
        self.assertEqual(status, 'bar breached', detail)
        self.assertIn('BREACHED', shadow.guardrail_lines(self.paths)[0])
        self.assertIn('no earlier gate configuration', detail)

    def test_collecting_below_min_guard_even_if_the_rate_is_high(self):
        self._deliver(10)
        shadow.guard_sample(self.paths, '2026-08-17')
        self._read_guards(['out'] * 3 + ['in'] * 7)
        status, detail, _ = shadow.guardrail(self.paths)
        self.assertEqual(status, 'collecting')
        self.assertIn('20 more to read', detail)

    def test_the_revert_target_is_the_configuration_recorded_before_the_current(self):
        ledger.append(self.paths.deliveries_home, 'gate_configs', [
            {'fingerprint': 'old0000000', 'first_seen': '2026-08-01T00:00:00+00:00', 'mode': 'evidence'},
            {'fingerprint': '7931c8e9cd', 'first_seen': '2026-08-16T00:00:00+00:00', 'mode': 'evidence'}])
        self.assertIn('old0000000 (recorded 2026-08-01)', shadow._revert_target(self.paths))


if __name__ == '__main__':
    unittest.main()
