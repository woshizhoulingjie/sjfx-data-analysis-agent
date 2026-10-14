import asyncio
import base64
import io
import json
import time
import zipfile
from email.message import EmailMessage
from unittest.mock import patch

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI

from sjfx_protection.integration import current, install
from sjfx_protection.scanner import inspect_file, safe_mail_preview
from sjfx_protection.store import ProtectionError, Store, uid
from sjfx_protection.threat_rules import inspect_payload
from sjfx_protection.validation import local_path, timestamp
from sjfx_protection.worker import inventory, verify_inventory


@pytest.fixture
def scenario(tmp_path):
    store = Store(tmp_path / 'state')
    key = Ed25519PrivateKey.generate()
    pub = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    device = {'id': store.enroll(store.enrollment('alice', 'user'), pub, 'endpoint'), 'owner': 'alice'}
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'source.txt').write_text('original', encoding='utf-8')
    collection = store.create_collection('alice', str(root))
    with patch('sjfx_protection.worker.inspect_file', return_value={'risk': 'no_detection', 'findings': [], 'engines': {'test': 'completed'}}):
        inventory(store, collection, [str(root)])
    file = store.list_rows('files', 'alice')[0]
    record = {'file_id': file['id'], 'sha256': file['sha256'], 'local_path': 'D:/docs/source.txt', 'local_identity': 'vol:123'}
    binding = store.bind(device, [record])[0]['binding_id']
    return store, device, root, collection, file, record, binding


def event(binding, action='file.modified', **extra):
    return {'event_id': uid(), 'binding_id': binding, 'action': action, 'occurred': time.time(), **extra}


def binding_row(store, bid):
    return next(b for b in store.list_rows('bindings', 'alice') if b['id'] == bid)


def enable_auto_quarantine(store):
    """矩阵 A05：自动处置必须由管理员策略显式启用，观察模式默认不排队。"""
    from sjfx_protection.policy import write_policy
    write_policy(store.directory, 'admin', True,
                 {'enabled': True,
                  'operation_policy': {'observation_only': False, 'automatic_quarantine': True}},
                 '测试：启用自动隔离')


def quarantine_receipt(store, bid, aid):
    """矩阵 E07/E09：完成回执必须携带与冻结目标一致的真实实例。"""
    row = binding_row(store, bid)
    parent, _, name = row['local_path'].replace('\\', '/').rpartition('/')
    return {'action_id': aid, 'local_path': parent + '/.sjfx-quarantine/' + name,
            'local_identity': row['local_identity'], 'sha256': row['sha256']}


@pytest.mark.parametrize('path', ['relative.txt', 'C:relative', '../secret', '/a/../b', 'C:/x/../b', 'C:/file:stream', '//./C:/file', '//?/C:/file', '/tmp/bad' + chr(0), ''])
def test_invalid_local_paths(path):
    with pytest.raises(ProtectionError):
        local_path(path)


@pytest.mark.parametrize('path', ['D:/docs/report.txt', '/home/user/report.txt', '//server/share/report.txt'])
def test_absolute_local_paths(path):
    assert local_path(path)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1, 'invalid', None, 1_700_000_600, 1_700_000_300.001])
def test_bad_event_clock(value):
    # Collection-to-execution delays must not move an invalid future event
    # inside the permitted clock-skew window during a long regression run.
    with pytest.raises(ProtectionError):
        timestamp(value, 1_700_000_000)


@pytest.mark.parametrize('offset', [-0.001, 0, 300])
def test_event_clock_permitted_boundary(offset):
    now = 1_700_000_000
    assert timestamp(now + offset, now) == now + offset


def test_modified_without_fingerprint_invalidates_gate(scenario):
    store, device, root, _, _, _, bid = scenario
    store.ingest(device, [event(bid)])
    assert binding_row(store, bid)['state'] == 'unverified'
    with pytest.raises(ProtectionError):
        store.require_root('alice', device['id'], str(root))


def test_two_mutations_same_batch_and_ledger(scenario):
    store, device, _, _, _, _, bid = scenario
    now = time.time()
    store.ingest(device, [event(bid, sha256='b'*64, identity='v:2', occurred=now-2), event(bid, sha256='c'*64, identity='v:3', occurred=now-1)])
    assert binding_row(store, bid)['sha256'] == 'c'*64
    assert binding_row(store, bid)['state'] == 'changed'
    assert store.verify_chain()['ok']


def test_older_event_cannot_rewind_binding(scenario):
    store, device, _, _, _, _, bid = scenario
    now = time.time()
    latest = event(bid, sha256='b'*64, identity='v:2', occurred=now-1)
    old = event(bid, sha256='c'*64, identity='v:3', occurred=now-10)
    store.ingest(device, [latest, old, latest])
    assert binding_row(store, bid)['sha256'] == 'b'*64
    rows = [r for r in store.list_rows('events', 'alice') if r['action'] == 'file.modified']
    assert len(rows) == 2
    assert any(json.loads(r['payload'])['payload'].get('out_of_order') for r in rows)


def test_dangerous_identical_copy_is_queued(scenario):
    store, device, _, _, file, record, bid = scenario
    enable_auto_quarantine(store)
    with store.db(True) as db:
        db.execute("UPDATE files SET risk='dangerous' WHERE id=?", (file['id'],))
    store.bind(device, [record])
    data = {'source_binding': bid, 'sha256': file['sha256'], 'local_path': 'E:/copy.txt', 'local_identity': 'v:copy'}
    copied = store.related_copy(device, data)
    store.related_copy(device, data)
    queued = store.pending_actions(device['id'])
    assert len(queued) == 2
    assert {r['binding'] for r in queued} == {bid, copied['binding_id']}
    assert all(r['kind'] == 'quarantine' and r['expected_sha256'] == file['sha256'] for r in queued)


def test_changed_file_invalidates_pending_action(scenario):
    store, device, _, _, _, _, bid = scenario
    aid = store.command('alice', bid, 'quarantine', 'test')
    store.ingest(device, [event(bid, sha256='a'*64, identity='v:new')])
    store.action_result(device, aid, {'status': 'completed'})
    assert next(a for a in store.list_rows('actions', 'alice') if a['id'] == aid)['state'] == 'stale'
    assert binding_row(store, bid)['state'] == 'changed'


def test_known_dangerous_file_cannot_be_restored(scenario):
    store, device, _, _, file, _, bid = scenario
    aid = store.command('alice', bid, 'quarantine', 'test')
    # 矩阵 E07：完成回执必须带真实实例；状态按动作分类，不再是统一 protected。
    store.action_result(device, aid,
                        {'status': 'completed', 'actual': quarantine_receipt(store, bid, aid)})
    assert binding_row(store, bid)['state'] == 'quarantined'
    with store.db(True) as db:
        db.execute("UPDATE files SET risk='dangerous' WHERE id=?", (file['id'],))
    with pytest.raises(ProtectionError, match='危险'):
        store.command('alice', bid, 'restore', 'undo', aid)


def test_safe_action_restore_and_idempotence(scenario):
    store, device, _, _, _, _, bid = scenario
    aid = store.command('alice', bid, 'deny_read', 'test')
    assert aid == store.command('alice', bid, 'deny_read', 'test repeated')
    store.action_result(device, aid, {'status': 'completed'})
    # 矩阵 E07：读锁按动作分类记录。
    assert binding_row(store, bid)['state'] == 'read_locked'
    rid = store.command('alice', bid, 'restore', 'undo', aid)
    row = binding_row(store, bid)
    store.action_result(device, rid, {'status': 'completed', 'actual': {
        'action_id': rid, 'local_path': row['local_path'],
        'local_identity': row['local_identity'], 'sha256': row['sha256']}})
    # 矩阵 E09：恢复后必须重新核验，不能直接当作 watching。
    assert binding_row(store, bid)['state'] == 'unverified'
    assert store.verify_chain()['ok']


def test_monitor_gap_invalidates_bindings_and_actions(scenario):
    store, device, root, _, _, record, bid = scenario
    store.command('alice', bid, 'protect_delete', 'test')
    store.ingest(device, [event('', 'monitor.gap', detail='queue lost')])
    assert binding_row(store, bid)['state'] == 'unverified'
    assert store.pending_actions(device['id']) == []
    with pytest.raises(ProtectionError):
        store.require_root('alice', device['id'], str(root))
    store.bind(device, [record])
    assert store.require_root('alice', device['id'], str(root))


def test_cross_owner_actions_and_trace_denied(scenario):
    store, _, _, _, file, _, bid = scenario
    with pytest.raises(ProtectionError):
        store.command('bob', bid, 'quarantine', 'test')
    with pytest.raises(ProtectionError):
        store.file_trace('bob', file['id'])


def test_file_trace_contains_both_paths_and_freshness(scenario):
    store, device, _, _, file, _, bid = scenario
    store.heartbeat(device['id'], {'healthy': True})
    trace = store.file_trace('alice', file['id'])
    assert trace['file']['path'] == file['path']
    assert trace['instances'][0]['id'] == bid
    assert trace['instances'][0]['verified'] is True
    assert trace['instances'][0]['monitor_healthy'] is True
    assert '不保证' in trace['coverage']


def test_path_collision_is_audited_not_server_error(scenario):
    store, device, _, _, file, _, bid = scenario
    store.related_copy(device, {'source_binding': bid, 'sha256': file['sha256'], 'local_path': 'E:/copy.txt', 'local_identity': 'v:copy'})
    store.ingest(device, [event(bid, 'file.renamed', path='E:/copy.txt')])
    assert binding_row(store, bid)['state'] == 'unverified'
    assert any(r['action'] == 'monitor.path_conflict' for r in store.list_rows('events', 'alice'))


@pytest.mark.parametrize('change,expected', [('modify', 'server.file.modified'), ('delete', 'server.file.deleted'), ('rename', 'server.file.renamed'), ('create', 'server.file.created')])
def test_server_changes_are_file_level_audited(scenario, change, expected):
    store, _, root, cid, file, _, _ = scenario
    original = root / 'source.txt'
    if change == 'modify':
        original.write_text('changed', encoding='utf-8')
    elif change == 'delete':
        original.unlink()
    elif change == 'rename':
        original.rename(root / 'renamed.txt')
    else:
        (root / 'new.txt').write_text('new', encoding='utf-8')
    verify_inventory(store, cid)
    assert store.collection('alice', cid)['state'] == 'failed'
    rows = [r for r in store.list_rows('events', 'alice') if r['action'] == expected]
    assert len(rows) == 1
    if change != 'create':
        assert rows[0]['file_id'] == file['id']
    assert store.verify_chain()['ok']


def test_no_change_does_not_duplicate_events(scenario):
    store, _, _, cid, _, _, _ = scenario
    count = store.verify_chain()['count']
    verify_inventory(store, cid)
    assert store.verify_chain()['count'] == count
    assert store.collection('alice', cid)['state'] == 'ready'


def zip_bytes(entries):
    target = io.BytesIO()
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return target.getvalue()


def rules(raw, name, **kw):
    return {f['rule'] for f in inspect_payload(raw, name, **kw)}


def test_credential_harvesting_html_passively_detected(tmp_path):
    path = tmp_path / 'invoice.html'
    path.write_text('<form action="https://example.invalid/collect"><input type=password></form>')
    result = inspect_file(path, clam='missing-test-scanner')
    assert result['risk'] == 'dangerous'
    assert 'credential_form' in {f['rule'] for f in result['findings']}


def test_html_mail_attachment_checked(tmp_path):
    message = EmailMessage()
    message['Subject'] = 'invoice'
    message.set_content('see attached')
    message.add_attachment(b'<form><input type=password></form>', maintype='text', subtype='html', filename='invoice.html')
    path = tmp_path / 'mail.eml'
    path.write_bytes(message.as_bytes())
    assert inspect_file(path, clam='missing-test-scanner')['risk'] == 'dangerous'


def test_archive_traversal_and_script_detected_without_extracting():
    raw = zip_bytes([('../invoice.pdf.exe', b'not executed')])
    assert {'archive_path_escape', 'double_extension', 'executable_content'} <= rules(raw, 'mail.zip')


def test_archive_limits_nested_macro_and_invalid_zip():
    assert 'archive_expansion_limit' in rules(zip_bytes([('big.txt', b'a'*100000)]), 'a.zip')
    assert 'embedded_macro' in rules(zip_bytes([('word/vbaProject.bin', b'content')]), 'a.docx')
    assert 'unreadable_archive' in rules(b'not a zip', 'a.zip')
    inner = zip_bytes([('x.txt', b'x')])
    for _ in range(4):
        inner = zip_bytes([('nested.zip', inner)])
    assert 'archive_depth_limit' in rules(inner, 'outer.zip')


def test_magic_detection_avoids_mz_text_false_positive():
    assert 'disguised_executable' not in rules(b'MZ is a prefix in normal text', 'note.txt')
    raw = bytearray(100)
    raw[:2] = b'MZ'
    raw[60:64] = (64).to_bytes(4, 'little')
    raw[64:68] = bytes([80, 69, 0, 0])
    assert 'disguised_executable' in rules(bytes(raw), 'invoice.pdf')


def test_unknown_charset_preview_and_scan_fail_safely(tmp_path):
    path = tmp_path / 'mail.eml'
    path.write_bytes(bytes([10]).join([b'Subject: test', b'Content-Type: text/plain; charset=no-such-codec', b'', b'hello']))
    assert 'hello' in safe_mail_preview(path)['text']
    # 矩阵 C05/C01：必要引擎缺失记 incomplete，不能当作普通未检查状态。
    assert inspect_file(path, clam='missing-test-scanner')['risk'] == 'incomplete'


def test_api_trace_csrf_and_block_audit(tmp_path):
    async def run():
        app = FastAPI()
        p = install(app, state_dir=tmp_path/'api-state', allowed_roots=[tmp_path], mode='enforce', session_resolver=lambda _: {'owner_id': 'alice', 'user_id': 'u1', 'role': 'admin'})
        @app.get('/api/protected-file')
        def business():
            return {'must_not_run': True}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', cookies={'sjfx_session': 'valid'}) as client:
            # 矩阵 A02/A06/D01：关闭保护时原业务直通；全局按路由拦截已由按数据包准入取代。
            assert (await client.get('/api/protected-file')).status_code == 200
            assert not any(e['action'] == 'system.blocked' for e in p.store.list_rows('events', 'alice'))
            assert (await client.get('/protection/api/trace?file_id=unknown')).status_code == 404
            response = await client.post('/protection/api/actions', json={}, headers={'X-SJFX-Protection': '1', 'Origin': 'https://evil.invalid'})
            assert response.status_code == 403
    asyncio.run(run())


def test_audit_guard_schedules_verification_without_blocking(scenario):
    store, _, root, cid, _, _, _ = scenario
    app = FastAPI()
    p = install(app, state_dir=store.directory, allowed_roots=[root], mode='audit', session_resolver=lambda _: None)
    with store.db(True) as db:
        db.execute('DELETE FROM settings WHERE key=?', ('verified:'+cid,))
    token = current.set({'owner': 'alice', 'device': '', 'enforced': False})
    try:
        with patch('sjfx_protection.integration.subprocess.Popen') as launch:
            # 矩阵 A02/E04：总开关关闭时不启动核验进程，也不阻塞原业务。
            p.guard_root(str(root))
            launch.assert_not_called()
            # 开启后核验异步进行（不等结果即返回），矩阵 E04/E05。
            enable_auto_quarantine(store)
            p.schedule_verification(cid)
            launch.assert_called_once()
            assert '--verify' in launch.call_args[0][0]
    finally:
        current.reset(token)


@pytest.mark.parametrize('path', ['C:/NUL', 'C:/AUX.txt', 'C:/folder./file', 'C:/folder /file'])
def test_windows_device_aliases_denied(path):
    with pytest.raises(ProtectionError):
        local_path(path)


def test_gap_cannot_be_cleared_by_protected_event(scenario):
    store, device, root, _, _, _, bid = scenario
    store.ingest(device, [event('', 'monitor.gap'), event(bid, 'file.protected')])
    assert binding_row(store, bid)['state'] == 'unverified'
    with pytest.raises(ProtectionError):
        store.require_root('alice', device['id'], str(root))


def test_newer_failed_collection_cannot_fall_back_to_old_ready(scenario):
    store, device, root, _, _, _, _ = scenario
    latest = store.create_collection('alice', str(root))
    with store.db(True) as db:
        db.execute("UPDATE collections SET state='failed' WHERE id=?", (latest,))
    with pytest.raises(ProtectionError):
        store.require_root('alice', device['id'], str(root))


def test_stale_server_manifest_not_displayed_as_verified(scenario):
    store, _, root, cid, file, _, _ = scenario
    (root/'source.txt').write_text('different', encoding='utf-8')
    verify_inventory(store, cid)
    assert store.file_trace('alice', file['id'])['instances'][0]['verified'] is False


def test_health_string_is_not_true(scenario):
    store, device, _, _, _, _, _ = scenario
    with pytest.raises(ProtectionError):
        store.heartbeat(device['id'], {'healthy': 'false'})


def test_file_reappearance_requeues_dangerous_quarantine(scenario):
    store, device, _, _, file, record, bid = scenario
    enable_auto_quarantine(store)
    with store.db(True) as db:
        db.execute("UPDATE files SET risk='dangerous' WHERE id=?", (file['id'],))
    store.bind(device, [record])
    aid = store.pending_actions(device['id'])[0]['id']
    store.action_result(device, aid,
                        {'status': 'completed', 'actual': quarantine_receipt(store, bid, aid)})
    store.ingest(device, [event(bid, 'file.reappeared', sha256=file['sha256'], identity='v:new')])
    assert len(store.pending_actions(device['id'])) == 1
    assert store.pending_actions(device['id'])[0]['id'] != aid


def test_background_observer_detects_changes_without_requests(scenario):
    from sjfx_protection.watch import observe_once
    store, _, root, cid, _, _, _ = scenario
    with store.db(True) as db:
        db.execute('DELETE FROM settings WHERE key=?', ('verified:'+cid,))
    (root/'source.txt').write_text('different', encoding='utf-8')
    # 矩阵 E04/A02：关闭保护时不启动服务器观察。
    assert observe_once(store, interval=30) == 0
    enable_auto_quarantine(store)
    assert observe_once(store, interval=30) == 1
    assert store.collection('alice', cid)['state'] == 'failed'
    assert observe_once(store, interval=30) == 0


def test_distinct_file_reads_not_collapsed_and_status_capability(tmp_path):
    async def run():
        app = FastAPI()
        p = install(app, state_dir=tmp_path/'audit-state', allowed_roots=[tmp_path], mode='audit', session_resolver=lambda _: {'owner_id': 'alice', 'user_id': 'u1', 'role': 'admin'})
        @app.get('/api/items')
        def business():
            return {'ok': True}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', cookies={'sjfx_session': 'valid'}) as client:
            # 矩阵 A02：关闭保护时原业务直通，且不写入保护事件、不初始化保护库。
            assert (await client.get('/api/items?path=a')).status_code == 200
            assert (await client.get('/api/items?path=b')).status_code == 200
            assert [e for e in p.store.list_rows('events', 'alice') if e['action'] == 'system.request'] == []
            status = (await client.get('/protection/api/status')).json()
            # 矩阵 E14：状态暴露真实开关与能力（管理员视角）。
            assert status['protection_version'] == '4.0-local-candidate'
            assert status['access_version'] == 1
            assert status['enabled'] is False
            assert status['capabilities']['file_trace'] is True
    asyncio.run(run())


def test_nonregular_files_fail_without_waiting(tmp_path):
    import os
    from sjfx_protection.scanner import bounded_read, stable_hash
    if not hasattr(os, 'mkfifo'):
        pytest.skip('POSIX FIFO test')
    fifo = tmp_path/'pipe'
    os.mkfifo(fifo)
    with pytest.raises(ValueError):
        stable_hash(fifo)
    with pytest.raises(ValueError):
        bounded_read(fifo)


def test_protection_event_does_not_fake_completed_receipt(scenario):
    store, device, _, _, _, _, bid = scenario
    aid = store.command('alice', bid, 'quarantine', 'test')
    store.ingest(device, [event(bid, 'file.protected')])
    assert binding_row(store, bid)['state'] == 'watching'
    assert store.pending_actions(device['id'])[0]['id'] == aid
    # 矩阵 E07：只有带真实实例的 action_result 才算成功，观察事件不能伪造完成。
    store.action_result(device, aid,
                        {'status': 'completed', 'actual': quarantine_receipt(store, bid, aid)})
    assert binding_row(store, bid)['state'] == 'quarantined'
