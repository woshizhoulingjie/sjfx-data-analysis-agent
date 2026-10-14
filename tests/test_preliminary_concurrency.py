# -*- coding: utf-8 -*-
"""初步摘要多路并发的契约测试。

默认（PRELIMINARY_SUMMARY_CONCURRENCY=1）必须与改动前逐字一致：一次一批、
串行；显式配置 4 时才真正并发 4 路，且覆盖的文件集合不变。
"""
import json
import threading
import time

import pytest

from config import Config
from services.storage import Storage
from services.preliminary_summary_fast import generate_ready_results


class ProbeModel:
    """记录同时在飞的请求数，并模拟模型耗时。"""

    model = Config.LLM_MODEL

    def __init__(self, delay=0.4):
        self.delay = delay
        self.lock = threading.Lock()
        self.live = 0
        self.peak = 0
        self.calls = []

    def chat_json(self, system, prompt, **kwargs):
        rows = json.loads(prompt.split("分析材料：\n", 1)[1])
        with self.lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
            self.calls.append([r["path"] for r in rows])
        try:
            time.sleep(self.delay)
            return {
                "json": {"documents": [
                    {"file_id": r["file_id"], "path": r["path"], "title": r["path"],
                     "core_summary": "本季度收入120万元，尚未审计。", "topics": ["收入"]}
                    for r in rows
                ]},
                "model": self.model,
                "usage": {"completion_tokens": 120},
                "finish_reason": "stop",
            }
        finally:
            with self.lock:
                self.live -= 1


def fallback(document, path, reason):
    return {"core_summary": document["text"], "warnings": [reason]}


@pytest.fixture
def setup(tmp_path):
    store = Storage(tmp_path / "test.db")
    store.save_scan({"root": str(tmp_path), "tree": {}}, "scan", owner_id="owner")
    names = ["f%02d.txt" % index for index in range(16)]
    for name in names:
        store.save_document("scan", name, {
            "text": name + " 本季度收入120万元，同比提高10%，尚未审计。",
            "source": {"name": name},
            "structure": {"title": name},
            "evidence": [{"evidence_id": name, "source_path": name,
                          "text": "本季度收入120万元，同比提高10%，尚未审计。"}],
        })
        store.create_or_get_typed_job(
            "scan", "generate_summary",
            options={"path": name, "kind": "file", "analysis_level": "preview",
                     "workflow_source": "preliminary_model_summary"},
            owner_id="owner",
        )
    return store, names


def _run(store, monkeypatch, width):
    # The four-file batching contract uses a fake model with a 32K window.
    monkeypatch.setattr(Config, "LLM_CONTEXT_TOKENS", 32768)
    monkeypatch.setattr(Config, "PRELIMINARY_SUMMARY_CONCURRENCY", width)
    monkeypatch.setattr(Config, "PRELIMINARY_SUMMARY_BATCH_SIZE", 4)
    lead = store.claim_next_job("worker")
    model = ProbeModel()
    started = time.monotonic()
    result = generate_ready_results(model, store, lead, fallback)
    return model, result["_preliminary_ready_results"], time.monotonic() - started


def test_default_width_is_serial_and_unchanged(setup, monkeypatch):
    store, names = setup
    model, ready, _elapsed = _run(store, monkeypatch, 1)
    assert model.peak == 1                      # 默认不并发
    assert [len(call) for call in model.calls] == [4]   # 一次一批 4 个文件
    assert len(ready) == 4
    assert all(not item["result"]["degraded"] for item in ready.values())


def test_width_four_issues_four_concurrent_batches(setup, monkeypatch):
    store, names = setup
    model, ready, _elapsed = _run(store, monkeypatch, 4)
    assert model.peak == 4                      # 真的同时有 4 个请求在飞
    assert sorted(len(call) for call in model.calls) == [4, 4, 4, 4]
    assert len(ready) == 16
    covered = sorted(path for call in model.calls for path in call)
    assert covered == sorted(names)             # 覆盖集合不变、不重复
    assert all(not item["result"]["degraded"] for item in ready.values())
    assert all(item["result"]["summary"]["generated_by"] == "model-preliminary-analysis"
               for item in ready.values())


def test_width_four_recovers_only_missing_files(tmp_path, monkeypatch):
    store = Storage(tmp_path / "test.db")
    store.save_scan({"root": str(tmp_path), "tree": {}}, "scan", owner_id="owner")
    names = ["g%02d.txt" % index for index in range(8)]
    for name in names:
        store.save_document("scan", name, {
            "text": name + " 收入120万元。", "source": {"name": name},
            "structure": {"title": name},
            "evidence": [{"evidence_id": name, "source_path": name, "text": "收入120万元。"}],
        })
        store.create_or_get_typed_job(
            "scan", "generate_summary",
            options={"path": name, "kind": "file", "analysis_level": "preview",
                     "workflow_source": "preliminary_model_summary"},
            owner_id="owner",
        )

    class Omitting(ProbeModel):
        def chat_json(self, system, prompt, **kwargs):
            rows = json.loads(prompt.split("分析材料：\n", 1)[1])
            with self.lock:
                self.live += 1
                self.peak = max(self.peak, self.live)
                self.calls.append([r["path"] for r in rows])
            try:
                time.sleep(self.delay)
                keep = [r for r in rows if not (r["path"] == "g03.txt" and len(rows) > 1)]
                return {"json": {"documents": [
                    {"file_id": r["file_id"], "path": r["path"], "title": r["path"],
                     "core_summary": "收入120万元。", "topics": ["收入"]} for r in keep
                ]}, "model": self.model, "usage": {}, "finish_reason": "stop"}
            finally:
                with self.lock:
                    self.live -= 1

    monkeypatch.setattr(Config, "LLM_CONTEXT_TOKENS", 32768)
    monkeypatch.setattr(Config, "PRELIMINARY_SUMMARY_CONCURRENCY", 2)
    monkeypatch.setattr(Config, "PRELIMINARY_SUMMARY_BATCH_SIZE", 4)
    lead = store.claim_next_job("worker")
    model = Omitting()
    result = generate_ready_results(model, store, lead, fallback)
    ready = result["_preliminary_ready_results"]
    assert len(ready) == 8
    # 只有缺失的那个文件被单独重试，而且结果没有降级
    assert [call for call in model.calls if call == ["g03.txt"]]
    assert not ready[[k for k, v in ready.items()
                      if v["result"]["path"] == "g03.txt"][0]]["result"]["degraded"]
