"""Resolve instance-bundled native tools before a declared PATH fallback."""
import os
from pathlib import Path
import shutil
import sys
import subprocess
import importlib.util


def msg_interpreter(config=None):
    """Select an explicit MSG-only Python without a silent fallback."""
    from config import Config, resolve_config_path
    config = config or Config
    configured = os.getenv('SJFX_MSG_PYTHON', '').strip()
    if not configured:
        return sys.executable
    if len(configured)>=2 and configured[0]==configured[-1] and configured[0] in {'"', "'"}:
        configured=configured[1:-1]
    base_dir=getattr(config,'BASE_DIR',Config.BASE_DIR)
    # A venv/bin/python symlink must keep its invocation name: resolving its
    # final component selects the base interpreter and loses the venv packages.
    resolve_config_path(configured,base_dir=base_dir)  # platform/path validation
    path=Path(configured).expanduser()
    return str(resolve_config_path(path.parent,base_dir=base_dir)/path.name)


def msg_parser_probe(config=None, module_probe=None):
    """Report dependency availability; this never opens or converts a MSG."""
    explicit = bool(os.getenv('SJFX_MSG_PYTHON', '').strip())
    result = {'dependency': 'extract-msg', 'content_parsing_tested': False,
              'explicit_interpreter': explicit,
              'verification': 'interpreter_import_probe' if explicit else 'module_presence'}
    try:
        python = msg_interpreter(config)
        result['python'] = python
    except (OSError, RuntimeError, ValueError):
        return {**result, 'ok': False, 'code': 'invalid_msg_interpreter'}
    if not explicit:
        try:
            present = module_probe('extract_msg') if module_probe else importlib.util.find_spec('extract_msg') is not None
        except (ImportError, ValueError, AttributeError):
            present = False
        return {**result, 'ok': bool(present)}
    binary = Path(python)
    if not binary.is_file() or (os.name != 'nt' and not os.access(binary, os.X_OK)):
        return {**result, 'ok': False, 'code': 'missing_msg_interpreter'}
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    try:
        process = subprocess.run([python, '-I', '-B', '-c', 'import extract_msg'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment,
            timeout=8, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0)
    except subprocess.TimeoutExpired:
        return {**result, 'ok': False, 'code': 'msg_import_timeout'}
    except OSError:
        return {**result, 'ok': False, 'code': 'msg_interpreter_launch_failed'}
    return {**result, 'ok': process.returncode == 0,
            **({'code': 'msg_import_failed'} if process.returncode else {})}


def readpst_executable(config=None):
    if config is None:
        from config import Config
        config = Config
    root = Path(config.TOOLS_DIR)
    names = ('readpst.exe',) if os.name == 'nt' else ('readpst',)
    for bundle in ('mail-native', 'libpst', 'readpst', ''):
        for directory in (root / bundle / 'bin', root / bundle / 'usr' / 'bin', root / bundle):
            for name in names:
                path = directory / name
                if path.is_file() and (os.name == 'nt' or os.access(path, os.X_OK)):
                    return str(path)
    return next((path for name in names if (path := shutil.which(name))), None)


def readpst_environment(executable, config=None, environ=None):
    """Give only this native child its bundle's Linux shared-library paths.

    Both wrapper and raw usr/bin layouts are supported. System PATH fallbacks
    keep their existing environment; private libraries never affect Python.
    """
    environment = dict(os.environ if environ is None else environ)
    if os.name == 'nt' or not executable:
        return environment
    if config is None:
        from config import Config
        config = Config
    root = Path(config.TOOLS_DIR).resolve()
    binary = Path(executable).absolute()
    for bundle in ('mail-native', 'libpst', 'readpst', ''):
        private_root = root / bundle
        if binary.parent not in (private_root / 'bin', private_root / 'usr' / 'bin', private_root):
            continue
        libraries = [str(path) for path in (
            private_root / 'usr' / 'lib' / 'x86_64-linux-gnu',
            private_root / 'usr' / 'lib64', private_root / 'usr' / 'lib',
            private_root / 'lib64', private_root / 'lib',
        ) if path.is_dir()]
        if libraries:
            inherited = environment.get('LD_LIBRARY_PATH')
            environment['LD_LIBRARY_PATH'] = os.pathsep.join(libraries + ([inherited] if inherited else []))
        break
    return environment
