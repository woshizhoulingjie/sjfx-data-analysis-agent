"""The import milestone is independent of optional deep-summary batches."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from config import Config
from large_package_worker import LargePackageWorker
from services.large_package_isolated import LargePackageStore


def test_catalog_finishes_import_without_deep_summaries_and_selection_keeps_it_complete():
    with tempfile.TemporaryDirectory() as directory, \
            patch.object(Config, 'SCAN_ALLOWED_ROOTS', ()), \
            patch.object(Config, 'LP_CATALOG_KMODE', False), \
            patch.object(Config, 'LP_OVERVIEW_EAGER', False):
        root = Path(directory)
        store = LargePackageStore(root / 'state.db')
        pid = store.create(root)['id']
        store.upsert_file(pid, {'path': 'a.txt', 'name': 'a.txt', 'size': 1, 'modified_at_ns': 1})
        with store._connect() as conn:
            conn.execute("INSERT INTO queue(package_id,path,stage,status,updated_at) VALUES(?,?,'catalog','queued',0)",
                         (pid, 'a.txt'))
        store.update_package(pid, status='waiting_for_selection', phase='catalog_ready', progress=70)
        worker = LargePackageWorker(store=store)
        try:
            with patch('services.large_package_catalog_naming.state', return_value={'active': False}), \
                    patch('services.large_package_catalog_naming.validate_catalog', return_value={'blocking': False}), \
                    patch('services.large_package_catalog_tree.stale', return_value=False), \
                    patch.object(worker, '_topic_rebuild_owed', return_value=False):
                # Pending catalog work still blocks the import milestone.
                assert not worker._mark_catalog_refined(pid)
                assert not store.workflow_snapshot(pid)['import_completed']
                with store._connect() as conn:
                    conn.execute("UPDATE queue SET status='completed' WHERE package_id=?", (pid,))
                assert worker._mark_catalog_refined(pid)
                assert not worker._mark_catalog_refined(pid)
            ready = store.workflow_snapshot(pid)
            assert ready['import_status'] == 'completed'
            assert ready['import_progress'] == 100
            assert ready['allowed_actions']['select']
            assert not ready['deep_status'].get('completed')
            stamp = store.get(pid)['counts']['import_completed_at']
            store.save_selection(pid, ['a.txt'])
            processing = store.workflow_snapshot(pid)
            assert processing['status'] == 'queued'
            assert processing['phase'] == 'deep_parse'
            assert processing['import_status'] == 'completed'
            assert processing['import_progress'] == 100
            assert store.pending_total(pid, 'deep') == 1
            history = store.list_history()['items'][0]
            assert history['status'] == 'completed'
            assert history['large_status'] == 'queued'
            assert history['progress'] == 100
            store.update_package(pid, status='paused', phase='paused', progress=0)
            assert store.workflow_snapshot(pid)['import_completed']
            assert store.get(pid)['counts']['import_completed_at'] == stamp
        finally:
            worker.preview_runner.close()


def test_partial_catalog_does_not_complete_import():
    with tempfile.TemporaryDirectory() as directory, \
            patch.object(Config, 'SCAN_ALLOWED_ROOTS', ()), \
            patch.object(Config, 'LP_CATALOG_KMODE', False):
        root = Path(directory)
        store = LargePackageStore(root / 'state.db')
        pid = store.create(root)['id']
        store.update_package(pid, status='waiting_for_selection', phase='catalog_ready', progress=70)
        with patch('services.large_package_isolated._catalog_publication_fact', return_value={'complete': False}):
            workflow = store.workflow_snapshot(pid)
        assert workflow['catalog_state'] == 'partial'
        assert workflow['import_status'] == 'running'
        assert workflow['import_progress'] == 70
        assert not workflow['import_completed']


def test_catalog_commit_does_not_overwrite_a_concurrent_deep_selection():
    with tempfile.TemporaryDirectory() as directory, \
            patch.object(Config, 'SCAN_ALLOWED_ROOTS', ()), \
            patch.object(Config, 'LP_CATALOG_KMODE', False):
        root = Path(directory)
        store = LargePackageStore(root / 'state.db')
        pid = store.create(root)['id']
        store.update_package(pid, status='waiting_for_selection', phase='catalog_ready')
        store.save_selection(pid, ['a.txt'])
        store.update_package(pid, progress=23, message='深度摘要处理中')
        assert store.mark_import_complete(pid, '智能目录已生成')
        item = store.get(pid)
        assert item['counts']['import_completed_at']
        assert item['import_progress'] == 100
        assert item['status'] == 'queued' and item['phase'] == 'deep_parse'
        assert item['progress'] == 23 and item['message'] == '深度摘要处理中'


def test_resuming_naming_does_not_claim_catalog_or_report_already_finished():
    with tempfile.TemporaryDirectory() as directory, \
            patch.object(Config, 'SCAN_ALLOWED_ROOTS', ()), \
            patch.object(Config, 'LP_CATALOG_KMODE', False):
        root = Path(directory)
        store = LargePackageStore(root / 'state.db')
        pid = store.create(root)['id']
        store.update_package(pid, status='running', phase='catalog_naming')
        worker = LargePackageWorker(store=store)
        try:
            with patch('services.large_package_catalog_naming.queue_groups'), \
                    patch('services.large_package_catalog_naming.ensure_round'), \
                    patch.object(worker, '_mark_catalog_refined', return_value=False):
                assert worker._naming_one(store.get(pid))
            pending = store.workflow_snapshot(pid)
            assert pending['phase'] == 'catalog_ready'
            assert pending['status'] == 'waiting_for_selection'
            assert not pending['import_completed']
        finally:
            worker.preview_runner.close()
