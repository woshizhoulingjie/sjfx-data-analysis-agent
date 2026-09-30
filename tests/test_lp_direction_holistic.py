"""全包综合研究方向（SJFX_LP_DIRECTION_HOLISTIC）回归测试。

覆盖：开关关闭 = 旧路径与旧规则版本；打开后模型读到完整目录（全部主题与文件数）、
首选方向按覆盖面把关、未知主题被拒、分数不再是 10**6、证据带 scope、Word 导出可用。
"""
from collections import Counter
from contextlib import closing
import json

import pytest

from config import Config
from services import large_package_reporting as lpr


def _group(files, leaves, text, path, models=0):
    return {"files": files, "confidence": 0, "text_files": files, "models": models,
            "examples": [{"source_path": path, "source_sha256": path, "text": text,
                          "scope": "快速解析有限预览", "deep": False, "topic": next(iter(leaves))}],
            "keywords": Counter({"政策": 3, "通知": 2}), "leaves": Counter(leaves)}


def _groups():
    return {
        "教育政策与考试招生": _group(3000, {"高考招生通知": 1800, "中考改革": 1200},
                              "教育部关于做好高考招生工作的通知，要求各地规范加分政策。", "edu/a.txt"),
        "卫生健康与医保": _group(2500, {"医保目录调整": 1500, "公共卫生应急": 1000},
                            "国家医保局发布药品目录调整方案，新增药品 111 种。", "health/b.txt"),
        "交通出行与安全提醒": _group(1500, {"春运出行提示": 1400, "世界杯购票防骗": 100},
                              "领事提醒：赴美观看世界杯请警惕购票诈骗。", "travel/c.txt"),
        "未分类": _group(754, {"未能识别内容": 754}, "", "misc/d.bin"),
    }


def _answer(first_topics, first_title="主线：教育与医疗政策如何协同调整公共服务供给"):
    return {
        "package_profile": "该数据包以政策通知为主，教育与卫生健康两条主线合计超过七成文件。" * 3,
        "main_threads": [{"name": "公共服务政策", "topics": ["教育政策与考试招生", "卫生健康与医保"],
                          "summary": "教育与医保政策的调整节奏与协同。"}],
        "directions": [
            {"title": first_title, "why": "综合教育政策（3000 个文件）与卫生健康（2500 个文件）两大主题，"
                                          "二者都呈现规范化调整的趋势，值得对照研究其节奏与机制。",
             "topics": first_topics, "questions": ["问题一", "问题二", "问题三"],
             "methods": ["按年份梳理通知 → 政策时间线", "对照两类政策 → 协同矩阵"], "confidence": 0.8},
            {"title": "公共服务：教育医疗交通三线政策的整体演进", "why": "把教育、卫生健康与交通出行三个主题"
                                                          "放在一起看公共服务政策的整体演进与联动关系。",
             "topics": ["教育政策与考试招生", "卫生健康与医保", "交通出行与安全提醒"],
             "questions": ["问题一", "问题二"], "methods": ["对照 → 矩阵"], "confidence": 0.7},
            {"title": "编造：不存在的主题研究方向示例", "why": "这个方向依据的主题在目录里根本不存在，应被拒绝掉。",
             "topics": ["外星政策"], "questions": ["问题一", "问题二"], "methods": ["x → y"], "confidence": 0.9},
        ],
    }


class FakeClient:
    supports_json_response_format = True

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def chat(self, system, user, **kwargs):
        self.calls.append({"system": system, "user": user, "kwargs": kwargs})
        answer = self.answers.pop(0)
        content = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        return {"content": content, "usage": {"total_tokens": 100}}


@pytest.fixture
def holistic(monkeypatch):
    monkeypatch.setattr(Config, "LP_DIRECTIONS_ENABLED", True)
    monkeypatch.setattr(Config, "LP_DIRECTION_HOLISTIC", True)
    monkeypatch.setattr(Config, "LP_DIRECTION_HOLISTIC_MIN_SHARE", 0.3, raising=False)
    monkeypatch.setattr(lpr, "_direction_budget_ok", lambda store, pid: True)
    monkeypatch.setattr(lpr, "_record_direction_call", lambda *a, **k: None)
    monkeypatch.setattr(lpr, "_catalog_descriptions", lambda store, pid: {"高考招生通知": "各地高考招生录取政策"})


# ---------------------------------------------------------------- 开关与规则版本
def test_switch_off_keeps_old_path_and_version(monkeypatch):
    monkeypatch.setattr(Config, "LP_DIRECTION_HOLISTIC", False)
    monkeypatch.setattr(Config, "LP_DIRECTIONS_ENABLED", True)
    assert lpr.direction_rules_version() == lpr.DIRECTION_RULES_VERSION == "directions/2"
    called = []
    monkeypatch.setattr(lpr, "_holistic_model_directions", lambda *a, **k: called.append(1) or ([], "x"))
    monkeypatch.setattr(lpr, "_direction_budget_ok", lambda store, pid: False)
    dirs, reason = lpr._model_directions(None, "p", _groups(), Counter(), 0, 7754)
    assert called == [] and reason == "budget_exhausted"          # old path
    # 开关一直关闭：老报告（无版本字段）与 directions/2 报告都算"当前"，不会被重建
    assert lpr.direction_rules_current({"research_brief": {}})
    assert lpr.direction_rules_current({"research_brief": {"direction_rules_version": "directions/2"}})
    assert not lpr.direction_rules_current({"research_brief": {"direction_rules_version": "directions/3-holistic"}})


def test_switch_on_changes_version(holistic):
    assert lpr.direction_rules_version() == "directions/3-holistic"
    assert not lpr.direction_rules_current({"research_brief": {"direction_rules_version": "directions/2"}})
    assert not lpr.direction_rules_current({"research_brief": {}})
    assert lpr.direction_rules_current({"research_brief": {"direction_rules_version": "directions/3-holistic"}})


# ---------------------------------------------------------------- 完整目录输入
def test_payload_covers_all_topics_and_files():
    catalog, text = lpr._holistic_payload(_groups(), {"高考招生通知": "各地高考招生录取政策"}, 14000)
    assert catalog["已盘点文件"] == 7754
    assert sum(topic["文件数"] for topic in catalog["主题"]) == 7754
    assert [topic["主题"] for topic in catalog["主题"]][0] == "教育政策与考试招生"   # 按规模排序
    leaves = {leaf["叶组"] for topic in catalog["主题"] for leaf in topic["叶组"]}
    assert {"高考招生通知", "世界杯购票防骗", "未能识别内容"} <= leaves
    placeholder = next(topic for topic in catalog["主题"] if topic["主题"] == "未分类")
    assert "说明" in placeholder and "代表片段" not in placeholder
    assert "各地高考招生录取政策" in text


def test_payload_shrinks_but_keeps_every_topic():
    groups = {"主题%02d" % i: _group(100, {"叶组%02d-%02d" % (i, j): 10 for j in range(10)},
                                    "片段" * 80, "p/%d.txt" % i) for i in range(30)}
    catalog, text = lpr._holistic_payload(groups, {}, 4000)
    listed = sum(topic["文件数"] for topic in catalog["主题"])
    assert catalog["已盘点文件"] == 3000
    assert listed == 3000 or "其余小主题" in catalog          # 所有文件都有去处
    assert len(text) <= 4000


def test_prompt_asks_for_profile_threads_and_scale():
    _catalog, text = lpr._holistic_payload(_groups(), {}, 14000)
    system, user = lpr._holistic_prompt(text, Counter(inventory_files=7754))
    assert "package_profile" in user and "main_threads" in user
    assert "覆盖文件最多" in user and "个别文件" in user
    assert "文件数不是优先级" not in user and "不得按文件数排序" not in user


# ---------------------------------------------------------------- 覆盖面把关
def test_primary_is_holistic_and_fabricated_topic_rejected(holistic):
    client = FakeClient([_answer(["教育政策与考试招生", "卫生健康与医保", "交通出行与安全提醒"])])
    dirs, reason = lpr._holistic_model_directions(None, "p", _groups(), Counter(inventory_files=7754), 7754,
                                                  client=client)
    assert reason == "ok" and len(dirs) == 2                        # 编造主题的方向被拒
    primary = dirs[0]
    assert primary["source"] == "model_holistic"
    assert primary["coverage"]["files"] == 7000
    assert primary["coverage_note"] == "综合 3 个主题、7000 个文件（占全部 90%）"
    assert primary["package_profile"] and primary["main_threads"][0]["files"] == 5500
    assert primary["score"] != 10 ** 6 and primary["score"] > 1000
    assert all(e["scope"] and e["source_path"] for e in primary["evidence_chain"])
    assert client.calls[0]["kwargs"]["response_format"] == {"type": "json_object"}
    assert "7754" in client.calls[0]["user"]


def test_narrow_primary_is_demoted(holistic):
    narrow = _answer(["交通出行与安全提醒"], first_title="世界杯购票：领事提醒中的诈骗风险")
    client = FakeClient([narrow])
    dirs, reason = lpr._holistic_model_directions(None, "p", _groups(), Counter(), 7754, client=client)
    assert reason == "ok"
    assert dirs[0]["title"] == "公共服务：教育医疗交通三线政策的整体演进"
    assert dirs[1]["title"].startswith("世界杯购票")
    assert any("已改用覆盖" in note for note in dirs[0]["limitations"])


def test_placeholder_topic_does_not_count(holistic):
    answer = _answer(["未分类", "交通出行与安全提醒"])
    answer["directions"] = answer["directions"][:1]
    dirs, reason = lpr._holistic_model_directions(None, "p", _groups(), Counter(), 7754,
                                                  client=FakeClient([answer]))
    assert reason == "ok"
    assert dirs[0]["coverage"]["topics"] == ["交通出行与安全提醒"]
    assert any("代表性有限" in note for note in dirs[0]["limitations"])


def test_bad_json_retries_once_with_smaller_payload(holistic):
    client = FakeClient(["不是 JSON", _answer(["教育政策与考试招生", "卫生健康与医保", "交通出行与安全提醒"])])
    dirs, reason = lpr._holistic_model_directions(None, "p", _groups(), Counter(), 7754, client=client)
    assert reason == "ok" and len(client.calls) == 2
    assert "上一次输出不是合法 JSON" in client.calls[1]["system"]


def test_all_bad_falls_back(holistic):
    dirs, reason = lpr._holistic_model_directions(None, "p", _groups(), Counter(), 7754,
                                                  client=FakeClient(["x", "y"]))
    assert dirs == [] and reason == "bad_format"


# ---------------------------------------------------------------- 端到端（报告 + Word）
@pytest.fixture
def lp(tmp_path, monkeypatch):
    from services.large_package_isolated import LargePackageStore
    monkeypatch.setattr(Config, "SCAN_ALLOWED_ROOTS", ())
    monkeypatch.setattr(Config, "LP_REPORT_CACHE_FIX", True)
    store = LargePackageStore(tmp_path / "state" / "large.db")
    package = store.create(tmp_path)
    for name, category, text in (("a/1.txt", "教育政策与考试招生", "高考招生通知内容"),
                                 ("a/2.txt", "教育政策与考试招生", "中考改革通知内容"),
                                 ("b/3.txt", "卫生健康与医保", "医保目录调整方案"),
                                 ("c/4.txt", "交通出行与安全提醒", "世界杯购票防骗提醒")):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        stat = path.stat()
        store.upsert_file(package["id"], {"path": name, "name": path.name, "extension": ".txt",
                                          "size": stat.st_size, "modified_at_ns": stat.st_mtime_ns})
        store.update_file(package["id"], name, quick_status="completed", deep_status="completed",
                          category=category, deep_summary={"summary_type": "model", "summary": text * 5})
        with closing(store._connect()) as conn:
            # update_file 不写 category / summary_quality（由各自的 worker 写），这里直接落库
            conn.execute("UPDATE files SET summary_quality='complete',category=? WHERE package_id=? AND path=?",
                         (category, package["id"], name))
            conn.commit()
    store.update_package(package["id"], status="completed", phase="report_ready")
    return store, package["id"]


def test_build_report_and_docx_with_holistic(lp, holistic, monkeypatch, tmp_path):
    store, pid = lp
    client = FakeClient([_answer(["教育政策与考试招生", "卫生健康与医保", "交通出行与安全提醒"])])
    monkeypatch.setattr(lpr, "_direction_client", lambda: client)
    report = lpr.build_report(store, store.get(pid))
    brief = report["research_brief"]
    assert brief["direction_rules_version"] == "directions/3-holistic"
    primary = brief["recommended_research_direction"]
    assert primary["source"] == "model_holistic"
    assert brief["direction_source"] == "model_holistic"      # 标记取主方向的实际来源
    assert primary["coverage"]["files"] == 4 and primary["coverage_note"].endswith("（占全部 100%）")
    assert len(client.calls) == 1
    path = tmp_path / "out.docx"
    lpr.write_docx(report, path)
    from docx import Document
    body = "\n".join(p.text for p in Document(str(path)).paragraphs)
    assert "覆盖范围：综合 3 个主题" in body and "全包画像：" in body


def test_build_report_switch_off_unchanged(lp, monkeypatch):
    store, pid = lp
    monkeypatch.setattr(Config, "LP_DIRECTIONS_ENABLED", False)
    monkeypatch.setattr(Config, "LP_DIRECTION_HOLISTIC", False)
    report = lpr.build_report(store, store.get(pid))
    brief = report["research_brief"]
    assert brief["direction_rules_version"] == "directions/2"
    primary = brief["recommended_research_direction"]
    assert primary and "coverage_note" not in primary and primary.get("source") != "model_holistic"
    assert brief["direction_source"] != "model_holistic"
