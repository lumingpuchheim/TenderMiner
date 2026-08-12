"""`asof.py` — the time-isolation guarantees, tested once, against the engine.

REFACTOR.md phase 5: three programs used to carry their own copy of the
rewind machinery, and none of the copies had tests — a fix (the crash-safe
parquet write) or a subtlety (the pyarrow filter preserving role metadata)
lived wherever it was last remembered. These tests pin the guarantees to the
one implementation that remains.

Everything here runs on synthetic parquet fixtures — no real store, no
calibration, no model. The properties are structural.
"""

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asof                                                      # noqa: E402


def write_store(store, name, rows, role=b'meta'):
    """A small parquet with a `role` tag on publication_date — the metadata
    `load_with_roles` reads and a pandas round-trip silently drops."""
    tab = pa.table({
        'procedure_id': [r[0] for r in rows],
        'lot_id': [r[1] for r in rows],
        'publication_date': pa.array([r[2] for r in rows], pa.date32()),
    })
    fields = [f.with_metadata({b'role': role})
              if f.name == 'publication_date' else f for f in tab.schema]
    pq.write_table(tab.cast(pa.schema(fields)), store / f'{name}.parquet')


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        (self.data / 'store').mkdir()
        (self.data / 'embeddings').mkdir()
        (self.data / 'embeddings' / 'vectors.bin').write_bytes(b'sidecar')
        rows = [('p1', 'L1', date(2026, 1, 10)),
                ('p2', 'L2', date(2026, 3, 10)),
                ('p3', 'L3', date(2026, 6, 10))]
        write_store(self.data / 'store', 'tenders', rows)
        write_store(self.data / 'store', 'awards', rows[:2])
        self.work = self.data / 'asof' / 'test'

    def world(self):
        return asof.World(self.data, self.work)


class TheRewind(Fixture):
    def test_only_pre_cutoff_publications_survive(self):
        """The guarantee everything else rests on: nothing in the world is
        dated on or after the cutoff — in either file."""
        self.world().rewind(pd.Timestamp('2026-03-10'))
        for name, expect in (('tenders', ['p1']), ('awards', ['p1'])):
            got = pd.read_parquet(self.work / 'store' / f'{name}.parquet')
            self.assertEqual(list(got['procedure_id']), expect,
                             f'{name}: a publication dated on/after the '
                             f'cutoff reached the as-of side')

    def test_role_metadata_survives_the_filter(self):
        """`load_with_roles` reads a `role` tag off each column; a pandas
        round-trip drops it silently and everything downstream degrades.
        The pyarrow filter must preserve it — the subtlety only one of the
        three old copies still documented."""
        self.world().rewind(pd.Timestamp('2026-03-10'))
        schema = pq.read_schema(self.work / 'store' / 'tenders.parquet')
        md = schema.field('publication_date').metadata or {}
        self.assertEqual(md.get(b'role'), b'meta')

    def test_an_interrupted_rewind_never_leaves_a_truncated_parquet(self):
        """The 2026-08-11 failure: a rewind killed mid-write left a parquet
        without its footer, and every later run died on it. The write goes
        to `.partial` and is renamed, so a crash leaves the previous world's
        file — stale, but readable, and the next rewind repairs it."""
        w = self.world()
        w.rewind(pd.Timestamp('2026-06-10'))
        real = asof.pq.write_table

        def dies_on_awards(tab, path, *a, **k):
            if 'awards' in str(path):
                Path(path).write_bytes(b'half a parquet')   # the crash
                raise KeyboardInterrupt
            return real(tab, path, *a, **k)

        asof.pq.write_table = dies_on_awards
        try:
            with self.assertRaises(KeyboardInterrupt):
                w.rewind(pd.Timestamp('2026-03-10'))
        finally:
            asof.pq.write_table = real
        # both store files still parse; awards still shows the OLD cutoff
        for name in ('tenders', 'awards'):
            pd.read_parquet(self.work / 'store' / f'{name}.parquet')
        aw = pd.read_parquet(self.work / 'store' / 'awards.parquet')
        self.assertEqual(list(aw['procedure_id']), ['p1', 'p2'])
        # and the next rewind repairs, rather than needing manual deletion
        w.rewind(pd.Timestamp('2026-03-10'))
        aw = pd.read_parquet(self.work / 'store' / 'awards.parquet')
        self.assertEqual(list(aw['procedure_id']), ['p1'])

    def test_rewinding_again_drops_the_frame_caches(self):
        """A frame served from the previous cutoff's cache would be a
        time-travel bug wearing a performance optimisation's clothes."""
        w = self.world()
        w.rewind(pd.Timestamp('2026-06-10'))
        self.assertEqual(len(w.tenders), 2)
        w.rewind(pd.Timestamp('2026-03-10'))
        self.assertEqual(len(w.tenders), 1)

    def test_rewinding_keeps_the_calibration(self):
        """Deliberately NOT dropped: recalibration cadence is the caller's
        decision (the all-lots rewind recalibrates every eighth cutoff), so
        going stale must be a visible choice, not an engine surprise."""
        w = self.world()
        w.rewind(pd.Timestamp('2026-06-10'))
        w._calibration = {'sentinel': True}
        w.rewind(pd.Timestamp('2026-03-10'))
        self.assertEqual(w._calibration, {'sentinel': True})


class TheSidecar(Fixture):
    def test_copied_once_and_reused(self):
        """Copied whole because re-embedding per cutoff would dominate the
        run; reused across Worlds because it is immutable per model tag."""
        self.world()
        marker = self.work / 'embeddings' / 'marker'
        marker.write_bytes(b'')
        self.world()                        # second engine over the same dir
        self.assertTrue(marker.exists(), 'the sidecar was re-copied')


class TheRecipes(Fixture):
    RESULT = {'baseline': 0.3, 'trust_cut': 0.45, 'cohesion': {},
              'configs': {
                  'F hard/soft codes + floor/consensus': {
                      'soft_floor': 0.5, 'soft_consensus': 2,
                      'threshold': 0.61, 'code_threshold': 0.71,
                      'soft_threshold': 0.81},
                  'H single bar + trade-read corroboration': {
                      'soft_floor': 0.5, 'soft_consensus': 2,
                      'corr_form': 'margin', 'corr_param': 0.07}}}

    def test_the_two_live_recipes_map_as_their_call_sites_did(self):
        """F carries all three bars on the config (the all-lots convention);
        H carries corroboration and NO bars (the report rewind has always
        left those to DEFAULT_CONFIG). Preserved verbatim — REFACTOR.md
        phase 5 records the difference instead of unifying it silently."""
        w = self.world()
        w._calibration = self.RESULT
        f = w.calibrated_config('F')
        self.assertEqual((f.min_relevance, f.min_code_hard, f.min_code_soft),
                         (0.61, 0.71, 0.81))
        h = w.calibrated_config('H')
        self.assertEqual((h.trade_read_form, h.trade_read_param),
                         ('margin', 0.07))
        import relevance as rel
        self.assertEqual(h.min_relevance, rel.DEFAULT_CONFIG.min_relevance)

    def test_an_unknown_recipe_is_refused_by_name(self):
        w = self.world()
        w._calibration = self.RESULT
        with self.assertRaises(ValueError):
            w.calibrated_config('Q')


if __name__ == '__main__':
    unittest.main()
