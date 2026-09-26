import hashlib
import copy
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from unittest.mock import patch

from config import Config
from services import package_analysis as analysis
from services.storage import Storage
from services.unified_parser import UnifiedDocumentParser


class PipelinePerformanceTests(unittest.TestCase):
    def parse_fixture(self, largest=True, cancel_check=None, fail_path=None):
        candidates = [{'path': str(i), 'size': size} for i, size in enumerate([2, 8, 1, 9, 4, 7, 3, 6, 5])]
        started, completed = [], []
        def parse(_parser, _snapshot, path, *_args, **_kwargs):
            if path == fail_path:
                raise ValueError('fixture file failure')
            return {'source': {'path': path}}
        with ExitStack() as stack:
            stack.enter_context(patch.object(Config, 'PARSE_LARGEST_FIRST', largest))
            stack.enter_context(patch.object(analysis, '_init_parallel_parser'))
            stack.enter_context(patch.object(analysis, '_logical_source_snapshot', side_effect=lambda *_args: nullcontext('fixture')))
            stack.enter_context(patch.object(analysis, '_restore_source_provenance'))
            stack.enter_context(patch.object(analysis, '_parse_with_timeout_fallback', side_effect=parse))
            result = analysis._parallel_parse_files(
                object.__new__(UnifiedDocumentParser), candidates, '/fixture', 'full', max_workers=2,
                cancel_check=cancel_check, on_start=lambda index, node: started.append(index),
                on_complete=lambda *item: completed.append(item),
            )
        return result, started, completed

    def test_largest_first_preserves_indices_and_callbacks(self):
        result, started, completed = self.parse_fixture()
        self.assertEqual(started, [3, 1, 5, 7, 8, 4, 6, 0, 2])
        self.assertEqual([row[0] for row in result], list(range(9)))
        self.assertEqual(sorted(row[0] for row in completed), list(range(9)))
        self.assertTrue(all(row[2]['source']['path'] == str(row[0]) for row in result))

    def test_order_switch_and_recoverable_file_errors(self):
        result, started, completed = self.parse_fixture(largest=False, fail_path='4')
        self.assertEqual(started, list(range(9)))
        self.assertIsInstance(result[4][3], ValueError)
        self.assertIsNone(result[4][2])
        self.assertEqual(len(completed), 9)
        self.assertEqual(sum(row[3] is None for row in result), 8)

    def test_cancellation_remains_a_cancellation_not_file_failure(self):
        with self.assertRaises(analysis.ParseIsolationCancelled):
            self.parse_fixture(cancel_check=lambda: True)

    @staticmethod
    def documents():
        return {f'{i:02d}-{copy}.txt': {'source': {'sha256': f'hash-{i}'}, 'profile': f'profile-{i}'}
                for i in range(8) for copy in range(3)}

    class Embedder:
        model = 'fixture-embedding'
        max_chars = 1700
        def __init__(self):
            self.calls = []
        def embed(self, texts):
            self.calls.append(list(texts))
            return [[(value - 128) / 128 for value in hashlib.sha256(text.encode()).digest()[:12]]
                    for text in texts]

    def test_batch_size_keeps_clusters_fingerprints_and_aliases_identical(self):
        docs = self.documents()
        small, large = self.Embedder(), self.Embedder()
        profile = lambda path, document, **kwargs: document['profile']
        with patch.object(analysis, '_semantic_document_profile', side_effect=profile):
            before = analysis._semantic_document_clusters(docs, small, batch_size=6)
            progress = []
            after = analysis._semantic_document_clusters(docs, large, batch_size=24, batch_progress=lambda *row: progress.append(row))
        self.assertEqual(before, after)
        self.assertEqual(len(small.calls), 2)
        self.assertEqual(len(large.calls), 1)
        self.assertEqual(sum(map(len, large.calls)), 8)
        self.assertEqual(progress[-1], (24, 24))
        self.assertEqual(sorted(path for cluster in after[0] for path in cluster['members']), sorted(docs))

    def test_cache_remains_content_profile_and_model_scoped(self):
        def semantic_result(value):
            value = copy.deepcopy(value)
            def strip_counters(item):
                if isinstance(item, dict):
                    item.pop('embedding_cache_hits', None)
                    item.pop('embedding_cache_misses', None)
                    for child in item.values(): strip_counters(child)
                elif isinstance(item, (tuple, list)):
                    for child in item: strip_counters(child)
            strip_counters(value)
            return value
        docs, embedder = self.documents(), self.Embedder()
        docs['different-source.txt'] = {'source': {'sha256': 'other'}, 'profile': 'profile-0'}
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / 'cache.db')
            with patch.object(analysis, '_semantic_document_profile', side_effect=lambda path, document, **kwargs: document['profile']):
                first = analysis._semantic_document_clusters(docs, embedder, batch_size=24, storage=storage)
                self.assertEqual(sum(map(len, embedder.calls)), 9)
                second = analysis._semantic_document_clusters(docs, embedder, batch_size=6, storage=storage)
                self.assertEqual(semantic_result(first), semantic_result(second))
                self.assertEqual(len(embedder.calls), 1)
                embedder.model = 'different-model'
                analysis._semantic_document_clusters(docs, embedder, batch_size=24, storage=storage)
                self.assertEqual(len(embedder.calls), 2)


if __name__ == '__main__':
    unittest.main()
