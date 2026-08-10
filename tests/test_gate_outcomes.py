"""The gate-outcome snapshot — doc/SIMULATION.md, "the gate rides along".

    python -m unittest discover -t . -s tests        # from the repository root
    python tests/test_gate_outcomes.py               # or directly

What is being protected here is a TIME SERIES. The verdict x outcome join is
recomputed from scratch on every read, so the only way yesterday's numbers
still exist tomorrow is that a cycle wrote them down — and the only way the
series is trustworthy is that re-running a cycle adds nothing and changes
nothing.

Same house rules as test_storage.py: temporary directories only, stdlib only,
no real data, no network.
"""

import csv
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
import ledger
import simulation

TS1 = '2026-08-10T03:00:00+00:00'
TS2 = '2026-08-11T03:00:00+00:00'

SIMS = [
    {'ts': TS1, 'company': 'Acme GmbH', 'procedure_id': 'p-1',
     'lot_id': 'LOT-1', 'model': 'm-1', 'score': 0.9, 'cpv3': '452'},
    {'ts': TS1, 'company': 'Acme GmbH', 'procedure_id': 'p-2',
     'lot_id': 'LOT-2', 'model': 'm-1', 'score': 0.8, 'cpv3': '452'},
    {'ts': TS1, 'company': 'Beta Bau AG', 'procedure_id': 'p-1',
     'lot_id': 'LOT-1', 'model': 'm-1', 'score': 0.9, 'cpv3': '452'},
]

VERDICTS = [
    {'ts': TS1, 'company': 'Acme GmbH', 'procedure_id': 'p-1',
     'lot_id': 'LOT-1', 'verdict': 'admit', 'gate_pass': 1},
    {'ts': TS1, 'company': 'Acme GmbH', 'procedure_id': 'p-2',
     'lot_id': 'LOT-2', 'verdict': 'reject', 'gate_pass': 0},
    {'ts': TS1, 'company': 'Beta Bau AG', 'procedure_id': 'p-1',
     'lot_id': 'LOT-1', 'verdict': 'no_profile', 'gate_pass': None},
]

GRADE = {'graded_at': '2026-08-11T00:00:00+00:00', 'procedure_id': 'p-1',
         'lot_id': 'LOT-1', 'label': 1, 'n_tenders': 1,
         'award_pub': '2026-08-09', 'model': 'm-1', 'score': 0.9}


def as_text(value):
    """A cell as `csv` will have written it."""
    return '' if value is None else str(value)


class Snapshot(unittest.TestCase):
    """A throwaway storage home per test, with picks and verdicts on record
    and nothing graded yet — the state the real system is actually in until
    the simulated picks start being graded (~October 2026)."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix='tm-gout-'))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        db.init(self.home)
        ledger.append(self.home, 'simulations', SIMS)
        ledger.append(self.home, 'simulations_gate', VERDICTS)

    def rows(self):
        return ledger.read(self.home, 'gate_outcomes')

    def csv_rows(self):
        path = self.home / 'reports' / simulation.OUTCOME_CSV
        with path.open(encoding='utf-8', newline='') as f:
            return list(csv.DictReader(f))

    # ------------------------------------------------- the zero row matters

    def test_a_cycle_with_nothing_graded_still_writes_its_row(self):
        """The series has to start before the numbers are interesting, or it
        cannot show that they were zero."""
        simulation.snapshot_verdict_outcomes(self.home, ts=TS1)
        rows = self.rows()
        self.assertEqual([r['verdict'] for r in rows],
                         ['admit', 'no_profile', 'reject'])
        for r in rows:
            self.assertEqual(r['ts'], TS1)
            self.assertEqual((r['graded'], r['own_wins']), (0, 0))
            self.assertEqual((r['lonely_rate'], r['own_rate']), (0.0, 0.0))
            self.assertEqual((r['picks_total'], r['verdicts_total']), (3, 3))

    def test_one_ts_per_snapshot(self):
        """A cycle is one addressable point in the series, so its rows share
        a single stamp rather than each carrying the moment it was built."""
        simulation.snapshot_verdict_outcomes(self.home)
        self.assertEqual(len({r['ts'] for r in self.rows()}), 1)

    # -------------------------------------------------- two cycles, in order

    def test_two_cycles_are_two_points_in_the_series(self):
        simulation.snapshot_verdict_outcomes(self.home, ts=TS1)
        ledger.append(self.home, 'grades', [GRADE])   # an award publishes
        simulation.snapshot_verdict_outcomes(self.home, ts=TS2)
        rows = self.rows()
        self.assertEqual(len(rows), 6)
        self.assertEqual([r['ts'] for r in rows], [TS1] * 3 + [TS2] * 3)
        graded = {(r['ts'], r['verdict']): r['graded'] for r in rows}
        self.assertEqual(graded[(TS1, 'admit')], 0)
        # the graded lot was picked by two simulated customers, one admitted
        # and one with no profile; the rejected pick is still ungraded
        self.assertEqual(graded[(TS2, 'admit')], 1)
        self.assertEqual(graded[(TS2, 'no_profile')], 1)
        self.assertEqual(graded[(TS2, 'reject')], 0)
        second = {r['verdict']: r for r in rows if r['ts'] == TS2}
        self.assertEqual(second['admit']['lonely_rate'], 1.0)
        self.assertEqual(second['admit']['own_wins'], 0)   # no awards store

    def test_the_snapshot_agrees_with_the_dashboard_aggregation(self):
        """One computation, two readers: the snapshot must not be a second,
        drifting definition of the same numbers."""
        ledger.append(self.home, 'grades', [GRADE])
        simulation.snapshot_verdict_outcomes(self.home, ts=TS2)
        live = {r['verdict']: r for r in simulation.verdict_outcomes(self.home)}
        for r in self.rows():
            if r['verdict'] in live:
                for col in ('graded', 'lonely_rate', 'own_wins', 'own_rate'):
                    with self.subTest(verdict=r['verdict'], col=col):
                        self.assertEqual(r[col], live[r['verdict']][col])

    # -------------------------------------------------------- re-running it

    def test_resnapshotting_the_same_ts_is_a_noop(self):
        """A re-run of a cycle must not double a point in the series. The
        natural key (ts, verdict) is what makes that structural rather than
        remembered."""
        simulation.snapshot_verdict_outcomes(self.home, ts=TS1)
        before = self.rows()
        simulation.snapshot_verdict_outcomes(self.home, ts=TS1)
        self.assertEqual(self.rows(), before)
        con = db.connect(self.home, create=False)
        seqs = [r['seq'] for r in
                con.execute('SELECT seq FROM gate_outcome ORDER BY seq')]
        self.assertEqual(seqs, [1, 2, 3])

    def test_a_rerun_after_a_grade_lands_keeps_the_first_answer(self):
        """The stamp is the record of what was known THEN. Re-running the same
        cycle after new evidence arrives must not rewrite that day's numbers —
        the append-only key refuses, and the new state gets its own ts."""
        simulation.snapshot_verdict_outcomes(self.home, ts=TS1)
        ledger.append(self.home, 'grades', [GRADE])
        simulation.snapshot_verdict_outcomes(self.home, ts=TS1)
        self.assertTrue(all(r['graded'] == 0 for r in self.rows()))

    # ------------------------------------------------------------- the CSV

    def test_csv_is_the_full_history_with_exactly_the_table_columns(self):
        simulation.snapshot_verdict_outcomes(self.home, ts=TS1)
        ledger.append(self.home, 'grades', [GRADE])
        simulation.snapshot_verdict_outcomes(self.home, ts=TS2)
        got = self.csv_rows()
        self.assertEqual(list(got[0]), list(simulation.OUTCOME_COLUMNS))
        table = self.rows()
        self.assertEqual(len(got), len(table))
        for csv_row, row in zip(got, table):
            self.assertEqual(csv_row,
                             {c: as_text(row[c]) for c in simulation.OUTCOME_COLUMNS})

    def test_csv_columns_are_the_tables_data_columns(self):
        """`seq` and `raw` are storage bookkeeping; everything else in the
        table is part of the record and must reach a consumer without
        sqlite."""
        con = db.connect(self.home, create=False)
        cols = [c for c in db.columns_of(con, 'gate_outcome')
                if c not in ('seq', 'raw')]
        self.assertEqual(cols, list(simulation.OUTCOME_COLUMNS))

    def test_csv_is_rewritten_not_appended(self):
        """One stable path, overwritten idempotently: re-running a cycle must
        leave the file byte-identical rather than duplicating its history."""
        simulation.snapshot_verdict_outcomes(self.home, ts=TS1)
        path = self.home / 'reports' / simulation.OUTCOME_CSV
        first = path.read_bytes()
        simulation.snapshot_verdict_outcomes(self.home, ts=TS1)
        self.assertEqual(path.read_bytes(), first)
        self.assertEqual(len(self.csv_rows()), 3)

    def test_csv_appears_even_with_no_snapshot_rows(self):
        """A fresh deployment has no verdicts, so the snapshot writes nothing —
        a consumer still gets a header rather than a missing file."""
        home = Path(tempfile.mkdtemp(prefix='tm-gout-empty-'))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        db.init(home)
        self.assertEqual(simulation.snapshot_verdict_outcomes(home), [])
        with (home / 'reports' / simulation.OUTCOME_CSV).open(encoding='utf-8') as f:
            self.assertEqual(f.read().strip(),
                             ','.join(simulation.OUTCOME_COLUMNS))

    # ------------------------------------------------------ the storage rule

    def test_the_ledger_is_where_the_rows_live(self):
        """Storage access goes through ledger.py: the rows must be in the
        database, not in a jsonl file this module opened itself."""
        simulation.snapshot_verdict_outcomes(self.home, ts=TS1)
        self.assertEqual(ledger.storage(self.home, 'gate_outcomes')[0], 'db')
        self.assertFalse(ledger.file_path(self.home, 'gate_outcomes').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
