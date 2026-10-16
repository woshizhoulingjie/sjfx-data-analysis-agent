"""Ordinary preliminary recovery budgets, without a model service or database."""
import copy
import json
from types import SimpleNamespace

from config import Config
from services import preliminary_summary_fast as fast
from services.agent_runtime import PydanticAgentRuntime
from services.model_budget import request_budget
from sjfx_protection import cache, ready


def _rows():
    rows = []
    for job_id in ("a", "b", "c", "d"):
        path = job_id + ".txt"
        text = "模拟材料记录了检查流程，其他条件尚未验证。"
        rows.append({
            "job": {"id": job_id, "scan_id": "fixture", "owner_id": "owner",
                    "options": {"path": path, "kind": "file",
                                "workflow_source": "preliminary_model_summary"}},
            "document": {"text": text, "source": {"path": path},
                         "structure": {"title": "模拟检查"},
                         "evidence": [{"evidence_id": "E-" + job_id,
                                       "source_path": path, "text": text}]},
            "stamp": {},
        })
    return rows


class Store:
    db_path = "unused-fixture-database"

    def __init__(self, rows):
        self.rows = {row["job"]["id"]: row for row in rows}

    def preliminary_document_snapshot(self, job):
        row = self.rows[job["id"]]
        return {"document": copy.deepcopy(row["document"]), "stamp": {}}

    def is_job_cancel_requested(self, job_id):
        return False


class RecoveryTransport:
    model = "fixture"
    base_url = "fixture://local"
    configured = True
    requires_confirmation = False
    privacy_label = "fixture"

    def __init__(self, fail_recovery=False):
        self.calls = []
        self.fail_recovery = fail_recovery

    def chat(self, system, prompt, **kwargs):
        files = json.loads(prompt.split("分析材料：\n", 1)[1])
        self.calls.append(([item["file_id"] for item in files], kwargs))
        # A valid sibling survives a per-file batch failure. The recovery needs
        # more than the former 700-token allowance for its larger contract.
        selected = files[1:] if len(files) > 1 else files
        values = [{"file_id": item["file_id"], "path": item["path"],
                   "title": "模拟检查", "core_summary": "检查已记录，其他条件尚未验证。",
                   "key_facts": [], "conclusions": [],
                   "structure_overview": {"sections": []},
                   "recommended_research_direction": {"title": "", "questions": []}}
                  for item in selected]
        exhausted = len(files) == 1 and (self.fail_recovery or kwargs["max_tokens"] < 1000)
        return {"content": json.dumps({"documents": values}, ensure_ascii=False),
                "model": self.model, "usage": {},
                "finish_reason": "length" if exhausted else "stop"}


def _generate(monkeypatch, transport, rows=None, context=32768):
    rows = rows or _rows()[:2]
    jobs = [row["job"] for row in rows]
    monkeypatch.setattr(fast, "collect_jobs", lambda *args, **kwargs: jobs)
    monkeypatch.setattr(Config, "PRELIMINARY_SUMMARY_BATCH_SIZE", 4)
    monkeypatch.setattr(Config, "PRELIMINARY_SUMMARY_CONCURRENCY", 1)
    monkeypatch.setattr(Config, "LLM_CONTEXT_TOKENS", context)
    monkeypatch.setattr(cache, "capture_sources", lambda: [])
    monkeypatch.setattr(ready, "publish_many", lambda *args: None)
    fallback_calls = []

    def fallback(document, path, reason):
        fallback_calls.append(path)
        return {"core_summary": document["text"], "warnings": [reason]}

    result = fast.generate_ready_results(PydanticAgentRuntime(transport), Store(rows), jobs[0], fallback)
    return result["_preliminary_ready_results"], fallback_calls


def test_single_recovery_uses_existing_preview_budget_and_preserves_sibling(monkeypatch):
    transport = RecoveryTransport()
    packets, fallback_calls = _generate(monkeypatch, transport)
    assert [(ids, kwargs["max_tokens"]) for ids, kwargs in transport.calls] == [
        (["a", "b"], 1300), (["a"], 1200)]
    assert fallback_calls == []
    assert all(not packet["result"]["degraded"] for packet in packets.values())
    assert packets["b"]["result"]["summary"]["core_summary"] == "检查已记录，其他条件尚未验证。"


def test_exhausted_recovery_remains_degraded_without_an_extra_retry(monkeypatch):
    transport = RecoveryTransport(fail_recovery=True)
    packets, fallback_calls = _generate(monkeypatch, transport)
    assert [ids for ids, kwargs in transport.calls] == [["a", "b"], ["a"]]
    assert fallback_calls == ["a.txt"]
    assert packets["a"]["result"]["degraded"] is True
    assert packets["a"]["result"]["summary"]["generated_by"] == "local-preliminary-fallback"
    assert packets["b"]["result"]["degraded"] is False


def test_multifile_packing_keeps_existing_budget_boundary(monkeypatch):
    rows = _rows()
    monkeypatch.setattr(Config, "LLM_CONTEXT_TOKENS", 5200)
    monkeypatch.setattr(fast, "build_preliminary_input", lambda *args: "fixture")
    monkeypatch.setattr(fast, "_estimated_tokens", lambda text: 100)
    groups = fast._collect_groups(Store(rows), [row["job"] for row in rows], 4, 2)
    # Two files exactly fit the existing 3700-token estimate plus reserve;
    # adding a third must start a separate group.
    assert [[row["job"]["id"] for row in group] for group in groups] == [["a", "b"], ["c", "d"]]


def test_oversized_full_input_is_rejected_before_transport_and_falls_back(monkeypatch):
    class BudgetTransport(RecoveryTransport):
        def chat(self, system, prompt, **kwargs):
            request_budget(system, prompt, kwargs["max_tokens"],
                           config=SimpleNamespace(LLM_CONTEXT_TOKENS=2048, LLM_TOKENIZER_PATH=""))
            return super().chat(system, prompt, **kwargs)

    transport = BudgetTransport()
    rows = _rows()[:1]
    source = "模拟长材料，其他条件尚未验证。" * 1000
    rows[0]["document"]["text"] = source
    rows[0]["document"]["evidence"][0]["text"] = source
    packets, fallback_calls = _generate(monkeypatch, transport, rows, context=2048)
    assert transport.calls == []
    assert fallback_calls == ["a.txt"]
    assert packets["a"]["result"]["degraded"] is True
    assert rows[0]["document"]["text"] == source


def test_previous_output_budget_ready_packet_is_invalidated():
    job = _rows()[0]["job"]
    packet = {"identity": fast.result_identity(job), "configuration": fast.configuration_key(),
              "created_at": fast.time.time()}
    assert fast.ready_matches(job, packet)
    packet["configuration"].pop("output_budget")
    assert not fast.ready_matches(job, packet)
