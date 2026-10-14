"""Small boundary tests for the deployable provider, without large models."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from services import quickmt_translation as module

provider_path = Path(module.__file__)
Provider = module.OfflineQuickMTProvider
Error = module.TranslationProviderError


class FakeSP:
    protos = []
    def __init__(self, *, model_proto):
        self.protos.append(model_proto)
        self.tag = model_proto.decode()
    def encode(self, text, out_type=int):
        return text.split() if out_type is str else [1] * max(1, len(text.split()))
    def eos_id(self):
        return 0
    def is_control(self, index):
        return index == 0
    def decode(self, value):
        return ' '.join(str(item) for item in value)


class FakeCT2:
    paths = []
    active = maximum = 0
    lock = threading.Lock()
    def __init__(self, directory, **kwargs):
        self.paths.append(directory)
        self.device = kwargs['device']
        self.compute_type = 'int8_float32'
    def translate_batch(self, encoded, **kwargs):
        with self.lock:
            type(self).active += 1
            type(self).maximum = max(self.maximum, self.active)
        try:
            time.sleep(0.005)
            return [types.SimpleNamespace(hypotheses=[['译文']]) for _ in encoded]
        finally:
            with self.lock:
                type(self).active -= 1


class ProviderBoundaryTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='QuickMT 中文 路径_')
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        models = {}
        for pair in ('en-zh', 'zh-en', 'th-en'):
            directory = self.root / pair
            directory.mkdir()
            payloads = {'model.bin': b'fake-weights', 'src.spm.model': ('source-' + pair).encode(),
                        'tgt.spm.model': ('target-' + pair).encode(), 'config.json': b'{}',
                        'source_vocabulary.json': b'[]', 'target_vocabulary.json': b'[]'}
            entries = []
            for name, content in payloads.items():
                (directory / name).write_bytes(content)
                entries.append({'name': name, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
            model_hash = hashlib.sha256(payloads['model.bin']).hexdigest()
            manifest = {'commit': 'fixed-' + pair, 'actual_files': entries}
            (directory / 'resource_manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            models[pair] = {'commit': manifest['commit'], 'actual_model_sha256': model_hash}
        (self.root / 'resource_manifest.json').write_text(json.dumps({'models': models}), encoding='utf-8')

    def native_modules(self):
        return patch.dict(sys.modules, {
            'sentencepiece': types.SimpleNamespace(SentencePieceProcessor=FakeSP),
            'ctranslate2': types.SimpleNamespace(Translator=FakeCT2),
        })

    def test_import_identity_is_lazy_and_never_reads_weights(self):
        import builtins
        original_import = builtins.__import__
        original_read = Path.read_bytes
        def guarded_import(name, *args, **kwargs):
            if name.split('.')[0] in ('ctranslate2', 'sentencepiece', 'torch', 'transformers'):
                raise AssertionError('native import during Web initialization: ' + name)
            return original_import(name, *args, **kwargs)
        def guarded_read(path):
            if path.name == 'model.bin':
                raise AssertionError('provider_id scanned weights')
            return original_read(path)
        with patch('builtins.__import__', side_effect=guarded_import), patch.object(Path, 'read_bytes', guarded_read):
            fresh_spec = importlib.util.spec_from_file_location('quickmt_lazy_test', provider_path)
            fresh = importlib.util.module_from_spec(fresh_spec)
            fresh_spec.loader.exec_module(fresh)
            provider = fresh.OfflineQuickMTProvider(self.root)
            first = provider.provider_id
            self.assertEqual(first, provider.provider_id)
            self.assertEqual({}, provider.pairs)

    def test_unicode_path_tokenizer_proto_and_protected_layout(self):
        FakeSP.protos.clear()
        FakeCT2.paths.clear()
        with self.native_modules():
            provider = Provider(self.root)
            response = provider.translate('The purchase is approved.\r\n__SJFX_KEEP_0001__', 'en')
        self.assertEqual('译文\r\n__SJFX_KEEP_0001__', response.text)
        self.assertIn(b'source-en-zh', FakeSP.protos)
        self.assertIn(str(self.root / 'en-zh'), FakeCT2.paths)
        self.assertEqual('int8_float32', response.metadata['compute_type'])

    def test_cache_identity_isolates_settings_and_changed_resources(self):
        base = Provider(self.root).provider_id
        self.assertNotEqual('offline_nllb:600m:ct2-int8', base)
        for change in ({'beam_size': 10}, {'cpu_threads': 8}, {'batch_size': 4},
                       {'max_input_tokens': 2048}, {'max_new_tokens': 1024}):
            self.assertNotEqual(base, Provider(self.root, **change).provider_id)
        model = self.root / 'en-zh/model.bin'
        stat = model.stat()
        import os
        existing = Provider(self.root)
        self.assertEqual(base, existing.provider_id)
        os.utime(model, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        self.assertNotEqual(base, Provider(self.root).provider_id)
        with self.assertRaises(Error) as changed:
            _ = existing.provider_id
        self.assertEqual('provider_unavailable', changed.exception.code)
        # A changed tokenizer with a corresponding deployment manifest gets
        # its own identity, even with the same model revision.
        source = self.root / 'en-zh/src.spm.model'
        source.write_bytes(b'changed-source')
        manifest_path = self.root / 'en-zh/resource_manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        for entry in manifest['actual_files']:
            if entry['name'] == source.name:
                entry.update(size=source.stat().st_size, sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        self.assertNotEqual(base, Provider(self.root).provider_id)

    def test_unsupported_language_does_not_import_or_fallback(self):
        provider = Provider(self.root)
        for source, target in (('el', 'zh-CN'), ('en', 'fr'), ('mixed', 'en')):
            with self.assertRaises(Error) as caught:
                provider.translate('test', source, target)
            self.assertEqual('unsupported_language', caught.exception.code)
            self.assertFalse(caught.exception.retryable)
        self.assertEqual({}, provider.pairs)

    def test_missing_model_and_bad_small_hash_fail_explicitly(self):
        model = self.root / 'en-zh/model.bin'
        model.rename(self.root / 'en-zh/model.saved')
        with self.assertRaises(Error) as caught:
            Provider(self.root).translate('The purchase is approved.', 'en')
        self.assertEqual('provider_unavailable', caught.exception.code)
        self.assertFalse(caught.exception.retryable)
        (self.root / 'en-zh/model.saved').rename(model)
        (self.root / 'en-zh/config.json').write_bytes(b'{"changed":true}')
        with self.assertRaises(Error) as corrupt:
            Provider(self.root).translate('The purchase is approved.', 'en')
        self.assertEqual('provider_unavailable', corrupt.exception.code)

    def test_chinese_passthrough_mixed_routes_and_concurrent_calls(self):
        provider = Provider(self.root)
        text = '现有中文\r\n__SJFX_KEEP_0001__'
        response = provider.translate(text, 'zh-CN', 'zh-CN')
        self.assertEqual(text, response.text)
        self.assertEqual({}, provider.pairs)
        self.assertEqual(text, provider.translate(text, 'mixed').text)
        with self.assertRaises(Error) as unsupported:
            provider.translate('中文 Καλημέρα', 'mixed')
        self.assertEqual('unsupported_language', unsupported.exception.code)
        FakeCT2.maximum = FakeCT2.active = 0
        with self.native_modules(), ThreadPoolExecutor(max_workers=4) as pool:
            jobs = [pool.submit(provider.translate, 'The purchase is approved.', 'en'),
                    pool.submit(provider.translate, '采购尚未批准。', 'zh', 'en'),
                    pool.submit(provider.translate, '现有中文\nThe purchase is approved.', 'mixed'),
                    pool.submit(provider.translate, 'คณะกรรมการยังไม่อนุมัติ', 'th')]
            outputs = [job.result() for job in jobs]
        self.assertEqual(1, FakeCT2.maximum)
        self.assertEqual(['en-zh'], outputs[0].metadata['route'])
        self.assertEqual(['zh-en'], outputs[1].metadata['route'])
        self.assertEqual('现有中文\n译文', outputs[2].text)
        self.assertEqual(['th-en', 'en-zh'], outputs[3].metadata['route'])

