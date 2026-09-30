"""2026-09-30 大数据包 7 项改修的回归测试（第 1–4 项）。

  1. 超上限的同名兼容组：SJFX_LP_DEDUP_OVERCAP_RENAME 开启时改名消歧，目录校验通过；
     关闭时保持旧行为（只标 needs_review，重名保留）。
  2. 发布被同主题重名（INV-3）拦下：SJFX_LP_PUBLISH_DEDUP_RETRY 开启时事务外去重并
     重试一次、自动恢复；拒绝原因不是重名或开关关闭时保持阻断。
  3. 概览 API：最近一条 catalog/rejected 比当前正式版新时返回 reason 与时间。
  4. direction_source：全包综合开关开启且主方向来自全包综合时记 "model_holistic"。
"""
from contextlib import closing
import json
import time
from unittest.mock import patch

import pytest

from config import Config
from services import large_package_catalog_naming as naming
from services import large_package_reporting as reports
from services.large_package_isolated import LargePackageStore

NAME = "设备运行监测技术"


# ---------------------------------------------------------------- 夹具
def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "SCAN_ALLOWED_ROOTS", ())
    monkeypatch.setattr(Config, "LP_DIRECTIONS_ENABLED", False)
    monkeypatch.setattr(Config, "LP_OVERVIEW_EAGER", True)
    store = LargePackageStore(tmp_path / "state.db")
    pid = store.create(tmp_path)["id"]
    store.update_package(pid, status="waiting_for_selection", phase="catalog_ready")
    return store, pid


def _add_group(store, pid, gid, n, name=NAME, vector=b"\x01\x02\x03\x04"):
    """一个带 n 个成员（有向量）、当前版本标签的叶组；member_count 与实际一致。"""
    now = time.time()
    with closing(store._connect()) as conn:
        conn.execute(
            "INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,"
            "result,status,updated_at,content_revision,member_count) "
            "VALUES(?,?,'test',?,?,'{}','named',?,1,?)",
            (pid, gid, vector, gid + "-0.txt", now, n))
        conn.commit()
    for i in range(n):
        path = "%s-%d.txt" % (gid, i)
        store.upsert_file(pid, dict(path=path, name=path, size=100, modified_at_ns=1))
        with closing(store._connect()) as conn:
            conn.execute("INSERT INTO large_catalog_members VALUES(?,?,?,?,?)",
                         (pid, path, gid, "hash-" + path, vector))
            conn.commit()
    with closing(store._connect()) as conn:
        # 插成员会推进 content_revision（触发器），标签按**当前**修订号写；
        # member_count 也按实际成员数回写，避免 INV-6 干扰。
        rev = conn.execute("SELECT content_revision FROM large_catalog_groups "
                           "WHERE package_id=? AND id=?", (pid, gid)).fetchone()[0]
        conn.execute("UPDATE large_catalog_groups SET member_count=(SELECT COUNT(*) FROM "
                     "large_catalog_members WHERE package_id=? AND group_id=?) "
                     "WHERE package_id=? AND id=?", (pid, gid, pid, gid))
        conn.execute(
            "INSERT INTO lp_catalog_labels(package_id,group_id,revision,version,name,description,"
            "evidence,model,digest,needs_review,locked,updated) "
            "VALUES(?,?,?,1,?,?,'[]','test','d',0,0,?)",
            (pid, gid, int(rev), name, name + "的内容范围与结果。", now))
        conn.commit()


def _set_topic(store, pid, name, children):
    with closing(store._connect()) as conn:
        conn.execute("DELETE FROM lp_catalog_branches WHERE package_id=?", (pid,))
        conn.execute(
            "INSERT INTO lp_catalog_branches(package_id,id,name,children,revisions,round_id,updated) "
            "VALUES(?,?,?,?,?,?,?)",
            (pid, "topic:" + name, name, json.dumps(children), "{}", "", time.time()))
        conn.commit()


def _labels(store, pid):
    with closing(store._connect()) as conn:
        return {r[0]: (r[1], int(r[2])) for r in conn.execute(
            "SELECT group_id,name,needs_review FROM lp_catalog_labels WHERE package_id=?", (pid,))}


def _inv3_blocking(store, pid):
    return any(v.startswith("INV-3") for v in naming.validate_catalog(store, pid)["violations"])


# ---------------------------------------------------------------- 第 1 项
@pytest.fixture
def overcap(tmp_path, monkeypatch):
    store, pid = _store(tmp_path, monkeypatch)
    _add_group(store, pid, "big", 3)
    _add_group(store, pid, "small", 2)
    _set_topic(store, pid, "主题甲", ["big", "small"])
    assert _inv3_blocking(store, pid)
    return store, pid


def _normalize(store, pid):
    # 内容相容（可以合并），但 3+2 > cap=3：没有宿主装得下 → 超上限分支。
    with patch("services.large_package_catalog_quality.compatible_groups", return_value=True):
        return naming.normalize_leaves(store, pid, cap=3)


def test_overcap_compatible_duplicates_renamed_when_enabled(overcap, monkeypatch):
    store, pid = overcap
    monkeypatch.setattr(Config, "LP_DEDUP_OVERCAP_RENAME", True)
    stats = _normalize(store, pid)
    assert stats["over_cap"] >= 0 and stats.get("renamed_over_cap") == 1, stats
    labels = _labels(store, pid)
    assert labels["big"][0] == NAME                       # 大组保留原名
    assert labels["small"] == (NAME + "·其二", 1)          # 小组改名并标 needs_review
    with closing(store._connect()) as conn:
        members = dict(conn.execute("SELECT group_id,COUNT(*) FROM large_catalog_members "
                                    "WHERE package_id=? GROUP BY group_id", (pid,)).fetchall())
        reasons = [r[0] for r in conn.execute(
            "SELECT reason FROM lp_workflow_events WHERE package_id=? AND object='catalog.leaf' "
            "AND new_state=?", (pid, NAME + "·其二"))]
    assert members == {"big": 3, "small": 2}              # 不合并、不搬成员
    assert reasons and "超过容量上限" in reasons[0]
    assert not naming.duplicate_name_groups(store, pid)
    verdict = naming.validate_catalog(store, pid)
    assert verdict["ok"] and not verdict["violations"], verdict


def test_overcap_default_keeps_old_behaviour(overcap, monkeypatch):
    store, pid = overcap
    monkeypatch.setattr(Config, "LP_DEDUP_OVERCAP_RENAME", False)
    stats = _normalize(store, pid)
    assert "renamed_over_cap" not in stats
    labels = _labels(store, pid)
    assert labels["big"] == (NAME, 1) and labels["small"] == (NAME, 0)
    assert _inv3_blocking(store, pid)


def test_overcap_rename_handles_same_key_in_two_topics(tmp_path, monkeypatch):
    """同一个名字在两个主题下各有一簇超上限：两簇都要改名，且改出来的名字互不相同。"""
    store, pid = _store(tmp_path, monkeypatch)
    for gid, n in (("a1", 3), ("a2", 2), ("b1", 3), ("b2", 2)):
        _add_group(store, pid, gid, n)
    with closing(store._connect()) as conn:
        for topic, kids in (("主题甲", ["a1", "a2"]), ("主题乙", ["b1", "b2"])):
            conn.execute(
                "INSERT INTO lp_catalog_branches(package_id,id,name,children,revisions,round_id,updated) "
                "VALUES(?,?,?,?,?,?,?)",
                (pid, "topic:" + topic, topic, json.dumps(kids), "{}", "", time.time()))
        conn.commit()
    monkeypatch.setattr(Config, "LP_DEDUP_OVERCAP_RENAME", True)
    stats = _normalize(store, pid)
    labels = _labels(store, pid)
    assert stats.get("renamed_over_cap") == 2, stats
    names = [labels[g][0] for g in ("a1", "a2", "b1", "b2")]
    assert not _inv3_blocking(store, pid)
    assert len({naming._normalize_name(n) for n in names[1:]}) == 3, names


# ---------------------------------------------------------------- 第 2 项
@pytest.fixture
def published(tmp_path, monkeypatch):
    """先正常发布一版；再加一个同主题同名的叶组，让下一次发布只因 INV-3 被拒。"""
    store, pid = _store(tmp_path, monkeypatch)
    _add_group(store, pid, "g1", 3)
    _set_topic(store, pid, "主题甲", ["g1"])
    first = reports.build_overview(store, store.get(pid), milestone="catalog")
    assert first.get("published"), naming.validate_catalog(store, pid)
    _add_group(store, pid, "g2", 2)
    _set_topic(store, pid, "主题甲", ["g1", "g2"])
    violations = naming.validate_catalog(store, pid)["violations"]
    assert violations and all(v.startswith("INV-3") for v in violations), violations
    return store, pid, first


def _rejected_events(store, pid):
    with closing(store._connect()) as conn:
        return conn.execute("SELECT reason FROM lp_workflow_events WHERE package_id=? "
                            "AND object='catalog' AND new_state='rejected'", (pid,)).fetchall()


def test_publish_blocked_by_same_topic_duplicates_recovers(published, monkeypatch):
    store, pid, first = published
    monkeypatch.setattr(Config, "LP_PUBLISH_DEDUP_RETRY", True)
    # 内容不相容 → normalize_leaves 走改名消歧（不调用模型、不改修订号）
    with patch("services.large_package_catalog_quality.compatible_groups", return_value=False), \
            patch.object(naming, "normalize_leaves", wraps=naming.normalize_leaves) as spy:
        report = reports.build_overview(store, store.get(pid), milestone="catalog")
    assert spy.call_count == 1                            # 只重试一次
    assert report.get("published") is True
    current = reports.published_report(store, store.get(pid))
    assert current["report_id"] != first["report_id"]     # 新版已发布
    assert current["provenance"]["catalog_id"]
    assert _labels(store, pid)["g2"][0] == NAME + "·其二"
    assert _rejected_events(store, pid)                   # 第一次的拒绝仍留审计记录
    # 发布成功后横幅不再出现（拒绝早于当前正式版）
    assert store.workflow_snapshot(pid)["catalog_rejection"] is None


def test_publish_duplicate_block_unchanged_when_switch_off(published, monkeypatch):
    store, pid, first = published
    monkeypatch.setattr(Config, "LP_PUBLISH_DEDUP_RETRY", False)
    with patch.object(naming, "normalize_leaves") as spy:
        report = reports.build_overview(store, store.get(pid), milestone="catalog")
    assert spy.call_count == 0
    assert not report.get("published")
    assert reports.published_report(store, store.get(pid))["report_id"] == first["report_id"]
    assert _rejected_events(store, pid)


def test_publish_block_for_other_reason_is_not_retried(published, monkeypatch):
    store, pid, first = published
    monkeypatch.setattr(Config, "LP_PUBLISH_DEDUP_RETRY", True)
    with closing(store._connect()) as conn:
        # 另加一个 member_count 失真的组 → INV-6 与 INV-3 同时存在：去重修不好，保持阻断。
        conn.execute("UPDATE large_catalog_groups SET member_count=99 WHERE package_id=? AND id='g1'",
                     (pid,))
        conn.commit()
    violations = naming.validate_catalog(store, pid)["violations"]
    assert any(v.startswith("INV-6") for v in violations), violations
    with patch.object(naming, "normalize_leaves") as spy:
        report = reports.build_overview(store, store.get(pid), milestone="catalog")
    assert spy.call_count == 0
    assert not report.get("published")
    assert reports.published_report(store, store.get(pid))["report_id"] == first["report_id"]


def test_dedup_retry_reason_filter():
    assert reports._dedup_retry_wanted({"violations": ["INV-3：同一主题下有 1 组归一化同名的叶组（涉及 2 个组）"]})
    assert not reports._dedup_retry_wanted({"violations": ["INV-3：x", "INV-7：有 1 个语义组既没有名称"]})
    assert not reports._dedup_retry_wanted({"violations": []})
    assert not reports._dedup_retry_wanted({})


# ---------------------------------------------------------------- 第 3 项
def test_overview_api_returns_latest_rejection(published, monkeypatch):
    import app as web
    store, pid, _first = published
    monkeypatch.setattr(Config, "LP_PUBLISH_DEDUP_RETRY", False)
    assert store.workflow_snapshot(pid)["catalog_rejection"] is None     # 还没被拒
    reports.build_overview(store, store.get(pid), milestone="catalog")   # 被 INV-3 拒绝
    snap = store.workflow_snapshot(pid)
    rejection = snap["catalog_rejection"]
    assert rejection and "目录校验" in rejection["reason"] and "INV-3" in rejection["reason"]
    assert rejection["created"] > 0
    with patch.object(web, "large_package_store", store), \
            patch.object(web, "_request_owner_id", return_value="legacy"):
        with web.app.test_request_context("/api/large-packages/x/report"):
            body = web.large_package_report(pid).get_json()
    assert body["published"] is True
    assert body["catalog_rejection"] == rejection
    assert body["snapshot"]["catalog_rejection"] == rejection


def test_rejection_without_previous_version_has_no_banner(tmp_path, monkeypatch):
    """没有上一版可显示时不给横幅（"当前显示上一版"不成立）。"""
    store, pid = _store(tmp_path, monkeypatch)
    _add_group(store, pid, "g1", 3)
    _add_group(store, pid, "g2", 2)
    _set_topic(store, pid, "主题甲", ["g1", "g2"])
    report = reports.build_overview(store, store.get(pid), milestone="catalog")
    assert not report.get("published") and _rejected_events(store, pid)
    assert store.workflow_snapshot(pid)["catalog_rejection"] is None


def test_frontend_banner_text_present():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "static"
    for name in ("intelligence-overview.js", "large-package-result.js"):
        source = (root / name).read_text(encoding="utf-8")
        assert "目录校验未通过，当前显示上一版" in source, name
        assert "catalog_rejection" in source, name


# ---------------------------------------------------------------- 第 4 项
def test_direction_source_follows_primary_origin(monkeypatch):
    holistic = {"title": "全包方向", "source": "model_holistic"}
    evidence = {"title": "证据方向", "source": "model_evidence"}
    template = [{"title": "模板", "category": "教育", "score": 1}]
    args = ("ok", template, True, {}, [])
    monkeypatch.setattr(Config, "LP_DIRECTION_HOLISTIC", True)
    assert reports._direction_fields([holistic], *args)["direction_source"] == "model_holistic"
    assert reports._direction_fields([evidence], *args)["direction_source"] == "model_evidence"
    monkeypatch.setattr(Config, "LP_DIRECTION_HOLISTIC", False)
    assert reports._direction_fields([holistic], *args)["direction_source"] == "model_evidence"
    assert reports._direction_fields([], *args)["direction_source"] == "template"
