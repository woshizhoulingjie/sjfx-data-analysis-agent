# -*- coding: utf-8 -*-
"""大包 QA 索引的「按需 / 懒加载」需求判定与节流决策。

背景（实测）：`scripts/large_qa_worker.py` 原实现遍历**全部**大包，并且每 120 秒
对每个包重读一遍 `files`（含 384 维向量）做摘要同步——`changed=0` 也照读。结果
1.5 天读掉 **337 GB** 磁盘、覆盖率只有 **7.0%**（8089/115063），还把唯一一个后台
模型槽和磁盘从命名、导入手里抢走。

本模块把「要不要干活」变成可判定的纯逻辑，便于单测：

  需求信号（任一即算有需求）：
    1. 最近用过的包：`conversations.scan_id` 为 `lp-…` 且 `updated_at` 在 TTL 内；
    2. 被提问点名过的包：该包索引里有 `priority>0 AND parse_status='pending'` 的文件
       ——`services/large_qa_search.py` 在问答时 `enqueue()` 过，这是最直接的按需信号；
    3. 人工钉住：`<base>/demand.txt` 一行一个包 id（临时补索引用）。

  节流决策：
    · `should_sync()`：包指纹没变且未到兜底时间 → **跳过**这次全量重读；
    · 每轮抽取数量与文件间隔由配置限流（见 `extract_budget()` / `extract_pause()`）。

开关 `LARGE_QA_DEMAND_ONLY=0` 可完全回到旧的全量行为。
"""
from __future__ import annotations

import os
import sqlite3
import time

from config import Config

LOGGER_NAME = "sjfx.large_qa_demand"

DEFAULTS = {
    "LARGE_QA_DEMAND_ONLY": 1,
    "LARGE_QA_DEMAND_TTL": 604800.0,        # 7 天
    "LARGE_QA_SYNC_INTERVAL": 120.0,
    "LARGE_QA_SYNC_FORCE_AFTER": 1800.0,    # 没变化也至少每 30 分钟兜底同步一次
    "LARGE_QA_BUDGET_FILES_PER_ROUND": 8,
    "LARGE_QA_EXTRACT_PAUSE": 1.5,
    "LARGE_QA_IDLE_SLEEP": 60.0,
}


def _number(name, default):
    value = getattr(Config, name, None)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def demand_only():
    """是否启用按需模式（默认启用；设 0 回到旧的全量行为）。"""
    value = getattr(Config, "LARGE_QA_DEMAND_ONLY", None)
    if value is None:
        return bool(DEFAULTS["LARGE_QA_DEMAND_ONLY"])
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "off", "no", ""}
    return bool(value)


def demand_ttl():
    return _number("LARGE_QA_DEMAND_TTL", DEFAULTS["LARGE_QA_DEMAND_TTL"])


def sync_interval():
    return _number("LARGE_QA_SYNC_INTERVAL", DEFAULTS["LARGE_QA_SYNC_INTERVAL"])


def sync_force_after():
    return _number("LARGE_QA_SYNC_FORCE_AFTER", DEFAULTS["LARGE_QA_SYNC_FORCE_AFTER"])


def extract_budget():
    return max(1, int(_number("LARGE_QA_BUDGET_FILES_PER_ROUND",
                             DEFAULTS["LARGE_QA_BUDGET_FILES_PER_ROUND"])))


def extract_pause():
    return max(0.0, _number("LARGE_QA_EXTRACT_PAUSE", DEFAULTS["LARGE_QA_EXTRACT_PAUSE"]))


def idle_sleep():
    return max(1.0, _number("LARGE_QA_IDLE_SLEEP", DEFAULTS["LARGE_QA_IDLE_SLEEP"]))


def age_seconds(value, now=None):
    """`updated_at` 可能是 epoch（float/int）也可能是 ISO 文本，两者都要认。

    解析不出来返回 None（视为"很久以前"）。
    """
    now = time.time() if now is None else now
    if value is None:
        return None
    try:
        return now - float(value)
    except (TypeError, ValueError):
        pass
    text = str(value).strip().replace("T", " ").split(".")[0][:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return now - time.mktime(time.strptime(text, fmt))
        except ValueError:
            continue
    return None


def pinned_packages(base):
    """`<base>/demand.txt`：一行一个包 id，`#` 开头为注释。"""
    path = os.path.join(str(base), "demand.txt")
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return {line.strip() for line in fh
                    if line.strip() and not line.strip().startswith("#")}
    except OSError:
        return set()


def recent_packages(agent_db, ttl=None, now=None):
    """最近 TTL 内被对话过的大包（`conversations.scan_id LIKE 'lp-%'`）。"""
    ttl = demand_ttl() if ttl is None else ttl
    now = time.time() if now is None else now
    if not agent_db or not os.path.exists(agent_db):
        return set()
    out = set()
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % agent_db, uri=True, timeout=10)
        try:
            for scan_id, updated in conn.execute(
                    "SELECT scan_id, updated_at FROM conversations WHERE scan_id LIKE 'lp-%'"):
                age = age_seconds(updated, now)
                if age is None or age <= ttl:
                    out.add(str(scan_id))
        finally:
            conn.close()
    except sqlite3.Error:
        return out
    return out


def enqueued_packages(base, package_ids):
    """索引里还有"被提问点名、但原文没抽"的包 —— 按需信号。"""
    out = set()
    for package_id in package_ids:
        path = os.path.join(str(base), str(package_id), "index.db")
        if not os.path.exists(path):
            continue
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=10)
            try:
                row = conn.execute("SELECT 1 FROM documents WHERE parse_status='pending' "
                                   "AND priority>0 LIMIT 1").fetchone()
            finally:
                conn.close()
            if row:
                out.add(str(package_id))
        except sqlite3.Error:
            continue
    return out


def demanded(agent_db, base, package_ids, now=None):
    """返回 {package_id: 原因}，只包含**确实存在**的包。"""
    known = {str(value) for value in package_ids}
    result = {}
    for package_id in sorted(pinned_packages(base) & known):
        result[package_id] = "pinned"
    for package_id in sorted(recent_packages(agent_db, now=now) & known):
        result.setdefault(package_id, "recent_conversation")
    for package_id in sorted(enqueued_packages(base, known)):
        result.setdefault(package_id, "question_enqueued")
    return result


def package_stamp(package):
    """包指纹：只要它变了就必须重新同步（O(1)，不读 files）。"""
    if not package:
        return ""
    parts = []
    for key in ("updated_at", "progress", "counts", "phase", "status", "content_revision"):
        value = package.get(key) if isinstance(package, dict) else None
        parts.append("%s=%s" % (key, value))
    return "|".join(parts)


def should_sync(now, last_sync_at, last_stamp, stamp, interval=None, force_after=None):
    """要不要做这次全量摘要同步。

    · 从未同步过 → 要；
    · 包指纹变了 → 要（但不超过 interval 的频率）；
    · 指纹没变 → 只在超过 force_after 时兜底同步一次。
    """
    interval = sync_interval() if interval is None else interval
    force_after = sync_force_after() if force_after is None else force_after
    if last_sync_at is None:
        return True
    elapsed = now - last_sync_at
    if last_stamp and stamp and last_stamp == stamp:
        return elapsed >= force_after
    return elapsed >= interval
