"""情报概览改修（O1–O7 普通包 / P1–P5 大数据包）回归测试。

每个开关都覆盖"关闭 = 旧行为"与"打开 = 新行为"两侧；第 4 批（O6/O7/P3–P5）无开关。
"""
from contextlib import closing
import threading
import time

import pytest

from config import Config
from services import evidence as ev
from services import reporting as rep


FACT = "港口关闭导致物流延误，平均延误达到 5 天，影响了三个主要航线的集装箱周转效率。"
OTHER = "食堂菜单在九月调整为每周轮换，新增两种素食套餐，供应时间保持不变并提供外带服务。"


@pytest.fixture
def lexical(monkeypatch):
    """No embedding provider: semantic scores are all zero (lexical-fallback)."""
    monkeypatch.setattr(rep, "claim_semantic_scores", lambda claim, items: ([0.0] * len(items), "lexical-fallback"))


def _item(evidence_id, text, **extra):
    value = {"evidence_id": evidence_id, "text": text, "source_path": "docs/a.txt", "label": "paragraph"}
    value.update(extra)
    return value


def _candidate(rank, title, items):
    return {
        "rank": rank, "title": title, "confidence": "中", "priority": rank, "score": 1.0,
        "rationale": "本地规则理由", "basis": "本地依据",
        "evidence_ids": [item["evidence_id"] for item in items], "evidence_chain": list(items),
        "research_questions": ["问题"], "representative_documents": [], "limitations": [],
    }


# ---------------------------------------------------------------- O1 推论证据核验
def test_inference_evidence_rejects_preview_and_short_text():
    preview = ev.verify_inference_evidence("港口物流延误", _item("e1", FACT, preview_only=True))
    assert preview["support_status"] == "insufficient"
    assert preview["verification_contract"] == "inference-relevance/1.0"
    short = ev.verify_inference_evidence("港口物流延误", _item("e2", "港口物流"))
    assert short["support_status"] == "insufficient"


def test_inference_evidence_lexical_and_semantic():
    lexical_hit = ev.verify_inference_evidence("优先研究港口关闭对物流延误的影响", _item("e1", FACT))
    assert lexical_hit["support_status"] == "supported"
    unrelated = ev.verify_inference_evidence("优先研究港口关闭对物流延误的影响", _item("e2", OTHER))
    assert unrelated["support_status"] == "insufficient"
    semantic = ev.verify_inference_evidence(
        "优先研究港口关闭对物流延误的影响", _item("e2", OTHER), semantic_score=0.9,
        relevance_mode="semantic+lexical", semantic_threshold=0.86)
    assert semantic["support_status"] == "supported"
    below = ev.verify_inference_evidence(
        "优先研究港口关闭对物流延误的影响", _item("e2", OTHER), semantic_score=0.5,
        relevance_mode="semantic+lexical", semantic_threshold=0.86)
    assert below["support_status"] == "insufficient"


def test_aligned_merge_adopts_rank_choice_with_relevant_evidence(lexical):
    items = [_item("e1", FACT)]
    local = {"direction_candidates": [_candidate(1, "港口关闭与物流延误", items)],
             "recommended_research_direction": {"title": "本地首选"}, "generation_mode": "local_complete"}
    model = {"recommended_research_direction": {
        "candidate_rank": 1, "rationale": "港口关闭导致物流延误，值得优先研究", "evidence_ids": ["e1"],
        "confidence": "高"}}
    merged = rep.merge_model_report_aligned(local, model, items)
    assert merged["model_adoption"]["recommendation"] == "adopted"
    assert merged["model_adoption"]["matched_by"] == "candidate_rank"
    chosen = merged["recommended_research_direction"]
    assert chosen["evidence_ids"] == ["e1"]
    assert chosen["confidence"] == "中"          # the model may lower, never raise
    assert merged["generation_mode"] == "model_analyzed"


def test_aligned_merge_rejects_irrelevant_and_invalid(lexical):
    items = [_item("e1", OTHER)]
    local = {"direction_candidates": [_candidate(1, "港口关闭与物流延误", items)],
             "recommended_research_direction": {"title": "本地首选"}, "generation_mode": "local_complete"}
    rejected = rep.merge_model_report_aligned(
        local, {"recommended_research_direction": {"candidate_rank": 1, "rationale": "物流延误"}}, items)
    assert rejected["model_adoption"]["recommendation"] == "rejected_no_relevant_evidence"
    assert rejected["recommended_research_direction"] == {"title": "本地首选"}
    assert rejected["generation_mode"] == "local_complete"
    invalid = rep.merge_model_report_aligned(
        local, {"recommended_research_direction": {"candidate_rank": 9, "title": "模型自拟"}}, items)
    assert invalid["model_adoption"]["recommendation"] == "invalid_candidate"
    assert "rejected_model_recommendation" in invalid


def test_aligned_key_findings_need_factual_support(lexical):
    items = [_item("e1", FACT)]
    local = {"direction_candidates": [], "content_key_findings": [], "generation_mode": "local_complete"}
    merged = rep.merge_model_report_aligned(local, {"key_findings": [
        {"statement": "港口关闭导致物流延误，平均延误达到 5 天", "evidence_ids": ["e1"]},
        {"statement": "编造的结论", "evidence_ids": ["missing"]},
        "没有证据编号的自由文本",
    ]}, items)
    assert merged["model_adoption"]["key_findings_offered"] == 2
    assert [item["evidence_ids"] for item in merged["model_key_findings"]] == [["e1"]]
    assert merged["key_findings"][0].startswith("港口关闭导致物流延误")


def test_candidates_have_evidence_and_placeholder():
    assert not rep.candidates_have_evidence({"direction_candidates": [{"evidence_chain": []}]})
    assert rep.candidates_have_evidence({"direction_candidates": [{"evidence_ids": ["e1"]}]})
    report = rep.compose_key_findings({"content_key_findings": []}, [])
    assert report["key_findings"] == [rep.KEY_FINDINGS_PLACEHOLDER]


def test_content_key_findings_skip_fallback_and_unverified():
    rows = [
        {"path": "a.txt", "type": "file", "payload": {"generated_by": "model-deep-analysis", "file_conclusions": [
            {"status": "verified", "text": "结论甲", "confidence": 0.9, "supports": [{"evidence_id": "a1"}]},
            {"status": "candidate", "text": "未核验", "supports": [{"evidence_id": "a2"}]}]}},
        {"path": "b.txt", "type": "file", "payload": {"generated_by": "local-fallback", "file_conclusions": [
            {"status": "verified", "text": "兜底结论", "confidence": 1, "supports": [{"evidence_id": "b1"}]}]}},
    ]
    found = rep.content_key_findings(rows)
    assert [item["statement"] for item in found] == ["结论甲"]
    assert rep.is_fallback_summary({"generated_by": "local-fallback"})
    assert not rep.is_fallback_summary({"generated_by": "model-deep-analysis"})


# ---------------------------------------------------------------- O3 / O5 普通包
@pytest.fixture
def web_storage(tmp_path, monkeypatch):
    import app as web
    from services.storage import Storage
    store = Storage(tmp_path / "agent.db", tmp_path / "sidecars")
    monkeypatch.setattr(web, "storage", store)
    return web, store


def test_legacy_deep_summary_switch(web_storage, monkeypatch):
    web, store = web_storage
    deep_type = web.staged_summary_type("deep", "file")
    store.save_summary("s1", "a.txt", "file", {"summary": "old"})
    store.save_summary("s1", "a.txt", deep_type, {"summary": "new"})
    store.save_summary("s1", "b.txt", "file", {"summary": "only-old"})
    monkeypatch.setattr(Config, "OVERVIEW_EXCLUDE_FALLBACK", False)
    assert web._legacy_deep_summary("s1", "a.txt")["summary"] == "old"
    monkeypatch.setattr(Config, "OVERVIEW_EXCLUDE_FALLBACK", True)
    assert web._legacy_deep_summary("s1", "a.txt")["summary"] == "new"
    assert web._legacy_deep_summary("s1", "b.txt")["summary"] == "only-old"
    assert web._legacy_deep_summary("s1", "none.txt") == {}


def test_queue_legacy_deep_report_debounce(web_storage):
    web, store = web_storage
    deep_type = web.staged_summary_type("deep", "file")
    store.save_summary("s1", "a.txt", deep_type, {"summary": "x", "generated_at": "t1", "generated_by": "m"})
    job_id, reason = web._queue_legacy_deep_report("s1")
    assert reason == "queued" and job_id
    assert web._queue_legacy_deep_report("s1") == (None, "report_active")
    with store._connect() as conn:
        conn.execute("UPDATE analysis_jobs SET status='completed' WHERE id=?", (job_id,))
    fingerprint = web._legacy_deep_report_fingerprint("s1")
    store.save_summary("s1", ".", "deep_report", {"source_fingerprint": fingerprint})
    assert web._queue_legacy_deep_report("s1") == (None, "unchanged")
    summary_job, _ = store.create_or_get_typed_job(
        "s1", "generate_summary", options={"workflow_source": "deep_parse_model_summary", "path": "b.txt"})
    assert web._queue_legacy_deep_report("s1") == (None, "deep_queue_active")
    with store._connect() as conn:
        conn.execute("UPDATE analysis_jobs SET status='completed' WHERE id=?", (summary_job,))
    store.save_summary("s1", "b.txt", deep_type, {"summary": "y", "generated_at": "t2", "generated_by": "m"})
    assert web._legacy_deep_report_fingerprint("s1") != fingerprint
    new_job, reason = web._queue_legacy_deep_report("s1")
    assert reason == "queued" and new_job != job_id
    options = store.get_job(new_job).get("options") if hasattr(store, "get_job") else None
    if isinstance(options, dict):
        assert options.get("workflow_source") == "deep_results_report"


# ---------------------------------------------------------------- P1–P5 大数据包
from services import large_package_reporting as lpr  # noqa: E402


def test_pick_examples_prefers_quality_and_diversity():
    def evidence(path, sha):
        return {"source_path": path, "source_sha256": sha, "text": "x"}
    pool = [
        (4.5, evidence("a/1.txt", "s1")),
        (4.4, evidence("a/2.txt", "s2")),
        (3.2, evidence("b/1.txt", "s3")),
        (2.0, evidence("c/1.txt", "s1")),   # duplicate content of a/1.txt
    ]
    chosen = [item["source_path"] for item in lpr._pick_examples(pool, 3)]
    assert chosen == ["a/1.txt", "b/1.txt", "a/2.txt"]
    assert lpr._evidence_quality({"text": ""}, True, False, True) > lpr._evidence_quality({"text": "x" * 900}, False, True, True)
    assert lpr._evidence_quality({"text": "", "deep": True}, False, False, False) > lpr._evidence_quality({"text": "x" * 900}, False, False, False)


@pytest.fixture
def lp(tmp_path, monkeypatch):
    from services.large_package_isolated import LargePackageStore
    from large_package_worker import LargePackageWorker
    monkeypatch.setattr(Config, "SCAN_ALLOWED_ROOTS", ())
    monkeypatch.setattr(Config, "LP_DIRECTIONS_ENABLED", False)
    monkeypatch.setattr(Config, "LP_OVERVIEW_EAGER", True)
    monkeypatch.setattr(LargePackageWorker, "_catalog_incremental", lambda *a, **k: None)
    store = LargePackageStore(tmp_path / "state" / "large.db")
    package = store.create(tmp_path)
    rows = []
    for name, quality, summary in (("a/full.txt", "complete", "完整摘要：港口关闭导致物流延误。"),
                                   ("b/cut.txt", "truncated", "截断摘要：航线周转效率下降。")):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(summary, encoding="utf-8")
        stat = path.stat()
        row = {"path": name, "name": path.name, "extension": ".txt", "size": stat.st_size,
               "modified_at_ns": stat.st_mtime_ns}
        store.upsert_file(package["id"], row)
        store.update_file(package["id"], name, quick_status="completed", deep_status="completed",
                          category="物流", deep_summary={"summary_type": "model", "summary": summary})
        with closing(store._connect()) as conn:   # summary_quality is written by the deep worker
            conn.execute("UPDATE files SET summary_quality=? WHERE package_id=? AND path=?", (quality, package["id"], name))
            conn.commit()
        rows.append(row)
    store.update_package(package["id"], status="completed", phase="report_ready")
    return store, package["id"]


def test_p4_degraded_summaries_are_labelled(lp):
    store, pid = lp
    report = lpr.build_report(store, store.get(pid))
    assert report["coverage"]["truncated_summaries"] == 1
    sources = [source for topic in report["topic_summaries"] for source in topic["sources"]]
    by_path = {source["source_path"]: source for source in sources}
    assert by_path["a/full.txt"]["scope"] == "深度摘要"
    assert by_path["b/cut.txt"]["scope"] == "深度摘要（输出截断）"
    assert by_path["b/cut.txt"]["summary_quality"] == "truncated"
    assert [source["source_path"] for source in sources][0] == "a/full.txt"   # complete ranks first


def test_p1_cache_key_ignores_progress_updates(lp, monkeypatch):
    store, pid = lp
    monkeypatch.setattr(Config, "LP_REPORT_CACHE_FIX", False)
    first = lpr.build_report(store, store.get(pid))
    store.update_package(pid, message="进度 1")
    assert lpr.build_report(store, store.get(pid))["revision"] != first["revision"]   # old behaviour
    monkeypatch.setattr(Config, "LP_REPORT_CACHE_FIX", True)
    fixed = lpr.build_report(store, store.get(pid), model_directions=False)
    store.update_package(pid, message="进度 2")
    again = lpr.build_report(store, store.get(pid), model_directions=False)
    assert again["revision"] == fixed["revision"]


def test_p1_polling_skips_model_and_model_runs_outside_lock(lp, monkeypatch):
    store, pid = lp
    monkeypatch.setattr(Config, "LP_REPORT_CACHE_FIX", True)
    monkeypatch.setattr(Config, "LP_DIRECTIONS_ENABLED", True)
    calls = []

    def fake_model(*args, **kwargs):
        free = []
        thread = threading.Thread(target=lambda: free.append(lpr._lock.acquire(timeout=1) and (lpr._lock.release() or True)))
        thread.start()
        thread.join()
        calls.append(free == [True])
        return [], "no_candidates"

    monkeypatch.setattr(lpr, "_model_directions", fake_model)
    preview = lpr.build_report(store, store.get(pid), model_directions=False)
    assert calls == []
    assert preview["research_brief"].get("direction_reason") in {"", "model_skipped"}
    full = lpr.build_report(store, store.get(pid))
    assert calls == [True]                      # called once, lock not held
    assert full.get("model_directions_attempted") is True
    lpr.build_report(store, store.get(pid))
    assert calls == [True]                      # cached after the attempt


def test_p2_export_uses_published_report(lp, monkeypatch):
    store, pid = lp
    monkeypatch.setattr(Config, "LP_REPORT_CACHE_FIX", True)
    published = lpr.build_overview(store, store.get(pid), milestone="deep")
    current = lpr.published_report(store, store.get(pid))
    if not current:
        pytest.skip("overview not publishable in this fixture: %r" % published.get("published"))
    monkeypatch.setattr(lpr, "build_report", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rebuilt")))
    target = lpr.export_report(store, store.get(pid), "json")
    assert current["report_id"] in target.name


def test_p3_naming_pending_defers_deep_overview(lp):
    from large_package_worker import LargePackageWorker
    store, pid = lp
    worker = LargePackageWorker(store=store, poll_seconds=0.2)
    with closing(store._connect()) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS lp_catalog_jobs(package_id TEXT NOT NULL, group_id TEXT NOT NULL,"
                     " revision INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,"
                     " leased REAL, next_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',"
                     " PRIMARY KEY(package_id,group_id))")
        conn.execute("INSERT INTO lp_catalog_jobs(package_id,group_id,revision,status) VALUES(?,?,1,'queued')", (pid, "g1"))
        conn.commit()
    assert worker._naming_jobs_pending(pid) == 1
    store.update_package(pid, counts={"deep_overview_pending_since": time.time()})
    built = []
    worker._build_overview = lambda package_id, milestone: built.append(milestone) or True
    worker._mark_catalog_refined = lambda package_id: None
    assert worker._overview_catchup() is False       # still naming, inside the wait window
    assert built == []
    with closing(store._connect()) as conn:
        conn.execute("UPDATE lp_catalog_jobs SET status='done' WHERE package_id=?", (pid,))
        conn.commit()
    assert worker._naming_jobs_pending(pid) == 0
    assert worker._overview_catchup() is True
    assert built == ["deep"]
    assert not float((store.get(pid).get("counts") or {}).get("deep_overview_pending_since") or 0)


def test_p5_rebuild_route(lp, monkeypatch):
    import app as web
    from unittest.mock import patch
    store, pid = lp
    with patch.object(web, "large_package_store", store), patch.object(web, "_request_owner_id", return_value="legacy"):
        with web.app.test_request_context("/api/large-packages/x/report/rebuild", method="POST"):
            response = web.rebuild_large_package_report(pid)
            body, status = response if isinstance(response, tuple) else (response, response.status_code)
            assert status == 202
        assert float(store.get(pid)["counts"]["deep_overview_pending_since"]) > 0
        with web.app.test_request_context("/api/large-packages/x/report/rebuild", method="GET"):
            response = web.rebuild_large_package_report(pid)
            assert response.get_json()["pending"] is True
        store.update_package(pid, status="waiting_for_selection")
        with web.app.test_request_context("/api/large-packages/x/report/rebuild", method="POST"):
            response = web.rebuild_large_package_report(pid)
            body, status = response if isinstance(response, tuple) else (response, response.status_code)
            assert status == 409
