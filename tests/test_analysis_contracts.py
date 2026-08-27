import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.evidence import compact_evidence, evidence_quality, verify_claim_evidence
from services.folder_analysis import _normalize_question_answer_evidence
from services.large_package import (
    build_coverage, build_policy, file_fingerprint, package_resource_plan,
    representative_paths,
)
from services.package_analysis import _build_structured_overview
from services.storage import Storage
from services.reporting import _direction_candidates
from services.retrieval import evidence_corpus


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

    def test_resource_plan_blocks_before_content_io_when_disks_are_too_small(self):
        plan = package_resource_plan(
            {"file_count": 50000, "total_size": 100 * 1024 ** 3},
            state_free_bytes=1024 ** 3,
            temp_free_bytes=5 * 1024 ** 3,
            max_content_bytes=10 * 1024 ** 3,
            temp_reserve_bytes=1024 ** 3,
        )
        self.assertFalse(plan["ready"])
        self.assertIn("state_disk_insufficient", plan["blockers"])
        self.assertIn("parse_scratch_insufficient", plan["blockers"])
        self.assertEqual(plan["mandatory_hash_read_bytes"], 100 * 1024 ** 3)

    def test_large_package_default_is_30_file_deep_batches_until_exhausted(self):
        policy = build_policy({"file_count": 3000, "total_size": 1})
        self.assertTrue(policy["enabled"])
        self.assertEqual(policy["batch_files"], 30)
        self.assertGreaterEqual(policy["batch_files"], 20)
        self.assertLessEqual(policy["batch_files"], 50)
        self.assertEqual(policy["batch_completion"], "continue_until_inventory_exhausted")
        self.assertEqual(policy["batch_checkpoint_scope"], "per_file")
        self.assertEqual(policy["deep_batch_contract"], "one_durable_job_per_20_to_50_files")

    def test_pipeline_coverage_keeps_all_nine_stages_separate(self):
        workflow = {
            "a.txt": {
                "safety_status": "checked", "light_index_status": "ready",
                "selection_state": "priority", "language_code": "zh",
                "ocr_candidate": False,
            },
            "b.txt": {
                "safety_status": "checked", "light_index_status": "ready",
                "selection_state": "deferred", "language_code": "en",
                "ocr_candidate": False,
            },
        }
        documents = {
            "a.txt": {
                "text": "原文", "evidence": [{"evidence_id": "E1", "text": "原文"}],
                "coverage": {"deep_parse_complete": True},
            },
            "b.txt": {"text": "preview", "coverage": {"preview_only": True}},
        }
        _for_paths, coverage = build_coverage(
            self.scan, documents, policy={"mode": "large_package", "enabled": True},
            workflow_states=workflow,
            translation_states={"b.txt": {"status": "partial"}},
        )
        self.assertEqual(set(coverage["pipeline_coverage"]), {
            "inventory", "safety", "light_index", "content_parse", "ocr",
            "selection", "deep_analysis", "translation", "evidence_readiness",
        })
        self.assertTrue(coverage["light_index_coverage"]["complete"])
        self.assertEqual(coverage["content_parse_ratio"], 0.5)
        self.assertEqual(coverage["parsed_files"], 1)
        self.assertEqual(coverage["document_records"], 2)
        self.assertFalse(coverage["semantic_analysis_coverage"]["complete"])
        self.assertFalse(coverage["translation_coverage"]["complete"])

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

    def test_relation_negation_scope_is_not_a_false_positive(self):
        result = verify_claim_evidence(
            "系统不支持加密",
            {"label": "paragraph", "text": "系统支持加密，但不支持压缩；该结论已经经过完整测试。"},
        )
        self.assertEqual(result["support_status"], "insufficient")
        self.assertEqual(result["support_relation"], "polarity_mismatch")
        self.assertEqual(result["verification_contract"], "claim-evidence/3.0")

    def test_relation_direction_is_not_a_false_positive(self):
        result = verify_claim_evidence(
            "组织甲攻击了组织乙",
            {"label": "paragraph", "text": "组织乙攻击了组织甲，调查结果已经完成并确认该事件。"},
        )
        self.assertEqual(result["support_status"], "insufficient")
        self.assertEqual(result["support_relation"], "direction_mismatch")

    def test_matching_relation_frame_is_supported(self):
        result = verify_claim_evidence(
            "系统支持加密",
            {"label": "paragraph", "text": "测试结果表明，系统支持加密并已经通过完整验证。"},
        )
        self.assertEqual(result["support_status"], "supported")
        self.assertEqual(result["support_relation"], "direct_frame")

    def test_chinese_claim_matches_working_translation_but_citation_keeps_original(self):
        evidence = {
            "evidence_id": "E-arabic", "source_path": "arabic.txt", "label": "paragraph",
            "text": "يدعم النظام التشفير وتم التحقق منه بالكامل.",
            "translated_text": "测试结果表明，系统支持加密并已经通过完整验证。",
            "source_language": "ar", "target_language": "zh-CN",
            "translation_source": "import_working_translation",
        }

        result = verify_claim_evidence("系统支持加密", evidence)
        compact = compact_evidence(evidence)

        self.assertEqual(result["support_status"], "supported")
        self.assertEqual(compact["text"], evidence["text"])
        self.assertEqual(compact["original_text"], evidence["text"])
        self.assertEqual(compact["translated_text"], evidence["translated_text"])
        self.assertEqual(compact["source_language"], "ar")

    def test_mixed_claim_support_is_partial_and_keeps_valid_evidence(self):
        good = {
            "evidence_id": "E-good", "label": "paragraph", "source_path": "a.pdf",
            "text": "测试结果表明，系统支持加密并已经通过完整验证。",
        }
        bad = {
            "evidence_id": "E-bad", "label": "paragraph", "source_path": "b.pdf",
            "text": "组织乙攻击了组织甲，调查结果已经完成并确认该事件。",
        }
        summary = _normalize_question_answer_evidence({
            "answer": "现有材料形成两项判断。",
            "claims": [
                {"statement": "系统支持加密", "evidence": [good]},
                {"statement": "组织甲攻击了组织乙", "evidence": [bad]},
            ],
        }, [good, bad], ".")
        self.assertEqual(summary["evidence_status"], "partially_supported")
        self.assertEqual(summary["evidence_contract"], "question-answer-evidence/3.0")
        self.assertEqual(summary["evidence_ids"], ["E-good"])
        self.assertEqual(summary["claim_status_counts"]["insufficient"], 1)

    def test_retrieval_corpus_preserves_exact_source_locators(self):
        document = {
            "source": {"path": "a.pdf", "sha256": "source-hash"},
            "evidence": [{
                "evidence_id": "E1", "source_path": "a.pdf", "label": "paragraph",
                "text": "该系统通过硬件信任根实现远程证明并降低伪造风险。",
                "page": 3, "paragraph_index": 7, "block_index": 2,
                "char_start": 120, "char_end": 148, "parser_version": "2.0",
            }],
        }
        item = evidence_corpus({"a.pdf": document})[0]
        self.assertEqual(item["paragraph_index"], 7)
        self.assertEqual(item["block_index"], 2)
        self.assertEqual((item["char_start"], item["char_end"]), (120, 148))
        self.assertEqual(item["parser_version"], "2.0")

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

    def test_direction_candidates_use_official_tree_evidence(self):
        evidence = {
            "evidence_id": "E-market", "source_path": "市场经营简报.md",
            "source_sha256": "market-source", "label": "paragraph",
            "text": "新客户转化率从18%提升至21%，但平均交付周期由7.2天延长至8.1天。",
        }
        table_evidence = {
            "evidence_id": "E-table", "source_path": "区域经营数据.csv",
            "source_sha256": "table-source", "label": "table",
            "text": "区域销售数据表明华东销售额为1280万元，环比增长12%，新客户数为46。",
        }
        analysis = {
            "statistics": {"parsed_files": 2},
            "coverage": {"parsed_file_ratio": 1.0, "complete_analysis": True},
            # Semantic metadata intentionally has no evidence. This was the
            # production failure: it shadowed the official evidenced tree.
            "semantic_topic_clusters": [{
                "name": "区域市场经营数据分析与风险预警",
                "members": ["区域经营数据.csv", "市场经营简报.md"],
            }],
            "analysis_tree": {"children": [{
                "kind": "group", "node_type": "topic", "classification_status": "classified",
                "node_id": "group-market", "name": "区域市场经营数据分析与风险预警",
                "member_paths": ["区域经营数据.csv", "市场经营简报.md"],
                "related_topics": ["销售分析", "交付周期", "风险预警"],
                "representative_documents": ["市场经营简报.md"],
                "evidence_chain": [evidence],
            }]},
            "retrieval": {"queries": [{"results": [table_evidence]}]},
            "document_index": [
                {"source": {"path": "区域经营数据.csv", "extension": ".csv"}, "classification": {"document_role": "结构化数据"}},
                {"source": {"path": "市场经营简报.md", "extension": ".md"}, "classification": {"document_role": "一般资料"}},
            ],
        }
        candidates = _direction_candidates({}, analysis)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["title"], "区域市场经营数据分析与风险预警")
        self.assertEqual(set(candidates[0]["evidence_ids"]), {"E-market", "E-table"})
        self.assertEqual(candidates[0]["independent_source_count"], 2)
        self.assertEqual(candidates[0]["candidate_source"], "official_analysis_tree")
        self.assertEqual(candidates[0]["evidence_status"], "supported")
        self.assertIn("交付周期", candidates[0]["research_questions"][0])

    def test_unclassified_tree_bucket_is_not_recommended(self):
        analysis = {
            "statistics": {"parsed_files": 1},
            "analysis_tree": {"children": [{
                "kind": "group", "classification_status": "unclassified",
                "name": "其他内容", "member_paths": ["unknown.txt"],
                "evidence_chain": [{
                    "evidence_id": "E1", "source_path": "unknown.txt", "label": "paragraph",
                    "text": "该材料提供了一段较长的事实性正文，但尚未形成稳定主题。",
                }],
            }]},
            "semantic_topic_clusters": [], "research_topic_clusters": [], "topic_clusters": [],
        }
        self.assertEqual(_direction_candidates({}, analysis), [])

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
