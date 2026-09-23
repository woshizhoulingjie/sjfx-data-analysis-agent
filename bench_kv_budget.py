#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把「初步摘要 2 路并发，上下文拉满会不会击穿 KV」算清楚 + 实测验证。

算的部分（全部用代码里的真实常量）：
  单文件输入上限 = PRELIMINARY_SUMMARY_INPUT_MAX_CHARS = 6500 字符
                  （build_preliminary_input 末尾有 assert len(text) <= limit）
  每批文件数     = PRELIMINARY_SUMMARY_BATCH_SIZE = 4
  输出上限       = max_tokens = 100 + 600 * len(rows) = 2500（4 个文件时）
  预算闸门       = 1700 + Σ(PRELIMINARY_SUMMARY_INPUT_MAX_CHARS + 900)
                   超过 LLM_CONTEXT_TOKENS - 1500 = 31268 就收组

实测的部分：造 8 个「刚好走整篇正文分支、选择器输出接近 6500 上限」的文件，
width=2 跑两路，读 vLLM 的 kv_cache_usage_perc / preemptions / waiting_by_reason。
"""
import io
import json
import math
import os
import re
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
from services.document_analysis import _estimated_tokens       # noqa: E402
from services.preliminary_summary_fast import generate_ready_results  # noqa: E402
from services.preliminary_summary_input import build_preliminary_input  # noqa: E402

METRICS = "http://127.0.0.1:8001/metrics"
TARGET_CHARS = 6420          # 让「整篇正文」分支刚好成立，选择器输出贴近 6500
FILES = 8                    # 8 = width 2 × 每批 4，正好两路都拉满

SENTENCE = (
    "第{n}项：本期营业收入{revenue}万元，同比增长{growth}%，毛利率{margin}%，"
    "应收账款余额{receivable}万元，其中账龄超过一年的占比{aging}%，公司尚未完成减值测试，"
    "审计机构未出具意见，关联方交易金额{related}万元，定价依据为成本加成。"
)


def build_text(seed, target=TARGET_CHARS):
    parts = []
    size = 0
    index = 0
    while size < target:
        piece = SENTENCE.format(n=index, revenue=1200 + seed * 7 + index,
                                growth=5 + (seed + index) % 9, margin=18 + index % 7,
                                receivable=600 + seed * 3 + index,
                                aging=8 + (seed + index) % 13, related=90 + seed + index)
        parts.append(piece)
        size += len(piece)
        index += 1
    text = "".join(parts)[:target]
    return text


class Metrics:
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


def compute():
    """纯计算：用代码里的真实常量推出单请求上限。"""
    per_file = Config.PRELIMINARY_SUMMARY_INPUT_MAX_CHARS
    batch = Config.PRELIMINARY_SUMMARY_BATCH_SIZE
    context = Config.LLM_CONTEXT_TOKENS
    output = 100 + 600 * batch
    budget = context - 1500
    # 预算闸门：estimated 从 1700 起，每个文件加 _estimated_tokens(输入) + 900
    # 最保守的 _estimated_tokens = 每个汉字 1 token，所以 6500 字 => 6500 token
    cost = per_file + 900
    estimated = 1700
    fits = 0
    while fits < batch and estimated + cost <= budget:
        estimated += cost
        fits += 1
    print("=== 计算：初步摘要单次请求的硬上限 ===")
    print("  每文件输入上限      = %d 字符  (PRELIMINARY_SUMMARY_INPUT_MAX_CHARS)" % per_file)
    print("  每批文件数上限      = %d        (PRELIMINARY_SUMMARY_BATCH_SIZE)" % batch)
    print("  预算闸门            = %d token  (LLM_CONTEXT_TOKENS - 1500)" % budget)
    print("  单个文件按最保守估算 = %d token  (_estimated_tokens 汉字按 1:1)" % per_file)
    print("  预算允许的文件数     = %d 个（第 %d 个会被预算闸门挡下）" % (fits, fits + 1))
    print("  应用自己的估算上限   = %d token" % estimated)
    print("  输出上限            = %d token  (max_tokens = 100 + 600*%d)" % (output, batch))
    return per_file, batch, output, estimated


def main():
    Config.PRELIMINARY_SUMMARY_CONCURRENCY = 2
    Config.PRELIMINARY_SUMMARY_BATCH_SIZE = 4
    Config.PRELIMINARY_SUMMARY_OPTIMIZATION_ENABLED = True

    compute()
    print()

    folder = tempfile.mkdtemp(prefix="sjfx-cap-")
    store = Storage(os.path.join(folder, "cap.db"))
    store.save_scan({"root": folder, "tree": {}}, "scan", owner_id="owner")

    sample = build_text(0)
    selected = build_preliminary_input(
        {"text": sample, "structure": {"title": "材料1"},
         "evidence": [{"evidence_id": "e", "source_path": "f", "text": line}
                      for line in sample.split("\n")[1:40]]},
        Config.PRELIMINARY_SUMMARY_INPUT_MAX_CHARS,
    )
    print("=== 实测：选择器实际输出 ===")
    print("  原始正文 %d 字 -> 选择器输出 %d 字（上限 %d，占 %.0f%%）"
          % (len(sample), len(selected), Config.PRELIMINARY_SUMMARY_INPUT_MAX_CHARS,
             100.0 * len(selected) / Config.PRELIMINARY_SUMMARY_INPUT_MAX_CHARS))
    print("  应用估算 _estimated_tokens(选择器输出) = %d token" % _estimated_tokens(selected))
    print()

    for index in range(FILES):
        name = "材料%03d.txt" % (index + 1)
        text = build_text(index)
        store.save_document("scan", name, {
            "text": text, "source": {"name": name}, "structure": {"title": name},
            "evidence": [{"evidence_id": name, "source_path": name, "text": text[:1200]}],
        })
        store.create_or_get_typed_job(
            "scan", "generate_summary",
            options={"path": name, "kind": "file", "analysis_level": "preview",
                     "workflow_source": "preliminary_model_summary"},
            owner_id="owner",
        )

    before = Metrics.snapshot()
    transport = VLLMClient(Config.VLLM_BASE_URL, Config.VLLM_MODEL,
                           timeout=Config.VLLM_REQUEST_TIMEOUT,
                           max_concurrency=2, api_key=Config.VLLM_API_KEY)
    llm = PydanticAgentRuntime(transport)

    sampler = Metrics()
    sampler.start()
    started = time.monotonic()
    calls = []
    done = {}
    while True:
        lead = store.claim_next_job("bench")
        if not lead:
            break
        result = generate_ready_results(llm, store, lead, fallback)
        ready = result.get("_preliminary_ready_results") or {}
        for value in ready.values():
            done[value["result"]["path"]] = value["result"]
            info = value["result"]["summary"].get("parser_info") or {}
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

    max_prompt = max((c["prompt"] or 0) for c in calls)
    max_completion = max((c["completion"] or 0) for c in calls)
    worst_kv = 2 * (max_prompt + max_completion)

    print("=== 实测：两路都拉满 ===")
    print("  完成文件数          : %d" % len(done))
    print("  墙钟耗时            : %.1fs" % elapsed)
    print("  vLLM 并发峰值       : %d 路" % peak_running)
    print("  单请求 prompt 最大  : %d token" % max_prompt)
    print("  单请求 completion 最大: %d token" % max_completion)
    print("  单请求 KV 占用      : %d token" % (max_prompt + max_completion))
    print("  两路最坏 KV 需求    : %d token" % worst_kv)
    print("  KV 池容量           : 61166 token")
    print("  计算余量            : %d token (%.1f%%)"
          % (61166 - worst_kv, 100.0 * (61166 - worst_kv) / 61166))
    print("  实测 KV 占用峰值    : %.1f%%  (= %d token)"
          % (peak_kv * 100, int(peak_kv * 61166)))
    print("  因容量等待峰值      : %d" % peak_wait)
    print("  抢占次数 前/后      : %s -> %s" % (before["preempt"], after["preempt"]))
    print("  降级条数            : %d" % sum(1 for c in calls if c["degraded"]))
    print("  生成来源            : %s"
          % json.dumps({k: sum(1 for v in done.values() if v["summary"].get("generated_by") == k)
                        for k in {v["summary"].get("generated_by") for v in done.values()}},
                       ensure_ascii=False))
    print("  请求批次分布        : %s"
          % json.dumps({str(k): sum(1 for c in calls if c["files"] == k)
                        for k in {c["files"] for c in calls}}, ensure_ascii=False))
    import shutil
    shutil.rmtree(folder, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
