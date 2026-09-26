"""Embedding dispatch must lend clients, not assign them by task ordinal."""
import threading
import unittest
from unittest.mock import patch
from services import large_package_semantic as semantic

class EmbeddingDispatchTests(unittest.TestCase):
    def test_skewed_chunks_never_reenter_busy_client_and_preserve_order(self):
        slow_started=threading.Event();third_started=threading.Event();collisions=[]
        class Client:
            max_batch_size=8
            model='same-model'
            def __init__(self):self.lock=threading.Lock()
            def embed(self,texts):
                if not self.lock.acquire(blocking=False):
                    collisions.append(texts[0]);third_started.set()
                    raise RuntimeError('client is still busy with first chunk')
                try:
                    if texts[0]=='t0':
                        slow_started.set()
                        if not third_started.wait(3):raise AssertionError('idle client did not pick up third chunk')
                    elif texts[0]=='t8':
                        if not slow_started.wait(3):raise AssertionError('first chunk did not start')
                    elif texts[0]=='t16':third_started.set()
                    return [[int(t[1:])] for t in texts]
                finally:self.lock.release()
        with patch.object(semantic,'embed_concurrency',return_value=2):
            out=semantic.embed_many(Client(),['t%d'%i for i in range(24)],factory=Client)
        self.assertEqual(out,[[i] for i in range(24)]);self.assertEqual(collisions,[])

    def test_duplicate_factory_client_falls_back_serially(self):
        calls=[]
        class Client:
            max_batch_size=2
            def embed(self,texts):calls.append(list(texts));return [[int(t)] for t in texts]
        original=Client();shared=Client();values=[str(i) for i in range(6)]
        with patch.object(semantic,'embed_concurrency',return_value=2):
            out=semantic.embed_many(original,values,factory=lambda:shared)
        self.assertEqual(out,[[i] for i in range(6)]);self.assertEqual(calls,[values])

    def test_failed_factory_falls_back_to_whole_serial_batch(self):
        calls=[]
        class Client:
            max_batch_size=2
            def embed(self,texts):calls.append(list(texts));return [[t] for t in texts]
        values=['a','b','c','d']
        with patch.object(semantic,'embed_concurrency',return_value=2):
            out=semantic.embed_many(Client(),values,factory=lambda:None)
        self.assertEqual(out,[[t] for t in values]);self.assertEqual(calls,[values])

    def test_factory_model_mismatch_never_mixes_vector_spaces(self):
        calls=[]
        class Client:
            max_batch_size=2
            def __init__(self,model='original'):self.model=model
            def embed(self,texts):calls.append(self.model);return [[self.model,t] for t in texts]
        values=['a','b','c','d']
        with patch.object(semantic,'embed_concurrency',return_value=2):
            out=semantic.embed_many(Client(),values,factory=lambda:Client('different'))
        self.assertEqual(out,[['original',t] for t in values]);self.assertEqual(calls,['original'])

    def test_each_chunk_length_checked_even_when_total_length_matches(self):
        class Client:
            max_batch_size=2
            def embed(self,texts):
                return [[0]] if texts[0]=='0' else [[2],[3],[99]]
        with patch.object(semantic,'embed_concurrency',return_value=2):
            with self.assertRaisesRegex(ValueError,'数量'):
                semantic.embed_many(Client(),['0','1','2','3'],factory=Client)

    def test_serial_result_length_is_also_checked(self):
        class Client:
            max_batch_size=2
            def embed(self,texts):return [[0]]
        with self.assertRaisesRegex(ValueError,'数量'):semantic.embed_many(Client(),['a','b'])

    def test_failed_chunk_propagates_without_a_synthetic_vector(self):
        class Client:
            max_batch_size=1
            def embed(self,texts):
                if texts[0]=='bad':raise RuntimeError('Connection refused')
                return [[t] for t in texts]
        with patch.object(semantic,'embed_concurrency',return_value=2):
            with self.assertRaisesRegex(RuntimeError,'Connection refused'):
                semantic.embed_many(Client(),['ok','bad','later'],factory=Client)

    def test_request_concurrency_stays_bounded(self):
        barrier=threading.Barrier(2,timeout=3);lock=threading.Lock();counts={'active':0,'peak':0}
        class Client:
            max_batch_size=1
            def embed(self,texts):
                with lock:counts['active']+=1;counts['peak']=max(counts['peak'],counts['active'])
                try:barrier.wait();return [[t] for t in texts]
                finally:
                    with lock:counts['active']-=1
        with patch.object(semantic,'embed_concurrency',return_value=2):
            out=semantic.embed_many(Client(),list('abcdefgh'),factory=Client)
        self.assertEqual(out,[[t] for t in 'abcdefgh']);self.assertEqual(counts['peak'],2)

if __name__=='__main__':unittest.main()
