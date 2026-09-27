import io
import os
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from config import Config
from services import package_analysis as analysis
from services import unified_parser as parser
from services.logical_units import archive_member_units
from services.parse_isolation import ParseIsolationCancelled


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    root=tmp_path/'source';root.mkdir()
    scratch=tmp_path/'scratch';scratch.mkdir()
    monkeypatch.setattr(Config,'PARSE_TEMP_DIR',scratch)
    monkeypatch.setattr(Config,'MAX_ARCHIVE_COMPRESSION_RATIO',200)
    return root,scratch


def container_node(path):
    st=path.stat()
    return {'path':path.name,'name':path.name,'size':st.st_size,
            'modified_at_ns':st.st_mtime_ns,'device':st.st_dev,'inode':st.st_ino}


def node(path,member='report.txt',size=5,**extra):
    c=container_node(path)
    return {'logical_unit':True,'logical_kind':'archive_member','kind':'logical_file',
            'path':path.name+'::'+member,'member_name':member,'name':Path(member).name,
            'extension':Path(member).suffix,'size':size,'container_path':path.name,
            'container_name':path.name,'container_size':c['size'],
            'container_modified_at_ns':c['modified_at_ns'],
            'container_device':c['device'],'container_inode':c['inode'],**extra}


def make_zip(root,members=None,compression=zipfile.ZIP_STORED):
    path=root/'input.zip'
    with zipfile.ZipFile(path,'w',compression=compression) as archive:
        for name,data in (members or [('report.txt',b'hello')]): archive.writestr(name,data)
    return path


def make_tar(root,suffix='.tar',members=None):
    path=root/('input'+suffix)
    mode={'.tar':'w','.tar.gz':'w:gz','.tar.bz2':'w:bz2','.tar.xz':'w:xz'}[suffix]
    with tarfile.open(path,mode) as archive:
        for name,data in (members or [('report.txt',b'hello')]):
            info=tarfile.TarInfo(name);info.size=len(data);archive.addfile(info,io.BytesIO(data))
    return path


@pytest.mark.parametrize('suffix',['.zip','.tar','.tar.gz','.tar.bz2','.tar.xz'])
def test_valid_member_round_trip_and_cleanup(sandbox,suffix):
    root,scratch=sandbox
    path=make_zip(root) if suffix=='.zip' else make_tar(root,suffix)
    original=path.read_bytes()
    with analysis._logical_source_snapshot(root,node(path)) as result:
        assert result.read_bytes()==b'hello'
        assert result.parent.parent==scratch
    assert not list(scratch.iterdir())
    assert path.read_bytes()==original


@pytest.mark.parametrize('bad',['../escape.txt','nested/escape.txt','..\\escape.txt','C:\\escape.txt','C:escape.txt'])
def test_unsafe_snapshot_name_rejected_before_writing(sandbox,bad):
    root,scratch=sandbox;path=make_zip(root)
    try:
        with pytest.raises(ValueError,match='不安全'):
            with analysis._logical_source_snapshot(root,node(path,name=bad)):pass
    finally:
        # Every baseline exploit stays inside this test's temporary directory.
        escaped=scratch/'escape.txt'
        if escaped.exists():escaped.unlink()
    assert not list(scratch.iterdir())


def test_absolute_output_does_not_escape_private_scratch(sandbox,tmp_path):
    root,scratch=sandbox;path=make_zip(root);outside=tmp_path/'outside.txt'
    with pytest.raises(ValueError,match='不安全'):
        with analysis._logical_source_snapshot(root,node(path,name=str(outside))):pass
    assert not outside.exists()


@pytest.mark.parametrize('member',['../report.txt','/report.txt','C:/report.txt','a/../report.txt','a\\report.txt','a//report.txt'])
def test_unsafe_member_not_rewritten_or_materialized(sandbox,member):
    root,scratch=sandbox;path=make_zip(root,[(member,b'hello')])
    assert list(archive_member_units(root,container_node(path)))==[]
    with pytest.raises(ValueError,match='不安全'):
        with analysis._logical_source_snapshot(root,node(path,member,name='safe.txt')):pass
    assert not list(scratch.iterdir())


def test_unicode_nested_name_still_works(sandbox):
    root,scratch=sandbox;member='合同/交付记录.txt';body='实收八台。'.encode()
    path=make_zip(root,[(member,body)])
    units=list(archive_member_units(root,container_node(path)))
    assert units[0]['member_name']==member
    with analysis._logical_source_snapshot(root,units[0]) as target:assert target.read_bytes()==body


@pytest.mark.parametrize('format',['zip','tar'])
def test_duplicate_name_is_not_silently_last_wins(sandbox,format):
    root,scratch=sandbox;members=[('report.txt',b'first'),('report.txt',b'other')]
    path=make_zip(root,members) if format=='zip' else make_tar(root,members=members)
    with pytest.raises(ValueError,match='重复'):
        with analysis._logical_source_snapshot(root,node(path)):pass
    assert not list(scratch.iterdir())


@pytest.mark.parametrize('kind',[tarfile.SYMTYPE,tarfile.LNKTYPE,tarfile.FIFOTYPE])
def test_tar_links_and_special_members_never_resolve_to_content(sandbox,kind):
    root,scratch=sandbox;path=root/'input.tar'
    with tarfile.open(path,'w') as archive:
        target=tarfile.TarInfo('target.txt');target.size=5;archive.addfile(target,io.BytesIO(b'hello'))
        link=tarfile.TarInfo('report.txt');link.type=kind;link.linkname='target.txt';archive.addfile(link)
    with pytest.raises(ValueError,match='链接|特殊'):
        with analysis._logical_source_snapshot(root,node(path,size=0)):pass
    assert not list(scratch.iterdir())


def test_zip_symlink_not_read_as_plain_file(sandbox):
    root,scratch=sandbox;info=zipfile.ZipInfo('report.txt');info.create_system=3
    info.external_attr=(stat.S_IFLNK|0o777)<<16
    path=make_zip(root,[(info,b'target.txt')])
    with pytest.raises(ValueError,match='链接'):
        with analysis._logical_source_snapshot(root,node(path,size=10)):pass
    assert not list(scratch.iterdir())


@pytest.mark.parametrize('limit',['MAX_ARCHIVE_MEMBER_BYTES','MAX_ARCHIVE_UNCOMPRESSED_BYTES'])
def test_zip_rejected_before_member_is_opened(sandbox,monkeypatch,limit):
    root,scratch=sandbox;path=make_zip(root);monkeypatch.setattr(Config,limit,4)
    monkeypatch.setattr(zipfile.ZipFile,'open',lambda *a,**k:pytest.fail('opened before limit validation'))
    with pytest.raises(ValueError,match='安全上限'):
        with analysis._logical_source_snapshot(root,node(path)):pass
    assert not list(scratch.iterdir())


def test_compression_ratio_checked_before_open(sandbox,monkeypatch):
    root,scratch=sandbox;path=make_zip(root,[('report.txt',b'x'*100000)],zipfile.ZIP_DEFLATED)
    monkeypatch.setattr(zipfile.ZipFile,'open',lambda *a,**k:pytest.fail('opened compressed bomb'))
    with pytest.raises(ValueError,match='压缩比|安全上限'):
        with analysis._logical_source_snapshot(root,node(path,size=100000)):pass


def test_size_change_rejected_before_open(sandbox,monkeypatch):
    root,scratch=sandbox;path=make_zip(root)
    monkeypatch.setattr(zipfile.ZipFile,'open',lambda *a,**k:pytest.fail('opened changed member'))
    with pytest.raises(analysis.SourceFileChangedError):
        with analysis._logical_source_snapshot(root,node(path,size=9)):pass


def test_zip_metadata_preflight_runs_before_zipfile(sandbox,monkeypatch):
    root,scratch=sandbox;path=make_zip(root,[('report.txt',b'hello'),('other.txt',b'world')])
    monkeypatch.setattr(parser,'ZIP_MAX_CENTRAL_DIRECTORY_ENTRIES',1)
    monkeypatch.setattr(zipfile,'ZipFile',lambda *a,**k:pytest.fail('ZipFile allocated before preflight'))
    with pytest.raises(ValueError,match='预检失败'):
        with analysis._logical_source_snapshot(root,node(path)):pass


def test_preflight_open_handle_keeps_position_and_ignores_replaced_path(sandbox):
    root,scratch=sandbox;path=make_zip(root)
    with path.open('rb') as handle:
        handle.seek(7);path.rename(root/'original.zip');path.write_bytes(b'not a zip')
        assert parser._zip_central_directory_preflight(handle)['safe']
        assert handle.tell()==7 and not handle.closed


def test_tar_member_count_limit(sandbox,monkeypatch):
    root,scratch=sandbox;path=make_tar(root,members=[('report.txt',b'hello'),('other.txt',b'world')])
    monkeypatch.setattr(Config,'MAX_LOGICAL_UNITS_PER_CONTAINER',1)
    with pytest.raises(ValueError,match='数量'):
        with analysis._logical_source_snapshot(root,node(path)):pass
    assert not list(scratch.iterdir())


def test_tar_logical_count_is_not_preview_count(sandbox,monkeypatch):
    root,scratch=sandbox
    path=make_tar(root,members=[('report.txt',b'hello'),('other.txt',b'world')])
    monkeypatch.setattr(Config,'MAX_ARCHIVE_ENTRIES',1)
    monkeypatch.setattr(Config,'MAX_LOGICAL_UNITS_PER_CONTAINER',3)
    with analysis._logical_source_snapshot(root,node(path)) as target:
        assert target.read_bytes()==b'hello'
    assert not list(scratch.iterdir())


@pytest.mark.parametrize('member',['./records/report.txt','records/report:2026.txt'])
def test_posix_archive_exact_names_remain_readable(sandbox,member):
    if os.name=='nt' and ':' in member:
        pytest.skip('POSIX colon name is not a Windows output filename')
    root,scratch=sandbox;path=make_tar(root,members=[(member,b'hello')])
    units=list(archive_member_units(root,container_node(path)))
    assert len(units)==1 and units[0]['member_name']==member
    with analysis._logical_source_snapshot(root,units[0]) as target:
        assert target.read_bytes()==b'hello'
    assert not list(scratch.iterdir())


def test_tar_total_limit_is_checked_before_skipping_body(sandbox,monkeypatch):
    root,scratch=sandbox;path=make_tar(root,members=[('other.txt',b'world'),('report.txt',b'hello')])
    monkeypatch.setattr(Config,'MAX_ARCHIVE_UNCOMPRESSED_BYTES',4)
    with pytest.raises(ValueError,match='解压总量'):
        with analysis._logical_source_snapshot(root,node(path)):pass


def test_cancelled_archive_scan_cleans_up(sandbox):
    root,scratch=sandbox;path=make_tar(root)
    with pytest.raises(ParseIsolationCancelled):
        with analysis._logical_source_snapshot(root,node(path),lambda:True):pass
    assert not list(scratch.iterdir())


@pytest.mark.parametrize('typecode',[tarfile.XHDTYPE,tarfile.GNUTYPE_LONGNAME])
def test_tar_extension_metadata_is_bounded_before_read(sandbox,typecode):
    root,scratch=sandbox;path=root/'input.tar'
    info=tarfile.TarInfo('extended');info.type=typecode;info.size=2*1024*1024
    path.write_bytes(info.tobuf()+b'\0'*1024)
    with pytest.raises(ValueError,match='元数据'):
        with analysis._logical_source_snapshot(root,node(path)):pass
    assert not list(scratch.iterdir())
