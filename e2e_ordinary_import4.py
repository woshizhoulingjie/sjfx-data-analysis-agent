#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""普通文件导入端到端验证 v4（按真实接口契约：目录盘点 -> 显式选择 -> 解析 -> 初步摘要）。

关键事实：目录扫描是「只盘点、不解析」（app.py 5343 行注释写明），
`waiting_for_selection` 时默认选择为空，必须由前端/调用方显式把清单里的
文件路径提交给 /selection/confirm。v4 就是这么做的。

同时以 0.5s 采样 vLLM /metrics 的 vllm:num_requests_running，
证明并发是否真的发生。

用法： .venv/bin/python e2e_ordinary_import4.py [文件数]
"""
import io
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:18000"
STATE = "/home/zhoulingjie/.local/state/sjfx-data-analysis-agent"
COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 12
POLL = 4.0
ROOT = "/home/nas511/zhoulingjie/sjfx-e2e-%d" % int(time.time())

STOP_AFTER = {"preliminary_nodes", "preliminary_overview", "deep_summarizing_files",
              "deep_summarizing_nodes", "deep_overview_updating", "completed", "partial"}

SAMPLE = """\
第 {index} 号材料：2026 年第 {quarter} 季度经营情况说明

一、总体情况
本季度实现营业收入 {revenue} 万元，同比增长 {growth}%，毛利率 {margin}%，
较上年同期上升 {delta} 个百分点。收入增长主要来自华东与华南两个区域的存量客户
复购，以及新产品线的小批量交付。

二、主要风险与不确定性
截至报告期末，应收账款余额 {receivable} 万元，其中账龄超过一年的部分占 {aging}%。
公司尚未完成对上述应收账款的减值测试，审计机构亦未出具意见。此外，报告期内发生的
关联方交易金额为 {related} 万元，定价依据为成本加成，尚待第三方评估确认。

三、下一步安排
计划在下季度完成应收账款的专项核查，并在 {deadline} 前提交补充材料。
本说明中的全部数据均来自内部台账，未经外部审验，仅供内部讨论使用。
"""


def token():
    with io.open(os.path.join(STATE, ".env"), encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("SJFX_API_ACCESS_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("未找到 SJFX_API_ACCESS_TOKEN")


TOKEN = token()


def api(method, path, payload=None, timeout=120):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"raw": body[:400]}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": repr(exc)}


class Metrics:
    def __init__(self, url="http://127.0.0.1:8001/metrics"):
        self.url = url
        self.stop = threading.Event()
        self.peak = 0
        self.histogram = {}
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.thread.start()

    def _loop(self):
        pattern = re.compile(r"^vllm:num_requests_running\S*\s+([0-9.eE+-]+)$")
        while not self.stop.is_set():
            running = 0
            try:
                with urllib.request.urlopen(self.url, timeout=5) as response:
                    for line in response.read().decode("utf-8", "replace").splitlines():
                        match = pattern.match(line.strip())
                        if match:
                            running = max(running, float(match.group(1)))
            except Exception:  # noqa: BLE001
                pass
            bucket = int(running)
            self.histogram[bucket] = self.histogram.get(bucket, 0) + 1
            self.peak = max(self.peak, bucket)
            self.stop.wait(0.5)

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=5)
        return self.peak, dict(sorted(self.histogram.items()))


def make_files():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT)
    for index in range(COUNT):
        body = SAMPLE.format(
            index=index + 1, quarter=(index % 4) + 1,
            revenue=1200 + index * 37, growth=5 + index % 9,
            margin=18 + index % 7, delta=index % 5,
            receivable=600 + index * 11, aging=8 + index % 13,
            related=90 + index * 3, deadline="2026 年 10 月 31 日",
        )
        with io.open(os.path.join(ROOT, "材料%03d.txt" % (index + 1)), "w", encoding="utf-8") as handle:
            handle.write(body)
    return sorted(os.listdir(ROOT))


def task_of(scan_id):
    _code, body = api("GET", "/api/import-tasks/%s" % scan_id)
    return (body or {}).get("import_task") or {}


def inventory_paths(scan_id):
    paths = []
    offset = 0
    while True:
        _code, body = api("GET", "/api/scan/%s/files?offset=%d&limit=100" % (scan_id, offset))
        items = (body or {}).get("items") or (body or {}).get("files") or []
        if not items:
            break
        for item in items:
            path = str(item.get("node_path") or item.get("path") or "")
            if path:
                paths.append(path)
        offset += len(items)
        if len(items) < 100 or offset > 5000:
            break
    return paths


def wait_status(scan_id, wanted, deadline, timeline, label):
    last = None
    while time.monotonic() < deadline:
        task = task_of(scan_id)
        status_name = str(task.get("status") or "")
        if status_name and status_name != last:
            timeline.append((round(time.monotonic(), 2), status_name,
                             str(task.get("current_phase") or "")))
            print("    [%s] %s  previewed=%s failed=%s" % (
                label, status_name, task.get("previewed_files"), task.get("failed_files")), flush=True)
            last = status_name
        if status_name in {"failed", "cancelled", "partial"}:
            return status_name, task
        if status_name in wanted:
            return status_name, task
        time.sleep(POLL)
    return last, {}


def main():
    print("=== 准备测试数据 ===")
    names = make_files()
    print("    目录 %s，%d 个文件" % (ROOT, len(names)))

    print("=== 1) POST /api/scan ===")
    code, body = api("POST", "/api/scan", {"path": ROOT})
    scan_id = (body or {}).get("job_id") or (body or {}).get("analysis_job_id")
    print("    http=%s scan_id=%s" % (code, scan_id))
    if not scan_id:
        raise SystemExit("没有拿到 scan_id")

    timeline = [(round(time.monotonic(), 2), "submitted", "")]
    print("=== 2) 等目录盘点完成（只盘点不解析）===")
    status_name, task = wait_status(scan_id, {"waiting_for_selection", "inventory_ready"},
                                    time.monotonic() + 900, timeline, "scan")
    print("    -> %s (total_files=%s)" % (status_name, task.get("total_files")))
    if status_name in {"failed", "cancelled"} or not status_name:
        raise SystemExit("盘点失败")

    print("=== 3) 从清单里取文件路径（目录扫描不会自动选）===")
    paths = [p for p in inventory_paths(scan_id) if p.lower().endswith(".txt")]
    print("    清单内 .txt = %d" % len(paths))
    if not paths:
        raise SystemExit("清单里没有可解析文件")

    print("=== 4) 提交选择 -> parsing_selected ===")
    metrics = Metrics()
    metrics.start()
    start = time.monotonic()
    code, body = api("POST", "/api/scan/%s/selection/confirm" % scan_id,
                     {"included_paths": paths, "excluded_paths": []})
    print("    http=%s accepted=%s selected=%s job=%s" % (
        code, (body or {}).get("accepted"), (body or {}).get("selected_count"),
        (body or {}).get("job_id")))
    if code not in (200, 202):
        metrics.finish()
        raise SystemExit("确认失败: %s" % json.dumps(body, ensure_ascii=False)[:400])

    print("=== 5) 跑完解析 + 初步摘要 ===")
    status_name, task = wait_status(scan_id, STOP_AFTER, time.monotonic() + 2700,
                                    timeline, "import")
    elapsed = time.monotonic() - start
    time.sleep(3)
    peak, histogram = metrics.finish()

    checkpoint = (task or {}).get("checkpoint") or {}
    counts = (task or {}).get("counts") or {}
    print()
    print("=== 结果 ===")
    print("    最终状态        : %s" % status_name)
    print("    确认->初步完成  : %.1fs（%d 个文件，%.2fs/文件）"
          % (elapsed, len(paths), elapsed / max(1, len(paths))))
    print("    vLLM 并发峰值   : %d 路" % peak)
    print("    并发分布(路:采样): %s" % histogram)
    print("    初步摘要任务数  : %s（已完成 %s）" % (
        checkpoint.get("preliminary_summary_expected"),
        checkpoint.get("preliminary_summary_completed")))
    print("    计数            : %s" % json.dumps(counts, ensure_ascii=False))
    print("    scan_id         : %s" % scan_id)
    print("    测试目录        : %s" % ROOT)
    print("    状态时间线      :")
    base = timeline[0][0]
    for stamp, name, phase in timeline:
        print("        +%7.1fs  %-26s %s" % (stamp - base, name, phase))
    return 0


if __name__ == "__main__":
    sys.exit(main())
