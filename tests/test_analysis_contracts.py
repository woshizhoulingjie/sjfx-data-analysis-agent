import unittest

from services.evidence import evidence_quality
from services.folder_analysis import _normalize_question_answer_evidence
from services.large_package import build_coverage, representative_paths
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


if __name__ == "__main__":
    unittest.main()
