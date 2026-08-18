"""The embedding model is opened once a week, for both jobs that need it.

Opening it costs ~1.2 GB whether it is then handed 278 tender texts or one
word (doc/HOSTING.md 0a). There are two jobs — lot texts, and the single
words evidence tier 3 falls back on — and they must share one open.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import embed                                                   # noqa: E402
import embed_vocab                                             # noqa: E402

REPO = Path(__file__).resolve().parents[1]


class OneOpen(unittest.TestCase):
    def test_ensure_embeddings_does_not_unload(self):
        """It used to. The word job runs straight after and needs it open."""
        src = (REPO / 'embed.py').read_text(encoding='utf-8')
        body = src.split('def ensure_embeddings', 1)[1].split('\ndef ', 1)[0]
        self.assertNotIn('unload_model()', body,
                         'unloading here reopens the model for the words')

    def test_the_cycle_unloads_after_both_jobs(self):
        src = (REPO / 'cycle.py').read_text(encoding='utf-8')
        self.assertIn('embed_vocab.top_up', src,
                      'the word job must ride along with the lot job')
        lots = src.index('embed.ensure_embeddings')
        words = src.index('embed_vocab.top_up')
        unload = src.index('embed.unload_model()')
        self.assertLess(lots, words, 'words are topped up after the lots')
        self.assertLess(words, unload, 'the model is released after both')

    def test_top_up_takes_no_lock(self):
        """The cycle holds the heavy lock already; locking twice is the one
        way this design could hang (heavy_lock, property 2)."""
        src = (REPO / 'embed_vocab.py').read_text(encoding='utf-8')
        body = src.split('def top_up', 1)[1].split('\ndef ', 1)[0]
        self.assertNotIn('heavy_lock.held', body)

    def test_top_up_is_quiet_and_zero_when_nothing_is_missing(self):
        class FullCache:
            vec_path = 'x'

            def __contains__(self, word):
                return True

            def __len__(self):
                return 42

        said = []
        real_syn, real_vocab = embed_vocab.ev.SynonymTier, embed_vocab.store_vocabulary
        embed_vocab.ev.SynonymTier = lambda *_: FullCache()
        embed_vocab.store_vocabulary = lambda *_a, **_k: ({'bau', 'dach'}, 2)
        try:
            n = embed_vocab.top_up('/nonexistent', progress=False,
                                   log=said.append)
        finally:
            embed_vocab.ev.SynonymTier = real_syn
            embed_vocab.store_vocabulary = real_vocab
        self.assertEqual(n, 0)
        self.assertTrue(any('current' in s for s in said), said)

    def test_unload_is_safe_when_the_model_was_never_opened(self):
        """`cycle.py` calls it in a `finally`, including on the path where
        ensure_embeddings raised before opening anything."""
        embed.unload_model()
        embed.unload_model()


if __name__ == '__main__':
    unittest.main()
