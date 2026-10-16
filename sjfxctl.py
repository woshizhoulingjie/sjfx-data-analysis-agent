"""Cross-platform lifecycle entry point for one SJFX instance.

Model engines remain external local services until the delivery hardware is
known. This launcher owns application, protection executor and local helper trees.
"""
from __future__ import annotations

import argparse
import errno
import importlib.util
import json
import os
from pathlib import Path
import platform
import secrets
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse
import urllib.request
import uuid
import webbrowser

# Installed application and runtime files are immutable; imports and child
# processes must keep bytecode caches out of the manifest-protected tree.
sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'

ROOT = Path(__file__).resolve().parent
RUNTIME_MODULES = ('fastapi', 'jinja2', 'uvicorn', 'pydantic', 'httpx', 'werkzeug',
                   'pypdf', 'docx', 'pptx', 'openpyxl', 'psutil', 'cryptography')

def _emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)

def _retry_windows_sharing(operation, *, allow_crt_eacces=False):
    deadline = time.monotonic() + 1.0
    while True:
        try:
            return operation()
        except PermissionError as exc:
            # Windows file readers and replacement briefly exclude each other.
            windows_error = getattr(exc, 'winerror', None)
            # CRT-backed io.open loses winerror and reports sharing locks as EACCES.
            sharing_error = windows_error in {5, 32, 33} or (
                allow_crt_eacces and windows_error is None and exc.errno == errno.EACCES)
            if os.name != 'nt' or not sharing_error:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(.025, remaining))

def _atomic_json(path, payload):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        os.chmod(temporary, 0o600)
        _retry_windows_sharing(lambda: os.replace(temporary, path))
    finally:
        temporary.unlink(missing_ok=True)

def _read_json(path):
    try:
        return json.loads(_retry_windows_sharing(lambda: path.read_text(encoding='utf-8'),
                                                allow_crt_eacces=True))
    except (FileNotFoundError, ValueError):
        return {}

def _load_config(args):
    if args.env_file:
        path = Path(args.env_file).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        os.environ['SJFX_ENV_FILE'] = str(path.resolve())
    from config import Config
    return Config

def _runtime(config):
    return config.DATA_DIR / 'runtime'

def _web_url(config):
    host = config.HOST
    if host in {'0.0.0.0', ''}:
        host = '127.0.0.1'
    elif host == '::':
        host = '::1'
    return 'http://{}:{}'.format('[' + host + ']' if ':' in host else host, config.PORT)

def _get_local_json(url, headers=None, timeout=2, web_host=None):
    if urlparse(url).hostname not in {'127.0.0.1', 'localhost', '::1', web_host}:
        raise ValueError('Health requests require a loopback endpoint')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers=headers or {})
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read(1024 * 1024))

def _web_ready(config):
    try:
        headers = {'X-SJFX-Token': config.API_ACCESS_TOKEN} if config.AUTH_REQUIRED else {}
        return bool(_get_local_json(_web_url(config) + '/api/status', headers, web_host=config.HOST))
    except Exception:
        return False

def _identity(pid):
    import psutil
    return psutil.Process(pid).create_time()

def _same_process(row):
    import psutil
    try:
        process = psutil.Process(int(row['pid']))
        return (abs(process.create_time() - float(row['created'])) < .001
                and process.is_running() and process.status() != psutil.STATUS_ZOMBIE)
    except (KeyError, ValueError, TypeError, psutil.Error):
        return False

def _status(config):
    state = _read_json(_runtime(config) / 'status.json')
    running = bool(state and _same_process(state.get('controller', {})))
    state['running'] = running
    if running:
        state['roles_alive'] = {name: _same_process(row) for name, row in state.get('roles', {}).items()}
        state['web_ready'] = _web_ready(config)
        state['ready'] = (state.get('phase') == 'ready' and state['web_ready']
                          and bool(state['roles_alive']) and all(state['roles_alive'].values()))
    else:
        state['ready'] = False
    state['application_ready'] = state['ready']
    from sjfx_protection.desktop_runtime import health_report
    state['desktop_protection'] = health_report(config)
    state['readiness_scope'] = 'initialized application roles and web; model health is reported separately'
    return state

def _ports_available(config):
    for host in (config.HOST,) + config.EXTRA_BIND_HOSTS:
        family = socket.AF_INET6 if ':' in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as listener:
            if os.name == 'nt':
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                # Match Uvicorn: a stopped listener's TIME_WAIT connections
                # must not be mistaken for a process still owning the port.
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind((host, config.PORT))
            except OSError as exc:
                raise RuntimeError('Web port unavailable: {}:{}'.format(host, config.PORT)) from exc
    from services.managed_embedding import selected
    if selected(config):
        with socket.socket() as listener:
            if os.name == 'nt':
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind(('127.0.0.1', config.MANAGED_EMBED_PORT))
            except OSError as exc:
                raise RuntimeError('Bundled embedding port unavailable: ' + str(config.MANAGED_EMBED_PORT)) from exc

def _dependency_check(modules):
    return [name for name in modules if importlib.util.find_spec(name) is None]

def _apply_linux_business_defaults(content):
    """Load public packaging defaults without importing instance configuration."""
    if not sys.platform.startswith('linux'):
        return content
    path = ROOT / 'packaging' / 'build_offline_bundle.py'
    spec = importlib.util.spec_from_file_location('sjfx_linux_business_defaults', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.apply_linux_business_defaults(content)


def setup(args):
    if not args.env_file:
        raise RuntimeError('setup requires --env-file; an existing configuration is never overwritten')
    if sys.version_info < (3, 10):
        raise RuntimeError('SJFX requires Python 3.10 or newer; delivery will use a fixed runtime')
    target = Path(args.env_file).expanduser().resolve()
    data = Path(args.data_dir).expanduser().resolve() if args.data_dir else target.parent / 'state'
    output = Path(args.output_dir).expanduser().resolve() if args.output_dir else target.parent / 'outputs'
    models = Path(args.models_dir).expanduser().resolve() if args.models_dir else target.parent / 'models'
    tools = Path(args.tools_dir).expanduser().resolve() if args.tools_dir else ROOT / '.tools'
    parse_temp = Path(args.parse_temp_dir).expanduser().resolve() if args.parse_temp_dir else data / 'parse_temp'
    optional_paths={name:Path(getattr(args,name)).expanduser().resolve() for name in ('helper_binary','clam_database','clam_binary','clam_certs') if getattr(args,name,None)}
    if target.exists():
        raise RuntimeError('Configuration already exists: ' + str(target))
    for path in (target, data, output, models, tools, parse_temp,*optional_paths.values()):
        if '\n' in str(path) or '\r' in str(path) or '"' in str(path):
            raise RuntimeError('Configuration paths cannot contain newline or double quote')
    for path in (target.parent, data, output, parse_temp):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Explicit model/tool roots are read-only inputs, and can be mounted later.
    # Preserve the convenient writable instance model folder for source users.
    if not args.models_dir:
        models.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_urlsafe(48)
    backend = args.backend or ('ollama' if os.name == 'nt' else 'vllm')
    content = '\n'.join([
        '# SJFX instance; absolute paths keep launch independent of cwd.',
        'SJFX_STATE_DIR="{}"'.format(data), 'SJFX_OUTPUT_DIR="{}"'.format(output),
        'SJFX_MODELS_DIR="{}"'.format(models),
        'SJFX_TOOLS_DIR="{}"'.format(tools),
        'SJFX_PARSE_TEMP_DIR="{}"'.format(parse_temp),
        'HF_HOME="{}"'.format(data / 'cache' / 'huggingface'),
        'SCAN_ALLOWED_ROOTS="{}"'.format(output / '_uploads'),
        'HOST=127.0.0.1', 'SJFX_EXTRA_BIND_HOSTS=', 'PORT={}'.format(args.port),
        'AUTH_REQUIRED=1', 'SJFX_API_ACCESS_TOKEN=' + token,
        'LLM_BACKEND=' + backend, 'ENABLE_VLLM=' + ('1' if backend == 'vllm' else '0'),
        'ENABLE_SHARED_OLLAMA=' + ('1' if backend == 'ollama' else '0'),
        'ENABLE_OPENAI_COMPATIBLE=' + ('1' if backend == 'openai_compatible' else '0'),
        *(['# Set these to the model already served by your local compatible service.',
           'OPENAI_COMPATIBLE_BASE_URL=http://127.0.0.1:8001/v1',
           'OPENAI_COMPATIBLE_MODEL=local-model',
           'OPENAI_COMPATIBLE_REQUEST_TIMEOUT=180',
           'OPENAI_COMPATIBLE_SUPPORTS_JSON_RESPONSE_FORMAT=0']
          if backend == 'openai_compatible' else []),
        'SJFX_LOCAL_AGENT_ENDPOINT=http://127.0.0.1:{}'.format(getattr(args,'helper_port',18767)),
        'SJFX_DESKTOP_ACCESS_AUDIT=1',
        *(['SJFX_DESKTOP_HELPER_BINARY="{}"'.format(optional_paths['helper_binary'])] if 'helper_binary' in optional_paths else []),
        *(['SJFX_PROTECTION_CLAM_DATABASE="{}"'.format(optional_paths['clam_database'])] if 'clam_database' in optional_paths else []),
        *(['SJFX_PROTECTION_CLAM="{}"'.format(optional_paths['clam_binary'])] if 'clam_binary' in optional_paths else []),
        *(['SJFX_PROTECTION_CLAM_CERTS="{}"'.format(optional_paths['clam_certs'])] if 'clam_certs' in optional_paths else []),
        'SJFX_PROTECTION_SCAN_WORKDIR="{}"'.format(data/'desktop_protection'/'scan-work'),
        'HF_HUB_OFFLINE=1', 'TRANSFORMERS_OFFLINE=1', '',
    ])
    content = _apply_linux_business_defaults(content)
    with target.open('x', encoding='utf-8', newline='\n') as handle:
        handle.write(content)
    os.chmod(target, 0o600)
    from sjfx_protection.desktop_runtime import protect_private_path
    protect_private_path(target)
    _emit({'configured': True, 'env_file': str(target), 'data_dir': str(data),
           'output_dir': str(output), 'models_dir': str(models), 'backend': backend,
           'next': 'Run doctor, then start with this --env-file. Model engines are separate local services.'})

def _model_health(config):
    from services.model_diagnostics import model_inventory
    return model_inventory(config)


def doctor(config, scope='full', required_features=()):
    from services.deployment_readiness import deployment_report
    model_health = _model_health(config)
    report = deployment_report(config, RUNTIME_MODULES, model_health,
                               scope=scope, required_features=required_features)
    report.update(platform=platform.system(), architecture=platform.machine(),
                  python=platform.python_version(), backend=config.LLM_BACKEND)
    from sjfx_protection.desktop_runtime import health_report
    from sjfx_protection.desktop_audit import preflight
    report['desktop_protection'] = health_report(config)
    from sjfx_protection.policy import file_protection_requested
    report['program_access_audit_preflight'] = (preflight() if file_protection_requested() else
        {'enabled':False,'status':'disabled','configuration_required':False,
         'healthy':False,'coverage_complete':False,'reason':'File protection is disabled by instance configuration'})
    from services.managed_embedding import resource_check
    if getattr(config, 'MANAGED_EMBEDDING', False):
        report['bundled_embedding'] = resource_check(config)
        if report['bundled_embedding']['selected'] and not report['bundled_embedding']['resource_complete']:
            report['ok'] = False
    # Compatibility metadata only. No readiness decision uses these fields.
    report['model_resources_scope'] = 'directory inventory only; see resource_checks for readiness'
    report['model_resources'] = {
        name: {'directory_exists': path.is_dir(),
               'contains_files': path.is_dir() and any(p.is_file() for p in path.rglob('*'))}
        for name, path in [('docling', Path(config.DOCLING_ARTIFACTS_DIR)),
                           ('rapidocr', Path(config.RAPIDOCR_MODEL_DIR)),
                           ('embedding', Path(config.EMBED_MODEL_PATH))]}
    _emit(report)
    return 0 if report['ok'] else 1


def model_config_command(args, config):
    from services.model_settings import get_model_settings, save_model_settings
    if args.config_action == 'get':
        _emit(get_model_settings(config))
        return 0
    patch = {}
    for name in ('backend', 'base_url', 'model', 'context_tokens', 'timeout_seconds',
                 'concurrency', 'dimension', 'version', 'weights_id', 'config_id', 'encoding', 'tokenizer_path'):
        value = getattr(args, name, None)
        if value is not None:
            patch[name] = value
    if args.enabled is not None:
        patch['enabled'] = args.enabled == 'true'
    if args.supports_json is not None:
        patch['supports_json_response_format'] = args.supports_json == 'true'
    if args.api_key_stdin:
        # Credentials are never accepted in argv or printed by this command.
        patch['api_key'] = sys.stdin.readline(4098).rstrip('\r\n')
    if args.clear_api_key:
        patch['clear_api_key'] = True
    if not patch:
        raise ValueError('config set requires at least one model field')
    _emit(save_model_settings(config, {args.role: patch}))
    return 0


def model_test_command(args, config):
    from services.model_diagnostics import test_models
    report = test_models(config, role=args.role, timeout_seconds=args.timeout)
    _emit(report)
    return 0 if report['ok'] else 1


def init_admin(args, config):
    """Set the first password via a private prompt or stdin, never argv/logs."""
    if _status(config).get('running'):
        raise RuntimeError('Stop this instance before initializing its administrator')
    from config import ensure_runtime_directories
    from sjfx_multiuser.users import UserStore
    ensure_runtime_directories()
    store = UserStore(str(config.DB_PATH))
    try:
        if store.user_count():
            raise RuntimeError('Existing accounts are preserved; init-admin only supports a fresh instance')
        if args.password_stdin:
            password = sys.stdin.readline(4098).rstrip('\r\n')
            if len(password) > 4096:
                raise ValueError('Password exceeds input limit')
        else:
            from getpass import getpass
            password = getpass('管理员密码: ')
            if password != getpass('再次输入: '):
                raise ValueError('两次密码不一致')
        store.validate_password(password)
        result = store.bootstrap_first_admin(owner_id=config.OWNER_ID, password=password)
        if not result:
            raise RuntimeError('Administrator was already initialized')
        _emit({'initialized': True, 'username': result['username'], 'password_logged': False,
               'must_change_password': True})
        return 0
    finally:
        store.close()

def _spawn_role(role, config, runtime, token, command=None):
    from services.process_liveness import own_process_tree, terminate_process_tree
    gate = runtime / (token + '-' + role + '.gate')
    env = os.environ.copy()
    env.update({'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1',
                'SJFX_PROCESS_GATE': str(gate), 'SJFX_PROCESS_TOKEN': token,
                'SJFX_ROLE_READY_FILE': str(gate.with_suffix('.ready')), 'SJFX_ROLE_TOKEN': token,
                'SJFX_CONTROLLER_PID': str(os.getpid()), 'SJFX_CONTROLLER_CREATED': str(_identity(os.getpid())),
                'TEMP': str(config.PARSE_TEMP_DIR), 'TMP': str(config.PARSE_TEMP_DIR),
                'TMPDIR': str(config.PARSE_TEMP_DIR), 'SQLITE_TMPDIR': str(config.PARSE_TEMP_DIR)})
    env['SJFX_ROLE'] = role
    if os.name == 'nt':
        # Go's OS configuration lookup requires APPDATA even when --data is
        # supplied. Keep all library fallback caches within this instance.
        for name, folder in (('APPDATA','appdata'), ('LOCALAPPDATA','localappdata')):
            path = config.DATA_DIR / 'cache' / folder
            path.mkdir(parents=True, exist_ok=True)
            env[name] = str(path)
    env['XDG_CACHE_HOME'] = str(config.DATA_DIR / 'cache')
    env['TORCH_HOME'] = str(config.DATA_DIR / 'cache' / 'torch')
    if role == 'embedding':
        env.update(SJFX_EMBED_PORT=str(config.MANAGED_EMBED_PORT),
                   SJFX_EMBED_MODEL_NAME='multilingual-e5-small')
    log = open(config.LOG_DIR / ('launcher-' + role + '.log'), 'ab', buffering=0)
    kwargs = {'start_new_session': True} if os.name != 'nt' else {'creationflags': subprocess.CREATE_NO_WINDOW}
    process = None
    owner = None
    try:
        process = subprocess.Popen(command or [sys.executable, '-B', '-X', 'utf8', '-m', 'services.managed_entry', role],
                                   cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, **kwargs)
        owner = own_process_tree(process.pid)
        if os.name == 'nt' and owner is None:
            raise RuntimeError('Cannot acquire Windows process ownership for ' + role)
        gate.write_text(token, encoding='ascii')
        return process, owner, log, gate
    except BaseException:
        if process is not None:
            terminate_process_tree(process, job=owner, grace_seconds=1, private_group=os.name != 'nt')
        gate.unlink(missing_ok=True)
        log.close()
        raise

def supervise(args, config):
    from config import ensure_runtime_directories
    from services.process_liveness import try_lock_file, terminate_process_tree
    missing = _dependency_check(RUNTIME_MODULES)
    if missing:
        raise RuntimeError('Missing application dependencies: ' + ', '.join(missing))
    ensure_runtime_directories()
    from sjfx_protection.desktop_runtime import prepare_environment,helper_command,helper_health,health_report
    from sjfx_protection.policy import file_protection_requested
    protection_paths = prepare_environment(config) if file_protection_requested() else None
    runtime = _runtime(config)
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    stop_path = runtime / 'stop.json'
    token = args.run_id or uuid.uuid4().hex
    children = {}
    stop = [False]
    with (runtime / 'controller.lock').open('a+b') as lock:
        if not try_lock_file(lock):
            raise RuntimeError('This SJFX instance already has a controller')
        _ports_available(config)
        stop_path.unlink(missing_ok=True)
        state = {'run_id': token, 'phase': 'starting', 'url': _web_url(config),
                 'controller': {'pid': os.getpid(), 'created': _identity(os.getpid())},
                 'roles': {}, 'logs': str(config.LOG_DIR), 'heartbeat': time.time()}
        def publish():
            state['heartbeat'] = time.time()
            _atomic_json(runtime / 'status.json', state)
        def requested():
            return stop[0] or _read_json(stop_path).get('run_id') == token
        for name in ('SIGTERM', 'SIGINT', 'SIGBREAK'):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), lambda *_: stop.__setitem__(0, True))
        publish()
        try:
            deadline = time.monotonic() + args.timeout
            if protection_paths is not None:
                children['protection-helper'] = _spawn_role('protection-helper',config,runtime,token,helper_command(protection_paths))
                process=children['protection-helper'][0]
                state['roles']['protection-helper']={'pid':process.pid,'created':_identity(process.pid)}
                while True:
                    if process.poll() is not None: raise RuntimeError('Protection helper exited; see launcher-protection-helper.log')
                    try:
                        helper_health(protection_paths)
                        break
                    except (OSError,ValueError,RuntimeError): pass
                    if requested(): return 0
                    if time.monotonic()>=deadline: raise RuntimeError('Protection helper startup timed out')
                    time.sleep(.2)
                children['protection-executor'] = _spawn_role('protection-executor',config,runtime,token,
                    [sys.executable,'-B','-X','utf8','-m','sjfx_protection.desktop_runtime','executor','--state',protection_paths['core_state']])
                process=children['protection-executor'][0]
                state['roles']['protection-executor']={'pid':process.pid,'created':_identity(process.pid)}
                ready=children['protection-executor'][3].with_suffix('.ready')
                while not (ready.is_file() and ready.read_text(encoding='ascii')==token):
                    if process.poll() is not None: raise RuntimeError('Protection executor exited; see launcher-protection-executor.log')
                    if requested(): return 0
                    if time.monotonic()>=deadline: raise RuntimeError('Protection executor startup timed out')
                    time.sleep(.1)
            state['desktop_protection']=health_report(config,protection_paths)
            from services.managed_embedding import selected, resource_check
            if selected(config):
                check = resource_check(config)
                if not check['resource_complete']:
                    raise RuntimeError('Bundled embedding resources are missing: ' + ', '.join(check['missing_files']))
                children['embedding'] = _spawn_role('embedding', config, runtime, token)
                process, _, _, gate = children['embedding']
                state['roles']['embedding'] = {'pid': process.pid, 'created': _identity(process.pid)}
                publish()
                ready = gate.with_suffix('.ready')
                while not (ready.is_file() and ready.read_text(encoding='ascii') == token):
                    if process.poll() is not None:
                        raise RuntimeError('Bundled embedding exited; see launcher-embedding.log')
                    if requested():
                        return 0
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Bundled embedding startup timed out; see launcher-embedding.log')
                    time.sleep(.2)
                state['bundled_embedding'] = check
            children['web'] = _spawn_role('web', config, runtime, token)
            state['roles']['web'] = {'pid': children['web'][0].pid, 'created': _identity(children['web'][0].pid)}
            publish()
            deadline = time.monotonic() + args.timeout
            while not _web_ready(config):
                if children['web'][0].poll() is not None:
                    raise RuntimeError('Web exited during startup; see launcher-web.log')
                if requested():
                    return 0
                if time.monotonic() >= deadline:
                    raise RuntimeError('Web startup timed out; see launcher-web.log')
                time.sleep(.25)
            roles = ['worker', 'large-worker']
            if config.LARGE_QA_ENABLED:
                roles.append('qa-worker')
            for role in roles:
                children[role] = _spawn_role(role, config, runtime, token)
                state['roles'][role] = {'pid': children[role][0].pid, 'created': _identity(children[role][0].pid)}
                publish()
            while True:
                initialized = True
                for role, (process, _, _, gate) in children.items():
                    if process.poll() is not None:
                        raise RuntimeError(role + ' exited during startup; see launcher-' + role + '.log')
                    if role not in {'web','protection-helper'}:
                        ready = gate.with_suffix('.ready')
                        initialized = initialized and ready.is_file() and ready.read_text(encoding='ascii') == token
                if initialized:
                    break
                if requested():
                    return 0
                if time.monotonic() >= deadline:
                    raise RuntimeError('Role initialization timed out; see launcher logs')
                time.sleep(.1)
            state['phase'] = 'ready'
            publish()
            while not requested():
                for role, (process, _, _, _) in children.items():
                    if process.poll() is not None:
                        raise RuntimeError(role + ' exited; all owned roles will be stopped')
                publish()
                time.sleep(.5)
            return 0
        except BaseException as exc:
            state['error'] = str(exc)
            raise
        finally:
            state['phase'] = 'stopping'
            try:
                publish()
            except OSError:
                pass  # Cleanup must run even when state storage is full.
            failures = []
            state['protection_history_preserved']=True
            if protection_paths is not None:
                try:
                    health=helper_health(protection_paths)
                    state['pending_dispositions_at_stop']=health.get('active_dispositions',0)
                except (OSError,ValueError,RuntimeError): pass
            for role in reversed(list(children)):
                process, owner, log, gate = children[role]
                try:
                    if role=='protection-helper':
                        Path(protection_paths['stop_file']).write_text(token,encoding='ascii')
                        try: process.wait(timeout=4)
                        except subprocess.TimeoutExpired: pass
                    terminate_process_tree(process, job=owner, grace_seconds=3, private_group=os.name != 'nt')
                except Exception as exc:
                    failures.append(role + ': ' + type(exc).__name__)
                finally:
                    for cleanup in (log.close, lambda: gate.unlink(missing_ok=True),
                                    lambda: gate.with_suffix('.ready').unlink(missing_ok=True)):
                        try:
                            cleanup()
                        except OSError as exc:
                            failures.append(role + ' cleanup: ' + type(exc).__name__)
            state['phase'] = 'failed' if state.get('error') or failures else 'stopped'
            if failures:
                state['cleanup_errors'] = failures
            try:
                publish()
            except OSError:
                pass
            try:
                stop_path.unlink(missing_ok=True)
            except OSError:
                pass

def start(args, config):
    from config import ensure_runtime_directories
    current = _status(config)
    if current.get('running'):
        current['local_model_services'] = _model_health(config)
        _emit(current)
        return 0 if current['ready'] else 1
    missing = _dependency_check(RUNTIME_MODULES)
    if missing:
        raise RuntimeError('Missing application dependencies: ' + ', '.join(missing))
    _ports_available(config)
    ensure_runtime_directories()
    run_id = uuid.uuid4().hex
    command = [sys.executable, '-B', '-X', 'utf8', str(ROOT / 'sjfxctl.py')]
    if args.env_file:
        command += ['--env-file', str(Path(args.env_file).expanduser().resolve())]
    command += ['run', '--run-id', run_id, '--timeout', str(args.timeout)]
    kwargs = {'start_new_session': True} if os.name != 'nt' else {'creationflags': subprocess.CREATE_NO_WINDOW}
    with (config.LOG_DIR / 'launcher-controller.log').open('ab') as log:
        process = subprocess.Popen(command, cwd=ROOT, env=os.environ.copy(), stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, **kwargs)
    deadline = time.monotonic() + args.timeout + 5
    while time.monotonic() < deadline:
        state = _status(config)
        if state.get('run_id') == run_id and state.get('ready'):
            state['local_model_services'] = _model_health(config)
            _emit(state)
            if args.open_browser:
                webbrowser.open(state['url'])
            return 0
        if process.poll() is not None:
            raise RuntimeError('Controller exited; see ' + str(config.LOG_DIR / 'launcher-controller.log'))
        time.sleep(.25)
    _atomic_json(_runtime(config) / 'stop.json', {'run_id': run_id})
    raise RuntimeError('Startup timed out; stop requested. See launcher logs.')

def stop(config, timeout):
    state = _status(config)
    if not state.get('running'):
        _emit({'running': False, 'phase': state.get('phase', 'not_started')})
        return 0
    _atomic_json(_runtime(config) / 'stop.json', {'run_id': state['run_id']})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        latest = _status(config)
        if not latest.get('running'):
            _emit(latest)
            return 1 if latest.get('cleanup_errors') else 0
        time.sleep(.25)
    raise RuntimeError('Stop is still in progress; inspect status and launcher logs')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', help='Explicit instance config; CLI relative path uses current directory')
    sub = parser.add_subparsers(dest='command', required=True)
    install = sub.add_parser('setup', help='Write a fresh instance config; never overwrite existing files')
    install.add_argument('--data-dir')
    install.add_argument('--output-dir')
    install.add_argument('--models-dir')
    install.add_argument('--tools-dir', help='Read-only parser tool resource root')
    install.add_argument('--parse-temp-dir', help='Writable temporary directory outside application/resources')
    install.add_argument('--helper-port',type=int,default=18767,choices=range(1024,65536),metavar='PORT')
    install.add_argument('--helper-binary',help='Installed native Windows helper executable')
    install.add_argument('--clam-database',help='Already installed ClamAV database; never downloaded by launcher')
    install.add_argument('--clam-binary',help='Installed ClamAV clamscan executable')
    install.add_argument('--clam-certs',help='Installed ClamAV 1.5 CVD certificate directory')
    install.add_argument('--backend', choices=('vllm', 'ollama', 'openai_compatible'))
    install.add_argument('--port', type=int, default=18000, choices=range(1024, 65536), metavar='PORT')
    for name in ('start', 'run', 'stop'):
        command = sub.add_parser(name)
        command.add_argument('--timeout', type=float, default=90 if name != 'stop' else 30)
        # Launchers append user flags after the implicit command. Accepting
        # --env-file here keeps both `sjfxctl.py --env-file X start` and
        # `start_sjfx --env-file X` equivalent.
        command.add_argument('--env-file', dest='env_file', default=argparse.SUPPRESS,
                             help=argparse.SUPPRESS)
        if name == 'start':
            command.add_argument('--open-browser', action='store_true')
        if name == 'run':
            command.add_argument('--run-id', help=argparse.SUPPRESS)
    sub.add_parser('status')
    admin = sub.add_parser('init-admin', help='Initialize a fresh administrator without logging its password')
    admin.add_argument('--password-stdin', action='store_true', help='Read a private stdin line; default uses hidden interactive prompts')
    check = sub.add_parser('doctor', help='Check deployment prerequisites; does not certify functional inference')
    check.add_argument('--scope', choices=('runtime', 'full'), default='full',
                       help='runtime: modules and writable directories; full (default): configured resources/tools/endpoints')
    check.add_argument('--require-feature', choices=('msg', 'pst'), action='append', default=[],
                       help='Require an optional mail format dependency in full scope; may be repeated')
    model_test = sub.add_parser('model-test', help='Perform bounded local chat, structured output and embedding inference probes')
    model_test.add_argument('--role', choices=('all', 'generation', 'embedding'), default='all')
    model_test.add_argument('--timeout', type=float, default=20, help='Total diagnostic time, 1–60 seconds')
    model_config = sub.add_parser('config', help='Read or persist this instance model settings; restart all roles to apply')
    config_sub = model_config.add_subparsers(dest='config_action', required=True)
    config_sub.add_parser('get', help='Show active and saved model settings; credentials remain private')
    config_set = config_sub.add_parser('set')
    config_set.add_argument('--role', choices=('generation', 'embedding'), required=True)
    for field in ('backend', 'base-url', 'model', 'version', 'weights-id', 'config-id', 'encoding', 'tokenizer-path'):
        config_set.add_argument('--' + field)
    for field in ('context-tokens', 'timeout-seconds', 'concurrency', 'dimension'):
        config_set.add_argument('--' + field, type=int)
    config_set.add_argument('--enabled', choices=('true', 'false'))
    config_set.add_argument('--supports-json', choices=('true', 'false'))
    secrets_input = config_set.add_mutually_exclusive_group()
    secrets_input.add_argument('--api-key-stdin', action='store_true', help='Read write-only credential from one stdin line, never argv')
    secrets_input.add_argument('--clear-api-key', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'setup':
            setup(args)
            return 0
        config = _load_config(args)
        if args.command == 'init-admin':
            return init_admin(args, config)
        if args.command == 'config':
            return model_config_command(args, config)
        if args.command == 'model-test':
            return model_test_command(args, config)
        if args.command == 'doctor':
            return doctor(config, args.scope, args.require_feature)
        if args.command == 'status':
            state = _status(config)
            state['local_model_services'] = _model_health(config)
            _emit(state)
            return 0 if state.get('ready') else 1
        if args.command == 'start':
            return start(args, config)
        if args.command == 'stop':
            return stop(config, args.timeout)
        return supervise(args, config)
    except (RuntimeError, OSError, ImportError, ValueError) as exc:
        _emit({'ok': False, 'error': str(exc)})
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
