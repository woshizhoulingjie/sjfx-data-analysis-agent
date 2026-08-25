import re
import unittest
from unittest import mock

from services.translation import (
    InMemoryTranslationMemory,
    OllamaTranslationProvider,
    ProviderResponse,
    TranslationPolicy,
    TranslationProvider,
    TranslationProviderError,
    TranslationService,
    UnavailableTranslationProvider,
    build_translation_plan,
    build_translation_units,
    detect_language,
    glossary_fingerprint,
    protect_text,
    segment_text,
    translation_memory_key,
    validate_translation,
)


TOKEN_RE = re.compile(r"__SJFX_(?:TERM|KEEP)_\d{4}__")


class DeterministicProvider(TranslationProvider):
    def __init__(self, failures=0):
        self.calls = 0
        self.failures = failures
        self.inputs = []

    @property
    def provider_id(self):
        return "test:deterministic"

    def translate(self, text, source_language, target_language, glossary=None,
                  timeout=None, retries=0):
        self.calls += 1
        self.inputs.append(text)
        if self.calls <= self.failures:
            return "模型暂时没有给出中文"
        tokens = TOKEN_RE.findall(text)
        unprotected = TOKEN_RE.sub("", text)
        # Produce enough Chinese to satisfy the bounded length sanity check;
        # protection tokens are returned once and restored by the service.
        chinese = "译" * max(4, len(unprotected.strip()) // 2)
        return ProviderResponse(" ".join(tokens + [chinese]), model="test-model", usage={"completion_tokens": 8})


class MissingTokenProvider(TranslationProvider):
    def __init__(self):
        self.calls = 0

    @property
    def provider_id(self):
        return "test:bad-token"

    def translate(self, text, source_language, target_language, glossary=None,
                  timeout=None, retries=0):
        self.calls += 1
        return "数字被模型删除了"


class CopyProvider(TranslationProvider):
    @property
    def provider_id(self):
        return "test:copy"

    def translate(self, text, source_language, target_language, glossary=None,
                  timeout=None, retries=0):
        return text


class RecoveringProvider(DeterministicProvider):
    def __init__(self, unavailable_calls):
        super().__init__()
        self.unavailable_calls = unavailable_calls

    def translate(self, text, source_language, target_language, glossary=None,
                  timeout=None, retries=0):
        if self.calls < self.unavailable_calls:
            self.calls += 1
            raise TranslationProviderError("模型忙", retryable=True, code="model_busy")
        return super().translate(text, source_language, target_language, glossary, timeout, retries)


class FakeOllamaClient:
    configured = True
    model = "qwen-translate:test"

    def __init__(self):
        self.calls = []

    def chat_json(self, system_prompt, user_prompt, **kwargs):
        self.calls.append((system_prompt, user_prompt, kwargs))
        return {
            "json": {"translation": "这是本地模型译文。"},
            "model": self.model,
            "usage": {"prompt_tokens": 12, "completion_tokens": 6},
            "finish_reason": "stop",
        }


def foreign_document(text, title="中文标题"):
    return {
        "content_sha256": "content-hash",
        "source": {"path": "docs/report.txt", "sha256": "source-hash"},
        "structure": {"title": title},
        "text": text,
    }


class TranslationCoreTests(unittest.TestCase):
    def test_language_detection_routes_chinese_and_common_foreign_scripts(self):
        self.assertEqual(detect_language("这是一份中文资料，包含完整说明。") ["language"], "zh")
        self.assertFalse(detect_language("这是一份中文资料，包含完整说明。") ["needs_translation"])
        self.assertEqual(detect_language("This is the report and the result of the study.") ["language"], "en")
        self.assertTrue(detect_language("This is the report and the result of the study.") ["needs_translation"])
        self.assertEqual(detect_language("これは日本語の文書です。") ["language"], "ja")
        self.assertEqual(detect_language("Это русский документ.") ["language"], "ru")
        self.assertEqual(detect_language("यह एक हिंदी दस्तावेज़ है।") ["language"], "other")
        self.assertTrue(detect_language("นี่คือเอกสารภาษาไทย") ["needs_translation"])
        mixed = detect_language("中文说明后附 an English section with enough foreign words for review.")
        self.assertEqual(mixed["language"], "mixed")
        self.assertTrue(mixed["needs_translation"])

    def test_segmentation_is_bounded_and_lossless(self):
        text = ("First sentence. " * 25) + "\n\n" + ("第二段内容。" * 30)
        segments = segment_text(text, max_chars=150)
        self.assertGreater(len(segments), 2)
        self.assertEqual("".join(item["text"] for item in segments), text)
        self.assertTrue(all(len(item["text"]) <= 150 for item in segments))
        self.assertEqual(segments[0]["start"], 0)
        self.assertEqual(segments[-1]["end"], len(text))

    def test_protection_covers_glossary_names_numbers_dates_and_placeholders(self):
        source = "OpenAI revenue was 12.5% on 2024-01-02 for {{customer}} at user@example.com."
        protected = protect_text(source, {"revenue": "收入"})
        self.assertNotIn("revenue", protected.text)
        self.assertGreaterEqual(len(protected.replacements), 6)
        translated = "译文 " + " ".join(protected.replacements)
        restored = protected.restore(translated)
        for expected in ("OpenAI", "收入", "12.5%", "2024-01-02", "{{customer}}", "user@example.com"):
            self.assertIn(expected, restored)

    def test_protection_does_not_freeze_uppercase_headings_or_partial_words(self):
        protected = protect_text(
            "THIS QUARTERLY REPORT COVERS REVENUE AND API RESULTS.",
            {"art": "艺术"},
        )
        self.assertIn("THIS QUARTERLY REPORT", protected.text)
        self.assertNotIn("__SJFX_TERM", protected.text)
        self.assertNotIn("API", protected.text)
        self.assertEqual(list(protected.kinds.values()), ["acronym"])
        adjacent = protect_text("使用AI模型", {"AI": "人工智能"})
        self.assertNotIn("AI", adjacent.text)
        self.assertIn("人工智能", adjacent.restore(adjacent.text))

    def test_foreign_document_returns_original_and_verified_chinese_versions(self):
        provider = DeterministicProvider()
        service = TranslationService(provider, policy=TranslationPolicy(max_unit_chars=300, max_attempts=2))
        source = "The quarterly revenue was 12.5% for OpenAI on 2024-01-02."
        result = service.translate_document(foreign_document(source), glossary={"revenue": "收入"})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["original_text"], source)
        self.assertIsNotNone(result["translated_text"])
        self.assertIn("收入", result["translated_text"])
        self.assertIn("12.5%", result["translated_text"])
        self.assertIn("OpenAI", result["translated_text"])
        self.assertTrue(all(unit["qa"]["passed"] for unit in result["units"] if unit["status"] == "completed"))

    def test_chinese_document_does_not_need_a_model_and_keeps_original(self):
        service = TranslationService(UnavailableTranslationProvider())
        result = service.translate_document(foreign_document("这是已经是中文的正文。"))
        self.assertEqual(result["status"], "not_required")
        self.assertEqual(result["translated_text"], result["original_text"])
        self.assertEqual(result["progress"]["required_units"], 0)

    def test_unavailable_model_never_masquerades_foreign_source_as_translation(self):
        service = TranslationService(UnavailableTranslationProvider("模型离线"))
        result = service.translate_document(foreign_document("This foreign report needs translation."))
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["translated_text"])
        failed = next(unit for unit in result["units"] if unit["status"] == "failed")
        self.assertEqual(failed["error"]["code"], "provider_unavailable")
        self.assertFalse(failed["retryable"])

    def test_translation_memory_and_checkpoint_make_reruns_idempotent(self):
        memory = InMemoryTranslationMemory()
        first_provider = DeterministicProvider()
        document = foreign_document("The project is ready for delivery.")
        first = TranslationService(first_provider, memory=memory).translate_document(document)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first_provider.calls, 1)

        second_provider = DeterministicProvider()
        second = TranslationService(second_provider, memory=memory).translate_document(document, resume_state=first)
        self.assertEqual(second["translated_text"], first["translated_text"])
        self.assertEqual(second_provider.calls, 0)
        reused = next(unit for unit in second["units"] if unit["translation_required"])
        self.assertIn(reused["reused_from"], {"checkpoint", "translation_memory"})

    def test_failed_checkpoint_can_be_retried_after_provider_recovers(self):
        provider = RecoveringProvider(unavailable_calls=2)
        service = TranslationService(provider, policy=TranslationPolicy(max_attempts=2))
        document = foreign_document("The delayed report still requires a verified translation.")
        failed = service.translate_document(document)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["units"][-1]["error"]["code"], "model_busy")

        recovered = service.translate_document(document, resume_state=failed)
        self.assertEqual(recovered["status"], "completed")
        self.assertIsNotNone(recovered["translated_text"])

    def test_cancelled_run_is_checkpointable_and_resumable(self):
        provider = DeterministicProvider()
        service = TranslationService(provider)
        document = foreign_document("The report should wait until the worker resumes.")
        cancelled = service.translate_document(document, cancel_check=lambda: True)
        self.assertTrue(cancelled["cancelled"])
        self.assertEqual(cancelled["status"], "partial")
        self.assertEqual(provider.calls, 0)

        resumed = service.translate_document(document, resume_state=cancelled)
        self.assertEqual(resumed["status"], "completed")

    def test_budgeted_run_resumes_remaining_large_document_units(self):
        body = ("This is a long foreign paragraph for bounded translation. " * 8) + "\n\n" + (
            "Another foreign paragraph remains available for later work. " * 8
        )
        provider = DeterministicProvider()
        memory = InMemoryTranslationMemory()
        service = TranslationService(
            provider, memory=memory,
            policy=TranslationPolicy(max_unit_chars=180, max_attempts=2),
        )
        checkpoints = []
        first = service.translate_document(
            foreign_document(body), max_units=1,
            checkpoint_callback=lambda state: checkpoints.append(state),
        )
        self.assertEqual(first["status"], "partial")
        self.assertEqual(first["progress"]["completed_units"], 1)
        self.assertGreater(first["progress"]["pending_units"], 0)
        self.assertGreaterEqual(len(checkpoints), 2)

        second = service.translate_document(foreign_document(body), resume_state=first)
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["original_text"], body)
        self.assertIsNotNone(second["translated_text"])

    def test_long_document_uses_bounded_full_state_checkpoints(self):
        class CountingTranslationService(TranslationService):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.result_calls = 0

            def _result(self, *args, **kwargs):
                self.result_calls += 1
                return super()._result(*args, **kwargs)

        body = (
            "This foreign paragraph contains enough English words for "
            "verified translation and checkpoint coverage.\n\n"
        ) * 80
        provider = DeterministicProvider()
        policy = TranslationPolicy(
            max_unit_chars=128,
            checkpoint_unit_interval=1,
            checkpoint_interval_seconds=3600,
            max_intermediate_checkpoints=4,
        )
        service = CountingTranslationService(provider, policy=policy)
        checkpoints = []

        result = service.translate_document(
            foreign_document(body),
            checkpoint_callback=lambda state: checkpoints.append(state),
        )

        self.assertEqual(result["status"], "completed")
        self.assertGreater(provider.calls, 20)
        # One initial plan, at most the configured intermediate snapshots,
        # and one terminal result. In particular, _result is not called for
        # every translated unit and the terminal result is not deep-copied.
        self.assertLessEqual(len(checkpoints), policy.max_intermediate_checkpoints + 2)
        self.assertEqual(service.result_calls, len(checkpoints))
        self.assertLess(service.result_calls, provider.calls)
        self.assertIs(checkpoints[-1], result)
        self.assertEqual(checkpoints[0]["progress"]["completed_units"], 0)
        self.assertEqual(checkpoints[-1]["progress"]["pending_units"], 0)

    def test_time_triggered_checkpoints_also_obey_the_hard_cap(self):
        body = (
            "This is another foreign paragraph that must be translated safely.\n\n"
        ) * 24
        provider = DeterministicProvider()
        policy = TranslationPolicy(
            max_unit_chars=128,
            checkpoint_unit_interval=10000,
            checkpoint_interval_seconds=1,
            max_intermediate_checkpoints=2,
        )
        service = TranslationService(provider, policy=policy)
        checkpoints = []
        ticks = iter(range(0, 10000, 2))

        with mock.patch("services.translation.time.monotonic", side_effect=lambda: next(ticks)):
            result = service.translate_document(
                foreign_document(body),
                checkpoint_callback=lambda state: checkpoints.append(state),
            )

        self.assertEqual(result["status"], "completed")
        self.assertGreater(provider.calls, policy.max_intermediate_checkpoints)
        self.assertEqual(len(checkpoints), policy.max_intermediate_checkpoints + 2)

    def test_chunk_boundaries_preserve_original_outer_whitespace(self):
        source = "  The report is ready for review.\n\n"
        result = TranslationService(DeterministicProvider()).translate_document(foreign_document(source))
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["translated_text"].startswith("  "))
        self.assertTrue(result["translated_text"].endswith("\n\n"))

    def test_unchanged_japanese_is_not_accepted_as_chinese_translation(self):
        service = TranslationService(CopyProvider(), policy=TranslationPolicy(max_attempts=1))
        result = service.translate_document(foreign_document("これは翻訳が必要な日本語の文書です。"))
        self.assertEqual(result["status"], "failed")
        failed = next(unit for unit in result["units"] if unit["status"] == "failed")
        self.assertEqual(failed["error"]["code"], "source_copied_without_translation")

    def test_quality_failure_is_corrected_by_local_reviewer(self):
        reviewer = DeterministicProvider()
        service = TranslationService(
            CopyProvider(), reviewer=reviewer,
            policy=TranslationPolicy(max_attempts=1),
        )
        result = service.translate_document(
            foreign_document("This draft must be corrected into verified Chinese.")
        )
        self.assertEqual(result["status"], "completed")
        corrected = next(unit for unit in result["units"] if unit["translation_required"])
        self.assertEqual(corrected["reviewed_by"], "test-model")
        self.assertEqual(reviewer.calls, 1)
        self.assertTrue(corrected["qa"]["passed"])

    def test_missing_protected_number_is_retried_then_rejected(self):
        provider = MissingTokenProvider()
        service = TranslationService(provider, policy=TranslationPolicy(max_attempts=2))
        result = service.translate_document(foreign_document("Revenue reached 2024 million dollars."))
        self.assertEqual(provider.calls, 2)
        self.assertEqual(result["status"], "failed")
        failed = next(unit for unit in result["units"] if unit["status"] == "failed")
        self.assertEqual(failed["error"]["code"], "protected_token_mismatch")
        self.assertIsNone(failed["target_text"])

    def test_glossary_and_contract_are_part_of_memory_key(self):
        source = "revenue"
        first = translation_memory_key(source, "en", glossary={"revenue": "收入"})
        second = translation_memory_key(source, "en", glossary={"revenue": "营收"})
        third = translation_memory_key(source, "en", glossary={"revenue": "收入"}, contract_version="v2")
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, third)
        self.assertEqual(glossary_fingerprint({"b": "乙", "a": "甲"}), glossary_fingerprint({"a": "甲", "b": "乙"}))

    def test_service_contract_upgrade_invalidates_old_translation_memory(self):
        memory = InMemoryTranslationMemory()
        document = foreign_document("The report is complete and ready for delivery.")
        first_provider = DeterministicProvider()
        first = TranslationService(
            first_provider, memory=memory, policy=TranslationPolicy(contract_version="v1")
        ).translate_document(document)
        self.assertEqual(first["status"], "completed")

        second_provider = DeterministicProvider()
        second = TranslationService(
            second_provider, memory=memory, policy=TranslationPolicy(contract_version="v2")
        ).translate_document(document, resume_state=first)
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second_provider.calls, 1)

    def test_plan_counts_only_foreign_units(self):
        document = foreign_document("中文段落。\n\nThis paragraph must be translated.")
        plan = build_translation_plan(document, max_unit_chars=200)
        self.assertTrue(plan["translation_required"])
        self.assertGreaterEqual(plan["required_unit_count"], 1)
        self.assertLess(plan["required_unit_count"], plan["unit_count"])

    def test_units_preserve_heading_paragraph_table_and_footnote_metadata(self):
        document = foreign_document(
            "Executive Summary\n\nThe plan was approved.\n\nName\tAmount\nAlice\t$1,200\n\n[1] External audit note."
        )
        document["structure"]["headings"] = ["Executive Summary"]
        units = build_translation_units(document, max_unit_chars=128)
        body_units = [unit for unit in units if unit["kind"] == "body"]
        self.assertEqual("".join(unit["source_text"] for unit in body_units), document["text"])
        self.assertIn("heading", {unit["block_kind"] for unit in body_units})
        self.assertIn("table", {unit["block_kind"] for unit in body_units})
        self.assertIn("footnote", {unit["block_kind"] for unit in body_units})
        self.assertTrue(all(unit["paragraph_index"] for unit in body_units))
        self.assertTrue(any(unit["section"] == "Executive Summary" for unit in body_units[1:]))

    def test_money_and_table_structure_are_hard_quality_constraints(self):
        protected = protect_text("Name\tAmount\nAlice\t$1,200")
        self.assertIn("amount", set(protected.kinds.values()))
        restored = protected.restore("姓名 金额 " + " ".join(protected.replacements))
        qa = validate_translation(
            "Name\tAmount\nAlice\t$1,200", protected,
            "姓名 金额 " + " ".join(protected.replacements), restored, "en",
        )
        self.assertFalse(qa["passed"])
        self.assertIn("table_structure_changed", qa["errors"])

    def test_ollama_adapter_uses_strict_local_json_contract(self):
        client = FakeOllamaClient()
        provider = OllamaTranslationProvider(client)
        response = provider.translate("This is a report.", "en", "zh-CN")
        self.assertEqual(response.text, "这是本地模型译文。")
        self.assertEqual(response.model, client.model)
        self.assertEqual(len(client.calls), 1)
        system_prompt, user_prompt, kwargs = client.calls[0]
        self.assertIn("不得总结", system_prompt)
        self.assertIn('{"translation":', system_prompt)
        self.assertIn("This is a report.", user_prompt)
        self.assertEqual(kwargs["required_fields"], ["translation"])


if __name__ == "__main__":
    unittest.main()
