import threading
import unittest
from unittest.mock import patch
from services import catalog_prefetch as prefetch
from services import large_package_catalog_naming as naming

class EvidencePrefetchTests(unittest.TestCase):
    def tearDown(self): prefetch.shutdown()
    def test_global_queue_is_bounded_and_deduplicated(self):
        entered, release = threading.Event(), threading.Event()
        seen = []
        def bundle(store,pid,gid):
            seen.append(gid); entered.set(); release.wait(3)
        with patch.object(naming,'bundle',side_effect=bundle):
            try:
                self.assertTrue(prefetch.start(None,'p',['a','a','b','c'],limit=2))
                self.assertTrue(entered.wait(2))
                self.assertFalse(prefetch.start(None,'q',['x']))
            finally: release.set(); prefetch.shutdown()
        self.assertEqual(seen,['a','b'])
    def test_cancel_stops_between_groups(self):
        stopped = threading.Event()
        with patch.object(naming,'bundle',side_effect=lambda *a: stopped.set()) as bundle:
            self.assertTrue(prefetch.start(None,'p',['a','b'],stopped.is_set))
            self.assertTrue(stopped.wait(2))
            prefetch.shutdown()
            self.assertEqual(bundle.call_count,1)
            self.assertFalse(prefetch.start(None,'p',['c'],stopped.is_set))
    def test_failure_does_not_publish_or_block_next_group(self):
        with patch.object(naming,'bundle',side_effect=[ValueError('fixture'),{}]) as bundle:
            with prefetch.scope():
                prefetch.start(None,'p',['a','b'])
            self.assertEqual(bundle.call_count,2)
    def test_scope_restores_after_failure(self):
        self.assertFalse(prefetch.enabled())
        with self.assertRaises(ValueError):
            with prefetch.scope():
                self.assertTrue(prefetch.enabled()); raise ValueError()
        self.assertFalse(prefetch.enabled())
    def test_scope_joins_owned_work_before_store_cleanup(self):
        completed = threading.Event()
        with patch.object(naming,'bundle',side_effect=lambda *args: completed.set()):
            with prefetch.scope():
                self.assertTrue(prefetch.start(None,'p',['a']))
            self.assertTrue(completed.is_set())
    def test_token_count_exact_and_encoder_scoped(self):
        class Encoder:
            def __init__(self,n): self.calls=0; self.n=n
            def encode(self,*args,**kwargs):
                self.calls+=1
                return type('Result',(),{'ids':list(range(self.n))})()
        naming._TOKEN_COUNT_CACHE.clear()
        first, second = Encoder(3), Encoder(9)
        with patch.object(naming,'_tokenizer',return_value=first):
            self.assertEqual(naming.token_count('测试🙂'),3)
            self.assertEqual(naming.token_count('测试🙂'),3)
        self.assertEqual(first.calls,1)
        with patch.object(naming,'_tokenizer',return_value=second): self.assertEqual(naming.token_count('测试🙂'),9)
        with patch.object(naming,'_tokenizer',return_value=None): self.assertEqual(naming.token_count('测试🙂'),len('测试🙂'.encode()))
    def test_unhashable_encoder_keeps_correct_counts(self):
        class Encoder:
            __hash__=None
            def encode(self,*args,**kwargs): return type('Result',(),{'ids':[]})()
        with patch.object(naming,'_tokenizer',return_value=Encoder()): self.assertEqual(naming.token_count(''),0)

if __name__ == '__main__': unittest.main()
