"""Real smart-catalog selection -> original member bytes in handoff ZIPs."""
import hashlib
import io
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
import tarfile
import zipfile

import pytest

from config import Config
from services.exporter import export_node
from services.large_package_isolated import LargePackageStore, iter_inventory
from services.package_analysis import SourceFileChangedError


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / 'source'
    root.mkdir()
    monkeypatch.setattr(Config, 'SCAN_ALLOWED_ROOTS', (root,))
    monkeypatch.setattr(Config, 'PARSE_TEMP_DIR', tmp_path / 'scratch')
    monkeypatch.setattr(Config, 'PARSE_TEMP_DISK_RESERVE_BYTES', 0)
    return root


def write_archive(path, entries):
    if path.suffix == '.zip':
        with zipfile.ZipFile(path, 'w') as archive:
            for name, body in entries:
                archive.writestr(name, body)
    else:
        mode = next((mode for suffix, mode in (('.tar.gz', 'w:gz'), ('.tar.bz2', 'w:bz2'),
                                                ('.tar.xz', 'w:xz'))
                     if str(path).endswith(suffix)), 'w')
        with tarfile.open(path, mode) as archive:
            for name, body in entries:
                info = tarfile.TarInfo(name)
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))


def selection_for(root, paths):
    store = LargePackageStore(root.parent / 'large.db')
    package = store.create(str(root), owner_id='export-owner')
    store.upsert_files(package['id'], list(iter_inventory(root)))
    return store.export_selection(package['id'], paths=paths)


def export_selection(root, selection, **options):
    return export_node(
        root, root, {}, root.parent / 'output', options.pop('max_bytes', 1024 * 1024),
        task_topic='真实成员导出测试', member_paths=selection['paths'],
        inventory_metadata=selection['inventory'], disk_reserve_bytes=0,
        **options,
    )


@pytest.mark.parametrize('suffix', ['.zip', '.tar', '.tar.gz', '.tar.bz2', '.tar.xz'])
def test_smart_catalog_selection_exports_exact_archive_member_bytes(project, suffix):
    payload = '真实测试资料：采购验收\n'.encode() + bytes(range(256))
    archive = project / ('资料' + suffix)
    write_archive(archive, [('采购/原件.bin', payload), ('unselected.txt', b'unselected')])
    logical_path = archive.name + '::采购/原件.bin'
    selection = selection_for(project, [logical_path])
    assert selection['inventory'][logical_path]['source_kind'] == 'archive_member'
    result = export_selection(project, selection, content_deduplication=False)
    with zipfile.ZipFile(result) as output:
        archive_path = archive.name + '.members/采购/原件.bin'
        assert output.read(archive_path) == payload
        assert not any('unselected' in name for name in output.namelist())
        manifest = json.loads(output.read('源文件SHA-256清单.json'))
        assert manifest['files'][0]['path'] == logical_path
        assert manifest['files'][0]['archive_path'] == archive_path
        assert manifest['files'][0]['sha256'] == hashlib.sha256(payload).hexdigest()
    assert not list(Config.PARSE_TEMP_DIR.glob('sjfx-*'))


@pytest.mark.parametrize('deduplicate', [False, True])
def test_mixed_physical_and_member_selection_preserves_bytes_and_provenance(project, deduplicate):
    (project / 'ordinary.bin').write_bytes(b'ordinary')
    write_archive(project / 'a.zip', [('item.bin', b'member')])
    selection = selection_for(project, ['ordinary.bin', 'a.zip::item.bin'])
    result = export_selection(project, selection, content_deduplication=deduplicate)
    with zipfile.ZipFile(result) as output:
        assert output.read('ordinary.bin') == b'ordinary'
        assert output.read('a.zip.members/item.bin') == b'member'
        manifest = json.loads(output.read('源文件SHA-256清单.json'))
        assert {item['path'] for item in manifest['files']} == set(selection['paths'])


def test_identical_members_and_physical_files_can_be_deduplicated(project):
    (project / 'ordinary.bin').write_bytes(b'identical')
    write_archive(project / 'a.zip', [('item.bin', b'identical')])
    selection = selection_for(project, ['ordinary.bin', 'a.zip::item.bin'])
    result = export_selection(project, selection, content_deduplication=True)
    with zipfile.ZipFile(result) as output:
        handoff = json.loads(output.read('整编任务说明.json'))
        groups = handoff['deduplication']['content_duplicate_groups']
        assert len(groups) == 1
        assert {groups[0]['canonical'], *groups[0]['omitted']} == set(selection['paths'])
        assert output.read('a.zip.members/item.bin') == b'identical'


def replace_container(path):
    before = path.stat()
    replacement = path.with_name('replacement' + path.suffix)
    replacement.write_bytes(path.read_bytes())
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(replacement, path)
    assert path.stat().st_size == before.st_size
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert path.stat().st_ino != before.st_ino


def assert_no_published_export(root):
    assert not list((root.parent / 'output').glob('*.zip'))
    assert not list((root.parent / 'output').glob('*.part'))
    assert not list(Config.PARSE_TEMP_DIR.glob('sjfx-*'))


def test_same_size_timestamp_container_replacement_is_rejected(project):
    archive = project / 'a.zip'
    write_archive(archive, [('item.bin', b'original')])
    selection = selection_for(project, ['a.zip::item.bin'])
    replace_container(archive)
    with pytest.raises(SourceFileChangedError, match='对象'):
        export_selection(project, selection, content_deduplication=False)
    assert_no_published_export(project)


def test_container_replacement_after_snapshot_rejects_atomic_zip(project, monkeypatch):
    import services.large_archive_members as members

    archive = project / 'a.zip'
    write_archive(archive, [('item.bin', b'original')])
    selection = selection_for(project, ['a.zip::item.bin'])
    snapshot = members.member_snapshot

    @contextmanager
    def replacing_snapshot(root, row, **kwargs):
        with snapshot(root, row, **kwargs) as path:
            yield path
            replace_container(archive)

    monkeypatch.setattr(members, 'member_snapshot', replacing_snapshot)
    with pytest.raises(SourceFileChangedError, match='对象'):
        export_selection(project, selection, content_deduplication=False)
    assert_no_published_export(project)


def test_container_ancestor_link_outside_scan_is_rejected(project):
    nested = project / 'nested'
    nested.mkdir()
    write_archive(nested / 'a.zip', [('item.bin', b'original')])
    selection = selection_for(project, ['nested/a.zip::item.bin'])
    outside = project.parent / 'outside'
    # Both move targets are synthetic paths within this fixture's temp base.
    nested.resolve().relative_to(project.parent.resolve())
    outside.resolve().relative_to(project.parent.resolve())
    nested.rename(outside)
    try:
        nested.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != 'nt':
            pytest.skip('platform cannot create directory links')
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(nested), str(outside)],
                                capture_output=True, check=False)
        if result.returncode != 0:
            pytest.skip('platform cannot create junctions')
    # The container object and timestamps are unchanged; path containment alone
    # must still reject this alias before original member bytes can be exported.
    with pytest.raises((SourceFileChangedError, OSError, ValueError)) as rejected:
        export_selection(project, selection, content_deduplication=False)
    if isinstance(rejected.value, ValueError):
        # POSIX descriptor traversal refuses a symlink with ENOTDIR/ELOOP;
        # the export adapter wraps that I/O error for its public API contract.
        assert '无法读取源文件' in str(rejected.value)
        assert isinstance(rejected.value.__cause__ or rejected.value.__context__, OSError)
    assert_no_published_export(project)


@pytest.mark.parametrize('changed', ['member_name', 'container_inode', 'member_size'])
def test_member_source_metadata_tampering_is_rejected(project, changed):
    write_archive(project / 'a.zip', [('item.bin', b'original')])
    selection = selection_for(project, ['a.zip::item.bin'])
    row = selection['inventory']['a.zip::item.bin']
    row['source_metadata'] = dict(row['source_metadata'])
    row['source_metadata'][changed] = ('different.bin' if changed == 'member_name'
                                      else row['source_metadata'][changed] + 1)
    with pytest.raises((ValueError, SourceFileChangedError)):
        export_selection(project, selection, content_deduplication=False)
    assert_no_published_export(project)


def test_forged_logical_flags_without_trusted_metadata_are_rejected(project):
    selection = {'paths': ['a.zip::item.bin'], 'inventory': {
        'a.zip::item.bin': {'kind': 'file', 'path': 'a.zip::item.bin',
                            'logical_unit': True, 'logical_kind': 'archive_member', 'size': 1}}}
    with pytest.raises(ValueError, match='可信来源身份'):
        export_selection(project, selection, content_deduplication=False)
    assert_no_published_export(project)


def test_sensitive_and_noise_members_are_excluded(project):
    names = ['.env', 'credentials.json', 'id_rsa', 'private.pem', '.env.production',
             '.git/config', 'work/result.txt', 'normal.txt']
    write_archive(project / 'a.zip', [(name, name.encode()) for name in names])
    (project / 'credentials.json').write_text('physical secret')
    paths = ['a.zip::' + name for name in names] + ['credentials.json']
    selection = selection_for(project, paths)
    # A stale/incorrect flag must not bypass filename policy.
    for row in selection['inventory'].values():
        row['sensitive'] = False
    result = export_selection(project, selection, content_deduplication=False, documents=[{
        'path': 'a.zip::.env', 'payload': {'text': 'sensitive-marker', 'evidence': [{
            'evidence_id': 'secret', 'source_path': 'a.zip::.env', 'text': 'sensitive-marker'}]},
    }])
    with zipfile.ZipFile(result) as output:
        manifest = json.loads(output.read('源文件SHA-256清单.json'))
        assert [item['path'] for item in manifest['files']] == ['a.zip::normal.txt']
        assert output.read('a.zip.members/normal.txt') == b'normal.txt'
        exclusions = json.loads(output.read('导出清单.json'))
        assert set(exclusions['excluded_files']) == set(paths) - {'a.zip::normal.txt'}
        assert not any(b'sensitive-marker' in output.read(name) for name in output.namelist())


def test_member_expected_content_hash_mismatch_rejects_export(project):
    write_archive(project / 'a.zip', [('item.bin', b'original')])
    selection = selection_for(project, ['a.zip::item.bin'])
    with pytest.raises(ValueError, match='摘要不一致'):
        export_selection(project, selection, content_deduplication=False,
                         known_hashes={'a.zip::item.bin': '0' * 64})
    assert_no_published_export(project)


def test_empty_member_exports_zero_bytes(project):
    write_archive(project / 'a.zip', [('empty.bin', b'')])
    selection = selection_for(project, ['a.zip::empty.bin'])
    result = export_selection(project, selection, content_deduplication=True)
    with zipfile.ZipFile(result) as output:
        assert output.read('a.zip.members/empty.bin') == b''
        row = json.loads(output.read('源文件SHA-256清单.json'))['files'][0]
        assert row['size'] == 0 and row['sha256'] == hashlib.sha256(b'').hexdigest()


@pytest.mark.parametrize('names', [(['Item.txt', 'item.txt']), (['item', 'item/sub.txt']),
                                  (['./item.txt', 'item.txt'])])
def test_member_export_names_cannot_conflict(project, names):
    write_archive(project / 'a.tar', [(name, name.encode()) for name in names])
    selection = selection_for(project, ['a.tar::' + name for name in names])
    with pytest.raises(ValueError, match='文件名冲突'):
        export_selection(project, selection, content_deduplication=False)
    assert_no_published_export(project)


def test_member_namespace_cannot_collide_with_physical_file(project):
    write_archive(project / 'a.zip', [('item.bin', b'member')])
    physical = project / 'a.zip.members' / 'item.bin'
    physical.parent.mkdir()
    physical.write_bytes(b'physical')
    selection = selection_for(project, ['a.zip::item.bin', 'a.zip.members/item.bin'])
    with pytest.raises(ValueError, match='文件名冲突'):
        export_selection(project, selection, content_deduplication=False)
    assert_no_published_export(project)


@pytest.mark.parametrize('name', ['trailing.', 'trailing ', 'CON', 'a|b.txt'])
def test_member_export_names_are_portable_and_safe(project, name):
    write_archive(project / 'a.zip', [(name, b'original')])
    selection = selection_for(project, ['a.zip::' + name])
    with pytest.raises(ValueError, match='文件名不安全'):
        export_selection(project, selection, content_deduplication=False)
    assert_no_published_export(project)


def test_generated_metadata_names_cannot_shadow_selected_physical_source(project):
    (project / '源文件SHA-256清单.json').write_bytes(b'original')
    selection = selection_for(project, ['源文件SHA-256清单.json'])
    with pytest.raises(ValueError, match='文件名冲突'):
        export_selection(project, selection, content_deduplication=False)
    assert_no_published_export(project)


@pytest.mark.parametrize('gate', ['export_limit', 'member_limit', 'ratio', 'output_disk', 'scratch_disk'])
def test_member_limits_and_capacity_fail_closed(project, monkeypatch, gate):
    import services.large_archive_members as members
    import services.exporter as exporter

    write_archive(project / 'a.zip', [('item.bin', b'content')])
    selection = selection_for(project, ['a.zip::item.bin'])
    called = []
    snapshot = members.member_snapshot

    @contextmanager
    def observed_snapshot(*args, **kwargs):
        called.append(True)
        with snapshot(*args, **kwargs) as path:
            yield path

    monkeypatch.setattr(members, 'member_snapshot', observed_snapshot)
    options = {'content_deduplication': False}
    if gate == 'export_limit':
        options['max_bytes'] = 1
    elif gate == 'member_limit':
        monkeypatch.setattr(Config, 'MAX_ARCHIVE_MEMBER_BYTES', 1)
    elif gate == 'ratio':
        monkeypatch.setattr(Config, 'MAX_ARCHIVE_COMPRESSION_RATIO', 0.01)
    else:
        usage = exporter.shutil.disk_usage
        monkeypatch.setattr(exporter.shutil, 'disk_usage',
                            lambda path: type('Usage', (), {'free': 0})()
                            if Path(path).name == ('output' if gate == 'output_disk' else 'scratch')
                            else usage(path))
    with pytest.raises(ValueError):
        export_selection(project, selection, **options)
    assert not called
    assert_no_published_export(project)


def test_members_can_export_as_independent_volumes(project):
    write_archive(project / 'a.zip', [('one.bin', b'123'), ('two.bin', b'456')])
    selection = selection_for(project, ['a.zip::one.bin', 'a.zip::two.bin'])
    result = export_selection(project, selection, content_deduplication=False, max_bytes=3)
    volumes = json.loads(result.with_name(result.name + '.parts.json').read_text(encoding='utf-8'))
    assert len(volumes['parts']) == 2
    for part in volumes['parts']:
        with zipfile.ZipFile(result.parent / part['file_name']) as output:
            row = json.loads(output.read('源文件SHA-256清单.json'))['files'][0]
            assert output.read(row['archive_path']) in {b'123', b'456'}


def test_existing_large_export_worker_uses_member_export_and_persists_owner(project, monkeypatch):
    import app as web
    from services.storage import Storage

    write_archive(project / 'a.zip', [('item.bin', b'worker raw bytes')])
    store = LargePackageStore(project.parent / 'large.db')
    package = store.create(str(project), owner_id='export-owner')
    store.upsert_files(package['id'], list(iter_inventory(project)))
    jobs = Storage(project.parent / 'jobs.db')
    job_id = jobs.create_job(package['id'], {
        'source_kind': 'large_package', 'paths': ['a.zip::item.bin'], 'task_topic': '实际导出任务',
    }, task_type='export_handoff', owner_id='export-owner')
    job = jobs.claim_next_job('test-export-worker')
    assert job['id'] == job_id
    monkeypatch.setattr(web, 'large_package_store', store)
    monkeypatch.setattr(web, 'storage', jobs)
    monkeypatch.setattr(web, '_request_owner_id', lambda: 'export-owner')
    monkeypatch.setattr(Config, 'OUTPUT_DIR', project.parent / 'output')
    monkeypatch.setattr(Config, 'EXPORT_DISK_RESERVE_BYTES', 0)
    result = web._run_claimed_export_job(job)
    assert result['source_file_count'] == 1
    assert jobs.artifact_owner(result['file_name']) == 'export-owner'
    archive = Config.OUTPUT_DIR / 'export-owner' / result['file_name']
    with zipfile.ZipFile(archive) as output:
        assert output.read('a.zip.members/item.bin') == b'worker raw bytes'
    with pytest.raises(ValueError, match='不属于'):
        web._run_claimed_export_job(dict(job, owner_id='different-owner'))
