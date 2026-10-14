"""Typed worker control metadata never grants access to cached content."""
import ast
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sjfx_protection import business, cache
from sjfx_protection.sql_cache import GuardedConnection
from sjfx_protection.storage_cache import SPECS
from sjfx_protection.store import ProtectionError


@pytest.fixture
def guard(monkeypatch):
    monkeypatch.setattr(business, 'enabled', lambda: True)
    monkeypatch.setattr(business, 'context', lambda: {'owner': 'alice'})
    monkeypatch.setattr(cache, 'require_payload', Mock(side_effect=ProtectionError('unapproved content')))
    monkeypatch.setattr(cache, 'record_payloads', Mock())
    raw = sqlite3.connect(':memory:')
    raw.row_factory = sqlite3.Row
    raw.executescript('CREATE TABLE scans(id TEXT PRIMARY KEY, owner_id TEXT);'
                      "INSERT INTO scans VALUES('scan','alice');"
                      'CREATE TABLE analysis_jobs(id TEXT PRIMARY KEY, scan_id TEXT, owner_id TEXT, result TEXT);'
                      'CREATE TABLE analysis_progress(scan_id TEXT PRIMARY KEY, payload TEXT);'
                      'CREATE TABLE summaries(scan_id TEXT PRIMARY KEY, payload TEXT);'
                      "INSERT INTO analysis_jobs VALUES('job','scan','alice',NULL);")
    checked = GuardedConnection(raw, 'isolated-progress', SPECS,
                                lambda *_args: [{'owner': 'alice', 'scope': 'scan'}]
                                if (business.context() or {}).get('owner') == 'alice' else [])
    yield checked
    checked.rollback()
    checked.cleanup()
    raw.close()


def test_initial_job_result_can_transition_to_selection_metadata(guard):
    result = {'scan_id': 'scan', 'scan_available': True, '_await_selection': True}
    guard.execute('UPDATE analysis_jobs SET result=? WHERE id=?', (json.dumps(result), 'job'))
    guard.commit()
    assert json.loads(guard.execute('SELECT result FROM analysis_jobs WHERE id=?', ('job',)).fetchone()[0]) == result
    cache.require_payload.assert_not_called()
    assert not list(guard.raw.execute('SELECT * FROM _sjfx_content_changes'))


def test_fixed_failure_progress_with_owned_scan_needs_no_content_source(guard):
    progress = {'schema_version': 'analysis-progress/1.0', 'progress': 0,
                'status': 'failed', 'stage': 'failed', 'message': '分析失败，请查看任务错误状态。'}
    guard.execute('INSERT OR REPLACE INTO analysis_progress(scan_id,payload) VALUES (?,?)',
                  ('scan', json.dumps(progress)))
    guard.commit()
    assert json.loads(guard.execute('SELECT payload FROM analysis_progress WHERE scan_id=?', ('scan',)).fetchone()[0]) == progress
    cache.require_payload.assert_not_called()


@pytest.mark.parametrize('table,column,payload', [
    ('analysis_jobs', 'result', {'scan_id': 'scan', 'summary': 'private body'}),
    ('analysis_jobs', 'result', {'scan_id': 'foreign', 'scan_available': True}),
    ('analysis_jobs', 'result', {'scan_id': 'private body'}),
    ('analysis_progress', 'payload', {'status': 'failed', 'message': 'private body'}),
    ('analysis_progress', 'payload', {'status': 'failed', 'text': 'private body'}),
    ('analysis_progress', 'payload', {'coverage': {'summary': 'private body'}}),
    ('summaries', 'payload', {'status': 'failed'}),
])
def test_control_metadata_does_not_exempt_content_writes(guard, table, column, payload):
    key = 'id' if table == 'analysis_jobs' else 'scan_id'
    with pytest.raises(ProtectionError):
        guard.execute(f'INSERT OR REPLACE INTO {table}({key},{column}) VALUES (?,?)',
                      ('job' if table == 'analysis_jobs' else 'scan', json.dumps(payload)))
        guard.commit()
    row = guard.raw.execute(f'SELECT {column} FROM {table} WHERE {key}=?',
                            ('job' if table == 'analysis_jobs' else 'scan',)).fetchone()
    assert row is None or row[0] is None


@pytest.mark.parametrize('owner', ['bob', None])
def test_control_metadata_cannot_bypass_stored_scan_ownership(guard, monkeypatch, owner):
    monkeypatch.setattr(business, 'context', lambda: {'owner': owner} if owner else None)
    with pytest.raises(ProtectionError):
        guard.execute('UPDATE analysis_jobs SET result=? WHERE id=?',
                      (json.dumps({'scan_id': 'scan'}), 'job'))
    assert guard.raw.execute('SELECT result FROM analysis_jobs').fetchone()[0] is None


def test_metadata_savepoint_keeps_outer_connection_rollback(guard):
    with pytest.raises(RuntimeError):
        with guard.raw:
            guard.execute('UPDATE analysis_jobs SET result=? WHERE id=?',
                          (json.dumps({'scan_id': 'scan'}), 'job'))
            raise RuntimeError('abort outer transaction')
    assert guard.raw.execute('SELECT result FROM analysis_jobs').fetchone()[0] is None


def test_metadata_then_rejected_content_rolls_back_together(guard):
    with pytest.raises(ProtectionError):
        try:
            guard.execute('UPDATE analysis_jobs SET result=? WHERE id=?',
                          (json.dumps({'scan_id': 'scan'}), 'job'))
            guard.execute('INSERT INTO summaries VALUES (?,?)', ('scan', json.dumps({'text': 'private body'})))
        except BaseException:
            guard.rollback()
            raise
    assert guard.raw.execute('SELECT result FROM analysis_jobs').fetchone()[0] is None
    assert guard.raw.execute('SELECT * FROM summaries').fetchone() is None


def test_old_content_cannot_be_read_or_relabelled_as_control_metadata(guard):
    guard.raw.execute('UPDATE analysis_jobs SET result=?', (json.dumps({'summary': 'private body'}),))
    guard.raw.execute('DELETE FROM _sjfx_content_changes')
    guard.raw.commit()
    with pytest.raises(ProtectionError):
        guard.execute('SELECT result FROM analysis_jobs').fetchall()
    with pytest.raises(ProtectionError):
        guard.execute('UPDATE analysis_jobs SET result=?', (json.dumps({'scan_id': 'scan'}),))
    assert 'private body' in guard.raw.execute('SELECT result FROM analysis_jobs').fetchone()[0]


def test_failed_progress_publication_does_not_stop_worker_or_skip_finalization():
    class StopLoop(BaseException):
        pass

    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / 'worker.py').read_text(encoding='utf-8'))
    names = {'run_forever', 'JobCancelled', 'JobExecutionTimeout', 'RemoteJobError', '_finalize_job_result'}
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    job = {'id': 'job', 'scan_id': 'scan', 'task_type': 'scan_and_analyze', 'options': {}}
    storage = Mock(sqlite_checkpoint_interval=60)
    for method in ('recover_orphaned_jobs_after_lock', 'reconcile_conversation_turn_jobs', 'wake_due_file_retries'):
        getattr(storage, method).return_value = 0
    storage.claim_next_job.side_effect = [job, StopLoop()]
    storage.scan_owned.return_value = True
    storage.update_analysis_progress_status.side_effect = ProtectionError('progress unavailable')
    failure = RuntimeError('content admission rejected')
    namespace = {'storage': storage, 'logger': Mock(), 'time': time,
                 'Config': SimpleNamespace(WORKER_STALE_SECONDS=300, HISTORY_RETENTION_DAYS=0,
                                           HISTORY_MAX_SCANS=0, MAX_JOB_RESUME_ATTEMPTS=3),
                 'WORKER_ID': 'isolated-test', 'ensure_runtime_directories': lambda: None,
                 '_configure_worker_logging': lambda: None, '_acquire_worker_lock': Mock(),
                 '_resume_strict_imports': lambda: 0, '_resume_preliminary_imports': lambda: 0,
                 '_background_resource_state': lambda _job: (True, []),
                 'execute_with_preliminary_cache': Mock(side_effect=failure),
                 '_is_transient_database_lock': lambda _exc: False,
                 '_finish_conversation_turn_after_worker_failure': Mock()}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(root / 'worker.py'), 'exec'), namespace)
    with pytest.raises(StopLoop):
        namespace['run_forever']()
    storage.finalize_job.assert_called_once_with('job', error=failure)
    assert storage.claim_next_job.call_count == 2


@pytest.fixture
def real_publication(tmp_path, monkeypatch):
    """Actual Storage/SQL guard and a real frozen local inspection receipt."""
    empty_env = tmp_path / 'empty.env'
    empty_env.write_text('', encoding='utf-8')
    for key, value in {'SJFX_ENV_FILE': str(empty_env), 'SJFX_STATE_DIR': str(tmp_path),
                       'SJFX_PROTECTION_TEST_STATIC_ONLY': '1', 'SJFX_PROTECTION_CLAM': '',
                       'SJFX_PROTECTION_CLAMD_SOCKET': '', 'HF_HUB_OFFLINE': '1',
                       'TRANSFORMERS_OFFLINE': '1'}.items():
        monkeypatch.setenv(key, value)
    from config import Config
    from services.storage import Storage
    from sjfx_protection.registration import PackageProtection
    from sjfx_protection.store import Store
    root = tmp_path / 'input'
    root.mkdir()
    (root / 'evidence.txt').write_text('isolated source evidence', encoding='utf-8')
    for name, value in {'DATA_DIR': tmp_path, 'DB_PATH': tmp_path / 'agent.db',
                        'OUTPUT_DIR': tmp_path / 'outputs', 'SCAN_ALLOWED_ROOTS': (root,)}.items():
        monkeypatch.setattr(Config, name, value)
    original = business._configured
    business.configure(SimpleNamespace(enabled=False))
    storage = Storage(Config.DB_PATH)
    storage.save_scan({'root': str(root), 'tree': {}, 'files': []}, scan_id='scan', owner_id='alice')
    with sqlite3.connect(Config.DB_PATH) as db:
        db.execute('CREATE TABLE users(user_id TEXT,owner_id TEXT,status TEXT,must_change_pw INTEGER,role TEXT)')
        db.execute("INSERT INTO users VALUES('u1','alice','active',0,'user')")
    store = Store(tmp_path / 'file_protection')
    with store.db(True) as db:
        db.execute('INSERT INTO settings VALUES(?,?)', ('package-root-grants:alice', json.dumps([str(root)])))
    package = PackageProtection(store, [str(root)], authorize_owner=lambda owner, user: owner == 'alice',
                                authorize_root=lambda owner, path: owner == 'alice')
    package.update_policy('admin', True, {'enabled': True,
                          'inspection_profile': {'required_engines': ['static']}}, 'isolated static TEST profile')
    intake = package.begin_local('alice', 'u1', str(root))
    assert package.process_job(intake['job_id'])['state'] == 'completed'
    frozen = package.complete_local('alice', intake)
    identifier = storage.create_job('scan', {'protection_context': frozen},
                                     task_type='scan_and_analyze', owner_id='alice')
    job = storage.claim_next_job('isolated-parent')
    assert job['id'] == identifier
    business.configure(SimpleNamespace(enabled=True, package=package, store=store))
    try:
        yield storage, job, package, store
    finally:
        business.configure(original)


def run_parent_publication(storage, job, execute):
    class StopLoop(BaseException):
        pass
    source = Path(__file__).resolve().parents[1] / 'worker.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    names = {'run_forever', 'JobCancelled', 'JobExecutionTimeout', 'RemoteJobError', '_finalize_job_result'}
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    proxy = Mock(wraps=storage, sqlite_checkpoint_interval=60)
    for method in ('initialize', 'recover_orphaned_jobs_after_lock', 'reconcile_conversation_turn_jobs',
                   'wake_due_file_retries', 'checkpoint_wal'):
        setattr(proxy, method, Mock(return_value=0))
    proxy.claim_next_job.side_effect = [job, StopLoop()]
    namespace = {'storage': proxy, 'logger': Mock(), 'time': time,
                 'Config': SimpleNamespace(WORKER_STALE_SECONDS=300, HISTORY_RETENTION_DAYS=0,
                                           HISTORY_MAX_SCANS=0, MAX_JOB_RESUME_ATTEMPTS=3),
                 'WORKER_ID': 'isolated-parent', 'ensure_runtime_directories': lambda: None,
                 '_configure_worker_logging': lambda: None, '_acquire_worker_lock': Mock(),
                 '_resume_strict_imports': lambda: 0, '_resume_preliminary_imports': lambda: 0,
                 '_background_resource_state': lambda _job: (True, []),
                 'execute_with_preliminary_cache': execute,
                 '_is_transient_database_lock': lambda _exc: False,
                 '_finish_conversation_turn_after_worker_failure': Mock()}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    with pytest.raises(StopLoop):
        namespace['run_forever']()
    assert business.context() is None
    return proxy


@pytest.mark.parametrize('content_result', [False, True])
def test_real_parent_publication_restores_frozen_context_after_execution(real_publication, content_result):
    storage, job, package, store = real_publication
    selection = {'scan_id': 'scan', 'scan_available': True, '_await_selection': True}
    final_result = {'scan_id': 'scan', 'summary': 'approved derived result'} if content_result else {'scan_id': 'scan', '_await_selection': True}
    def execute(job, _cache):
        with business.job_execution(job, record_result=False):
            storage.update_job(job['id'], result=selection)
        assert business.context() is None
        return final_result
    run_parent_publication(storage, job, execute)
    with sqlite3.connect(storage.db_path) as db:
        row = db.execute('SELECT status,result,error FROM analysis_jobs WHERE id=?', (job['id'],)).fetchone()
    assert row[0] == 'completed', row
    assert json.loads(row[1]) == final_result
    assert row[2] is None
    # Genuine cached content is stamped against the same immutable source set.
    with business.activate(job['options']['protection_context'], task_id=job['id']):
        with storage._connect() as db:
            assert json.loads(db.execute('SELECT result FROM analysis_jobs WHERE id=?', (job['id'],)).fetchone()[0]) == final_result
    assert store.verify_chain()['ok']
    receipts = [json.loads(row['payload'])['payload'] for row in store.list_rows('events', 'alice')
                if row['action'] == 'operation.analyze' and json.loads(row['payload'])['payload'].get('phase') == 'publication']
    assert len(receipts) == 1
    assert receipts[0]['result'] == 'completed' and receipts[0]['task_id'] == job['id']


def test_real_parent_publication_rejects_changed_policy_without_refreshing_context(real_publication):
    storage, job, package, store = real_publication
    frozen_json = json.dumps(job['options']['protection_context'], sort_keys=True)
    def execute(job, _cache):
        with business.job_execution(job, record_result=False):
            storage.update_job(job['id'], result={'scan_id': 'scan', '_await_selection': True})
        package.update_policy('admin', True, {'enabled': True}, 'simulate policy change before publication')
        return {'scan_id': 'scan', 'summary': 'must not be published'}
    run_parent_publication(storage, job, execute)
    with sqlite3.connect(storage.db_path) as db:
        row = db.execute('SELECT status,result,error FROM analysis_jobs WHERE id=?', (job['id'],)).fetchone()
    assert row[0] == 'failed'
    assert 'must not be published' not in row[1]
    assert json.dumps(job['options']['protection_context'], sort_keys=True) == frozen_json
    receipts = [json.loads(row['payload'])['payload'] for row in store.list_rows('events', 'alice')
                if row['action'] == 'operation.analyze' and json.loads(row['payload'])['payload'].get('phase') == 'publication']
    assert [receipt['result'] for receipt in receipts] == ['failed']


def test_real_preliminary_publication_records_actual_already_committed_result(real_publication):
    storage, job, package, store = real_publication
    final_result = {'scan_id': 'scan', 'summary': 'already committed approved result'}
    def execute(job, _cache):
        with business.job_execution(job, record_result=False):
            storage.finalize_job(job['id'], result=final_result)
        return {**final_result, '_preliminary_committed': True}
    proxy = run_parent_publication(storage, job, execute)
    proxy.finalize_job.assert_not_called()
    with sqlite3.connect(storage.db_path) as db:
        row = db.execute('SELECT status,result FROM analysis_jobs WHERE id=?', (job['id'],)).fetchone()
    assert row[0] == 'completed' and json.loads(row[1]) == final_result
