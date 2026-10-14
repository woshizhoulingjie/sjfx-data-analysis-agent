"""Synthetic original-download HTTP regressions; no lifespan or model calls."""
import asyncio
import gc
import io
import json
import os
from pathlib import Path
import tarfile
from types import SimpleNamespace
import zipfile

import pytest
from fastapi.testclient import TestClient

import app as web
from services import package_analysis
from services.large_package_isolated import LargePackageStore, iter_inventory
from services.logical_units import archive_member_units
from services.scanner import scan_directory
from services.storage import Storage


TEXT = b'Synthetic selected member, with no sibling contents.'
HTML = b'<script>window.syntheticMarker = true</script>'


def write_archive(path):
    members = {'docs/member.txt': TEXT, 'web/page.HTML': HTML,
               '.env': b'synthetic-secret-only', 'sibling.txt': b'other-member'}
    if path.suffix == '.zip':
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in members.items():
                archive.writestr(name, data)
    else:
        with tarfile.open(path, 'w:gz' if path.name.endswith('.gz') else 'w') as archive:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))


@pytest.fixture
def downloads(tmp_path, monkeypatch):
    root = tmp_path / 'sources'
    root.mkdir()
    for name, data in [('plain.txt', TEXT), ('page.HTML', HTML), ('.env', b'synthetic-secret-only'),
                       ('vector.svg', b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>')]:
        (root / name).write_bytes(data)
    for name in ('bundle.zip', 'bundle.tar', 'bundle.tar.gz'):
        write_archive(root / name)
    if os.getenv('SJFX_TEST_SIMULATE_DENIED_HANDLE_QUERY') == '1':
        # Explicit restricted-host mode: only the Windows path-query API is
        # simulated. Opens, fstat identity checks, copying and ASGI are real.
        # Default runs continue to exercise the native handle-path query.
        opened_paths = {}
        original_open = os.open
        def recorded_open(path, flags, *args, **kwargs):
            fd = original_open(path, flags, *args, **kwargs)
            opened_paths[fd] = Path(path).resolve()
            return fd
        monkeypatch.setattr(os, 'open', recorded_open)
        monkeypatch.setattr(package_analysis, '_opened_handle_path', lambda fd: opened_paths.get(fd))
    storage = Storage(tmp_path / 'ordinary.db')
    large = LargePackageStore(tmp_path / 'large.db')
    monkeypatch.setattr(web, 'storage', storage)
    monkeypatch.setattr(web, 'large_package_store', large)
    monkeypatch.setattr(web.Config, 'PARSE_TEMP_DIR', tmp_path / 'snapshots')
    monkeypatch.setattr(web.Config, 'PARSE_TEMP_DISK_RESERVE_BYTES', 0)
    monkeypatch.setattr(web.Config, 'SOURCE_STABILITY_SECONDS', 0)
    monkeypatch.setattr(web.Config, 'SCAN_ALLOWED_ROOTS', (root,))
    monkeypatch.setattr(web.Config, 'OWNER_ID', 'download-owner')
    monkeypatch.setattr(web.Config, 'AUTH_REQUIRED', True)
    monkeypatch.setattr(web.Config, 'API_ACCESS_TOKEN', 'synthetic-download-test')
    monkeypatch.setattr(web.Config, 'API_TOKEN_EXPIRES_AT', None)
    monkeypatch.setattr(web._file_protection, 'guard_root', lambda _root: None)
    monkeypatch.setattr(web._multiuser_runtime, 'get_current_user', lambda: None)
    identity = [{'owner_id': 'download-owner', 'role': 'user'}]
    monkeypatch.setattr(web, '_multiuser_session_from_request', lambda: identity[0])
    scan = scan_directory(root)
    scan_id = storage.save_scan(scan, scan_id='raw-owner', owner_id='download-owner')
    files = [node for node in web._walk_analysis_nodes(scan['tree']) if node.get('kind') == 'file']
    units = [unit for file in files for unit in archive_member_units(root, file)]
    storage.replace_logical_inventory_entries(scan_id, units)
    package = large.create(root, owner_id='download-owner')
    large.upsert_files(package['id'], list(iter_inventory(root)))
    client = TestClient(web.app)  # Entering lifespan would start workers.
    yield SimpleNamespace(root=root, storage=storage, large=large, scan_id=scan_id,
                          package_id=package['id'], client=client, identity=identity,
                          snapshots=tmp_path / 'snapshots', files=files)
    client.close()
    gc.collect()


def get(case, kind, path, **params):
    url = ('/api/document/' + case.scan_id + '/raw' if kind == 'ordinary'
           else '/api/large-packages/' + case.package_id + '/file')
    return case.client.get(url, params={'path': path, **params})


def put_inventory(case, node):
    with case.storage._connect() as conn:
        conn.execute('INSERT OR REPLACE INTO inventory_entries '
                     '(scan_id,node_path,parent_path,position,kind,payload) VALUES (?,?,?,0,?,?)',
                     (case.scan_id, node['path'], '.', node.get('kind', 'file'), json.dumps(node)))


@pytest.mark.parametrize('kind', ['ordinary', 'large'])
@pytest.mark.parametrize('name', ['plain.txt', 'page.HTML', 'vector.svg'])
def test_physical_bytes_mime_policy_and_cleanup(downloads, kind, name):
    response = get(downloads, kind, name)
    assert response.status_code == 200, response.text
    assert response.content == (downloads.root / name).read_bytes()
    expected_type = {'plain.txt': 'text/plain', 'page.HTML': 'text/html',
                     'vector.svg': 'image/svg+xml'}[name]
    assert response.headers['content-type'].split(';')[0] == expected_type
    disposition = response.headers['content-disposition']
    assert disposition.startswith('inline' if name == 'plain.txt' else 'attachment')
    assert name in disposition
    assert "default-src 'none'" in response.headers['content-security-policy']
    assert 'sandbox' in response.headers['content-security-policy']
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert not list(downloads.snapshots.iterdir())


@pytest.mark.parametrize('kind', ['ordinary', 'large'])
@pytest.mark.parametrize('archive', ['bundle.zip', 'bundle.tar', 'bundle.tar.gz'])
@pytest.mark.parametrize('member,data,mime', [('docs/member.txt', TEXT, 'text/plain'),
                                             ('web/page.HTML', HTML, 'text/html')])
def test_member_is_exact_bytes_with_correct_filename_and_mime(downloads, kind, archive, member, data, mime):
    response = get(downloads, kind, archive + '::' + member, download='1')
    assert response.status_code == 200, response.text
    assert response.content == data
    assert response.headers['content-type'].split(';')[0] == mime
    assert Path(member).name in response.headers['content-disposition']
    assert response.headers['content-disposition'].startswith('attachment')
    assert not list(downloads.snapshots.iterdir())


@pytest.mark.parametrize('kind', ['ordinary', 'large'])
def test_html_member_is_attachment_even_without_download_flag(downloads, kind):
    response = get(downloads, kind, 'bundle.zip::web/page.HTML')
    assert response.status_code == 200, response.text
    assert response.headers['content-disposition'].startswith('attachment')


@pytest.mark.parametrize('kind', ['ordinary', 'large'])
@pytest.mark.parametrize('path', ['.env', 'bundle.zip::.env', 'bundle.zip::missing.txt',
                                 'plain.txt::invented', '../outside.txt'])
def test_restricted_missing_and_escape_paths_fail_closed(downloads, kind, path):
    response = get(downloads, kind, path)
    assert response.status_code == 404, response.text
    assert 'synthetic-secret-only' not in response.text
    assert response.content != (downloads.root / 'bundle.zip').read_bytes()


@pytest.mark.parametrize('flag', ['sensitive', 'content_analysis_allowed', 'content_policy'])
@pytest.mark.parametrize('kind', ['ordinary', 'large'])
def test_inventory_policy_restricts_an_innocent_filename(downloads, monkeypatch, kind, flag):
    value = {'sensitive': True, 'content_analysis_allowed': False,
             'content_policy': 'metadata_only_sensitive'}[flag]
    if kind == 'ordinary':
        node = next(node for node in downloads.files if node['path'] == 'plain.txt')
        put_inventory(downloads, {**node, flag: value})
    else:
        original = downloads.large.get_file
        monkeypatch.setattr(downloads.large, 'get_file',
                            lambda package, path: {**original(package, path), flag: value})
    assert get(downloads, kind, 'plain.txt').status_code == 404


@pytest.mark.parametrize('kind', ['ordinary', 'large'])
def test_sensitive_member_basename_is_enforced_even_if_flag_is_absent(downloads, kind):
    if kind == 'large':
        row = downloads.large.get_file(downloads.package_id, 'bundle.zip::.env')
        row['sensitive'] = False
        downloads.large.upsert_files(downloads.package_id, [row])
    response = get(downloads, kind, 'bundle.zip::.env')
    assert response.status_code == 404
    assert 'synthetic-secret-only' not in response.text


def test_ordinary_member_missing_inventory_never_falls_back_to_container(downloads):
    with downloads.storage._connect() as conn:
        conn.execute('DELETE FROM inventory_entries WHERE scan_id=? AND node_path=?',
                     (downloads.scan_id, 'bundle.zip::docs/member.txt'))
    response = get(downloads, 'ordinary', 'bundle.zip::docs/member.txt')
    assert response.status_code == 404
    assert response.content != (downloads.root / 'bundle.zip').read_bytes()


@pytest.mark.parametrize('kind', ['ordinary', 'large'])
@pytest.mark.parametrize('path', ['plain.txt', 'bundle.zip::docs/member.txt'])
def test_same_size_same_mtime_replacement_fails_inventory_identity(downloads, kind, path):
    source = downloads.root / path.split('::')[0]
    before = source.stat()
    replacement = downloads.root / 'replacement'
    replacement.write_bytes(source.read_bytes().replace(b'Synthetic', b'Changed!!'))
    assert replacement.stat().st_size == before.st_size
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(replacement, source)
    response = get(downloads, kind, path)
    assert response.status_code == 409, response.text
    assert b'Changed!!' not in response.content


@pytest.mark.parametrize('kind', ['ordinary', 'large'])
def test_source_swap_after_response_creation_serves_verified_snapshot(downloads, monkeypatch, kind):
    original = web._raw_snapshot_response
    def swap(snapshot, filename, **kwargs):
        response = original(snapshot, filename, **kwargs)
        source = downloads.root / 'plain.txt'
        replacement = downloads.root / 'later.txt'
        replacement.write_bytes(b'synthetic bytes replaced after snapshot')
        os.replace(replacement, source)
        return response
    monkeypatch.setattr(web, '_raw_snapshot_response', swap)
    response = get(downloads, kind, 'plain.txt')
    assert response.status_code == 200, response.text
    assert response.content == TEXT
    assert not list(downloads.snapshots.iterdir())


def test_legacy_large_inventory_without_metadata_remains_readable(downloads):
    row = downloads.large.get_file(downloads.package_id, 'plain.txt')
    row['source_metadata'] = {}
    row['source_kind'] = ''
    downloads.large.upsert_files(downloads.package_id, [row])
    response = get(downloads, 'large', 'plain.txt')
    assert response.status_code == 200 and response.content == TEXT


@pytest.mark.parametrize('kind', ['ordinary', 'large'])
@pytest.mark.parametrize('path', ['plain.txt', 'bundle.zip::docs/member.txt'])
def test_marked_changed_source_cannot_be_downloaded(downloads, monkeypatch, kind, path):
    if kind == 'ordinary':
        node = (downloads.storage.get_inventory_entry(downloads.scan_id, path)
                or next(node for node in downloads.files if node['path'] == path))
        put_inventory(downloads, {**node, 'source_changed': True})
    else:
        original = downloads.large.get_file
        monkeypatch.setattr(downloads.large, 'get_file',
                            lambda package, name: {**original(package, name), 'source_changed': True})
    response = get(downloads, kind, path)
    assert response.status_code == 409
    assert response.content != TEXT


@pytest.mark.parametrize('kind', ['ordinary', 'large'])
def test_owner_boundary_still_applies(downloads, kind):
    downloads.identity[0] = {'owner_id': 'foreign-owner', 'role': 'user'}
    response = get(downloads, kind, 'plain.txt')
    assert response.status_code == 404
    assert TEXT not in response.content


@pytest.mark.parametrize('kind', ['ordinary', 'large'])
def test_symlink_replacement_is_rejected_when_platform_permits(downloads, kind):
    source = downloads.root / 'plain.txt'
    outside = downloads.root.parent / 'outside.txt'
    outside.write_bytes(TEXT)
    source.unlink()
    try:
        source.symlink_to(outside)
    except OSError as exc:
        pytest.skip('Host denies symlink creation: ' + str(exc))
    response = get(downloads, kind, 'plain.txt')
    assert response.status_code in {404, 409}
    assert response.content != TEXT


@pytest.mark.skipif(os.name != 'nt', reason='Windows descriptor-range branch')
def test_windows_opened_handle_outside_root_fails_closed(downloads, monkeypatch):
    monkeypatch.setattr(package_analysis, '_opened_handle_path',
                        lambda _fd: downloads.root.parent / 'outside.txt')
    response = get(downloads, 'large', 'plain.txt')
    assert response.status_code == 409
    assert response.content != TEXT


def test_unverifiable_opened_handle_fails_closed(downloads, monkeypatch):
    monkeypatch.setattr(package_analysis, '_opened_handle_path', lambda _fd: None)
    if os.name != 'nt':
        pytest.skip('POSIX uses no-follow directory descriptors instead')
    response = get(downloads, 'large', 'plain.txt')
    assert response.status_code == 409
    assert response.content != TEXT


def test_snapshot_is_removed_when_asgi_send_fails(downloads):
    node = next(node for node in downloads.files if node['path'] == 'plain.txt')
    response = web._raw_snapshot_response(web._secure_source_snapshot(downloads.root, node), 'plain.txt')
    assert Path(response.path).exists()
    async def send(_message):
        raise RuntimeError('synthetic disconnected sender')
    async def receive():
        return {'type': 'http.disconnect'}
    with pytest.raises(RuntimeError, match='synthetic disconnected sender'):
        asyncio.run(response({'type': 'http', 'method': 'GET', 'headers': []}, receive, send))
    assert not Path(response.path).exists()
    assert not list(downloads.snapshots.iterdir())
