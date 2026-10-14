"""Owner boundaries exercised with synthetic sources, repositories and workers."""
import hashlib
import json
import mailbox
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import zipfile
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from config import Config
from large_package_worker import LargePackageWorker
from services.large_package_isolated import LargePackageStore, _under_allowed_root, iter_inventory
from services.mail.api import register_mail_routes
from services.mail.attachments import read_message_raw
from services.mail.bootstrap import build_mail_services, worker_mail_services
from services.mail.jobs import _import
from services.mail.models import MailMember
from services.mail.policy import MailPolicy, MailPolicyError
from services.mail.source import DirectoryAdapter
from services.storage import Storage
from web_compat import SJFXFastAPI


def synthetic_mail():
    message = EmailMessage()
    message['From'] = 'synthetic-a@example.invalid'
    message['To'] = 'synthetic-b@example.invalid'
    message['Subject'] = 'Owner boundary test'
    message['Message-ID'] = '<synthetic-owner-boundary@example.invalid>'
    message.set_content('Synthetic content only, no business data.')
    return message.as_bytes()


def directory_link(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != 'nt':
            pytest.skip('native symlink unavailable')
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                                capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr.decode(errors='replace')


@pytest.fixture
def source_space(tmp_path, monkeypatch):
    outputs = tmp_path / 'outputs'
    upload_root = outputs / '_uploads'
    own = upload_root / 'assembled' / 'owner_a' / 'session_a'
    other = upload_root / 'assembled' / 'owner_b' / 'session_b'
    shared = tmp_path / 'shared'
    for path in (own, other, shared):
        path.mkdir(parents=True)
    monkeypatch.setattr(Config, 'OUTPUT_DIR', outputs)
    monkeypatch.setattr(Config, 'DATA_DIR', tmp_path / 'state')
    monkeypatch.setattr(Config, 'SCAN_ALLOWED_ROOTS', (shared,))
    monkeypatch.setattr(Config, 'MAIL_FEATURE_ENABLED', True)
    monkeypatch.setattr(Config, 'MAIL_ENABLE_VECTOR_INDEX', False)
    monkeypatch.setattr(Config, 'MAIL_IMPORT_SLICE_MESSAGES', 100)
    monkeypatch.setattr(Config, 'PARSE_TEMP_DIR', tmp_path / 'scratch')
    return SimpleNamespace(base=tmp_path, outputs=outputs, uploads=upload_root,
                           own=own, other=other, shared=shared)


def mail_services(space):
    services = build_mail_services(
        Storage(space.base / 'mail.db'), owner_resolver=lambda: 'owner_a',
        job_factory=lambda *_args, **_kwargs: ('synthetic-queued-job', True),
    )
    services.repository.migrate()
    return services


def package_job(services, path, kind='directory', revision='synthetic-revision'):
    package, _ = services.repository.create_package(
        owner_id='owner_a', display_name='Synthetic source', source_kind=kind,
        source_path=str(path), source_revision=revision, config={},
    )
    return package, {'id': 'synthetic-' + package['package_id'], 'owner_id': 'owner_a',
                     'options': {'package_id': package['package_id']}}


def test_config_injects_reserved_upload_and_native_cache_into_http_and_worker(source_space):
    space = source_space
    for factory in (build_mail_services, worker_mail_services):
        services = factory(Storage(space.base / (factory.__name__ + '.db')))
        assert services.policy.upload_root == space.uploads
        assert services.policy.native_cache_root == Config.DATA_DIR / 'mail-native'
        with pytest.raises(MailPolicyError):
            services.policy.resolve_source(str(space.other), owner_id='owner_a')


@pytest.mark.parametrize('kind', ['large', 'mail'])
def test_own_upload_works_outside_nas_allowlist_and_shared_sources_remain_usable(source_space, kind):
    space = source_space
    if kind == 'large':
        assert _under_allowed_root(space.own, owner_id='owner_a') == space.own.resolve()
        assert _under_allowed_root(space.shared) == space.shared.resolve()
        (space.own / 'a.txt').write_text('synthetic')
        assert [row['path'] for row in iter_inventory(space.own, None, 'owner_a')] == ['a.txt']
        assert list(iter_inventory(space.shared, lambda *_: None)) == []
    else:
        policy = MailPolicy.from_config(Config)
        assert policy.resolve_source(str(space.own), owner_id='owner_a') == space.own.resolve()
        assert policy.resolve_source(str(space.shared)) == space.shared.resolve()


@pytest.mark.parametrize('kind', ['large', 'mail'])
@pytest.mark.parametrize('target', ['ancestor', 'upload_root', 'assembled', 'owner_root', 'staging', 'other_owner'])
def test_broad_allowlist_never_grants_reserved_sources(source_space, monkeypatch, kind, target):
    space = source_space
    monkeypatch.setattr(Config, 'SCAN_ALLOWED_ROOTS', (space.base,))
    paths = {'ancestor': space.outputs, 'upload_root': space.uploads,
             'assembled': space.own.parents[1], 'owner_root': space.own.parent,
             'staging': space.uploads / 'owner_a' / 'staging_session', 'other_owner': space.other}
    paths['staging'].mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError):
        if kind == 'large':
            _under_allowed_root(paths[target], owner_id='owner_a')
        else:
            MailPolicy.from_config(Config).resolve_source(str(paths[target]), owner_id='owner_a')


@pytest.mark.parametrize('kind', ['large', 'mail'])
@pytest.mark.parametrize('owner', [None, '', 'owner_b', 'admin'])
def test_private_source_requires_matching_owner(source_space, kind, owner):
    with pytest.raises(ValueError):
        if kind == 'large':
            _under_allowed_root(source_space.own, owner_id=owner)
        else:
            MailPolicy.from_config(Config).resolve_source(str(source_space.own), owner_id=owner)


@pytest.mark.parametrize('kind', ['large', 'mail'])
@pytest.mark.parametrize('target', ['other_owner', 'upload_ancestor', 'own_owner'])
def test_external_alias_is_authorized_by_canonical_target(source_space, monkeypatch, kind, target):
    space = source_space
    monkeypatch.setattr(Config, 'SCAN_ALLOWED_ROOTS', (space.base,))
    link = space.shared / 'alias'
    destination = {'other_owner': space.other, 'upload_ancestor': space.outputs, 'own_owner': space.own}[target]
    directory_link(link, destination)
    resolve = (lambda: _under_allowed_root(link, owner_id='owner_a')) if kind == 'large' else (
        lambda: MailPolicy.from_config(Config).resolve_source(str(link), owner_id='owner_a'))
    if target == 'own_owner':
        assert resolve() == space.own.resolve()
    else:
        with pytest.raises(ValueError):
            resolve()


def test_large_create_rejects_other_source_and_worker_keeps_owner(source_space, monkeypatch):
    space = source_space
    (space.own / 'document.txt').write_text('synthetic')
    store = LargePackageStore(space.base / 'large.db')
    with pytest.raises(ValueError):
        store.create(str(space.other), owner_id='owner_a')
    assert store.list_history('owner_a')['items'] == []
    package = store.create(str(space.own), owner_id='owner_a')
    worker = LargePackageWorker(store)
    monkeypatch.setattr(worker, '_check_resources', lambda: None)
    assert worker._inventory_one(package)
    assert store.get_file(package['id'], 'document.txt') is not None
    assert store.get(package['id'])['phase'] == 'quick_parse'


def test_large_inventory_does_not_descend_into_private_child_link(source_space):
    space = source_space
    (space.other / 'private.txt').write_text('synthetic other owner content')
    directory_link(space.own / 'alias', space.other)
    assert list(iter_inventory(space.own, owner_id='owner_a')) == []


@pytest.mark.parametrize('phase', ['inventory', 'quick_parse', 'recognize', 'recognize_completed', 'deep_parse'])
def test_persisted_large_source_is_checked_before_worker_reads(source_space, monkeypatch, phase):
    space = source_space
    store = LargePackageStore(space.base / 'large.db')
    package = store.create(str(space.shared), owner_id='owner_a')
    with store._connect() as conn:
        conn.execute('UPDATE packages SET root_path=?,phase=? WHERE id=?', (str(space.other), phase, package['id']))
    worker = LargePackageWorker(store)
    monkeypatch.setattr(worker, '_check_resources', lambda: None)
    def forbidden(*_args, **_kwargs):
        pytest.fail('unauthorized source reached content processing')
    for name in ('_quick_one', '_recognize_one', '_deep_one'):
        monkeypatch.setattr(worker, name, forbidden)
    assert worker._run_admitted_once() is False
    assert store.get(package['id'])['status'] == 'failed'
    assert store.get_file(package['id'], 'document.txt') is None


def test_mail_api_ignores_spoofed_owner_and_does_not_persist_other_source(source_space):
    space = source_space
    services = mail_services(space)
    app = SJFXFastAPI()
    register_mail_routes(app, services)
    with TestClient(app) as client:
        response = client.post('/api/mail/packages', json={
            'owner_id': 'owner_b', 'source': {'path': str(space.other)}},
            headers={'X-Owner-ID': 'owner_b'})
        assert response.status_code == 400, response.text
        assert services.repository.list_packages('owner_a') == []
        response = client.post('/api/mail/packages', json={'source': {'path': str(space.own)}})
        assert response.status_code in (200, 202), response.text
        assert response.json()['package']['owner_id'] == 'owner_a'


def test_mail_worker_checks_persisted_source_before_inventory(source_space, monkeypatch):
    services = mail_services(source_space)
    package, job = package_job(services, source_space.other)
    def forbidden(*_args, **_kwargs):
        pytest.fail('unauthorized source reached adapter')
    monkeypatch.setattr('services.mail.jobs.AdapterRegistry.resolve', forbidden)
    with pytest.raises(MailPolicyError):
        _import(job, services)
    assert services.repository.list_messages(package['package_id'], 'owner_a')['items'] == []


@pytest.mark.parametrize('location', ['shared', 'own'])
@pytest.mark.parametrize('kind', ['directory', 'eml', 'zip', 'mbox'])
def test_mail_worker_source_formats_and_owner_signature_remain_compatible(source_space, location, kind):
    space = source_space
    root = getattr(space, location)
    raw = synthetic_mail()
    if kind == 'directory':
        (root / 'message.eml').write_bytes(raw)
        path = root
    elif kind == 'eml':
        path = root / 'message.eml'
        path.write_bytes(raw)
    elif kind == 'zip':
        path = root / 'messages.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('message.eml', raw)
    else:
        path = root / 'messages.mbox'
        box = mailbox.mbox(str(path), create=True)
        try:
            box.add(raw)
            box.flush()
        finally:
            box.close()
    services = mail_services(space)
    package, job = package_job(services, path, kind)
    result = _import(job, services)
    assert result['counts']['logical_messages'] == 1
    assert len(services.repository.list_messages(package['package_id'], 'owner_a')['items']) == 1


def test_mail_member_replaced_by_other_owner_link_is_never_opened(source_space, monkeypatch):
    from services.mail import foundation
    space = source_space
    child = space.own / 'child'
    child.mkdir()
    (child / 'message.eml').write_bytes(synthetic_mail())
    (space.other / 'message.eml').write_bytes(synthetic_mail())
    inventory = foundation.source_inventory
    def swapped_inventory(*args):
        entries = list(inventory(*args))
        (child / 'message.eml').unlink()
        child.rmdir()
        directory_link(child, space.other)
        yield from entries
    monkeypatch.setattr(foundation, 'source_inventory', swapped_inventory)
    def forbidden(*_args, **_kwargs):
        pytest.fail('replaced member reached open_message')
    monkeypatch.setattr(DirectoryAdapter, 'open_message', forbidden)
    services = mail_services(space)
    package, job = package_job(services, space.own)
    with pytest.raises(ValueError, match='没有成功解析'):
        _import(job, services)
    assert services.repository.list_messages(package['package_id'], 'owner_a')['items'] == []


@pytest.mark.parametrize('kind', ['msg', 'pst'])
def test_native_cache_is_preserved_outside_source_allowlist(source_space, monkeypatch, kind):
    space = source_space
    source = space.own / ('original.' + kind)
    source.write_bytes(b'synthetic native container')
    revision = 'synthetic-native-revision'
    cache = Config.DATA_DIR / 'mail-native' / hashlib.sha256(revision.encode()).hexdigest()
    cache.mkdir(parents=True)
    cached = cache / 'message.eml'
    raw = synthetic_mail()
    cached.write_bytes(raw)
    class SyntheticNativeAdapter:
        def inventory(self, _source, _policy):
            yield MailMember('message.eml', 0, len(raw), {'kind': 'file', 'path': str(cached)})
        def open_message(self, _source, member):
            return Path(member.locator['path']).open('rb')
    monkeypatch.setattr('services.mail.jobs.AdapterRegistry.resolve', lambda *_: SyntheticNativeAdapter())
    services = mail_services(space)
    package, job = package_job(services, source, kind, revision)
    assert _import(job, services)['counts']['logical_messages'] == 1
    key = services.repository.list_messages(package['package_id'], 'owner_a')['items'][0]['message_key']
    source.unlink()
    with services.repository._connection() as conn:
        conn.execute('DELETE FROM mail_raw_store')
    assert read_message_raw(services.repository, key, 'owner_a', package['package_id']) == raw
    with pytest.raises(MailPolicyError):
        services.policy.resolve_locator({'kind': 'file', 'path': str(space.other / 'message.eml')},
                                        owner_id='owner_a', source_kind=kind, source_revision=revision)
    with pytest.raises(MailPolicyError):
        services.policy.resolve_locator({'kind': 'file', 'path': str(cached)}, owner_id='owner_a',
                                        source_kind=kind, source_revision='wrong-revision')
    link = cache / 'alias'
    directory_link(link, space.other)
    with pytest.raises(MailPolicyError):
        services.policy.resolve_locator({'kind': 'file', 'path': str(link / 'message.eml')},
                                        owner_id='owner_a', source_kind=kind, source_revision=revision)


def test_historical_locator_fallback_rechecks_owner_without_losing_saved_raw(source_space, monkeypatch):
    space = source_space
    raw = synthetic_mail()
    own = space.own / 'message.eml'
    own.write_bytes(raw)
    other = space.other / 'message.eml'
    other.write_bytes(raw)  # matching content hash alone cannot authorize a path
    services = mail_services(space)
    package, job = package_job(services, own, 'eml')
    _import(job, services)
    key = services.repository.list_messages(package['package_id'], 'owner_a')['items'][0]['message_key']
    with services.repository._connection() as conn:
        conn.execute('DELETE FROM mail_raw_store')
    assert read_message_raw(services.repository, key, 'owner_a', package['package_id']) == raw
    with services.repository._connection() as conn:
        conn.execute('UPDATE mail_sources SET locator_json=? WHERE package_id=?',
                     (json.dumps({'kind': 'file', 'path': str(other)}), package['package_id']))
    def forbidden(*_args, **_kwargs):
        pytest.fail('historical unauthorized locator reached open')
    monkeypatch.setattr('services.mail.attachments.open_locator', forbidden)
    with pytest.raises(RuntimeError, match='原始邮件来源已不可读'):
        read_message_raw(services.repository, key, 'owner_a', package['package_id'])
    with services.repository._connection() as conn:
        conn.execute('INSERT INTO mail_raw_store(raw_sha256,raw_bytes) VALUES (?,?)',
                     (hashlib.sha256(raw).hexdigest(), raw))
    own.unlink()
    assert read_message_raw(services.repository, key, 'owner_a', package['package_id']) == raw
    with pytest.raises(LookupError):
        read_message_raw(services.repository, key, 'owner_b', package['package_id'])
