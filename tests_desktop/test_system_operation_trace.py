"""File history and real job attribution using isolated local receipts and IPC."""
import concurrent.futures
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from sjfx_protection import business
from sjfx_protection.registration import PackageProtection
from sjfx_protection.store import Store


def payload(row):
    return json.loads(row['payload'])['payload']


@pytest.mark.parametrize('field', ['targets', 'context'])
def test_file_history_includes_explicit_historical_operation_targets(tmp_path, field):
    store = Store(tmp_path / 'state')
    target = {'file_id': 'original-file', 'version_id': 'historical-version', 'sha256': 'a' * 64}
    details = {'registration_id': 'package', field: [target] if field == 'targets' else {'versions': [target]}}
    store.event('alice', 'operation.analyze', payload=details)
    rows = store.list_rows('events', 'alice', file_id='original-file')
    assert len(rows) == 1
    assert payload(rows[0])[field] == details[field]
    # A shared hash or a different logical identity does not establish history.
    assert store.list_rows('events', 'alice', file_id='different-file') == []
    assert store.list_rows('events', 'bob', file_id='original-file') == []
    assert store.verify_chain()['ok']


def test_file_history_deduplicates_targets_before_paginating(tmp_path):
    store = Store(tmp_path / 'state')
    target = {'file_id': 'original-file', 'version_id': 'old-version'}
    for i in range(3):
        store.event('alice', 'operation.analyze', payload={
            'task_id': str(i), 'targets': [target, target], 'context': {'versions': [target]}})
    rows = store.list_rows('events', 'alice', file_id='original-file', limit=1, offset=1)
    assert [payload(row)['task_id'] for row in rows] == ['1']


@pytest.fixture
def checked_job(tmp_path, monkeypatch):
    empty_env = tmp_path / 'empty.env'
    empty_env.write_text('', encoding='utf-8')
    monkeypatch.setenv('SJFX_ENV_FILE', str(empty_env))
    monkeypatch.setenv('SJFX_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('SJFX_PROTECTION_TEST_STATIC_ONLY', '1')
    monkeypatch.setenv('SJFX_PROTECTION_CLAM', '')
    monkeypatch.setenv('SJFX_PROTECTION_CLAMD_SOCKET', '')
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    monkeypatch.setenv('TRANSFORMERS_OFFLINE', '1')
    root = tmp_path / '资料'
    root.mkdir()
    file = root / 'evidence.txt'
    file.write_text('ordinary local evidence', encoding='utf-8')
    monkeypatch.setenv('SCAN_ALLOWED_ROOTS', str(root))
    from config import Config
    monkeypatch.setattr(Config, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(Config, 'DB_PATH', tmp_path / 'agent.db')
    monkeypatch.setattr(Config, 'OUTPUT_DIR', tmp_path / 'outputs')
    with sqlite3.connect(Config.DB_PATH) as db:
        db.execute('CREATE TABLE users(user_id TEXT,owner_id TEXT,status TEXT,must_change_pw INTEGER,role TEXT)')
        db.execute("INSERT INTO users VALUES('u1','alice','active',0,'user')")
        db.execute('CREATE TABLE scans(id TEXT,owner_id TEXT,root_path TEXT)')
        db.execute('INSERT INTO scans VALUES(?,?,?)', ('scan', 'alice', str(root)))
    store = Store(tmp_path / 'file_protection')
    with store.db(True) as db:
        db.execute('INSERT INTO settings VALUES(?,?)', ('package-root-grants:alice', json.dumps([str(root)])))
    package = PackageProtection(store, [str(root)],
        authorize_owner=lambda owner, user: owner == 'alice',
        authorize_root=lambda owner, path: owner == 'alice')
    package.update_policy('admin', True, {'enabled': True,
        'inspection_profile': {'required_engines': ['static']}}, 'isolated static TEST profile')
    receipt = package.begin_local('alice', 'u1', str(root))
    assert package.process_job(receipt['job_id'])['state'] == 'completed'
    frozen = package.complete_local('alice', receipt)
    business.configure(SimpleNamespace(enabled=True, package=package, store=store))
    job = {'id': 'real-job', 'owner_id': 'alice', 'scan_id': 'scan',
           'task_type': 'parse', 'options': {'protection_context': frozen}}
    yield store, package, file, frozen, job
    business.configure(None)


def file_events(store):
    return [row for row in store.list_rows('events', 'alice') if row['action'].startswith('file.parse.')]


@pytest.mark.parametrize('fail', [False, True])
def test_job_file_event_records_real_task_and_worker_result(checked_job, fail):
    store, package, file, frozen, job = checked_job
    try:
        with business.job_execution(job):
            with business.file_execution(file) as approved:
                assert approved.read_text(encoding='utf-8') == 'ordinary local evidence'
                if fail:
                    raise RuntimeError('business parser failed')
    except RuntimeError:
        assert fail
    row = file_events(store)[0]
    assert payload(row).get('task_id') == 'real-job'
    assert row['action'] == ('file.parse.failed' if fail else 'file.parse.completed')
    worker = [payload(row) for row in store.list_rows('events', 'alice')
              if row['action'] == 'operation.analyze' and payload(row).get('source') == 'local_business']
    assert worker[-1]['task_id'] == 'real-job'
    assert worker[-1]['result'] == ('failed' if fail else 'completed')
    assert business.context() is None
    assert store.verify_chain()['ok']


def test_nested_jobs_and_copied_thread_restore_task_identity(checked_job):
    from contextvars import copy_context
    store, package, file, frozen, job = checked_job
    inner = {**job, 'id': 'nested-job'}
    with business.job_execution(job):
        with business.job_execution(inner), business.file_execution(file):
            pass
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            def parse_in_thread():
                with business.file_execution(file):
                    pass
            pool.submit(copy_context().run, parse_in_thread).result()
    with business.activate(frozen), business.file_execution(file):
        pass
    assert [payload(row).get('task_id') for row in reversed(file_events(store))] == [
        'nested-job', 'real-job', '']


def test_reused_real_parser_process_keeps_each_job_identity(checked_job, tmp_path):
    from services.parse_isolation import IsolatedParserRunner
    from services.unified_parser import UnifiedDocumentParser
    store, package, file, frozen, job = checked_job
    runner = IsolatedParserRunner(UnifiedDocumentParser(artifacts_path=tmp_path / 'parser-artifacts'))
    try:
        for identifier in ('first-real-job', 'second-real-job'):
            with business.job_execution({**job, 'id': identifier}):
                document = runner.parse(file, file.name, mode='fast', timeout=30)
                assert 'ordinary local evidence' in document['text']
        with business.activate(frozen):
            runner.parse(file, file.name, mode='fast', timeout=30)
    finally:
        runner.close()
    assert [payload(row).get('task_id') for row in reversed(file_events(store))] == [
        'first-real-job', 'second-real-job', '']


def test_real_qa_process_receives_job_identity(checked_job):
    from services.large_qa_extract import extract_document
    store, package, file, frozen, job = checked_job
    info = file.stat()
    with business.job_execution(job):
        records, complete, note = extract_document(file.parent,
            {'path': file.name, 'size': info.st_size, 'mtime': info.st_mtime_ns}, timeout=30)
        assert complete, note
        assert 'ordinary local evidence' in ' '.join(record['text'] for record in records)
    assert payload(file_events(store)[0]).get('task_id') == 'real-job'


def test_preview_server_resets_task_between_requests(checked_job, monkeypatch):
    from services.large_package_preview_runner import _serve
    store, package, file, frozen, job = checked_job
    requests = iter([{'root': str(file.parent), 'item': {'path': file.name},
                      'protection_context': frozen, 'task_id': identifier}
                     for identifier in ('preview-task', '')])
    class Connection:
        def recv(self):
            try:
                return next(requests)
            except StopIteration:
                raise EOFError
        def send(self, value):
            assert 'result' in value, value
        def close(self):
            pass
    def preview_file(root, item):
        with business.file_execution(Path(root) / item['path']):
            return {'summary': 'local fixture'}, ''
    monkeypatch.setattr('services.large_package_preview.preview_file', preview_file)
    _serve(Connection())
    assert [payload(row).get('task_id') for row in reversed(file_events(store))] == ['preview-task', '']


def test_http_receipt_preserves_source_and_does_not_invent_business_task(checked_job):
    store, package, file, frozen, job = checked_job
    package.record_operation('alice', frozen['registration_id'], 'analyze', 'completed',
        context=frozen, source='local_http', trusted=True)
    row = next(row for row in store.list_rows('events', 'alice') if row['action'] == 'operation.analyze')
    assert payload(row)['source'] == 'local_http'
    assert payload(row)['task_id'] == ''
    assert not file_events(store)


@pytest.mark.parametrize('fail', [False, True])
def test_deferred_worker_terminal_receipts_keep_file_events_and_restore_nested_scope(checked_job, fail):
    store, package, file, frozen, job = checked_job
    try:
        with business.job_execution(job, record_result=False):
            with business.job_execution(job), business.file_execution(file):
                if fail:
                    raise RuntimeError('executor failed before publication')
    except RuntimeError:
        assert fail
    assert payload(file_events(store)[0])['task_id'] == job['id']
    assert not [row for row in store.list_rows('events', 'alice')
                if payload(row).get('source') == 'local_business']
    assert business.current_task_id() == ''
    assert business.result_receipts_enabled.get() is True
    business.record_execution_result(frozen, 'analyze', 'failed' if fail else 'completed',
                                     job['id'], phase='publication')
    receipts = [payload(row) for row in store.list_rows('events', 'alice')
                if payload(row).get('source') == 'local_business']
    assert len(receipts) == 1
    assert receipts[0]['phase'] == 'publication'
    assert receipts[0]['targets'] == frozen['versions']
    assert receipts[0]['task_id'] == job['id']
    assert receipts[0]['result'] == ('failed' if fail else 'completed')
    # Suppression resets even after failure; ordinary callers retain behavior.
    with business.job_execution(job):
        pass
    assert any(payload(row).get('phase') == 'execution' for row in store.list_rows('events', 'alice'))
