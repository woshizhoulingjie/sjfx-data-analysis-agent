"""Offline CPU translation provider backed by a local NLLB checkpoint."""
from __future__ import annotations

import threading
import time
import os
import re

from services.translation import ProviderResponse, TranslationProvider, TranslationProviderError

NLLB_LANGUAGES = {
    "ar": "arb_Arab", "en": "eng_Latn", "ja": "jpn_Jpan", "ko": "kor_Hang",
    "ru": "rus_Cyrl", "fr": "fra_Latn", "de": "deu_Latn", "es": "spa_Latn",
    "it": "ita_Latn", "pt": "por_Latn", "nl": "nld_Latn", "tr": "tur_Latn",
    "pl": "pol_Latn", "uk": "ukr_Cyrl", "fa": "pes_Arab", "he": "heb_Hebr",
    "hi": "hin_Deva", "bn": "ben_Beng", "th": "tha_Thai", "vi": "vie_Latn",
    "el": "ell_Grek", "el-gr": "ell_Grek",
    "zh": "zho_Hans", "zh-cn": "zho_Hans", "zh-CN": "zho_Hans",
}

# NLLB is not trained to copy arbitrary application placeholders.  Sending
# strings such as ``__SJFX_KEEP_0001__`` through the decoder therefore causes
# exactly the protected-token failures seen on mixed PDF pages.  We keep the
# public token contract, but split around these spans before inference and
# reassemble them verbatim afterwards.
_PROTECTED_TOKEN_RE = re.compile(r"__SJFX_(?:TERM|KEEP)_\d{4}__")


class OfflineNLLBProvider(TranslationProvider):
    """NLLB-200 provider constrained to CPU and local files."""

    # Newlines are split into literal layout spans in ``_prepare_requests``
    # and restored byte-for-byte after inference. This lets the service merge
    # adjacent prose paragraphs into one request without losing structure,
    # which is materially faster on CPU than one request per paragraph.
    preserves_line_breaks = True

    def __init__(self, model_path, device="cpu", batch_size=4, cpu_threads=4,
                 max_input_tokens=1024, max_new_tokens=1024):
        self.model_path = str(model_path or "").strip()
        self.device = "cpu"
        self.batch_size = max(1, min(32, int(batch_size or 4)))
        self.cpu_threads = max(1, min(64, int(cpu_threads or 4)))
        self.max_input_tokens = max(128, min(4096, int(max_input_tokens or 1024)))
        self.max_new_tokens = max(128, min(4096, int(max_new_tokens or 1024)))
        self._tokenizer = None
        self._model = None
        self._translator = None
        self._backend = "transformers"
        self._torch = None
        self._load_lock = threading.Lock()

    @property
    def provider_id(self):
        if os.path.isfile(os.path.join(self.model_path, "model.bin")):
            return "offline_nllb:600m:ct2-int8"
        return "offline_nllb:600m:transformers"

    def _ensure_loaded(self):
        if self._model is not None or self._translator is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            if not self.model_path:
                raise TranslationProviderError(
                    "未配置本地 NLLB 模型目录", retryable=False,
                    code="provider_unavailable",
                )
            try:
                from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
                tokenizer = AutoTokenizer.from_pretrained(self.model_path, local_files_only=True)
                # We perform our own token-aware chunking below.  Raising the
                # tokenizer's advisory limit prevents a misleading warning for
                # an input that is intentionally measured before inference.
                try:
                    tokenizer.model_max_length = max(
                        int(getattr(tokenizer, "model_max_length", 0) or 0),
                        1000000,
                    )
                except Exception:
                    pass
                if os.path.isfile(os.path.join(self.model_path, "model.bin")):
                    import ctranslate2
                    translator = ctranslate2.Translator(
                        self.model_path, device="cpu", inter_threads=1,
                        intra_threads=self.cpu_threads,
                    )
                    self._backend = "ct2"
                    self._tokenizer, self._translator = tokenizer, translator
                    return
                import torch
                torch.set_num_threads(self.cpu_threads)
                # Keep a single inter-op pool. Otherwise a shared worker can
                # multiply OpenMP pools and starve unrelated server tasks.
                try:
                    torch.set_num_interop_threads(1)
                except RuntimeError:
                    # PyTorch only permits this before the first parallel op;
                    # an initialized host remains safe to reuse.
                    pass
                model = AutoModelForSeq2SeqLM.from_pretrained(self.model_path, local_files_only=True)
                model.to("cpu")
                model.eval()
            except Exception as exc:
                raise TranslationProviderError(
                    "本地 NLLB 模型或依赖不可用：{}".format(str(exc)[:500]),
                    retryable=False, code="provider_unavailable",
                ) from exc
            self._torch, self._tokenizer, self._model = torch, tokenizer, model

    @staticmethod
    def _language_code(language):
        key = str(language or "").strip()
        return NLLB_LANGUAGES.get(key) or NLLB_LANGUAGES.get(key.lower())

    def _token_chunks(self, texts):
        """Token-split marker-free inputs so NLLB never silently truncates."""
        chunks = []
        owners = []
        token_counts = []
        limit = max(64, self.max_input_tokens - 2)
        for owner, value in enumerate(str(text or "") for text in texts):
            encoded = self._tokenizer(value, add_special_tokens=True, truncation=False)
            ids = list(encoded.get("input_ids") or [])
            token_counts.append(len(ids))
            if len(ids) <= self.max_input_tokens:
                chunks.append(value)
                owners.append(owner)
                continue
            for start in range(0, len(ids), limit):
                piece = self._tokenizer.decode(ids[start:start + limit], skip_special_tokens=True)
                if piece.strip():
                    chunks.append(piece)
                    owners.append(owner)
        return chunks, owners, token_counts

    def _prepare_requests(self, texts):
        """Prepare batched requests while keeping protected spans literal.

        Each layout contains literal spans (protected tokens, whitespace and
        punctuation-only text) plus indexes into ``requests``.  This lets one
        CTranslate2 batch handle all translatable pieces without ever asking
        the model to copy a synthetic marker.
        """
        layouts = []
        requests = []
        # Structured files commonly repeat the same headers, boilerplate and
        # field labels.  Translate each distinct chunk only once per provider
        # call, then let the layouts reference that shared result.  This is a
        # correctness-preserving optimisation (the source language and target
        # are constant for one call) and can substantially reduce CPU decode
        # time on repetitive packages.
        request_lookup = {}

        def add_request(value):
            key = str(value)
            existing = request_lookup.get(key)
            if existing is not None:
                return existing
            index = len(requests)
            requests.append(key)
            request_lookup[key] = index
            return index

        token_counts = [0 for _ in texts]
        limit = max(64, self.max_input_tokens - 2)
        for owner, value in enumerate(str(text or "") for text in texts):
            layout = []
            # Keep the delimiter in the result; it must be reinserted
            # byte-for-byte after inference.
            parts = re.split(r"(__SJFX_(?:TERM|KEEP)_\d{4}__)", value)
            for part in parts:
                if not part:
                    continue
                if _PROTECTED_TOKEN_RE.fullmatch(part):
                    layout.append(("literal", part))
                    continue
                # Keep line separators outside the model. NLLB may normalize
                # spaces inside a line, but it must never collapse paragraph
                # or page boundaries in the reconstructed document.
                line_parts = re.split(r"(\r\n|\n|\r)", part)
                for line_part in line_parts:
                    if line_part in {"\r\n", "\n", "\r"}:
                        layout.append(("literal", line_part))
                        continue
                    if not line_part:
                        continue
                    leading_match = re.match(r"^\s*", line_part)
                    trailing_match = re.search(r"\s*$", line_part)
                    leading = leading_match.group(0) if leading_match else ""
                    trailing = trailing_match.group(0) if trailing_match else ""
                    core_end = len(line_part) - len(trailing) if trailing else len(line_part)
                    core = line_part[len(leading):core_end]
                    if not core:
                        layout.append(("literal", line_part))
                        continue
                    # Do not spend inference on whitespace, separators, or
                    # Chinese text that was already in a mixed-language unit.
                    try:
                        from services.translation import detect_language
                        needs_translation = bool(detect_language(core).get("needs_translation"))
                    except Exception:
                        needs_translation = bool(re.search(r"[A-Za-z\u00c0-\u024f\u0400-\u04ff\u3040-\u30ff\uac00-\ud7af]", core))
                    if not needs_translation:
                        layout.append(("literal", line_part))
                        continue
                    encoded = self._tokenizer(core, add_special_tokens=True, truncation=False)
                    ids = list(encoded.get("input_ids") or [])
                    token_counts[owner] += len(ids)
                    indexes = []
                    if len(ids) <= self.max_input_tokens:
                        indexes.append(add_request(core))
                    else:
                        for start in range(0, len(ids), limit):
                            piece = self._tokenizer.decode(
                                ids[start:start + limit], skip_special_tokens=True
                            )
                            if piece.strip():
                                indexes.append(add_request(piece))
                    layout.append(("translated", leading, trailing, indexes))
            layouts.append(layout)
        return requests, layouts, token_counts

    @staticmethod
    def _render_layout(layout, chunk_outputs):
        pieces = []
        for item in layout:
            if item[0] == "literal":
                pieces.append(item[1])
                continue
            _, leading, trailing, indexes = item
            translated = " ".join(
                str(chunk_outputs[index] or "").strip() for index in indexes
            ).strip()
            pieces.append(leading + translated + trailing)
        return "".join(pieces)

    def translate_batch(self, texts, source_language, target_language="zh-CN",
                        glossary=None, timeout=None, retries=0):
        del glossary, timeout, retries
        source_code = self._language_code(source_language)
        target_code = self._language_code(target_language) or "zho_Hans"
        if not source_code:
            raise TranslationProviderError(
                "NLLB 不支持源语言：{}".format(source_language), retryable=False,
                code="unsupported_language",
            )
        if not texts:
            return []
        self._ensure_loaded()
        started = time.monotonic()
        try:
            self._tokenizer.src_lang = source_code
            chunks, layouts, token_counts = self._prepare_requests(texts)
            if not chunks:
                # This is possible for a unit made entirely of protected
                # literals and separators.  Return the exact source instead
                # of an empty response so the quality contract can still be
                # validated and the unit can be restored safely.
                outputs = [self._render_layout(layout, []) for layout in layouts]
                return [ProviderResponse(text=output, model=self.provider_id,
                                         usage={"batch_size": len(texts), "input_tokens": token_counts[index]},
                                         metadata={"source_code": source_code,
                                                   "target_code": target_code,
                                                   "device": "cpu", "protected_only": True})
                        for index, output in enumerate(outputs)]
            encoded = self._tokenizer(chunks, add_special_tokens=True, truncation=False)
            max_chunk_tokens = max(len(ids) for ids in encoded["input_ids"])
            generation_limit = min(
                self.max_new_tokens,
                max(64, int(max_chunk_tokens * 1.6) + 16),
            )
            if self._backend == "ct2":
                token_batches = [self._tokenizer.convert_ids_to_tokens(ids) for ids in encoded["input_ids"]]
                results = self._translator.translate_batch(
                    token_batches,
                    target_prefix=[[target_code] for _ in token_batches],
                    max_batch_size=self.batch_size,
                    max_decoding_length=generation_limit,
                )
                chunk_outputs = [
                    self._tokenizer.decode(
                        self._tokenizer.convert_tokens_to_ids(result.hypotheses[0]),
                        skip_special_tokens=True,
                    )
                    for result in results
                ]
            else:
                encoded = self._tokenizer(
                    chunks, return_tensors="pt", padding=True, truncation=False,
                )
                encoded = {key: value.to("cpu") for key, value in encoded.items()}
                forced_bos = self._tokenizer.convert_tokens_to_ids(target_code)
                with self._torch.inference_mode():
                    generated = self._model.generate(
                        **encoded, forced_bos_token_id=forced_bos,
                        max_new_tokens=generation_limit,
                    )
                chunk_outputs = self._tokenizer.batch_decode(generated, skip_special_tokens=True)
            outputs = [self._render_layout(layout, chunk_outputs) for layout in layouts]
        except Exception as exc:
            raise TranslationProviderError(
                "NLLB CPU 推理失败：{}".format(str(exc)[:500]), retryable=True,
                code="offline_inference_error",
            ) from exc
        elapsed = round(time.monotonic() - started, 4)
        output_tokens = [len(self._tokenizer(str(output or "")).input_ids) for output in outputs]

        # ``_prepare_requests`` returns a layout rather than the old
        # ``grouped`` structure.  Derive chunk counts from that layout for
        # telemetry.  The previous code referenced ``grouped`` here, which
        # was undefined and caused every successful CT2 inference to be
        # converted into an ``offline_inference_error`` while building the
        # response metadata.  Besides being incorrect, that forced retries
        # and made translation appear much slower.
        chunk_counts = []
        for layout in layouts:
            count = 0
            for item in layout:
                if item and item[0] == "translated":
                    count += len(item[3])
            chunk_counts.append(count)
        return [ProviderResponse(
            text=str(output or "").strip(), model=self.provider_id,
            usage={"batch_size": len(texts), "input_tokens": int(token_counts[index]),
                   "output_tokens": int(output_tokens[index])},
            metadata={"source_code": source_code, "target_code": target_code,
                      "elapsed_seconds": elapsed, "device": "cpu", "backend": self._backend,
                      "chunked": chunk_counts[index] > 1,
                      "chunk_count": chunk_counts[index]},
        ) for index, output in enumerate(outputs)]

    def translate(self, text, source_language, target_language="zh-CN",
                  glossary=None, timeout=None, retries=0):
        return self.translate_batch([text], source_language, target_language,
                                    glossary=glossary, timeout=timeout, retries=retries)[0]


__all__ = ["NLLB_LANGUAGES", "OfflineNLLBProvider"]
