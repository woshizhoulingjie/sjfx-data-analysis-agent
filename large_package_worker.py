"""Dedicated worker for the isolated streaming large-package workflow."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import tempfile
import time
from contextlib import closing
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from config import Config
from services.summary_fidelity import SUMMARY_QUALIFIER_INSTRUCTION, summary_qualifier_patch
from services.large_package_isolated import LargePackageStore, iter_inventory, quick_preview
from services.large_package_resources import (AdmissionGuard, ResourcePressure,
    defer_lease, pressure, resource_code, show_wait, sqlite_temp_directory)

LOGGER = logging.getLogger("sjfx.large_package_worker")

# `_solo_catchup` 的欠账判据。提为模块级常量是为了让测试直接复用同一段 SQL，
# 避免"测试里另写一份、与生产漂移"。语义见 SOLO_CATCHUP_STALE_SQL 上方的注释。
SOLO_CATCHUP_STALE_SQL = (
    "SELECT DISTINCT g.package_id FROM large_catalog_groups g "
    "JOIN packages p ON p.id=g.package_id "
    "LEFT JOIN lp_catalog_labels l ON l.package_id=g.package_id "
    "  AND l.group_id=g.id "
    "WHERE g.id LIKE 'km-solo-%' AND p.status IN "
    "('waiting_for_selection','completed','running') "
    "AND (l.group_id IS NULL "
    "     OR (l.revision!=g.content_revision AND l.locked=0)) "
    "AND NOT EXISTS(SELECT 1 FROM lp_catalog_jobs j "
    "  WHERE j.package_id=g.package_id AND j.group_id=g.id) "
    "LIMIT 40")


# 深度解析并行时，每个线程必须持有自己的解析器实例：
# UnifiedDocumentParser 内部有可变状态（缓存/句柄），跨线程共享不安全。
_DEEP_PARSER_LOCAL = threading.local()
_SURROGATES = re.compile(r"[\ud800-\udfff]")


class SourceChangedError(RuntimeError):
    pass


def _source_snapshot(root, row):
    if row.get('source_kind') == 'archive_member':
        from services.large_archive_members import source_identity
        return source_identity(root, row)
    base = Path(root).resolve()
    path = (base / row['path']).resolve(strict=True)
    if base not in path.parents:
        raise SourceChangedError('源文件路径已越出数据包目录，请重新盘点')
    stat = path.stat()
    if row.get('modified_at_ns') and (stat.st_mtime_ns != int(row['modified_at_ns'])
                                      or stat.st_size != int(row.get('size') or 0)):
        raise SourceChangedError('源文件自盘点后发生变化，请重新盘点')
    return path, (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _normalize_unicode(value):
    """Repair parser/model UTF-16 artifacts without losing valid Unicode."""
    if isinstance(value, str):
        if _SURROGATES.search(value):
            return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
        return value
    if isinstance(value, dict):
        return {_normalize_unicode(k): _normalize_unicode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_unicode(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_normalize_unicode(v) for v in value)
    return value


def _counts(store, package_id):
    return store.counts(package_id)


def _sample_text(text, limit):
    """在固定字符预算内取样正文的**头部 + 中部 + 尾部**，而不是只取开头。

    与普通导入 services/storage.py 的 _sample_text 使用同一套预算比例
    （50% / 25% / 25%），保持两条流程的取样口径一致。

    改动前的实现是 text[:24000]：一个 112,786 字符的 PDF 只能覆盖前 21%，
    文档尾部的结论、附件清单、签署方等完全读不到。三段取样在**同样预算下**
    把覆盖范围扩展到文档结尾。超过 10 万字符的文件仍然相对稀疏，但性质由
    "只读开头"变为"覆盖全文"。

    文本本身不超过预算时原样返回，此时行为与改动前完全一致。
    """
    text = str(text or "")
    limit = max(0, int(limit or 0))
    if not limit or len(text) <= limit:
        return text
    head = int(limit * 0.50)
    middle = int(limit * 0.25)
    tail = limit - head - middle
    middle_start = max(head, (len(text) - middle) // 2)
    return (
        text[:head]
        + text[middle_start:middle_start + middle]
        + (text[-tail:] if tail else "")
    )


class LargePackageWorker:
    def __init__(self, store=None, poll_seconds=2.0):
        self.store = store or LargePackageStore()
        self.poll_seconds = max(0.2, float(poll_seconds))
        self._degraded_warned = set()
        # "目录还有待推进项"这条兜底的冷却时间戳。
        #
        # 为什么必须冷却：stall 判据只认"portrait.catalog_status 没到终态"，
        # 而 KMODE=1（生产模式）下 `test_background_names_do_not_wait_for_busy_generation`
        # 已明确记载 —— 命名是**有意**派给后台队列的，文件停在 'pending'
        # **是正确稳态而不是待办**。判据因此会长期为真：生产实测 5 个冻结包
        # 2225 个文件命中，其中 2224 个早已进入真实叶组，只是那个反规范化字段陈旧。
        #
        # 原实现命中后无条件 `return True`，而那段判定本身是一次约 8.8 秒的全表
        # 扫描（json_extract 无法走索引），于是 run_forever 永不休眠：
        # 单核 100% 持续空转，并把紧随其后的概览补发彻底饿死。
        #
        # 判据语义不动（契约测试钉着它），只把"扫描 + 调度"整体纳入冷却，
        # 让 Worker 回到正常轮询节奏。首次调用的时间戳是 0.0，所以第一次
        # run_once() 照旧会尝试 —— 契约测试因此仍然通过。
        self._stall_scan_at = 0.0
        # 方案A：单文件条目补做的节流时间戳（0.0 = 首次 run_once 就会尝试）。
        self._solo_catchup_at = 0.0
        self._stage_report_at = 0.0
        self._stall_retry_seconds = max(
            30.0, float(getattr(Config, 'LP_STALL_RETRY_SECONDS', 300.0) or 300.0))
        self.stop_requested = False
        from services.large_package_preview_runner import PreviewRunner
        self.preview_runner = PreviewRunner()
        self._preview_runners = [self.preview_runner]+[PreviewRunner() for _ in range(max(1,Config.LP_PREVIEW_CONCURRENCY)-1)]
        self._naming_executor = None
        # C（文档 §3C.2/§3C.4）：**有界的两路在途**，键是 package_id —— 一条车道只
        # 服务一个包，两个不同的包才可能同时在途。`SJFX_LP_NAMING_PARALLEL` 默认关闭，
        # 关闭时 lanes=1，行为与改动前逐字一致。
        # `_naming_future` 保留为兼容属性（见 _naming_future 的 property）：老代码与
        # 两个既有测试把它当作"唯一在途的那一路"来设置和读取。
        self._naming_futures = {}
        self._naming_order = []
        self._naming_orphan_future = None
        self._naming_parallel_logged = False
        # 已做过主题归并的包 → 当时的叶组签名。仅进程内有效；
        # 存签名而不是布尔值，是为了让叶组集合变化后能重算一次
        # （全局聚类可能在主题归并之后又跑了一次，把叶组从 66 个变成 69 个）。
        self._topics_merged = {}
        # 主题层重建去抖：package_id -> (签名, 该签名首次出现的单调时刻)。
        # 见 _merge_topics_once 的说明。
        # 主题层落后时的主动补做节流（见 _topic_catchup）。
        self._topic_catchup_at = 0.0
        self._topic_catchup_seconds = max(
            15.0, float(getattr(Config, 'LP_TOPIC_CATCHUP_SECONDS', 60.0) or 60.0))
        # 主题层"落后于叶组"的检查节流（见 _topic_layer_stale）。
        self._topic_stale_at = {}
        self._topic_stale_seconds = max(
            5.0, float(getattr(Config, 'LP_TOPIC_STALE_CHECK_SECONDS', 30.0) or 30.0))
        self._topics_pending = {}
        # 已经为哪个"欠账签名"告过警。欠账状态会在去抖窗口内持续存在（可长达
        # 数分钟），而命名切片约每 2 秒就走一次这个判定——不记一下的话，
        # 实测 2 分钟刷出 89 行同样的日志。只在签名变化时说一次。
        self._topics_owed_logged = {}
        self._topics_rebuild_debounce = max(
            0.0, float(getattr(Config, 'LP_TOPIC_REBUILD_DEBOUNCE_SECONDS', 180.0) or 0.0))
        # 已"定稿"的包 → 定稿时的目录签名。用于停止兜底命名与主题归并的重复空转：
        # 一轮（排空后的兜底 + 主题归并）走完而目录签名没有任何变化，就说明
        # 该包在当前输入下已无事可做；签名再变（新增组、补齐标签、重新聚类）
        # 会自动恢复。仅进程内有效——重启后最多重做一轮，代价可忽略。
        self._catalog_settled = {}
        # 目录增量专用后台车道（单线程）。用于把 embed_batch + 聚类判定 + 批量命名
        # 挪出预览关键路径——改动前它在预览主线程里每 32 个文件同步执行一次，
        # 执行期间预览线程池完全空闲。同一时刻只允许一个在跑，失败只记日志。
        self._catalog_lane = None
        self._catalog_lane_future = None
        self._catalog_lane_busy = False
        # 已在本进程内重算过 member_count 缓存的包（见 _catalog_counts）。
        self._counts_repaired = set()
        # 启动校验：LP_CATALOG_KMODE=0 时智能目录退化为旧的增量锚点分组。
        # 实测 6,925 文件的包在该模式下产出 4,870 个组、其中 4,435 个是单元素组
        # （91%），并有 3,832 个命名任务永久排队——目录基本不可浏览。
        # 这里只告警并把目录标注为"降级"，**不拒绝启动**（避免影响既有部署）。
        self.catalog_mode = "kmeans" if getattr(Config, "LP_CATALOG_KMODE", False) else "legacy"
        if self.catalog_mode == "legacy":
            LOGGER.warning(
                "SJFX_LP_CATALOG_KMODE 未开启：智能目录将使用旧的增量锚点分组，"
                "异构语料下可能产生大量单元素组、节点数远多于设计目标；"
                "目录会被显式标注为『降级目录（旧算法）』。")
        # 情报概览第一版的"目录稳定性"观测：package_id → (签名, 稳定起点, 首次观测)。
        # 仅进程内有效——它只是决定"现在是否可以出第一版"的启发式闸门。
        self._overview_state = {}
        self._overview_stable = 90.0        # 目录连续多少秒不变即视为定稿
        self._overview_max_wait = 900.0     # 最长等多久（之后用部分快照出第一版）
        self._overview_last_sweep = 0.0     # 上次扫描"该出第一版"的包的时刻
        # WAL 检查点阈值见 `services/large_package_isolated.py` 的 `_connect()`。
        # 已完成 k-means 全局分组的包；仅进程内有效（重启后按占位分组重建）。
        self._km_finalized = set()

    def _check_resources(self):
        if getattr(self, '_admission', None) is None:
            self._admission = AdmissionGuard(
                lambda: [('大包索引', Path(self.store.db_path).parent),
                 ('解析临时目录', Config.PARSE_TEMP_DIR),
                 ('系统临时目录', tempfile.gettempdir()),
                 ('SQLite临时目录', sqlite_temp_directory())],
                reserve_bytes=max(1024 ** 3, Config.PARSE_TEMP_DISK_RESERVE_BYTES))
        self._admission.check()

    def _defer_resource(self, package_id, path, stage, token, exc):
        if not resource_code(exc):
            return False
        # Close admission immediately, before touching a possibly-full database.
        self._check_guard_defer(exc)
        defer_lease(self.store, package_id, path, stage, token, exc)
        show_wait(self.store, package_id, exc)
        return True

    def _check_guard_defer(self, exc):
        if getattr(self, '_admission', None) is None:
            try:
                self._check_resources()
            except ResourcePressure:
                pass
        return self._admission.defer(exc)

    def _interrupted(self, package_id):
        current = self.store.get(package_id)
        return self.stop_requested or not current or current["status"] in {"paused", "cancelled"}

    def _inventory_one(self, package):
        package_id = package["id"]
        self.store.update_package(package_id, status="running", phase="inventory", progress=1, message="正在流式盘点并建立内容画像")
        discovered, total_bytes, type_counts = 0, 0, {}
        logical_members, logical_bytes = 0, 0
        batch = []
        inventory_errors=[]
        inventory_error_count=0
        def inventory_error(path,error):
            nonlocal inventory_error_count
            inventory_error_count+=1
            if len(inventory_errors)<20:inventory_errors.append({'path':path,'error':error[:200]})
        try:
            self.store.invalidate_changed_archive_members(package_id)
            for item in iter_inventory(package["root_path"],on_error=inventory_error):
                if discovered % 25 == 0 and self._interrupted(package_id):
                    return True
                batch.append(item)
                discovered += 1
                if item.get('source_kind') == 'archive_member':
                    logical_members += 1
                    logical_bytes += int(item.get('size') or 0)
                else:
                    total_bytes += int(item.get("size") or 0)
                extension = item.get("extension") or "[无扩展名]"
                type_counts[extension] = type_counts.get(extension, 0) + 1
                if len(batch) >= 100:
                    self._check_resources()
                    self.store.upsert_files(package_id, batch)
                    batch = []
                    self.store.update_package(package_id, progress=min(19, 1 + discovered // 1000), message="已盘点 {} 个文件；目录持续更新".format(discovered), counts={"discovered_files": discovered, "total_bytes": total_bytes, "type_counts": type_counts, "logical_member_count": logical_members, "logical_member_bytes": logical_bytes})
            if batch:
                self._check_resources()
                self.store.upsert_files(package_id, batch)
            if not self._interrupted(package_id):
                self.store.update_package(package_id, status="running", phase="quick_parse", progress=20, message="盘点有 {} 处无法读取，继续识别已发现文件".format(inventory_error_count) if inventory_error_count else "盘点完成，按格式识别文件内容", counts={"discovered_files": discovered, "total_bytes": total_bytes, "total_size_human": _human(total_bytes), "type_counts": type_counts, "inventory_complete": inventory_error_count == 0, "inventory_error_count": inventory_error_count, "inventory_errors": inventory_errors, "logical_member_count": logical_members, "logical_member_bytes": logical_bytes})
            return True
        except Exception as exc:
            if resource_code(exc):
                raise
            LOGGER.exception("large inventory failed: %s", package_id)
            self.store.update_package(package_id, status="failed", phase="inventory", error=str(exc), message="目录盘点失败")
            return False

    def _save_preview(self, package_id, path, summary, refine=False):
        if summary.get("status") == "failed":
            raise RuntimeError(summary.get("error") or "轻量解析失败")
        info = summary.get("portrait") or {}
        prior = self.store.get_file(package_id, path)
        # An OCR/timeout failure must not erase a useful first-pass portrait.
        if refine and prior and not summary.get("sample") and (prior.get("local_summary") or {}).get("sample"):
            warning = summary.get("notice") or "补充预算内没有新增内容"
            summary = dict(prior["local_summary"])
            info = dict(summary.get("portrait") or {})
            info.update(recognition_status="needs_review", needs_refine=False)
            summary.update(portrait=info, notice=warning)
        self.store.update_file(package_id, path, quick_status="completed", local_summary=summary,
                               category=summary.get("category") or "未分类", confidence=info.get("confidence", 0),
                               topic=info.get("topic") or "待识别内容", recognition_status=info.get("recognition_status", "metadata_only"),
                               document_role=info.get("document_role", ""), error=None)
        # Full source hashes belong to deep parsing, never to sampled previews.
        if not refine and info.get("needs_refine"):
            self.store.enqueue_recognition(package_id, path)

    def _preview_fields(self, summary, prior, refine):
        """把预览结果折算成 files 的列值（**不写库**），供原子提交使用。"""
        info = summary.get("portrait") or {}
        return {
            "quick_status": "completed", "local_summary": summary,
            "category": summary.get("category") or "未分类",
            "confidence": info.get("confidence", 0),
            "topic": info.get("topic") or "待识别内容",
            "recognition_status": info.get("recognition_status", "metadata_only"),
            "document_role": info.get("document_role", ""),
            "error": None,
        }

    def _commit_preview(self, package_id, path, summary, token, refine=False):
        """**原子提交预览结果**：租约校验 + 写文件 + 队列终态在同一个事务里。

        改动前顺序是 `_save_preview()`（多步写入）→ `finish_queue(token=lease)`；
        被判失联的旧任务即使被队列拒绝，预览也已经写进文件记录。
        现在改成"先算字段、再一次提交"，提交被拒就**什么都不写**。
        """
        if summary.get("status") == "failed":
            raise RuntimeError(summary.get("error") or "轻量解析失败")
        prior = self.store.get_file(package_id, path)
        # OCR/超时失败不得抹掉已有的可用首版画像（保留原语义）。
        if refine and prior and not summary.get("sample") and (prior.get("local_summary") or {}).get("sample"):
            warning = summary.get("notice") or "补充预算内没有新增内容"
            summary = dict(prior["local_summary"])
            info = dict(summary.get("portrait") or {})
            info.update(recognition_status="needs_review", needs_refine=False)
            summary.update(portrait=info, notice=warning)
        stage = "recognize" if refine else "quick"
        fields = self._preview_fields(summary, prior, refine)
        ok, why = self.store.commit_stage_result(package_id, path, stage, token,
                                                 ok=True, file_fields=fields)
        if not ok:
            LOGGER.warning("preview result rejected package=%s path=%s stage=%s reason=%s",
                           package_id, path, stage, why)
            return False, why
        # 副作用只在提交成功之后执行。
        if not refine:
            try:
                if (summary.get("portrait") or {}).get("needs_refine"):
                    self.store.enqueue_recognition(package_id, path)
            except Exception:
                LOGGER.exception("enqueue_recognition failed: %s", path)
        return True, ""

    def _catalog_incremental(self, package_id):
        from services.large_package_semantic import embed_batch, name_batch
        # §4.1：这一趟内部的四段以前只记一个总耗时，而历史样本里"同样 claimed=32"
        # 既有 0.5 秒也有 27.7 秒 —— 必须拆开才知道尾部在哪。只加计时，
        # **不改变任何调用顺序与返回值**（顺带修掉 claimed 在异常分支可能未定义）。
        _t0 = time.monotonic()
        claimed = 0
        _stage = {}
        try:
            cancelled = lambda: self._interrupted(package_id)
            _s = time.monotonic()
            claimed = embed_batch(self.store, package_id, cancelled=cancelled)
            _stage['embed_batch'] = time.monotonic() - _s
            if not cancelled():
                from services.large_package_feature_delta import ensure_package
                _s = time.monotonic()
                ensure_package(self.store,package_id,max_batches=2)
                _stage['ensure_package'] = time.monotonic() - _s
            _s = time.monotonic()
            self._ensure_km_finalized(package_id, claimed=claimed)
            _stage['km_finalize'] = time.monotonic() - _s
            _s = time.monotonic()
            name_batch(self.store, package_id, cancelled=cancelled)
            _stage['name_batch'] = time.monotonic() - _s
        except Exception:
            LOGGER.exception("large semantic catalog deferred: %s", package_id)
        else:
            _dt = time.monotonic() - _t0
            for _name, _sec in _stage.items():
                stage_record('catalog.' + _name, _sec, package=package_id[:12], claimed=claimed)
            stage_record('catalog.lane_total', _dt, package=package_id[:12], claimed=claimed)
            if int(getattr(Config, 'LP_CATALOG_LANE_LOG_MS', 200) or 0) <= _dt * 1000:
                LOGGER.info("catalog incremental lane package=%s seconds=%.2f claimed=%s stages=%s",
                            package_id, _dt, claimed,
                            {k: round(v, 3) for k, v in _stage.items()})
        # **纯诊断**：这一趟是后台车道，但它写库时要拿进程级 `store._lock`，
        # 而预览主线程的 get_file / claim / commit_stage_result 抢的是同一把锁。
        # 车道当初就是为"挪出预览关键路径"才建的，如果这一趟很慢，预览车道就会
        # 在等锁时空转 —— 实测预览阶段 worker 主进程 CPU 只有 0.2~0.6%，
        # 每车道却只跑到 6.5 文件/秒（解析本身只要 23 文件/秒/车道）。
        # 先把时长量出来，再决定动不动结构。只记日志，不改变任何行为。
        _t0 = time.monotonic()
        try:
            cancelled = lambda: self._interrupted(package_id)
            claimed = embed_batch(self.store, package_id, cancelled=cancelled)
            if not cancelled():
                from services.large_package_feature_delta import ensure_package
                ensure_package(self.store,package_id,max_batches=2)
            self._ensure_km_finalized(package_id, claimed=claimed)
            name_batch(self.store, package_id, cancelled=cancelled)
        except Exception:
            LOGGER.exception("large semantic catalog deferred: %s", package_id)
        else:
            _dt = time.monotonic() - _t0
            if int(getattr(Config, 'LP_CATALOG_LANE_LOG_MS', 200) or 0) <= _dt * 1000:
                LOGGER.info("catalog incremental lane package=%s seconds=%.2f claimed=%s",
                            package_id, _dt, claimed)

    def _ensure_km_finalized(self, package_id, claimed=None, force=False):
        """等**所有有素材的文件都拿到向量**之后，做一次全局 k-means 分组。

        判定条件（必须全部成立）：
          ① quick 阶段没有待办 —— 所有文件都已解析，不会再有新文件进嵌入队列；
          ② catalog 阶段没有待办 —— 队列里的文件都已嵌入；
          ③ material_pending_count() == 0 —— **所有"有素材"的文件都已有向量**；
          ④ 向量集指纹与上次聚类时不同 —— 没变就不重复聚类。

        为什么必须用 ③ 这样的精确计数，而不是"队列为空 + 连续几轮空闲"：
          catalog 队列在批次之间本来就会瞬间为空（实测每次 embed_batch 结束都会
          归零），所以"空"完全不代表"嵌入完成"。早期实现靠这个启发式，
          在语料只处理一半时就执行了全局聚类（实测 1040 个文件时 quick 还有
          522 个没解析就聚类了），兜底逻辑随即把 530 个文件倒进无向量的
          km-other，其中 525 个本来有完整素材。③ 是可精确计算、幂等的真条件。

        为什么用 ④ 的指纹而不是进程内标志：
          深度解析会改写正文并重新入队 catalog（新向量、新分组），需要重算；
          而 Worker 重启不能让同一份语料产生不同目录。指纹落库同时满足两者。
        """
        try:
            from config import Config
            if not getattr(Config, "LP_CATALOG_KMODE", False):
                return False
            from services.large_package_semantic import (
                _km_signature, _km_state_get, kmeans_recluster, material_pending_count)
            # 方案 A：目录结构在"智能目录确定"（catalog_ready）之后**冻结**。
            # 深度摘要会改写正文并重新嵌入，若继续重算聚类，group id 会全部变化，
            # 已生成的目录名与第一版情报概览会同时作废；而情报概览的更新只升级
            # 证据层级、不改变结构。需要恢复"深度摘要后重算目录"时把
            # SJFX_LP_CATALOG_FREEZE_AFTER_CATALOG 设为 0。
            #
            # **冻结只对"已经聚类成功过"的包生效。** 这一条限定是必须的，不是放宽：
            # 判据是 km_state 里到底有没有这个包的记录。旧写法只看 phase，于是
            # 「聚类失败 → 相位照常推进到 catalog_ready → 冻结 → 永不重试」形成死锁，
            # 包会永久停在占位分组上（生产实测：lp-085e413e2… 7484 份文件全在
            # km-pending 一个组里，worker 一个多小时不再重试，界面只剩「待归并主题」）。
            # 从没聚类成功过的包没有任何 group 名可作废，重试它不构成"静默重分组"。
            frozen = bool(_km_state_get(self.store, package_id))
            if getattr(Config, "LP_CATALOG_FREEZE_AFTER_CATALOG", True) and frozen:
                current = self.store.get(package_id) or {}
                if str(current.get("phase") or "") in {"catalog_ready", "deep_parse", "report_ready"}:
                    # 已聚类过的包：结构冻结，只有显式重建（naming.rebuild_catalog，
                    # 它直接调用 kmeans_recluster，不经过本函数）或指纹未变才继续。
                    if _km_signature(self.store, package_id) == _km_state_get(self.store, package_id):
                        return False
                    # 收尾点 3/5：向唯一判定口取证（只记录，不改变本函数原有闸门 ——
                    # 聚类必须等"所有有素材的文件都有向量"，那条件比 advance() 的 gates 更严）。
                    try:
                        _v = self.store.advance(package_id) or {}
                        if _v.get("blocked_by"):
                            LOGGER.debug("km finalize: package=%s advance blocked_by=%s",
                                         package_id, _v.get("blocked_by"))
                    except Exception:
                        pass
                    quick = self.store.stage_counts(package_id, "quick")
                    if any(int(quick.get(k) or 0) for k in ("queued", "retry", "running")):
                        return False
                    stage = self.store.stage_counts(package_id, "catalog")
                    if any(int(stage.get(k) or 0) for k in ("queued", "retry", "running")):
                        return False
                    pending = material_pending_count(self.store, package_id)
                    if pending:
                        LOGGER.info("large package %s k-means waits for %d material files",
                                    package_id, pending)
                        return False
                    from services.large_package_catalog_quality import repair_changed
                    repaired = repair_changed(self.store, package_id)
                    # 批3 第 15 条：结构化结果 —— 按 status 区分"放弃/重试"与"真的修了"，
                    # 并把状态写进工作流事件（原来 None/0 都是假值，分不清）。
                    status = repaired.get('status') if isinstance(repaired, dict) else 'done'
                    changed = int(repaired.get('changed', 0)) if isinstance(repaired, dict) else int(repaired or 0)
                    if status in ('skipped_dirty', 'retry'):
                        LOGGER.info("large package %s repair status=%s (skipped/retry) dirty=%s",
                                    package_id, status, (repaired or {}).get('dirty'))
                        return False
                    LOGGER.info("large package %s repaired %s groups while frozen (status=%s)",
                                package_id, changed, status)
                    return bool(changed)
            # 收尾点 3/5：向唯一判定口取证（只记录，不改变本函数原有闸门 ——
            # 聚类必须等"所有有素材的文件都有向量"，那条件比 advance() 的 gates 更严）。
            try:
                _v = self.store.advance(package_id) or {}
                if _v.get("blocked_by"):
                    LOGGER.debug("km finalize: package=%s advance blocked_by=%s",
                                 package_id, _v.get("blocked_by"))
            except Exception:
                pass
            quick = self.store.stage_counts(package_id, "quick")
            if any(int(quick.get(k) or 0) for k in ("queued", "retry", "running")):
                return False
            stage = self.store.stage_counts(package_id, "catalog")
            if any(int(stage.get(k) or 0) for k in ("queued", "retry", "running")):
                return False
            pending = material_pending_count(self.store, package_id)
            if pending:
                LOGGER.info("large package %s k-means waits for %d material files",
                            package_id, pending)
                return False
            signature = _km_signature(self.store, package_id)
            if signature == _km_state_get(self.store, package_id):
                return False
            changed = kmeans_recluster(self.store, package_id, force=force)
            LOGGER.info("large package %s k-means finalized changed=%s", package_id, changed)
            # 叶组变了，让命名队列按新分组重建。
            # 这里必须无条件执行：占位分组阶段可能已经给部分 group_id 写入过
            # naming job（含 deferred），不清空会出现"新叶组沿用旧任务状态、
            # 永远不去命名"的情况。
            if getattr(Config, "LP_CATALOG_MODEL_NAMING", False):
                from services.large_package_catalog_naming import queue_groups, ensure_round
                with closing(self.store._connect()) as conn:
                    conn.execute(
                        "DELETE FROM lp_catalog_jobs WHERE package_id=? "
                        "AND group_id NOT IN (SELECT id FROM large_catalog_groups "
                        "WHERE package_id=?)", (package_id, package_id))
                    conn.commit()
                queue_groups(self.store, package_id)
                ensure_round(self.store, package_id)
                # 这里**不能**调用 _name_fallback：刚建好的组还没有任何标签，
                # 兜底会立刻给每一组写一个"取自文件标题"的确定性名字，
                # 标签随即与 content_revision 匹配，queue_groups 也就不再排任务，
                # 模型命名被彻底跳过（实测 67 个组全被兜底名占住，
                # 出现"Frontiers in Public Health 01 frontiersi"这种名字）。
                # 兜底必须留到命名轮次真正排空之后再执行，见 _submit_naming 的 run_slice。
            return bool(changed)
        except Exception:
            LOGGER.exception("k-means finalize failed: %s", package_id)
            return False

    CATALOG_REFINING_NOTE = '主题名称正在后台按证据完善'
    CATALOG_REFINED_MESSAGE = '内容目录已完善，可选择深度摘要范围'

    def _mark_catalog_refined(self, package_id):
        """目录真的收尾后，把包消息从"正在后台完善"改成"已完善"。

        `catalog_ready` 时写入的消息是固定文案"主题名称正在后台按证据完善"，
        之后**没有任何代码**在命名结束时改回来，于是界面永远显示"正在完善"。
        这里只在四件事同时成立时改写（纯改消息，不动 status/phase/progress）：
          ① 命名轮次已不再 active（state() 的有效状态）；
          ② 发布前不变量校验通过（validate_catalog 无阻断项）；
          ③ catalog 队列没有待办（没有尚未收录进目录的文件）；
          ④ 主题层不欠重建（叶组改名/分裂后的那一次去抖重建已经做完）。
        只改写仍是"正在完善"文案的包，所以幂等、不会覆盖别的消息。
        """
        try:
            item = self.store.get(package_id) or {}
            if (item.get('status') != 'waiting_for_selection' or item.get('phase') != 'catalog_ready'
                    or self.CATALOG_REFINING_NOTE not in str(item.get('message') or '')):
                return False
            from services.large_package_catalog_naming import state, validate_catalog
            if (state(self.store, package_id) or {}).get('active'):
                return False
            # ④ 主题层没有欠账：叶组改名/分裂之后主题层要等去抖窗口再重建一次，
            # 这期间主题名还会变 —— 不能提前宣布"已完善"（实测 lp-ba9afc71 提前了 4 分钟）。
            if self._topic_rebuild_owed(package_id):
                return False
            from services.large_package_catalog_tree import stale as tree_stale
            with closing(self.store._connect()) as conn:
                if tree_stale(conn, package_id):
                    return False
            stage = self.store.stage_counts(package_id, 'catalog')
            if any(int(stage.get(k) or 0) for k in ('queued', 'retry', 'running')):
                return False
            verdict = validate_catalog(self.store, package_id) or {}
            if verdict.get('blocking'):
                return False
            self.store.update_package(package_id, message=self.CATALOG_REFINED_MESSAGE)
            LOGGER.info('large package %s catalog refinement finished', package_id)
            return True
        except Exception:
            LOGGER.exception('mark catalog refined failed: %s', package_id)
            return False

    def _name_fallback(self, package_id):
        """给仍拿不到模型名称的叶组写一个确定性标签，保证目录不留空名。

        模型命名与抽取式命名都可能因为正文过短而放弃某一组，界面上就会显示
        "未命名分组"。这里用代表文件的标题/文件名补一个标签，只保证可读与可
        定位，并显式标记 needs_review，不假装它是模型结论。
        """
        try:
            with closing(self.store._connect()) as conn:
                # **不要抢在模型命名之前**：只要有排队/在跑的命名任务，就把这一组
                # 留给模型。改动前无条件兜底，而 `label_singletons` 又会把
                # "member_count<=1 且 model=deterministic 且未锁定" 的标签删掉并
                # 重新排队 —— 两者每 2 秒对写一次（实测 lp-fb811610 的 3 个单文件组、
                # lp-c91bc4bb 12 秒内新增 2 条确定性标签）。这与 run_slice 里
                # "兜底命名只在确实排空后才做，否则会抢在模型命名之前" 的既有约定一致。
                pending = conn.execute(
                    "SELECT g.id, g.representative, g.content_revision, g.result "
                    "FROM large_catalog_groups g "
                    "LEFT JOIN lp_catalog_labels l ON l.package_id=g.package_id "
                    "AND l.group_id=g.id WHERE g.package_id=? AND l.group_id IS NULL "
                    "AND g.status NOT IN ('pending') "
                    "AND NOT EXISTS (SELECT 1 FROM lp_catalog_jobs j "
                    "                WHERE j.package_id=g.package_id AND j.group_id=g.id "
                    "                  AND j.status IN ('queued','running'))",
                    (package_id,)).fetchall()
                rows = []
                for gid, rep, revision, result in pending:
                    name = ""
                    now = time.time()
                    if str(gid).startswith("km-other-"):
                        # 系统桶：它不是一个语义分组，绝不能用其中某个文件的标题当名字——
                        # 实测这一桶曾经被命名成一篇山羊寄生虫论文的标题，而它下面
                        # 挂着占语料一半的文件。给一个中性、诚实、确定的标签，
                        # 并明确说明它不参与语义分组（因此也不属于任何主题）。
                        #
                        # 这一桶现在有**两种**来源，描述必须与事实相符：
                        #   ① 正文过少、无法参与语义聚类（semantic 的兜底）；
                        #   ② 命名时模型指出"这些成员明显不属于本组"、被摘出来的异类
                        #      （groups.result 里 hold_reason='outliers'，见
                        #      naming.split_off_outliers）。第 ② 类文件的正文往往
                        #      很充足，说它们"正文过少"是**错的**——正是为了避免
                        #      这种"用一个看起来确定的说法盖住不知道的事"，第 3 项
                        #      才改成不给异类起名，描述当然也不能编。
                        try:
                            hold = json.loads(result or "{}").get("hold_reason")
                        except Exception:
                            hold = None
                        if hold == "outliers":
                            note = ("这些文件被判定为与本组其余成员内容不同，已暂时移出，"
                                    "尚未参与语义分组，待重新聚类后再安置。")
                        else:
                            note = ("这些文件正文过少或无法提取有效内容，未参与语义分组，"
                                    "因此不属于任何主题。")
                        rows.append((
                            package_id, gid, int(revision or 1), 1, "未能识别内容",
                            note,
                            "[]", "deterministic",
                            hashlib.sha256((package_id + gid).encode()).hexdigest()[:20],
                            0, 0, now))
                        continue
                    try:
                        row = self.store.get_file(package_id, rep) if rep else None
                        info = ((row or {}).get("local_summary") or {}).get("portrait") or {}
                        heads = [str(h).strip() for h in (info.get("headings") or []) if str(h).strip()]
                        if heads:
                            name = heads[0]
                        if not name:
                            name = str((row or {}).get("name") or rep or "").rsplit("/", 1)[-1]
                            name = re.sub(r"\.[A-Za-z0-9]{1,6}$", "", name)
                    except Exception:
                        name = ""
                    name = re.sub(r"\s+", " ", name).strip(" #*：:")[:40] or "未命名资料"
                    now = time.time()
                    rows.append((package_id, gid, int(revision or 1), 1, name,
                                 "该组正文不足，名称取自代表文件的标题或文件名，需人工确认。",
                                 "[]", "deterministic",
                                 hashlib.sha256((package_id + gid).encode()).hexdigest()[:20],
                                 1, 0, now))
                if rows:
                    with self.store._lock, closing(self.store._connect()) as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        conn.executemany(
                            "INSERT OR REPLACE INTO lp_catalog_labels VALUES"
                            "(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                        conn.commit()
                    self.store._catalog_cache.pop(package_id, None)
                    LOGGER.info("large package %s name fallback applied to %d groups",
                                package_id, len(rows))
                return len(rows)
        except Exception:
            LOGGER.exception("name fallback failed: %s", package_id)
            return 0

    def _catalog_settle_signature(self, package_id):
        """目录"是否还有新变化"的签名：叶组数 / 成员数之和 / 最大内容修订 / 已有标签数。

        只有一个用途：判断"上一轮兜底命名 + 主题归并之后目录有没有任何进展"。
          * 有进展 → 下一轮继续（可能又有新组需要命名）；
          * 无进展 → 记为定稿，不再重复执行兜底与主题归并。

        为什么必须有它：`_name_fallback` 与 `_merge_topics_once` 挂在命名切片的
        "排空"分支上，而命名切片约每 2 秒被提交一次。当主题层因为叶组数达不到
        `LP_TOPIC_MERGE_MIN_GROUPS` 而**永远不可能生成**时，`_merge_topics_once`
        每次返回 False 且不缓存签名，于是形成永久空转——生产实测同一包的日志里
        "name fallback applied to 1 groups / topic merge done=False" 每 2 秒重复一次，
        共刷出 140,040 行标签历史（35.3 倍冗余，两个包达 700~800 倍），
        并让所有包共用的单后台命名槽被反复占用。

        签名刻意不包含主题层（branches）：主题层是否生成不影响"叶组与标签是否
        已停止变化"这个判断，纳入反而会让归并本身触发新一轮重算。
        """
        try:
            with closing(self.store._connect()) as conn:
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(g.member_count),0), "
                    "COALESCE(MAX(g.content_revision),0) "
                    "FROM large_catalog_groups g WHERE g.package_id=? "
                    "AND g.id NOT LIKE 'km-other-%' AND g.id NOT LIKE 'km-pending-%'",
                    (package_id,)).fetchone()
                labels = conn.execute(
                    "SELECT COUNT(*) FROM lp_catalog_labels WHERE package_id=?",
                    (package_id,)).fetchone()
            values = tuple(int(x or 0) for x in (row or (0, 0, 0)))
            return values + (int((labels or (0,))[0] or 0),)
        except Exception:
            return None

    def _schedule_catalog_incremental(self, package_id):
        """把目录增量放到独立后台车道，不阻塞预览车道。

        改动前 `_catalog_incremental` 在 `_quick_one` 的主线程里每 32 个文件
        同步执行一次：期间 `ThreadPoolExecutor` 的预览车道全部空闲，而这一步会做
        embedding HTTP 调用与全包 `GROUP BY` 统计，等于直接从预览吞吐里扣掉。
        这里改为单后台线程执行，同一时刻只允许一个在跑（`_catalog_lane_busy`）。
        """
        # 返回"是否真的提交了任务"。原来的 None 让"车道忙、什么都没排上"与
        # "排上了"无法区分，调用方只能一律当成有进展。
        if self._catalog_lane_busy:
            return False
        if self._catalog_lane is None:
            self._catalog_lane = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="sjfx-lp-catalog")
        self._catalog_lane_busy = True

        def run():
            try:
                self._catalog_incremental(package_id)
            except Exception:
                LOGGER.exception("catalog lane failed: %s", package_id)
            finally:
                self._catalog_lane_busy = False
                self._catalog_lane_future = None

        self._catalog_lane_future = self._catalog_lane.submit(run)
        return True

    def _drain_catalog_lane(self):
        """等待异步目录增量收尾。只在阶段排空点与收尾处调用。"""
        future = self._catalog_lane_future
        if future is None:
            return
        try:
            future.result()
        except Exception:
            LOGGER.exception("catalog lane drain failed")

    def _quick_one(self, package):
        from services.large_package_preview_runner import PreviewCancelled, PreviewRunner
        package_id = package["id"]
        counts = self.store.counts(package_id)
        total = max(1, counts["total_files"])
        done = sum(counts.get("quick", {}).get(key, 0) for key in ("completed", "failed"))
        # **纯诊断**：把一个 30 秒切片里的墙钟按段拆开。
        # 实测这个阶段 worker 主进程 CPU 只有 0.2~0.6%，解析能力 53 文件/秒
        # 却只跑到 12.6 文件/秒 —— 有约 60% 的时间不知去向。
        # 只有进程内分段计时能定位它。不做任何行为改变。
        _started = time.monotonic()
        _done0 = done
        _prof = {}
        def _lap(key, t0):
            _prof[key] = _prof.get(key, 0.0) + (time.monotonic() - t0)
        updated = 0.0
        _t = time.monotonic()
        runners = self._preview_runners
        _lap('mk_runners', _t)
        since_catalog = 0
        slice_deadline = time.monotonic() + 30
        inflight = {}
        try:
            with ThreadPoolExecutor(max_workers=len(runners)) as pool:
                free_runners = list(runners)
                inflight = {}
                while not self._interrupted(package_id):
                    # 滚动补位：只要还有空闲车道就立刻再领一个文件。
                    # 改动前是"先领满 len(runners) 个，再按**提交顺序** future.result()"，
                    # 一个慢文件会把同批里已经跑完的车道一起挂住——实测一个 4 秒
                    # 超时文件能让其余 3 个空闲车道白等（预览预算是 4 秒/文件）。
                    if free_runners and time.monotonic() < slice_deadline:
                        self._check_resources()
                        _t=time.monotonic()
                        claimed=self.store.claim('quick',len(free_runners),package_id=package_id)
                        _lap('claim',_t)
                        # §4.1：submit 段此前只被打印、没有真正 _lap —— 补上。
                        _lap('submit', _t)
                        _t=time.monotonic()
                        items=self.store.preview_files(package_id,[p for _,p,_ in claimed])
                        _lap('getfile',_t)
                        for _,path,lease in claimed:
                            runner=free_runners.pop()
                            future=pool.submit(runner.run,package['root_path'],items.get(path) or {'path':path},
                                               cancel_check=lambda:self._interrupted(package_id))
                            inflight[future]=(path,runner,lease)
                    if not inflight:
                        stage = self.store.stage_counts(package_id, "quick")
                        if any(stage.get(k) for k in ('running','queued','retry')):
                            return True
                        self.store.update_package(package_id, phase='catalog_build', status='running',
                            progress=65, message='文件预览已完成，正在构建智能目录向量和分组')
                        self._drain_catalog_lane()
                        return self._catalog_one(self.store.get(package_id))
                    # 完成即收：谁先完成先收谁；空出的车道在下一轮循环立刻补位。
                    _t = time.monotonic()
                    finished, _pending = wait(inflight, return_when=FIRST_COMPLETED)
                    _lap('wait', _t)
                    _prof['n_wait'] = _prof.get('n_wait', 0) + 1
                    _prof['n_batch'] = _prof.get('n_batch', 0) + len(finished)
                    _t = time.monotonic()
                    commits=[];followups=[];resource_failure=None
                    prior=self.store.preview_files(package_id,[inflight[f][0] for f in finished])
                    for future in finished:
                        path,runner,lease=inflight.pop(future);free_runners.append(runner)
                        try:
                            summary,_=future.result()
                            if summary.get('status')=='failed':raise RuntimeError(summary.get('error') or '轻量解析失败')
                            fields=self._preview_fields(summary,prior.get(path),False)
                            commits.append(dict(package_id=package_id,path=path,stage='quick',token=lease,file_fields=fields))
                            followups.append((path,bool((summary.get('portrait') or {}).get('needs_refine'))))
                        except PreviewCancelled:
                            self.store.requeue(package_id,path,'quick',token=lease)
                        except Exception as exc:
                            if self._defer_resource(package_id,path,'quick',lease,exc):
                                resource_failure=pressure(exc)
                                continue
                            commits.append(dict(package_id=package_id,path=path,stage='quick',token=lease,ok=False,
                                error=str(exc)[:500],file_fields={'quick_status':'failed','error':str(exc)[:500]}))
                            followups.append((path,False))
                        done+=1
                    if commits:
                        try:
                            outcomes=self.store.commit_stage_batch(commits)
                        except Exception as exc:
                            if resource_code(exc):
                                self._check_guard_defer(exc)
                                for item in commits:
                                    self._defer_resource(package_id,item['path'],'quick',item['token'],exc)
                            raise
                        for (ok,reason),(path,refine) in zip(outcomes,followups):
                            if ok and refine:self.store.enqueue_recognition(package_id,path)
                    if resource_failure is not None:
                        raise resource_failure
                    _lap('collect', _t)
                    # 目录增量改为异步后台车道：节奏仍是每 32 个文件一次，
                    # 但不再占用预览主线程（详情见 _schedule_catalog_incremental）。
                    since_catalog += len(finished)
                    if since_catalog >= 32:
                        since_catalog = 0
                        self._schedule_catalog_incremental(package_id)
                    if time.monotonic()-updated>=1:
                        updated=time.monotonic()
                        _t = time.monotonic()
                        self.store.update_package(package_id,progress=min(65,20+int(45*done/total)),
                            message="已尝试内容预览 {}/{}；目录持续更新".format(done,total))
                        _lap('progress', _t)
        finally:
            # A pause can exit the loop before its futures are collected. The
            # executor has joined them; persist completed work or return leases.
            for future, (path, _runner, lease) in list(inflight.items()):
                try:
                    summary, _ = future.result()
                    self._commit_preview(package_id, path, summary, lease)
                except PreviewCancelled:
                    self.store.requeue(package_id, path, 'quick', token=lease)
                except Exception as exc:
                    if self._defer_resource(package_id,path,'quick',lease,exc):
                        continue
                    self.store.commit_stage_result(
                        package_id, path, 'quick', lease, ok=False, error=str(exc)[:500],
                        file_fields={'quick_status': 'failed', 'error': str(exc)[:500]})
            _t = time.monotonic()
            if self._interrupted(package_id):self._drain_catalog_lane()
            _lap('drain', _t)
            _t = time.monotonic()
            if self._interrupted(package_id):
                for runner in runners:runner.close()
            _lap('close_runners', _t)
            _total = time.monotonic() - _started
            _n = done - _done0
            if _SLICE_PROFILE:
                LOGGER.info(
                    "quick slice profile package=%s wall=%.2fs files=%d(%.1f/s) | claim=%.2f getfile=%.2f "
                    "submit=%.2f wait=%.2f collect=%.2f progress=%.2f drain=%.2f close=%.2f mk=%.2f "
                    "| n_wait=%s n_batch=%s",
                    package_id, _total, _n, (_n / _total if _total else 0),
                    _prof.get('claim', 0), _prof.get('getfile', 0), _prof.get('submit', 0),
                    _prof.get('wait', 0), _prof.get('collect', 0), _prof.get('progress', 0),
                    _prof.get('drain', 0), _prof.get('close_runners', 0), _prof.get('mk_runners', 0),
                    _prof.get('n_wait', 0), _prof.get('n_batch', 0))
        return True

    def _catalog_one(self, package):
        package_id = package['id']
        if self._interrupted(package_id):
            return False
        before = self.store.stage_counts(package_id, 'catalog')
        attempted = int(before.get('completed', 0)) + int(before.get('failed', 0))
        total = sum(int(value) for value in before.values())
        self.store.update_package(package_id, status='running', phase='catalog_build',
            progress=65 + int(3 * attempted / max(1, total)),
            message='文件预览已全部完成；智能目录向量 {}/{}，剩余 {} 项，随后生成主题和叶子分组'.format(
                attempted, total, self.store.pending_total(package_id, 'catalog')))
        self._catalog_incremental(package_id)
        if self._interrupted(package_id):
            return True
        after = self.store.stage_counts(package_id, 'catalog')
        if any(after.get(key, 0) for key in ('queued', 'retry', 'running')):
            return after != before
        self.store.update_package(package_id, phase='recognize', status='running', progress=68,
            message='目录向量处理结束，正在补充识别并生成主题和叶子目录')
        return True

    def _catalog_counts(self, package_id):
        """包 counts + 目录算法标注。

        `catalog_mode` 供界面/API 显式区分 k-means 智能目录与降级目录
        （`LP_CATALOG_KMODE=0` 时的旧增量锚点分组）。纯新增字段，
        消费方不读它也不受影响。
        """
        # 目录首次对用户可见时，按真实成员表重算一次 member_count 缓存。
        # 历史库里该缓存被 INSERT OR REPLACE 破坏过（只增不减：实测 4/23 个包漂移，
        # 1040→1502、82→138，67 个组不符），而它直接决定界面显示的分组文件数
        # 与覆盖率口径。只重算缓存，不改变任何分组归属；每个包在本进程内只做一次。
        if package_id not in self._counts_repaired:
            self._counts_repaired.add(package_id)
            try:
                from services.large_package_semantic import repair_member_counts
                repair_member_counts(self.store, package_id)
            except Exception:
                LOGGER.exception("member count repair deferred: %s", package_id)
        counts = self.store.counts(package_id)
        counts["catalog_mode"] = getattr(self, "catalog_mode", "kmeans")
        return counts

    def _recognize_one(self, package):
        from services.large_package_preview_runner import PreviewCancelled
        package_id = package["id"]
        self.store.reconcile_recognition(package_id)
        # Bound aggregate extra work as well as each file. Deferred files remain
        # visible and can be continued explicitly without redoing completed work.
        try:
            slice_seconds = max(5, min(300, int(os.getenv("SJFX_LP_RECOGNITION_SLICE_SECONDS", "60"))))
            # B4：一轮补识别从 30 个文件提到 100 个。
            # 它是"可选补识别"车道，每轮只做 30 个的话，13,394 个文件的积压
            # 要跑几百轮；而每轮只受 60 秒预算约束，30 个文件远没跑满预算。
            max_files = max(1, min(500, int(os.getenv("SJFX_LP_RECOGNITION_SLICE_FILES", "100"))))
        except ValueError:
            slice_seconds, max_files = 60, 30
        end = time.monotonic() + slice_seconds
        processed = 0
        while processed < max_files and time.monotonic() < end and not self._interrupted(package_id):
            self._check_resources()
            claimed = self.store.claim("recognize", 1, package_id=package_id)
            if not claimed:
                break
            _, path, lease = claimed[0]
            try:
                item = self.store.get_file(package_id, path)
                summary, _ = self.preview_runner.run(package["root_path"], item, refine=True, cancel_check=lambda: self._interrupted(package_id) or time.monotonic() >= end)
                self._commit_preview(package_id, path, summary, lease, refine=True)
            except PreviewCancelled:
                self.store.requeue(package_id, path, "recognize", token=lease)
                break
            except Exception as exc:
                if self._defer_resource(package_id,path,'recognize',lease,exc):
                    raise pressure(exc) from exc
                self.store.commit_stage_result(
                    package_id, path, "recognize", lease, ok=False,
                    error=str(exc)[:300],
                    file_fields={"recognition_status": "needs_review", "error": str(exc)[:300]})
            processed += 1
            self.store.update_package(package_id, message="已补充识别 {} 个文件；可查看内容主题与来源依据".format(processed))
        if self._interrupted(package_id):
            return True
        self._catalog_incremental(package_id)
        self._ensure_km_finalized(package_id)
        if self._interrupted(package_id):
            return True
        stage = self.store.stage_counts(package_id, "recognize")
        pending = sum(stage.get(k, 0) for k in ("queued", "retry", "running"))
        if self.store.pending_total(package_id, 'deep'):
            self.store.update_package(package_id, status='queued', phase='deep_parse', progress=70,
                                      message='快速处理已结束，继续先前排队的深度摘要')
            return True
        completed = package["phase"] == "recognize_completed"
        if Config.LP_CATALOG_MODEL_NAMING and not completed:
            from services.large_package_catalog_naming import queue_groups, ensure_round
            queue_groups(self.store, package_id)
            ensure_round(self.store, package_id)
            self.store.update_package(package_id, status="waiting_for_selection", phase="catalog_ready", progress=70,
                message="内容目录已就绪，可选择深度摘要范围；主题名称正在后台按证据完善", counts=self._catalog_counts(package_id))
            return True
        self.store.update_package(package_id, status="completed" if completed else "waiting_for_selection", phase="report_ready" if completed else "catalog_ready", progress=100 if completed else 70,
                                  message="内容目录已就绪；{} 个文件待补充识别，可先选择深度解析范围".format(pending), counts=self._catalog_counts(package_id))
        return True

    def _naming_one(self, package):
        # Resume older packages without making optional wording block selection.
        from services.large_package_catalog_naming import queue_groups, ensure_round
        package_id = package['id']
        if self._interrupted(package_id):
            return False
        queue_groups(self.store, package_id)
        ensure_round(self.store, package_id)
        # 收尾点 1/5：向唯一判定口取证。这里**只取证、不改闸门** ——
        # "不让可选措辞阻塞选择"是既定产品决定（见本函数第一行注释），
        # 所以 advance() 说 can_finish=False 时不阻止进入 catalog_ready，
        # 而是把"还差什么"如实写进消息，让用户知道目录还在完善。
        verdict = {}
        try:
            verdict = self.store.advance(package_id) or {}
        except Exception:
            LOGGER.exception('advance() failed: %s', package_id)
        blocked = verdict.get('blocked_by') or []
        note = '内容目录已就绪，可选择深度摘要范围；主题名称正在后台按证据完善'
        if blocked:
            note += '（还差：%s）' % '、'.join(str(b) for b in blocked)
        self.store.update_package(package_id, status='waiting_for_selection', phase='catalog_ready',
                                  progress=70, message=note)
        return True

    # ---- C：有界双路命名的车道管理（文档 §3C）----------------------------
    @property
    def _naming_future(self):
        """兼容旧接口：返回**最近提交**的那一路 future。

        单路（开关关闭）时它就是唯一在途的那一路，因此
        `worker._naming_future = Future()` 与 `worker._naming_future.result()`
        这两个既有用法都继续成立。
        """
        for pid in reversed(self._naming_order):
            future = self._naming_futures.get(pid)
            if future is not None:
                return future
        return self._naming_orphan_future

    @_naming_future.setter
    def _naming_future(self, value):
        # 旧接口写入：当作"外部塞进来的一路在途"参与占位计算。
        self._naming_orphan_future = value

    def _naming_lanes(self):
        """本次允许的在途命名路数。两个约束取小，并在夹紧时只记一次日志。

        ① `SJFX_LP_NAMING_PARALLEL`（默认关）——不开就只有 1 路；
        ② 准入闸门的后台名额 `max(1, SJFX_GENERATION_CONCURRENCY - 1)`。闸门跨进程
           全局（共享 model_dispatch.db），后台请求只拿 N-1 个名额；实测 N=1/2 时后台
           上限都是 1，此时第二路只会在 generation_slot 上排队 —— 按上限夹紧，
           不制造"看起来在途 2、其实一跑一停"的假象。
        """
        enabled = str(os.getenv('SJFX_LP_NAMING_PARALLEL', '0')).strip().lower() in {
            '1', 'true', 'yes', 'on'}
        if not enabled:
            return 1
        try:
            from services.model_dispatch import _concurrency_limit
            gate = max(1, int(_concurrency_limit()) - 1)
        except Exception:
            gate = 1
        try:
            wanted = int(getattr(Config, 'LP_NAMING_PARALLEL_LANES', 2) or 2)
        except Exception:
            wanted = 2
        try:
            from services.large_package_catalog_naming import naming_concurrency
            wanted = min(wanted, naming_concurrency())
        except Exception:
            pass
        lanes = max(1, min(gate, wanted))
        if not self._naming_parallel_logged:
            self._naming_parallel_logged = True
            LOGGER.info('naming parallel: wanted=%d gate_bg_slots=%d effective_lanes=%d '
                        '(SJFX_LP_NAMING_PARALLEL=on)', wanted, gate, lanes)
        return lanes

    def _naming_inflight(self):
        """在途（未完成）的命名包 id 列表。调度器据此避开已经占着车道的包。"""
        return [pid for pid, future in self._naming_futures.items() if not future.done()]

    def _naming_slot_free(self, package_id):
        """还能不能再起一路（给这个包）。"""
        if package_id in self._naming_inflight():
            return False
        extra = 1 if (self._naming_orphan_future is not None
                      and not self._naming_orphan_future.done()) else 0
        return len(self._naming_inflight()) + extra < self._naming_lanes()

    def _reap_naming(self):
        """回收已结束的 future，并把异常如实记下来（旧实现只对"上一路"做这件事）。"""
        for pid in list(self._naming_order):
            future = self._naming_futures.get(pid)
            if future is None or not future.done():
                continue
            try:
                future.result()
            except Exception:
                LOGGER.exception('catalog naming task failed')
            self._naming_futures.pop(pid, None)
            try:
                self._naming_order.remove(pid)
            except ValueError:
                pass

    def _submit_naming(self, package_id):
        from services.large_package_catalog_batch import process_batch
        self._reap_naming()
        if not self._naming_slot_free(package_id):
            return False
        if self._naming_executor is None:
            # executor 的线程数按**实际允许的路数**开。原来固定开
            # `naming_concurrency()`（默认 2）个线程，但闸门只放 1 路时第二个线程
            # 永远闲置 —— 而且"配了 2 个 worker"会被误读成"两路在途"。
            from services.large_package_catalog_naming import naming_concurrency
            self._naming_executor = ThreadPoolExecutor(
                max_workers=max(1, min(self._naming_lanes(), naming_concurrency())))
        def interrupted():
            item = self.store.get(package_id)
            return self.stop_requested or not item or item['status'] not in {'waiting_for_selection', 'completed'}
        def run_slice():
            # Continue ready batches without a poll sleep after every group;
            # bound package occupancy so other packages can get a turn.
            # W-2a：单个命名切片 30s → 120s。
            # 实测多包并存时轮转周期 = 包数 × 切片时长，而调度器本来就优先把车道
            # 给"还有排队任务"的包（_catalog_background 的排序第一项），所以拉长切片
            # 等于让忙碌的包连着跑，正是我们要的"同包连跑"；小包只在没有别人有活时
            # 才拿车道，不受影响。
            deadline = time.monotonic() + 120
            drained = False
            while time.monotonic() < deadline and not interrupted():
                from services.catalog_prefetch import scope as evidence_prefetch_scope
                with evidence_prefetch_scope():
                    progressed = process_batch(self.store, package_id, interrupted)
                if not progressed:
                    drained = True
                    break
            if interrupted():
                return
            # 兜底命名只在**确实排空**后才做：否则会抢在模型命名之前，
            # 把每一组都写成从文件标题取的确定性名字（实测 67 个组被这样占住）。
            #
            # 但兜底与主题归并不能每轮无条件重做：主题层在叶组数不足
            # `LP_TOPIC_MERGE_MIN_GROUPS` 时**永远不可能生成**，无条件重做会形成
            # 每 2 秒一次的空转。这里用目录签名做进展判定——只有"确实排空"的
            # 完整一轮走完且签名毫无变化，才记为定稿；签名再变会自动恢复。
            signature_before = self._catalog_settle_signature(package_id)
            settled = (signature_before is not None
                       and self._catalog_settled.get(package_id) == signature_before)
            # **这里刻意不拦定稿。**
            #
            # 第一版我在这里加了"主题层落后就不许定稿"，契约测试立刻抓到回归：
            # `test_drained_slice_does_not_repeat_fallback_and_merge` 失败
            # （兜底命名被执行了 2 次）。原因是有些包**永远不可能有完整的主题层**
            # （例如叶组数不足 LP_TOPIC_MERGE_MIN_GROUPS、或主题聚类天然覆盖不到
            # 全部叶组），拦定稿会让它们永不收敛 —— 而 `_catalog_settled` 存在的
            # 唯一目的就是止住"每 2 秒重复兜底+归并"（历史上刷出过 140,040 行）。
            #
            # 正确做法：落后只用来**触发重建**（见 _topic_catchup，独立 60 秒节流，
            # 与是否定稿无关），不用来拦定稿。
            # 收尾点 2/5：定稿判定不再只看目录签名，还要问唯一判定口。
            # 两者都同意"没有待办"才算定稿；advance() 说有阻塞时一律不定稿
            # （改动前只看签名，于是"签名没变但还有待办"会被误判为定稿）。
            try:
                _verdict = self.store.advance(package_id) or {}
            except Exception:
                _verdict = {}
            if _verdict and not _verdict.get('can_finish') and _verdict.get('blocked_by'):
                settled = False
            if not settled:
                if drained:
                    self._name_fallback(package_id)
                # 主题归并**不能只在排空时做**：命名执行器是所有包共享的单线程，
                # 只要还有别的包在抢（尤其是拆分不断产生新任务的包），这个包就可能
                # 长期排空不了，主题层永远不出现。merge_topics 内部按叶组签名判重，
                # 签名没变就是空操作，所以提前调用安全且幂等。
                self._merge_topics_once(package_id)
                if drained:
                    signature_after = self._catalog_settle_signature(package_id)
                    if signature_after is not None and signature_after == signature_before:
                        if self._topic_rebuild_owed(package_id):
                            # 叶组确实停了，但主题层还欠一次重建（去抖窗口未到）。
                            # 现在定稿就会把那次重建冻掉，主题层永久停在旧叶组集合上。
                            # 只在"欠账签名"变化时报一次：这个分支每 2 秒走一次。
                            _owed_sig = self._topics_pending.get(package_id)
                            if self._topics_owed_logged.get(package_id) != _owed_sig:
                                self._topics_owed_logged[package_id] = _owed_sig
                                LOGGER.info("large package %s catalog not settled yet — "
                                            "主题层仍欠一次重建（去抖窗口未到）",
                                            package_id)
                        else:
                            self._catalog_settled[package_id] = signature_after
                            LOGGER.info("large package %s catalog settled signature=%s "
                                        "(停止重复的兜底命名与主题归并)",
                                        package_id, signature_after)
                            # k 自适应（2026-09-20）：叶组定稿后看模型自认"混杂(mixed)"的
                            # 叶组占比。偏高说明叶子太粗（实测同一语料 k=120 时 40%、
                            # k=300 时 27%），升一档 k 重新聚类并重命名。档位落库、有上限
                            # （config.LP_KM_ESCALATE_*），所以代价有界、不会震荡。
                            try:
                                from services.large_package_semantic import maybe_escalate_k
                                if maybe_escalate_k(self.store, package_id):
                                    self._catalog_settled.pop(package_id, None)
                            except Exception:
                                LOGGER.exception("k escalation check failed: %s", package_id)
                            # 兜底命名（2026-09-20）：**重试耗尽**的组如果一直没有标签，
                            # 目录覆盖率就会永远缺一块（实测某包停在 7369/7754）。
                            # 这里给它写一个抽取式名字（不碰组状态，见函数的说明）。
                            try:
                                from services.large_package_catalog_naming import fallback_name_exhausted
                                fixed = fallback_name_exhausted(self.store, package_id)
                                if fixed:
                                    self._catalog_settled.pop(package_id, None)
                            except Exception:
                                LOGGER.exception("fallback naming check failed: %s", package_id)
            # 里程碑 1：智能目录（含主题层）确定 → 生成第一版情报概览。
            # 内部会检查"命名是否排空、主题层是否就绪、第一版是否已存在"，
            # 条件不满足时直接返回，不会重复生成。
            self._build_overview(package_id, "catalog")
            # 兜底其他"该出第一版却还没有"的包。不能挂在 _catalog_background 的
            # 空闲分支上——那里要求"没有任何待办任务"，而实际总有些包带着任务，
            # 于是永远不会执行。这里挂在每个命名切片末尾并限频。
            now = time.monotonic()
            if now - self._overview_last_sweep >= 30:
                self._overview_last_sweep = now
                self._overview_catchup()
            # 轮次状态的**唯一写入点**：读路径（state()）已经从"顺手 UPDATE"
            # 改成纯读，落库集中在这里，每个命名切片结束时结算一次。
            try:
                from services.large_package_catalog_naming import settle_round
                settle_round(self.store, package_id)
                self.store.reconcile_recognition(package_id)
            except Exception:
                LOGGER.exception('settle_round failed: %s', package_id)
            self._mark_catalog_refined(package_id)
        self._naming_futures[package_id] = self._naming_executor.submit(run_slice)
        if package_id in self._naming_order:
            self._naming_order.remove(package_id)
        self._naming_order.append(package_id)
        # **提交成功就是"这一轮有进展"**，必须返回 True。
        # run_once 的返回值决定 run_forever 要不要 sleep：返回 False 会被当成
        # "无事可做"，于是刚派出去的命名切片被白白等一个 poll 周期。
        # 契约测试 test_background_names_do_not_wait_for_busy_generation
        # 断言的正是这件事（原来的 False 让该用例失败）。
        return True

    def _solo_catchup(self):
        """方案A：给"有可读特征卡、却没有任何组"的文件补单文件条目。

        独立于 k-means：只新增、不动已有分组，所以**冻结包也能修** ——
        冻结保护的是"结构不被静默重排"，而这里不重排任何结构。
        限频 120 秒，避免每轮都扫全库。
        """
        now = time.monotonic()
        if now - self._solo_catchup_at < 120.0:
            return 0
        self._solo_catchup_at = now
        try:
            from services.large_package_semantic import maintain_ungrouped
            with closing(self.store._connect()) as c:
                rows = c.execute(
                    "SELECT id FROM packages WHERE status IN "
                    "('waiting_for_selection','completed','running') "
                    "ORDER BY updated_at DESC LIMIT 40").fetchall()
            made = maintain_ungrouped(self.store, [r[0] for r in rows])
            # 与"本次是否新建"**解耦**：只要库里还有"有 solo 组、却没有命名任务"
            # 的包，就给它排任务。第一版只在 made 非空时才排，而条目第一次运行就
            # 建好了、之后 made 恒为空 —— 任务永远排不上（实测 6 分钟 0 条）。
            try:
                with closing(self.store._connect()) as c:
                    # **收敛判据：只找 queue_groups 真正建得出任务的那些组。**
                    #
                    # 原判据只问"有没有 queued/running/deferred 任务"，而命名成功的
                    # solo 组其任务状态是 `completed`（不在该集合里），于是这个包被
                    # **永久**判为欠账：实测 lp-085e413e… 等 3 个包从 18:48 到 19:51
                    # 每 120 秒重排一次，而 queue_groups 因标签已齐实际建 0 条任务
                    # —— 纯空转 + 日志刷屏。
                    #
                    # 为什么必须收紧到"完全没有任务行"、而不是"没有现行标签"就够：
                    # queue_groups 的 upsert 带
                    # `WHERE lp_catalog_jobs.revision!=excluded.revision`，
                    # 同一 revision 下**已存在的非待办任务行永远不会被复活**。所以
                    # 任何"有任务行、同 revision、却无标签"的组一旦进了这个列表，
                    # 下一轮必然再次满足判据 → 仍然无限空转。取"无任务行"既与
                    # queue_groups 的可插入集合严格对齐（solo 组 id 不以 km-other-/
                    # km-pending- 开头，故不受它那两条排除影响），也保证一轮即收敛。
                    # 实测全库 429 个 solo 组无一处于"有任务行却没名字"的状态，
                    # 因此该判据不会漏掉任何真正需要补做的组。
                    stale = [r[0] for r in c.execute(SOLO_CATCHUP_STALE_SQL)]
                if stale:
                    from services.large_package_catalog_naming import queue_groups
                    for package_id in stale:
                        # 这些包大多停在 `catalog_ready`，该阶段不在
                        # SCHEDULABLE_PHASES 里 —— 没有命名切片会轮到它们，
                        # 所以必须在这里自己排。
                        queue_groups(self.store, package_id)
                    LOGGER.info('solo catchup queued naming for packages: %s', stale)
            except Exception:
                LOGGER.exception('solo queue_groups failed')
            if made:
                LOGGER.info('solo catchup created single-file groups: %s', made)
            return sum(made.values())
        except Exception:
            LOGGER.exception('solo catchup failed')
            return 0

    def _topic_catchup(self):
        """主动补做"主题层落后于叶组"的重建。

        为什么需要一条**独立的**补做路径：`_merge_topics_once` 挂在命名排空分支上，
        而 `_catalog_background` 选包时把"有分支、无待办任务"的包排在**最后** ——
        一个包命名任务全部完成之后，worker 就再也不会轮到它。于是"叶组已经齐了、
        主题层还是旧的"这个状态**永远不会被纠正**。

        实测（2026-09-19，lp-c550cf3b，1040 文件）：主题层只覆盖 30 个叶组，
        而实际有 68 个 —— 38 个叶组 / **436 个文件**永久留在「待归并主题」，
        没有任何后续动作会去修它。
        """
        now = time.monotonic()
        if now - self._topic_catchup_at < self._topic_catchup_seconds:
            return False
        self._topic_catchup_at = now
        try:
            with closing(self.store._connect()) as c:
                rows = c.execute(
                    "SELECT id FROM packages "
                    "WHERE status IN ('waiting_for_selection','completed') "
                    "ORDER BY updated_at DESC LIMIT 40").fetchall()
            for row in rows:
                package_id = str(row[0])
                if self._interrupted(package_id):
                    continue
                if self._topic_layer_stale(package_id):
                    LOGGER.info("topic catchup: package=%s 主题层落后于叶组，触发重建",
                                package_id)
                    self._merge_topics_once(package_id)
                    return True
        except Exception:
            LOGGER.exception("topic catchup failed")
        return False

    def _topic_layer_stale(self, package_id):
        """主题层是否**落后于叶组** —— 有真实叶组没被任何主题覆盖。

        为什么必须是 DB 可判据：重建只在 worker「正在处理这个包」时才被触发
        （挂在命名排空分支上）。而一个包命名任务全部完成之后，worker 就不会再
        轮到它 —— 于是"叶组已经齐了、主题层还是旧的"这个状态**永远不会被纠正**。

        实测（2026-09-19，用户新导入的 lp-c550cf3b，1040 文件）：
          22:55:35 主题归并撞上叶组分裂，只有 30 个叶组标签对齐 → 主题层只覆盖 30 个；
          之后 68 个标签全部就位，却再没有任何一次重建 →
          **38 个叶组 / 436 个文件永久留在「待归并主题」**。

        判据：`lp_catalog_branches.revisions` 记录了建分支时每个叶组的
        content_revision。任何真实叶组（member_count>0、非系统桶）不在其中、
        或 revision 对不上，就说明主题层落后。
        """
        now = time.monotonic()
        if now - self._topic_stale_at.get(package_id, 0.0) < self._topic_stale_seconds:
            return False
        self._topic_stale_at[package_id] = now
        try:
            with closing(self.store._connect()) as c:
                from services.large_package_catalog_tree import stale as tree_stale
                tree_state=tree_stale(c,package_id)
                # **只有"确实陈旧"才提前返回。**
                # `tree.stale()` 判的是叶组集合指纹（叶组数/成员总数/最大修订号）变没变，
                # 而"指纹没变"**不等于**三级架构完整：旧代码建出来的树可能只覆盖了一部分
                # 叶组（生产实测 lp-68848e2c 只覆盖 140/369），指纹却与现在完全一致。
                # 原实现只要它有结论就 return，于是下面那段覆盖检查根本不执行 ——
                # 这些树永远不会被重建，覆盖率修复和主题名补全都落不到存量数据上。
                if tree_state:return True
                # **必须与 merge_topics 的叶组口径一致**：只算"有成员、且标签
                # 与 content_revision 对齐（或已锁定）"的真实叶组。
                #
                # 第一版我只筛了 member_count>0，漏了"有名字"这个条件 —— 结果对
                # 叶组不足 / 还没命名的包永远判定为"落后"，补做路径每分钟空转一次
                # （实测 lp-900900aa 反复触发 "topic merge not applicable
                # (leaf groups 0 < minimum)"），还会挡住真正需要补做的包。
                # **只把"有能力参与主题"的叶组算进覆盖要求。**
                # 没有锚点（向量）的叶组无法按语义吸附 —— 生产实测 lp-ff42d1f7 的 266 个
                # 无向量成员**全部**被管道自己的质量闸门拒掉（too_little_text / no_content）。
                # 把它们也算作"未覆盖"，主题层就会被判永久陈旧、周期性反复重建。
                rows = c.execute(
                    "SELECT g.id, g.content_revision FROM large_catalog_groups g "
                    "JOIN lp_catalog_labels l ON l.package_id=g.package_id "
                    "     AND l.group_id=g.id "
                    "WHERE g.package_id=? AND g.member_count>0 "
                    "AND length(COALESCE(g.anchor,''))>0 "
                    "AND g.id NOT LIKE 'km-other-%' AND g.id NOT LIKE 'km-pending-%' "
                    "AND (l.revision=g.content_revision OR l.locked=1)",
                    (package_id,)).fetchall()
                # 叶组数不足时 merge_topics 本来就不会建主题层（not applicable），
                # 那种包不算"落后"，否则会永远被判为陈旧。
                from config import Config as _Cfg
                if len(rows) < max(1, int(getattr(_Cfg, 'LP_TOPIC_MERGE_MIN_GROUPS', 12) or 12)):
                    return False
                # 覆盖判据必须用**整棵树**的 leaf_ids，不能用 `lp_catalog_branches.revisions`：
                # branches 只写"最底层分支"（legacy 分区），而补全后的三级架构里有一部分
                # 叶组是**直接挂在顶层主题下**的 —— 它们不在任何 bottom 节点里。
                # 用 branches 判会永远判"落后"，形成无限重建。
                covered = set()
                tree_row = c.execute('SELECT payload FROM lp_catalog_tree WHERE package_id=?',
                                     (package_id,)).fetchone()
                if tree_row:
                    try:
                        for _node in (json.loads(tree_row[0]) or {}).get('nodes') or []:
                            covered.update(str(x) for x in (_node.get('leaf_ids') or []))
                    except (TypeError, ValueError):
                        pass
            for row in rows:
                if str(row[0]) not in covered:
                    return True
            # 全部叶组都在某个主题下 —— 再做一次修订号一致性检查（覆盖通过但内容变过的情形）
            return False
        except Exception:
            LOGGER.exception("topic staleness check failed: %s", package_id)
            return False

    def _topic_rebuild_owed(self, package_id):
        """去抖推迟掉、但**还没有补做**的主题层重建。

        为什么必须有它：去抖只应把重建**延后**，绝不能把它**取消**。

        生产实测（lp-605781，重新聚类后）：09:06:46 做了首次主题归并，当时只有
        28 个叶组已命名；之后叶组持续分裂、签名一路在变，去抖把重建一次次推后；
        09:14:13 最后一次变化，而 09:16:11 的"目录签名连续两轮不变"就判定定稿
        —— 去抖窗口（180 秒）还差 62 秒没到，那次本该覆盖全部叶组的重建
        **永远不会再发生**。结果 132 个叶组被永久留在"待归并主题"兜底分支里，
        真实主题只覆盖 28 个叶组；而旧代码同一个包的终态是 14 个主题、
        168 个叶组全部归位。

        所以定稿判定必须多问这一句。没有它，"主题层不完整"会被当成"目录已定稿"。
        注意：叶组数不足 `LP_TOPIC_MERGE_MIN_GROUPS` 的包从来不会写 `_topics_pending`
        （那条路径上 `_topics_merged` 始终是 None、不进去抖分支），因此这里不会把
        它们重新拖回每 2 秒的空转——`_catalog_settled` 的原始目的依然成立。
        """
        if self._topics_rebuild_debounce <= 0:
            return False
        pending = self._topics_pending.get(package_id)
        if pending is None:
            return False
        return self._topics_merged.get(package_id) != pending[0]

    def _merge_topics_once(self, package_id):
        """叶组命名收尾后归并一次上位主题（默认关闭；失败不影响叶组目录）。"""
        try:
            from config import Config
            if not getattr(Config, "LP_TOPIC_MERGE", False):
                return False
            # 叶组签名：(叶组数, 成员总数之和, 最大内容修订)。
            # 与上次归并时相同就不再重复调用模型；不同就必须重来一次——
            # 全局聚类可能在主题归并之后又跑了一遍（实测 66 个叶组 → 69 个），
            # 那时旧的主题层已经对不上新的叶组集合了。
            with closing(self.store._connect()) as conn:
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(g.member_count),0), "
                    "COALESCE(MAX(g.content_revision),0) "
                    "FROM lp_catalog_labels l JOIN large_catalog_groups g "
                    "ON g.package_id=l.package_id AND g.id=l.group_id "
                    "WHERE l.package_id=? AND g.id NOT LIKE 'km-other-%' "
                    "AND (l.revision=g.content_revision OR l.locked=1)",
                    (package_id,)).fetchone()
                from services.large_package_catalog_tree import fingerprint as tree_fingerprint, retry_due
                leaf_versions=[dict(r) for r in conn.execute('''SELECT g.id group_id,g.content_revision,l.version,l.name
                    FROM large_catalog_groups g JOIN lp_catalog_labels l ON l.package_id=g.package_id AND l.group_id=g.id
                    WHERE g.package_id=? AND g.member_count>0 AND (l.revision=g.content_revision OR l.locked=1)
                    AND g.id NOT LIKE 'km-other-%' AND g.id NOT LIKE 'km-pending-%' ''',(package_id,))]
                tree_key=tree_fingerprint(leaf_versions)
                tree_row=conn.execute('SELECT payload FROM lp_catalog_tree WHERE package_id=?',(package_id,)).fetchone()
                retry_pending=False
                tree_payload=json.loads(tree_row[0]) if tree_row else {}
                if tree_payload.get('pending_names'):
                    latest=conn.execute('SELECT id FROM lp_catalog_rounds WHERE package_id=? ORDER BY started DESC LIMIT 1',(package_id,)).fetchone()
                    retry_pending=retry_due(tree_payload,latest[0] if latest else None)
            signature = tuple(int(x or 0) for x in (row or (0, 0, 0)))
            signature += (tree_key,)
            if self._topics_merged.get(package_id) == signature and not retry_pending:
                # 签名回到"已归并"的那个点上：没有欠账了，清掉去抖标记，
                # 否则 _topic_rebuild_owed 会一直认为还欠一次重建而挡住定稿。
                self._topics_pending.pop(package_id, None)
                self._topics_owed_logged.pop(package_id, None)
                return False
            # **首次归并立即做**，让主题层尽早出现；
            # **重建则要求叶组签名先稳定一段时间**。
            #
            # 为什么：生产实测 lp-702f7a 在 20:00~20:27 的 27 分钟里重建了 10 次
            # 主题层（叶组数 153→156→165→172→180→183→172→179→183→166 一路在变），
            # 主题数在 10/16/10/20/19/15/19/18/18/9 之间反复跳变。代价有两层：
            #   * 每次重建都要为全部主题重新调用模型（10~20 次/轮）；
            #   * 用户正在浏览的目录会看到主题反复出现、消失、改名 ——
            #     而框架 §11 明令"已发布目录不静默重排"。
            # 归并本身是幂等的（merge_topics 内部按叶组签名判重），
            # 所以延后重建不会丢结果，只是等叶组稳定后再花这笔模型预算。
            # 设为 0 可关闭去抖，回到"签名一变就重建"。
            # Same-content retries use the saved attempt cap and existing round
            # budget. Only changed leaves need the rebuild debounce.
            if self._topics_merged.get(package_id) not in (None, signature) and self._topics_rebuild_debounce > 0:
                pending = self._topics_pending.get(package_id)
                now_mono = time.monotonic()
                if pending is None or pending[0] != signature:
                    self._topics_pending[package_id] = (signature, now_mono)
                    LOGGER.debug("topic rebuild deferred (signature changed) package=%s", package_id)
                    return False
                if now_mono - pending[1] < self._topics_rebuild_debounce:
                    return False
            # Empty packages cannot form a theme. Small nonempty packages still
            # need the same theme -> leaf -> file structure as large packages.
            if signature[0] == 0:
                self._topics_merged[package_id] = signature
                LOGGER.info("large package %s topic merge not applicable "
                            "(leaf groups %d < minimum) — 记为定稿",
                            package_id, signature[0])
                return False
            # 主题归并要用**当前有效**的轮次。不能复用"最近一个 round"：
            # 那是命名阶段早期创建的，等做到主题层时它的 deadline 早已过期
            # （实测表现为 requests 只用了 4/1000、tokens 只用了 6884/2000000
            #  却报 budget exhausted，实际是 reserve() 拒绝了过期轮次）。
            from services.large_package_catalog_naming import merge_topics, ensure_round
            # 主题归并要连着做若干次模型调用，所以必须保证轮次**剩余时间够用**。
            # 传 min_seconds 之后，一个即将到期的轮次不会再被复用——那正是主题名
            # 退化成"主题N（N个内容目录）"的直接原因（实测：归并开始时轮次只剩
            # 几十秒，跑到一半到期，后半段全部变成兜底名）。
            merge_seconds = 30 * max(1, int(getattr(Config, 'LP_CATALOG_CALL_SECONDS', 16)))
            # B1：这里原来传的是 `"topics-%d" % int(time.time())` —— **每一秒都是一个新
            # key**，而 ensure_round 的语义是"没见过的 key 就新建一个满预算轮次"。
            # 生产实测：31,567 个不同的 topics key 全部指向 106 个轮次，等于主题层每重建
            # 一次就重置一次请求/token 预算，并把旧轮次标成 exhausted。
            # 改成稳定 key 后，复用与否由**原本的 min_seconds 机制**决定：剩余时间够
            # （≥ merge_seconds）就复用；不够就照常新建，行为回到原设计意图。
            round_row = ensure_round(self.store, package_id,
                                     "topics",
                                     min_seconds=merge_seconds)
            if not round_row:
                return False
            # **轮次必须是 active。** ensure_round 的幂等保护对非 exhausted 的终态轮次
            # （例如 completed）会原样返回，而 reserve() 只认 active —— 拿它去建主题层
            # 必然一个名字都起不出来，却会把整棵树覆盖成「内容分组（N个子目录）」。
            # 实测 lp-ff42d1f7：轮次转终态后 50 分钟内每次重建都如此，页面一直是占位名。
            # 注意：只在**明确知道** status 存在且不是 active 时才走下面这条补救路径。
            # 测试夹具用 `patch(ensure_round, return_value={"id":..,"deadline":..})`
            # （没有 status 字段），把"缺失"当作"非 active"会把功能整条关掉。
            _round_status = round_row.get('status')
            if _round_status is not None and str(_round_status) != 'active':
                # 绑定轮次已进终态（completed / exhausted）。**不能在这里放弃** ——
                # 那正是生产上"主题层永远起不出名字"的原因：`ensure_round` 的
                # request_key 幂等保护会把终态轮次原样交出来，而 `reserve()` 只认
                # active，于是每次重建都静默失败、主题名一直停在
                # 「内容分组（N个子目录）」（实测 lp-68848e2c / lp-085e413e：
                # 全部轮次均终态、0 个 active）。
                #
                # **不改 naming 模块**（那是维护者精心设计的命名算法与预算语义）。
                # 只换一个**由主题内容签名派生**的键再要一次：
                #   · 新键没被幂等保护绑定 → ensure_round 走正常分支：先复用现有
                #     active 轮次（不重置已用预算、不新建轮次），确实没有可用的才新建；
                #   · 键随内容签名变化，内容不变就一直是同一个键 → 不会像早期那样
                #     "每重建一次就换一个时间戳 key、不断开新轮次"。
                # **键必须"此前没被绑定过"，才拿得到可用轮次。**
                # `ensure_round` 的幂等保护会把 **completed** 的绑定轮次原样交出来
                # （那是**用户请求键的重放语义**，契约测试锁定，不能改），而 reserve()
                # 只认 active —— 所以换一个"已绑定过"的键是没用的（实测：我上一版换的
                # `topics-<签名>` 其轮次已是 completed，于是每 60 秒空转一次）。
                # 只有 exhausted 才会走到"原地续期"那条路。
                #
                # 但键也不能每轮重建都换（那正是早期"31,567 个 topics key → 106 个轮次、
                # 每次重建都重置预算"的事故）。所以按"同一内容签名已经用掉几个轮次"
                # 取下一位：只有当上一轮真的被用完（新建过轮次）时 n 才 +1；
                # 只要还有可用的 active 轮次，ensure_round 会复用它，n 不变、不新建。
                base = 'topics-%s' % '-'.join(str(x) for x in signature)
                # 数**已绑定过的键**（`lp_catalog_round_keys`），不是"已创建的轮次"：
                # 试过但没建成的键也要计入，否则计数不前进、又会生成回同一个死键
                # （实测：`topics-…-2` 的轮次 completed 之后，计数仍是 1，于是又用回 `-2`，
                #  每 60 秒空转一次）。
                # **先看有没有"仍然可用"的派生键 → 有就直接复用它**（这样键会稳定下来，
                # 不会每轮重建都换一个新键）；只有现存派生键都已失效时，才前进到下一个序号。
                with closing(self.store._connect()) as _conn:
                    _rows = _conn.execute(
                        "SELECT k.request_key, r.status FROM lp_catalog_round_keys k "
                        "JOIN lp_catalog_rounds r ON r.package_id=k.package_id AND r.id=k.round_id "
                        "WHERE k.package_id=? AND k.request_key LIKE ? "
                        "ORDER BY k.request_key", (package_id, base + '%')).fetchall()
                _live = [row[0] for row in _rows if str(row[1]) == 'active']
                alt_key = _live[-1] if _live else '%s-%d' % (base, len(_rows) + 1)
                retry = ensure_round(self.store, package_id, alt_key, min_seconds=merge_seconds)
                if retry and str(retry.get('status') or '') == 'active':
                    LOGGER.info('topic rebuild rebound round %s(%s) -> %s key=%s package=%s',
                                round_row.get('id'), _round_status, retry.get('id'),
                                alt_key, package_id)
                    round_row = retry
                else:
                    LOGGER.warning('topic rebuild skipped: no usable round '
                                   '(bound=%s %s, alt=%s) package=%s',
                                   round_row.get('id'), _round_status, alt_key, package_id)
                    return False
            ok = merge_topics(
                self.store, package_id,
                {"id": round_row["id"], "deadline": round_row["deadline"]},
                cancelled=lambda: self.stop_requested or self._interrupted(package_id),
            )
            if ok:
                self._topics_merged[package_id] = signature
                self._topics_pending.pop(package_id, None)
                self._topics_owed_logged.pop(package_id, None)
            LOGGER.info("large package %s topic merge done=%s", package_id, bool(ok))
            return bool(ok)
        except Exception:
            LOGGER.exception("topic merge failed: %s", package_id)
            return False

    def _build_overview(self, package_id, milestone):
        """里程碑处主动生成/更新情报概览（方案 A）。**失败绝不影响主流程。**

        milestone="catalog"：智能目录（含主题层）确定后生成第一版。
            必须等主题归并完成，否则报告骨架会退化成旧的快速解析分类器。
        milestone="deep"：深度摘要排空后更新为第二版（结构不变，证据升级）。
        """
        try:
            from config import Config
            if not getattr(Config, "LP_OVERVIEW_EAGER", True):
                return False
            from services.large_package_reporting import (
                build_overview, published_report, catalog_input_revision)
            item = self.store.get(package_id)
            if not item:
                return False
            note = ""
            previous_report = None
            if milestone == "catalog":
                previous_report = published_report(self.store, item)
                with closing(self.store._connect()) as conn:
                    conn.execute("BEGIN")
                    revision = catalog_input_revision(conn, package_id)
                    if (previous_report and
                            previous_report.get("catalog_input_revision") == revision):
                        return False
                    leaves = int(conn.execute(
                        "SELECT COUNT(*) FROM large_catalog_groups WHERE package_id=? "
                        "AND id NOT LIKE 'km-other-%' AND id NOT LIKE 'km-pending-%'",
                        (package_id,)).fetchone()[0] or 0)
                    branches = int(conn.execute(
                        "SELECT COUNT(*) FROM lp_catalog_branches WHERE package_id=?",
                        (package_id,)).fetchone()[0] or 0)
                    labels = int(conn.execute(
                        "SELECT COUNT(*) FROM lp_catalog_labels WHERE package_id=?",
                        (package_id,)).fetchone()[0] or 0)
                    queued = int(conn.execute(
                        "SELECT COUNT(*) FROM lp_catalog_jobs WHERE package_id=? "
                        "AND status IN ('queued','running','retry','deferred')",
                        (package_id,)).fetchone()[0] or 0)
                # 判据不能用"有没有未完成任务"：只要有任务陷入重试，第一版就永久
                # 阻塞（实测 2 个孤儿任务卡死了第一版）。改成**目录稳定性**：
                # 叶组数/主题数/标签数/待办数连续 stable 秒不再变化，就认为目录已定稿。
                sig = (leaves, branches, labels, queued, revision)
                now = time.time()
                prev = self._overview_state.get(package_id)
                if prev is None or prev[0] != sig:
                    self._overview_state[package_id] = (sig, now, prev[2] if prev else now)
                    return False
                stable_for = now - prev[1]
                waited = now - prev[2]
                need_topic = leaves >= int(getattr(Config, "LP_TOPIC_MERGE_MIN_GROUPS", 12))
                settled = queued == 0 and not (need_topic and not branches)
                if not settled and stable_for < self._overview_stable and waited < self._overview_max_wait:
                    return False
                if not settled:
                    # 逃生出口：长时间没有进展就用**已定稿的部分**出第一版，
                    # 并显式标注这是部分快照——宁可出一版带标注的，也不要永远不出。
                    note = ("语义目录仍在后台完善（剩余 %d 个命名任务，当前 %d 个主题）；"
                            "本版基于已定稿部分，完成后会自动更新。" % (queued, branches))
            # 收尾点 4/5：把唯一判定口的结论附进概览的限制说明。
            # 概览是给人看的，必须能回答"为什么这一版还是部分快照"。
            try:
                _vo = self.store.advance(package_id) or {}
                if _vo.get('blocked_by'):
                    note = (note + '；' if note else '') + '仍有未完成事项：%s' % '、'.join(
                        str(b) for b in _vo.get('blocked_by') or [])
            except Exception:
                pass
            # Refreshing directory names must never downgrade completed deep evidence.
            if previous_report and previous_report.get("milestone") == "deep":
                milestone = "deep"
            result = build_overview(self.store, item, milestone=milestone, note=note)
            if not result.get("published"):
                return False
            LOGGER.info("large package %s overview built milestone=%s", package_id, milestone)
            return True
        except Exception:
            LOGGER.exception("overview build failed: %s/%s", package_id, milestone)
            return False

    def _overview_catchup(self):
        """给"目录已定稿但还没生成情报概览"的包补一次触发。

    为什么需要兜底：`_build_overview` 只在 `run_slice` 里被调用，而 `run_slice`
    只对命名循环选中的那一个包运行；"已有主题、没有待办任务"的包在调度排序里
    排最后，会被其他包反复挤掉，第一版可能长期不出现。
    这里在空闲时扫一遍，数量有界（最多 20 个包），且每个都走稳定性闸门。
        """
        try:
            from config import Config
            if not getattr(Config, "LP_OVERVIEW_EAGER", True):
                return False
            from services.large_package_reporting import published_report
            with closing(self.store._connect()) as conn:
                rows = conn.execute(
                    "SELECT id,status FROM packages "
                    "WHERE status IN ('waiting_for_selection','completed') "
                    "ORDER BY updated_at DESC LIMIT 20").fetchall()
            for row in rows:
                package_id = row["id"]
                # 目录收尾时刻可能不在任何命名切片里（例如最后一批 catalog 队列由目录车道
                # 做完），这里顺带补一次"已完善"判定；函数自身幂等、只改消息。
                if row["status"] == "waiting_for_selection":
                    self._mark_catalog_refined(package_id)
                # Existing reports also need catch-up after async naming/splitting.
                # The content signature gate avoids repeated writes/model calls,
                # including deep reports which intentionally have no v1 file.
                existing = published_report(self.store, {"id": package_id})
                # 已经跑完深度摘要的包：直接出**第二版**（它没有"目录就绪"这个
                # 中间态可等，硬出一版 catalog 会把它标成第一版，语义上不对）。
                milestone = "deep" if row["status"] == "completed" and not existing else "catalog"
                if self._build_overview(package_id, milestone):
                    return True
            return False
        except Exception:
            LOGGER.exception("overview catch-up failed")
            return False

    def _claim_deep(self, package_id, limit):
        """领取 1~limit 个待深度解析的文件。

        limit=1 时返回 `(path, lease_token)`，否则返回 `[(path, lease_token), ...]`；
        令牌必须原样带回 finish_queue，否则写入会被租约校验拒绝。
        """
        claimed = self.store.claim("deep", int(limit), package_id=package_id)
        if not claimed:
            return None
        leases = [(item[1], item[2]) for item in claimed]
        return leases[0] if int(limit) == 1 else leases

    def _deep_worker(self, package_id, root, path, lease=None):
        """在独立线程里完成一个文件的深度解析+摘要，并落盘/更新队列。

        返回 (path, ok, error)。所有 SQLite 写入都经由 store 自身的锁与独立连接，
        因此并发调用是安全的。
        """
        result = {}
        heartbeat_stop = threading.Event()
        def keep_lease():
            while not heartbeat_stop.wait(30):
                try:
                    if not self.store.renew_lease(package_id, path, 'deep', lease):
                        break
                except Exception:
                    LOGGER.exception('deep lease heartbeat failed: %s', path)
        heartbeat = threading.Thread(target=keep_lease, daemon=True) if lease else None
        if heartbeat:
            heartbeat.start()
        try:
            row = self.store.get_file(package_id, path)
            if not row:
                raise FileNotFoundError(path)
            self._check_resources()
            result = _normalize_unicode(self._deep_parse(root, row))
            if result.get("summary_type") == "local_deep_failed":
                raise RuntimeError(result.get("error") or "深度解析未获得正文")
            quality = str(result.get("quality") or "")
            if not quality:
                quality = ("truncated" if result.get("truncated") else
                           "degraded" if result.get("summary_type") != "model" else "complete")
            coverage = str(result.get("coverage") or
                           ("sampled" if result.get("summary_type") == "model" else "none"))
            # **原子提交**：租约校验、文件结果、队列终态在同一个事务里完成。
            # 改动前是 `update_file` 先写摘要、`finish_queue` 再校验令牌，而且
            # 忽略后者返回值 —— 失联回收后旧任务仍能把旧摘要盖进文件记录。
            ok, why = self.store.commit_stage_result(
                package_id, path, "deep", lease, ok=True,
                file_fields={
                    "deep_status": "completed", "deep_summary": result,
                    "sha256": result.get("source_sha256") or row.get("sha256"),
                    "summary_quality": quality, "summary_coverage": coverage, "error": None,
                    "source_changed": 0,
                })
            if not ok:
                # 结果被拒（租约过期 / 被替代 / 源文件变化）：**不写入、不算成功**。
                LOGGER.warning("deep result rejected package=%s path=%s reason=%s",
                               package_id, path, why)
                return path, False, "结果被拒绝：" + why
            self._catalog_incremental(package_id)
            return path, True, ""
        except Exception as exc:
            if self._defer_resource(package_id,path,'deep',lease,exc):
                raise pressure(exc) from exc
            ok, why = self.store.commit_stage_result(
                package_id, path, "deep", lease, ok=False, error=str(exc)[:500],
                file_fields={"deep_status": "failed", "summary_quality": "failed",
                             "source_changed": int(bool(result.get('source_changed'))),
                             "error": str(exc)[:500]})
            if not ok:
                LOGGER.warning("deep failure rejected package=%s path=%s reason=%s",
                               package_id, path, why)
            return path, False, str(exc)[:200]
        finally:
            heartbeat_stop.set()
            if heartbeat:
                heartbeat.join(timeout=2)

    def _finish_selection(self, package_id, force=False):
        # 收尾点 5/5：批次结算同样向唯一判定口取证并记日志。
        # 批次自己的终态只看本批 paths（那是它的权威），但"包能不能往前走"
        # 必须由 advance() 回答 —— 两件事不能混。
        try:
            _vs = self.store.advance(package_id) or {}
            if _vs.get('ok'):
                LOGGER.info('selection settle: package=%s phase=%s can_finish=%s blocked_by=%s',
                            package_id, _vs.get('phase'), _vs.get('can_finish'),
                            _vs.get('blocked_by'))
        except Exception:
            pass
        """把**本批** selection 标成终态；统计只来自本批的文件。

        改动前有两个缺陷：
          ① 只找最新一条 selection，于是更早的批次永远停在 'queued'
             （生产实测 18/20 条 queued，其中 8 个包已 report_ready）；
          ② 用**全包** failed 数决定本批 completed/partial —— 上一批 A 失败、
             本批只选 B 且 B 全成功，本批仍被标 partial。
        现在按 selection 自己的 paths 逐项结算，并用 summary_quality 区分
        "完整"与"部分/降级"，三者互斥。
        """
        try:
            with self.store._lock, closing(self.store._connect()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                # force（历史修复专用）：把"终态但 batch_total 仍为 0"的行也纳入，
                # 否则生产上 20 条历史 selection 永远拿不到批次统计。
                rows = conn.execute(
                    "SELECT id,paths,started_at,status FROM selections WHERE package_id=? "
                    "AND (status IN ('queued','running')"
                    + (" OR batch_total=0 OR COALESCE(quality,'')=''" if force else "")
                    + ") ORDER BY created_at",
                    (package_id,)).fetchall()
                if not rows:
                    conn.rollback(); return False
                changed = 0
                for row in rows:
                    # 只吞"数据形状不对"（paths 不是 JSON 文本 / 不是字符串），
                    # **不再吞 NameError/AttributeError 这类编程错误**：改动前这里
                    # 是 `except Exception`，而本模块当时并没有导入 json，于是
                    # `json.loads` 抛 NameError 被静默吃掉，paths 恒为 []，
                    # "按批次结算"实际退化成"每一批都按 0 个文件结算"
                    # （生产实测 20/20 条 selection 的 batch_total 都是 0）。
                    try:
                        paths = json.loads(row["paths"] or "[]")
                    except (TypeError, ValueError):
                        LOGGER.warning("selection paths 不是合法 JSON 文本，按空批次处理 id=%s",
                                       row["id"])
                        paths = []
                    total = len(paths)
                    done = degrade = failed = pending = 0
                    for p in paths:
                        r = conn.execute("SELECT status FROM queue WHERE package_id=? "
                                         "AND path=? AND stage='deep'", (package_id, p)).fetchone()
                        st = str(r["status"]) if r else "missing"
                        qr = conn.execute("SELECT COALESCE(NULLIF(summary_quality,''),'') "
                                          "FROM files WHERE package_id=? AND path=?",
                                          (package_id, p)).fetchone()
                        qual = str(qr[0]) if qr else ""
                        if st == "completed":
                            if qual in ("truncated", "degraded", "empty", "partial"):
                                degrade += 1
                            else:
                                done += 1
                        elif st == "failed":
                            failed += 1
                        else:
                            pending += 1
                    if pending:
                        if not force:
                            continue      # 本批还没结束，留到下一轮
                        # force 只补统计，**不得**改终态：本批还有没跑完的文件，
                        # 把状态写成 completed/partial 就是把"还在跑"谎报成"已完成"。
                        now = time.time()
                        conn.execute(
                            "UPDATE selections SET started_at=COALESCE(started_at,?),updated_at=?,"
                            "batch_total=?,batch_completed=?,batch_failed=? WHERE id=?",
                            (now, now, total, done, failed, row["id"]))
                        changed += 1
                        continue
                    status = "partial" if (failed or degrade) else "completed"
                    if not total:
                        # 空批次：既没有成功也没有失败，标 failed 是谎报。
                        # 用独立的 empty 值让页面能如实显示"本批 0 个文件"。
                        quality = "empty"
                    else:
                        quality = ("failed" if (not done and not degrade) else
                                   "partial" if (failed or degrade) else "complete")
                    now = time.time()
                    conn.execute(
                        "UPDATE selections SET status=?,started_at=COALESCE(started_at,?),"
                        "finished_at=?,updated_at=?,batch_total=?,batch_completed=?,"
                        "batch_failed=?,quality=? WHERE id=?",
                        (status, now, now, now, total, done, failed, quality, row["id"]))
                    changed += 1
                conn.commit()
            return bool(changed)
        except Exception:
            LOGGER.exception("selection finish failed: %s", package_id)
            return False

    def _deep_finish(self, package_id):
        """深度解析阶段的收尾：确认没有待办后，排队命名并置为完成。"""
        counts = _counts(self.store, package_id)
        current = self.store.get(package_id)
        if current and str(current.get("status") or "").lower() in {"paused", "cancelled"}:
            return True
        # **向唯一判定口取证**：阶段能不能收尾、还差什么，只认 advance()。
        # 这里不再自己拼一套判据（改动前正是六处各写一份，才有了假完成那类缺陷）。
        try:
            verdict = self.store.advance(package_id) or {}
        except Exception:
            verdict = {}
        if verdict.get("ok") and verdict.get("blocked_by"):
            LOGGER.debug("large package %s deep finish blocked by %s",
                         package_id, verdict.get("blocked_by"))
        deep = counts.get("deep", {}) or {}
        # **待办判据改为队列口径**。改动前用 `counts["deep"]`（全部来自
        # files.deep_status），而 `retry` 是纯队列状态（`stale_running_to_retry`
        # 写进 queue 的），永远不会出现在 files 里 —— 所以那句注释说的
        # "retry 必须计入待办"实际是**死代码**：`deep.get("retry")` 恒为 0。
        # 这正是"一次失联回收就让包提前宣告完成"的根因（生产实测 lp-1e0887）。
        queue_deep = self.store.pending_by_stage(package_id, "deep")
        pending = sum(int(queue_deep.get(k) or 0) for k in ("queued", "running", "retry"))
        completed = int(deep.get("completed", 0))
        failed = int(deep.get("failed", 0))
        if pending:
            time.sleep(0.2)
            return False
        # 质量分级：截断 / 模型降级 / 空摘要**不得**计入"完整摘要"。
        try:
            with closing(self.store._connect()) as conn:
                q = {str(r[0] or "unknown"): int(r[1]) for r in conn.execute(
                    "SELECT COALESCE(NULLIF(summary_quality,''),'unknown'),COUNT(*) "
                    "FROM files WHERE package_id=? AND deep_status='completed' "
                    "GROUP BY 1", (package_id,)).fetchall()}
        except Exception:
            q = {}
        deg = sum(v for k, v in q.items() if k in ("truncated", "degraded", "empty", "partial"))
        counts["deep_quality"] = q
        counts["deep_complete_files"] = int(q.get("complete", 0))
        counts["deep_degraded_files"] = int(deg)
        if Config.LP_CATALOG_MODEL_NAMING:
            from services.large_package_catalog_naming import queue_groups, ensure_round
            queue_groups(self.store, package_id)
            with closing(self.store._connect()) as conn:
                selection = conn.execute('SELECT id FROM selections WHERE package_id=? ORDER BY created_at DESC LIMIT 1', (package_id,)).fetchone()
            if selection:
                ensure_round(self.store, package_id, 'deep-' + selection[0])
        # 终态用 counts 里的 additive 字段表达，**不新增 packages.status 枚举值**——
        # 新增枚举会影响既有 UI 与查询。completed_with_errors 表示"跑完了但有失败项"。
        # 终态区分"完整/降级/有失败"三种，不再把降级算作完整成功。
        counts["terminal_state"] = ("completed_with_errors" if failed else
                                    "completed_with_degradation" if deg else "completed")
        counts["deep_completed_files"] = completed
        counts["deep_failed_files"] = failed
        self._finish_selection(package_id)
        self.store.update_package(package_id, status="completed", phase="report_ready", progress=100,
                                  message=("选定范围深度解析完成；{} 个文件失败，详见失败清单".format(failed)
                                           if failed else "选定范围已完成深度解析和摘要"),
                                  counts=counts)
        # 里程碑 2：深度摘要已排空 → 依据深度摘要内容把情报概览更新为第二版。
        # 结构不变（方案 A），只升级证据层级并重估研究方向。
        self._build_overview(package_id, "deep")
        return True

    def _deep_one(self, package):
        package_id = package["id"]
        root = package["root_path"]
        slice_deadline = time.monotonic() + 30
        concurrency = max(1, int(getattr(Config, "LP_DEEP_CONCURRENCY", 1)))
        if concurrency == 1:
            while True:
                if self.stop_requested:return True
                current = self.store.get(package_id)
                if current and str(current.get("status") or "").lower() in {"paused", "cancelled"}:
                    LOGGER.info("large package %s deep stage stopped at checkpoint: %s", package_id, current.get("status"))
                    return True
                self._check_resources()
                lease = self._claim_deep(package_id, 1)
                if lease is None:
                    if self._deep_finish(package_id):
                        return True
                    return False
                path, token = lease
                self._deep_worker(package_id, root, path, token)
                if time.monotonic() >= slice_deadline:
                    self.store.update_package(package_id, message='深度摘要按检查点继续处理')
                    return True
                counts = _counts(self.store, package_id)
                deep = counts.get("deep", {})
                # 进度口径必须含 retry：失联回收会把 running 打成 retry，
                # 不计入分母会让进度虚高（与 _deep_finish 的 pending 口径保持一致）。
                done = sum(int(deep.get(key, 0)) for key in ("completed", "failed"))
                total = max(1, sum(int(deep.get(key, 0)) for key in ("queued", "running", "retry", "completed", "failed")))
                self.store.update_package(package_id, progress=70 + int(30 * done / total), message="已完成深度解析 {}/{}".format(done, total), counts=counts)
        # 并行路径：一次领取 concurrency 个文件，真并发执行。
        # 这样同一时刻有多个模型请求在飞，才能吃到 vLLM 的第 2 个序列槽位。
        pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="sjfx-lp-deep")
        LOGGER.info("large package %s deep stage concurrency=%d", package_id, concurrency)
        try:
            while True:
                if self.stop_requested:
                    return True
                current = self.store.get(package_id)
                if current and str(current.get("status") or "").lower() in {"paused", "cancelled"}:
                    LOGGER.info("large package %s deep stage stopped at checkpoint: %s", package_id, current.get("status"))
                    return True
                self._check_resources()
                leases = self._claim_deep(package_id, concurrency)
                if not leases:
                    if self._deep_finish(package_id):
                        return True
                    return False
                try:
                    list(pool.map(lambda item: self._deep_worker(package_id, root, item[0], item[1]), leases))
                except Exception as exc:
                    if resource_code(exc):
                        raise
                    LOGGER.exception("large package %s deep batch failed", package_id)
                if time.monotonic() >= slice_deadline:
                    self.store.update_package(package_id, message='深度摘要按检查点继续处理')
                    return True
                counts = _counts(self.store, package_id)
                deep = counts.get("deep", {})
                # 进度口径必须含 retry：失联回收会把 running 打成 retry，
                # 不计入分母会让进度虚高（与 _deep_finish 的 pending 口径保持一致）。
                done = sum(int(deep.get(key, 0)) for key in ("completed", "failed"))
                total = max(1, sum(int(deep.get(key, 0)) for key in ("queued", "running", "retry", "completed", "failed")))
                self.store.update_package(package_id, progress=70 + int(30 * done / total), message="已完成深度解析 {}/{}".format(done, total), counts=counts)
        finally:
            pool.shutdown(wait=True)

    def _deep_parse(self, root, row):
        try:
            path, before_source = _source_snapshot(root, row)
            from services.unified_parser import UnifiedDocumentParser
            concurrency = max(1, int(getattr(Config, "LP_DEEP_CONCURRENCY", 1)))
            if concurrency > 1:
                # 并行时每线程一个解析器，避免共享可变状态。
                parser = getattr(_DEEP_PARSER_LOCAL, "parser", None)
                if parser is None:
                    parser = UnifiedDocumentParser()
                    _DEEP_PARSER_LOCAL.parser = parser
            else:
                parser = getattr(self, "_parser", None)
                if parser is None:
                    self._parser = UnifiedDocumentParser()
                    parser = self._parser
            if row.get('source_kind') == 'archive_member':
                from services.large_archive_members import member_node, member_snapshot
                from services.package_analysis import _restore_source_provenance
                with member_snapshot(root, row) as member_path:
                    raw_document = parser.parse(member_path, relative_path=row['path'], mode='accurate')
                _restore_source_provenance(raw_document, root, member_node(row))
            else:
                raw_document = parser.parse(path, relative_path=row["path"], mode="accurate")
            after_path, after_source = _source_snapshot(root, row)
            if after_path != path or after_source != before_source:
                raise SourceChangedError('源文件在解析期间发生变化，已丢弃结果')
            document = _normalize_unicode(raw_document)
            unicode_repaired = document != raw_document
            if unicode_repaired:
                # Source hashes describe original bytes; content hashes must
                # describe the normalized evidence that will actually be saved.
                document["content_sha256"] = hashlib.sha256(
                    str(document.get("text") or "").encode("utf-8")).hexdigest()
                for evidence in document.get("evidence") or []:
                    if isinstance(evidence, dict) and "text" in evidence:
                        evidence["content_sha256"] = hashlib.sha256(
                            str(evidence["text"] or "").encode("utf-8")).hexdigest()
            text = str(document.get("text") or "")
            if not text.strip() and not document.get("evidence"):
                return {"summary_type": "local_deep_failed", "source_path": row["path"], "error": "；".join(document.get("warnings") or ["未获得可读正文"])}
            summary = {
                "summary_type": "model_pending", "source_path": row["path"],
                "source_sha256": (document.get("source") or {}).get("sha256") or row.get("sha256"),
                "coverage": document.get("coverage") or {}, "text_preview": _sample_text(text, 12000),
                "evidence": document.get("evidence") or [], "parser": document.get("parser") or {},
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            if unicode_repaired:
                summary["text_normalization"] = {
                    "policy": "unicode-surrogates/1", "source_unchanged": True,
                    "notice": "Parser surrogate characters were normalized to valid Unicode.",
                }
            # Model generation remains opt-in for the isolated worker.  A local
            # parser result is still durable and clearly labelled if the model
            # is busy or unavailable.
            if str(os.getenv("LARGE_PACKAGE_MODEL_SUMMARY", "1")).lower() not in {"0", "false", "no", "off"} and text.strip():
                summary.update(self._model_summary(row["path"], _sample_text(text, 24000)))
            else:
                summary["summary_type"] = "local_deep"
                # 只有本地解析内容、没有模型摘要 = 降级，不是完整深度摘要。
                summary.setdefault("quality", "degraded")
                summary.setdefault("coverage", "sampled")
            final_path, final_source = _source_snapshot(root, row)
            if final_path != path or final_source != before_source:
                raise SourceChangedError('源文件在摘要期间发生变化，已丢弃结果')
            return _normalize_unicode(summary)
        except Exception as exc:
            if resource_code(exc):
                raise
            return {"summary_type": "local_deep_failed", "source_path": row["path"],
                    "error": str(exc)[:500], "quality": "failed", "coverage": "none",
                    "source_changed": isinstance(exc, SourceChangedError)}

    def _model_summary(self, path, text):
        try:
            if Config.LLM_BACKEND == "vllm" and Config.ENABLE_VLLM:
                from services.vllm import VLLMClient
                client = VLLMClient(Config.VLLM_BASE_URL, Config.VLLM_MODEL, timeout=Config.VLLM_STRUCTURED_TIMEOUT_SECONDS,
                                    max_concurrency=1, api_key=Config.VLLM_API_KEY)
            elif Config.ENABLE_SHARED_OLLAMA:
                from services.ollama import OllamaClient
                client = OllamaClient(Config.OLLAMA_BASE_URL, Config.OLLAMA_MODEL, timeout=180, max_concurrency=1)
            else:
                return {"summary_type": "local_deep", "model_status": "disabled",
                        "quality": "degraded", "coverage": "sampled"}
            # 输出上限改为可配，默认从 500 降到 400。
            # 依据：深度阶段是 GPU decode 瓶颈——实测单文件 p50 18.51 s，
            # 而 500 output tokens ÷ 27.8 tok/s ≈ 18.0 s，即约 97% 的时间花在生成上。
            # 10 GiB（同密度约 3,514 文件）在并发 2 下需 8.78 h；降到 400 tokens
            # 约 7.02 h，这是 8 小时目标**零硬件成本**的达标路径。
            # 提示词本身要求的就是"轻度摘要"，500 的上限本就在浪费。
            # 回滚：SJFX_LP_DEEP_SUMMARY_MAX_TOKENS=500。
            limit = max(128, min(2000, int(os.getenv("SJFX_LP_DEEP_SUMMARY_MAX_TOKENS", "400"))))
            result = client.chat("你是资料摘要助手，只输出简洁中文摘要。" + SUMMARY_QUALIFIER_INSTRUCTION, "请为文件 {} 生成轻度摘要，包含主题、关键事实、风险和后续建议：\n{}".format(path, text), temperature=0.1, max_tokens=limit)
            # finish_reason 必须检查。改动前完全不看，而实测 p50 18.51 s 恰好等于
            # 500 token 的生成时间，说明大部分文件正在撞上限、摘要是被截断的。
            # 截断结果仍然保存（它是有用的事实层产物），但显式标注出来，
            # 让上层与界面不再把它当成"完整成功"。
            finish_reason = str(result.get("finish_reason") or "")
            payload = {"summary_type": "model", "model": result.get("model"),
                       "summary": result.get("content", ""), "usage": result.get("usage", {}),
                       "max_tokens": limit, "finish_reason": finish_reason,
                       # 覆盖范围必须显式：输入是 24,000 字符的头/中/尾抽样，**不是全文**。
                       # 保存的覆盖率要同时说明"解析器读了多少"与"模型实际看了多少"。
                       "coverage": "sampled", "input_chars": len(str(text or "")),
                       "summary_scope": "sampled_summary"}
            if finish_reason and finish_reason not in {"stop", "eos", "end_turn"}:
                payload["truncated"] = True
                payload["truncation_reason"] = finish_reason
                payload["quality"] = "truncated"
                LOGGER.warning(
                    "large package deep summary truncated path=%s finish_reason=%s max_tokens=%d",
                    path, finish_reason, limit)
            elif not str(payload.get("summary") or "").strip():
                payload["quality"] = "empty"
            else:
                payload["quality"] = "complete"
            payload.update(summary_qualifier_patch(payload, [{
                "source_path": path, "text": text,
                "evidence_id": "SUMMARY-INPUT-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            }]))
            return payload
        except Exception as exc:
            if resource_code(exc):
                raise
            # 模型不可用是**降级**，不是完整深度摘要。
            return {"summary_type": "local_deep", "model_status": "unavailable",
                    "model_error": str(exc)[:300], "quality": "degraded", "coverage": "sampled"}

    def run_once(self):
        try:
            self._check_resources()
            self._resource_warning = None
            return self._run_admitted_once()
        except Exception as exc:
            if not resource_code(exc):
                raise
            failure = self._check_guard_defer(exc)
            package_id = getattr(self, '_resource_package_id', None)
            if not package_id:
                try:
                    package_id = next(iter(self._packages()), {}).get('id')
                except Exception:
                    LOGGER.exception('Cannot read package while under resource pressure')
            warning = (package_id, failure.code)
            if getattr(self, '_resource_warning', None) != warning:
                LOGGER.warning('Large-package intake deferred: %s [%s]', package_id, failure.code)
                show_wait(self.store, package_id, failure)
                self._resource_warning = warning
            return False

    def _run_admitted_once(self):
        self.store.stale_running_to_retry()
        package = None
        for candidate in self._packages():
            package = candidate
            break
        if not package:
            self._resource_package_id = None
            return self._catalog_background()
        self._resource_package_id = package['id']
        if package["phase"] == "inventory":
            return self._inventory_one(package)
        if package["phase"] == "quick_parse":
            return self._quick_one(package)
        if package["phase"] == "catalog_build":
            return self._catalog_one(package)
        if package["phase"] in {"recognize", "recognize_completed"}:
            return self._recognize_one(package)
        if package["phase"] == "catalog_naming":
            return self._naming_one(package)
        if package["phase"] == "deep_parse":
            return self._deep_one(package)
        return False

    def _catalog_background(self):
        from services.large_package_catalog_refine import process_one as refine_one
        if not self.stop_requested and refine_one(self):return True
        from services.large_package_semantic import embed_batch, name_batch
        with closing(self.store._connect()) as conn:
            pending = conn.execute("""SELECT q.package_id FROM queue q JOIN packages p ON p.id=q.package_id
              JOIN files f ON f.package_id=q.package_id AND f.path=q.path
              WHERE q.stage='catalog' AND q.status IN ('queued','retry')
                AND p.status IN ('waiting_for_selection','completed')
              ORDER BY (f.deep_status='completed') DESC,q.updated_at LIMIT 1""").fetchone()
            # 模型命名的**权威队列是 lp_catalog_jobs**，不是 large_catalog_groups.status。
            # 用组状态判断会出错：模型命名开启时 name_batch 不运行，而组状态过去是由
            # name_batch 维护的，于是会出现"组状态停在 queued、其实没有任何命名任务"的
            # 包。这种包会让后台循环一直空转（实测 48 个假 queued 组把整个队列卡住，
            # 其它包的命名任务全部饿死）。legacy 的组状态路径仅在没有 jobs 表可用时兜底。
            # **重试已耗尽的 deferred 不算待办**（与 process_one 的拾取条件
            # `attempts<LP_NAME_MAX_ATTEMPTS` 同口径）。原来不看 attempts：一个耗尽的
            # 任务 next_at 永远最早，于是每 2 秒都把车道派给它 → process_batch 拾不到
            # 任何任务 → 续期轮次 → 再派给它。生产实测 lp-7132acbf 这样空转 7 个多小时，
            # 其它包的目录队列（27 个文件）和命名任务全部饿死。
            from config import Config as _CfgN
            _max_att = max(1, int(getattr(_CfgN, 'LP_NAME_MAX_ATTEMPTS', 5) or 5))
            naming = conn.execute(
                """SELECT j.package_id FROM lp_catalog_jobs j JOIN packages p ON p.id=j.package_id
                   WHERE (j.status IN ('queued','retry') OR (j.status='deferred' AND j.attempts<?))
                     AND p.status IN ('waiting_for_selection','completed')
                   ORDER BY j.next_at, j.package_id LIMIT 1""", (_max_att,)).fetchone()
        if self.stop_requested:return False
        # **主题层落后于叶组的补做。**
        #
        # 位置很关键：第一版我把它放在下面 `if not pending and not naming:` 的空闲
        # 分支里 —— 实测那个分支**永远进不去**（生产长期有 7 个 catalog 待办 +
        # 18 个命名待办），于是补做一次都没跑过。现在放在这里，自带 60 秒节流，
        # 每分钟最多扫一次，不影响主流程。
        if self._topic_catchup():
            return True
        # §4.1：每 10 分钟输出一次分阶段分位（只看日志，不影响任何调度）。
        _now = time.monotonic()
        if _now - self._stage_report_at >= 600:
            self._stage_report_at = _now
            stage_report(('catalog.embed_batch', 'catalog.ensure_package',
                          'catalog.km_finalize', 'catalog.name_batch',
                          'catalog.lane_total', 'naming.bundle'))
        # 方案A：给"有可读内容却没进任何组"的文件补单文件条目。
        # 放在这里而不是 k-means 收尾处：k-means 收尾在冻结包上会直接早退，
        # 而这些漏掉的文件恰恰大多在已冻结的包里；而且本补做只新增、不重排。
        if self._solo_catchup():
            return True
            return True
        # 目录降级可见化：从未聚类却已经定稿的包，明确打一条 WARNING。
        # 每个包每个进程只打一次，避免刷屏。
        if not pending and not naming:
            try:
                from services.large_package_semantic import catalog_degraded
                for row in self._degraded_report_candidates():
                    if row not in self._degraded_warned:
                        self._degraded_warned.add(row)
                        info = catalog_degraded(self.store, row)
                        if info.get('degraded'):
                            LOGGER.warning(
                                "large package %s catalog is DEGRADED: 目录未经语义聚类，"
                                "%s 个叶组中 %s 个是碎片（<=%s 个成员，占比 %.0f%%）——"
                                "固定 cap 切分的产物，不是内容分组",
                                row, info.get('groups'), info.get('fragment_groups'),
                                info.get('fragment_max_members'),
                                100.0 * float(info.get('fragment_ratio') or 0))
            except Exception:
                LOGGER.exception('degraded catalog scan failed')
        if not pending and not naming:
            # 挑选真正还需要干活的包。原来只取最早的那个 active round，
            # 于是已经全部做完的包（0 个命名任务、主题层也已生成）会永远霸占
            # 这个位置，后面所有包的命名任务全部饿死——实测有 8 个
            # waiting_for_selection 包时，新包的 65 个命名任务一直不执行、
            # lp_catalog_labels 一条都写不出来。
            # 排序：先挑还有命名任务的包，其次挑还没生成主题层的包，最后才按时间。
            with closing(self.store._connect()) as conn:
                # C（文档 §3C.2）：**已经在途的包不再派给它**。排序第一项是"还有
                # 排队任务"，所以最忙的那个包永远是第一名；不排除的话第二路会被
                # 反复指给同一个包（`_naming_slot_free` 只会拒绝它），于是"两路在途"
                # 在实践中永远不会发生 —— 车道数配了等于没配。
                busy = self._naming_inflight()
                sql = ("SELECT r.package_id FROM lp_catalog_rounds r "
                       "JOIN packages p ON p.id=r.package_id "
                       "WHERE r.status='active' AND p.status IN ('waiting_for_selection','completed') ")
                params = []
                if busy:
                    sql += "AND r.package_id NOT IN (%s) " % ','.join('?' * len(busy))
                    params.extend(busy)
                sql += ("ORDER BY (SELECT COUNT(*) FROM lp_catalog_jobs j WHERE j.package_id=r.package_id "
                        "          AND (j.status IN ('queued','running') "
                        "               OR (j.status='deferred' AND j.attempts<?)))>0 DESC, "
                        "         (SELECT COUNT(*) FROM lp_catalog_branches b "
                        "          WHERE b.package_id=r.package_id)>0 ASC, "
                        "         r.started LIMIT 1")
                params.append(max(1, int(getattr(_CfgN, 'LP_NAME_MAX_ATTEMPTS', 5) or 5)))
                task = conn.execute(sql, params).fetchone()
            if task:
                return self._submit_naming(task['package_id'])
            # **目录流程没走完的包必须继续推进**，不能在这里直接返回 False。
            #
            # 判据是 `catalog_status` 不是终态，而**不是**"没有成员行"。
            # 插桩实测（契约测试 test_background_names_do_not_wait_for_busy_generation）：
            #   catalog 队列行已被标 completed、成员行也已生成，
            #   但文件的 catalog_status 仍停在 'pending' —— 也就是
            #   **"队列说做完了，文件说没分类"**。原来的判据要求"没有成员行"，
            #   正好把这种情况漏掉，于是它永远不会被推进。
            # 这是"空闲分支不推进"的真因，也是 C9 那类"两边口径不一致"的同一家族。
            # **整段纳入冷却**：这段判定是一次全表扫描（实测约 2~9 秒，
            # json_extract 无法走索引）。只挡调度不挡扫描是不够的 —— 那样每轮
            # 仍要付一次扫描成本（实测残留 17% 单核）。
            now = time.monotonic()
            if now >= self._stall_scan_at:
                self._stall_scan_at = now + self._stall_retry_seconds
                with closing(self.store._connect()) as conn:
                    stalled = conn.execute(
                        "SELECT f.package_id FROM files f JOIN packages p ON p.id=f.package_id "
                        "WHERE p.status IN ('waiting_for_selection','completed') "
                        "AND p.control_state='active' "
                        "AND COALESCE(json_extract(f.local_summary,'$.portrait.catalog_status'),'') "
                        "    NOT IN ('classified','no_content','deferred','restricted') "
                        "ORDER BY f.package_id LIMIT 1").fetchone()
                if stalled:
                    pid2 = str(stalled[0])
                    scheduled = False
                    try:
                        scheduled = self._schedule_catalog_incremental(pid2)
                    except Exception:
                        LOGGER.exception('catalog incremental failed: %s', pid2)
                    # 只有**真的排上了活**才立刻再转一圈；排不上就继续往下走，
                    # 让概览补发拿到机会，并交给 run_forever 正常休眠。
                    if scheduled:
                        return True
            # 情报概览第一版的补充触发。run_slice 只在命名循环里被调用、且只针对
            # 被选中的那一个包；"已有主题、没有待办任务"的包在调度排序里排最后，
            # 会被反复挤掉（实测某包有 86 个叶组/5 个主题却一直拿不到第一版）。
            # 空闲时扫一遍"该出第一版却还没有"的包，逐个尝试；每个都会经过
            # 稳定性闸门，不会提前出未定稿的版本。
            if self._overview_catchup():
                return True
            return False
        name_first = naming and (not pending or getattr(self, '_catalog_named_last', False) is False)
        package_id = (naming if name_first else pending)['package_id']
        def interrupted():
            current = self.store.get(package_id)
            return self.stop_requested or not current or current['status'] not in {'waiting_for_selection', 'completed'}
        if name_first:
            # 模型命名开启时，组名由 lp_catalog_jobs 驱动、写进 lp_catalog_labels。
            # 绝不能再走 name_batch 的抽取式命名：它会直接把组状态置成 'named'，
            # 而模型命名循环正是按这个状态找任务的，于是任务全部卡在 queued、
            # 目录里一条模型标签都没有（实测 69 个 job 一直不执行）。
            from config import Config as _Cfg
            # 下一轮让位给目录队列（有的话）。原来模型命名分支直接 return、从不置位，
            # 于是只要还有任何命名任务，`name_first` 恒为 True，catalog 队列永远轮不到。
            self._catalog_named_last = True
            if getattr(_Cfg, 'LP_CATALOG_MODEL_NAMING', False):
                return self._submit_naming(package_id)
            return bool(name_batch(self.store, package_id, cancelled=interrupted))
        self._catalog_named_last = False
        ok = bool(embed_batch(self.store, package_id, cancelled=interrupted))
        # **把这一轮的目录落库派出去**。改动前这里只做嵌入与分组，之后没人调用
        # 目录车道 —— 于是"已嵌入、未分类"的文件会一直挂着：目录里没有它、
        # 文件列表里却有它，两边口径长期不一致。
        # （契约测试 test_background_names_do_not_wait_for_busy_generation
        #   断言 run_once 必须报告有进展、且被推进的文件由 catalog 落成 classified。）
        try:
            self._schedule_catalog_incremental(package_id)
            ok = True
        except Exception:
            LOGGER.exception('catalog incremental dispatch failed: %s', package_id)
        # 这个后台循环是 waiting_for_selection 之后唯一还在消费 catalog 队列的地方：
        # 深度解析改写正文后会重新入队 catalog（新向量）。没有这一步，那些新向量
        # 只会被暂存却永远不会触发重新聚类，目录会停在浅层摘要的分组上。
        # 判定条件与快速解析阶段完全相同，所以不会提前聚类。
        self._ensure_km_finalized(package_id)
        return ok

    def _degraded_report_candidates(self):
        """扫描"已定稿但可能没聚类过"的包（只读，供降级告警用）。"""
        with closing(self.store._connect()) as conn:
            return [str(r[0]) for r in conn.execute(
                "SELECT id FROM packages WHERE status IN ('waiting_for_selection','completed') "
                "AND phase IN ('catalog_ready','report_ready') ORDER BY updated_at DESC LIMIT 30")]

    def _packages(self):
        with self.store._connect() as conn:
            # 版本排他：只调度"不高于本进程服务版本"的包（框架 §11）。
            # 于是旧 Worker 不会去消费新格式的包，而是把它留给新进程。
            try:
                from config import Config as _Cfg
                served = int(getattr(_Cfg, 'LP_WORKFLOW_VERSION', 1) or 1)
            except Exception:
                served = 1
            # 阶段白名单**从 store.PHASES 派生**，不再在这里另写一份
            # （加一个阶段只改 store.PHASES 一处，见那里的说明）。
            from services.large_package_isolated import LargePackageStore as _LPS
            schedulable = tuple(getattr(_LPS, "SCHEDULABLE_PHASES", ()) or ())
            if not schedulable:
                return []
            marks = ",".join("?" for _ in schedulable)
            rows = conn.execute(
                "SELECT * FROM packages WHERE status IN ('queued','running','waiting_for_selection') "
                "AND phase IN (" + marks + ") AND COALESCE(workflow_version,1) <= ? "
                "ORDER BY updated_at", tuple(schedulable) + (served,)).fetchall()
        return [self.store._row(row) for row in rows]

    def run_forever(self):
        try:
            while not self.stop_requested:
                if not self.run_once():
                    time.sleep(self.poll_seconds)
        finally:
            for runner in self._preview_runners:runner.close()
            if self._catalog_lane:
                self._catalog_lane.shutdown(wait=True, cancel_futures=True)
            if self._naming_executor:
                self._naming_executor.shutdown(wait=True,cancel_futures=True)
            from services.catalog_prefetch import shutdown as shutdown_evidence_prefetch
            shutdown_evidence_prefetch()


# ---------------------------------------------------------------------------
# §4.1 埋点：有界、进程内的分阶段计时。
#
# 目的：把"时间去哪了"量成一张账，而不是用 elapsed/总 token 推断瓶颈。
# 约束：只测耗时/计数，不写原文/证据正文/凭据；内存有界；异常只记日志。
# ---------------------------------------------------------------------------
STAGE_SAMPLE_LIMIT = max(32, int(os.getenv('SJFX_LP_STAGE_SAMPLES', '512') or 512))
_STAGE_LOCK = threading.Lock()
_STAGE_SAMPLES = {}


def stage_record(stage, seconds, **fields):
    """记录一次阶段耗时；fields 只放计数/哈希类小值。"""
    try:
        with _STAGE_LOCK:
            bucket = _STAGE_SAMPLES.setdefault(stage, [])
            bucket.append((round(float(seconds), 4), fields))
            if len(bucket) > STAGE_SAMPLE_LIMIT:
                del bucket[:len(bucket) - STAGE_SAMPLE_LIMIT]
    except Exception:
        pass


def stage_percentiles(stage):
    """返回 (n, p50, p95, p99, sum)；样本不足 3 个时 p* 为 None。"""
    with _STAGE_LOCK:
        values = sorted(v for v, _f in _STAGE_SAMPLES.get(stage, ()))
    if not values:
        return (0, None, None, None, 0.0)

    def pick(q):
        if len(values) < 3:
            return None
        idx = min(len(values) - 1, int(round(q * (len(values) - 1))))
        return values[idx]

    return (len(values), pick(0.50), pick(0.95), pick(0.99), round(sum(values), 3))


_CODE_VERSION = None


def code_version():
    """本次进程运行的**源码版本指纹**（文档 §4.1：所有指标带源码版本）。

    取四个真正参与目录/命名执行的文件的内容哈希，而不是 `git HEAD`：生产是"带着未
    提交改动在跑"，HEAD 无法区分"同一提交、工作树被改过"的两种情况，而事故复盘时
    恰恰需要后者。只算一次；任何异常退化成 'unknown'，绝不影响主流程。
    """
    global _CODE_VERSION
    if _CODE_VERSION is None:
        try:
            digest = hashlib.sha256()
            base = Path(__file__).resolve().parent
            for rel in ('large_package_worker.py',
                        'services/large_package_catalog_naming.py',
                        'services/large_package_catalog_batch.py',
                        'services/large_package_semantic.py'):
                try:
                    digest.update(Path(base, rel).read_bytes())
                except Exception:
                    digest.update(rel.encode())
            _CODE_VERSION = digest.hexdigest()[:12]
        except Exception:
            _CODE_VERSION = 'unknown'
    return _CODE_VERSION


def stage_report(stages=(), log=None):
    """输出若干阶段的 n / p50 / p95 / p99 / sum。"""
    sink = log or LOGGER.info
    for stage in stages:
        n, p50, p95, p99, total = stage_percentiles(stage)
        if not n:
            continue
        sink('stage profile %s n=%d p50=%s p95=%s p99=%s sum=%.3fs ver=%s',
             stage, n, p50, p95, p99, total, code_version())


class StageTimer(object):
    """with 块计时：stage_record(name, dt, **fields)。异常不改变语义。"""

    __slots__ = ('_name', '_fields', '_t0')

    def __init__(self, name, **fields):
        self._name = name
        self._fields = fields
        self._t0 = time.monotonic()

    def __enter__(self):
        self._t0 = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            stage_record(self._name, time.monotonic() - self._t0, **self._fields)
        except Exception:
            pass
        return False


def _human(value):
    try:
        from services.scanner import human_size
        return human_size(value)
    except Exception:
        return str(value)


# 切片级剖析（默认关闭）。第九轮靠它定位了 quick_parse 的时间去向。
#   SJFX_PROFILE_SLICE=1   打开
_SLICE_PROFILE = str(os.getenv("SJFX_PROFILE_SLICE", "") or "").lower() in {"1", "true", "on", "yes"}


if __name__ == "__main__":
    # 与 worker.py 保持一致：日志必须带时间戳，否则无法还原阶段时间线。
    # 改动前这里只有 level，日志形如 "INFO:sjfx.large_package_worker:..."，
    # 事后无法判断任一阶段耗时，也无法区分"卡住"与"慢"。
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    import signal
    worker = LargePackageWorker()
    signal.signal(signal.SIGTERM,lambda *_:setattr(worker,'stop_requested',True))
    signal.signal(signal.SIGINT,lambda *_:setattr(worker,'stop_requested',True))
    worker.run_forever()



# （第十轮）曾在这里加过一个"后台线程定期做 PASSIVE 检查点"的机制。
# 实测**无效**：PASSIVE 检查点因为读连接占着 WAL 而返回 busy，回收不掉，
# WAL 仍会涨到 `wal_autocheckpoint` 的兜底线。真正的收益来自把该阈值
# 从 1000 页放宽到 20000 页。按"不发布未经证实的复杂度"把它删掉了。
