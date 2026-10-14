"""Exercise real instance lifecycle on the host OS without model resources."""
import errno
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('winerror', [5, 32, 33])
def test_atomic_json_retries_windows_sharing_errors(tmp_path, monkeypatch, winerror):
    import sjfxctl
    path = tmp_path / 'status.json'
    path.write_text('{"phase": "old"}', encoding='utf-8')
    attempts = []
    sleeps = []
    def replace(source, destination):
        attempts.append(source)
        assert json.loads(path.read_text(encoding='utf-8')) == {'phase': 'old'}
        assert json.loads(source.read_text(encoding='utf-8')) == {'phase': 'ready'}
        if len(attempts) < 3:
            error = PermissionError('injected sharing conflict')
            error.winerror = winerror
            raise error
        os.replace(source, destination)
    monkeypatch.setattr(sjfxctl, 'os', SimpleNamespace(name='nt', chmod=os.chmod, replace=replace))
    monkeypatch.setattr(sjfxctl, 'time', SimpleNamespace(monotonic=lambda: sum(sleeps), sleep=sleeps.append))
    sjfxctl._atomic_json(path, {'phase': 'ready'})
    assert len(attempts) == 3
    assert sleeps and 0 < sum(sleeps) < 1
    assert json.loads(path.read_text(encoding='utf-8')) == {'phase': 'ready'}
    assert not list(tmp_path.glob('*.tmp'))

@pytest.mark.parametrize(('platform_name', 'winerror'), [('nt', 1), ('posix', 5), ('nt', None)])
def test_atomic_json_does_not_retry_other_permission_errors(tmp_path, monkeypatch, platform_name, winerror):
    import sjfxctl
    path = tmp_path / 'status.json'
    path.write_text('{"phase": "old"}', encoding='utf-8')
    error = PermissionError('persistent permission failure')
    error.winerror = winerror
    attempts = []
    def replace(*args):
        attempts.append(args)
        raise error
    monkeypatch.setattr(sjfxctl, 'os', SimpleNamespace(name=platform_name, chmod=os.chmod, replace=replace))
    monkeypatch.setattr(sjfxctl, 'time', SimpleNamespace(monotonic=lambda: 0, sleep=lambda _: pytest.fail('unexpected retry')))
    with pytest.raises(PermissionError) as raised:
        sjfxctl._atomic_json(path, {'phase': 'ready'})
    assert raised.value is error
    assert len(attempts) == 1
    assert json.loads(path.read_text(encoding='utf-8')) == {'phase': 'old'}
    assert not list(tmp_path.glob('*.tmp'))

@pytest.mark.skipif(os.name != 'nt', reason='Windows file sharing semantics')
def test_atomic_json_recovers_after_real_reader_releases_file(tmp_path, monkeypatch):
    import sjfxctl
    path = tmp_path / 'status.json'
    path.write_text('{"phase": "old"}', encoding='utf-8')
    reader = path.open('rb')
    conflict = threading.Event()
    errors = []
    def replace(source, destination):
        try:
            os.replace(source, destination)
        except PermissionError as exc:
            errors.append(exc.winerror)
            conflict.set()
            raise
    def release():
        if conflict.wait(2):
            time.sleep(.075)
        reader.close()
    releaser = threading.Thread(target=release)
    monkeypatch.setattr(sjfxctl, 'os', SimpleNamespace(name='nt', chmod=os.chmod, replace=replace))
    releaser.start()
    try:
        sjfxctl._atomic_json(path, {'phase': 'ready'})
    finally:
        conflict.set()
        releaser.join(timeout=3)
        reader.close()
    assert not releaser.is_alive()
    assert errors and set(errors) <= {5, 32, 33}
    assert json.loads(path.read_text(encoding='utf-8')) == {'phase': 'ready'}
    assert not list(tmp_path.glob('*.tmp'))

@pytest.mark.skipif(os.name != 'nt', reason='Windows file sharing semantics')
def test_atomic_json_persistent_reader_lock_is_bounded_and_cleans_up(tmp_path):
    import sjfxctl
    path = tmp_path / 'status.json'
    path.write_text('{"phase": "old"}', encoding='utf-8')
    with path.open('rb'):
        started = time.monotonic()
        with pytest.raises(PermissionError) as raised:
            sjfxctl._atomic_json(path, {'phase': 'ready'})
        elapsed = time.monotonic() - started
    assert raised.value.winerror in {5, 32, 33}
    assert .9 <= elapsed < 3
    assert json.loads(path.read_text(encoding='utf-8')) == {'phase': 'old'}
    assert not list(tmp_path.glob('*.tmp'))

def _exclusive_windows_reader(path):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    def close():
        nonlocal handle
        if handle is not None:
            if not kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
            handle = None
    return close

@pytest.mark.skipif(os.name != 'nt', reason='Windows file sharing semantics')
def test_read_json_recovers_after_real_exclusive_handle_is_released(tmp_path):
    import sjfxctl
    path = tmp_path / 'status.json'
    path.write_text('{"phase": "ready"}', encoding='utf-8')
    close = _exclusive_windows_reader(path)
    conflict = threading.Event()
    errors = []
    def read_text(**kwargs):
        try:
            return path.read_text(**kwargs)
        except PermissionError as exc:
            errors.append((exc.errno, exc.winerror))
            conflict.set()
            raise
    def release():
        if conflict.wait(2):
            time.sleep(.075)
        close()
    releaser = threading.Thread(target=release)
    releaser.start()
    try:
        assert sjfxctl._read_json(SimpleNamespace(read_text=read_text)) == {'phase': 'ready'}
    finally:
        conflict.set()
        releaser.join(timeout=3)
        close()
    assert not releaser.is_alive()
    assert errors
    assert all(windows_error in {5, 32, 33} or (code == errno.EACCES and windows_error is None)
               for code, windows_error in errors)

@pytest.mark.skipif(os.name != 'nt', reason='Windows file sharing semantics')
def test_read_json_persistent_exclusive_handle_is_bounded(tmp_path):
    import sjfxctl
    path = tmp_path / 'status.json'
    path.write_text('{"phase": "ready"}', encoding='utf-8')
    close = _exclusive_windows_reader(path)
    try:
        started = time.monotonic()
        with pytest.raises(PermissionError) as raised:
            sjfxctl._read_json(path)
        elapsed = time.monotonic() - started
    finally:
        close()
    error = raised.value
    assert error.winerror in {5, 32, 33} or (error.errno == errno.EACCES and error.winerror is None)
    assert .9 <= elapsed < 3
    assert sjfxctl._read_json(path) == {'phase': 'ready'}

@pytest.mark.parametrize('contents', [None, '{invalid json'])
def test_read_json_missing_or_invalid_still_returns_empty(tmp_path, contents):
    import sjfxctl
    path = tmp_path / 'status.json'
    if contents is not None:
        path.write_text(contents, encoding='utf-8')
    assert sjfxctl._read_json(path) == {}

@pytest.mark.parametrize(('platform_name', 'code', 'winerror'),
                         [('posix', errno.EACCES, None), ('nt', errno.EPERM, None),
                          ('nt', errno.EACCES, 1)])
def test_read_json_does_not_retry_unrelated_permission_errors(monkeypatch, platform_name, code, winerror):
    import sjfxctl
    error = PermissionError(code, 'permanent unrelated permission failure')
    error.winerror = winerror
    attempts = []
    def read_text(**kwargs):
        attempts.append(kwargs)
        raise error
    monkeypatch.setattr(sjfxctl, 'os', SimpleNamespace(name=platform_name))
    monkeypatch.setattr(sjfxctl, 'time', SimpleNamespace(monotonic=lambda: 0, sleep=lambda _: pytest.fail('unexpected retry')))
    with pytest.raises(PermissionError) as raised:
        sjfxctl._read_json(SimpleNamespace(read_text=read_text))
    assert raised.value is error
    assert len(attempts) == 1

def invoke(envfile, *args, timeout=60, cwd=None):
    env = os.environ.copy()
    for key in ('SJFX_ENV_FILE', 'SJFX_STATE_DIR', 'SJFX_OUTPUT_DIR', 'SJFX_MODELS_DIR',
                'SJFX_EXTRA_BIND_HOSTS', 'HOST', 'PORT', 'AUTH_REQUIRED', 'LLM_BACKEND',
                'ENABLE_VLLM', 'ENABLE_SHARED_OLLAMA', 'ENABLE_VLLM_EMBEDDINGS',
                'SCAN_ALLOWED_ROOTS', 'SJFX_PARSE_TEMP_DIR', 'HF_HOME', 'DOCLING_CACHE_DIR'):
        env.pop(key, None)
    return subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'sjfxctl.py'),
                           '--env-file', str(envfile), *args],
                          cwd=cwd or ROOT, env=env, capture_output=True, text=True,
                          encoding='utf-8', timeout=timeout)

def _port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]

def _setup(tmp_path, port=None):
    envfile = tmp_path / '配置 目录' / 'sjfx.env'
    options = ['setup', '--port', str(port or _port()), '--backend', 'ollama', '--helper-port', str(_port())]
    if os.name == 'nt':
        helper = os.getenv('SJFX_TEST_WINDOWS_HELPER')
        if helper:
            options += ['--helper-binary', helper]
    result = invoke(envfile, *options, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'SJFX_API_ACCESS_TOKEN' not in result.stdout
    return envfile

def test_setup_does_not_overwrite_existing_config(tmp_path):
    envfile = _setup(tmp_path)
    original = envfile.read_bytes()
    again = invoke(envfile, 'setup')
    assert again.returncode == 1
    assert envfile.read_bytes() == original
    assert 'already exists' in again.stdout

def test_doctor_handles_real_config_and_missing_model_resources(tmp_path):
    envfile = _setup(tmp_path)
    content = envfile.read_text(encoding='utf-8').replace('ENABLE_SHARED_OLLAMA=1', 'ENABLE_SHARED_OLLAMA=0')
    content += '\nENABLE_VLLM_EMBEDDINGS=0\nENABLE_SHARED_OLLAMA_EMBEDDINGS=0\nMAIL_ENABLE_VECTOR_INDEX=0\n'
    envfile.write_text(content, encoding='utf-8')
    result = invoke(envfile, 'doctor', cwd=tmp_path)
    assert result.returncode in (0, 1), result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert 'Traceback' not in result.stderr
    assert set(report['model_resources']) == {'docling', 'rapidocr', 'embedding'}
    assert report['model_resources']['embedding'] == {'directory_exists': False, 'contains_files': False}
    assert isinstance(report['missing_modules'], list)
    assert report['directories']['data']['exists']

def test_occupied_port_does_not_launch_controller(tmp_path):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        envfile = _setup(tmp_path, listener.getsockname()[1])
        result = invoke(envfile, 'start', '--timeout', '10')
        assert result.returncode == 1
        assert 'port unavailable' in result.stdout.lower()
        assert not (envfile.parent / 'state' / 'runtime' / 'status.json').exists()

def test_lifecycle_four_roles_and_restart_preserves_sqlite(tmp_path, monkeypatch):
    if not all(__import__('importlib.util', fromlist=['find_spec']).find_spec(name)
               for name in ('uvicorn', 'psutil')):
        pytest.skip('Application runtime dependencies are required')
    envfile = _setup(tmp_path)
    with envfile.open('a', encoding='utf-8') as handle:
        handle.write('\nENABLE_SHARED_OLLAMA=0\nENABLE_VLLM=0\nENABLE_VLLM_EMBEDDINGS=0\n'
                     'ENABLE_TRANSLATION=0\nENABLE_IMPORT_TRANSLATION=0\nLARGE_QA_ENABLED=1\n')
    # Last duplicate is not used by Config's setdefault .env loader. Rewrite
    # existing backend enable lines instead of accidentally enabling a model.
    content = envfile.read_text(encoding='utf-8').replace('ENABLE_SHARED_OLLAMA=1', 'ENABLE_SHARED_OLLAMA=0')
    envfile.write_text(content, encoding='utf-8')
    # Inherited system temp paths may be absent or unwritable. Application
    # roles must use the instance directory before importing SQLite/parsers.
    inherited_temp = str(tmp_path / 'missing-system-temp')
    for name in ('TEMP', 'TMP', 'TMPDIR', 'SQLITE_TMPDIR'):
        monkeypatch.setenv(name, inherited_temp)
    old_ids = None
    try:
        started = invoke(envfile, 'start', '--timeout', '35', timeout=50, cwd=tmp_path)
        assert started.returncode == 0, started.stdout + started.stderr
        state = json.loads(started.stdout)
        assert state['ready']
        assert set(state['roles_alive']) == {'web', 'worker', 'large-worker', 'qa-worker', 'protection-helper', 'protection-executor'}
        assert state['desktop_protection']['helper']['running']
        assert state['desktop_protection']['executor']['healthy']
        assert all(state['roles_alive'].values())
        import psutil
        expected_temp = str(envfile.parent / 'state' / 'parse_temp')
        for row in state['roles'].values():
            child_env = psutil.Process(row['pid']).environ()
            assert all(child_env[name] == expected_temp for name in ('TEMP', 'TMP', 'TMPDIR', 'SQLITE_TMPDIR'))
        assert all(os.environ[name] == inherited_temp for name in ('TEMP', 'TMP', 'TMPDIR', 'SQLITE_TMPDIR'))
        old_ids = state['roles']
        second = invoke(envfile, 'start', '--timeout', '10')
        assert second.returncode == 0, second.stdout
        assert json.loads(second.stdout)['run_id'] == state['run_id']
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(state['url'] + '/login', timeout=5) as response:
            assert response.status == 200
        db = envfile.parent / 'state' / 'agent.db'
        assert db.is_file()
        import sqlite3
        with sqlite3.connect(db) as connection:
            connection.execute('CREATE TABLE platform_marker (value TEXT)')
            connection.execute("INSERT INTO platform_marker VALUES ('saved')")
        connection.close()
        stopped = invoke(envfile, 'stop', '--timeout', '25', timeout=35)
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        import psutil
        for row in old_ids.values():
            try:
                p = psutil.Process(row['pid'])
                assert abs(p.create_time() - row['created']) > .001 or p.status() == psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                pass
        restarted = invoke(envfile, 'start', '--timeout', '35', timeout=50)
        assert restarted.returncode == 0, restarted.stdout
        assert json.loads(restarted.stdout)['run_id'] != state['run_id']
        with sqlite3.connect(db) as connection:
            assert connection.execute('SELECT value FROM platform_marker').fetchone() == ('saved',)
        connection.close()
    finally:
        invoke(envfile, 'stop', '--timeout', '25', timeout=35)

def test_stale_pid_identity_never_stops_unrelated_process(tmp_path):
    import sjfxctl
    with subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)']) as child:
        try:
            assert not sjfxctl._same_process({'pid': child.pid, 'created': 0})
            assert child.poll() is None
        finally:
            child.terminate()
            child.wait(timeout=5)

def test_controller_crash_reaps_only_owned_roles(tmp_path):
    import psutil
    envfile = _setup(tmp_path)
    text = envfile.read_text(encoding='utf-8').replace('ENABLE_SHARED_OLLAMA=1', 'ENABLE_SHARED_OLLAMA=0')
    envfile.write_text(text + '\nENABLE_VLLM_EMBEDDINGS=0\nENABLE_TRANSLATION=0\nENABLE_IMPORT_TRANSLATION=0\n', encoding='utf-8')
    try:
        result = invoke(envfile, 'start', '--timeout', '35', timeout=50)
        assert result.returncode == 0, result.stdout
        state = json.loads(result.stdout)
        controller = psutil.Process(state['controller']['pid'])
        assert abs(controller.create_time() - state['controller']['created']) < .001
        controller.kill()
        deadline = time.monotonic() + 10
        import sjfxctl
        while any(sjfxctl._same_process(row) for row in state['roles'].values()) and time.monotonic() < deadline:
            time.sleep(.1)
        assert not any(sjfxctl._same_process(row) for row in state['roles'].values())
        restarted = invoke(envfile, 'start', '--timeout', '35', timeout=50)
        assert restarted.returncode == 0, restarted.stdout
    finally:
        invoke(envfile, 'stop', '--timeout', '25', timeout=35)

def test_disabled_models_are_not_probed(monkeypatch):
    import sjfxctl
    from types import SimpleNamespace
    config = SimpleNamespace(LLM_ENABLED=False, ENABLE_VLLM_EMBEDDINGS=False,
                             ENABLE_SHARED_OLLAMA_EMBEDDINGS=False,
                             MAIL_FEATURE_ENABLED=True, MAIL_ENABLE_VECTOR_INDEX=False)
    monkeypatch.setattr(sjfxctl, '_get_local_json', lambda *a, **kw: pytest.fail('disabled service was contacted'))
    assert all(not row['enabled'] for row in sjfxctl._model_health(config).values())

def test_storage_failure_during_shutdown_still_reaps_all_roles(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import config
    import sjfxctl
    from services import process_liveness
    cfg = SimpleNamespace(DATA_DIR=tmp_path, LOG_DIR=tmp_path, HOST='127.0.0.1',
                          PORT=18000, EXTRA_BIND_HOSTS=(), LARGE_QA_ENABLED=True)
    monkeypatch.setattr(config, 'ensure_runtime_directories', lambda: None)
    monkeypatch.setattr(sjfxctl, '_dependency_check', lambda _: [])
    monkeypatch.setattr(sjfxctl, '_ports_available', lambda _: None)
    monkeypatch.setattr(sjfxctl, '_web_ready', lambda _: True)
    monkeypatch.setattr(sjfxctl, '_identity', lambda _: 1.0)
    def publish(path, state):
        if state['phase'] in {'ready', 'stopping', 'failed'}:
            raise OSError('injected disk full')
    monkeypatch.setattr(sjfxctl, '_atomic_json', publish)
    reaped = []
    class Log:
        def close(self):
            raise PermissionError('injected log cleanup failure')
    def spawn(role, config, runtime, token, command=None):
        gate = runtime / (role + '.gate')
        gate.with_suffix('.ready').write_text(token, encoding='ascii')
        process = SimpleNamespace(pid=len(reaped) + 42, poll=lambda: None, role=role, wait=lambda **kw: None)
        return process, None, Log(), gate
    monkeypatch.setattr(sjfxctl, '_spawn_role', spawn)
    from sjfx_protection import desktop_runtime
    monkeypatch.setattr(desktop_runtime, 'prepare_environment', lambda _: {'core_state': str(tmp_path), 'stop_file': str(tmp_path/'helper.stop')})
    monkeypatch.setattr(desktop_runtime, 'helper_command', lambda _: [])
    monkeypatch.setattr(desktop_runtime, 'helper_health', lambda _: {})
    monkeypatch.setattr(desktop_runtime, 'health_report', lambda *_: {})
    monkeypatch.setattr(process_liveness, 'terminate_process_tree', lambda process, **kwargs: reaped.append(process.role))
    with pytest.raises(OSError, match='disk full'):
        sjfxctl.supervise(SimpleNamespace(run_id='test-failure', timeout=10), cfg)
    assert reaped == ['qa-worker', 'large-worker', 'worker', 'web', 'protection-executor', 'protection-helper']
