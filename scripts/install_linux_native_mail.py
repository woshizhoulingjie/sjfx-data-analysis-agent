"""Build private Ubuntu 24.04 x64 PST/MSG dependencies in a fresh directory.

Downloads/extracts debs, creates one independent venv, and writes an inventory.
Never invokes apt install, upgrades packages, or edits the referenced app venv.
The application site-packages reference is read-only and only written in the
new environment's .pth file. No production configuration is changed.
"""
from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time

PACKAGE_VERSION = '0.6.76-1.1build1'
PST_UTILS_SHA256 = 'aadfc35b93b4aa3a0659a016191f83821d9eee5fc3aa8a9887833a212332b7b5'
PACKAGES = ('pst-utils', 'libpst4t64')
WRAPPER = '''#!/bin/sh
set -eu
bundle_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
LD_LIBRARY_PATH="$bundle_root/usr/lib/x86_64-linux-gnu:$bundle_root/usr/lib:$bundle_root/lib/x86_64-linux-gnu:$bundle_root/lib"
export LD_LIBRARY_PATH
exec "$bundle_root/usr/bin/readpst" "$@"
'''


def checksum(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def run(command, log_dir, name, *, cwd=None, env=None, timeout=600):
    """No shell and no caller-controlled install command; retain bounded logs."""
    with (log_dir / (name + '.log')).open('wb') as log:
        result = subprocess.run(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
    text = (log_dir / (name + '.log')).read_text(encoding='utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError('{} failed ({}); see {}'.format(name, result.returncode,
                           log_dir / (name + '.log')))
    return text


def _stanzas(value):
    for block in value.strip().split('\n\n'):
        fields = {}
        for line in block.splitlines():
            if line and not line[0].isspace() and ': ' in line:
                key, field = line.split(': ', 1)
                fields[key] = field
        if fields:
            yield fields


def validate_host():
    if sys.platform != 'linux' or platform.machine().lower() not in ('x86_64', 'amd64'):
        raise RuntimeError('Requires Linux x86_64; do not run against a copied Windows environment')
    fields = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines()
                  if '=' in line and not line.startswith('#'))
    if fields.get('ID', '').strip('"') != 'ubuntu' or fields.get('VERSION_ID', '').strip('"') != '24.04':
        raise RuntimeError('This pinned package bundle is for Ubuntu 24.04 only')
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError('Use a Linux Python 3.12 interpreter')
    for executable in ('apt-cache', 'apt-get', 'dpkg-deb', 'ldd'):
        if shutil.which(executable) is None:
            raise RuntimeError('Missing build tool: ' + executable)


def inspect_application(application_python):
    result = subprocess.run([str(application_python), '-I', '-B', '-c',
        'import json,sys,sysconfig; print(json.dumps({"python":list(sys.version_info[:2]),'
        '"prefix":sys.prefix,"base_prefix":sys.base_prefix,"purelib":sysconfig.get_path("purelib"),'
        '"platlib":sysconfig.get_path("platlib")}))'], stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding='utf-8', timeout=30)
    if result.returncode:
        raise RuntimeError('Could not query the referenced application interpreter read-only')
    app = json.loads(result.stdout)
    if app['python'] != [3, 12] or app['prefix'] == app['base_prefix']:
        raise RuntimeError('--application-python must reference an existing Linux Python 3.12 venv')
    prefix = Path(app['prefix']).resolve()
    for raw in (app['purelib'], app['platlib']):
        path = Path(raw).resolve()
        if not path.is_dir() or prefix not in path.parents:
            raise RuntimeError('Application site-packages must exist inside its venv')
        if '\n' in str(path) or '\r' in str(path):
            raise ValueError('Application site-packages path cannot contain newlines')
    return app


def prepare_root(raw_root, application_python, *, application_prefix=None):
    if not raw_root.is_absolute() or not application_python.is_absolute():
        raise ValueError('--root and --application-python must be absolute paths')
    if raw_root.is_symlink():
        raise ValueError('--root cannot be a symlink')
    root = raw_root.resolve()
    protected = tuple(Path(p) for p in ('/bin', '/sbin', '/usr', '/lib', '/lib64', '/etc', '/var/lib'))
    if root == Path('/') or any(root == p or p in root.parents for p in protected):
        raise ValueError('--root must be a fresh private directory outside system directories')
    # A venv's bin/python is commonly a symlink to the system interpreter.
    # Check the lexical venv location as well as its resolved executable.
    app_location = application_python.parent.parent.absolute()
    if root == app_location or app_location in root.parents:
        raise ValueError('--root must be outside the referenced application environment')
    if application_prefix:
        actual_prefix = Path(application_prefix).resolve()
        if root == actual_prefix or actual_prefix in root.parents:
            raise ValueError('--root must be outside the actual application environment')
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError('--root must be absent or empty; existing contents are never overwritten')
    root.mkdir(parents=True, exist_ok=True)
    return root


def _native_library_environment(bundle):
    env = os.environ.copy()
    env['LD_LIBRARY_PATH'] = ':'.join(str(bundle / p) for p in (
        'usr/lib/x86_64-linux-gnu', 'usr/lib', 'lib/x86_64-linux-gnu', 'lib'))
    return env


def build_readpst(root, logs, manifest, timeout):
    downloads = root / 'downloads'
    downloads.mkdir()
    bundle = root
    for name in PACKAGES:
        spec = name + ':amd64=' + PACKAGE_VERSION
        metadata = run(['apt-cache', 'show', spec], logs, 'apt-cache-' + name, timeout=timeout)
        record = next((row for row in _stanzas(metadata) if row.get('Package') == name
                       and row.get('Version') == PACKAGE_VERSION and row.get('Architecture') == 'amd64'), None)
        if record is None or not record.get('SHA256'):
            raise RuntimeError('Pinned apt metadata is unavailable for ' + spec)
        expected = record['SHA256']
        if name == 'pst-utils' and expected != PST_UTILS_SHA256:
            raise RuntimeError('pst-utils SHA256 differs from the reviewed Ubuntu package')
        run(['apt-get', 'download', spec], logs, 'download-' + name, cwd=downloads, timeout=timeout)
        candidates = tuple(downloads.glob(name + '_*.deb'))
        if len(candidates) != 1:
            raise RuntimeError('Expected exactly one downloaded deb for ' + name)
        deb = candidates[0]
        actual = checksum(deb)
        if actual != expected:
            raise RuntimeError('Downloaded package checksum mismatch: ' + name)
        identity = run(['dpkg-deb', '-f', str(deb), 'Package', 'Version', 'Architecture'],
                       logs, 'identity-' + name, timeout=timeout)
        values = dict((line.split(':', 1)[0], line.split(':', 1)[1].strip())
                      for line in identity.splitlines() if ':' in line)
        if values != {'Package': name, 'Version': PACKAGE_VERSION, 'Architecture': 'amd64'}:
            raise RuntimeError('Downloaded package identity mismatch: ' + name)
        run(['dpkg-deb', '-x', str(deb), str(bundle)], logs, 'extract-' + name, timeout=timeout)
        license_file = bundle / 'usr/share/doc' / name / 'copyright'
        if not license_file.is_file():
            raise RuntimeError('Missing Debian copyright file: ' + name)
        manifest['apt_packages'].append({'package': name, 'version': PACKAGE_VERSION,
            'architecture': 'amd64', 'deb': str(deb.relative_to(root)), 'sha256': actual,
            'expected_sha256_source': 'reviewed constant' if name == 'pst-utils' else 'existing apt-cache metadata',
            'apt_filename': record.get('Filename'), 'depends': record.get('Depends'),
            'license': str(license_file.relative_to(root)), 'license_sha256': checksum(license_file)})
    binary = bundle / 'usr/bin/readpst'
    if not binary.is_file():
        raise RuntimeError('Downloaded pst-utils does not contain usr/bin/readpst')
    wrapper = bundle / 'bin/readpst'
    wrapper.parent.mkdir()
    wrapper.write_text(WRAPPER, encoding='utf-8', newline='\n')
    wrapper.chmod(0o755)
    environment = _native_library_environment(bundle)
    ldd = run(['ldd', str(binary)], logs, 'readpst-ldd', env=environment, timeout=timeout)
    if 'not found' in ldd:
        raise RuntimeError('Private readpst has unresolved libraries; see readpst-ldd.log; no system install attempted')
    version = run([str(wrapper), '-V'], logs, 'readpst-version', timeout=timeout)
    manifest['readpst'] = {'wrapper': str(wrapper.relative_to(root)), 'binary_sha256': checksum(binary),
        'wrapper_sha256': checksum(wrapper), 'version_output': version.strip(),
        'ldd_log': 'logs/readpst-ldd.log', 'verification': 'binary version and dynamic-library resolution',
        'pst_content_parsing_tested': False}


def build_msg_environment(root, logs, manifest, application_python, app, msg_version, timeout):
    (logs / 'application-python-read-only.log').write_text(json.dumps(app, indent=2), encoding='utf-8')
    app_prefix = Path(app['prefix']).resolve()
    env_root = root / 'msg-venv'
    if root == app_prefix or app_prefix in root.parents:
        raise RuntimeError('Private root is inside the referenced application environment')
    app_sites = tuple(dict.fromkeys((app['purelib'], app['platlib'])))
    for raw in app_sites:
        path = Path(raw).resolve()
        if not path.is_dir() or app_prefix not in path.parents:
            raise RuntimeError('Application site-packages must be an existing directory inside its environment')
        if '\n' in str(path) or '\r' in str(path):
            raise ValueError('Application site-packages path cannot contain newlines')
    # venv creation reads the existing interpreter; every resulting file is
    # created under the new root. No invocation of production pip occurs.
    run([str(application_python), '-I', '-B', '-m', 'venv', str(env_root)],
        logs, 'create-msg-venv', timeout=timeout)
    python = env_root / 'bin/python'
    pip_env = {k: v for k, v in os.environ.items() if not k.startswith('PIP_')
               and k not in ('PYTHONPATH', 'PYTHONHOME')}
    pip_env.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1',
                   PIP_CONFIG_FILE=os.devnull, PIP_REQUIRE_VIRTUALENV='1')
    # Install before adding the app .pth, and ignore externally installed
    # distributions. pip cannot select or uninstall production dependencies.
    report = root / 'pip-install-report.json'
    run([str(python), '-I', '-B', '-m', 'pip', '--disable-pip-version-check', '--no-cache-dir',
         'install', '--ignore-installed', '--index-url', 'https://pypi.org/simple',
         '--report', str(report), 'extract-msg==' + msg_version],
        logs, 'install-extract-msg-private', env=pip_env, timeout=timeout)
    site = Path(run([str(python), '-I', '-B', '-c',
        'import sysconfig; print(sysconfig.get_path("purelib"))'],
        logs, 'private-site-packages', timeout=timeout).strip())
    if env_root.resolve() not in site.resolve().parents:
        raise RuntimeError('New interpreter site-packages is outside its private venv')
    bridge = site / 'sjfx_application_read_only.pth'
    bridge.write_text('\n'.join(app_sites) + '\n', encoding='utf-8')
    smoke = run([str(python), '-I', '-B', '-c',
        'import extract_msg,importlib.metadata,json,sys; print(json.dumps({'
        '"version":importlib.metadata.version("extract-msg"),"module":extract_msg.__file__,'
        '"prefix":sys.prefix}))'], logs, 'extract-msg-import', env=pip_env, timeout=timeout)
    verified = json.loads(smoke)
    if env_root.resolve() not in Path(verified['module']).resolve().parents:
        raise RuntimeError('extract_msg was unexpectedly resolved outside the private environment')
    freeze = run([str(python), '-I', '-B', '-m', 'pip', '--disable-pip-version-check',
                  'freeze', '--local'], logs, 'msg-venv-freeze', env=pip_env, timeout=timeout)
    licenses = root / 'licenses/python'
    licenses.mkdir(parents=True)
    distributions = []
    for info in sorted(site.glob('*.dist-info')):
        copied = []
        for source in sorted(info.rglob('*')):
            if source.is_file() and ('licenses' in source.relative_to(info).parts or
                                     source.name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE'))):
                destination = licenses / info.name / source.relative_to(info)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                copied.append({'path': str(destination.relative_to(root)), 'sha256': checksum(destination)})
        metadata = info / 'METADATA'
        license_metadata = Parser().parsestr(metadata.read_text(encoding='utf-8', errors='replace'))
        metadata_copy = licenses / info.name / 'METADATA'
        metadata_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(metadata, metadata_copy)
        distributions.append({'dist_info': info.name, 'metadata_sha256': checksum(metadata),
            'metadata_copy': str(metadata_copy.relative_to(root)),
            'license_expression': license_metadata.get('License-Expression'),
            'license_metadata': license_metadata.get('License'),
            'license_classifiers': [v for v in license_metadata.get_all('Classifier', [])
                                    if v.startswith('License ::')], 'license_files': copied})
    manifest['extract_msg'] = {'python': str(python.relative_to(root)), 'requested_version': msg_version,
        'import_probe': verified, 'verification': 'module import only', 'msg_content_parsing_tested': False,
        'application_python_read_only': str(application_python), 'application_site_packages_read_only': app_sites,
        'bridge': str(bridge.relative_to(root)), 'pip_report': report.name,
        'pip_report_sha256': checksum(report), 'local_freeze': freeze.splitlines(),
        'distribution_inventory': distributions}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='Fresh absolute private output directory')
    parser.add_argument('--application-python', type=Path, required=True,
                        help='Existing Linux app venv interpreter; queried read-only')
    parser.add_argument('--extract-msg-version', default='0.56.1')
    parser.add_argument('--command-timeout', type=int, default=600,
                        help='Maximum seconds for each download/build command (1–1800)')
    args = parser.parse_args()
    if not 1 <= args.command_timeout <= 1800:
        parser.error('--command-timeout must be 1–1800')
    if not args.extract_msg_version or any(c not in '0123456789.' for c in args.extract_msg_version):
        parser.error('--extract-msg-version must be an explicit numeric release')
    try:
        validate_host()
        app = inspect_application(args.application_python)
        root = prepare_root(args.root, args.application_python, application_prefix=app['prefix'])
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        parser.error(str(exc))
    logs = root / 'logs'
    logs.mkdir()
    manifest = {'platform': platform.platform(), 'python': platform.python_version(),
        'root': str(root), 'created_at_unix': time.time(), 'apt_packages': [], 'ok': False,
        'production_configuration_modified': False, 'application_environment_modified': False,
        'functional_parsing_verified': False, 'scope': 'isolated dependencies and availability probes'}
    try:
        build_readpst(root, logs, manifest, args.command_timeout)
        build_msg_environment(root, logs, manifest, args.application_python, app,
                              args.extract_msg_version, args.command_timeout)
        manifest['ok'] = True
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        manifest['error'] = str(exc)
    finally:
        (root / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'ok': manifest['ok'], 'manifest': str(root / 'manifest.json'),
                      'production_configuration_modified': False, 'application_environment_modified': False}))
    return 0 if manifest['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
