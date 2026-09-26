import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import numpy as np

from config import Config
from services.large_package_isolated import LargePackageStore
from services import large_package_semantic as semantic
from services import large_package_catalog_naming as naming
from services import catalog_prefetch
from services.large_package_catalog import material
from services.large_package_catalog_batch import process_batch
import test_catalog_batch as fixtures


class CatalogPreparationPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for key, value in {'SCAN_ALLOWED_ROOTS': (), 'LP_CATALOG_MODEL_NAMING': True,
                           'LP_CATALOG_SECONDS': 60, 'LP_CATALOG_BATCH_SIZE': 4,
                           'LP_CATALOG_PREPARE_LIMIT': 4, 'LP_CATALOG_BATCH_INPUT_TOKENS': 20000,
                           'LP_CATALOG_TOTAL_TOKENS': 100000}.items():
            self.enterContext(patch.object(Config, key, value))
        self.store = LargePackageStore(self.root / 'state.db')
        self.pid = self.store.create(self.root)['id']
        self.model = fixtures.BatchModel()
        for i in range(12):
            path = 'file%02d.txt' % i
            self.store.upsert_file(self.pid, {'path': path, 'name': path, 'size': 100})
            self.store.update_file(self.pid, path, quick_status='completed',
                local_summary={'sample': ('Device evidence verification and trusted state confirmation. %d ' % i) * 3,
                               'portrait': {'fields': [], 'headings': []}})
            digest = material(self.store.get_file(self.pid, path))[1]
            semantic._group(self.store, self.pid, path, digest, np.eye(12, dtype=np.float32)[i], 'fixture')
        semantic.name_batch(self.store, self.pid)
        self.store.update_package(self.pid, status='waiting_for_selection', phase='catalog_ready', progress=70)

    def test_prepares_one_batch_and_eventually_names_every_group(self):
        before = [self.store.get_file(self.pid, 'file%02d.txt' % i) for i in range(12)]
        with closing(self.store._connect()) as connection:
            membership = [tuple(row) for row in connection.execute('SELECT * FROM large_catalog_members ORDER BY group_id, path')]
        with patch.object(naming, 'bundle', wraps=naming.bundle) as bundle:
            for expected in (4, 8, 12):
                self.assertTrue(process_batch(self.store, self.pid, model_client=self.model))
                self.assertEqual(bundle.call_count, expected)
                self.assertEqual(naming.state(self.store, self.pid)['named_nodes'], expected)
        self.assertEqual(self.model.calls, 3)
        after = [self.store.get_file(self.pid, 'file%02d.txt' % i) for i in range(12)]
        for old, new in zip(before, after):
            for key in ('local_summary', 'deep_summary', 'sha256', 'deep_status', 'quick_status'):
                self.assertEqual(old[key], new[key], key)
        with closing(self.store._connect()) as connection:
            self.assertEqual(membership, [tuple(row) for row in connection.execute('SELECT * FROM large_catalog_members ORDER BY group_id, path')])

    def test_mixed_routes_expand_window_until_batch_is_full(self):
        original, prepared = naming.bundle, []
        def mixed(store, package_id, group_id):
            result = dict(original(store, package_id, group_id))
            prepared.append(group_id)
            result['route'] = 'clear' if 1 < len(prepared) <= 4 else 'mixed'
            return result
        with patch.object(naming, 'bundle', side_effect=mixed):
            self.assertTrue(process_batch(self.store, self.pid, model_client=self.model))
        self.assertEqual(len(prepared), 8)
        self.assertEqual(naming.state(self.store, self.pid)['named_nodes'], 4)
        self.assertEqual(self.model.calls, 1)

    def test_invalid_early_candidates_do_not_starve_later_groups(self):
        original, prepared = naming.bundle, []
        def failed_first(store, package_id, group_id):
            prepared.append(group_id)
            if len(prepared) <= 4:
                raise ValueError('fixture: unavailable evidence')
            return original(store, package_id, group_id)
        with patch.object(naming, 'bundle', side_effect=failed_first):
            self.assertTrue(process_batch(self.store, self.pid, model_client=self.model))
        self.assertEqual(len(prepared), 8)
        self.assertEqual(naming.state(self.store, self.pid)['named_nodes'], 4)
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM lp_catalog_jobs WHERE package_id=? AND status='running'", (self.pid,)
            ).fetchone()[0], 0)

    def test_prefetch_overlaps_model_wait_without_publishing_or_extra_requests(self):
        self.addCleanup(catalog_prefetch.shutdown)
        original, warmed, ready = naming.bundle, [], threading.Event()
        def prepare(store, package_id, group_id):
            result = original(store, package_id, group_id)
            if threading.current_thread().name.startswith('sjfx-evidence-prefetch'):
                warmed.append(group_id)
                if len(warmed) == 4: ready.set()
            return result
        observed = []
        def while_model_waits():
            observed.append(ready.wait(5))
            observed.append(naming.state(self.store, self.pid)['named_nodes'])
        self.model.pause = while_model_waits
        with patch.object(naming, 'bundle', side_effect=prepare), patch.object(Config, 'LP_EVIDENCE_PREFETCH', True), patch.object(Config, 'LP_EVIDENCE_PREFETCH_GROUPS', 4):
            with catalog_prefetch.scope():
                self.assertTrue(process_batch(self.store, self.pid, model_client=self.model))
        self.assertEqual(observed, [True, 0])
        self.assertEqual(len(set(warmed)), 4)
        self.assertEqual(naming.state(self.store, self.pid)['named_nodes'], 4)
        self.assertEqual(self.model.calls, 1)


if __name__ == '__main__':
    unittest.main()
