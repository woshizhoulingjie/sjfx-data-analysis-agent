#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""极限测试：初步摘要输入拉满（每文件 6500 字上限）时的 2 路并发安全性。

关注四件事：
  1) 单次请求真实 prompt/completion tokens 是多少
  2) 两路并发时 KV 占用峰值
  3) vLLM 有没有发生 preemption（抢占=上下文被击穿的信号）
  4) 结果有没有降级

用法： .venv/bin/python bench_max_context.py
"""
import io
import json
import os
import re
import statistics
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config                                     # noqa: E402
from services.storage import Storage                          # noqa: E402
from services.vllm import VLLMClient                          # noqa: E402
from services.agent_runtime import PydanticAgentRuntime        # noqa: E402
from services.preliminary_summary_fast import generate_ready_results  # noqa: E402
from services.preliminary_summary_input import build_preliminary_input  # noqa: E402

FILES = 8          # 8 个文件 = width 2 × 每批 4 个，正好两路拉满
METRICS = "http://127.0.0.1:8001/metrics"

SENTENCE = (
    "第{n}项数据：本期营业收入 {revenue} 万元，同比增长 {growth}%，毛利率 {margin}%，"
    "应收账款余额 {receivable} 万元，其中账龄超过一年的部分占比 {aging}%，"
    "公司尚未完成减值测试，审计机构未出具意见，关联方交易金额 {related} 万元。"
)


def build_text(seed):
    parts = []
    for index in range(300):
        parts.append(SENTENCE.format(
            n=index, revenue=1200 + seed * 7 + index, growth=5 + (seed + index) % 9,
            margin=18 + index % 7, receivable=600 + seed * 3 + index,
            aging=8 + (seed + index) % 13, related=90 + seed + index,
        ))
    return "第 %d 号材料：2026 年度经营与风险情况说明\n" % (seed + 1) + "\n".join(parts)


class Metrics:
    """采样并发、KV 占用、容量等待、抢占计数。"""

    def __init__(self):
        self.stop = threading.Event()
        self.max_running = 0
        self.max_kv = 0.0
        self.max_wait_capacity = 0
        self.thread = threading.Thread(target=self._loop, daemon=True)

    @staticmethod
    def snapshot():
        values = {"running": 0.0, "kv": 0.0, "wait_capacity": 0.0, "preempt": 0.0}
        with urllib.request.urlopen(METRICS, timeout=5) as response:
            for line in response.read().decode("utf-8", "replace").splitlines():
                line = line.strip()
                if line.startswith("vllm:num_requests_running"):
                    values["running"] = max(values["running"], float(line.rsplit(" ", 1)[1]))
                elif line.startswith("vllm:kv_cache_usage_perc"):
                    values["kv"] = max(values["kv"], float(line.rsplit(" ", 1)[1]))
                elif line.startswith("vllm:num_requests_waiting_by_reason") and 'reason="capacity"' in line:
                    values["wait_capacity"] = max(values["wait_capacity"], float(line.rsplit(" ", 1)[1]))
                elif line.startswith("vllm:num_preemptions_total"):
                    values["preempt"] = max(values["preempt"], float(line.rsplit(" ", 1)[1]))
        return values

    def start(self):
        self.thread.start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                values = self.snapshot()
                self.max_running = max(self.max_running, int(values["running"]))
                self.max_kv = max(self.max_kv, values["kv"])
                self.max_wait_capacity = max(self.max_wait_capacity, int(values["wait_capacity"]))
            except Exception:  # noqa: BLE001
                pass
            self.stop.wait(0.2)

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=5)
        return self.max_running, self.max_kv, self.max_wait_capacity


def fallback(document, path, reason):
    return {"core_summary": str(document.get("text") or "")[:400], "warnings": [reason]}


def main():
    Config.PRELIMINARY_SUMMARY_CONCURRENCY = 2
    Config.PRELIMINARY_SUMMARY_BATCH_SIZE = 4
    Config.PRELIMINARY_SUMMARY_OPTIMIZATION_ENABLED = True

    folder = tempfile.mkdtemp(prefix="sjfx-maxctx-")
    store = Storage(os.path.join(folder, "max.db"))
    store.save_scan({"root": folder, "tree": {}}, "scan", owner_id="owner")

    # 先看看输入构建器到底选了多少字
    sample = build_text(0)
    selected = build_preliminary_input(
        {"text": sample, "structure": {"title": "材料1"},
         "evidence": [{"evidence_id": "e", "source_path": "f", "text": s}
                      for s in sample.split("\n")[1:40]]},
        Config.PRELIMINARY_SUMMARY_INPUT_MAX_CHARS,
    )
    print("单文件原始正文 %d 字 -> 选择器输出 %d 字（上限 %d）"
          % (len(sample), len(selected), Config.PRELIMINARY_SUMMARY_INPUT_MAX_CHARS))
    print("每批 4 个文件 -> 正文约 %d 字，加系统提示与 JSON 契约后 ≈ 每请求上限" % (4 * len(selected)))

    for index in range(FILES):
        name = "材料%03d.txt" % (index + 1)
        text = build_text(index)
        store.save_document("scan", name, {
            "text": text, "source": {"name": name}, "structure": {"title": name},
            "evidence": [{"evidence_id": name, "source_path": name, "text": line}
                         for line in text.split("\n")[1:60]],
        })
        store.create_or_get_typed_job(
            "scan", "generate_summary",
            options={"path": name, "kind": "file", "analysis_level": "preview",
                     "workflow_source": "preliminary_model_summary"},
            owner_id="owner",
        )

    before = Metrics.snapshot()
    print("\n测试前：preemptions=%s  kv=%.1f%%" % (before["preempt"], before["kv"] * 100))

    transport = VLLMClient(Config.VLLM_BASE_URL, Config.VLLM_MODEL,
                           timeout=Config.VLLM_REQUEST_TIMEOUT,
                           max_concurrency=2, api_key=Config.VLLM_API_KEY)
    llm = PydanticAgentRuntime(transport)

    sampler = Metrics()
    sampler.start()
    started = time.monotonic()
    calls = []
    ready_all = {}
    while True:
        lead = store.claim_next_job("bench")
        if not lead:
            break
        result = generate_ready_results(llm, store, lead, fallback)
        ready = result.get("_preliminary_ready_results") or {}
        for value in ready.values():
            ready_all[value["result"]["path"]] = value["result"]
            info = (value["result"]["summary"].get("parser_info") or {})
            usage = info.get("usage") or {}
            calls.append({"files": info.get("batch_size"),
                          "prompt": usage.get("prompt_tokens"),
                          "completion": usage.get("completion_tokens"),
                          "degraded": info.get("degraded")})
        if str(lead["id"]) in ready:
            store.commit_preliminary_result(lead, ready[str(lead["id"])])
        if len(ready) <= 1:
            break
    elapsed = time.monotonic() - started
    peak_running, peak_kv, peak_wait = sampler.finish()
    after = Metrics.snapshot()

    print("\n=== 结果 ===")
    print("完成文件数        : %d" % len(ready_all))
    print("墙钟耗时          : %.1fs" % elapsed)
    print("vLLM 并发峰值     : %d 路" % peak_running)
    print("KV 占用峰值       : %.1f%%  (KV 池 %d tokens)"
          % (peak_kv * 100, 61166))
    print("因容量等待的峰值  : %d" % peak_wait)
    print("抢占次数 前/后    : %s -> %s" % (before["preempt"], after["preempt"]))
    print("降级条数          : %d" % sum(1 for c in calls if c["degraded"]))
    print("\n每次模型请求：")
    for call in calls:
        print("   files=%s prompt=%s completion=%s degraded=%s"
              % (call["files"], call["prompt"], call["completion"], call["degraded"]))
    prompts = [c["prompt"] for c in calls if c["prompt"]]
    completions = [c["completion"] for c in calls if c["completion"]]
    if prompts:
        print("\n单请求 prompt 最大 : %d tokens" % max(prompts))
        print("两路并发 KV 需求上限: %d tokens（2 × (%d + %d)）"
              % (2 * (max(prompts) + max(completions)), max(prompts), max(completions)))
        print("KV 池容量          : 61166 tokens")
    print("\n生成来源分布       : %s"
          % json.dumps({k: sum(1 for v in ready_all.values()
                               if v["summary"].get("generated_by") == k)
                        for k in {v["summary"].get("generated_by") for v in ready_all.values()}},
                       ensure_ascii=False))
    import shutil
    shutil.rmtree(folder, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
