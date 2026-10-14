"""Private Windows bundles must work without installation into system PATH."""
import os
from types import SimpleNamespace

import pytest

from config import Config
from services import archive_formats, native_tools
from services.mail.source import NativeAdapter


@pytest.mark.parametrize('name', ['7z.exe', '7zz.exe', '7za.exe'])
def test_windows_rar_backend_resolves_declared_bundle(tmp_path, monkeypatch, name):
    tool = tmp_path / '工具 目录' / '7zip' / name
    tool.parent.mkdir(parents=True); tool.write_bytes(b'fixture')
    monkeypatch.setattr(Config, 'TOOLS_DIR', tool.parent.parent)
    monkeypatch.setattr(archive_formats, 'os', SimpleNamespace(name='nt', access=os.access, X_OK=os.X_OK))
    monkeypatch.setattr('shutil.which', lambda _: None)
    module = SimpleNamespace(SEVENZIP_TOOL='7z')
    assert archive_formats.configure_rar_backend(module) == str(tool)
    assert module.SEVENZIP_TOOL == str(tool)


def test_windows_readpst_uses_private_bundle_without_path(tmp_path, monkeypatch):
    tool = tmp_path / '工具 目录' / 'mail-native' / 'bin' / 'readpst.exe'
    tool.parent.mkdir(parents=True); tool.write_bytes(b'fixture')
    monkeypatch.setattr(Config, 'TOOLS_DIR', tool.parents[2])
    monkeypatch.setattr(native_tools, 'os', SimpleNamespace(name='nt', access=os.access, X_OK=os.X_OK))
    monkeypatch.setattr('shutil.which', lambda _: None)
    assert native_tools.readpst_executable() == str(tool)
    assert NativeAdapter('pst', {'.pst'}, 'readpst').available()


def test_windows_readpst_rejects_linux_binary(tmp_path, monkeypatch):
    tool = tmp_path / 'mail-native' / 'bin' / 'readpst'
    tool.parent.mkdir(parents=True); tool.write_bytes(b'ELF fixture')
    monkeypatch.setattr(native_tools, 'os', SimpleNamespace(name='nt', access=os.access, X_OK=os.X_OK))
    monkeypatch.setattr('shutil.which', lambda _: None)
    assert native_tools.readpst_executable(SimpleNamespace(TOOLS_DIR=tmp_path)) is None


@pytest.mark.skipif(os.name == 'nt', reason='Linux private ELF library lookup')
@pytest.mark.parametrize('layout', ['bin', 'usr/bin', ''])
def test_readpst_raw_bundle_gets_child_private_library_path(tmp_path, layout):
    root = tmp_path / '工具 目录'
    tool = root / 'mail-native' / layout / 'readpst'
    tool.parent.mkdir(parents=True); tool.touch()
    library = root / 'mail-native' / 'usr/lib/x86_64-linux-gnu'
    library.mkdir(parents=True)
    inherited = {'LD_LIBRARY_PATH': '/existing', 'PATH': '/usr/bin'}
    result = native_tools.readpst_environment(str(tool), SimpleNamespace(TOOLS_DIR=root), inherited)
    paths = result['LD_LIBRARY_PATH'].split(os.pathsep)
    assert paths[0] == str(library)
    assert paths[-1] == '/existing'
    assert inherited['LD_LIBRARY_PATH'] == '/existing'
    assert native_tools.readpst_environment('/usr/bin/readpst', SimpleNamespace(TOOLS_DIR=root), inherited) == inherited


def test_isolated_mail_attachment_from_external_cwd(tmp_path, monkeypatch):
    from services.mail.extraction import extract_attachment
    state = tmp_path / '用户 状态'
    scratch = state / '解析 缓存'
    foreign = tmp_path / '外部 工作目录'; foreign.mkdir()
    monkeypatch.setattr(Config, 'MAIL_ENABLE_ISOLATED_PARSING', True)
    monkeypatch.setattr(Config, 'PARSE_TEMP_DIR', scratch)
    monkeypatch.setenv('SJFX_STATE_DIR', str(state))
    monkeypatch.setenv('SJFX_OUTPUT_DIR', str(state / 'outputs'))
    monkeypatch.setenv('SJFX_PARSE_TEMP_DIR', str(scratch))
    monkeypatch.delenv('SJFX_ENV_FILE', raising=False)
    monkeypatch.delenv('PYTHONPATH', raising=False)
    monkeypatch.chdir(foreign)
    part = SimpleNamespace(raw_payload='预算 125000'.encode(), content_type='text/plain',
                           filename_raw='正文.txt', charset='utf-8')
    result = extract_attachment(part)
    assert result['status'] == 'ready'
    assert '125000' in result['text']
    assert scratch.is_dir()
    assert not list(scratch.iterdir())


def test_native_retry_publishes_before_yield_without_mixing_failed_cache(tmp_path, monkeypatch):
    import hashlib
    import json
    from pathlib import Path
    from services.mail.models import SourceRef
    from services.mail.policy import MailPolicy, MailPolicyError
    adapter = NativeAdapter('msg', {'.msg'}, 'extract-msg')
    monkeypatch.setattr(Config, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(adapter, 'available', lambda: True)
    source = tmp_path / 'original.msg'; source.write_bytes(b'original remains')
    ref = SourceRef('package', 'msg', str(source), 'retry-revision')
    cache = tmp_path / 'mail-native' / hashlib.sha256(ref.source_revision.encode()).hexdigest()
    cache.mkdir(parents=True)
    old = cache / 'old-failed.eml'; old.write_bytes(b'old retained partial bytes')
    calls = []
    def convert(command, **kwargs):
        target = Path(command[-1]); calls.append(target)
        (target / 'message.eml').write_bytes(b'Subject: new\r\n\r\nnew valid message')
        return SimpleNamespace(returncode=19 if len(calls) == 1 else 0)
    monkeypatch.setattr('subprocess.run', convert)
    policy = MailPolicy.from_config(Config)
    with pytest.raises(MailPolicyError, match='19'):
        list(adapter.inventory(ref, policy))
    assert not list(cache.glob('attempt-*'))
    assert not (cache / 'complete.json').exists()
    iterator = adapter.inventory(ref, policy)
    first = next(iterator)
    published = json.loads((cache / 'complete.json').read_text())['subdir']
    assert published in first.locator['path']
    iterator.close()
    members = list(adapter.inventory(ref, policy))
    assert len(members) == 1 and len(calls) == 2
    assert old.read_bytes() == b'old retained partial bytes'
    assert source.read_bytes() == b'original remains'


def test_native_legacy_complete_cache_is_still_readable(tmp_path, monkeypatch):
    import hashlib
    import json
    from services.mail.models import SourceRef
    from services.mail.policy import MailPolicy
    adapter = NativeAdapter('msg', {'.msg'}, 'extract-msg')
    monkeypatch.setattr(Config, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(adapter, 'available', lambda: True)
    ref = SourceRef('package', 'msg', str(tmp_path / 'mail.msg'), 'legacy')
    cache = tmp_path / 'mail-native' / hashlib.sha256(b'legacy').hexdigest()
    cache.mkdir(parents=True)
    (cache / 'message.eml').write_bytes(b'Subject: legacy\r\n\r\nbody')
    (cache / 'complete.json').write_text(json.dumps({'revision': 'legacy'}))
    assert len(list(adapter.inventory(ref, MailPolicy.from_config(Config)))) == 1


def test_explicit_msg_interpreter_resolves_against_app_and_never_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv('SJFX_MSG_PYTHON', '工具 中文/sidecar-python')
    config = SimpleNamespace(BASE_DIR=tmp_path)
    expected = tmp_path / '工具 中文/sidecar-python'
    assert native_tools.msg_interpreter(config) == str(expected.resolve())
    result = native_tools.msg_parser_probe(config, lambda _: True)
    assert not result['ok'] and result['code'] == 'missing_msg_interpreter'
    assert result['python'] == str(expected.resolve())


@pytest.mark.parametrize('returncode,expected', [(0, True), (1, False)])
def test_msg_sidecar_probe_import_is_bounded_and_isolated(tmp_path, monkeypatch, returncode, expected):
    from pathlib import Path
    python = tmp_path / 'Python 工具.exe'
    python.touch(); python.chmod(0o755)
    monkeypatch.setenv('SJFX_MSG_PYTHON', str(python))
    monkeypatch.setenv('PYTHONPATH', '/must-not-inherit')
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=returncode)
    monkeypatch.setattr('subprocess.run', run)
    result = native_tools.msg_parser_probe()
    assert result['ok'] is expected
    command, kwargs = calls[0]
    assert command[0] == str(python)
    assert '-I' in command and 'import extract_msg' in command
    assert 0 < kwargs['timeout'] <= 8
    assert 'PYTHONPATH' not in kwargs['env']
    assert result['content_parsing_tested'] is False


def test_explicit_msg_missing_dependency_does_not_use_main_runtime(monkeypatch):
    import sys
    monkeypatch.setenv('SJFX_MSG_PYTHON', sys.executable)
    monkeypatch.setattr('subprocess.run', lambda *a, **k: SimpleNamespace(returncode=1))
    assert not native_tools.msg_parser_probe(module_probe=lambda _: True)['ok']
    assert not NativeAdapter('msg', {'.msg'}, 'extract-msg').available()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX venv interpreter symlinks')
def test_msg_python_preserves_venv_symlink_invocation(tmp_path, monkeypatch):
    import sys
    python = tmp_path / 'sidecar/bin/python'
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    monkeypatch.setenv('SJFX_MSG_PYTHON', str(python))
    assert native_tools.msg_interpreter() == str(python)


def test_msg_sidecar_conversion_ignores_inherited_python_settings(tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    from services.mail.models import SourceRef
    from services.mail.policy import MailPolicy
    adapter = NativeAdapter('msg', {'.msg'}, 'extract-msg')
    monkeypatch.setattr(adapter, 'available', lambda: True)
    monkeypatch.setattr(Config, 'DATA_DIR', tmp_path)
    monkeypatch.setenv('SJFX_MSG_PYTHON', sys.executable)
    monkeypatch.setenv('PYTHONPATH', '/untrusted/modules')
    monkeypatch.setenv('PYTHONHOME', '/untrusted/home')
    calls=[]
    def convert(command, **kwargs):
        calls.append((command, kwargs))
        (Path(command[-1]) / 'message.eml').write_bytes(b'Subject: safe\r\n\r\nbody')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr('subprocess.run', convert)
    ref=SourceRef('test','msg',str(tmp_path / 'mail.msg'),'sidecar-command')
    assert len(list(adapter.inventory(ref, MailPolicy.from_config(Config))))==1
    command, kwargs=calls[0]
    assert command[1:4]==['-I','-B','-c']
    assert 'from services.mail.native_adapter import main' in command[4]
    assert 'PYTHONPATH' not in kwargs['env'] and 'PYTHONHOME' not in kwargs['env']
