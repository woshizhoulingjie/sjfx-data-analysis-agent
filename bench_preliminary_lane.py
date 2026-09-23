#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""在真实 vLLM + 真实闸门上，对「初步摘要」这条泳道做 A/B 测速。

用法（服务器）：
    .venv/bin/python bench_preliminary_lane.py 16 1 2
（文件数、width 列表）

它只用一个临时 SQLite 库造文档，不碰生产 agent.db。
"""
import io
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config                      # noqa: E402
from services.storage import Storage           # noqa: E402
from services.preliminary_summary_fast import generate_ready_results  # noqa: E402
from services.vllm import VLLMClient           # noqa: E402
from services.agent_runtime import PydanticAgentRuntime  # noqa: E402

COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 16
WIDTHS = [int(x) for x in sys.argv[2:]] or [1, 2]

PARAGRAPH = (
    "本季度华东区域实现营业收入 {revenue} 万元，同比增长 {growth}%，毛利率 {margin}%。"
    "应收账款余额 {receivable} 万元，其中账龄超过一年的占 {aging}%，公司尚未完成减值测试，"
    "审计机构未出具意见。报告期内关联方交易金额 {related} 万元，定价依据为成本加成，"
    "尚待第三方评估确认。销售费用同比上升 {cost}%，主要来自渠道下沉与新产品试产。"
)


def build_text(index):
    parts = []
    for block in range(46):
        parts.append(PARAGRAPH.format(
            revenue=1200 + index * 7 + block, growth=5 + (index + block) % 9,
            margin=18 + (index + block) % 7, receivable=600 + index * 3 + block,
            aging=8 + (index + block) % 13, related=90 + index + block,
            cost=3 + (index + block) % 6,
        ))
    return "第 %d 号材料：2026 年季度经营情况说明\n\n" % (index + 1) + "\n".join(parts)


class Sampler:
    def __init__(self):
        self.stop = threading.Event()
        self.peak = 0
        self.hist = {}
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.thread.start()

    def _loop(self):
        pattern = re.compile(r"^vllm:num_requests_running\S*\s+([0-9.eE+-]+)$")
        while not self.stop.is_set():
            running = 0
            try:
                with urllib.request.urlopen("http://127.0.0.1:8001/metrics", timeout=5) as resp:
                    for line in resp.read().decode("utf-8", "replace").splitlines():
                        match = pattern.match(line.strip())
                        if match:
                            running = max(running, float(match.group(1)))
            except Exception:  # noqa: BLE001
                pass
            bucket = int(running)
            self.hist[bucket] = self.hist.get(bucket, 0) + 1
            self.peak = max(self.peak, bucket)
            self.stop.wait(0.5)

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=5)
        return self.peak, dict(sorted(self.hist.items()))


def fallback(document, path, reason):
    return {"core_summary": document.get("text", "")[:400], "warnings": [reason]}


def run_one(width, documents, folder):
    Config.PRELIMINARY_SUMMARY_CONCURRENCY = width
    Config.PRELIMINARY_SUMMARY_BATCH_SIZE = 4
    Config.PRELIMINARY_SUMMARY_OPTIMIZATION_ENABLED = True
    db = os.path.join(folder, "bench_%d.db" % width)
    if os.path.exists(db):
        os.remove(db)
    store = Storage(db)
    store.save_scan({"root": folder, "tree": {}}, "scan", owner_id="owner")
    names = []
    for index in range(COUNT):
        name = "材料%03d.txt" % (index + 1)
        text = documents[index]
        rows = [ln for ln in text.split("\n") if ln.strip()]
        store.save_document("scan", name, {
            "text": text, "source": {"name": name}, "structure": {"title": name},
            "evidence": [{"evidence_id": name, "source_path": name, "text": ln}
                         for ln in rows[1:24]],
        })
        store.create_or_get_typed_job(
            "scan", "generate_summary",
            options={"path": name, "kind": "file", "analysis_level": "preview",
                     "workflow_source": "preliminary_model_summary"},
            owner_id="owner",
        )
        names.append(name)

    transport = VLLMClient(Config.VLLM_BASE_URL, Config.VLLM_MODEL,
                           timeout=Config.VLLM_REQUEST_TIMEOUT,
                           max_concurrency=max(1, width),
                           api_key=Config.VLLM_API_KEY)
    llm = PydanticAgentRuntime(transport)

    sampler = Sampler()
    sampler.start()
    started = time.monotonic()
    total_files = 0
    degraded = 0
    llm_calls = 0
    while True:
        lead = store.claim_next_job("bench")
        if not lead:
            break
        result = generate_ready_results(llm, store, lead, fallback)
        ready = result.get("_preliminary_ready_results") or {}
        total_files += len(ready)
        for value in ready.values():
            item = value["result"]
            if item.get("degraded"):
                degraded += 1
            for call in (item.get("preliminary_timing") or {}).get("model_calls") or []:
                llm_calls += 1
        if str(lead["id"]) not in ready:
            break
        store.commit_preliminary_result(lead, ready[str(lead["id"])])
        if len(ready) <= 1:
            break
    elapsed = time.monotonic() - started
    peak, hist = sampler.finish()
    return {
        "width": width, "files": total_files, "seconds": round(elapsed, 1),
        "per_file": round(elapsed / max(1, total_files), 2),
        "degraded": degraded, "model_calls": llm_calls,
        "vllm_peak": peak, "vllm_hist": hist,
    }


def main():
    folder = tempfile.mkdtemp(prefix="sjfx-bench-")
    print("临时目录 %s，文件数 %d" % (folder, COUNT))
    documents = [build_text(i) for i in range(COUNT)]
    print("单文件正文约 %d 字符" % statistics.mean(len(d) for d in documents))
    rows = []
    for width in WIDTHS:
        print("\n--- width=%d 开始 ---" % width, flush=True)
        row = run_one(width, documents, folder)
        rows.append(row)
        print("    %s" % json.dumps(row, ensure_ascii=False), flush=True)
    print("\n=== 汇总 ===")
    base = next((r for r in rows if r["width"] == 1), None)
    for row in rows:
        gain = ""
        if base and row["width"] != 1 and base["per_file"]:
            gain = "  加速 %.2fx" % (base["per_file"] / row["per_file"])
        print("    width=%d  文件=%d  用时=%.1fs  %.2fs/文件  降级=%d  模型调用=%d  vLLM峰值=%d%s"
              % (row["width"], row["files"], row["seconds"], row["per_file"],
                 row["degraded"], row["model_calls"], row["vllm_peak"], gain))
    shutil.rmtree(folder, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
