"""Reviewed public defaults cover Linux setup, installation and upgrades."""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_PROFILE_ENV_SHA256 = '7d381fe841d4a5c26603401f3bc451881df3e649896ea528d622936aceba0206'


def _builder():
    spec = importlib.util.spec_from_file_location(
        'linux_profile_test_builder', ROOT / 'packaging/build_offline_bundle.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parse(text):
    return dict(line.split('=', 1) for line in text.splitlines()
                if line and not line.startswith('#') and '=' in line)


class LinuxBusinessProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='sjfx-linux-profile-test-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_profile_matches_reviewed_public_contract(self):
        builder = _builder()
        defaults = builder.LINUX_BUSINESS_ENV_DEFAULTS
        # Environment mapping from the independent audit file with SHA256
        # 157d91e529e7eac5fda3dc4ba4aea0c8b7725cbd05b485e4311ae70ee7bc1478.
        # Its canonical environment digest is independent of this implementation.
        canonical = json.dumps(defaults, ensure_ascii=False, sort_keys=True,
                               separators=(',', ':')).encode('utf-8')
        self.assertEqual(len(defaults), 287)
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), PUBLIC_PROFILE_ENV_SHA256)
        forbidden = {'HOST', 'PORT', 'AUTH_REQUIRED', 'SCAN_ALLOWED_ROOTS', 'LLM_BACKEND',
                     'SJFX_API_ACCESS_TOKEN', 'SJFX_MODEL_ALLOWED_ENDPOINTS',
                     'SJFX_DESKTOP_ACCESS_AUDIT', 'SJFX_MANAGED_EMBEDDING',
                     'LLM_CONTEXT_TOKENS', 'LLM_MAX_CONCURRENCY', 'SESSION_HOURS_DEFAULT'}
        self.assertFalse(forbidden.intersection(defaults))
        for key in defaults:
            self.assertFalse(key.endswith(('_API_KEY', '_BASE_URL', '_MODEL_PATH', '_TOKENIZER_PATH')))
            self.assertFalse(key.startswith(('SJFX_PROTECTION_', 'SJFX_STATE_', 'SJFX_OUTPUT_')))
        self.assertEqual(_parse(builder.linux_business_defaults_block()), defaults)

    def test_linux_template_sets_business_behavior_and_keeps_platform_settings(self):
        builder = _builder()
        builder._write_templates(self.directory, 'linux-x64', ROOT / 'packaging')
        raw = (self.directory / 'config/sjfx.env.template').read_bytes()
        self.assertNotIn(b'\r', raw)
        text = raw.decode('utf-8')
        self.assertEqual(text.count(builder.LINUX_BUSINESS_DEFAULTS_BEGIN), 1)
        self.assertEqual(text.count(builder.LINUX_BUSINESS_DEFAULTS_END), 1)
        values = _parse(text)
        self.assertEqual({key: values[key] for key in builder.LINUX_BUSINESS_ENV_DEFAULTS},
                         builder.LINUX_BUSINESS_ENV_DEFAULTS)
        for key, value in {'HOST': '127.0.0.1', 'SCAN_ALLOWED_ROOTS': '__DATA_DIR__/outputs/_uploads',
                           'AUTH_REQUIRED': '1', 'ENABLE_OPENAI_COMPATIBLE': '0',
                           'SJFX_MANAGED_EMBEDDING': '1',
                           'SJFX_EMBED_BASE_URL': 'http://127.0.0.1:18083/v1'}.items():
            self.assertEqual(values[key], value)
        self.assertNotIn('SJFX_API_ACCESS_TOKEN', values)
        self.assertEqual((self.directory / 'install_linux.sh').read_bytes(),
                         (ROOT / 'packaging/install_linux.sh').read_bytes())
        self.assertEqual((self.directory / 'INSTALL.md').read_bytes(),
                         (ROOT / 'docs/offline-installation.md').read_bytes())

    def test_linux_profile_does_not_change_windows_template(self):
        builder = _builder()
        builder._write_templates(self.directory, 'windows-x64', ROOT / 'packaging')
        text = (self.directory / 'config/sjfx.env.template').read_text(encoding='utf-8')
        self.assertNotIn(builder.LINUX_BUSINESS_DEFAULTS_BEGIN, text)
        self.assertNotIn('PRELIMINARY_SUMMARY_OPTIMIZATION_ENABLED', _parse(text))

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux setup only')
    def test_linux_cli_setup_uses_profile_and_never_overwrites_existing_instance(self):
        import sjfxctl
        import sjfx_protection.desktop_runtime as desktop_runtime
        env = self.directory / 'instance.env'
        args = argparse.Namespace(
            env_file=str(env), data_dir=str(self.directory / 'state'),
            output_dir=str(self.directory / 'outputs'), models_dir=str(self.directory / 'models'),
            tools_dir=str(self.directory / 'tools'), parse_temp_dir=str(self.directory / 'parse'),
            helper_binary=None, clam_database=None, clam_binary=None, clam_certs=None,
            backend='openai_compatible', port=18077, helper_port=18767)
        with mock.patch.object(desktop_runtime, 'protect_private_path'), mock.patch.object(sjfxctl, '_emit'):
            sjfxctl.setup(args)
            before = env.read_bytes()
            values = _parse(before.decode('utf-8'))
            defaults = _builder().LINUX_BUSINESS_ENV_DEFAULTS
            self.assertEqual({key: values[key] for key in defaults}, defaults)
            self.assertEqual(values['PORT'], '18077')
            self.assertEqual(values['ENABLE_OPENAI_COMPATIBLE'], '1')
            self.assertGreaterEqual(len(values['SJFX_API_ACCESS_TOKEN']), 48)
            with self.assertRaisesRegex(RuntimeError, 'already exists'):
                sjfxctl.setup(args)
            self.assertEqual(env.read_bytes(), before)

    def test_upgrade_fills_missing_defaults_preserves_explicit_values_and_is_idempotent(self):
        builder = _builder()
        template = self.directory / 'template'
        template.write_text('SJFX_OFFLINE_INSTALLATION=1\n' + builder.linux_business_defaults_block(), encoding='utf-8')
        target = self.directory / 'instance.env'
        preserved = ('SJFX_LP_CATALOG_REQUESTS=37\n'
                     'LLM_BACKEND=ollama\nOLLAMA_BASE_URL=http://127.0.0.1:19222\n'
                     'SJFX_MANAGED_EMBEDDING=0\nPORT=19876\n'
                     'SCAN_ALLOWED_ROOTS=/srv/example\nSJFX_API_ACCESS_TOKEN=synthetic-only\n')
        target.write_text(preserved, encoding='utf-8')
        installer = (ROOT / 'packaging/install_linux.sh').read_text(encoding='utf-8')
        section = installer.split('# Fill missing local runtime and reviewed business defaults on upgrade.', 1)[1]
        code = section.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        ast.parse(code)
        arguments = [sys.executable, '-B', '-', str(target), str(template),
                     str(self.directory / 'data'), str(self.directory / 'program')]
        subprocess.run(arguments, input=code, text=True, check=True, capture_output=True)
        once = target.read_bytes()
        self.assertTrue(once.startswith(preserved.encode('utf-8')))
        values = _parse(once.decode('utf-8'))
        self.assertEqual(values['SJFX_LP_CATALOG_REQUESTS'], '37')
        for key, value in builder.LINUX_BUSINESS_ENV_DEFAULTS.items():
            if key != 'SJFX_LP_CATALOG_REQUESTS':
                self.assertEqual(values[key], value)
        subprocess.run(arguments, input=code, text=True, check=True, capture_output=True)
        self.assertEqual(target.read_bytes(), once)

    def test_profile_replaces_duplicate_template_keys_without_touching_other_settings(self):
        builder = _builder()
        key = next(iter(builder.LINUX_BUSINESS_ENV_DEFAULTS))
        source = 'HOST=localhost\n' + key + '=old\n' + key + '=duplicate\n'
        updated = builder.apply_linux_business_defaults(source)
        self.assertEqual(updated.count(key + '='), 1)
        self.assertEqual(_parse(updated)[key], builder.LINUX_BUSINESS_ENV_DEFAULTS[key])
        self.assertEqual(_parse(updated)['HOST'], 'localhost')
        self.assertEqual(builder.apply_linux_business_defaults(updated), updated)


if __name__ == '__main__':
    unittest.main()
