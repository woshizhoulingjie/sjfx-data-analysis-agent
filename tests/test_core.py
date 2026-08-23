import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from services.ollama import OllamaClient
from services.document_analysis import _local_merge
from services.evidence import select_evidence, set_embedding_provider
from services.exporter import export_node
from services.package_analysis import (
    SourceFileChangedError,
    _assert_source_stable,
    _parallel_parse_files,
    _secure_source_snapshot,
    _source_has_open_writer,
    _document_role,
    _content_topics,
    _group_similar,
    _hamming,
    _topic_clusters,
    _walk_files,
    analyze_package,
    simhash64,
)
from services.reporting import build_local_report, build_report_analysis_prompt, merge_model_report
from services.folder_analysis import analyze_folder
from services.model_output import ModelOutputError, extract_json_value, validate_json_object
from services.retrieval import retrieve_evidence
from services.scanner import resolve_under, scan_directory
from services.storage import Storage
from services.unified_parser import UnifiedDocumentParser


class CoreRegressionTests(unittest.TestCase):
    class _NativeOllamaResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def __iter__(self):
            yield b'{"model":"qwen-agent:latest","message":{"content":"{\\"ok\\":true}"},"done":false}\n'
            yield b'{"model":"qwen-agent:latest","done":true,"done_reason":"stop","prompt_eval_count":12,"eval_count":4}\n'

    class _SummaryModel:
        max_concurrency = 1

        def __init__(self):
            self.calls = 0

        def chat_json(self, system_prompt, user_prompt, **kwargs):
            self.calls += 1
            return {
                "json": {
                    "title": "主题簇概览",
                    "summary": "证据显示该主题包含实验结果和方法对照。",
                    "topics": ["实验"],
                    "notable_items": ["不同来源存在可核验差异"],
                    "evidence_ids": ["E-1"],
                    "limitations": [],
                    "recommended_research_direction": {
                        "title": "复核实验条件与结论一致性",
                        "rationale": "需要对照来源中的实验条件。",
                        "questions": ["实验条件是否一致？"],
                        "methods": ["建立证据矩阵"],
                        "evidence_ids": ["E-1"],
                    },
                },
                "model": "test-model",
                "usage": {},
            }

    def _overview_scan(self, physical_children):
        return {
            "root": "C:/package",
            "scanned_at": "2026-08-16T00:00:00+00:00",
            "file_count": 3,
            "directory_count": len(physical_children),
            "total_size": 60,
            "total_size_human": "60.0 B",
            "type_counts": {".txt": 3},
            "truncated": False,
            "errors": [],
            "tree": {"kind": "directory", "name": "package", "path": ".", "children": physical_children},
        }

    def _overview_document(self, path, role, size=20):
        return {
            "source": {"path": path, "extension": ".txt", "size": size},
            "classification": {"document_role": role},
            "structure": {"headings": []},
            "coverage": {"complete": True},
        }

    def test_overview_uses_complete_adaptive_categories_not_top_ten_directories(self):
        physical_children = [
            {"kind": "directory", "name": "物理目录{}".format(index), "path": "folder{}".format(index), "file_count": 1}
            for index in range(12)
        ]
        scan = self._overview_scan(physical_children)
        analysis = {
            "statistics": {"parsed_files": 3},
            "classification_dimensions": [{"name": "内容类别", "reason": "正文分类"}],
            "analysis_tree": {
                "kind": "analysis_root",
                "children": [
                    {
                        "kind": "group", "dimension": "内容类别", "name": "研究文献", "summary": "研究材料。",
                        "children": [
                            {"kind": "file", "path": "folder1/paper.txt", "content_topics": ["模型", "实验"]},
                        ],
                    },
                    {
                        "kind": "group", "dimension": "内容类别", "name": "结构化数据", "summary": "数据表。",
                        "children": [
                            {"kind": "file", "path": "folder2/data.txt", "content_topics": ["销售", "指标"]},
                        ],
                    },
                    {
                        "kind": "group", "dimension": "内容类别", "name": "要求与说明材料", "summary": "需求材料。",
                        "children": [
                            {"kind": "file", "path": "folder3/requirements.txt", "content_topics": ["验收", "功能"]},
                        ],
                    },
                ],
            },
            "document_index": [
                self._overview_document("folder1/paper.txt", "研究文献"),
                self._overview_document("folder2/data.txt", "结构化数据"),
                self._overview_document("folder3/requirements.txt", "要求与说明材料"),
            ],
        }
        report = build_local_report(scan, [], analysis)

        self.assertEqual(report["classification_coverage"]["source"], "adaptive_analysis_tree")
        self.assertTrue(report["classification_coverage"]["complete"])
        self.assertEqual(report["classification_coverage"]["top_level_category_count"], 3)
        self.assertEqual(
            [item["name"] for item in report["global_categories"]],
            ["研究文献", "结构化数据", "要求与说明材料"],
        )
        self.assertNotIn("物理目录0", {item["name"] for item in report["global_categories"]})

    def test_overview_keeps_every_adaptive_topic_and_its_evidence(self):
        scan = self._overview_scan([])
        analysis = {
            "statistics": {"parsed_files": 3},
            "classification_dimensions": [
                {"name": "内容类别", "reason": "正文分类"},
                {"name": "内容主题", "reason": "重复主题"},
            ],
            "analysis_tree": {
                "kind": "analysis_root",
                "children": [{
                    "kind": "group", "dimension": "内容类别", "name": "研究文献", "summary": "研究材料。",
                    "children": [
                        {
                            "kind": "group", "dimension": "内容主题", "name": "模型安全", "summary": "共同主题模型安全。",
                            "children": [{"kind": "file", "path": "a.txt", "content_topics": ["模型安全"]}],
                        },
                        {
                            "kind": "group", "dimension": "内容主题", "name": "实验方法", "summary": "共同主题实验方法。",
                            "children": [
                                {"kind": "file", "path": "b.txt", "content_topics": ["实验方法"]},
                                {"kind": "file", "path": "c.txt", "content_topics": ["实验方法"]},
                            ],
                        },
                    ],
                }],
            },
            "document_index": [
                self._overview_document("a.txt", "研究文献"),
                self._overview_document("b.txt", "研究文献"),
                self._overview_document("c.txt", "研究文献"),
            ],
            "topic_clusters": [{
                "members": ["a.txt", "b.txt"],
                "representative_documents": ["a.txt", "b.txt"],
                "evidence_chain": [
                    {"evidence_id": "E-a", "source_path": "a.txt", "text": "模型安全证据"},
                    {"evidence_id": "E-b", "source_path": "b.txt", "text": "实验方法证据"},
                ],
            }],
        }
        report = build_local_report(scan, [], analysis)
        category = report["global_categories"][0]

        self.assertEqual([item["name"] for item in category["subcategories"]], ["模型安全", "实验方法"])
        self.assertEqual(category["file_count"], 3)
        self.assertEqual(category["evidence_chain"][0]["source_path"], "a.txt")

    def test_overview_fallback_is_one_bounded_root_inventory_category(self):
        physical_children = [
            {"kind": "directory", "name": "目录{}".format(index), "path": "folder{}".format(index), "file_count": 1,
             "total_size": 10, "type_counts": {".txt": 1}, "simple_summary": "目录摘要"}
            for index in range(12)
        ]
        report = build_local_report(self._overview_scan(physical_children), [], {"statistics": {"parsed_files": 12}})

        self.assertEqual(report["classification_coverage"]["source"], "root_inventory_fallback")
        self.assertEqual(len(report["global_categories"]), 1)
        self.assertIn("分类未完成", report["global_categories"][0]["name"])

    def test_model_merge_does_not_replace_complete_local_categories(self):
        local = {"global_categories": [{"name": "研究文献"}], "key_findings": ["本地发现"], "directions": []}
        merged = merge_model_report(local, {"global_categories": [{"name": "模型错误分类"}], "key_findings": ["增强发现"]})

        self.assertEqual(merged["global_categories"], [{"name": "研究文献"}])
        self.assertEqual(merged["key_findings"], ["本地发现"])

    def test_model_research_prompt_uses_numbered_evidence_and_merge_rejects_unknown_ids(self):
        scan = self._overview_scan([])
        analysis = {
            "statistics": {"parsed_files": 1},
            "topic_clusters": [{"evidence_chain": [{
                "evidence_id": "E-1", "source_path": "a.txt", "page": 1,
                "text": "材料讨论了可验证的实验结果。",
            }]}],
        }
        report = build_local_report(scan, [], analysis)
        prompt, catalog = build_report_analysis_prompt(scan, [], analysis, report)
        self.assertIn('"evidence_id": "E-1"', prompt)
        merged = merge_model_report(report, {
            "recommended_research_direction": {
                "title": "基于实验结果的复核研究", "evidence_ids": ["unknown", "E-1"],
            }
        }, catalog)
        self.assertEqual(merged["recommended_research_direction"]["evidence_chain"][0]["evidence_id"], "E-1")

    def test_folder_summary_uses_one_topic_evidence_request(self):
        model = self._SummaryModel()
        context = {
            "total_files": 20,
            "total_dirs": 2,
            "total_size_human": "10 MB",
            "type_counts": {".pdf": 20},
            "documents": [],
            "topic_clusters": [{
                "cluster_id": "TOPIC-0001",
                "topic": "实验方法",
                "members": ["a.pdf", "b.pdf"],
                "representative_documents": ["a.pdf"],
                "evidence": [{"evidence_id": "E-1", "source_path": "a.pdf", "page": 3, "label": "paragraph", "text": "实验采用相同样本条件进行对照，结果显示两种方法在准确率方面存在可核验差异。"}],
            }],
        }
        summary, result, errors = analyze_folder(model, context, ".")
        self.assertEqual(model.calls, 1)
        self.assertEqual(summary["summary_mode"], "topic_cluster_evidence")
        self.assertEqual(summary["evidence_status"], "supported")
        self.assertEqual(summary["evidence"][0]["evidence_id"], "E-1")
        self.assertEqual(summary["recommended_research_direction"]["evidence_chain"][0]["page"], 3)
        self.assertFalse(errors)

    def test_ollama_json_uses_native_api_with_thinking_disabled(self):
        client = OllamaClient("http://127.0.0.1:11434/v1", "qwen-agent:latest", timeout=5)
        with patch("services.ollama.urllib.request.urlopen", return_value=self._NativeOllamaResponse()) as urlopen:
            result = client.chat_json("system", "user", max_tokens=1200, timeout=5)
        request = urlopen.call_args[0][0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/chat")
        self.assertFalse(payload["think"])
        self.assertEqual(payload["format"], "json")
        self.assertEqual(result["json"], {"ok": True})

    def test_recursive_file_and_directory_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a.txt").write_text("alpha", encoding="utf-8")
            (root / "nested").mkdir()
            (root / "nested" / "b.txt").write_text("beta", encoding="utf-8")
            result = scan_directory(root)
            self.assertEqual(result["file_count"], 2)
            self.assertEqual(result["directory_count"], 1)
            self.assertEqual(result["tree"]["direct_file_count"], 1)
            self.assertEqual(result["tree"]["file_count"], 2)

    def test_hybrid_retrieval_returns_traceable_evidence(self):
        documents = [{
            "path": "sales.md",
            "payload": {
                "source": {"path": "sales.md", "sha256": "a" * 64},
                "evidence": [{
                    "evidence_id": "E-sales-1",
                    "source_path": "sales.md",
                    "page": 2,
                    "section": "经营指标",
                    "text": "华东销售额增长百分之十二，平均交付天数为七点八天。",
                }],
            },
        }]
        result = retrieve_evidence(documents, "销售增长与交付效率", top_k=5)
        self.assertEqual(result["results"][0]["evidence_id"], "E-sales-1")
        self.assertEqual(result["results"][0]["page"], 2)
        self.assertGreater(result["results"][0]["retrieval_score"], 0)

    def test_duplicate_job_creation_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "test.db")
            first, created_first = storage.create_or_get_job("scan-1")
            second, created_second = storage.create_or_get_job("scan-1")
            self.assertTrue(created_first)
            self.assertFalse(created_second)
            self.assertEqual(first, second)
            self.assertEqual(storage.recover_stale_jobs(), 0)
            third, created_third = storage.create_or_get_job("scan-1")
            self.assertFalse(created_third)
            self.assertEqual(first, third)
            self.assertEqual(storage.get_job(first)["status"], "queued")

    def test_worker_claim_is_atomic_and_healthy_heartbeat_is_not_requeued(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "test.db")
            job_id = storage.create_scan_job(folder, 10, "fast", 8)
            claimed = storage.claim_next_job("worker-a")
            self.assertEqual(claimed["id"], job_id)
            self.assertEqual(claimed["task_type"], "scan_and_analyze")
            self.assertIsNone(storage.claim_next_job("worker-b"))
            self.assertEqual(storage.recover_stale_jobs(stale_after_seconds=900), 0)
            storage.cancel_job(job_id)
            self.assertEqual(storage.get_job(job_id)["status"], "cancelling")

    def test_new_locked_worker_immediately_recovers_fresh_orphaned_jobs(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "test.db")
            running_id = storage.create_scan_job(folder, 10, "fast", 8)
            storage.claim_next_job("worker-that-exited")
            storage.heartbeat_job(running_id, "worker-that-exited")
            self.assertEqual(storage.recover_stale_jobs(stale_after_seconds=900), 0)

            cancelling_id = storage.create_scan_job(folder, 10, "fast", 8)
            storage.cancel_job(running_id)
            storage.claim_next_job("worker-that-exited")
            storage.cancel_job(cancelling_id)

            self.assertEqual(storage.recover_orphaned_jobs_after_lock(), 2)
            recovered = storage.get_job(running_id)
            cancelled = storage.get_job(cancelling_id)
            self.assertEqual(recovered["status"], "cancelled")
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertIsNotNone(cancelled["finished_at"])

    def test_new_locked_worker_requeues_fresh_running_job(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "test.db")
            job_id = storage.create_scan_job(folder, 10, "fast", 8)
            storage.claim_next_job("worker-that-exited")
            storage.heartbeat_job(job_id, "worker-that-exited")

            self.assertEqual(storage.recover_orphaned_jobs_after_lock(), 1)
            recovered = storage.get_job(job_id)
            self.assertEqual(recovered["status"], "queued")
            self.assertIsNone(recovered["worker_id"])
            self.assertFalse(recovered["cancel_requested"])

    def test_model_json_extractor_handles_fence_prefix_and_quoted_braces(self):
        value = extract_json_value(
            "模型结果如下：\n```json\n{\"regex\": \"[0-9]{1,3}\", \"ok\": true}\n```"
        )
        self.assertEqual(value["regex"], "[0-9]{1,3}")
        self.assertTrue(value["ok"])
        self.assertEqual(extract_json_value("说明 {\"ok\": true} 尾注"), {"ok": True})
        with self.assertRaises(ModelOutputError):
            validate_json_object(["not", "an", "object"])

    def test_scan_depth_limit_and_symlink_are_observable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            current = root
            for name in ("one", "two", "three"):
                current = current / name
                current.mkdir()
            (current / "deep.txt").write_text("deep", encoding="utf-8")
            link = root / "self-link"
            try:
                link.symlink_to(root, target_is_directory=True)
            except (NotImplementedError, OSError):
                link = None
            result = scan_directory(root, max_depth=2)
            self.assertGreaterEqual(result["depth_limited_directory_count"], 1)
            self.assertEqual(result["max_depth"], 2)
            if link is not None:
                self.assertGreaterEqual(result["skipped_symlink_count"], 1)

    def test_job_can_be_cancelled_without_cancelling_other_jobs(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "test.db")
            first, _ = storage.create_or_get_job("scan-1")
            second, _ = storage.create_or_get_job("scan-2")
            storage.cancel_job(first)
            self.assertEqual(storage.get_job(first)["status"], "cancelled")
            self.assertEqual(storage.get_job(second)["status"], "queued")

    def test_queued_cancel_is_terminal_and_retry_gets_a_new_job(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "test.db")
            first, _ = storage.create_or_get_job("scan-1")
            cancelled = storage.cancel_job(first)
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertEqual(cancelled["stage"], "cancelled")
            self.assertIsNotNone(cancelled["finished_at"])
            second, created = storage.create_or_get_job("scan-1")
            self.assertTrue(created)
            self.assertNotEqual(first, second)

    def test_finalize_job_resolves_cancel_completion_race(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "test.db")
            job_id = storage.create_scan_job(folder, 10, "fast", 8)
            storage.claim_next_job("worker-a")
            storage.cancel_job(job_id)
            final = storage.finalize_job(job_id, result={"should_not_publish": True})
            self.assertEqual(final["status"], "cancelled")
            self.assertIsNone(final["result"])

    def test_running_progress_is_monotonic(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "test.db")
            job_id = storage.create_scan_job(folder, 10, "fast", 8)
            storage.claim_next_job("worker-a")
            storage.update_job(job_id, progress=15, heartbeat=True)
            storage.update_job(job_id, progress=2, heartbeat=True)
            self.assertEqual(storage.get_job(job_id)["progress"], 15)

    def test_primary_import_has_priority_over_optional_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "test.db")
            summary_id = storage.create_job("scan-old", task_type="generate_summary")
            scan_id = storage.create_scan_job(folder, 10, "fast", 8)
            claimed = storage.claim_next_job("worker-a")
            self.assertEqual(claimed["id"], scan_id)
            self.assertEqual(storage.get_queue_position(summary_id), 1)

    def test_summary_batch_is_published_in_one_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "test.db")
            written = storage.save_summaries("scan-1", [
                (".", "folder", {"summary": "根目录"}),
                ("a.txt", "file", {"summary": "文件摘要"}),
            ])
            self.assertEqual(written, 2)
            self.assertEqual(len(storage.list_summaries("scan-1")), 2)

    def test_local_long_document_fallback_has_actionable_direction(self):
        result = _local_merge("paper.txt", [
            {"chunk_index": 1, "section_summary": "实验设计和结论", "key_facts": ["样本存在差异"]},
        ], [{"chunk_index": 1, "section_summary": "实验设计和结论", "key_facts": ["样本存在差异"]}], [])
        direction = result["recommended_research_direction"]
        self.assertNotIn("待模型完成", direction["title"])
        self.assertTrue(direction["basis"])

    def test_deep_directory_walk_avoids_recursion_limit(self):
        node = {"kind": "directory", "children": []}
        current = node
        for index in range(1100):
            child = {"kind": "directory", "children": []}
            current["children"] = [child]
            current = child
        current["children"] = [{"kind": "file", "path": "deep.txt"}]
        self.assertEqual(list(_walk_files(node))[0]["path"], "deep.txt")

    def test_retrieval_result_can_be_used_as_second_query_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "test.db")
            storage.save_retrieval_result("r-1", "scan-1", ".", ["E-1", "E-2"])
            saved = storage.get_retrieval_result("r-1", scan_id="scan-1")
            self.assertEqual(saved["evidence_ids"], ["E-1", "E-2"])
            self.assertIsNone(storage.get_retrieval_result("r-1", scan_id="other"))

    def test_legacy_summary_fields_are_normalized_on_read(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "test.db")
            storage.save_summary("scan-1", ".", "folder", {"core_summary": "旧摘要", "evidence": []})
            summary = storage.get_summary("scan-1", ".", "folder")
            self.assertEqual(summary["summary"], "旧摘要")
            self.assertEqual(summary["evidence_chain"], [])

    def test_new_request_keeps_older_queued_jobs(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "test.db")
            older, _ = storage.create_or_get_job("scan-old")
            self.assertEqual(storage.cancel_queued_jobs(except_scan_id="scan-new"), 0)
            newer, created = storage.create_or_get_job("scan-new")
            self.assertTrue(created)
            self.assertEqual(storage.get_job(older)["status"], "queued")
            self.assertEqual(storage.get_job(newer)["progress"], 1)
            self.assertEqual(storage.get_queue_position(older), 1)
            self.assertEqual(storage.get_queue_position(newer), 2)

    def test_overview_keeps_unparsed_file_type_counts_visible(self):
        scan = self._overview_scan([
            {"kind": "file", "name": "parsed.txt", "path": "parsed.txt", "extension": ".txt", "size": 20},
            {"kind": "file", "name": "broken.pdf", "path": "broken.pdf", "extension": ".pdf", "size": 40},
        ])
        scan["file_count"] = 2
        scan["type_counts"] = {".txt": 1, ".pdf": 1}
        analysis = {
            "statistics": {"parsed_files": 1, "failed_files": 1},
            "analysis_tree": {"children": [{
                "kind": "group", "dimension": "内容类别", "name": "研究文献", "children": [
                    {"kind": "file", "path": "parsed.txt", "content_topics": ["实验"]},
                ],
            }]},
            "document_index": [self._overview_document("parsed.txt", "研究文献")],
        }
        report = build_local_report(scan, [], analysis)
        unparsed = next(item for item in report["global_categories"] if item["name"] == "未解析文件（待复核）")
        self.assertEqual(unparsed["type_counts"], {".pdf": 1})
        self.assertEqual(unparsed["unparsed_file_count"], 1)

    def test_export_includes_explicit_compilation_topic_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "package"
            root.mkdir()
            (root / "note.txt").write_text("evidence", encoding="utf-8")
            archive_path = export_node(root, root, {"summary": "节点摘要"}, Path(folder), 1024 * 1024, task_topic="测试主题")
            with zipfile.ZipFile(str(archive_path)) as archive:
                handoff = json.loads(archive.read("整编任务说明.json").decode("utf-8"))
            self.assertTrue(handoff["task_topic_required"])
            self.assertEqual(handoff["task_topic"], "测试主题")
            self.assertEqual(handoff["task_topic_status"], "provided")

    def test_export_rejects_missing_compilation_topic(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "package"
            root.mkdir()
            (root / "note.txt").write_text("evidence", encoding="utf-8")
            with self.assertRaises(ValueError):
                export_node(root, root, {"summary": "节点摘要"}, Path(folder), 1024 * 1024)

    def test_export_virtual_topic_keeps_selected_files_and_conclusion_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "package"
            root.mkdir()
            (root / "topic.txt").write_text("可追溯主题证据", encoding="utf-8")
            summary = {
                "summary": "主题节点摘要",
                "conclusion_evidence": [{
                    "statement": "主题资料形成了一个可继续整编的方向。",
                    "evidence": [{"evidence_id": "E-1", "source_path": "topic.txt", "text": "可追溯主题证据"}],
                }],
            }
            archive_path = export_node(
                root, root, summary, Path(folder), 1024 * 1024,
                task_topic="主题整编", member_paths=["topic.txt"],
                node_name="测试主题", node_id="group-test",
            )
            with zipfile.ZipFile(str(archive_path)) as archive:
                self.assertIn("topic.txt", archive.namelist())
                self.assertIn("结论-证据链.json", archive.namelist())
                handoff = json.loads(archive.read("整编任务说明.json").decode("utf-8"))
            self.assertEqual(handoff["selected_node_id"], "group-test")

    def test_combined_export_deduplicates_files_and_records_handoff_nodes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "package"
            root.mkdir()
            (root / "a.txt").write_text("证据 A", encoding="utf-8")
            (root / "b.txt").write_text("证据 B", encoding="utf-8")
            archive_path = export_node(
                root, root, {"summary": "组合摘要"}, Path(folder), 1024 * 1024,
                task_topic="组合整编主题", member_paths=["a.txt", "a.txt", "b.txt"],
                node_name="组合节点", node_id="combined-test",
                selection_metadata=[{"name": "主题 A"}, {"name": "主题 B"}],
            )
            with zipfile.ZipFile(str(archive_path)) as archive:
                handoff = json.loads(archive.read("整编任务说明.json").decode("utf-8"))
                manifest = json.loads(archive.read("导出清单.json").decode("utf-8"))
            self.assertEqual(handoff["selection_mode"], "combined")
            self.assertEqual(handoff["unique_source_file_count"], 2)
            self.assertEqual(manifest["unique_source_file_count"], 2)

    def test_export_reports_progress_and_commits_zip_atomically(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "package"
            root.mkdir()
            (root / "a.txt").write_text("A", encoding="utf-8")
            calls = []
            archive_path = export_node(
                root, root, {"summary": "摘要"}, Path(folder), 1024 * 1024,
                task_topic="主题",
                progress_callback=lambda *args: calls.append(args),
            )
            self.assertTrue(archive_path.exists())
            self.assertFalse(archive_path.with_name(archive_path.name + ".part").exists())
            self.assertEqual(calls[-1][0], 1)

    def test_large_package_processes_all_files_in_bounded_batches(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for index in range(4):
                (root / "doc{}.txt".format(index)).write_text(
                    "大数据包代表资料 {} 主题证据".format(index) * 120, encoding="utf-8"
                )
            scan = scan_directory(root)
            scan["parse_mode"] = "fast"
            storage = Storage(root / "analysis.db")
            scan_id = storage.save_scan(scan)
            analysis = analyze_package(
                scan_id, scan, storage, UnifiedDocumentParser(max_chars=1000),
                large_options={"threshold_bytes": 1, "initial_parse_files": 2, "deepen_batch_files": 2, "overview_chars_per_file": 500},
            )
            coverage = analysis["coverage"]
            self.assertTrue(analysis["policy"]["large_package"]["enabled"])
            self.assertEqual(coverage["inventory_files"], 4)
            self.assertEqual(coverage["parsed_files"], 4)
            self.assertEqual(coverage["pending_files"], 0)
            self.assertFalse(any(node.get("node_type") == "pending_scope" for node in analysis["analysis_tree"]["children"]))
            stored = storage.list_documents(scan_id)
            self.assertEqual(len(stored), 4)
            self.assertTrue(all(not item["payload"]["coverage"].get("overview_sampled") for item in stored))

    def test_ten_gb_package_uses_bounded_batches_but_processes_every_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for index in range(7):
                (root / "doc{}.txt".format(index)).write_text(
                    "远程证明通过硬件信任根验证运行环境状态。" * 1000,
                    encoding="utf-8",
                )
            scan = scan_directory(root)
            scan["total_size"] = 10 * 1024 * 1024 * 1024
            storage = Storage(str(root / "analysis.db"))
            scan_id = storage.save_scan(scan)
            import services.package_analysis as package_analysis
            original = package_analysis._parallel_parse_files
            batch_sizes = []

            def record_batch(*args, **kwargs):
                batch_sizes.append(len(args[1]))
                return original(*args, **kwargs)

            with patch("services.package_analysis._parallel_parse_files", side_effect=record_batch):
                analysis = analyze_package(
                    scan_id,
                    scan,
                    storage,
                    UnifiedDocumentParser(max_chars=12000),
                    large_options={
                        "threshold_bytes": 1,
                        "batch_files": 3,
                        "overview_chars_per_file": 4000,
                    },
                )
            self.assertEqual(batch_sizes, [3, 3, 1])
            self.assertEqual(analysis["coverage"]["parsed_files"], 7)
            self.assertEqual(analysis["coverage"]["pending_files"], 0)
            self.assertEqual(analysis["policy"]["large_package"]["inventory_bytes"], 10 * 1024 * 1024 * 1024)
            self.assertGreater(len(storage.get_document(scan_id, "doc0.txt")["text"]), 4000)

    def test_content_topics_include_the_end_of_long_documents(self):
        document = {
            "structure": {"headings": ["项目背景", "技术结论"]},
            "text": ("项目背景 通用介绍 " * 3000) + ("远程证明 硬件信任根 " * 2000),
        }
        topics = _content_topics(document, 12)
        self.assertIn("远程证明", topics)

    def test_large_package_scope_resume_reuses_checkpoint_and_extends_coverage(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for index in range(4):
                (root / "doc{}.txt".format(index)).write_text("分批深挖资料 {}".format(index) * 160, encoding="utf-8")
            scan = scan_directory(root)
            scan["parse_mode"] = "fast"
            storage = Storage(root / "analysis.db")
            scan_id = storage.save_scan(scan)
            options = {"threshold_bytes": 1, "initial_parse_files": 2, "deepen_batch_files": 2, "overview_chars_per_file": 500}
            analyze_package(scan_id, scan, storage, UnifiedDocumentParser(max_chars=1000), large_options=options)
            analysis = analyze_package(
                scan_id, scan, storage, UnifiedDocumentParser(max_chars=1000),
                large_options=options, target_paths=["doc1.txt"],
            )
            self.assertGreaterEqual(analysis["coverage"]["parsed_files"], 3)
            self.assertLessEqual(analysis["coverage"]["pending_files"], 1)

    def test_retry_success_removes_the_historical_failure_for_the_same_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "retry.txt").write_text("重试后成功的正文", encoding="utf-8")
            scan = scan_directory(root)
            scan["parse_mode"] = "fast"
            storage = Storage(root / "analysis.db")
            scan_id = storage.save_scan(scan)
            storage.set_file_state(scan_id, "retry.txt", "obsolete", "failed", error="旧错误")

            analysis = analyze_package(
                scan_id, scan, storage, UnifiedDocumentParser(max_chars=1000)
            )

            self.assertEqual(analysis["coverage"]["failed_files"], 0)
            self.assertFalse(any(item.get("path") == "retry.txt" for item in analysis["failures"]))
            self.assertEqual(storage.get_file_state(scan_id, "retry.txt")["status"], "completed")
            self.assertIn("重试后成功", storage.get_document(scan_id, "retry.txt")["text"])

    def test_retry_failure_deletes_the_old_document_and_its_text(self):
        class AlwaysFailParser:
            docling_device = "cpu"

            def parse(self, *_args, **_kwargs):
                raise ValueError("本次解析失败")

            def status(self):
                return {"available": True}

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "retry.txt").write_text("当前文件", encoding="utf-8")
            scan = scan_directory(root)
            scan["parse_mode"] = "fast"
            storage = Storage(root / "analysis.db", sidecar_threshold=32768)
            scan_id = storage.save_scan(scan)
            storage.save_document(scan_id, "retry.txt", {
                "source": {"path": "retry.txt", "sha256": "0" * 64},
                "text": "不应泄漏的旧正文" * 5000,
                "evidence": [{"source_path": "retry.txt", "text": "旧证据"}],
                "coverage": {"complete": True},
            })
            self.assertTrue(list((root / "document_payloads" / scan_id).glob("*.json.gz")))
            storage.set_file_state(scan_id, "retry.txt", "obsolete", "failed", error="旧错误")

            analysis = analyze_package(scan_id, scan, storage, AlwaysFailParser())

            self.assertEqual(analysis["coverage"]["parsed_files"], 0)
            self.assertEqual(analysis["coverage"]["failed_files"], 1)
            self.assertIsNone(storage.get_document(scan_id, "retry.txt"))
            self.assertFalse(list((root / "document_payloads" / scan_id).glob("*.json.gz")))
            self.assertEqual(storage.get_file_state(scan_id, "retry.txt")["status"], "failed")
            self.assertNotIn("不应泄漏的旧正文", json.dumps(analysis, ensure_ascii=False))

    def test_large_document_payload_is_stored_in_sidecar_not_sqlite_blob(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            storage = Storage(root / "analysis.db", root / "sidecars", sidecar_threshold=32768)
            payload = {"source": {"path": "large.txt"}, "text": "x" * 50000, "evidence": []}
            storage.save_document("scan-1", "large.txt", payload)
            self.assertEqual(storage.get_document("scan-1", "large.txt")["text"], payload["text"])
            projection = storage.list_documents("scan-1", hydrate=False)[0]["payload"]
            self.assertTrue(projection["sidecar_stored"])
            self.assertLess(len(projection["text"]), len(payload["text"]))
            self.assertTrue(list((root / "sidecars" / "scan-1").glob("*.json.gz")))

    def test_nonhydrated_inline_documents_are_projected_and_iterated_in_batches(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            storage = Storage(
                root / "analysis.db", root / "sidecars",
                sidecar_threshold=2 * 1024 * 1024,
            )
            long_text = "内联正文" * 62500
            profiles = [{
                "member": "sheet-{}.csv".format(profile_index),
                "profile": {
                    "status": "completed",
                    "coverage": {"complete": True},
                    "columns": {"金额": {"sum": profile_index}},
                },
            } for profile_index in range(40)]
            for index in range(3):
                storage.save_document("scan-1", "{}.txt".format(index), {
                    "source": {"path": "{}.txt".format(index)},
                    "text": long_text,
                    "evidence": [],
                    "coverage": {"complete": True},
                    "data_profiles": profiles,
                })

            projected = list(storage.iter_documents(
                "scan-1", hydrate=False, batch_size=1
            ))
            hydrated = storage.list_documents("scan-1", hydrate=True)

            self.assertEqual([item["path"] for item in projected], ["0.txt", "1.txt", "2.txt"])
            self.assertTrue(all(len(item["payload"]["text"]) < len(long_text) for item in projected))
            self.assertTrue(all(item["payload"]["coverage"]["semantic_projection"] for item in projected))
            self.assertTrue(all(item["payload"]["data_profiles_total"] == 40 for item in projected))
            self.assertTrue(all(item["payload"]["data_profiles_projected_count"] == 30 for item in projected))
            self.assertTrue(all(item["payload"]["data_profiles_omitted_count"] == 10 for item in projected))
            self.assertEqual(
                projected[0]["payload"]["data_profiles"][0]["profile"]["columns"]["金额"]["sum"],
                0,
            )
            self.assertTrue(all(item["payload"]["text"] == long_text for item in hydrated))

    def test_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(ValueError):
                resolve_under(root, "../outside.txt")

    def test_source_changed_after_scan_is_rejected_before_parse(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "copying.zip"
            path.write_bytes(b"first")
            file_node = next(_walk_files(scan_directory(root)["tree"]))
            path.write_bytes(b"first-second")
            with self.assertRaisesRegex(SourceFileChangedError, "仍在复制|扫描后发生变化"):
                _assert_source_stable(path, file_node)

    def test_parse_uses_a_verified_private_snapshot_and_restores_provenance(self):
        class RecordingParser:
            def __init__(self):
                self.paths = []

            def parse(self, path, relative_path, mode="fast"):
                self.paths.append(Path(path))
                return {"source": {}, "text": Path(path).read_text(encoding="utf-8")}

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "inside.txt"
            source.write_text("受保护正文", encoding="utf-8")
            file_node = next(_walk_files(scan_directory(root)["tree"]))
            scratch = root / "scratch"
            parser = RecordingParser()
            with patch("services.package_analysis.Config.PARSE_TEMP_DIR", scratch), patch(
                "services.package_analysis.Config.PARSE_TEMP_DISK_RESERVE_BYTES", 0
            ):
                result = _parallel_parse_files(parser, [file_node], root, "fast")
            document = result[0][2]
            self.assertEqual(document["text"], "受保护正文")
            self.assertEqual(document["source"]["path"], "inside.txt")
            self.assertTrue(document["source"]["source_snapshot_verified"])
            self.assertNotEqual(parser.paths[0], source)
            self.assertFalse(parser.paths[0].exists())

    def test_secure_snapshot_rejects_a_file_replaced_by_symlink(self):
        if not hasattr(os, "symlink"):
            self.skipTest("当前平台不支持符号链接")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "inside.txt"
            outside = root.parent / (root.name + "-outside.txt")
            source.write_text("诱饵", encoding="utf-8")
            outside.write_text("根外秘密", encoding="utf-8")
            file_node = next(_walk_files(scan_directory(root)["tree"]))
            source.unlink()
            try:
                os.symlink(str(outside), str(source))
            except OSError as exc:
                outside.unlink()
                self.skipTest("无法创建测试符号链接：{}".format(exc))
            scratch = root / "scratch"
            try:
                with patch("services.package_analysis.Config.PARSE_TEMP_DIR", scratch), patch(
                    "services.package_analysis.Config.PARSE_TEMP_DISK_RESERVE_BYTES", 0
                ):
                    with self.assertRaises((SourceFileChangedError, OSError)):
                        with _secure_source_snapshot(root, file_node):
                            pass
            finally:
                outside.unlink()

    def test_secure_snapshot_preserves_sensitive_filename_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / ".env"
            source.write_text("TOP_SECRET=must-not-enter-model", encoding="utf-8")
            file_node = next(_walk_files(scan_directory(root)["tree"]))
            scratch = root / "scratch"
            with patch("services.package_analysis.Config.PARSE_TEMP_DIR", scratch), patch(
                "services.package_analysis.Config.PARSE_TEMP_DISK_RESERVE_BYTES", 0
            ):
                result = _parallel_parse_files(
                    UnifiedDocumentParser(max_chars=1000), [file_node], root, "fast"
                )
            document = result[0][2]
            self.assertTrue(document["source"]["sensitive"])
            self.assertEqual(document["source"]["content_policy"], "metadata_only_sensitive")
            self.assertEqual(document["text"], "")
            self.assertTrue(document["coverage"]["content_restricted"])

    def test_bad_zip_warning_explains_incomplete_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "broken.zip"
            path.write_bytes(b"PK\x03\x04incomplete")
            document = UnifiedDocumentParser(max_chars=1000).parse(path, "broken.zip")
            self.assertTrue(document["parser"]["degraded"])
            self.assertTrue(any(
                "ZIP 中央目录" in item and ("预检失败" in item or "仍在复制" in item)
                for item in document["warnings"]
            ))

    def test_archive_container_has_separate_size_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bounded.zip"
            path.write_bytes(b"PK\x03\x04incomplete")
            with patch.dict("os.environ", {"MAX_SINGLE_FILE_BYTES": "4", "MAX_ARCHIVE_FILE_BYTES": "1024"}):
                document = UnifiedDocumentParser(max_chars=1000).parse(path, "bounded.zip")
            self.assertTrue(document["parser"].get("archive"))
            self.assertNotEqual(document["parser"]["name"], "metadata-only")

    def test_linux_open_archive_writer_is_detected(self):
        if not Path("/proc/self/fd").is_dir():
            self.skipTest("Linux /proc is required for open-writer detection")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "upload.zip"
            with path.open("wb") as handle:
                handle.write(b"partial")
                handle.flush()
                self.assertTrue(_source_has_open_writer(path))

    def test_long_text_reports_coverage_and_chunks_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "long.txt"
            path.write_text("证据内容。" * 400, encoding="utf-8")
            document = UnifiedDocumentParser(max_chars=1000).parse(path, "long.txt")
            self.assertFalse(document["coverage"]["complete"])
            self.assertLess(document["coverage"]["coverage_ratio"], 1)
            self.assertGreaterEqual(len(document["evidence"]), 1)
            self.assertEqual(document["evidence"][0]["label"], "text_chunk")

    def test_fast_mode_uses_lightweight_parser(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "brief.md"
            path.write_text("快速解析测试正文", encoding="utf-8")
            document = UnifiedDocumentParser(max_chars=1000).parse(path, "brief.md", mode="fast")
            self.assertEqual(document["parser"]["mode"], "fast")
            self.assertEqual(document["parser"]["name"], "text")
            self.assertIn("快速解析测试正文", document["text"])

    def test_small_files_and_all_folders_have_immediate_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "small.txt").write_text("根目录小文件", encoding="utf-8")
            (root / "nested").mkdir()
            (root / "nested" / "child.txt").write_text("子目录小文件", encoding="utf-8")
            scan = scan_directory(root)
            scan["parse_mode"] = "fast"
            storage = Storage(root / "analysis.db")
            scan_id = storage.save_scan(scan)
            analysis = analyze_package(scan_id, scan, storage, UnifiedDocumentParser(max_chars=1000))
            summaries = {(item["path"], item["type"]) for item in storage.list_summaries(scan_id)}
            self.assertIn((".", "folder"), summaries)
            self.assertIn(("nested", "folder"), summaries)
            self.assertIn(("small.txt", "file"), summaries)
            self.assertIn(("nested/child.txt", "file"), summaries)
            self.assertEqual(analysis["statistics"]["small_file_summary_skipped"], 0)

    def test_analysis_tree_classifies_same_file_type_by_content(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "requirements.txt").write_text(
                "产品定位与功能要求：系统应满足验收标准并记录用户故事。", encoding="utf-8"
            )
            (root / "research.txt").write_text(
                "摘要：本文介绍实验设计。关键词：模型。研究方法包括对照实验，最后给出参考文献。", encoding="utf-8"
            )
            (root / "sales.txt").write_text(
                "区域,销售额,增长率\n华东,1280,12%\n华南,960,-4%", encoding="utf-8"
            )
            scan = scan_directory(root)
            scan["parse_mode"] = "fast"
            storage = Storage(root / "analysis.db")
            scan_id = storage.save_scan(scan)
            analysis = analyze_package(scan_id, scan, storage, UnifiedDocumentParser(max_chars=1000))

            tree = analysis["analysis_tree"]
            self.assertEqual(tree["dimensions"][0]["name"], "内容主题")
            self.assertTrue(tree["children"])
            self.assertNotIn("文档格式", {item["dimension"] for item in tree["children"]})
            self.assertTrue(all(item["dimension"] == "内容主题" for item in tree["children"]))
            self.assertTrue(all(item.get("node_type") == "topic" for item in tree["children"]))
            self.assertTrue(all(item.get("children") for item in tree["children"]))
            leaves = [leaf["path"] for leaf in _walk_files(tree)]
            self.assertCountEqual(leaves, ["requirements.txt", "research.txt", "sales.txt"])

    def test_document_role_does_not_depend_on_extension(self):
        content = "摘要：研究方法采用对照实验。关键词：安全。参考文献共十篇。"
        base = {"structure": {"headings": []}, "text": content}
        txt_document = {**base, "source": {"name": "sample.txt", "extension": ".txt"}}
        csv_document = {**base, "source": {"name": "sample.csv", "extension": ".csv"}}
        self.assertEqual(_document_role(txt_document), "研究文献")
        self.assertEqual(_document_role(csv_document), "研究文献")

    def test_research_document_is_not_reclassified_by_one_requirement_phrase(self):
        document = {
            "source": {"name": "paper.txt"},
            "structure": {"headings": ["摘要", "研究方法", "实验结果", "参考文献"]},
            "text": "本文讨论相关验收标准，并使用对照实验验证结论。",
        }
        self.assertEqual(_document_role(document), "研究文献")

    def test_topic_cluster_uses_cooccurring_keywords_when_available(self):
        documents = {
            "a.txt": {"source": {"name": "a"}, "structure": {"headings": []}, "text": "网络安全 数据隐私 网络安全 数据隐私 访问控制", "evidence": []},
            "b.txt": {"source": {"name": "b"}, "structure": {"headings": []}, "text": "网络安全 数据隐私 网络安全 数据隐私 风险评估", "evidence": []},
        }
        cluster = _topic_clusters(documents)[0]
        self.assertIn("/", cluster["topic"])
        self.assertGreaterEqual(len(cluster["keywords"]), 2)

    def test_simhash_similarity_path_is_python37_compatible(self):
        base = "网络安全 数据隐私 访问控制 实验结果 风险评估 方法结论 " * 100
        shortened = base[:1200]
        self.assertLessEqual(_hamming(simhash64(base), simhash64(shortened)), 14)

    def test_similar_cluster_accepts_high_containment_length_variant(self):
        base = "网络安全 数据隐私 访问控制 实验结果 风险评估 方法结论 " * 100
        shortened = base[:1200]
        extended = base + ("其他背景内容 供应链 财务 报告 " * 2000)
        documents = {
            "abstract.txt": {"source": {"sha256": "a"}, "text": shortened},
            "full.txt": {"source": {"sha256": "b"}, "text": extended},
        }
        groups = _group_similar(documents, [])
        self.assertEqual(groups[0]["canonical"], "full.txt")
        self.assertEqual(groups[0]["derived_from"]["abstract.txt"], "full.txt")

    def test_fallback_pdf_markers_keep_page_number(self):
        parser = UnifiedDocumentParser(max_chars=1000)
        base = {
            "source": {"path": "scan.pdf", "sha256": "a" * 64},
            "parser": {"name": "PyPDF2"},
            "text": "[第 7 页]\n这里是可回查的 PDF 正文。",
            "warnings": [],
        }
        parser._add_fallback_evidence(base)
        self.assertEqual(base["evidence"][0]["page"], 7)

    def test_report_lists_fast_preview_paths(self):
        scan = self._overview_scan([])
        report = build_local_report(scan, [], {
            "statistics": {"parsed_files": 0, "fast_preview_paths": ["scans/core.pdf"]},
        })
        self.assertTrue(any("scans/core.pdf" in item for item in report["key_findings"]))

    def test_evidence_projection_is_topic_aligned_and_bounded(self):
        items = [
            {"evidence_id": "E-1", "source_path": "a.pdf", "page": 1, "text": "网络安全访问控制的实验结果。"},
            {"evidence_id": "E-2", "source_path": "a.pdf", "page": 2, "text": "无关背景。" * 300},
            {"evidence_id": "E-3", "source_path": "b.pdf", "page": 4, "text": "网络安全风险评估结论。"},
        ]
        selected = select_evidence(items, topics=["网络安全"], max_items=2, per_source=1, max_chars=80)
        self.assertEqual(len(selected), 2)
        self.assertEqual({item["evidence_id"] for item in selected}, {"E-1", "E-3"})
        self.assertTrue(all(len(item["text"]) <= 80 for item in selected))
        self.assertTrue(all("excerpt" in item for item in selected))

    def test_semantic_embedding_can_link_unmatched_terms_with_lexical_fallback(self):
        def fake_embed(values):
            vectors = []
            for value in values:
                vectors.append([1.0, 0.0] if value == "供应链中断" or "港口" in value else [0.0, 1.0])
            return vectors

        set_embedding_provider(fake_embed)
        try:
            selected = select_evidence([
                {"evidence_id": "E-port", "source_path": "a.txt", "page": 1, "text": "港口关闭导致物流延误。"},
                {"evidence_id": "E-other", "source_path": "b.txt", "page": 1, "text": "年度财务报表已归档。"},
            ], topics=["供应链中断"], max_items=1, per_source=1)
        finally:
            set_embedding_provider(None)
        self.assertEqual(selected[0]["evidence_id"], "E-port")
        self.assertEqual(selected[0]["relevance_mode"], "semantic+lexical")
        self.assertGreater(selected[0]["semantic_score"], 0.9)


if __name__ == "__main__":
    unittest.main()
