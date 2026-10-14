import copy
import json
import sqlite3
import time

import pytest

from config import Config
from services.storage import Storage
from services.preliminary_summary_input import build_preliminary_input
from services.preliminary_summary_fast import enabled, generate_ready_results


@pytest.fixture
def setup(tmp_path, monkeypatch):
    # Ordinary generation/cache tests assume a fake model that fits four files.
    monkeypatch.setattr(Config, "LLM_CONTEXT_TOKENS", 32768)
    monkeypatch.setattr(Config, "PRELIMINARY_SUMMARY_OPTIMIZATION_ENABLED", True)
    store = Storage(tmp_path / "test.db")
    store.save_scan({"root": str(tmp_path), "tree": {}}, "scan", owner_id="owner")
    jobs = []
    for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
        store.save_document("scan", name, {"text": name + " 本季度收入120万元，同比提高10%，尚未审计。", "source": {"name": name},
            "structure": {"title": name}, "evidence": [{"evidence_id": name, "source_path": name, "text": "本季度收入120万元，同比提高10%，尚未审计。"}]})
        ident, _ = store.create_or_get_typed_job("scan", "generate_summary", options={"path": name, "kind": "file", "analysis_level": "preview", "workflow_source": "preliminary_model_summary"}, owner_id="owner")
        jobs.append(ident)
    return store, jobs


class Model:
    model = Config.LLM_MODEL

    def __init__(self, omit=None, broken=False):
        self.calls = []
        self.omit = omit
        self.broken = broken

    def chat_json(self, system, prompt, **kwargs):
        rows = json.loads(prompt.split("分析材料：\n", 1)[1])
        self.calls.append((rows, kwargs))
        if self.broken:
            return {"json": {"documents": []}}
        return {"json": {"documents": [
            {"file_id": r["file_id"], "path": r["path"], "title": r["path"], "core_summary": "季度收入120万元，同比提高10%，尚未审计。", "key_facts": ["收入120万元"], "conclusions": ["尚未审计"], "topics": ["收入"]}
            for r in reversed(rows) if not (r["path"] == self.omit and len(rows) > 1)
        ]}, "model": self.model, "usage": {"completion_tokens": 200}, "finish_reason": "stop"}


def fallback(document, path, reason):
    return {"core_summary": document["text"], "warnings": [reason]}


def prepared(store):
    lead = store.claim_next_job("worker")
    result = generate_ready_results(Model(), store, lead, fallback)
    return lead, result["_preliminary_ready_results"]


def test_input_bound_middle_and_critical_values():
    evidence = [{"text": f"第{i}章，收入{i}万元，未通过验收。" * 25, "page": i} for i in range(1, 21)]
    doc = {"text": "\n".join(x["text"] for x in evidence), "evidence": evidence, "structure": {"title": "年度报告"}}
    original = copy.deepcopy(doc)
    text = build_preliminary_input(doc)
    assert 4000 < len(text) <= 6500
    assert "第8章" in text or "第12章" in text
    assert "未通过验收" in text
    assert doc == original


def test_short_input_keeps_entire_text_once():
    text = "收入12万元；收入不是120万元，尚未确认。"
    output = build_preliminary_input({"text": text, "evidence": [{"text": text}]})
    assert output.count(text) == 1


def test_batch_prepares_without_publishing_and_commits_each_claimed_job(setup):
    store, ids = setup
    lead, ready = prepared(store)
    assert len(ready) == 4
    assert store.get_summary("scan", "a.txt", "preliminary_file_summary") is None
    assert store.commit_preliminary_result(lead, ready[lead["id"]]) == "completed"
    assert store.get_job(lead["id"])["status"] == "completed"
    assert store.get_summary("scan", "a.txt", "preliminary_file_summary")["preview_only"]
    # A sibling cannot publish before it is claimed.
    sibling = store.get_job(ids[1])
    assert store.commit_preliminary_result(sibling, ready[ids[1]]) == "not_owned"
    sibling = store.claim_next_job("worker")
    assert store.commit_preliminary_result(sibling, ready[sibling["id"]]) == "completed"
    assert store.commit_preliminary_result(sibling, ready[sibling["id"]]) == "not_owned"


def test_cancel_before_commit_wins(setup):
    store, ids = setup
    lead, ready = prepared(store)
    store.cancel_job(lead["id"])
    assert store.commit_preliminary_result(lead, ready[lead["id"]]) == "cancelled"
    assert store.get_summary("scan", "a.txt", "preliminary_file_summary") is None


def test_pause_prevents_publication(setup):
    store, ids = setup
    lead, ready = prepared(store)
    store.set_package_processing_state("scan", "paused")
    assert store.commit_preliminary_result(lead, ready[lead["id"]]) == "cancelled"
    assert store.get_summary("scan", "a.txt", "preliminary_file_summary") is None


def test_changed_document_and_selection_invalidate(setup):
    store, ids = setup
    lead, ready = prepared(store)
    store.save_document("scan", "a.txt", {"text": "新的内容"})
    assert store.commit_preliminary_result(lead, ready[lead["id"]]) == "stale"
    updated = generate_ready_results(Model(), store, lead, fallback)["_preliminary_ready_results"]
    store.save_scan_selection("scan", ["a.txt", "b.txt", "c.txt", "d.txt"], status="confirmed")
    assert store.commit_preliminary_result(lead, updated[lead["id"]]) == "stale"


def test_atomic_commit_rolls_back_when_job_write_fails(setup):
    store, ids = setup
    lead, ready = prepared(store)
    with store._connect() as conn:
        conn.execute("CREATE TRIGGER reject_completion BEFORE UPDATE ON analysis_jobs WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.commit_preliminary_result(lead, ready[lead["id"]])
    assert store.get_summary("scan", "a.txt", "preliminary_file_summary") is None
    assert store.get_job(lead["id"])["status"] == "running"


def test_only_missing_file_is_retried(setup):
    store, ids = setup
    lead = store.claim_next_job("worker")
    model = Model(omit="b.txt")
    results = generate_ready_results(model, store, lead, fallback)["_preliminary_ready_results"]
    assert len(results) == 4
    assert [len(rows) for rows, kwargs in model.calls] == [4, 1]
    assert model.calls[1][0][0]["path"] == "b.txt"
    assert all(kwargs["structured_retry_limit"] == 0 for rows, kwargs in model.calls)
    assert not any(v["result"]["degraded"] for v in results.values())


def test_4k_packing_leaves_unsent_files_queued_until_their_own_claim(setup, monkeypatch):
    store, ids = setup
    monkeypatch.setattr(Config, "LLM_CONTEXT_TOKENS", 4096)
    monkeypatch.setattr(Config, "PRELIMINARY_SUMMARY_BATCH_SIZE", 4)
    monkeypatch.setattr(Config, "PRELIMINARY_SUMMARY_CONCURRENCY", 1)
    model = Model()
    completed = []
    original_documents = {
        store.get_job(job_id)["options"]["path"]:
            store.get_document("scan", store.get_job(job_id)["options"]["path"])
        for job_id in ids
    }

    for expected_id in ids:
        lead = store.claim_next_job("worker")
        assert lead["id"] == expected_id
        result = generate_ready_results(model, store, lead, fallback)
        ready = result["_preliminary_ready_results"]
        sent = model.calls[-1][0]
        assert [row["file_id"] for row in sent] == [expected_id]
        assert set(ready) == {expected_id}
        assert result["_preliminary_batch_size"] == 1
        assert store.get_job(expected_id)["status"] == "running"
        assert not ready[expected_id]["result"]["degraded"]
        unsent = ids[len(completed) + 1:]
        assert all(store.get_job(job_id)["status"] == "queued" for job_id in unsent)
        assert all(store.get_summary("scan", store.get_job(job_id)["options"]["path"],
                                     "preliminary_file_summary") is None for job_id in unsent)
        assert store.commit_preliminary_result(lead, ready[expected_id]) == "completed"
        completed.append(expected_id)

    assert completed == ids
    assert [row["file_id"] for rows, _kwargs in model.calls for row in rows] == ids
    assert all(store.get_job(job_id)["status"] == "completed" for job_id in ids)
    assert all(store.get_summary("scan", path, "preliminary_file_summary") is not None
               for path in original_documents)
    assert all(store.get_document("scan", path) == document
               for path, document in original_documents.items())
    assert store.claim_next_job("worker") is None


def test_fallback_is_marked_and_retries_bounded(setup):
    store, ids = setup
    lead = store.claim_next_job("worker")
    model = Model(broken=True)
    results = generate_ready_results(model, store, lead, fallback)["_preliminary_ready_results"]
    assert len(model.calls) == 5
    assert all(v["result"]["degraded"] for v in results.values())
    assert all(v["result"]["summary"]["generated_by"] == "local-preliminary-fallback" for v in results.values())


def test_worker_cache_hit_never_spawns_and_preserves_priority(setup, monkeypatch):
    import worker
    store, ids = setup
    lead, ready = prepared(store)
    store.commit_preliminary_result(lead, ready[lead["id"]])
    store.create_or_get_typed_job("scan", "conversation_turn", options={"question": "test"}, owner_id="owner")
    # Explicit priority demonstrates that the original queue decides the next job.
    with store._connect() as conn:
        conn.execute("UPDATE analysis_jobs SET priority=1000 WHERE task_type='conversation_turn'")
    higher = store.claim_next_job("worker")
    assert higher["task_type"] == "conversation_turn"
    store.finalize_job(higher["id"], result={})
    sibling = store.claim_next_job("worker")
    monkeypatch.setattr(worker, "storage", store)
    monkeypatch.setattr(worker, "execute_supervised", lambda job: pytest.fail("cache hit must not spawn"))
    result = worker.execute_with_preliminary_cache(sibling, ready)
    assert result["_preliminary_committed"]
    assert store.get_job(sibling["id"])["status"] == "completed"


def test_other_lanes_and_disabled_flag_unchanged(setup, monkeypatch):
    store, ids = setup
    job = store.claim_next_job("worker")
    for source in ("candidate_preview_model_summary", "idle_deep_model_summary", "preliminary_node_summary"):
        other = copy.deepcopy(job);other["options"]["workflow_source"] = source
        assert not enabled(other)
    monkeypatch.setattr(Config, "PRELIMINARY_SUMMARY_OPTIMIZATION_ENABLED", False)
    assert not enabled(job)


def test_expired_wrong_owner_and_wrong_model_rejected(setup):
    store, ids = setup
    lead, ready = prepared(store)
    packet = ready[lead["id"]]
    for mutate in (lambda p: p.update(created_at=time.time()-1900), lambda p: p["identity"].update(owner_id="other"), lambda p: p["configuration"].update(model="other")):
        bad = copy.deepcopy(packet);mutate(bad)
        assert store.commit_preliminary_result(lead, bad) == "stale"


def test_runtime_retry_opt_out_preserves_default():
    from services.agent_runtime import PydanticAgentRuntime
    from services.ollama import LocalModelError
    class Transport:
        model="fake";base_url="local";configured=True;requires_confirmation=False;privacy_label="test"
        def __init__(self):self.calls=0
        def chat(self, *args, **kwargs):
            self.calls+=1
            return {"content": '{"wrong":1}' if self.calls==1 else '{"core_summary":"valid"}'}
    transport=Transport()
    with pytest.raises(LocalModelError):
        PydanticAgentRuntime(transport).chat_json("s","u",required_fields=("core_summary",),structured_retry_limit=0)
    assert transport.calls==1
    transport=Transport()
    assert PydanticAgentRuntime(transport).chat_json("s","u",required_fields=("core_summary",))["json"]["core_summary"]=="valid"
    assert transport.calls==2


def test_sidecar_replacement_invalidates_ready(setup):
    store, ids = setup
    store.sidecar_threshold = 1
    store.save_document("scan", "a.txt", {"source": {"name": "a.txt"}, "text": "old text " * 1000, "evidence": []})
    lead, ready = prepared(store)
    store.save_document("scan", "a.txt", {"source": {"name": "a.txt"}, "text": "new text " * 1000, "evidence": []})
    assert store.commit_preliminary_result(lead, ready[lead["id"]]) == "stale"


def test_deleted_package_never_recreated_by_ready_result(setup):
    store, ids = setup
    lead, ready = prepared(store)
    with store._connect() as conn:
        conn.execute("DELETE FROM scans WHERE id='scan'")
    assert store.commit_preliminary_result(lead, ready[lead["id"]]) in {"missing", "stale"}
    assert store.get_summary("scan", "a.txt", "preliminary_file_summary") is None


def test_ready_does_not_overwrite_deep_summary(setup):
    store, ids = setup
    store.save_summary("scan", "a.txt", "deep_file_summary", {"summary": "deep result", "deep_analysis": True})
    lead, ready = prepared(store)
    assert store.commit_preliminary_result(lead, ready[lead["id"]]) == "completed"
    assert store.get_summary("scan", "a.txt", "deep_file_summary")["summary"] == "deep result"


def test_unversioned_active_summary_jobs_are_reused_without_duplicates(setup, monkeypatch):
    import app
    store, ids = setup
    monkeypatch.setattr(app, "storage", store)
    now = time.time()
    checkpoint = {"selected_paths": ["a.txt", "b.txt", "c.txt", "d.txt"],
                  "preliminary_summary_job_ids": ids}
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO import_tasks(task_id,scan_id,root_path,status,current_phase,checkpoint,owner_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("import", "scan", "/test", "parsed_overview", "parsed_overview",
             json.dumps(checkpoint), "owner", now, now),
        )
    assert app._queue_preliminary_file_summaries("scan", "owner") == ids
    jobs = [item for item in store.list_jobs(owner_id="owner")
            if item["task_type"] == "generate_summary"]
    assert len(jobs) == len(ids)
    assert {item["id"] for item in jobs} == set(ids)


def test_ready_node_starts_before_unrelated_file_jobs_finish(setup, monkeypatch):
    import app
    store, ids = setup
    monkeypatch.setattr(app, "storage", store)
    paths = ["a.txt", "b.txt", "c.txt", "d.txt"]
    checkpoint = {"selected_paths": paths, "preliminary_summary_job_ids": ids}
    now = time.time()
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO import_tasks(task_id,scan_id,root_path,status,current_phase,checkpoint,owner_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("import", "scan", "/test", "preliminary_summarizing", "preliminary_summarizing",
             json.dumps(checkpoint), "owner", now, now),
        )
    monkeypatch.setattr(app, "_build_candidate_preview_directory", lambda _scan_id: {
        "topics": [
            {"node_id": "ready", "name": "已完成节点", "member_paths": ["a.txt", "b.txt"]},
            {"node_id": "waiting", "name": "等待节点", "member_paths": ["c.txt", "d.txt"]},
        ]
    })
    for _ in range(2):
        job, ready = prepared(store)
        assert store.commit_preliminary_result(job, ready[job["id"]]) == "completed"
    node_ids = app._queue_preliminary_node_summaries("scan", "owner")
    assert node_ids
    assert store.get_job(node_ids[0])["options"]["node_id"] == "ready"
    assert not any(store.get_job(job_id)["options"].get("node_id") == "waiting"
                   for job_id in node_ids)


def test_file_completion_advances_existing_node_and_overview_hooks(setup, monkeypatch):
    import app
    store, ids = setup
    monkeypatch.setattr(app, "storage", store)
    paths = ["a.txt", "b.txt", "c.txt", "d.txt"]
    checkpoint = {"selected_paths": paths, "preliminary_summary_job_ids": ids}
    now = time.time()
    with store._connect() as conn:
        conn.execute("INSERT INTO import_tasks(task_id,scan_id,root_path,status,current_phase,checkpoint,owner_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     ("import", "scan", "/test", "preliminary_summarizing", "preliminary_summarizing", json.dumps(checkpoint), "owner", now, now))
    monkeypatch.setattr(app, "_build_candidate_preview_directory", lambda scan_id: {"topics": [{"node_id": "topic", "name": "主题", "member_paths": paths}]})
    lead, ready = prepared(store)
    assert app._queue_preliminary_node_summaries("scan", "owner") == []
    job = lead
    for _ in paths:
        assert store.commit_preliminary_result(job, ready[job["id"]]) == "completed"
        if job["id"] != ids[-1]:
            assert app._queue_preliminary_node_summaries("scan", "owner") == []
            job = store.claim_next_job("worker")
    nodes = app._queue_preliminary_node_summaries("scan", "owner")
    assert len(nodes) == 1
    assert app._queue_preliminary_node_summaries("scan", "owner") == nodes
    assert app._queue_preliminary_overview("scan", "owner") is None
    node = store.claim_next_job("worker")
    store.save_summary("scan", "node:topic", "preliminary_node_summary", {
        "analysis_stage": "preliminary", "member_paths": paths,
        "member_source_fingerprint": node["options"]["member_source_fingerprint"],
        "summary": "node summary",
    })
    store.finalize_job(node["id"], result={})
    report = app._queue_preliminary_overview("scan", "owner")
    assert report
    assert app._queue_preliminary_overview("scan", "owner") == report
    assert store.get_job(report)["options"]["workflow_source"] == "preliminary_results_report"


def test_transient_commit_error_retains_ready_cache(setup, monkeypatch):
    import worker
    store, ids = setup
    lead, cache = prepared(store)
    monkeypatch.setattr(worker, "storage", store)
    def locked(*args):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(store, "commit_preliminary_result", locked)
    monkeypatch.setattr(worker, "execute_supervised", lambda job: pytest.fail("ready result must be retained"))
    with pytest.raises(sqlite3.OperationalError):
        worker.execute_with_preliminary_cache(lead, cache)
    assert lead["id"] in cache


def test_disabling_feature_discards_new_prefetch_and_uses_legacy_executor(setup, monkeypatch):
    import worker
    store, ids = setup
    job, ready = prepared(store)
    monkeypatch.setattr(worker, "storage", store)
    monkeypatch.setattr(Config, "PRELIMINARY_SUMMARY_OPTIMIZATION_ENABLED", False)
    seen = []
    def legacy(current):
        seen.append(current)
        return {"legacy": True}
    monkeypatch.setattr(worker, "execute_supervised", legacy)
    assert worker.execute_with_preliminary_cache(job, ready) == {"legacy": True}
    assert seen == [job]
    assert "_preliminary_prefetched_summary" not in seen[0]["options"]


def test_restart_without_cache_keeps_independent_jobs_executable(setup, monkeypatch):
    import worker
    store, ids = setup
    lead, ready = prepared(store)
    store.commit_preliminary_result(lead, ready[lead["id"]])
    job = store.claim_next_job("restarted-worker")
    monkeypatch.setattr(worker, "storage", store)
    calls=[]
    def regenerate(current):
        calls.append(current["id"])
        return generate_ready_results(Model(), store, current, fallback)
    monkeypatch.setattr(worker, "execute_supervised", regenerate)
    result = worker.execute_with_preliminary_cache(job, {})
    assert result["_preliminary_committed"]
    assert calls == [job["id"]]
    assert store.get_job(lead["id"])["status"] == "completed"
    assert store.get_job(job["id"])["status"] == "completed"
