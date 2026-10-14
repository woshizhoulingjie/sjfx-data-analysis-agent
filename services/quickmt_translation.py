"""Local QuickMT CPU provider using the tested SJFX protection/layout contract.

Native dependencies and model weights are loaded only on a translation call.
Deployment verifies full weight hashes; the cache identity reads small resources
and weight size/mtime, never the 1.22 GB of weights at every Web startup.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import threading
import time

from services.offline_translation import OfflineNLLBProvider
from services.translation import ProviderResponse, TranslationProviderError, detect_language, segment_text

_PAIRS = ('en-zh', 'zh-en', 'th-en')
_ROUTES = {('en', 'zh'): ('en-zh',), ('zh', 'en'): ('zh-en',),
           ('th', 'zh'): ('th-en', 'en-zh')}
_SMALL_FILES = ('src.spm.model', 'tgt.spm.model', 'config.json',
                'source_vocabulary.json', 'target_vocabulary.json')


def _unavailable(message):
    return TranslationProviderError(message, retryable=False, code='provider_unavailable')


class _SourceTokenizer:
    def __init__(self, processor):
        self.processor = processor

    def __call__(self, value, add_special_tokens=True, truncation=False):
        def encode(text):
            ids = self.processor.encode(text, out_type=int)
            if add_special_tokens:
                ids.append(self.processor.eos_id())
            return ids
        return {'input_ids': [encode(text) for text in value] if isinstance(value, list) else encode(value)}

    def decode(self, ids, skip_special_tokens=True):
        if skip_special_tokens:
            ids = [index for index in ids if not self.processor.is_control(index)]
        return self.processor.decode(ids)


class _PairModel:
    def __init__(self, directory, threads):
        directory = Path(directory)
        for name in ('model.bin', *_SMALL_FILES):
            if not (directory / name).is_file():
                raise _unavailable('QuickMT 本地资源缺失：' + str(directory / name))
        try:
            import ctranslate2
            import sentencepiece
            # Python opens Unicode paths that SentencePiece's filename API
            # cannot reliably open on Windows.
            self.source = sentencepiece.SentencePieceProcessor(
                model_proto=(directory / 'src.spm.model').read_bytes())
            self.target = sentencepiece.SentencePieceProcessor(
                model_proto=(directory / 'tgt.spm.model').read_bytes())
            self.engine = ctranslate2.Translator(str(directory), device='cpu', compute_type='int8',
                                               inter_threads=1, intra_threads=threads)
        except Exception as exc:
            raise _unavailable('QuickMT 本地模型或依赖不可用：' + str(exc)[:500]) from exc

    def translate(self, texts, beam, batch_size, output_limit):
        encoded = [self.source.encode(text, out_type=str) + ['</s>'] for text in texts]
        options = dict(beam_size=beam, max_batch_size=batch_size,
            max_input_length=0, max_decoding_length=output_limit, disable_unk=True,
            replace_unknowns=False, length_penalty=1, coverage_penalty=0, repetition_penalty=1)
        results = self.engine.translate_batch(encoded, **options)
        outputs = []
        for text, source_tokens, row in zip(texts, encoded, results):
            if len(row.hypotheses[0]) < output_limit:
                outputs.append(self.target.decode(row.hypotheses[0]))
                continue
            # PDF line fragments can make beam search repeat a phrase until
            # the decoder cap, even for a very short source. Retry only the
            # affected source with a modest repetition penalty before splitting.
            retry = self.engine.translate_batch([source_tokens],
                **dict(options, repetition_penalty=1.1))[0]
            if len(retry.hypotheses[0]) < output_limit:
                outputs.append(self.target.decode(retry.hypotheses[0]))
                continue
            # A long Chinese rendering may exceed the decoder window even if
            # its source fits the input window. Retry only that source as smaller
            # lossless pieces; never accept the truncated draft as complete.
            if len(text) <= 128:
                raise TranslationProviderError('QuickMT 译文达到输出上限', code='output_truncated')
            pieces = [piece['text'] for piece in segment_text(text, max_chars=max(128, len(text) // 2))]
            if len(pieces) < 2 or any(len(piece) >= len(text) for piece in pieces):
                raise TranslationProviderError('QuickMT 译文达到输出上限', code='output_truncated')
            recovered = self.translate(pieces, beam, batch_size, output_limit)
            outputs.append(' '.join(piece.strip() for piece in recovered))
        return outputs


class OfflineQuickMTProvider(OfflineNLLBProvider):
    """Explicit en→zh, zh→en and th→en→zh routes; no fallback provider."""

    preserves_line_breaks = True

    def __init__(self, resource_root, *, resource_manifest_path=None, beam_size=5,
                 cpu_threads=4, batch_size=8, max_input_tokens=1024, max_new_tokens=512):
        super().__init__(resource_root, cpu_threads=cpu_threads, batch_size=batch_size,
                         max_input_tokens=max_input_tokens, max_new_tokens=max_new_tokens)
        self.resource_root = Path(resource_root).resolve() if str(resource_root or '').strip() else None
        self.resource_manifest_path = (Path(resource_manifest_path).resolve() if resource_manifest_path
                                       else self.resource_root / 'resource_manifest.json' if self.resource_root else None)
        self.beam_size = max(1, min(32, int(beam_size)))
        self.pairs = {}
        self._identity = None
        self._resource_signature = None
        self._request_lock = threading.RLock()
        self._backend = 'ct2'

    def _resource_stats(self):
        if self.resource_root is None or self.resource_manifest_path is None:
            raise _unavailable('未配置 QuickMT 本地资源目录')
        files = [self.resource_manifest_path]
        for pair in _PAIRS:
            directory = self.resource_root / pair
            files.extend(directory / name for name in ('resource_manifest.json', 'model.bin', *_SMALL_FILES))
        result = []
        try:
            for path in files:
                if not path.is_file():
                    raise _unavailable('QuickMT 本地资源缺失：' + str(path))
                stat = path.stat()
                result.append((str(path), stat.st_size, stat.st_mtime_ns))
        except OSError as exc:
            raise _unavailable('QuickMT 资源不可读取：' + str(exc)[:500]) from exc
        return tuple(result)

    @property
    def provider_id(self):
        with self._request_lock:
            signature = self._resource_stats()
            if self._identity is not None:
                if signature != self._resource_signature:
                    # Existing engines must not use changed on-disk resource
                    # identity with stale loaded weights. Restart after deploy.
                    raise _unavailable('QuickMT 资源已变更，请重启翻译服务后重试')
                return self._identity
            try:
                root_bytes = self.resource_manifest_path.read_bytes()
                root_manifest = json.loads(root_bytes)
                resources = []
                for pair in _PAIRS:
                    directory = self.resource_root / pair
                    pair_bytes = (directory / 'resource_manifest.json').read_bytes()
                    manifest = json.loads(pair_bytes)
                    root_version = root_manifest['models'][pair]
                    if not manifest.get('commit') or manifest['commit'] != root_version.get('commit'):
                        raise _unavailable('QuickMT 资源版本不一致：' + pair)
                    actual_files = {entry['name']: entry for entry in manifest['actual_files']}
                    model_entry = actual_files['model.bin']
                    if (directory / 'model.bin').stat().st_size != int(model_entry['size']):
                        raise _unavailable('QuickMT 模型大小与已验证清单不一致：' + pair)
                    if not model_entry.get('sha256') or model_entry['sha256'] != root_version.get('actual_model_sha256'):
                        raise _unavailable('QuickMT 模型校验身份不一致：' + pair)
                    hashes = {}
                    for name in _SMALL_FILES:
                        hashes[name] = hashlib.sha256((directory / name).read_bytes()).hexdigest()
                        if hashes[name] != actual_files[name].get('sha256'):
                            raise _unavailable('QuickMT 小资源校验失败：' + pair + '/' + name)
                    stat = (directory / 'model.bin').stat()
                    resources.append({'pair': pair, 'commit': manifest['commit'],
                        'manifest_sha256': hashlib.sha256(pair_bytes).hexdigest(),
                        'model_sha256_verified_at_deploy': model_entry['sha256'],
                        'model_size': stat.st_size, 'model_mtime_ns': stat.st_mtime_ns,
                        'small_hashes': hashes})
                payload = {'contract': 'quickmt-layout/1', 'device': 'cpu', 'compute_type': 'int8',
                    'beam': self.beam_size, 'cpu_threads': self.cpu_threads, 'inter_threads': 1,
                    'batch_size': self.batch_size, 'max_input_tokens': self.max_input_tokens,
                    'max_new_tokens': self.max_new_tokens,
                    'root_manifest_sha256': hashlib.sha256(root_bytes).hexdigest(), 'resources': resources}
                fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            except TranslationProviderError:
                raise
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise _unavailable('QuickMT 资源清单不可用：' + str(exc)[:500]) from exc
            self._identity = ('offline_quickmt:cpu-int8:beam{}:threads{}:batch{}:input{}:output{}:sha256:{}'
                .format(self.beam_size, self.cpu_threads, self.batch_size,
                        self.max_input_tokens, self.max_new_tokens, fingerprint))
            self._resource_signature = signature
            return self._identity

    def _pair(self, name):
        if name not in self.pairs:
            self.pairs[name] = _PairModel(self.resource_root / name, self.cpu_threads)
        return self.pairs[name]

    def _ensure_loaded(self):
        # Do not inherit the NLLB loader's optional Transformers path.
        with self._request_lock:
            _ = self.provider_id
            for pair in _PAIRS:
                self._pair(pair)

    @staticmethod
    def _language(language):
        return str(language or '').strip().lower().replace('_', '-').split('-')[0]

    @staticmethod
    def _mixed_source(text):
        # The existing service can label Chinese plus foreign text as mixed.
        # Inspect the foreign part only for routing; layout preparation stays
        # unchanged and does not send protected markers into the decoder.
        foreign = re.sub(r'__SJFX_(?:TERM|KEEP)_\d{4}__|[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]', '', text)
        detection = detect_language(foreign)
        if not detection.get('needs_translation'):
            return 'zh'
        language = detection.get('language')
        if language not in ('en', 'th'):
            raise TranslationProviderError('QuickMT 不支持混合文本中的语言：' + str(language),
                                           retryable=False, code='unsupported_language')
        counts = detection.get('script_counts') or {}
        if any(counts.get(script, 0) for script in ('kana', 'hangul', 'cyrillic', 'arabic',
                                                  'devanagari', 'hebrew', 'greek', 'other')):
            raise TranslationProviderError('QuickMT 不支持此混合文本的外语字符',
                                           retryable=False, code='unsupported_language')
        return language

    def translate_batch(self, texts, source_language, target_language='zh-CN',
                        glossary=None, timeout=None, retries=0):
        del glossary, retries
        source, target = self._language(source_language), self._language(target_language)
        if source == 'mixed' and target == 'zh':
            # Group supported routes separately instead of guessing one route
            # for a batch containing both English and Thai mixed units.
            groups, responses = {}, [None for _ in texts]
            for index, text in enumerate(texts):
                groups.setdefault(self._mixed_source(str(text or '')), []).append((index, text))
            for language, items in groups.items():
                translated = self.translate_batch([text for _, text in items], language, target,
                                                  timeout=timeout)
                for (index, _), response in zip(items, translated):
                    responses[index] = response
            return responses
        route = _ROUTES.get((source, target))
        passthrough = source == target == 'zh'
        if route is None and not passthrough:
            raise TranslationProviderError('QuickMT 不支持语言方向：{}→{}'.format(source_language, target_language),
                                           retryable=False, code='unsupported_language')
        if not texts:
            return []
        started = time.monotonic()
        # Covers model loading, mutable source tokenizer, preparation, both
        # pivot stages and rendering. No cross-route tokenizer races.
        with self._request_lock:
            identity = self.provider_id
            if passthrough:
                return [ProviderResponse(text=str(text or ''), model=identity,
                    usage={'input_tokens': 0, 'batch_size': len(texts)},
                    metadata={'backend': 'ctranslate2', 'device': 'cpu', 'beam_size': self.beam_size,
                              'route': [], 'pivot': False, 'protected_only': True,
                              'not_required': True, 'elapsed_seconds': time.monotonic() - started}) for text in texts]
            engines = [self._pair(pair) for pair in route]
            self._tokenizer = _SourceTokenizer(engines[0].source)
            try:
                prepare = _chinese_requests if source == 'zh' else OfflineNLLBProvider._prepare_requests
                chunks, layouts, input_counts = prepare(self, texts)
                outputs, intermediate, stage_timings = chunks, None, []
                for pair, engine in zip(route, engines):
                    before = time.monotonic()
                    if outputs:
                        outputs = engine.translate(outputs, self.beam_size, self.batch_size, self.max_new_tokens)
                    stage_timings.append({'pair': pair, 'seconds': time.monotonic() - before})
                    if pair == 'th-en':
                        intermediate = list(outputs)
                    if timeout is not None and time.monotonic() - started > float(timeout):
                        raise TranslationProviderError('QuickMT CPU 翻译超时', code='provider_timeout')
                rendered = [self._render_layout(layout, outputs) for layout in layouts]
            except TranslationProviderError:
                raise
            except Exception as exc:
                raise TranslationProviderError('QuickMT CPU 推理失败：' + str(exc)[:500],
                                               code='offline_inference_error') from exc
            elapsed = time.monotonic() - started
            return [ProviderResponse(text=text, model=identity,
                usage={'input_tokens': input_counts[index], 'batch_size': len(texts)},
                metadata={'backend': 'ctranslate2', 'device': 'cpu',
                    'compute_type': engines[0].engine.compute_type, 'requested_compute_type': 'int8',
                    'beam_size': self.beam_size, 'cpu_threads': self.cpu_threads,
                    'route': list(route), 'pivot': source == 'th', 'elapsed_seconds': elapsed,
                    'stage_timings': stage_timings, 'intermediate_english_chunks': intermediate,
                    'model_requests': len(chunks), 'protected_only': not chunks})
                for index, text in enumerate(rendered)]

    def translate(self, text, source_language, target_language='zh-CN',
                  glossary=None, timeout=None, retries=0):
        return self.translate_batch([text], source_language, target_language,
                                    glossary=glossary, timeout=timeout, retries=retries)[0]


def _chinese_requests(provider, texts):
    """The tested reverse-direction layout helper, without changing prose."""
    layouts, requests, counts, lookup = [], [], [], {}
    delimiters = re.compile(r'(__SJFX_(?:TERM|KEEP)_\d{4}__|\r\n|[\r\n]|\t+|\|+)')
    for text in texts:
        layout, count = [], 0
        for part in delimiters.split(str(text)):
            if not part:
                continue
            if delimiters.fullmatch(part) or not re.search(r'[\u3400-\u9fff]', part):
                layout.append(('literal', part))
                continue
            leading = part[:len(part) - len(part.lstrip())]
            trailing = part[len(part.rstrip()):]
            core = part.strip()
            ids = provider._tokenizer(core, add_special_tokens=True)['input_ids']
            count += len(ids)
            chunks = [core] if len(ids) <= provider.max_input_tokens else [
                provider._tokenizer.decode(ids[start:start + provider.max_input_tokens - 2], skip_special_tokens=True)
                for start in range(0, len(ids), provider.max_input_tokens - 2)]
            indexes = []
            for chunk in chunks:
                if not chunk.strip():
                    continue
                if chunk not in lookup:
                    lookup[chunk] = len(requests)
                    requests.append(chunk)
                indexes.append(lookup[chunk])
            layout.append(('translated', leading, trailing, indexes))
        layouts.append(layout)
        counts.append(count)
    return requests, layouts, counts


__all__ = ['OfflineQuickMTProvider']
