"""Small source-isolated checks: no app import, runtime DB or model process."""
import ast
import copy
import hashlib
import json
import re
import sqlite3
import time
from contextlib import nullcontext
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from services import folder_analysis as folders
from services.import_state_machine import deep_report_outcome, summary_type
from services.research_opportunities import RESEARCH_VERSION


SOURCE = Path(__file__).resolve().parents[1]


def app_functions(store, **overrides):
    names = {
        "_preliminary_summary_content_fingerprint", "_preliminary_summary_version_current",
        "_preliminary_summary_identity", "_preliminary_member_source_fingerprint",
        "_preliminary_overview_source_fingerprint", "_ordinary_research_evidence_fingerprint",
        "_ordinary_research_current_evidence_fingerprint",
        "_ordinary_report_source_fingerprint", "_node_member_source_fingerprint",
        "_node_summary_reuse", "_prepare_node_summary", "_virtual_node_context",
        "_generate_node_summary", "_write_local_overview", "_overview_report_fingerprint",
    }
    tree = ast.parse((SOURCE / "app.py").read_text(encoding="utf-8"))
    declarations = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    constants = [n for n in tree.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id in {"PRELIMINARY_SUMMARY_VERSION_PAIRS", "_OVERVIEW_VOLATILE_KEYS"} for t in n.targets)]
    namespace = dict(storage=store, hashlib=hashlib, json=json, time=time, re=re,
                     llm_generation_enabled=False, nullcontext=nullcontext,
                     datetime=datetime, Path=Path, Counter=Counter,
                     staged_summary_type=summary_type,
                     document_source_fingerprint=lambda d: hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest(),
                     _preliminary_expected_node_members=lambda *_: {"node:n": {"a.md"}},
                     _walk_analysis_nodes=lambda tree: tree.get("children") or [],
                     _package_documents=lambda *_args, **_kwargs: [{"path": p, "payload": d} for p, d in store.documents.items()],
                     human_size=lambda value: str(value))
    namespace.update(overrides)
    exec(compile(ast.Module(body=constants + declarations, type_ignores=[]), "app-gates", "exec"), namespace)
    return namespace


class Store:
    def __init__(self):
        self.task = {"checkpoint": {"selected_paths": ["a.md"], "selection_version": 1,
                                    "research_goal": "比较验证边界"}}
        self.analysis = {"analysis_tree": {"children": [{"node_id": "n", "name": "主题1", "member_paths": ["a.md"]}]}}
        self.documents = {"a.md": {"source": {"path": "a.md"}, "text": "采用对照方法，仅在小样本验证。", "coverage": {"complete": True}, "evidence": []}}
        self.evidence = [{"source_path": "a.md", "evidence_id": "E-a", "text": "采用对照方法，仅在小样本验证。"}]
        self.summaries = {}
        self.saved = []

    def get_import_task(self, *_): return copy.deepcopy(self.task)
    def get_analysis(self, *_): return self.analysis
    def get_document(self, _scan, path): return copy.deepcopy(self.documents.get(path))
    def get_summary(self, _scan, path, kind): return copy.deepcopy(self.summaries.get((path, kind)))
    def list_summaries(self, *_): return [{"path": p, "type": kind, "payload": copy.deepcopy(value)} for (p, kind), value in self.summaries.items()]
    def list_evidence_index(self, *_): return copy.deepcopy(self.evidence)
    def get_relationship_catalog(self, *_): return {}
    def get_content_map(self, *_): return {}
    def _connect(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE evidence_index (scan_id TEXT, payload TEXT)")
        conn.executemany("INSERT INTO evidence_index VALUES (?,?)", [("s", json.dumps(item)) for item in self.evidence])
        return conn
    def artifact_owner(self, *_): return "owner"
    def save_artifact(self, *args, **kwargs): self.saved.append((args, kwargs))
    def save_summary(self, _scan, path, kind, payload):
        self.summaries[path, kind] = copy.deepcopy(payload)
        self.saved.append((path, kind))
    def save_analysis(self, *_): raise AssertionError("report discovery must not rebuild directory")


def ready_store():
    store = Store()
    ns = app_functions(store)
    file_summary = {"analysis_stage": "preliminary", "summary": "材料采用对照方法，尚未验证大样本。",
                    "summary_input_version": "preview-source-blocks-v3-sections",
                    "summary_prompt_version": "preview-claims-qualifiers-v4-sections",
                    "source_fingerprint": ns["document_source_fingerprint"](store.documents["a.md"])}
    store.summaries["a.md", summary_type("preliminary", "file")] = file_summary
    identity = ns["_preliminary_summary_identity"]("s", "a.md")
    store.summaries["node:n", summary_type("preliminary", "node")] = {
        "analysis_stage": "preliminary", "member_paths": ["a.md"], "title": "已核验主题",
        "summary": "节点局部研究", "member_source_fingerprint": ns["_preliminary_member_source_fingerprint"]([identity])}
    return store, ns


def test_preliminary_fingerprint_requires_all_expected_nodes_and_exact_members():
    store, ns = ready_store()
    fp = ns["_preliminary_overview_source_fingerprint"]
    assert fp("s")
    ns["_preliminary_expected_node_members"] = lambda *_: {"node:n": {"a.md"}, "node:missing": {"a.md"}}
    assert fp("s") == ""
    ns["_preliminary_expected_node_members"] = lambda *_: {"node:n": {"a.md", "b.md"}}
    assert fp("s") == ""


def test_recipe_body_goal_and_local_direction_invalidate_fingerprints(monkeypatch):
    store, ns = ready_store()
    preliminary = ns["_preliminary_overview_source_fingerprint"]
    report = ns["_ordinary_report_source_fingerprint"]
    first = preliminary("s")
    report_first = report("s", "preliminary")
    store.task["checkpoint"]["research_goal"] = "复核资源条件"
    assert preliminary("s") != first and report("s", "preliminary") != report_first
    store.evidence[0]["text"] += " 相反，方法在噪声条件下失败。"
    assert report("s", "preliminary") != report_first
    old = ns["_preliminary_summary_content_fingerprint"](store.summaries["a.md", summary_type("preliminary", "file")])
    store.summaries["a.md", summary_type("preliminary", "file")]["recommended_research_direction"] = {"title": "新的局部问题"}
    assert ns["_preliminary_summary_content_fingerprint"](store.summaries["a.md", summary_type("preliminary", "file")]) != old
    monkeypatch.setattr(folders, "NODE_SUMMARY_INPUT_VERSION", "test-next-recipe")
    assert preliminary("s") == ""


def test_body_fingerprint_is_order_stable_and_scope_filtered():
    ns = app_functions(Store())
    fp = ns["_ordinary_research_evidence_fingerprint"]
    local = {"source_path": "a.md", "evidence_id": "A", "text": "局部正文"}
    outside = {"source_path": "other.md", "evidence_id": "O", "text": "全包其他内容"}
    assert fp([local, outside], ["a.md"]) == fp([outside, local], ["a.md"])
    outside["text"] = "其他范围改动"
    assert fp([local, outside], ["a.md"]) == fp([local], ["a.md"])


def test_historical_model_inputs_are_not_current_but_explicit_local_fallback_is():
    ns = app_functions(Store())
    old = {"summary_input_version": "preview-source-blocks-v2", "summary_prompt_version": "preview-claims-evidence-v2"}
    assert not ns["_preliminary_summary_version_current"](old)
    assert ns["_preliminary_summary_version_current"](dict(old, generated_by="local-preliminary-fallback", parser_info={"degraded": True}))


def test_deep_node_must_wait_for_own_deep_members():
    store = Store()
    context = {"member_paths": ["a.md"], "file_summaries_complete": True, "deep_file_summaries_complete": False}
    ns = app_functions(store, _find_analysis_node=lambda *_: {"member_paths": ["a.md"], "node_id": "n"})
    ns["_virtual_node_context"] = lambda *_args, **_kwargs: context
    with pytest.raises(ValueError, match="成员深度摘要未就绪"):
        ns["_prepare_node_summary"]("s", {"node_id": "n", "workflow_source": "deep_node_rebuild", "member_paths": ["a.md"]})


def test_member_body_change_invalidates_deep_node_cache():
    store = Store()
    store.summaries["a.md", summary_type("deep", "file")] = {"summary": "本地深度摘要", "deep_analysis": True}
    ns = app_functions(store)
    before = ns["_node_member_source_fingerprint"]("s", ["a.md"], "deep")
    store.documents["a.md"]["text"] += " 未验证其他条件。"
    assert ns["_node_member_source_fingerprint"]("s", ["a.md"], "deep") != before


def test_node_input_and_hierarchical_map_keep_local_constraints():
    rows = [{"path": "a.md", "summary": "资料摘要", "methodology": ["小样本对照"],
             "research_context": {"limitation": [{"text": "仅在小样本验证，尚未测试外部资料。", "evidence_id": "L"}],
                                  "condition": [{"text": "方法不适用于高噪声条件。", "evidence_id": "C"}]}}]
    original = copy.deepcopy(rows)
    selected, _, _ = folders._bounded_node_inputs(rows, [], [])
    assert "方法不适用于高噪声条件。" in selected[0]["limitations"]
    digest = folders._local_batch_digest(0, 1, rows)
    assert digest["batch_paths"] == ["a.md"]
    assert digest["research_context"]["condition"][0]["source_path"] == "a.md"
    assert rows == original


def test_inherited_support_cannot_borrow_other_node_evidence():
    context = {"member_paths": ["a.md"], "file_summaries": [{"path": "a.md", "file_conclusions": [{
        "statement": "本节点结论", "supports": [{"evidence_id": "foreign", "source_path": "other.md", "text": "其他节点的正文证据足够长。"}]}]}]}
    assert folders._file_summary_evidence(context, 8) == []


def test_report_projection_does_not_invalidate_own_directory_fingerprint():
    store, ns = ready_store()
    candidate = {"candidate_id": "c1", "title": "验证小样本边界", "quality_gate": {"status": "passed"}}
    ns.update(Config=SimpleNamespace(OVERVIEW_EXCLUDE_FALLBACK=True),
              require_scan=lambda *_: {"root": "/fixture", "file_count": 1},
              is_fallback_summary=lambda *_: False,
              build_local_report=lambda *_args, **_kwargs: {"research_version": RESEARCH_VERSION,
                  "recommended_research_direction": copy.deepcopy(candidate), "direction_candidates": [copy.deepcopy(candidate)],
                  "coverage": {}, "value_judgment": {}, "limitations": [], "basic_information": []},
              _analyze_report_with_model=lambda _scan, _summaries, _analysis, report, **_kwargs: (report, "fixture", {}, None),
              _inventory_by_path=lambda *_: {"a.md": {"size": 1}},
              _effective_owner=lambda: "owner", _resolve_output_file=lambda *_: None,
              safe_name=lambda *_: "fixture", create_report_docx=lambda *_: None,
              _output_path_for_write=lambda *_: "/unused", KEY_FINDINGS_PLACEHOLDER="placeholder")
    result = ns["_write_local_overview"]("s", owner_id="owner", summary_stage="preliminary",
                                         source_fingerprint=ns["_preliminary_overview_source_fingerprint"]("s"))
    assert result["report"]["research_publication"]["status"] == "passed"
    assert store.analysis["analysis_tree"]["children"][0]["name"] == "主题1"
    assert all(kind != "research_elements" for _, kind in store.summaries)
    store.saved.clear()
    ns["create_report_docx"] = lambda *_: store.evidence[0].update(text="原文依据已在导出时变化。")
    with pytest.raises(ValueError, match="导出期间研究依据已变化"):
        ns["_write_local_overview"]("s", owner_id="owner", summary_stage="preliminary",
                                     source_fingerprint=ns["_preliminary_overview_source_fingerprint"]("s"))
    assert store.saved == []


def test_package_brief_keeps_late_counterevidence_and_all_bounded_candidates():
    store = Store()
    chain = [{"evidence_id": str(i), "text": "背景依据"} for i in range(10)]
    chain.append({"evidence_id": "counter-last", "text": "相反，高噪声条件未能改善结果。"})
    store.summaries[".", "deep_report"] = {
        "recommended_research_direction": {"title": "条件验证", "evidence_chain": chain},
        "direction_candidates": [{"candidate_id": str(i)} for i in range(8)],
    }
    store.latest_artifact = lambda *_args, **_kwargs: None
    ns = dict(storage=store, jsonify=lambda value: value,
              require_scan=lambda *_args, **_kwargs: {"_owner_id": "owner"},
              build_package_overview_from_storage=lambda *_args, **_kwargs: {},
              _published_deep_scope=lambda *_: ({"a.md"}, True))
    tree = ast.parse((SOURCE / "app.py").read_text(encoding="utf-8"))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "package_overview")
    function.decorator_list = []
    exec(compile(ast.Module(body=[function], type_ignores=[]), "package-brief", "exec"), ns)
    brief = ns["package_overview"]("s")["research_brief"]
    assert len(brief["direction_candidates"]) == 8
    assert brief["recommended_research_direction"]["evidence_chain"][-1]["evidence_id"] == "counter-last"


def test_timeout_cannot_close_new_report_and_stale_goal_is_rejected():
    task = {"checkpoint": {"selected_paths": ["a.md"], "selection_version": 1, "research_goal": "当前目标"}}
    report = {"research_version": RESEARCH_VERSION, "research_goal": "当前目标", "research_source_fingerprint": "source",
              "research_publication": {"version": RESEARCH_VERSION, "research_goal": "当前目标", "source_fingerprint": "source", "status": "passed"},
              "report_scope": {"selected_paths": ["a.md"], "selection_version": 1},
              "coverage": {"pipeline_coverage": {"content_parse": {"complete": True}, "deep_analysis": {"complete": True}}},
              "summary_quality": {"stage": "deep", "file_summaries": 1}, "model_analysis": {"status": "failed", "error": "timeout"}}
    assert deep_report_outcome(task, report) == "partial"
    report["model_analysis"] = {"status": "completed"}
    assert deep_report_outcome(task, report) == "completed"
    task["checkpoint"]["research_goal"] = "变更目标"
    assert deep_report_outcome(task, report) is None
    task["checkpoint"]["research_goal"] = "当前目标"
    report["research_publication"]["status"] = "insufficient"
    report["model_analysis"] = {"status": "skipped", "reason": "no_candidate_evidence"}
    assert deep_report_outcome(task, report) == "completed"
