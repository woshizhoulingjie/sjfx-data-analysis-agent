import json
import tempfile
import unittest
from pathlib import Path

from services.document_analysis import _split_text
from services.exporter import export_node
from services.package_analysis import (
    _add_related_topic_mounts,
    _stable_group_node_id,
)


class PerfectionContractTests(unittest.TestCase):
    def test_model_wording_does_not_change_group_identity(self):
        members = ["a.pdf", "b.pdf"]
        self.assertEqual(
            _stable_group_node_id("内容主题", "英文退化名称", members),
            _stable_group_node_id("内容主题", "中文人工确认名称", members),
        )

    def test_related_topics_do_not_duplicate_primary_membership(self):
        tree = {"kind": "analysis_root", "children": [
            {"kind": "group", "name": "漏洞攻击", "member_paths": ["a.txt"], "file_count": 1,
             "children": [{"kind": "file", "path": "a.txt", "name": "a.txt"}]},
            {"kind": "group", "name": "检测响应", "member_paths": ["b.txt"], "file_count": 1,
             "children": [{"kind": "file", "path": "b.txt", "name": "b.txt"}]},
        ]}
        documents = {
            "a.txt": {"text": "漏洞攻击 检测响应 漏洞攻击 检测响应"},
            "b.txt": {"text": "检测响应 威胁研判 检测响应 威胁研判"},
        }
        result = _add_related_topic_mounts(tree, documents)
        self.assertEqual(sum(group["file_count"] for group in result["children"]), 2)
        self.assertEqual(sum(len(group["member_paths"]) for group in result["children"]), 2)
        self.assertEqual(result["membership_contract"]["related_membership"], "non_counting_reference")

    def test_token_budgeted_chunks_preserve_document_tail(self):
        text = ("第一节\n漏洞分析结论。\n\n" * 2000) + "全文最后的核验标记"
        chunks = _split_text(text, max_chunks=2, preferred_chars=5000, max_input_tokens=1500, overlap_chars=50)
        self.assertGreater(len(chunks), 2)
        self.assertIn("全文最后的核验标记", chunks[-1]["text"])
        self.assertTrue(all(item["estimated_input_tokens"] <= 1500 for item in chunks))

    def test_large_export_produces_independent_zip_volumes_and_index(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"
            output = Path(folder) / "output"
            root.mkdir()
            (root / "a.bin").write_bytes(b"a" * 6)
            (root / "b.bin").write_bytes(b"b" * 6)
            index = export_node(
                root, root, {}, output, max_bytes=10,
                task_topic="真实分卷导出验证", content_deduplication=False,
                disk_reserve_bytes=0,
            )
            sidecar = index.with_name(index.name + ".parts.json")
            manifest = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(manifest["volume_count"], 2)
            self.assertTrue(all((output / item["file_name"]).is_file() for item in manifest["parts"]))


if __name__ == "__main__":
    unittest.main()
