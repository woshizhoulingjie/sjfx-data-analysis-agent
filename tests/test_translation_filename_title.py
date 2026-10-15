import re
import unittest
from unittest import mock

from services.file_translation import build_file_translation_service
from services.translation import (
    InMemoryTranslationMemory, ProviderResponse, TranslationProvider,
    TranslationProviderError, build_translation_units, translation_memory_key,
)


ENGLISH_BODY = (
    "The document describes the delivery process and the result of the study. "
    "This report is ready for review with the team and contains the complete findings."
)
SPANISH_BODY = "El informe es para la revisión de los resultados y la entrega en una semana."


def filename_document(body=ENGLISH_BODY):
    # This is the existing parsed checkpoint shape: no title-origin metadata.
    return {
        "source": {"name": "translate-en.txt", "path": "docs/translate-en.txt"},
        "structure": {"title": "translate-en", "headings": []},
        "text": body,
    }


class EnglishOnlyProvider(TranslationProvider):
    provider_id = "test:english-only"

    def __init__(self):
        self.languages = []

    def translate(self, text, source_language, target_language="zh-CN", **kwargs):
        self.languages.append(source_language)
        if source_language != "en":
            raise TranslationProviderError(
                "unsupported language", retryable=False, code="unsupported_language",
            )
        tokens = re.findall(r"__SJFX_(?:TERM|KEEP)_\d{4}__", text)
        return ProviderResponse(" ".join(tokens + ["这份文件说明交付流程和完整研究结果。"]),
                                model=self.provider_id)


class FilenameTitleTranslationTests(unittest.TestCase):
    def test_filename_title_is_metadata_and_keys_match_the_planned_languages(self):
        for source in [
            {"name": "translate-en.txt"},
            {"path": "docs/translate-en.txt"},
            {"absolute_path": "C:\\资料\\translate-en.txt"},
        ]:
            with self.subTest(source=source):
                document = filename_document()
                document["source"] = source
                units = build_translation_units(document)
                title, body = units
                self.assertEqual(title["source_language"], "und")
                self.assertEqual(title["status"], "not_required")
                self.assertEqual(title["target_text"], "translate-en")
                self.assertEqual(title["preserved_reason"], "filename_metadata")
                self.assertEqual(body["source_language"], "en")
                self.assertTrue(body["translation_required"])
                for unit in units:
                    self.assertEqual(unit["memory_key"], translation_memory_key(
                        unit["source_text"], unit["source_language"],
                    ))
                self.assertNotEqual(title["memory_key"], translation_memory_key("translate-en", "es"))

    def test_retry_of_old_failed_checkpoint_rebuilds_title_and_executes_body(self):
        document = filename_document()
        provider = EnglishOnlyProvider()
        service = build_file_translation_service(None, None, provider=provider)
        # Reproduce the old planner: the filename's "en" was a Spanish stopword.
        with mock.patch("services.translation._is_filename_title", return_value=False):
            previous = service.translate_document(document)
        self.assertEqual(provider.languages, ["es"])
        self.assertEqual(previous["units"][0]["status"], "failed")
        self.assertFalse(previous["units"][0]["retryable"])
        self.assertEqual(previous["units"][1]["status"], "pending")

        result = service.translate_document(document, resume_state=previous)
        self.assertEqual(provider.languages, ["es", "en"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["source_language"], "en")
        self.assertEqual(result["translated_title"], "translate-en")
        self.assertIn("完整研究结果", result["translated_text"])
        self.assertEqual(result["progress"]["required_units"], 1)
        self.assertEqual(result["units"][0]["source_language"], "und")
        self.assertNotEqual(result["units"][0]["memory_key"], previous["units"][0]["memory_key"])

    def test_spanish_body_remains_spanish_and_is_not_sent_as_english(self):
        provider = EnglishOnlyProvider()
        result = build_file_translation_service(None, None, provider=provider).translate_document(
            filename_document(SPANISH_BODY),
        )
        self.assertEqual(provider.languages, ["es"])
        self.assertEqual(result["source_language"], "es")
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["translated_text"])

    def test_multilingual_body_units_keep_their_own_detection_and_offsets(self):
        body = ENGLISH_BODY + "\n\n" + SPANISH_BODY + "\n\n中文说明 " + ENGLISH_BODY
        units = build_translation_units(filename_document(body))
        body_units = [unit for unit in units if unit["kind"] == "body"]
        self.assertEqual({unit["source_language"] for unit in body_units}, {"en", "es", "zh"})
        self.assertEqual("".join(unit["source_text"] for unit in body_units), body)
        for unit in body_units:
            self.assertEqual(body[unit["start"]:unit["end"]], unit["source_text"])
            self.assertEqual(unit["memory_key"], translation_memory_key(
                unit["source_text"], unit["source_language"],
            ))

    def test_actual_titles_are_not_skipped_even_when_the_filename_matches(self):
        for evidence_kind in ["heading", "title_evidence", "different_filename"]:
            with self.subTest(evidence_kind=evidence_kind):
                document = filename_document()
                document["source"]["name"] = "el informe.pdf"
                document["structure"]["title"] = "el informe"
                if evidence_kind == "heading":
                    document["structure"]["headings"] = ["el informe"]
                elif evidence_kind == "title_evidence":
                    document["evidence"] = [{"label": "title", "text": "el informe"}]
                else:
                    document["source"]["name"] = "document.pdf"
                title = build_translation_units(document)[0]
                self.assertEqual(title["source_language"], "es")
                self.assertTrue(title["translation_required"])
                self.assertIsNone(title["preserved_reason"])

    def test_completed_body_memory_remains_reusable_on_same_file_retry(self):
        provider = EnglishOnlyProvider()
        service = build_file_translation_service(None, InMemoryTranslationMemory(), provider=provider)
        first = service.translate_document(filename_document())
        second = service.translate_document(filename_document(), resume_state=first)
        self.assertEqual(provider.languages, ["en"])
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["units"][1]["reused_from"], "checkpoint")


if __name__ == "__main__":
    unittest.main()
