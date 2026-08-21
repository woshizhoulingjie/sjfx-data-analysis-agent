import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from services.folder_analysis import _attach_model_evidence
from services.large_package import build_coverage
from services.package_analysis import (
    _build_value_judgment,
    _canonical_projection,
    _group_exact,
    _semantic_document_clusters,
    _topic_clusters,
)
from services.reporting import _model_evidence_chain
from services.retrieval import retrieve_evidence
from services.storage import Storage
from services.unified_parser import UnifiedDocumentParser


def document(path, digest, text):
    return {
        "source": {"path": path, "name": Path(path).name, "sha256": digest, "size": len(text)},
        "structure": {"title": Path(path).stem, "headings": ["实验结果"]},
        "coverage": {"complete": True},
        "text": text,
        "evidence": [{
            "evidence_id": "E-{}".format(path),
            "source_path": path,
            "source_sha256": digest,
            "label": "paragraph",
            "text": "正文明确说明该方法通过对照实验验证结果，并记录了可复核的样本条件和性能差异。",
        }],
    }


class AnalysisHardeningTests(unittest.TestCase):
    def test_duplicate_copies_do_not_change_topics_or_research_score(self):
        base = {
            "a.txt": document("a.txt", "same", "远程证明 硬件信任根 实验结果 " * 40),
            "b.txt": document("b.txt", "other", "远程证明 协议验证 实验结果 " * 40),
            "c.txt": document("c.txt", "third", "远程证明 状态校验 实验结果 " * 40),
        }
        copied = dict(base)
        for index in range(100):
            path = "copies/a-{}.txt".format(index)
            copied[path] = document(path, "same", base["a.txt"]["text"])
        base_groups = _group_exact(base)
        copied_groups = _group_exact(copied)
        base_canonical, _map, _aliases = _canonical_projection(base, base_groups)
        copied_canonical, _map, _aliases = _canonical_projection(copied, copied_groups)
        self.assertEqual(len(base_canonical), len(copied_canonical))
        self.assertEqual(
            [len(item["members"]) for item in _topic_clusters(base_canonical)],
            [len(item["members"]) for item in _topic_clusters(copied_canonical)],
        )
        coverage = {"parsed_file_ratio": 1.0, "parsed_byte_ratio": 1.0, "complete_analysis": True}
        structured = {"profiled_files": 0, "total_rows": 0, "entity_statistics": {}}
        left = _build_value_judgment(
            {"file_count": 3}, base_canonical, {"parsed_files": 3}, coverage, base_groups,
            _topic_clusters(base_canonical), [], set(), structured,
        )
        right = _build_value_judgment(
            {"file_count": 103}, copied_canonical, {"parsed_files": 3}, coverage, copied_groups,
            _topic_clusters(copied_canonical), [], set(), structured,
        )
        self.assertEqual(left["research_score"], right["research_score"])
        self.assertEqual(left["research_potential"]["level"], right["research_potential"]["level"])

    def test_invalid_model_evidence_never_falls_back(self):
        catalog = [{
            "evidence_id": "E1", "source_path": "a.txt", "label": "paragraph",
            "text": "正文明确说明系统通过硬件信任根校验环境状态，并给出完整实验过程。",
        }]
        summary = _attach_model_evidence({
            "summary": "模型回答", "evidence_ids": ["unknown"],
            "recommended_research_direction": {"title": "错误方向", "evidence_ids": ["unknown"]},
        }, catalog)
        self.assertEqual(summary["evidence"], [])
        self.assertEqual(summary["recommended_research_direction"]["evidence_chain"], [])
        self.assertEqual(summary["recommended_research_direction"]["priority"], "低")
        self.assertEqual(_model_evidence_chain(catalog, ["unknown"], catalog), [])

    def test_archive_member_limits_force_partial_coverage(self):
        with tempfile.TemporaryDirectory() as folder:
            archive_path = Path(folder) / "bundle.zip"
            with zipfile.ZipFile(str(archive_path), "w") as archive:
                archive.writestr("first.txt", "第一份正文证据 " * 50)
                archive.writestr("second.txt", "第二份正文证据 " * 50)
            with patch("services.unified_parser.ARCHIVE_MAX_ENTRIES", 1):
                parsed = UnifiedDocumentParser(max_chars=10000).parse(archive_path, "bundle.zip", mode="fast")
            manifest = parsed["archive_manifest"]
            self.assertEqual(manifest["total_members"], 2)
            self.assertEqual(manifest["parsed_members"], 1)
            self.assertEqual(manifest["skipped_members"], 1)
            self.assertEqual(manifest["coverage_status"], "partial")
            self.assertFalse(parsed["coverage"]["complete"])
            scan = {"tree": {"kind": "directory", "path": ".", "children": [
                {"kind": "file", "path": "bundle.zip", "size": archive_path.stat().st_size}
            ]}}
            _for_paths, coverage = build_coverage(scan, {"bundle.zip": parsed})
            self.assertEqual(coverage["archive_member_totals"]["skipped_members"], 1)
            self.assertFalse(coverage["complete_analysis"])

    def test_archive_member_evidence_is_in_container_scope(self):
        docs = {"bundle.zip": {
            "source": {"path": "bundle.zip", "sha256": "sha"},
            "evidence": [{
                "evidence_id": "E1", "source_path": "bundle.zip::member.txt",
                "archive_source_path": "bundle.zip", "archive_member": "member.txt",
                "source_sha256": "sha", "label": "paragraph",
                "text": "正文记录销售增长百分比和对应统计口径，可直接用于复核结果。",
            }],
        }}
        result = retrieve_evidence(docs, "销售增长统计", scope="bundle.zip")
        self.assertEqual(result["results"][0]["evidence_id"], "E1")

    def test_small_sample_research_potential_is_capped(self):
        docs = {
            "a.txt": document("a.txt", "a", "高密度主题正文 " * 200),
            "b.txt": document("b.txt", "b", "高密度主题正文 " * 200),
        }
        judgment = _build_value_judgment(
            {"file_count": 2}, docs, {"parsed_files": 2},
            {"parsed_file_ratio": 1.0, "parsed_byte_ratio": 1.0, "complete_analysis": True},
            [], _topic_clusters(docs), [], set(),
            {"profiled_files": 0, "total_rows": 0, "entity_statistics": {}},
        )
        self.assertEqual(judgment["research_potential"]["level"], "待确认")
        self.assertLessEqual(judgment["research_score"], 59.0)
        self.assertEqual(judgment["task_relevance"]["level"], "未评估")

    def test_embedding_cache_and_large_cluster_algorithm(self):
        class Embedder:
            model = "test-embed"

            def __init__(self):
                self.calls = 0

            def embed(self, texts):
                self.calls += 1
                return [[float(index % 7), 1.0, 0.5, -0.25] for index, _text in enumerate(texts)]

        docs = {
            "{}.txt".format(index): document("{}.txt".format(index), "sha-{}".format(index), "主题正文 {} ".format(index) * 20)
            for index in range(501)
        }
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "cache.db")
            embedder = Embedder()
            clusters, threshold = _semantic_document_clusters(docs, embedder, batch_size=128, storage=storage)
            self.assertIsNone(threshold)
            self.assertTrue(clusters)
            self.assertIn(clusters[0]["algorithm"], {"MiniBatchKMeans", "deterministic-vector-buckets"})
            calls = embedder.calls
            _semantic_document_clusters(docs, embedder, batch_size=128, storage=storage)
            self.assertEqual(embedder.calls, calls)
            chunks = [{
                "evidence_id": "E1", "source_path": "0.txt", "label": "paragraph",
                "text": "正文说明批量聚类结果和缓存复用方式，可以直接用于性能复核。",
            }]
            self.assertEqual(storage.replace_evidence_index("scan", chunks), 1)
            indexed = storage.list_evidence_index("scan")
            result = retrieve_evidence({}, "聚类缓存复用", indexed_chunks=indexed)
            self.assertEqual(result["index_mode"], "persistent-evidence-index")


if __name__ == "__main__":
    unittest.main()
