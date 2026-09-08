import tempfile
import unittest
from pathlib import Path

from services.homogeneous_documents import (
    analyze_homogeneous_documents,
    extract_record,
)
from services.storage import Storage


def document(text, name):
    return {
        "source": {"name": name, "sha256": name},
        "text": text,
        "structure": {"headings": []},
        "evidence": [],
    }


class HomogeneousDocumentAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.documents = [
            {
                "path": "letters/a.txt",
                "payload": document(
                    """文件编号：甲函〔2026〕1号
日期：2026年3月1日
发件单位：甲单位
收件单位：乙单位
主题：合同付款安排
事项编号：HT-100
请贵方在十个工作日内回复付款安排。""",
                    "a.txt",
                ),
            },
            {
                "path": "letters/b.txt",
                "payload": document(
                    """文件编号：乙复〔2026〕8号
日期：2026年3月5日
发件单位：乙单位
收件单位：甲单位
主题：合同付款安排
事项编号：HT-100
关于贵方来函甲函〔2026〕1号，现答复如下：付款正在审批。""",
                    "b.txt",
                ),
            },
            {
                "path": "letters/c.txt",
                "payload": document(
                    """文件编号：甲函〔2026〕12号
日期：2026年3月20日
发件单位：甲单位
收件单位：乙单位
主题：合同付款安排
事项编号：HT-100
我方尚未收到付款，再次催办并请尽快处理。""",
                    "c.txt",
                ),
            },
        ]

    def test_extracts_labelled_fields_with_evidence(self):
        record = extract_record(self.documents[0]["path"], self.documents[0]["payload"])
        self.assertEqual(record["fields"]["sender"], "甲单位")
        self.assertEqual(record["fields"]["recipient"], "乙单位")
        self.assertEqual(record["fields"]["date"], "2026-03-01")
        self.assertEqual(record["fields"]["matter_id"], "HT-100")
        self.assertIn("sender", record["field_evidence"])
        self.assertIn("提出请求", record["summary"])

    def test_content_understanding_is_evidence_bound(self):
        record = extract_record("letters/request.txt", document(
            """文件编号：甲函〔2026〕1号
日期：2026年3月1日
发件单位：甲单位
收件单位：乙单位
主题：合同付款安排
回复期限：十个工作日
请贵方在十个工作日内回复付款安排。
目前付款正在审批。""",
            "request.txt",
        ))
        understanding = record["content_understanding"]
        self.assertEqual(record["action"], "提出请求")
        self.assertEqual(understanding["document_role"], "request")
        self.assertTrue(understanding["response_requested"])
        self.assertIn("请贵方", understanding["requested_action"])
        self.assertIn("目前付款正在审批", understanding["key_facts"])
        self.assertNotIn("回复期限", understanding["key_conclusions"])

    def test_content_conclusions_include_original_support(self):
        record = extract_record("letters/conclusion.txt", document(
            "文件编号：甲函〔2026〕1号\n"
            "发件单位：甲单位\n收件单位：乙单位\n主题：付款安排\n"
            "请贵方在十个工作日内回复付款安排。\n目前付款正在审批。",
            "conclusion.txt",
        ))
        conclusions = record["content_understanding"]["conclusions"]
        self.assertTrue(conclusions)
        action = next(item for item in conclusions if item["type"] == "动作")
        self.assertEqual(action["support_level"], "direct")
        self.assertIn("十个工作日", action["supports"][0]["text"])
        self.assertGreaterEqual(action["supports"][0]["char_end"], action["supports"][0]["char_start"])
        self.assertTrue(any(item["type"] == "事实" and "付款正在审批" in item["text"] for item in conclusions))

    def test_builds_reply_followup_and_case_timeline(self):
        result = analyze_homogeneous_documents(self.documents)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["document_count"], 3)
        self.assertGreaterEqual(result["stable_field_count"], 6)
        relation_types = {item["relation_type"] for item in result["relations"]}
        self.assertIn("reply_to", relation_types)
        self.assertIn("same_matter", relation_types)
        self.assertEqual(len(result["cases"]), 1)
        self.assertEqual(result["cases"][0]["document_count"], 3)
        self.assertEqual(result["cases"][0]["timeline"][0]["date"], "2026-03-01")

    def test_rejects_batch_without_common_structure(self):
        result = analyze_homogeneous_documents([
            {"path": "a.txt", "payload": document("完全自由的正文。", "a.txt")},
            {"path": "b.txt", "payload": document("另一篇没有字段标签的文章。", "b.txt")},
        ])
        self.assertFalse(result["eligible"])
        self.assertIn("未发现至少两个稳定公共字段", result["eligibility_reasons"])

    def test_discovers_repeated_custom_label_value_fields(self):
        result = analyze_homogeneous_documents([
            {"path": "x.txt", "payload": document("业务区域：华东\n工单等级：紧急\n处理说明正文。", "x.txt")},
            {"path": "y.txt", "payload": document("业务区域：华南\n工单等级：普通\n另一份处理说明。", "y.txt")},
        ])
        self.assertTrue(result["eligible"])
        custom = {item["label"]: item for item in result["schema_fields"] if item.get("custom")}
        self.assertEqual(custom["业务区域"]["coverage"], 1.0)
        self.assertEqual(custom["工单等级"]["coverage"], 1.0)
        self.assertEqual(result["records"][0]["custom_fields"]["业务区域"], "华东")

    def test_storage_round_trip_is_paged_and_scan_cascades(self):
        result = analyze_homogeneous_documents(self.documents)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            storage = Storage(root / "agent.db", root / "sidecars", 1024)
            storage.save_scan({"root": str(root), "tree": {}, "file_count": 3}, scan_id="scan-h", owner_id="owner")
            counts = storage.save_homogeneous_analysis("scan-h", result)
            self.assertEqual(counts["records"], 3)
            page = storage.get_homogeneous_analysis("scan-h", offset=0, limit=2)
            self.assertEqual(page["records"]["total"], 3)
            self.assertEqual(len(page["records"]["items"]), 2)
            self.assertEqual(page["records"]["next_offset"], 2)
            detail = storage.get_homogeneous_record("scan-h", "letters/b.txt")
            self.assertEqual(detail["record"]["fields"]["document_number"], "乙复〔2026〕8号")
            self.assertTrue(detail["relations"])
            catalog = storage.get_relationship_catalog("scan-h")
            self.assertEqual(catalog["schema_version"], "relationship-catalog/1.0")
            self.assertTrue(catalog["items"])
            self.assertEqual(catalog["items"][0]["calibration"], "validated")
            storage.delete_scan("scan-h", owner_id="owner")
            self.assertIsNone(storage.get_homogeneous_analysis("scan-h"))

    def test_frontend_exposes_independent_module(self):
        project = Path(__file__).resolve().parents[1]
        template = (project / "templates" / "index.html").read_text(encoding="utf-8")
        shell = (project / "static" / "product-shell.js").read_text(encoding="utf-8")
        client = (project / "static" / "homogeneous-analysis.js").read_text(encoding="utf-8")
        self.assertIn('data-route="homogeneous"', template)
        self.assertIn('data-view="homogeneous"', template)
        self.assertIn("homogeneous: 'homogeneous'", shell)
        self.assertIn("/api/homogeneous-analysis/", client)
        self.assertIn("文件结论", client)
        self.assertIn("原文支撑", client)
        self.assertIn("support_level", client)


    def test_shared_subject_different_matters_stays_candidate(self):
        result = analyze_homogeneous_documents([
            {"path": "a.txt", "payload": document(
                "文件编号：甲函〔2026〕1号\n日期：2026年1月1日\n发件单位：甲公司\n收件单位：乙公司\n主题：付款安排\n事项编号：A-1\n请处理。", "a.txt")},
            {"path": "b.txt", "payload": document(
                "文件编号：丙函〔2026〕2号\n日期：2026年1月2日\n发件单位：丙公司\n收件单位：丁公司\n主题：付款安排\n事项编号：B-1\n请处理。", "b.txt")},
        ])
        self.assertEqual(len(result["cases"]), 0)
        self.assertTrue(result["relations"])
        self.assertTrue(all(item["relation_status"] == "candidate" for item in result["relations"]))

    def test_own_number_and_reply_are_not_false_anomalies(self):
        result = analyze_homogeneous_documents([
            {"path": "a.txt", "payload": document(
                "文件编号：甲函〔2026〕1号\n日期：2026年1月1日\n发件单位：甲\n收件单位：乙\n主题：测试\n事项编号：A\n请处理。", "a.txt")},
            {"path": "b.txt", "payload": document(
                "文件编号：乙复〔2026〕8号\n日期：2026年1月2日\n发件单位：乙\n收件单位：甲\n主题：测试\n事项编号：A\n关于甲函〔2026〕1号，现答复如下。", "b.txt")},
        ])
        self.assertNotIn("甲函〔2026〕1号", result["records"][0]["references"])
        self.assertFalse(any(item["type"] == "missing_reference" for item in result["anomalies"]))
        self.assertFalse(any(item["type"] == "possible_unanswered" and item["path"] == "b.txt" for item in result["anomalies"]))

    def test_email_headers_entities_and_evidence_are_normalized(self):
        result = analyze_homogeneous_documents([
            {"path": "mail/a.eml", "payload": document(
                "Message-ID: <a@example.com>\n"
                "From: Alice <alice@example.com>\n"
                "To: Bob <bob@example.com>\n"
                "Cc: Carol <carol@example.com>\n"
                "Reply-To: reply@example.com\n"
                "References: <old@example.com>\n"
                "Subject: 足球资料\n"
                "Date: 2026-03-01\n"
                "事项编号：FB-100\n联系电话：13800138000\n发送IP：10.0.0.1",
                "a.eml")},
            {"path": "mail/b.eml", "payload": document(
                "Message-ID: <b@example.com>\n"
                "In-Reply-To: <a@example.com>\n"
                "From: Bob <bob@example.com>\n"
                "To: Alice <alice@example.com>\n"
                "Subject: Re: 足球资料\n"
                "Date: 2026-03-02\n"
                "事项编号：FB-100",
                "b.eml")},
        ])
        record = result["records"][0]
        self.assertEqual(record["record_type"], "email")
        self.assertEqual(record["fields"]["cc"], "Carol <carol@example.com>")
        self.assertEqual(record["fields"]["reply_to"], "reply@example.com")
        self.assertIn("13800138000", record["entity_keys"])
        reply = next(item for item in result["relations"] if item["relation_type"] == "reply_to")
        self.assertGreaterEqual(len(reply["evidence_refs"]), 2)
        self.assertTrue(result["integrity"]["email_record_count"] == 2)

    def test_unrelated_adjacent_correspondence_is_not_linked(self):
        result = analyze_homogeneous_documents([
            {"path": "a.txt", "payload": document(
                "文件编号：甲函〔2026〕1号\n日期：2026年1月1日\n发件单位：甲\n收件单位：乙\n主题：采购设备\n请处理。",
                "a.txt")},
            {"path": "b.txt", "payload": document(
                "文件编号：乙函〔2026〕2号\n日期：2026年6月1日\n发件单位：乙\n收件单位：甲\n主题：人员培训\n请处理。",
                "b.txt")},
        ])
        self.assertFalse(any(item["relation_type"] == "correspondence_flow" for item in result["relations"]))

    def test_mail_channel_runs_without_common_schema_and_aggregates_signals(self):
        result = analyze_homogeneous_documents([
            {"path": "mail/a.eml", "payload": document(
                "Message-ID: <a@example.com>\nFrom: alice@example.com\nTo: bob@example.com\n"
                "Subject: 设备采购\nDate: 2026-04-01\n联系电话：13800138000\n发送IP：10.0.0.1\n请确认采购安排。", "a.eml")},
            {"path": "mail/b.eml", "payload": document(
                "Message-ID: <b@example.com>\nIn-Reply-To: <a@example.com>\n"
                "From: bob@example.com\nTo: alice@example.com\nSubject: Re: 设备采购\n"
                "Date: 2026-04-02\n回复如下：已确认。", "b.eml")},
        ])
        self.assertTrue(result["eligible"])
        self.assertTrue(result["structured_eligible"])
        self.assertTrue(result["relation_eligible"])
        self.assertGreaterEqual(result["metrics"]["unique_entity_count"], 4)
        self.assertGreaterEqual(result["metrics"]["communication_pair_count"], 2)
        self.assertTrue(any(item["relation_type"] == "reply_to" for item in result["relations"]))

    def test_custom_conflict_and_truncation_are_explicit(self):
        base = "文件编号：A1\n日期：2026-01-01\n发件单位：甲\n收件单位：乙\n主题：测试\n客户-编号：X\n客户编号：Y"
        result = analyze_homogeneous_documents([
            {"path": "a.txt", "payload": document(base, "a.txt")},
            {"path": "b.txt", "payload": document(base.replace("A1", "B1"), "b.txt")},
        ])
        custom = [item for item in result["schema_fields"] if item.get("custom")]
        self.assertLessEqual(max(item["coverage"] for item in custom), 1.0)
        self.assertTrue(any(item["type"] == "custom_field_conflict" for item in result["anomalies"]))
        long_text = "文件编号：L1\n日期：2026-01-01\n发件单位：甲\n收件单位：乙\n主题：长文\n" + ("正文。" * 60000)
        long_result = analyze_homogeneous_documents([
            {"path": "long-a.txt", "payload": document(long_text, "long-a.txt")},
            {"path": "long-b.txt", "payload": document(long_text.replace("L1", "L2"), "long-b.txt")},
        ])
        self.assertEqual(long_result["integrity"]["text_truncated_files"], 2)


if __name__ == "__main__":
    unittest.main()
