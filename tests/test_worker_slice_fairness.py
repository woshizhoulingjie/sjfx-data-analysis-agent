"""Exercise ordinary claims and the real loop without starting application services."""
import ast
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from services.storage import Storage


@pytest.fixture
def queue(tmp_path):
    return Storage(tmp_path / 'queue.db')


def job(queue, priority, name):
    jid = queue.create_job(name, task_type='mail_research', options={'checkpoint': {'cursor': 7}})
    with queue._connect() as conn:
        conn.execute('UPDATE analysis_jobs SET priority=? WHERE id=?', (priority, jid))
    return jid


def test_yield_gives_import_a_turn_and_preserves_checkpoint(queue):
    research = job(queue, 120, 'research')
    importing = job(queue, 90, 'import')
    with queue._connect() as conn:
        conn.execute("UPDATE analysis_jobs SET task_type='mail_import' WHERE id=?", (importing,))
    assert queue.claim_next_job('worker')['id'] == research
    with queue._connect() as conn:
        conn.execute("UPDATE analysis_jobs SET result=?,progress=42 WHERE id=?", ('{"cursor":7}', research))
    assert queue.requeue_job_slice(research, 'saved slice')
    saved = queue.get_job(research)
    assert queue.claim_next_job('worker', exclude_job_id=research)['id'] == importing
    assert queue.get_job(research)['status'] == 'queued'
    resumed = queue.claim_next_job('worker')
    assert resumed['id'] == research and resumed['attempt_count'] == 2
    for key in ('options', 'result', 'priority', 'progress'):
        assert resumed[key] == saved[key]


def test_no_competitor_falls_back_immediately(queue):
    jid = job(queue, 120, 'only')
    queue.claim_next_job('worker')
    assert queue.requeue_job_slice(jid, 'continue')
    assert queue.claim_next_job('worker', exclude_job_id=jid)['id'] == jid


def test_remaining_jobs_keep_priority_and_fifo_and_default_is_unchanged(queue):
    yielded = job(queue, 120, 'yielded')
    first = job(queue, 90, 'first')
    second = job(queue, 90, 'second')
    urgent = job(queue, 130, 'urgent')
    assert queue.claim_next_job('worker', exclude_job_id=yielded)['id'] == urgent
    assert queue.claim_next_job('worker')['id'] == yielded
    assert queue.requeue_job_slice(yielded, 'continue')
    assert queue.claim_next_job('worker', exclude_job_id=yielded)['id'] == first
    assert queue.claim_next_job('worker', exclude_job_id=yielded)['id'] == second


def test_cancelled_delayed_and_running_jobs_are_not_competitors(queue):
    cancelled = job(queue, 150, 'cancelled')
    delayed = job(queue, 150, 'delayed')
    running = job(queue, 150, 'running')
    yielded = job(queue, 120, 'yielded')
    with queue._connect() as conn:
        conn.execute('UPDATE analysis_jobs SET cancel_requested=1 WHERE id=?', (cancelled,))
        conn.execute('UPDATE analysis_jobs SET available_at=? WHERE id=?', (time.time()+3600, delayed))
        conn.execute("UPDATE analysis_jobs SET status='running',worker_id='other' WHERE id=?", (running,))
    assert queue.claim_next_job('worker', exclude_job_id=yielded)['id'] == yielded
    assert queue.claim_next_job('worker', exclude_job_id=yielded) is None
    assert queue.get_job(running)['worker_id'] == 'other'
    assert queue.get_job(delayed)['status'] == 'queued'
    assert queue.get_job(cancelled)['cancel_requested']


def test_concurrent_claims_are_unique_with_soft_exclusion(queue):
    yielded = job(queue, 120, 'yielded')
    importing = job(queue, 90, 'import')
    second = Storage(queue.db_path)
    barrier = threading.Barrier(2)
    def claim(storage, worker):
        barrier.wait(timeout=5)
        return storage.claim_next_job(worker, exclude_job_id=yielded)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(claim, queue, 'one')
        b = pool.submit(claim, second, 'two')
        claimed = [a.result(timeout=10), b.result(timeout=10)]
    assert {value['id'] for value in claimed} == {yielded, importing}
    assert all(value['attempt_count'] == 1 for value in claimed)
    assert queue.claim_next_job('three') is None


class StopLoop(BaseException):
    pass


class SliceTimeout(Exception):
    pass


def loop_harness(claims, executions, requeue=True, database_retry=False):
    # Compile the actual run_forever function, avoiding app imports or real services.
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'worker.py').read_text(encoding='utf-8'))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'run_forever')
    module = ast.Module(body=[function], type_ignores=[])
    storage = MagicMock()
    storage.sqlite_checkpoint_interval = 60
    storage.recover_orphaned_jobs_after_lock.return_value = 0
    storage.reconcile_conversation_turn_jobs.return_value = 0
    storage.wake_due_file_retries.return_value = 0
    storage.claim_next_job.side_effect = claims
    storage.requeue_job_slice.return_value = requeue
    config = SimpleNamespace(WORKER_STALE_SECONDS=3600, HISTORY_RETENTION_DAYS=0,
                             HISTORY_MAX_SCANS=0, WORKER_POLL_SECONDS=0, MAX_JOB_RESUME_ATTEMPTS=10)
    namespace = {'storage': storage, 'Config': config, 'WORKER_ID': 'worker',
        'time': SimpleNamespace(monotonic=lambda: 0, time=lambda: 0, sleep=lambda seconds: None),
        'logger': MagicMock(), 'ensure_runtime_directories': lambda: None,
        '_configure_worker_logging': lambda: None, '_acquire_worker_lock': MagicMock(),
        '_resume_strict_imports': lambda: 0, '_resume_preliminary_imports': lambda: 0,
        '_background_resource_state': lambda task: (True, []),
        'execute_with_preliminary_cache': MagicMock(side_effect=executions),
        'JobCancelled': type('JobCancelled', (Exception,), {}),
        'JobExecutionTimeout': SliceTimeout,
        'RemoteJobError': type('RemoteJobError', (Exception,), {}),
        '_is_transient_database_lock': lambda exc: database_retry,
        '_requeue_after_database_lock': MagicMock(return_value=database_retry),
        '_finish_conversation_turn_after_worker_failure': MagicMock()}
    exec(compile(module, 'worker.py', 'exec'), namespace)
    with pytest.raises(StopLoop):
        namespace['run_forever']()
    return storage, namespace


def task(jid):
    return {'id': jid, 'task_type': 'mail_import', 'options': {}, 'scan_id': jid}


def test_worker_yield_is_once_and_failed_claim_does_not_consume_it():
    storage, env = loop_harness([task('A'), RuntimeError('database is locked'), task('B'), StopLoop()],
                               [{'_requeue_slice': True}, {}])
    calls = storage.claim_next_job.call_args_list
    assert [call[1] for call in calls] == [{}, {'exclude_job_id': 'A'}, {'exclude_job_id': 'A'}, {}]
    assert [call[0][0]['id'] for call in env['execute_with_preliminary_cache'].call_args_list] == ['A', 'B']


@pytest.mark.parametrize('result,requeue', [({'_requeue_slice': True}, False), ({}, True),
                                          ({'_defer_slice': True, '_defer_seconds': 10}, True)])
def test_worker_only_successful_normal_requeue_sets_yield(result, requeue):
    storage, env = loop_harness([task('A'), StopLoop()], [result], requeue=requeue)
    assert all(not call[1] for call in storage.claim_next_job.call_args_list)


def test_worker_exception_does_not_set_cooperative_yield():
    storage, env = loop_harness([task('A'), StopLoop()], [RuntimeError('execution failed')])
    assert all(not call[1] for call in storage.claim_next_job.call_args_list)
    assert storage.finalize_job.call_count == 1


def test_worker_database_lock_retry_does_not_set_cooperative_yield():
    storage, env = loop_harness([task('A'), StopLoop()], [RuntimeError('database is locked')], database_retry=True)
    assert all(not call[1] for call in storage.claim_next_job.call_args_list)
    assert env['_requeue_after_database_lock'].call_count == 1
    assert not storage.finalize_job.called


def test_worker_timeout_requeue_does_not_set_cooperative_yield():
    timed = {**task('A'), 'task_type': 'analyze_package', 'attempt_count': 1}
    storage, env = loop_harness([timed, StopLoop()], [SliceTimeout('slice timeout')])
    assert all(not call[1] for call in storage.claim_next_job.call_args_list)
    assert storage.requeue_job_slice.call_count == 1
    assert not storage.finalize_job.called

