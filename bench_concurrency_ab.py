#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""干净地量化单卡上「2 路并发」到底能提速多少。

同样的提示词、同样的总工作量，只改变「一次发 1 个」还是「一次发 2 个」：
    N=1: 4 个请求串行
    N=2: 4 个请求，2 路并发跑 2 轮
直接打 vLLM 的 /v1/chat/completions，同时采样 vllm:num_requests_running。
"""
import json
import re
import statistics
import sys
import threading
import time
import urllib.request

URL = "http://127.0.0.1:8001/v1/chat/completions"
METRICS = "http://127.0.0.1:8001/metrics"
REQUESTS = int(sys.argv[1]) if len(sys.argv) > 1 else 4
MAX_TOKENS = int(sys.argv[2]) if len(sys.argv) > 2 else 1200
# 生产实测：一批 4 个文件的初步摘要约 7266 prompt tokens。
# 中文大约 1 token ≈ 1.15 字，取 8000 字左右做基准。
BODY_CHARS = int(sys.argv[3]) if len(sys.argv) > 3 else 8600

FILLER = (
    "第{index}项：本季度实现营业收入{revenue}万元，同比增长{growth}%，毛利率{margin}%，"
    "应收账款余额{receivable}万元，其中账龄超过一年占比{aging}%，尚未完成减值测试，"
    "审计机构未出具意见；关联方交易金额{related}万元，定价依据为成本加成。"
)


def make_prompt(seed):
    parts = []
    size = 0
    index = 0
    while size < BODY_CHARS:
        piece = FILLER.format(index=index, revenue=1200 + seed * 7 + index,
                              growth=5 + (seed + index) % 9, margin=18 + index % 7,
                              receivable=600 + seed * 3 + index,
                              aging=8 + (seed + index) % 13, related=90 + seed + index)
        parts.append(piece)
        size += len(piece)
        index += 1
    return ("请阅读下面的材料，输出 JSON：{\"documents\":[{\"file_id\":\"原ID\","
            "\"core_summary\":\"约150字中文摘要\",\"key_facts\":[],\"conclusions\":[]}]}\n材料：\n"
            + "".join(parts))


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
                with urllib.request.urlopen(METRICS, timeout=5) as resp:
                    for line in resp.read().decode("utf-8", "replace").splitlines():
                        match = pattern.match(line.strip())
                        if match:
                            running = max(running, float(match.group(1)))
            except Exception:  # noqa: BLE001
                pass
            bucket = int(running)
            self.hist[bucket] = self.hist.get(bucket, 0) + 1
            self.peak = max(self.peak, bucket)
            self.stop.wait(0.2)

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=5)
        return self.peak, dict(sorted(self.hist.items()))


def fire(prompt, out, slot):
    body = json.dumps({
        "model": "qwen3.6:27b",
        "messages": [{"role": "system", "content": "你是严谨的中文资料摘要助手，只输出 JSON。"},
                     {"role": "user", "content": prompt}],
        "temperature": 0.1, "max_tokens": MAX_TOKENS, "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(URL, data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            data = json.loads(response.read().decode("utf-8"))
        out[slot] = {"seconds": round(time.monotonic() - started, 1),
                     "usage": data.get("usage") or {}}
    except Exception as exc:  # noqa: BLE001
        out[slot] = {"error": repr(exc)[:200]}


def run(width, total, prompts):
    sampler = Sampler()
    sampler.start()
    started = time.monotonic()
    done = 0
    slot = 0
    while done < total:
        wave = min(width, total - done)
        out = {}
        threads = [threading.Thread(target=fire, args=(prompts[(done + i) % len(prompts)], out, i))
                   for i in range(wave)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(880)
        done += wave
        slot += wave
    elapsed = time.monotonic() - started
    peak, hist = sampler.finish()
    return elapsed, peak, hist, out


def main():
    prompts = [make_prompt(i) for i in range(4)]
    print("提示词长度 %d 字符，max_tokens=%d，请求数=%d" % (len(prompts[0]), MAX_TOKENS, REQUESTS))
    rows = []
    for width in (1, 2):
        print("\n--- N=%d 开始 ---" % width, flush=True)
        elapsed, peak, hist, last = run(width, REQUESTS, prompts)
        usage = last.get(0, {}).get("usage") or {}
        print("    用时 %.1fs  vLLM峰值=%d  分布=%s" % (elapsed, peak, hist))
        print("    最后一次请求: %s" % json.dumps(last.get(0, {}), ensure_ascii=False))
        rows.append({"width": width, "seconds": round(elapsed, 1), "peak": peak,
                     "hist": hist, "usage": usage})
    print("\n=== 汇总 ===")
    base = rows[0]["seconds"]
    for row in rows:
        print("    N=%d  总用时 %.1fs  单请求平均 %.1fs  vLLM峰值=%d  加速 %.2fx"
              % (row["width"], row["seconds"], row["seconds"] / REQUESTS, row["peak"],
                 base / row["seconds"]))
    print("    用量: %s" % json.dumps(rows[-1]["usage"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
