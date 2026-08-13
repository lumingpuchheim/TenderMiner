"""The synonym tier's word cache — the memory-mapped format and its save.

What these guard is not a feature but a memory bound: the cache is ~93k words
by 768 float32, and the whole point of the `.npy` + `_words.json` pair is that
opening it does not make that resident. A regression here does not fail
anything visibly — it just puts 273 MB back into every run that touches tier 3
(doc/MEMORY_BUDGET.md), which is why the mmap is asserted directly.

The embedding model is never loaded: `embed_texts` is replaced, so a test that
somehow reaches for a real vector fails loudly instead of downloading jina.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import embed  # noqa: E402
import evidence as ev  # noqa: E402

DIM = 8


def fake_vectors(words):
    """Deterministic per-word vectors — the value only has to be recognisable."""
    return np.array([[float(len(w))] + [float(ord(c)) for c in w[:DIM - 1].ljust(DIM - 1)]
                     for w in words], dtype=np.float32)


class SynonymCache(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cache = self.dir / 'word_vecs.npz'
        self.embedded = []
        self._real = embed.embed_texts
        embed.embed_texts = self._embed
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, embed, 'embed_texts', self._real)

    def _embed(self, texts, batch_size=64):
        texts = list(texts)
        self.embedded.extend(texts)
        return fake_vectors(texts)

    def legacy_npz(self, words):
        np.savez_compressed(self.cache, words=np.array(words),
                            vecs=fake_vectors(words))

    # ------------------------------------------------------------------ format

    def test_reads_the_legacy_npz_and_saves_the_mmap_pair(self):
        self.legacy_npz(['blitzschutz', 'erdungsanlag'])
        syn = ev.SynonymTier(self.cache)
        self.assertEqual(len(syn), 2)
        self.assertIn('blitzschutz', syn)
        syn.save()
        self.assertTrue((self.dir / 'word_vecs.npy').exists())
        self.assertEqual(
            json.loads((self.dir / 'word_vecs_words.json').read_text('utf-8')),
            ['blitzschutz', 'erdungsanlag'])

    def test_the_saved_cache_is_memory_mapped_when_reopened(self):
        self.legacy_npz(['blitzschutz'])
        ev.SynonymTier(self.cache).save()
        syn = ev.SynonymTier(self.cache)
        self.assertIsInstance(syn.base, np.memmap)

    def test_vectors_survive_the_round_trip_unchanged(self):
        words = ['blitzschutz', 'erdungsanlag', 'fangeinrichtung']
        self.legacy_npz(words)
        before = ev.SynonymTier(self.cache)._embed(words)
        ev.SynonymTier(self.cache).save()
        after = ev.SynonymTier(self.cache)._embed(words)
        np.testing.assert_array_equal(before, after)
        self.assertEqual(self.embedded, [], 'a cached word was re-embedded')

    # ------------------------------------------------------------- the misses

    def test_a_cached_word_never_reaches_the_model(self):
        self.legacy_npz(['blitzschutz'])
        syn = ev.SynonymTier(self.cache)
        syn._embed(['blitzschutz'])
        self.assertEqual(self.embedded, [])
        self.assertEqual(syn.misses, 0)

    def test_an_uncached_word_is_embedded_once_and_counted(self):
        self.legacy_npz(['blitzschutz'])
        syn = ev.SynonymTier(self.cache)
        syn._embed(['blitzschutz', 'ueberspannungsschutz'])
        syn._embed(['ueberspannungsschutz'])
        self.assertEqual(self.embedded, ['ueberspannungsschutz'])
        self.assertEqual(syn.misses, 1)

    def test_a_word_repeated_in_one_call_is_embedded_once(self):
        syn = ev.SynonymTier(self.cache)
        out = syn._embed(['ableitung', 'ableitung'])
        self.assertEqual(self.embedded, ['ableitung'])
        np.testing.assert_array_equal(out[0], out[1])

    def test_saving_merges_the_new_words_in(self):
        self.legacy_npz(['blitzschutz'])
        syn = ev.SynonymTier(self.cache)
        syn._embed(['ueberspannungsschutz'])
        syn.save()
        reopened = ev.SynonymTier(self.cache)
        self.assertEqual(len(reopened), 2)
        np.testing.assert_array_equal(
            reopened._embed(['ueberspannungsschutz']),
            fake_vectors(['ueberspannungsschutz']))
        self.assertEqual(self.embedded, ['ueberspannungsschutz'],
                         'the merged word was embedded again after saving')

    def test_saving_an_empty_cache_writes_nothing(self):
        ev.SynonymTier(self.cache).save()
        self.assertFalse((self.dir / 'word_vecs.npy').exists())

    def test_a_saved_cache_can_be_saved_again(self):
        # the second save has to replace a file this process has mapped, which
        # Windows refuses while the mapping is open
        self.legacy_npz(['blitzschutz'])
        syn = ev.SynonymTier(self.cache)
        syn.save()
        syn._embed(['ableitung'])
        syn.save()
        self.assertEqual(len(ev.SynonymTier(self.cache)), 2)

    # ------------------------------------------------------------- the matcher

    def test_the_matcher_still_returns_pairs_above_the_bar(self):
        # identical vectors -> cosine 1.0, so the pair must clear any bar
        self.legacy_npz([])
        syn = ev.SynonymTier(self.cache)
        unit = np.ones((1, DIM), dtype=np.float32) / np.sqrt(DIM)
        syn.extra = {'dachdeck': unit[0], 'dachbahn': unit[0]}
        self.assertEqual(syn(['dachbahn'], ['dachdeck']),
                         [('dachdeck', 'dachbahn')])

    def test_the_matcher_is_empty_without_words_or_keywords(self):
        syn = ev.SynonymTier(self.cache)
        self.assertEqual(syn([], ['dachdeck']), [])
        self.assertEqual(syn(['dachbahn'], []), [])


if __name__ == '__main__':
    unittest.main()
