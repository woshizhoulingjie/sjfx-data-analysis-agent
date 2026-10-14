"""Backend selection and file translation contracts without model hardware."""
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from services.model_backend import create_model_client
from services.ollama import OllamaClient
from services.vllm import VLLMClient
from services.file_translation import build_file_translation_service
from services.translation import TranslationProviderError
from scripts import large_qa_worker as worker
from large_package_worker import LargePackageWorker


def settings(**overrides):
    values = dict(LLM_BACKEND="ollama", LLM_MAX_CONCURRENCY=2,
                  ENABLE_VLLM=True, ENABLE_SHARED_OLLAMA=True,
                  VLLM_BASE_URL="http://127.0.0.1:8001/v1", VLLM_MODEL="vllm-model",
                  VLLM_REQUEST_TIMEOUT=600, VLLM_API_KEY="test-key",
                  OLLAMA_BASE_URL="http://127.0.0.1:11434/v1", OLLAMA_MODEL="ollama-model",
                  SHARED_OLLAMA_REQUEST_TIMEOUT=300)
    values.update(overrides)
    return SimpleNamespace(**values)


class ModelBackendTests(unittest.TestCase):
    def test_ollama_selection_uses_native_client_without_vllm_construction(self):
        with patch("services.vllm.VLLMClient", side_effect=AssertionError("wrong backend")):
            client = create_model_client(settings(), timeout=90, max_concurrency=1)
        self.assertIsInstance(client, OllamaClient)
        self.assertEqual((client.model, client.timeout, client.max_concurrency),
                         ("ollama-model", 90, 1))

    def test_vllm_selection_keeps_api_key_and_defaults(self):
        client = create_model_client(settings(LLM_BACKEND="vllm"))
        self.assertIsInstance(client, VLLMClient)
        self.assertEqual((client.model, client.timeout, client.api_key),
                         ("vllm-model", 600, "test-key"))

    def test_disabled_selected_backend_does_not_fall_back(self):
        for config in (settings(ENABLE_SHARED_OLLAMA=False),
                       settings(LLM_BACKEND="vllm", ENABLE_VLLM=False)):
            with self.subTest(backend=config.LLM_BACKEND):
                self.assertIsNone(create_model_client(config))
                self.assertIsNotNone(create_model_client(config, require_enabled=False))

    def test_invalid_backend_is_rejected(self):
        with self.assertRaises(ValueError):
            create_model_client(settings(LLM_BACKEND="remote"))

    def test_deep_summary_disabled_vllm_never_falls_back_to_enabled_ollama(self):
        config = settings(LLM_BACKEND="vllm", ENABLE_VLLM=False,
                          ENABLE_SHARED_OLLAMA=True, VLLM_STRUCTURED_TIMEOUT_SECONDS=240)
        with patch("large_package_worker.Config", config), \
                patch("services.vllm.VLLMClient") as vllm, \
                patch("services.ollama.OllamaClient") as ollama:
            payload = LargePackageWorker._model_summary(None, "test.txt", "正文")
        vllm.assert_not_called()
        ollama.assert_not_called()
        self.assertEqual(payload, {"summary_type":"local_deep", "model_status":"disabled",
                                   "quality":"degraded", "coverage":"sampled"})

    def test_deep_summary_honors_selected_backend_and_original_timeouts(self):
        for backend, timeout in (("vllm", 240), ("ollama", 180)):
            with self.subTest(backend=backend):
                config = settings(LLM_BACKEND=backend, VLLM_STRUCTURED_TIMEOUT_SECONDS=240)
                reply = {"content":"完整摘要", "finish_reason":"stop", "model":"fixture"}
                with patch("large_package_worker.Config", config), \
                        patch("services.vllm.VLLMClient") as vllm, \
                        patch("services.ollama.OllamaClient") as ollama:
                    chosen, unused = (vllm, ollama) if backend == "vllm" else (ollama, vllm)
                    chosen.return_value.chat.return_value = reply
                    payload = LargePackageWorker._model_summary(None, "test.txt", "原文依据")
                unused.assert_not_called()
                self.assertEqual(chosen.call_args.kwargs["timeout"], timeout)
                self.assertEqual(chosen.call_args.kwargs["max_concurrency"], 1)
                self.assertEqual(chosen.call_args.args[1], config.VLLM_MODEL if backend == "vllm" else config.OLLAMA_MODEL)
                self.assertEqual(payload["summary_type"], "model")
                self.assertEqual(payload["summary"], "完整摘要")

    def test_file_translation_ollama_uses_supported_arguments_and_metadata(self):
        client = create_model_client(settings())
        with patch.object(client, "_request", return_value={"content":'{"translation":"完整译文"}',
                          "finish_reason":"stop", "model":client.model}) as request:
            service = build_file_translation_service(client, None)
            response = service.provider.translate("Complete source.", "en")
        self.assertEqual(response.text, "完整译文")
        self.assertEqual(response.metadata["backend"], "ollama")
        self.assertEqual(service.provider.provider_id, "ollama:ollama-model")
        self.assertTrue(service.policy.contract_version.endswith(":ollama:ollama-model"))
        self.assertNotIn("response_format", request.call_args.args[0])

    def test_file_translation_vllm_keeps_json_format_and_cache_identity(self):
        client = create_model_client(settings(LLM_BACKEND="vllm"))
        with patch.object(client, "chat", return_value={"content":'{"translation":"完整译文"}'}) as chat:
            service = build_file_translation_service(client, None)
            service.provider.translate("Complete source.", "en")
        self.assertEqual(chat.call_args.kwargs["response_format"], {"type":"json_object"})
        self.assertTrue(service.policy.contract_version.endswith(":vllm:vllm-model"))

    def test_ollama_invalid_structured_output_remains_failure(self):
        client = create_model_client(settings())
        with patch.object(client, "_request", return_value={"content":"not JSON"}):
            with self.assertRaises(TranslationProviderError) as caught:
                build_file_translation_service(client, None).provider.translate("Source", "en")
        self.assertEqual(caught.exception.code, "ollama_error")

    def test_topic_worker_uses_selected_backend_and_skips_disabled_refinement(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
                base = Path(folder)
                config = settings(ENABLE_SHARED_OLLAMA=enabled, DB_PATH=base/"db",
                                  LARGE_QA_REFINE_TOPICS=True)
                index = Mock()
                index.next_parse.return_value = None
                args = SimpleNamespace(source_db="unused", package=None, once=True, summaries_only=False)
                stack.enter_context(patch.object(worker, "Config", config))
                stack.enter_context(patch.object(worker, "STOP", False))
                stack.enter_context(patch.object(worker, "enabled", return_value=True))
                stack.enter_context(patch.object(worker, "ReadOnlyStore", return_value=SimpleNamespace(
                    packages=lambda:[dict(id="test-package", root_path=str(base))])))
                stack.enter_context(patch.object(worker.demand, "demand_only", return_value=False))
                stack.enter_context(patch.object(worker.demand, "package_stamp", return_value="stamp"))
                stack.enter_context(patch.object(worker.demand, "should_sync", return_value=False))
                stack.enter_context(patch.object(worker, "LargeQAIndex", return_value=index))
                stack.enter_context(patch.object(worker, "DiskGuard"))
                stack.enter_context(patch.object(worker, "foreground_busy", return_value=False))
                stack.enter_context(patch.object(worker.time, "monotonic", return_value=100))
                refine = stack.enter_context(patch.object(worker, "refine_one", return_value=False))
                stack.enter_context(patch("services.vllm.VLLMClient", side_effect=AssertionError("wrong backend")))
                output = io.StringIO()
                with redirect_stdout(output):
                    worker._run_locked(args, base)
                self.assertNotIn("error", json.loads(output.getvalue())["test-package"])
                if enabled:
                    self.assertIsInstance(refine.call_args.args[1], OllamaClient)
                else:
                    refine.assert_not_called()
