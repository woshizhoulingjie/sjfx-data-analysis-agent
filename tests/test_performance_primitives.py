import threading
import unittest
from services.performance_primitives import WeightedLRU, parse_order, unique_embedding_paths

class PerformancePrimitivesTests(unittest.TestCase):
    def test_lru_capacity_and_zero(self):
        cache = WeightedLRU(2, 10)
        cache.put('a', 0, 3); cache.put('b', 2, 3)
        self.assertEqual(cache.get('a'), 0)
        cache.put('c', 3, 3)
        self.assertIsNone(cache.get('b'))
        self.assertEqual(cache.get('a'), 0)
    def test_weight_and_oversized_entries(self):
        cache = WeightedLRU(9, 6)
        cache.put('a', 1, 4); cache.put('b', 2, 4)
        self.assertIsNone(cache.get('a'))
        cache.put('b', 3, 2); cache.put('c', 4, 4)
        self.assertEqual(cache.get('b'), 3)
        cache.put('huge', 9, 20)
        self.assertIsNone(cache.get('huge'))
        cache.clear(); self.assertIsNone(cache.get('b'))
    def test_thread_safe_bound(self):
        cache = WeightedLRU(20, 60)
        def work():
            for i in range(1000): cache.put(i % 50, i, 3); cache.get(i % 50)
        threads = [threading.Thread(target=work) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertLessEqual(len(cache._items), 20)
        self.assertLessEqual(cache._weight, 60)
    def test_largest_first_stable_and_reversible(self):
        files = [{'size':2}, {'size':100}, {'size':100}, {'size':None}, {'size':'bad'}, {'size':-9}]
        self.assertEqual(parse_order(files), [1,2,0,3,4,5])
        self.assertEqual(parse_order(files, False), list(range(6)))
        self.assertEqual(files[0]['size'], 2)
    def test_embedding_dedup_is_profile_scoped(self):
        keys = {'a':'same:profile1','b':'same:profile1','c':'same:profile2'}
        reps, aliases = unique_embedding_paths(['a','b','c'], keys)
        self.assertEqual(reps,['a','c'])
        self.assertEqual(aliases[keys['a']],['a','b'])
        self.assertEqual(aliases[keys['c']],['c'])

if __name__ == '__main__': unittest.main()
