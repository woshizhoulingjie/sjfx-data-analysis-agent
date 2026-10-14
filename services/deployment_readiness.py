"""Offline deployment prerequisites; presence is never an inference result.

This module deliberately does not import model runtimes or load any weights.
Only disposable directory writes and bounded native-tool help probes execute.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

from services.native_tools import readpst_executable, readpst_environment, msg_parser_probe

PARSER_MODULES = ('docling', 'torch', 'transformers', 'rapidocr', 'onnxruntime', 'PIL', 'pypdfium2',
                  'cv2', 'sklearn', 'xlrd', 'striprtf', 'olefile', 'py7zr',
                  'rarfile', 'pythainlp')
RAPID_MODELS = ('ch_PP-OCRv5_det_mobile.onnx', 'ch_PP-OCRv5_rec_mobile.onnx',
                'ch_ppocr_mobile_v2.0_cls_mobile.onnx')


def module_present(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def file_check(path, minimum_bytes=1, *, json_object=False):
    path = Path(path)
    row = {'path': str(path), 'verification': 'presence', 'minimum_bytes': minimum_bytes,
           'ok': False}
    try:
        row['size_bytes'] = path.stat().st_size if path.is_file() else 0
        if row['size_bytes'] < minimum_bytes:
            row['code'] = 'missing_or_incomplete_file'
            return row
        with path.open('rb') as handle:
            handle.read(1)
        if json_object and not isinstance(json.loads(path.read_text(encoding='utf-8')), dict):
            raise ValueError('JSON object required')
        row['ok'] = True
    except (OSError, ValueError, UnicodeError) as exc:
        row['code'] = type(exc).__name__
    return row


def resource_group(files, *, required=True, reason=''):
    return {'required': required, 'verification': 'presence', 'ok': all(r['ok'] for r in files),
            'inference_tested': False, 'reason': reason, 'files': files}


def directory_check(path, *, writable):
    path = Path(path)
    row = {'path': str(path), 'exists': path.exists(), 'required': writable,
           'verification': 'write_probe' if writable else 'presence', 'ok': False}
    try:
        ancestor = path
        while not ancestor.exists() and ancestor.parent != ancestor:
            ancestor = ancestor.parent
        row.update(parent_writable=os.access(ancestor, os.W_OK),
                   free_bytes=shutil.disk_usage(ancestor).free)
        if path.exists() and not path.is_dir():
            raise NotADirectoryError(str(path))
        if writable:
            # Probe without leaving configured directories/files behind. For an
            # absent directory also exercise creation of its missing depth.
            with tempfile.TemporaryDirectory(prefix='.sjfx-doctor-', dir=ancestor) as scratch:
                target = Path(scratch)
                if not path.exists():
                    target = target.joinpath(*path.relative_to(ancestor).parts)
                    target.mkdir(parents=True)
                probe = target / 'write-test'
                probe.write_bytes(b'SJFX directory write probe')
                if probe.read_bytes() != b'SJFX directory write probe':
                    raise OSError('write/read mismatch')
                probe.replace(target / 'replace-test')
            row['ok'] = True
        else:
            row['ok'] = path.is_dir() and os.access(path, os.R_OK)
    except OSError as exc:
        row['code'] = type(exc).__name__
    return row


def _find_tool(config, name):
    roots = (Path(config.TOOLS_DIR) / 'legacy-office' / 'usr',
             Path(config.TOOLS_DIR) / 'legacy-office', Path(config.TOOLS_DIR))
    names = (name + '.exe',) if os.name == 'nt' else (name,)
    for root in roots:
        for directory in (root / 'bin', root):
            for filename in names:
                path = directory / filename
                if path.is_file():
                    return path, root
    return next(((Path(p), None) for n in names if (p := shutil.which(n))), (None, None))


def _rar_executable(config):
    # Use the parser's resolver with a private settings carrier: doctor must
    # not mutate the already-imported rarfile backend selection.
    try:
        import rarfile
    except ImportError:
        return None
    from services.archive_formats import configure_rar_backend
    selected = configure_rar_backend(SimpleNamespace(SEVENZIP_TOOL=rarfile.SEVENZIP_TOOL), config=config)
    return selected if selected and Path(selected).is_file() else shutil.which(selected) if selected else None


def tool_probe(binary, args, *, required, env=None, required_lines=()):
    row = {'required': required, 'path': str(binary) if binary else None,
           'verification': 'execution_probe', 'ok': False, 'content_parsing_tested': False}
    if binary is None:
        row['code'] = 'missing_executable'
        return row
    try:
        # Store tool output privately, bounded by this small diagnostic command.
        # No source filename or shell command is accepted from user input.
        with tempfile.TemporaryFile() as output:
            completed = subprocess.run([str(binary), *args], stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, env=env, timeout=5,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0)
            output.seek(0)
            diagnostic = output.read(65536).decode('utf-8', errors='replace')
        row['returncode'] = completed.returncode
        # antiword/catppt use exit 1 for usage on some distributions. A loader
        # error is never a successful execution probe, even with visible text.
        loader_error = any(s in diagnostic.lower() for s in
            ('error while loading shared libraries', 'permission denied', 'cannot execute'))
        row['ok'] = completed.returncode in (0, 1) and bool(diagnostic.strip()) and not loader_error
        if required_lines:
            row['missing_required_lines'] = sorted(set(required_lines) -
                                                   {line.strip() for line in diagnostic.splitlines()})
            row['ok'] &= completed.returncode == 0 and not row['missing_required_lines']
        if not row['ok']:
            row['code'] = 'tool_probe_failed'
    except (OSError, subprocess.TimeoutExpired) as exc:
        row['code'] = type(exc).__name__
    return row


def translation_resources(config, provider=None):
    enabled = getattr(config, 'ENABLE_TRANSLATION', True)
    provider = provider or getattr(config, 'TRANSLATION_PROVIDER', 'offline_nllb')
    if enabled and provider == 'offline_quickmt':
        root = Path(config.TRANSLATION_QUICKMT_MODEL_DIR)
        files = [file_check(root / 'resource_manifest.json', json_object=True)]
        for pair in ('en-zh', 'zh-en', 'th-en'):
            directory = root / pair
            files.extend([file_check(directory / 'model.bin', 100 * 1024 * 1024),
                          file_check(directory / 'config.json', json_object=True),
                          file_check(directory / 'resource_manifest.json', json_object=True),
                          file_check(directory / 'src.spm.model'), file_check(directory / 'tgt.spm.model'),
                          file_check(directory / 'source_vocabulary.json'),
                          file_check(directory / 'target_vocabulary.json')])
        dependencies = {name: module_present(name) for name in ('ctranslate2', 'sentencepiece')}
        group = resource_group(files, reason='QuickMT CPU trial: English/Chinese and Thai via English')
        return {**group, 'ok': group['ok'] and all(dependencies.values()), 'provider': provider,
                'backend': 'ctranslate2', 'path': str(root), 'enabled': True,
                'cpu_only': True, 'dependencies': dependencies}
    offline = enabled and provider == 'offline_nllb' 
    root = Path(config.TRANSLATION_MODEL_PATH)
    ct2 = Path(config.TRANSLATION_CT2_MODEL_PATH)
    # Match app.py selection exactly; a partially copied CT2 model must fail
    # this check instead of silently certifying the unused Transformers folder.
    selected_ct2 = (getattr(config, 'TRANSLATION_PREFER_CT2', True)
                    and module_present('ctranslate2') and (ct2 / 'model.bin').is_file())
    if selected_ct2:
        root = ct2
    if enabled and provider not in ('offline_nllb', 'ollama'):
        return {'required': True, 'ok': False, 'verification': 'configuration',
                'provider': provider, 'enabled': True, 'inference_tested': False,
                'reason': 'unsupported enabled translation provider', 'code': 'invalid_translation_provider'}
    if not offline:
        return {'required': False, 'ok': True, 'verification': 'configuration',
                'provider': provider, 'enabled': enabled, 'inference_tested': False,
                'reason': 'offline NLLB is not selected'}
    files = [file_check(root / 'config.json', json_object=True),
             file_check(root / 'tokenizer_config.json', json_object=True)]
    tokenizer = next((root / n for n in ('tokenizer.json', 'sentencepiece.bpe.model')
                      if (root / n).is_file()), root / 'tokenizer.json')
    files.append(file_check(tokenizer, json_object=tokenizer.suffix == '.json'))
    if selected_ct2 or (root / 'model.bin').is_file():
        files.append(file_check(root / 'model.bin', 1024 * 1024))
        vocabulary = next((root / n for n in ('shared_vocabulary.json', 'shared_vocabulary.txt')
                           if (root / n).is_file()), None)
        if vocabulary:
            files.append(file_check(vocabulary))
        else:
            for stem in ('source_vocabulary', 'target_vocabulary'):
                path = next((root / (stem + ext) for ext in ('.json', '.txt')
                             if (root / (stem + ext)).is_file()), root / (stem + '.json'))
                files.append(file_check(path))
        backend = 'ctranslate2'
    else:
        backend = 'transformers'
        weight = next((root / n for n in ('model.safetensors', 'pytorch_model.bin')
                       if (root / n).is_file()), None)
        if weight:
            files.append(file_check(weight, 100 * 1024 * 1024))
        else:
            index = next((root / n for n in ('model.safetensors.index.json', 'pytorch_model.bin.index.json')
                          if (root / n).is_file()), root / 'model.safetensors.index.json')
            index_row = file_check(index, json_object=True)
            files.append(index_row)
            if index_row['ok']:
                try:
                    shards = set(json.loads(index.read_text(encoding='utf-8'))['weight_map'].values())
                    if not shards:
                        raise ValueError('empty weights')
                    for shard in sorted(shards):
                        if not isinstance(shard, str) or Path(shard).name != shard:
                            raise ValueError('invalid shard path')
                        files.append(file_check(root / shard, 1024 * 1024))
                except (KeyError, TypeError, ValueError):
                    index_row.update(ok=False, code='invalid_weight_index')
    return {**resource_group(files, reason='selected offline translation backend'),
            'provider': provider, 'backend': backend, 'path': str(root), 'enabled': True}


def deployment_report(config, runtime_modules, model_health, *, scope='full', required_features=()):
    if scope not in ('runtime', 'full'):
        raise ValueError('scope must be runtime or full')
    if any(f not in ('msg', 'pst') for f in required_features):
        raise ValueError('required features must be msg or pst')
    dirs = {name: directory_check(path, writable=writable) for name, path, writable in (
        ('data', config.DATA_DIR, True), ('outputs', config.OUTPUT_DIR, True),
        ('parse_temp', config.PARSE_TEMP_DIR, True), ('logs', config.LOG_DIR, True),
        ('models', config.MODELS_DIR, False), ('tools', config.TOOLS_DIR, False))}
    runtime_missing = [n for n in runtime_modules if not module_present(n)]
    runtime_ready = not runtime_missing and all(r['ok'] for r in dirs.values() if r['required'])
    root = Path(config.DOCLING_ARTIFACTS_DIR)
    layout = root / 'docling-project--docling-layout-heron'
    resources = {
        'docling_layout': resource_group([file_check(layout / 'config.json', json_object=True),
                                         file_check(layout / 'model.safetensors', 150 * 1024 * 1024)],
                                         reason='ordinary PDF/image parsing is enabled by default'),
        'rapidocr': resource_group([file_check(Path(config.RAPIDOCR_MODEL_DIR) / n, 65536)
                                   for n in RAPID_MODELS], reason='ordinary image/scanned PDF parsing'),
        'translation': translation_resources(config),
    }
    file_backend = getattr(config, 'TRANSLATION_FILE_BACKEND', 'vllm')
    if (getattr(config, 'ENABLE_TRANSLATION', True)
            and file_backend in ('offline_nllb', 'offline_quickmt')
            and file_backend != getattr(config, 'TRANSLATION_PROVIDER', 'offline_nllb')):
        resources['file_translation'] = translation_resources(config, provider=file_backend)
    mail_enabled = getattr(config, 'MAIL_FEATURE_ENABLED', True)
    rec_mode = os.getenv('MAIL_OCR_REC_MODEL', 'auto').strip().lower()
    server_rec = Path(config.RAPIDOCR_MODEL_DIR) / 'ch_PP-OCRv5_rec_server.onnx'
    if mail_enabled and (rec_mode == 'server' or rec_mode == 'auto' and server_rec.is_file()):
        resources['mail_rapidocr_server'] = resource_group([file_check(server_rec, 65536)],
            reason='mail OCR selects the local server recognition model')
    if mail_enabled and rec_mode not in ('auto', 'mobile', 'server'):
        resources['mail_ocr_configuration'] = {'required': True, 'ok': False,
            'verification': 'configuration', 'code': 'invalid_MAIL_OCR_REC_MODEL'}
    tables = root / 'docling-project--docling-models' / 'model_artifacts' / 'tableformer'
    table_profiles = {mode: resource_group([
        file_check(tables / mode / filename, size * 1024 * 1024),
        file_check(tables / mode / 'tm_config.json', json_object=True)]) for mode, filename, size in (
            ('accurate', 'tableformer_accurate.safetensors', 100),
            ('fast', 'tableformer_fast.safetensors', 80))}
    resources['docling_tables'] = {'required': True, 'verification': 'presence',
        'ok': any(r['ok'] for r in table_profiles.values()), 'profiles': table_profiles,
        'inference_tested': False, 'reason': 'missing table weights degrades ordinary structured PDF extraction'}
    modules = list(PARSER_MODULES)
    for name in ('translation', 'file_translation'):
        translation = resources.get(name, {})
        if translation.get('required') and translation.get('backend'):
            modules.extend(('transformers', 'sentencepiece', translation['backend'] == 'ctranslate2'
                            and 'ctranslate2' or 'torch'))
    missing = list(dict.fromkeys(runtime_missing + [n for n in modules if not module_present(n)]))
    tools = {}
    if scope == 'full':
        for name in ('antiword', 'catppt'):
            binary, tool_root = _find_tool(config, name)
            env = os.environ.copy()
            if tool_root:
                env['ANTIWORDHOME'] = str(tool_root / 'share' / 'antiword')
                if os.name != 'nt' and (tool_root / 'lib').is_dir():
                    env['LD_LIBRARY_PATH'] = str(tool_root / 'lib') + (
                        ':' + env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
            tools[name] = tool_probe(binary, ['-h'], required=True, env=env)
        tools['7z'] = tool_probe(_rar_executable(config), ['i'], required=True)
        # Respect the same roots and complete tha+eng bundle as the mail parser.
        from services.mail.thai_ocr import _resolve_resources
        tess = _resolve_resources() if getattr(config, 'MAIL_FEATURE_ENABLED', True) else None
        language = os.getenv('MAIL_OCR_LANGUAGE', 'auto').strip().lower()
        language = {'ch': 'zh', 'chi_sim': 'zh', 'zh-cn': 'zh', 'th': 'tha', 'en': 'eng'}.get(language, language)
        thai_enabled = mail_enabled and language in ('auto', 'tha')
        if mail_enabled and language not in ('auto', 'zh', 'tha', 'eng'):
            resources['mail_ocr_language_configuration'] = {'required': True, 'ok': False,
                'verification': 'configuration', 'code': 'invalid_MAIL_OCR_LANGUAGE'}
        env = os.environ.copy()
        if tess:
            env['TESSDATA_PREFIX'] = str(tess[1])
            if os.name != 'nt' and tess[2]:
                env['LD_LIBRARY_PATH'] = ':'.join(map(str, tess[2])) + (
                    ':' + env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
        tools['tesseract_tha_eng'] = tool_probe(tess[0] if tess else None, ['--list-langs'],
                                              required=thai_enabled, env=env, required_lines=('tha', 'eng'))
        tools['tesseract_tha_eng']['reason'] = 'mail OCR language routing: ' + language
        if tess:
            tools['tesseract_tha_eng']['language_files'] = [file_check(tess[1] / n) for n in
                                                         ('tha.traineddata', 'eng.traineddata')]
            tools['tesseract_tha_eng']['ok'] &= all(r['ok'] for r in tools['tesseract_tha_eng']['language_files'])
    pst_binary = readpst_executable(config)
    optional = {
        'msg': {'required': 'msg' in required_features, **msg_parser_probe(config, module_present)},
        'pst': {'required': 'pst' in required_features, 'verification': 'executable_presence',
                'ok': pst_binary is not None, 'dependency': 'readpst',
                'path': pst_binary, 'content_parsing_tested': False},
    }
    if scope == 'full' and optional['pst']['required']:
        optional['pst'].update(tool_probe(pst_binary, ['-V'], required=True,
            env=readpst_environment(pst_binary, config)))
    endpoints_ready = all(r.get('reachable') and r.get('configured_model_present') is True
                          for r in model_health.values() if r.get('enabled'))
    full_ready = (runtime_ready and not missing and endpoints_ready
                  and all(r['ok'] for r in resources.values() if r['required'])
                  and bool(tools) and all(r['ok'] for r in tools.values() if r['required'])
                  and all(r['ok'] for r in optional.values() if r['required']))
    return {'scope': scope, 'verification_scope': 'deployment prerequisites; no OCR/translation/model inference',
            'missing_modules': missing, 'directories': dirs, 'resource_checks': resources,
            'native_tools': tools, 'optional_features': optional,
            'application_runtime_available': runtime_ready, 'runtime_missing_modules': runtime_missing,
            'full_dependencies_available': not missing, 'full_prerequisites_ready': full_ready,
            'complete_functional_readiness': None, 'functional_verification': 'not_tested',
            'local_model_services': model_health, 'model_services_prerequisites_ready': endpoints_ready,
            'ok': runtime_ready if scope == 'runtime' else full_ready}
