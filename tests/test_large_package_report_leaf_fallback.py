"""Reports retain readable named leaves when a small catalog has no parent themes."""
from contextlib import closing
import json
from types import SimpleNamespace

import pytest

from config import Config
from services import large_package_reporting as reports
from services.large_package_isolated import LargePackageStore
from sjfx_protection import business


BODY = "相变材料在温度循环试验中保持稳定，记录了样品制备条件、相变温度、重复测量误差及适用限制。"
LEAF = "相变材料温度循环与稳定性"
PATH = "docs/sample.txt"


@pytest.fixture
def report_case(tmp_path, monkeypatch):
    monkeypatch.setattr(business, "enabled", lambda: False)
    monkeypatch.setattr(Config, "SCAN_ALLOWED_ROOTS", ())
    monkeypatch.setattr(Config, "LP_DIRECTIONS_ENABLED", True)
    monkeypatch.setattr(Config, "LP_DIRECTION_HOLISTIC", False)
    monkeypatch.setattr(Config, "LP_REPORT_CACHE_FIX", True)
    monkeypatch.setattr(reports, "_direction_budget_ok", lambda *args: True)
    monkeypatch.setattr(reports, "_record_direction_call", lambda *args: None)
    calls = []

    def chat(system, user, **kwargs):
        calls.append(user)
        return {"content": json.dumps({"directions": [{
            "title": "温度循环：相变材料稳定性如何变化",
            "why": "材料记录了相变温度、重复试验误差和样品制备条件，可据此比较温度循环与稳定性之间的关系。",
            "questions": ["不同循环次数的相变温度是否一致？", "样品制备条件如何影响重复测量误差？"],
            "methods": ["对照循环试验与制备条件 → 条件与误差比较表"],
            "evidence_paths": [PATH], "confidence": 0.7,
        }]}, ensure_ascii=False), "usage": {"total_tokens": 100}}

    client = SimpleNamespace(supports_json_response_format=True, chat=chat)
    monkeypatch.setattr(reports, "_direction_client", lambda: client)
    store = LargePackageStore(tmp_path / "synthetic.db")
    pid = store.create(tmp_path)["id"]
    store.update_package(pid, status="waiting_for_selection", phase="catalog_ready")

    def seed(category="内容待归纳", leaf=LEAF, parent=None, text=BODY):
        with closing(store._connect()) as conn:
            conn.execute(
                "INSERT INTO files(package_id,path,name,quick_status,category,local_summary,updated_at) "
                "VALUES(?,?,?,'completed',?,?,1)",
                (pid, PATH, "sample.txt", category,
                 json.dumps({"sample": text, "keywords": ["相变材料", "温度循环"]}, ensure_ascii=False)))
            conn.execute(
                "INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,"
                "result,status,updated_at,content_revision,member_count) "
                "VALUES(?,'leaf-one','test',zeroblob(0),?,'{}','named',1,1,1)", (pid, PATH))
            conn.execute(
                "INSERT INTO large_catalog_members(package_id,path,group_id,input_hash,vector) "
                "VALUES(?,?,'leaf-one','synthetic',zeroblob(0))", (pid, PATH))
            conn.execute(
                "INSERT INTO lp_catalog_labels(package_id,group_id,revision,version,name,description,"
                "evidence,model,digest,needs_review,locked,updated) "
                "VALUES(?,'leaf-one',1,1,?,'合成试验资料','[]','test','synthetic',0,0,1)", (pid, leaf))
            if parent is not None:
                conn.execute(
                    "INSERT INTO lp_catalog_branches(package_id,id,name,children,revisions,round_id,updated) "
                    "VALUES(?,'parent-one',?,'[\"leaf-one\"]','{}','test',1)", (pid, parent))
            conn.commit()

    return SimpleNamespace(store=store, pid=pid, seed=seed, calls=calls,
                           build=lambda: reports.build_report(store, store.get(pid)))


@pytest.mark.parametrize("category", ["内容待归纳", "未分类", ""])
def test_readable_named_leaf_without_parent_generates_research_direction(report_case, category):
    case = report_case
    case.seed(category=category)
    report = case.build()
    brief = report["research_brief"]
    assert report["categories"] == {LEAF: 1}
    assert report["coverage"]["quick_completed"] == 1
    assert report["coverage"]["deep_completed"] == 0
    assert brief["direction_source"] == "model_evidence"
    assert brief["recommended_research_direction"]["representative_documents"] == [PATH]
    assert brief["recommended_research_direction"]["evidence_chain"][0]["text"] == BODY
    assert len(case.calls) == 1 and LEAF in case.calls[0]
    assert brief["preliminary"] is True


def test_parent_theme_remains_above_original_category_and_model_leaf(report_case):
    case = report_case
    case.seed(category="项目与技术", parent="材料性能与实验条件")
    report = case.build()
    assert report["categories"] == {"材料性能与实验条件": 1}
    assert json.loads(case.calls[0].split("证据汇总：\n", 1)[1].split("\n\n覆盖率：", 1)[0])["topics"][0]["topic"] == "材料性能与实验条件"


@pytest.mark.parametrize("parent", [None, "未分类"])
def test_existing_valid_category_is_preserved_before_model_leaf(report_case, parent):
    case = report_case
    case.seed(category="项目与技术", parent=parent)
    assert case.build()["categories"] == {"项目与技术": 1}


@pytest.mark.parametrize("leaf", ["内容待归纳", "待识别材料", ""])
def test_placeholder_leaves_with_body_do_not_invent_directions(report_case, leaf):
    case = report_case
    case.seed(leaf=leaf)
    brief = case.build()["research_brief"]
    assert brief["recommended_research_direction"] == {}
    assert brief["directions_total"] == 0
    assert brief["direction_reason"] == "no_evidence"
    assert case.calls == []


def test_named_leaf_without_readable_body_does_not_invent_direction(report_case):
    case = report_case
    case.seed(text="")
    report = case.build()
    assert report["categories"] == {LEAF: 1}
    assert report["coverage"]["metadata_summaries"] == 1
    assert report["research_brief"]["recommended_research_direction"] == {}
    assert report["research_brief"]["direction_reason"] == "no_evidence"
    assert case.calls == []


def test_model_failure_still_has_source_backed_leaf_fallback(report_case, monkeypatch):
    case = report_case
    case.seed()
    monkeypatch.setattr(reports, "_direction_client", lambda: None)
    brief = case.build()["research_brief"]
    assert brief["direction_source"] == "template"
    assert brief["recommended_research_direction"]["category"] == LEAF
    assert brief["recommended_research_direction"]["evidence_chain"][0]["text"] == BODY


def test_schema_upgrade_invalidates_report_cache_without_changing_catalog(report_case, monkeypatch):
    case = report_case
    case.seed()
    current_schema = reports.SCHEMA
    assert current_schema == "large-package-report/2.7"
    with monkeypatch.context() as old:
        old.setattr(reports, "SCHEMA", "large-package-report/2.6")
        previous = case.build()
    # The same source/catalog state must no longer reuse the pre-fix snapshot.
    current = case.build()
    assert current["revision"] != previous["revision"]
    assert current["schema_version"] == current_schema
    assert len(case.calls) == 2
    assert case.build()["revision"] == current["revision"]
    assert len(case.calls) == 2
