import tempfile
import unittest
import zipfile
from pathlib import Path

from docx import Document

from services.exporter import create_report_docx, xml_safe_text


class ReportXmlSafetyTests(unittest.TestCase):
    def test_xml_safe_text_filters_invalid_unicode_but_keeps_layout_chars(self):
        raw = (
            "A\x00B\x01C\tD\nE\rF\x7fG\x85H\ud800I\ufffeJ\ufdd0K"
            + chr(0x1FFFE)
            + "L😀"
        )
        cleaned = xml_safe_text(raw)
        for invalid in ("\x00", "\x01", "\x7f", "\x85", "\ud800", "\ufffe", "\ufdd0", chr(0x1FFFE)):
            self.assertNotIn(invalid, cleaned)
        self.assertIn("\t", cleaned)
        self.assertIn("\n", cleaned)
        self.assertIn("\r", cleaned)
        self.assertIn("😀", cleaned)

    def test_report_generation_sanitizes_every_dynamic_text_path(self):
        bad = "\x00\x01\x0b\ud800\ufffe\ufdd0"
        evidence = {
            "evidence_id": "E" + bad + "-1",
            "source_path": "资料" + bad + ".txt",
            "page": 1,
            "section": "章节" + bad,
            "text": "证据正文" + bad + "仍可阅读",
        }
        conclusion = {
            "statement": "结论" + bad,
            "confidence": "高" + bad,
            "basis": "依据" + bad,
            "evidence": [evidence],
        }
        report = {
            "generation_mode": "model_analyzed",
            "basic_information": ["基本信息" + bad, "保留\t制表\n换行"],
            "classification_coverage": {
                "source": "adaptive_analysis_tree",
                "top_level_category_count": 1,
                "classified_file_count": 1,
                "parsed_file_count": 1,
                "complete": True,
            },
            "global_categories": [{
                "name": "分类" + bad,
                "dimension": "维度" + bad,
                "file_count": 1,
                "description": "分类说明" + bad,
                "type_counts": {".txt" + bad: 1},
                "topics": ["主题" + bad],
                "representative_documents": ["代表文档" + bad],
                "conclusion_evidence": [conclusion],
                "subcategories": [{
                    "name": "子主题" + bad,
                    "dimension": "内容主题" + bad,
                    "file_count": 1,
                    "description": "子主题说明" + bad,
                    "topics": ["线索" + bad],
                    "representative_documents": ["子文档" + bad],
                    "conclusion_evidence": [conclusion],
                }],
                "evidence_chain": [evidence],
            }],
            "key_findings": ["关键发现" + bad],
            "recommended_research_direction": {
                "title": "推荐方向" + bad,
                "priority": "高" + bad,
                "confidence": "中" + bad,
                "rationale": "推荐理由" + bad,
                "research_questions": ["研究问题" + bad],
                "methods": ["研究方法" + bad],
                "evidence_chain": [evidence],
            },
            "directions": [{
                "direction": "其他方向" + bad,
                "type": "推论" + bad,
                "confidence": "低" + bad,
                "confidence_note": "置信度说明" + bad,
                "evidence_chain": [evidence],
            }],
            "analysis_method": {
                "parse": "统一解析" + bad,
                "deduplication": "精确去重" + bad,
                "similarity": "相似聚类" + bad,
                "retrieval": "证据检索" + bad,
                "classification": "自适应分类" + bad,
                "traceability": "证据回溯" + bad,
            },
        }
        scan = {
            "root": "/tmp/数据" + bad,
            "file_count": 1,
            "directory_count": 1,
            "total_size_human": "1 KB" + bad,
            "scanned_at": "2026-08-20" + bad,
        }
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "report.docx"
            create_report_docx(report, scan, output)
            self.assertTrue(output.is_file())
            document = Document(str(output))
            paragraph_text = "\n".join(paragraph.text for paragraph in document.paragraphs)
            self.assertIn("基本信息", paragraph_text)
            self.assertIn("仍可阅读", paragraph_text)
            self.assertIn("保留\t制表\n换行", paragraph_text)
            with zipfile.ZipFile(output) as archive:
                for name in archive.namelist():
                    if name.endswith(".xml"):
                        payload = archive.read(name)
                        self.assertNotIn(b"\x00", payload)
                        payload.decode("utf-8")


if __name__ == "__main__":
    unittest.main()
