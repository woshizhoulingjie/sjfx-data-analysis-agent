"""Offline CPU translation provider backed by a local NLLB checkpoint."""
from __future__ import annotations

import threading
import time
import os

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


class OfflineNLLBProvider(TranslationProvider):
    """NLLB-200 provider constrained to CPU and local files."""

    # The seq2seq decoder can normalize internal newlines. The service uses
    # this capability flag to keep paragraph units separate while preserving
    # its normal batching for providers with a layout-preserving contract.
    preserves_line_breaks = False

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
        """Token-split oversized inputs so NLLB never silently truncates."""
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
            chunks, owners, token_counts = self._token_chunks(texts)
            if not chunks:
                return [ProviderResponse(text="", model=self.provider_id,
                                         usage={"batch_size": 1, "input_tokens": 0},
                                         metadata={"source_code": source_code,
                                                   "target_code": target_code,
                                                   "device": "cpu"}) for _ in texts]
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
            grouped = [[] for _ in texts]
            for owner, output in zip(owners, chunk_outputs):
                grouped[owner].append(str(output or "").strip())
            outputs = [" ".join(parts).strip() for parts in grouped]
        except Exception as exc:
            raise TranslationProviderError(
                "NLLB CPU 推理失败：{}".format(str(exc)[:500]), retryable=True,
                code="offline_inference_error",
            ) from exc
        elapsed = round(time.monotonic() - started, 4)
        output_tokens = [len(self._tokenizer(str(output or "")).input_ids) for output in outputs]
        return [ProviderResponse(
            text=str(output or "").strip(), model=self.provider_id,
            usage={"batch_size": len(texts), "input_tokens": int(token_counts[index]),
                   "output_tokens": int(output_tokens[index])},
            metadata={"source_code": source_code, "target_code": target_code,
                      "elapsed_seconds": elapsed, "device": "cpu", "backend": self._backend,
                      "chunked": len(grouped[index]) > 1,
                      "chunk_count": len(grouped[index])},
        ) for index, output in enumerate(outputs)]

    def translate(self, text, source_language, target_language="zh-CN",
                  glossary=None, timeout=None, retries=0):
        return self.translate_batch([text], source_language, target_language,
                                    glossary=glossary, timeout=timeout, retries=retries)[0]


__all__ = ["NLLB_LANGUAGES", "OfflineNLLBProvider"]
