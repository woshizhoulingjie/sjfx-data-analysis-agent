import unittest

from services.evidence import evidence_quality, select_evidence, verify_claim_evidence
from services.package_analysis import _first_evidence


class EvidenceQualityRegressionTests(unittest.TestCase):
    def test_navigation_and_question_text_are_not_evidence(self):
        samples = [
            {"label": "title", "text": "开源软件有哪些优势.docx"},
            {"label": "heading", "text": "开源软件的特点与主要优势"},
            {"label": "paragraph", "text": "开源软件究竟有哪些主要优势和应用价值？"},
        ]
        self.assertTrue(all(not evidence_quality(item)["eligible"] for item in samples))

    def test_claim_selection_keeps_supporting_body_and_drops_irrelevant_body(self):
        items = [
            {"evidence_id": "E-title", "label": "title", "text": "开源软件有哪些优势.docx"},
            {"evidence_id": "E-question", "label": "paragraph", "text": "开源软件有哪些优势？"},
            {
                "evidence_id": "E-body",
                "label": "paragraph",
                "source_path": "open-source.docx",
                "text": "开源软件允许用户查看、修改和再分发源代码，因此能够降低采购成本，并提高系统的可定制性和透明度。",
            },
            {
                "evidence_id": "E-unrelated",
                "label": "paragraph",
                "source_path": "inspection.docx",
                "text": "该公司在2024年完成了三次厂区安全检查，并调整了夜间值班制度。",
            },
        ]
        selected = select_evidence(items, topics=["开源软件有哪些优势"], max_items=8)
        self.assertEqual([item["evidence_id"] for item in selected], ["E-body"])
        self.assertEqual(selected[0]["support_type"], "直接证据")
        self.assertIn("降低采购成本", selected[0]["supporting_quote"])
        self.assertTrue(selected[0]["supports_claim"])

    def test_compatibility_representative_skips_first_title(self):
        document = {
            "source": {"path": "open-source.docx", "sha256": "a" * 64},
            "structure": {"title": "开源软件有哪些优势", "headings": ["主要特点"]},
            "evidence": [
                {"evidence_id": "E-title", "label": "title", "text": "开源软件有哪些优势.docx"},
                {
                    "evidence_id": "E-body",
                    "label": "paragraph",
                    "source_path": "open-source.docx",
                    "text": "开源软件通过公开源代码支持审查和修改，从而提高透明度与可定制性。",
                },
            ],
        }
        selected = _first_evidence(document)
        self.assertEqual(selected["evidence_id"], "E-body")
        self.assertTrue(selected["evidence_quality"]["eligible"])

    def test_numeric_claim_requires_the_same_number_in_evidence(self):
        result = verify_claim_evidence(
            "系统准确率达到95%",
            {
                "label": "paragraph",
                "text": "实验结果显示系统准确率达到90%，并满足基本性能要求。",
            },
        )
        self.assertEqual(result["support_status"], "insufficient")

    def test_related_but_weak_paragraph_is_not_direct_support(self):
        result = verify_claim_evidence(
            "远程证明能够完全保证系统安全",
            {
                "label": "paragraph",
                "text": "系统包含远程证明模块，并提供运行状态查询接口。",
            },
        )
        self.assertNotEqual(result["support_status"], "supported")


if __name__ == "__main__":
    unittest.main()
