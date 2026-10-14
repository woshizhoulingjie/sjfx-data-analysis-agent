"""Reproduce platform regressions with an already prepared Python 3.12 runtime.

No packages, model engines or weights are installed by this script.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SUITES = (
    'tests/test_platform_config_paths.py', 'tests/test_platform_launcher.py',
    'tests/test_platform_process_regression.py', 'tests/test_process_liveness.py',
    'tests/test_process_pid_safety.py', 'tests/test_qa_extraction_platform.py',
    'tests/test_startup_contract.py', 'tests/test_deployment_readiness.py',
    'tests/test_native_tool_paths.py',
    'tests/test_parser_runtime_locations.py', 'tests/test_readonly_resource_paths.py',
    'tests/test_requirements_split.py', 'tests/mail/test_attachment_formats.py',
    'tests/test_offline_translation_loading.py', 'tests/test_quickmt_output_recovery.py',
    'tests/test_quickmt_provider.py',
    'tests/test_translation_checkpoint_storage.py', 'tests/test_translation_progress.py',
    'tests/test_file_translation_backend_selection.py',
    'tests/mail/test_person_activity_context_capacity.py',
    'tests/mail/test_person_activity_adaptive_batch.py',
    'tests/test_catalog_batch.py',
    'tests/test_model_context_decoupling.py', 'tests/test_evidence_prefetch.py',
    'tests/test_conversation_small_context.py', 'tests/test_evidence_extract_verification.py',
    'sjfx_protection/tests',
    'tests/test_archive_formats.py', 'tests/test_legacy_import.py',
    'tests/mail/test_thai_ocr_platform.py', 'tests/mail/test_mail_ocr_compatibility.py',
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--full', action='store_true', help='Run the complete Python suite')
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        parser.error('Platform acceptance requires Python 3.12; choose its interpreter explicitly')
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    packages = {}
    for name in ('pytest', 'fastapi', 'docling', 'rapidocr', 'onnxruntime', 'torch',
                 'ctranslate2', 'py7zr', 'rarfile', 'pythainlp'):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    command = [sys.executable, '-X', 'utf8', '-m', 'pytest', '-q', '-ra',
               '--junitxml=' + str(output / 'regression.xml'),
               *(('tests', 'sjfx_protection/tests') if args.full else SUITES)]
    evidence = {'platform': platform.platform(), 'architecture': platform.machine(),
                'python': platform.python_version(), 'executable': sys.executable,
                'source': str(ROOT), 'command': command, 'packages': packages,
                'scope': 'complete Python suite' if args.full else 'platform regression suites',
                'functional_inference_verified': False}
    with tempfile.TemporaryDirectory(prefix='sjfx-platform-regression-', dir=output) as scratch:
        instance = Path(scratch)
        parse_temp = instance / 'scratch'
        parse_temp.mkdir()
        empty_env = instance / 'isolated.env'
        empty_env.write_text('', encoding='utf-8')
        env = {**os.environ, 'SJFX_ENV_FILE': str(empty_env),
               'SJFX_STATE_DIR': str(instance / 'state'), 'SJFX_OUTPUT_DIR': str(instance / 'outputs'),
               'SJFX_PARSE_TEMP_DIR': str(parse_temp),
               **{name: str(parse_temp) for name in ('TEMP', 'TMP', 'TMPDIR', 'SQLITE_TMPDIR')},
               'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'PYTHONUTF8': '1',
               'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1'}
        with (output / 'regression.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
            for line in process.stdout:
                print(line, end='', flush=True)
                log.write(line)
                log.flush()
            code = process.wait()
    evidence['exit_code'] = code
    (output / 'platform-info.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                                              encoding='utf-8')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
