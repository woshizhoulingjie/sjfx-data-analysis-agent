"""Real worker loop/SQLite finalization after source-guarded progress rejection.

Startup and execution are bounded test adapters; no service, lock, supervised
child, model or lifespan starts. Claim/requeue/cancel/finalize/protection are real.
"""
import ast
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import test_pause_control_metadata as control
import test_package_cache_provenance as provenance
from sjfx_protection import business
from sjfx_protection.store import ProtectionError


class StopLoop(BaseException):
    pass


class JobCancelled(Exception):
    pass


class JobExecutionTimeout(Exception):
    pass


@pytest.fixture
def case():
    fixture = control.PauseControlMetadataTests()
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()


def run_bounded_loop(case, scenario, result, stale_result=False):
    store = case.storage
    case.p.enabled = False
    first = store.create_job('scan-A', task_type='analyze_package', owner_id='alice',
                             options={'protection_context': case.ctx})
    second = store.create_job('scan-A', task_type='mail_import', owner_id='alice')
    if result is not None:
        if stale_result:
            case.p.enabled = True
        store.update_job(first, result=result)
    if stale_result:
        store.update_job(first, status='completed')
        # A completed import receipt can be reused for confirmed analysis;
        # its old protected result is retained while only queue metadata moves.
        case.raw("UPDATE analysis_jobs SET status='queued',stage='queued',finished_at=NULL WHERE id=?", (first,))
        case.package.current['A'] = provenance.context(version='v2')
    case.raw('UPDATE analysis_jobs SET priority=200 WHERE id=?', (first,))
    case.raw('UPDATE analysis_jobs SET priority=100 WHERE id=?', (second,))
    case.p.enabled = True
    before_proofs = case.proof_rows()

    path = Path(__file__).resolve().parents[1] / 'worker.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    names = {'run_forever', '_finalize_job_result'}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(functions) == 2
    real_claim = store.claim_next_job
    executed, denied = [], []

    def claim(worker_id, **kwargs):
        if len(executed) >= 2:
            raise StopLoop()
        return real_claim(worker_id, **kwargs)

    def execute(job, _cache):
        assert business.context() is None
        assert business.metadata_projection.get() is False
        executed.append(job['id'])
        if job['id'] == second:
            return None  # A completed metadata-only synthetic task.
        assert job['id'] == first
        if scenario in {'cancel', 'timeout_cancel'}:
            if stale_result:
                store.pause_package_analysis_jobs('scan-A')
            else:
                store.cancel_job(first)
            assert store.is_job_cancel_requested(first)
        elif scenario == 'timeout_requeue':
            # Make the independent next task win the normal priority claim.
            case.raw('UPDATE analysis_jobs SET priority=10 WHERE id=?', (first,))
        if scenario == 'cancel':
            raise JobCancelled('synthetic user cancellation')
        if scenario.startswith('timeout'):
            raise JobExecutionTimeout('synthetic slice timeout')
        raise RuntimeError('synthetic execution error')

    real_progress = store.update_analysis_progress_status

    def progress(*args, **kwargs):
        try:
            return real_progress(*args, **kwargs)
        except ProtectionError as error:
            denied.append(error.code)
            raise

    # Only process startup/maintenance is replaced. The queue and guarded
    # progress operations still execute against the actual Storage database.
    store.initialize = lambda: store
    store.recover_orphaned_jobs_after_lock = lambda: 0
    store.reconcile_conversation_turn_jobs = lambda: 0
    store.wake_due_file_retries = lambda: 0
    store.claim_next_job = claim
    store.update_analysis_progress_status = progress
    config = SimpleNamespace(WORKER_STALE_SECONDS=3600, HISTORY_RETENTION_DAYS=0,
                             HISTORY_MAX_SCANS=0, WORKER_POLL_SECONDS=0,
                             MAX_JOB_RESUME_ATTEMPTS=10)
    namespace = {'storage': store, 'Config': config, 'WORKER_ID': 'synthetic-worker',
        'time': SimpleNamespace(monotonic=lambda: 0, time=time.time, sleep=lambda _seconds: None),
        'logger': MagicMock(), 'ensure_runtime_directories': lambda: None,
        '_configure_worker_logging': lambda: None, '_acquire_worker_lock': MagicMock(),
        '_resume_strict_imports': lambda: 0, '_resume_preliminary_imports': lambda: 0,
        '_background_resource_state': lambda _job: (True, []),
        'execute_with_preliminary_cache': execute,
        'JobCancelled': JobCancelled, 'JobExecutionTimeout': JobExecutionTimeout,
        'RemoteJobError': type('RemoteJobError', (Exception,), {}),
        '_is_transient_database_lock': lambda _error: False,
        '_requeue_after_database_lock': lambda *_args: False,
        '_finish_conversation_turn_after_worker_failure': lambda *_args: None}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), 'exec'), namespace)
    from services import managed_entry
    original_ready = managed_entry.mark_role_ready
    managed_entry.mark_role_ready = lambda: None
    token = business.metadata_projection.set(False)
    try:
        with business.activate(None), pytest.raises(StopLoop):
            namespace['run_forever']()
    finally:
        managed_entry.mark_role_ready = original_ready
        business.metadata_projection.reset(token)
    assert executed == [first, second]
    assert len(denied) == 1
    assert namespace['logger'].warning.called
    states = dict(case.raw('SELECT id,status FROM analysis_jobs'))
    assert states[second] == 'completed'
    assert states[first] == ('cancelled' if scenario in {'cancel', 'timeout_cancel'}
                             else 'queued' if scenario == 'timeout_requeue' else 'failed')
    assert case.proof_rows() == before_proofs
    return first


@pytest.mark.parametrize('scenario', ['cancel', 'timeout_cancel', 'timeout_requeue', 'error'])
@pytest.mark.parametrize('result', [None, {'scan_id': 'scan-A', 'scan_available': True, '_await_selection': False}])
def test_progress_denial_preserves_rich_content_and_next_job_runs(case, scenario, result):
    payload = dict(case.inventory(), progress=37,
                   analysis_tree={'summary': 'synthetic protected source-derived content'})
    case.storage.save_analysis_progress('scan-A', payload)
    before, proofs = case.progress_row(), case.proof_rows()
    first = run_bounded_loop(case, scenario, result)
    assert case.progress_row() == before
    assert case.proof_rows() == proofs
    persisted_result = case.raw('SELECT result FROM analysis_jobs WHERE id=?', (first,))[0][0]
    assert (json.loads(persisted_result) if persisted_result else None) == result
    assert case.storage.get_analysis_progress('scan-A')['analysis_tree'] == payload['analysis_tree']
    with business.activate(None), pytest.raises(ProtectionError):
        case.pause()
    with case.metadata(owner='bob'), pytest.raises(ProtectionError):
        case.pause()
    assert case.progress_row() == before
    assert case.proof_rows() == proofs


@pytest.mark.parametrize('scenario', ['cancel', 'error'])
def test_historical_rich_progress_is_not_blessed_by_worker_cleanup(case, scenario):
    case.p.enabled = False
    case.storage.save_analysis_progress('scan-A', dict(case.inventory(), summary='synthetic historical content'))
    case.p.enabled = True
    before = case.progress_row()
    run_bounded_loop(case, scenario, None)
    assert case.progress_row() == before
    assert case.proof_rows() == []
    with pytest.raises(ProtectionError) as caught:
        case.storage.get_analysis_progress('scan-A')
    assert caught.value.code == 'cache_provenance_missing'


@pytest.mark.parametrize('scenario', ['cancel', 'timeout_cancel', 'error'])
def test_reused_completed_job_keeps_stale_rich_result_while_finalizing(case, scenario):
    progress = dict(case.inventory(), summary='synthetic progress from revision one')
    case.storage.save_analysis_progress('scan-A', progress)
    before = case.progress_row()
    result = {'scan_id': 'scan-A', 'summary': 'synthetic approved import result from revision one'}
    first = run_bounded_loop(case, scenario, result, stale_result=True)
    assert case.progress_row() == before
    assert json.loads(case.raw('SELECT result FROM analysis_jobs WHERE id=?', (first,))[0][0]) == result
    with pytest.raises(ProtectionError):
        case.storage.get_job(first)


def test_successful_result_publication_still_requires_original_source_receipts(case):
    case.p.enabled = False
    job_id = case.storage.create_job('scan-A', task_type='analyze_package', owner_id='alice')
    case.storage.claim_next_job('synthetic-worker')
    case.p.enabled = True
    proofs = case.proof_rows()
    with business.activate(None), pytest.raises(ProtectionError):
        case.storage.finalize_job(job_id, result={'summary': 'synthetic unauthorized content'})
    assert case.raw('SELECT status,result FROM analysis_jobs WHERE id=?', (job_id,)) == [('running', None)]
    assert case.proof_rows() == proofs
    final = case.storage.finalize_job(job_id, result={'summary': 'synthetic approved content'})
    assert final['status'] == 'completed'
    assert final['result'] == {'summary': 'synthetic approved content'}
    assert case.storage.get_job(job_id)['result'] == final['result']
