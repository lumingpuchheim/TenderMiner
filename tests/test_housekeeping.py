"""Housekeeping tests — the sweeps that keep `data/` bounded.

Separate from `test_storage.py` on purpose: importing `loop` pulls in pandas,
numpy and CatBoost, and the storage tests are worth keeping free of the ML stack
so they stay fast and can fail for storage reasons only.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import download
import loop


class ScratchWorldSweep(unittest.TestCase):
    """`data/backtest_world` is a per-cutoff copy of the store plus a full copy
    of the embeddings, rebuilt from the real store on every backtest and read by
    nothing in between. It reached 203.8 MB — the second largest thing under
    `data/` after the notice archive — for something entirely reconstructible."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='tm-house-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.world = self.root / 'backtest_world' / 'store'
        self.world.mkdir(parents=True)
        (self.world / 'tenders.parquet').write_bytes(b'x' * 1024)
        self.paths = loop.Paths(self.root, self.root / 'models')

    def _age(self, days):
        when = time.time() - days * 86400
        for f in (self.root / 'backtest_world').rglob('*'):
            if f.is_file():
                os.utime(f, (when, when))

    def test_a_fresh_world_is_left_alone(self):
        """Age is the safety catch: a backtest can run for hours, and a sweep
        must not pull the floor out from under it."""
        self.assertEqual(loop._prune_scratch_world(self.paths, 30), (0, 0))
        self.assertTrue((self.root / 'backtest_world').exists())

    def test_an_aged_world_is_swept(self):
        self._age(40)
        n, freed = loop._prune_scratch_world(self.paths, 30)
        self.assertEqual((n, freed), (1, 1024))
        self.assertFalse((self.root / 'backtest_world').exists())

    def test_absent_world_is_not_an_error(self):
        shutil.rmtree(self.root / 'backtest_world')
        self.assertEqual(loop._prune_scratch_world(self.paths, 30), (0, 0))

    def test_prune_caches_never_raises(self):
        """A backup or housekeeping problem is not a delivery problem."""
        self.assertEqual(loop.prune_caches(self.paths, 30), 0)


class DiscoveryCacheSweep(unittest.TestCase):
    """The TED search resume cache is keyed by a hash of the query, and a query
    names a date window — so a cache is unresumable the day after its window
    passes. Nothing expired them and the directory reached 1.13 GB across 1,132
    dead scopes."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='tm-disc-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.saved = (download.DISCOVERY_DIR, download.LEGACY_DISCOVERY_DIR)
        download.DISCOVERY_DIR = self.root / 'cache' / 'discovery'
        download.LEGACY_DISCOVERY_DIR = self.root / 'logs' / 'discovery'
        self.addCleanup(self._restore)
        for d in (download.DISCOVERY_DIR, download.LEGACY_DISCOVERY_DIR):
            d.mkdir(parents=True)
            (d / 'abc123.jsonl').write_text('{}\n', encoding='utf-8')
            (d / 'abc123.state.json').write_text('{}', encoding='utf-8')

    def _restore(self):
        download.DISCOVERY_DIR, download.LEGACY_DISCOVERY_DIR = self.saved

    def _age(self, days):
        when = time.time() - days * 86400
        for d in (download.DISCOVERY_DIR, download.LEGACY_DISCOVERY_DIR):
            for f in d.iterdir():
                os.utime(f, (when, when))

    def test_fresh_caches_survive(self):
        n, freed = download.prune_discovery(30)
        self.assertEqual(n, 0)
        self.assertTrue((download.DISCOVERY_DIR / 'abc123.jsonl').exists())

    def test_both_locations_are_swept(self):
        """The cache moved from data/logs to data/cache; the sweep has to clear
        the old pile too or the relocation leaves 1.13 GB behind."""
        self._age(40)
        n, freed = download.prune_discovery(30)
        self.assertEqual(n, 4)
        self.assertGreater(freed, 0)
        self.assertFalse(download.DISCOVERY_DIR.exists())
        self.assertFalse(download.LEGACY_DISCOVERY_DIR.exists())

    def test_dry_run_deletes_nothing(self):
        self._age(40)
        n, freed = download.prune_discovery(30, dry_run=True)
        self.assertEqual(n, 4)
        self.assertTrue((download.DISCOVERY_DIR / 'abc123.jsonl').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
