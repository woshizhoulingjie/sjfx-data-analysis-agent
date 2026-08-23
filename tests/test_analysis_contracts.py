import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.evidence import evidence_quality
from services.folder_analysis import _normalize_question_answer_evidence
from services.large_package import build_coverage, file_fingerprint, representative_paths
from services.package_analysis import _build_structured_overview
from services.storage import Storage
from services.reporting import _direction_candidates


class AnalysisContractTests(unittest.TestCase):
    def setUp(self):
        self.scan = {
            "tree": {"kind": "directory", "path": ".", "children": [
                {"kind": "file", "path": "a.txt", "size": 10},
                {"kind": "file", "path": "b.txt", "size": 20},
            ]},
            "file_count": 2,
            "total_size": 30,
        }

    def test_empty_scope_does_not_fall_back_to_root(self):
        coverage_for_paths, _ = build_coverage(
            self.scan,
            {"a.txt": {"coverage": {"complete": True}}},
            pending_paths={"b.txt"},
            policy={"mode": "standard", "enabled": False},
        )
        result = coverage_for_paths([])
        self.assertEqual(result["inventory_files"], 0)
        self.assertEqual(result["parsed_files"], 0)

    def test_question_contract_rejects_navigation_evidence(self):
        title = {"evidence_id": "E-title", "label": "title", "text": "开源软件的特点"}
        body = {"evidence_id": "E-body", "label": "paragraph", "text": "开源软件允许用户查看、修改和再分发源代码，因此能够提高可定制性。"}
        summary = _normalize_question_answer_evidence({
            "summary": "开源软件支持用户修改和再分发。",
            "claims": [{"statement": "开源软件允许修改源代码", "evidence": [title, body]}],
        }, [title, body], "topic:1", {"coverage": {"complete_analysis": True}})
        self.assertEqual(summary["evidence_status"], "supported")
        self.assertEqual([item["evidence_id"] for item in summary["evidence"]], ["E-body"])
        self.assertEqual(summary["claims"][0]["evidence_ids"], ["E-body"])

    def test_direction_candidates_have_evidence_and_score(self):
        analysis = {
            "statistics": {"parsed_files": 2},
            "coverage": {"parsed_file_ratio": 1.0, "complete_analysis": True},
            "research_topic_clusters": [{
                "topic": "远程证明机制",
                "members": ["a.txt", "b.txt"],
                "representative_documents": ["a.txt"],
                "evidence_chain": [{
                    "evidence_id": "E1", "source_path": "a.txt", "label": "paragraph",
                    "text": "远程证明通过硬件信任根验证计算环境状态。",
                }],
            }],
            "document_index": [],
        }
        candidates = _direction_candidates({}, analysis)
        self.assertEqual(len(candidates), 1)
        self.assertGreater(candidates[0]["score"], 0)
        self.assertEqual(candidates[0]["evidence_ids"], ["E1"])

    def test_representatives_prioritize_small_actionable_text(self):
        files = [
            {"path": "reports/huge-{}.pdf".format(index), "extension": ".pdf", "size": 30_000_000}
            for index in range(20)
        ]
        files += [
            {"path": "notes/conclusion.txt", "extension": ".txt", "size": 12_000},
            {"path": "tables/key.csv", "extension": ".csv", "size": 80_000},
        ]
        selected = representative_paths(files, 5)
        self.assertIn("notes/conclusion.txt", selected)
        self.assertIn("tables/key.csv", selected)

    def test_empty_or_cuda_parse_pool_is_not_a_model_concurrency_switch(self):
        # The public contract is explicit: parse concurrency is a bounded CPU
        # setting and must not be confused with LLM_MAX_CONCURRENCY.
        from config import Config
        self.assertGreaterEqual(Config.PARSE_MAX_CONCURRENCY, 1)
        self.assertEqual(Config.LLM_MAX_CONCURRENCY, 1)

    def test_parse_pool_reserves_worst_case_temp_disk_per_worker(self):
        from config import Config
        from services.package_analysis import _temp_disk_worker_limit
        gib = 1024 ** 3
        with patch.object(Config, "MAX_CONTENT_BYTES", 10 * gib), patch.object(
            Config, "PARSE_TEMP_DISK_RESERVE_BYTES", gib
        ), patch("services.package_analysis.shutil.disk_usage", return_value=SimpleNamespace(free=15 * gib)):
            self.assertEqual(_temp_disk_worker_limit(4), 1)
        with patch.object(Config, "MAX_CONTENT_BYTES", 10 * gib), patch.object(
            Config, "PARSE_TEMP_DISK_RESERVE_BYTES", gib
        ), patch("services.package_analysis.shutil.disk_usage", return_value=SimpleNamespace(free=45 * gib)):
            self.assertEqual(_temp_disk_worker_limit(4), 2)

    def test_sidecar_projection_never_claims_full_text_analysis(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "agent.db")
            document = {
                "source": {"path": "a.txt", "sha256": "abc"},
                "text": "正文" * 10000,
                "evidence": [{"evidence_id": "E{}".format(index), "text": "证据"} for index in range(50)],
                "coverage": {"complete": True, "coverage_ratio": 1.0},
            }
            projection = storage.project_document(document, text_limit=1000, evidence_limit=4)
            self.assertTrue(projection["coverage"]["parse_complete"])
            self.assertFalse(projection["coverage"]["semantic_complete"])
            self.assertFalse(projection["coverage"]["complete"])
            _for_paths, coverage = build_coverage(
                self.scan, {"a.txt": projection}, pending_paths={"b.txt"},
                policy={"mode": "large_package", "enabled": True},
            )
            self.assertFalse(coverage["full_text_analysis"])
            self.assertEqual(coverage["coverage_level"], "representative_overview")
            self.assertEqual(coverage["inventory_coverage_ratio"], 1.0)
            self.assertEqual(coverage["content_parse_ratio"], 0.5)
            self.assertEqual(coverage["deep_analysis_ratio"], 0.0)
            self.assertIn("deep_analysis", coverage["coverage_contract"])

    def test_inventory_truncation_blocks_full_text_label(self):
        scan = dict(self.scan)
        scan["truncated"] = True
        documents = {
            path: {"coverage": {"complete": True}, "text": "完整正文"}
            for path in ("a.txt", "b.txt")
        }
        _for_paths, coverage = build_coverage(scan, documents)
        self.assertFalse(coverage["inventory_coverage"]["complete"])
        self.assertIsNone(coverage["inventory_coverage_ratio"])
        self.assertTrue(coverage["parse_coverage"]["complete"])
        self.assertFalse(coverage["semantic_analysis_coverage"]["complete"])
        self.assertFalse(coverage["complete_analysis"])

    def test_checkpoint_fingerprint_binds_content_mode_and_parser_contract(self):
        node = {"path": "a.txt", "size": 10, "modified_at_ns": 123}
        baseline = file_fingerprint(node, "fast", {"version": 1}, "a" * 64)
        self.assertNotEqual(baseline, file_fingerprint(node, "accurate", {"version": 1}, "a" * 64))
        self.assertNotEqual(baseline, file_fingerprint(node, "fast", {"version": 2}, "a" * 64))
        self.assertNotEqual(baseline, file_fingerprint(node, "fast", {"version": 1}, "b" * 64))

    def test_structured_overview_marks_samples_and_top_k_as_incomplete(self):
        profile = {
            "status": "partial",
            "row_count": 100000,
            "quality_score": 80,
            "coverage": {"complete": False, "truncated": True},
            "limits": {"truncated": True},
            "entity_columns": {"person": ["姓名"]},
            "entity_statistics": {"person": {
                "distinct_count": 100,
                "top_values": [{"value": "张三", "count": 5}],
            }},
        }
        overview = _build_structured_overview({"people.csv": {"data_profile": profile}})
        self.assertIsNone(overview["total_rows"])
        self.assertEqual(overview["sampled_rows"], 100000)
        self.assertEqual(overview["row_count_kind"], "sampled")
        self.assertFalse(overview["coverage"]["complete"])
        people = overview["entity_statistics"]["person"]
        self.assertIsNone(people["distinct_count"])
        self.assertEqual(people["observed_distinct_count"], 1)
        self.assertFalse(people["coverage"]["complete"])

    def test_structured_overview_exposes_omitted_and_unavailable_profiles(self):
        complete = {
            "status": "completed",
            "row_count": 2,
            "coverage": {"complete": True},
            "limits": {"truncated": False},
        }
        failed = {"status": "failed", "row_count": 0, "coverage": {"complete": False}}
        overview = _build_structured_overview({
            "archive.zip": {
                "data_profiles_total": 3,
                "data_profiles": [
                    {"member": "good.csv", "profile": complete},
                    {"member": "bad.csv", "profile": failed},
                ],
            }
        })

        self.assertIsNone(overview["total_rows"])
        self.assertFalse(overview["coverage"]["complete"])
        self.assertEqual(overview["coverage"]["omitted_projected_profiles"], 1)
        self.assertEqual(overview["coverage"]["unavailable_profiles"], 1)


if __name__ == "__main__":
    unittest.main()
