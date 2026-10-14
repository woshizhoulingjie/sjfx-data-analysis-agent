"""Real Unicode files remain searchable without embeddings or character vectors."""
import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services import large_qa_thai as thai
from services.retrieval import _tokens, build_retrieval_manifest, retrieve_evidence
from services.unified_parser import UnifiedDocumentParser


THAI_TEXT = (
    "แผนการตรวจสอบคลังสินค้าประจำเดือนตุลาคม ทีมงานต้องตรวจสอบรายการสินค้าในคลังสินค้า"
    "และบันทึกผลการตรวจสอบ หากพบข้อผิดพลาดให้กำหนดผู้รับผิดชอบและวันที่แก้ไข "
    "จากนั้นตรวจสอบความถูกต้องอีกครั้งก่อนส่งรายงานสรุปให้ผู้จัดการ"
)


class OrdinaryThaiRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="sjfx-thai-retrieval-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.documents = {}
        self.parser = UnifiedDocumentParser()

    def add(self, path, text):
        source = self.root / path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(text, encoding="utf-8")
        self.documents[path] = self.parser.parse(source, relative_path=path, mode="fast")

    def query(self, query, **kwargs):
        with patch("services.retrieval._tfidf_scores", side_effect=AssertionError("character vectors disabled")):
            return retrieve_evidence(self.documents, query, use_tfidf=False,
                                     semantic_scores=None, **kwargs)

    def test_real_thai_file_retrieves_substring_without_spaces_and_keeps_source_identity(self):
        self.add("ไทย/แผนงาน.txt", THAI_TEXT)
        self.add("English/unrelated.txt", "The maintenance team inspects hydraulic pressure readings before the October acceptance report.")
        result = self.query("คลังสินค้า")
        self.assertEqual(result["method"], "BM25")
        self.assertFalse(result["warnings"])
        hit = result["results"][0]
        self.assertEqual(hit["source_path"], "ไทย/แผนงาน.txt")
        self.assertIn("คลังสินค้า", hit["text"])
        self.assertTrue(hit["evidence_id"])
        self.assertEqual(hit["source_sha256"], self.documents[hit["source_path"]]["source"]["sha256"])
        self.assertEqual(hit["vector_score"], 0)
        self.assertFalse(hit["preview_only"])

    def test_exact_surface_substring_recovers_contextual_segmentation_and_respects_scope(self):
        # The bundled tokenizer splits this source as งบ + ประมาณการ,
        # while the isolated query งบประมาณ is a different dictionary word.
        text = "งบประมาณการชำระเงิน " + THAI_TEXT
        self.add("outside/แผน.txt", text)
        self.add("inside/แผน.txt", text)
        self.assertNotIn("งบประมาณ", _tokens(text))
        result = self.query("งบประมาณ", scope="inside", top_k=1)
        self.assertEqual([hit["source_path"] for hit in result["results"]], ["inside/แผน.txt"])
        self.assertIn("งบประมาณ", result["results"][0]["text"])
        evidence_id = result["results"][0]["evidence_id"]
        self.assertFalse(self.query("งบประมาณ", candidate_evidence_ids=["not-authorized"])["results"])
        self.assertEqual(self.query("งบประมาณ", scope="inside", candidate_evidence_ids=[evidence_id])["result_count"], 1)

    def test_surface_matching_preserves_tones_and_does_not_cross_spaces(self):
        self.add("ไทย/ข้อความ.txt", "ป่า งบ ประมาณ " + THAI_TEXT)
        self.assertTrue(self.query("ป่า")["results"])
        self.assertFalse(self.query("ปา")["results"])
        self.assertFalse(self.query("งบประมาณ")["results"])
        self.assertIn("ไม่", _tokens("ไม่อนุมัติ"))

    def test_non_thai_text_keeps_original_chinese_english_tokens_and_ranking(self):
        with patch.object(thai, "_provider", side_effect=AssertionError("Thai provider unnecessary")):
            self.assertEqual(_tokens("Budget approval 预算付款"),
                             ["budget", "approval", "预算付款", "预算", "算付", "付款", "预算付", "算付款"])
            self.add("a.txt", "The hydraulic pressure inspection requires comparing all pressure readings with the approved operating range.")
            self.add("b.txt", "The warehouse inspection team records all inventory corrections and verifies the final acceptance report.")
            self.assertEqual(self.query("hydraulic pressure")["results"][0]["source_path"], "a.txt")

    def test_missing_bundled_dictionary_is_visible_without_download_or_losing_english(self):
        self.add("ไทย/ข้อความ.txt", THAI_TEXT + " Budget approval is required before the inspection.")
        thai._provider.cache_clear()
        self.addCleanup(thai._provider.cache_clear)
        with patch.object(thai.resources, "files", side_effect=FileNotFoundError("bundled dictionary missing")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("offline retrieval must not download")):
            found = self.query("Budget approval")
            self.assertTrue(found["results"])
            self.assertTrue(any("泰文关键词检索暂不可用" in warning for warning in found["warnings"]))
            self.assertTrue(any("FileNotFoundError" in warning for warning in found["warnings"]))
            missing = self.query("คลังสินค้า")
            self.assertFalse(missing["results"])
            self.assertTrue(any("泰文关键词检索暂不可用" in warning for warning in missing["warnings"]))

    def test_tokenizer_resource_errors_are_not_reported_as_a_missing_dictionary(self):
        self.add("ไทย/ข้อความ.txt", THAI_TEXT)
        with patch.object(thai, "_provider", side_effect=OSError(errno.ENOSPC, "disk full")):
            with self.assertRaises(OSError):
                self.query("คลังสินค้า")

    def test_thai_auto_queries_use_unicode_words(self):
        self.add("ไทย/ข้อความ.txt", THAI_TEXT)
        manifest = build_retrieval_manifest(self.documents)
        self.assertTrue(manifest["queries"])
        self.assertTrue(thai.has_thai(manifest["queries"][0]["query"]))
        self.assertTrue(manifest["queries"][0]["results"])
