#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""对比「改造前 / 改造后」闸门每完成一次模型调用要付多少固定开销。

闸门每放行一次模型调用要写三行：取号 INSERT、置 active 的 UPDATE、
释放 DELETE。改造前每次都是一条新连接（开库+写事务+关库，状态盘是机械盘），
改造后按线程复用连接。这里直接在服务器上把两版代码各跑一遍。

用法： .venv/bin/python bench_gate_overhead.py
"""
import importlib.util
import io
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
OLD = os.path.join(ROOT, ".backup-concurrency-20260914-223309", "services", "model_dispatch.py")
STATE = "/home/zhoulingjie/.local/state/sjfx-gate-bench"
shutil.rmtree(STATE, ignore_errors=True)
os.makedirs(STATE, exist_ok=True)
os.environ["SJFX_STATE_DIR"] = STATE
sys.path.insert(0, ROOT)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bench(module, label, n=15):
    endpoint = "http://bench-%s" % label
    # 预热（建表）
    with module.generation_slot(endpoint, timeout=30):
        pass
    started = time.monotonic()
    for _ in range(n):
        with module.generation_slot(endpoint, timeout=30):
            pass
    each = (time.monotonic() - started) / n
    print("  %-28s %8.1f ms / 次模型调用" % (label, each * 1000))
    return each


def main():
    print("状态目录（与生产同一个盘 sdb1）: %s" % STATE)
    old = load("old_model_dispatch", OLD)
    old_each = bench(old, "改造前（每次新建连接）")
    old.__dict__.clear()

    sys.path.insert(0, ROOT)
    import services.model_dispatch as new  # noqa: E402
    importlib.reload(new)
    new_each = bench(new, "改造后（按线程复用连接）")

    print()
    print("  每次模型调用省下: %.0f ms" % ((old_each - new_each) * 1000))
    print("  降低倍数        : %.0fx" % (old_each / new_each if new_each else 0))
    for calls in (1000, 3000, 6000):
        save = (old_each - new_each) * calls
        print("  按 %5d 次模型调用算，整条导入省: %5.1f 分钟" % (calls, save / 60))
    shutil.rmtree(STATE, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
