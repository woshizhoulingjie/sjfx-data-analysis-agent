"""Production review regressions: bounded work, delayed retries and honest progress."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch

import pytest

import test_catalog_batch as fixtures
from config import Config
from large_package_worker import LargePackageWorker
from services import large_package_catalog_batch as batch
from services import large_package_catalog_naming as naming
from services.large_package_isolated import LargePackageStore


@pytest.fixture
def case():
    result = fixtures.CatalogBatchTests()
    result.setUp()
    try:
        yield result
    finally:
        result.tearDown()


def test_unreadable_held_out_member_does_not_exhaust_model_naming(case):
    import numpy as np
    gid = case.gids[0]
    path = 'unreadable.txt'
    case.store.upsert_file(case.pid, {'path': path, 'name': path, 'size': 0})
    case.store.update_file(case.pid, path, quick_status='completed', local_summary={})
    with closing(case.store._connect()) as conn:
        conn.execute('INSERT INTO large_catalog_members VALUES(?,?,?,?,?)',
                     (case.pid, path, gid, 'empty-material', np.eye(4, dtype=np.float32)[0].tobytes()))
    from services.large_package_catalog import material as source_material
    assert source_material(case.store.get_file(case.pid, path)) is None
    prepared = naming.bundle(case.store, case.pid, gid)
    assert {e['source_path'] for e in prepared['evidence']} == {'file0.txt'}
    assert prepared['held_out'] == []
    assert 'held_out' not in json.loads(prepared['user'])
    assert batch.process_batch(case.store, case.pid, model_client=case.model)
    with closing(case.store._connect()) as conn:
        job = conn.execute('SELECT status,attempts,error FROM lp_catalog_jobs '
                           'WHERE package_id=? AND group_id=?', (case.pid, gid)).fetchone()
        label = conn.execute('SELECT model FROM lp_catalog_labels WHERE package_id=? AND group_id=?',
                             (case.pid, gid)).fetchone()
        assert tuple(job) == ('completed', 0, '')
        assert label[0] == case.model.model
        assert conn.execute('SELECT COUNT(*) FROM large_catalog_members '
                            'WHERE package_id=? AND group_id=?', (case.pid, gid)).fetchone()[0] == 2


def test_held_out_skips_restricted_material_and_keeps_readable_comparison(case):
    gid = case.gids[0]
    summary = case.store.get_file(case.pid, 'file1.txt')['local_summary']
    summary['portrait']['recognition_status'] = 'restricted'
    case.store.update_file(case.pid, 'file1.txt', local_summary=summary)
    with closing(case.store._connect()) as conn:
        conn.execute('UPDATE large_catalog_members SET group_id=? '
                     'WHERE package_id=? AND path IN (?,?)',
                     (gid, case.pid, 'file1.txt', 'file2.txt'))
    from services.large_package_catalog import material as source_material
    assert source_material(case.store.get_file(case.pid, 'file1.txt')) is None
    held = naming.held_out_members(case.store, case.pid, gid, ['file0.txt'])
    assert [h['source_path'] for h in held] == ['file2.txt']
    assert 'Device evidence verification' in held[0]['text']


@pytest.mark.parametrize('model_naming', [False, True], ids=['legacy-naming', 'model-naming'])
def test_incremental_lane_executes_each_stage_once(case, model_naming):
    worker = LargePackageWorker(case.store)
    with patch.object(Config, 'LP_CATALOG_MODEL_NAMING', model_naming), \
            patch('services.large_package_semantic.embed_batch', return_value=32) as embed, \
            patch('services.large_package_feature_delta.ensure_package') as features, \
            patch.object(worker, '_ensure_km_finalized') as km, \
            patch('services.large_package_semantic.name_batch') as names:
        worker._catalog_incremental(case.pid)
    assert embed.call_count == features.call_count == km.call_count == 1
    # Model naming runs in its own lane; the incremental lane must not also
    # invoke the legacy naming stage when that mode is enabled.
    assert names.call_count == (0 if model_naming else 1)
    assert km.call_args.kwargs == {'claimed': 32}
    assert features.call_args.kwargs == {'max_batches': 2}


def test_unusable_refresh_retracts_stale_vector_only_under_owned_lease(case):
    from services import large_package_semantic as semantic
    path = 'file0.txt'
    with closing(case.store._connect()) as conn:
        conn.execute("UPDATE queue SET status='running',lease_token='current' "
                     "WHERE package_id=? AND path=? AND stage='catalog'", (case.pid, path))
        before = tuple(conn.execute('SELECT * FROM large_catalog_members '
                                    'WHERE package_id=? AND path=?', (case.pid, path)).fetchone())
    semantic._mark_unusable(case.store, case.pid, path, 'garbled', 'expired')
    with closing(case.store._connect()) as conn:
        assert tuple(conn.execute('SELECT * FROM large_catalog_members '
                                  'WHERE package_id=? AND path=?', (case.pid, path)).fetchone()) == before
    semantic._mark_unusable(case.store, case.pid, path, 'garbled', 'current')
    with closing(case.store._connect()) as conn:
        assert conn.execute('SELECT 1 FROM large_catalog_members WHERE package_id=? AND path=?',
                            (case.pid, path)).fetchone() is None
        assert conn.execute('SELECT COUNT(*) FROM large_catalog_members WHERE package_id=?',
                            (case.pid,)).fetchone()[0] == 3
        row = conn.execute("SELECT status,lease_token FROM queue WHERE package_id=? AND path=? "
                           "AND stage='catalog'", (case.pid, path)).fetchone()
        assert tuple(row) == ('completed', None)
    info = case.store.get_file(case.pid, path)['local_summary']['portrait']
    assert info['catalog_quality'] == 'garbled'
    assert info['catalog_status'] == 'deferred'
    assert info['catalog_error'] == semantic.QUALITY_NOTE['garbled']


def test_fallback_preserves_retryable_deferred_jobs_and_allows_exhausted(case):
    worker = LargePackageWorker(case.store)
    naming.queue_groups(case.store, case.pid)
    cap = max(1, int(getattr(Config, 'LP_NAME_MAX_ATTEMPTS', 5) or 5))
    with closing(case.store._connect()) as conn:
        conn.execute("UPDATE lp_catalog_jobs SET status='deferred',attempts=0,next_at=?",
                     (time.time() + 120,))
    assert worker._name_fallback(case.pid) == 0
    assert worker._naming_jobs_pending(case.pid) == 4
    with closing(case.store._connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM lp_catalog_labels').fetchone()[0] == 0
        conn.execute('UPDATE lp_catalog_jobs SET attempts=?', (cap,))
    assert worker._naming_jobs_pending(case.pid) == 0
    assert worker._name_fallback(case.pid) == 4
    with closing(case.store._connect()) as conn:
        assert all(tuple(row) == ('deterministic', 1) for row in conn.execute(
            'SELECT model,needs_review FROM lp_catalog_labels'))


def test_delayed_retry_is_not_treated_as_drained_or_finished(case):
    worker = LargePackageWorker(case.store)
    naming.queue_groups(case.store, case.pid)
    with closing(case.store._connect()) as conn:
        conn.execute("UPDATE lp_catalog_jobs SET status='deferred',attempts=0,next_at=?",
                     (time.time() + 120,))
    with patch.object(batch, 'process_batch', return_value=False), \
            patch.object(worker, '_name_fallback') as fallback, \
            patch.object(worker, '_merge_topics_once', return_value=False), \
            patch.object(worker, '_build_overview', return_value=False), \
            patch.object(worker, '_overview_catchup', return_value=False):
        assert worker._submit_naming(case.pid)
        worker._naming_future.result(timeout=10)
    fallback.assert_not_called()
    assert case.pid not in worker._catalog_settled
    case.store.update_package(case.pid, message=worker.CATALOG_REFINING_NOTE)
    with patch.object(naming, 'state', return_value={'active': False}), \
            patch.object(naming, 'validate_catalog', return_value={'blocking': False}):
        assert not worker._mark_catalog_refined(case.pid)


def test_concurrent_claim_loss_has_no_false_progress_or_extra_request(case):
    original = naming.bundle
    def lost_to_other_lane(*args):
        result = original(*args)
        with closing(case.store._connect()) as conn:
            conn.execute("UPDATE lp_catalog_jobs SET status='running',leased=?", (time.time(),))
        return result
    with patch.object(naming, 'bundle', side_effect=lost_to_other_lane):
        assert batch.process_batch(case.store, case.pid, model_client=case.model) is False
    assert case.model.calls == 0
    with closing(case.store._connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM lp_catalog_calls').fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM lp_catalog_jobs WHERE status='running'").fetchone()[0] == 4


def test_effective_batch_prompt_is_used_for_actual_budget(case):
    # Force a difference from the legacy constant, as compact output does.
    system = batch.BATCH_SYSTEM + ' Additional current output contract.' * 30
    original = naming.reserve
    reservations, requests = [], []
    def reserve(*args, **kwargs):
        reservations.append(args[4])
        return original(*args, **kwargs)
    chat = case.model.chat
    def capture(prompt, user, **kwargs):
        requests.append((prompt, user, kwargs))
        return chat(prompt, user, **kwargs)
    with patch.object(batch, 'batch_system', return_value=system) as prompt, \
            patch.object(naming, 'reserve', side_effect=reserve), \
            patch.object(case.model, 'chat', side_effect=capture):
        assert batch.process_batch(case.store, case.pid, model_client=case.model)
    assert prompt.call_count == 1
    assert len(requests) == len(reservations) == 1
    actual_prompt, user, kwargs = requests[0]
    assert actual_prompt == system
    assert reservations[0] == naming.token_count(system + '\n' + user) + 64 + kwargs['max_tokens']


def test_overview_deadline_survives_continuous_revision_changes(case):
    worker = LargePackageWorker(case.store)
    worker._overview_max_wait = 900
    naming.queue_groups(case.store, case.pid)
    captured = []
    def build(*args, **kwargs):
        captured.append(kwargs)
        return {'published': True}
    with patch('services.large_package_reporting.published_report', return_value=None), \
            patch('services.large_package_reporting.build_overview', side_effect=build):
        assert not worker._build_overview(case.pid, 'catalog')
        initial = worker._overview_state[case.pid][2]
        for elapsed in (60, 300, 899):
            with closing(case.store._connect()) as conn:
                conn.execute('UPDATE large_catalog_groups SET content_revision=content_revision+1')
            with patch('large_package_worker.time.time', return_value=initial + elapsed):
                assert not worker._build_overview(case.pid, 'catalog')
            assert worker._overview_state[case.pid][2] == initial
        with closing(case.store._connect()) as conn:
            conn.execute('UPDATE large_catalog_groups SET content_revision=content_revision+1')
        with patch('large_package_worker.time.time', return_value=initial + 901):
            assert worker._build_overview(case.pid, 'catalog')
    assert len(captured) == 1
    assert '本版基于已定稿部分' in captured[0]['note']
    assert worker._overview_state[case.pid][2] == initial


@pytest.mark.parametrize('message,category,min_delay,max_delay', [
    ('模型正在处理其他请求，请稍后重试', naming.FAILURE_BUSY, 6.4, 9.6),
    ('模型暂不可用，请稍后重试', naming.FAILURE_UNAVAILABLE, 120, 120),
], ids=['busy', 'unavailable'])
def test_model_failure_has_group_detail_and_durable_backoff(case, message, category, min_delay, max_delay):
    from services.ollama import LocalModelError
    before = time.time()
    with patch.dict('os.environ', {'SJFX_LP_NAME_BUSY_BACKOFF_SECONDS': '8'}), \
            patch.object(Config, 'LP_NAME_INFRA_BACKOFF_SECONDS', 120, create=True), \
            patch.object(case.model, 'chat', side_effect=LocalModelError(message)) as chat:
        assert batch.process_batch(case.store, case.pid, model_client=case.model)
        after = time.time()
        assert batch.process_batch(case.store, case.pid, model_client=case.model) is False
        assert chat.call_count == 1
    with closing(case.store._connect()) as conn:
        rows = conn.execute('SELECT status,attempts,next_at FROM lp_catalog_jobs').fetchall()
        call = conn.execute('SELECT status,detail FROM lp_catalog_calls').fetchone()
        requests = conn.execute('SELECT requests FROM lp_catalog_rounds').fetchone()[0]
    assert len(rows) == 4
    assert all(row['status'] == 'deferred' and row['attempts'] == 0
               and before + min_delay <= row['next_at'] <= after + max_delay for row in rows)
    assert call['status'] == 'refunded'
    assert requests == 0
    detail = json.loads(call['detail'])
    assert len(detail['failed']) == 4
    assert {item['group'] for item in detail['failed']} == set(case.gids)
    assert all(item['category'] == category for item in detail['failed'])


def test_batch_persists_the_same_budget_qualified_cache_key_as_single(case):
    with patch.dict('os.environ', {'SJFX_LP_CACHE_KEY_UNIFIED': '1'}):
        materials = [naming.bundle(case.store, case.pid, gid) for gid in case.gids]
        expected = {naming.cache_key(item, naming.output_budget_for(item)) for item in materials}
        assert batch.process_batch(case.store, case.pid, model_client=case.model)
        with closing(case.store._connect()) as conn:
            cached = {row[0] for row in conn.execute('SELECT digest FROM lp_catalog_cache')}
            assert cached == expected
            # Simulate a replay of the same evidence without existing labels.
            conn.execute('DELETE FROM lp_catalog_labels')
            conn.execute("UPDATE lp_catalog_jobs SET status='queued',next_at=0")
        assert batch.process_batch(case.store, case.pid, model_client=case.model)
    assert case.model.calls == 1
    assert naming.state(case.store, case.pid)['named_nodes'] == 4


def test_unavailable_client_before_claim_still_has_durable_backoff(case):
    with patch.object(naming, 'client', return_value=None):
        assert batch.process_batch(case.store, case.pid)
        with closing(case.store._connect()) as conn:
            rows = conn.execute('SELECT status,attempts,next_at FROM lp_catalog_jobs').fetchall()
            assert all(row['status'] == 'deferred' and row['attempts'] == 0 and row['next_at'] > time.time() for row in rows)
            assert conn.execute('SELECT COUNT(*) FROM lp_catalog_calls').fetchone()[0] == 0
        assert batch.process_batch(case.store, case.pid) is False


def test_preparation_guard_avoids_duplicate_work_and_keeps_two_model_calls_inflight(case):
    # Separate stores deliberately address the same DB; the guard must share
    # identity while the model barrier proves it is released before generation.
    second_store = LargePackageStore(case.store.db_path)
    assert batch._preparation_lock(case.store, case.pid) is batch._preparation_lock(second_store, case.pid)
    barrier = threading.Barrier(2)
    chat, prepared_groups = case.model.chat, []
    original_bundle = naming.bundle
    def bundle(*args, **kwargs):
        prepared_groups.append(args[2])
        return original_bundle(*args, **kwargs)
    def parallel_chat(*args, **kwargs):
        barrier.wait(timeout=10)
        return chat(*args, **kwargs)
    with patch.object(Config, 'LP_CATALOG_BATCH_SIZE', 2), \
            patch.object(Config, 'LP_CATALOG_PREPARE_LIMIT', 2), \
            patch.object(naming, 'bundle', side_effect=bundle), \
            patch.object(case.model, 'chat', side_effect=parallel_chat), \
            ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(batch.process_batch, store, case.pid, model_client=case.model)
                   for store in (case.store, second_store)]
        assert all(future.result(timeout=20) for future in futures)
    assert case.model.calls == 2
    assert len(prepared_groups) == len(set(prepared_groups)) == 4
    assert naming.state(case.store, case.pid)['named_nodes'] == 4


def test_preparation_guard_wait_is_cancelable_without_touching_jobs(case):
    lock = batch._preparation_lock(case.store, case.pid)
    lock.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(batch.process_batch, case.store, case.pid,
                                 lambda: True, case.model)
            assert future.result(timeout=3) is False
    finally:
        lock.release()
    assert case.model.calls == 0
    assert batch.process_batch(case.store, case.pid, model_client=case.model)


def test_complete_independent_leaves_are_not_reported_as_pending_themes(case):
    worker = LargePackageWorker(case.store)
    with closing(case.store._connect()) as conn:
        conn.execute('DELETE FROM lp_catalog_jobs')
        conn.execute('INSERT OR REPLACE INTO lp_catalog_tree(package_id,fingerprint,payload) '
                     'VALUES(?,?,?)',
                     (case.pid, 'fixture', json.dumps({'nodes': [], 'independent_leaf_ids': case.gids})))
    built = []
    with patch.object(Config, 'LP_TOPIC_MERGE_MIN_GROUPS', 2), \
            patch('services.large_package_catalog_tree.stale', return_value=False), \
            patch('services.large_package_reporting.published_report', return_value=None), \
            patch('services.large_package_reporting.build_overview',
                  side_effect=lambda *args, **kwargs: built.append(kwargs) or {'published': True}):
        assert not worker._build_overview(case.pid, 'catalog')
        assert worker._build_overview(case.pid, 'catalog')
    assert '语义目录仍在后台完善' not in built[0]['note']


def test_exhausted_topic_names_stay_partial_and_have_review_status(case):
    worker = LargePackageWorker(case.store)
    case.store.update_package(case.pid, message=worker.CATALOG_REFINING_NOTE)
    with closing(case.store._connect()) as conn:
        conn.execute('DELETE FROM lp_catalog_jobs')
        conn.execute('INSERT OR REPLACE INTO lp_catalog_tree(package_id,fingerprint,payload) VALUES(?,?,?)',
                     (case.pid, 'fixture', json.dumps({'nodes': [], 'pending_names': True,
                                                     'topic_regeneration_attempts': 999})))
        conn.execute("UPDATE queue SET status='completed' WHERE stage IN ('catalog','quick','inventory')")
    built = []
    with patch.object(naming, 'state', return_value={'active': False}), \
            patch.object(naming, 'validate_catalog', return_value={'blocking': False}), \
            patch('services.large_package_catalog_tree.stale', return_value=False), \
            patch('services.large_package_reporting.published_report', return_value=None), \
            patch('services.large_package_reporting.build_overview',
                  side_effect=lambda *args, **kwargs: built.append(kwargs) or {'published': True}):
        assert worker._mark_catalog_refined(case.pid)
        assert case.store.get(case.pid)['message'] == worker.CATALOG_REVIEW_MESSAGE
        assert not worker._mark_catalog_refined(case.pid)
        assert not worker._build_overview(case.pid, 'catalog')
        assert worker._build_overview(case.pid, 'catalog')
        with closing(case.store._connect()) as conn:
            conn.execute("UPDATE lp_catalog_tree SET payload='{}'")
        assert worker._mark_catalog_refined(case.pid)
        assert case.store.get(case.pid)['message'] == worker.CATALOG_REFINED_MESSAGE
    assert '主题命名未全部完成' in built[0]['note']
