"""Isolated streaming large-package workflow.

This module deliberately does not use the standard import task tables.  It
stores a durable manifest and per-file queues in ``large_package.db`` so a
long-running package cannot change the ordinary import workflow.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
from contextlib import closing
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from config import Config
from services.scanner import human_size, is_sensitive_file

LOGGER = logging.getLogger("sjfx.large_package_store")

# 阶段计时（进程内）：package_id → (phase, monotonic, wall_clock)。
# 只用于日志，不参与任何业务判断，因此进程重启后从头开始也无影响。
_PHASE_CLOCK = {}


TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl",
    ".xml", ".html", ".htm", ".log", ".ini", ".cfg", ".yaml", ".yml",
}
ARCHIVE_EXTENSIONS = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".tbz", ".7z", ".rar"}
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,8}")
STOPWORDS = {"the", "and", "for", "with", "from", "this", "that", "数据", "资料", "文件", "分析", "报告"}

# A completed embedding job or a fallback bucket is not a recognition result.
# Keep the same predicate for historical reconciliation and new requests.
_RECOGNITION_RESOLVED_SQL = """(
    COALESCE(f.recognition_status,'') IN ('identified','restricted','unsupported')
    OR (
        (COALESCE(json_extract(COALESCE(f.local_summary,'{}'),'$.portrait.catalog_status'),'')='classified'
         OR (json_extract(f.local_summary,'$.portrait.catalog_status') IN ('pending','grouped')
             AND EXISTS (SELECT 1 FROM large_catalog_members lm
             JOIN large_catalog_groups lg ON lg.package_id=lm.package_id AND lg.id=lm.group_id
             JOIN lp_catalog_labels ll ON ll.package_id=lg.package_id AND ll.group_id=lg.id
             WHERE lm.package_id=f.package_id AND lm.path=f.path AND ll.name<>''
               AND (ll.revision=lg.content_revision OR ll.locked=1))))
        AND EXISTS (SELECT 1 FROM queue cq
                    WHERE cq.package_id=f.package_id AND cq.path=f.path
                      AND cq.stage='catalog' AND cq.status='completed')
        AND EXISTS (SELECT 1 FROM large_catalog_members m
                    JOIN large_catalog_groups g ON g.package_id=m.package_id AND g.id=m.group_id
                    WHERE m.package_id=f.package_id AND m.path=f.path
                      AND m.group_id NOT LIKE 'km-other-%'
                      AND (m.group_id NOT LIKE 'km-pending-%' OR EXISTS (
                          SELECT 1 FROM lp_catalog_labels l WHERE l.package_id=g.package_id
                          AND l.group_id=g.id AND l.name<>''
                          AND (l.revision=g.content_revision OR l.locked=1)))
                      AND COALESCE(m.input_hash,'')<>''
                      AND m.input_hash=json_extract(COALESCE(f.local_summary,'{}'),
                                                   '$.portrait.catalog_input_hash'))
    )
)"""


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(value):
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _loads(value, default=None):
    try:
        return json.loads(value) if value else (default if default is not None else {})
    except (TypeError, ValueError):
        return default if default is not None else {}


def _under_allowed_root(path):
    candidate = Path(path).expanduser().resolve()
    roots = tuple(getattr(Config, "SCAN_ALLOWED_ROOTS", ()) or ())
    if roots and not any(candidate == root or root in candidate.parents for root in roots):
        raise ValueError("目录不在服务器扫描白名单内")
    if not candidate.is_dir():
        raise ValueError("目录不存在或不是文件夹")
    return candidate


class LargePackageStore:
    def __init__(self, db_path=None):
        self.db_path = str(db_path or (Config.DATA_DIR / "large_package.db"))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._catalog_cache = {}
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # **WAL 自动检查点的代价原来落在某一次 commit 上。**
        #
        # 实测（603 个文件的预览阶段，同一份代码）：603 次提交里
        #   p50 = 0.29 ms、p90 = 0.43 ms —— 提交本身几乎免费；
        #   但有 19 次（3.2%）超过 1 秒，最慢 1565 ms，
        #   这 19 次占了**全部提交时间的 83%**。
        # 机制：`wal_autocheckpoint` 默认 1000 页，每次提交约写 30 页，
        # 于是每 ~33 次提交就撞上一次检查点（实测 603/19 = 31.7，吻合）；
        # 触发它的那次 commit 要把页面写回 1.5 GB 的主库并同步，卡约 1.2 秒。
        # 现场还测到 `wal_checkpoint(PASSIVE)` 返回 busy=1 —— 有读连接占着 WAL，
        # 检查点更难做，stall 更久。
        #
        # 改为：连接不自动检查点，由 Worker 在**不持有任何事务的地方**按节流做。
        # 设 `SJFX_LP_WAL_AUTOCHECKPOINT=1000` 可退回原行为。
        conn.execute("PRAGMA wal_autocheckpoint=%d" % _WAL_AUTOCHECKPOINT)
        return conn

    def _init_db(self):
        with self._lock, self._connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS packages (
              id TEXT PRIMARY KEY, root_path TEXT NOT NULL, owner_id TEXT NOT NULL,
              status TEXT NOT NULL, phase TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
              message TEXT NOT NULL DEFAULT '', counts TEXT NOT NULL DEFAULT '{}',
              created_at REAL NOT NULL, updated_at REAL NOT NULL, error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_large_packages_owner ON packages(owner_id, updated_at);
            CREATE TABLE IF NOT EXISTS files (
              package_id TEXT NOT NULL, path TEXT NOT NULL, name TEXT NOT NULL,
              extension TEXT NOT NULL DEFAULT '', size INTEGER NOT NULL DEFAULT 0,
              modified_at_ns INTEGER NOT NULL DEFAULT 0, sha256 TEXT, sensitive INTEGER NOT NULL DEFAULT 0,
              file_type TEXT NOT NULL DEFAULT '', quick_status TEXT NOT NULL DEFAULT 'queued',
              deep_status TEXT NOT NULL DEFAULT 'idle', category TEXT NOT NULL DEFAULT '未分类',
              confidence REAL NOT NULL DEFAULT 0, local_summary TEXT NOT NULL DEFAULT '{}',
              deep_summary TEXT NOT NULL DEFAULT '{}', error TEXT, updated_at REAL NOT NULL,
              PRIMARY KEY(package_id, path)
            );
            CREATE INDEX IF NOT EXISTS idx_large_files_quick ON files(package_id, quick_status);
            CREATE INDEX IF NOT EXISTS idx_large_files_deep ON files(package_id, deep_status);
            CREATE INDEX IF NOT EXISTS idx_large_files_category ON files(package_id, category);
            CREATE TABLE IF NOT EXISTS queue (
              package_id TEXT NOT NULL, path TEXT NOT NULL, stage TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
              leased_at REAL, error TEXT, updated_at REAL NOT NULL,
              PRIMARY KEY(package_id, path, stage)
            );
            CREATE INDEX IF NOT EXISTS idx_large_queue_ready ON queue(stage,status,updated_at);
            CREATE TABLE IF NOT EXISTS selections (
              id TEXT PRIMARY KEY, package_id TEXT NOT NULL, paths TEXT NOT NULL,
              rule TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'queued',
              created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_nodes (
              id TEXT PRIMARY KEY, package_id TEXT NOT NULL, name TEXT NOT NULL,
              parent_id TEXT, paths TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_large_user_nodes_package ON user_nodes(package_id, created_at);
            """)

        # Additive migration confined to large_package.db. Existing task/results
        # remain readable; legacy previews are upgraded only on explicit request.
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
            for name, default in (("topic", ""), ("recognition_status", "legacy"), ("document_role", "")):
                if name not in columns:
                    conn.execute("ALTER TABLE files ADD COLUMN " + name + " TEXT NOT NULL DEFAULT '" + default + "'")
            queue_columns = {row[1] for row in conn.execute("PRAGMA table_info(queue)")}
            if "priority" not in queue_columns:
                conn.execute("ALTER TABLE queue ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            # 租约令牌：每次 claim 生成一个新的随机令牌，写入方必须带着它才能提交
            # 结果。这样"被判失联的旧 worker 晚到并覆盖新结果"不再可能发生。
            if "lease_token" not in queue_columns:
                conn.execute("ALTER TABLE queue ADD COLUMN lease_token TEXT")
            # 选择（深度解析范围）自身的起止时间与终态。
            # 为什么要加：改动前 `selections.status` 永远停在 'queued'——生产实测
            # 18/18 条都是 queued，其中包含已经 report_ready 的包。于是无法回答
            # "这次深析跑完了吗、跑了多久、失败几个"。加列是纯增量迁移，
            # 旧代码读到多出的列不受影响。
            selection_columns = {row[1] for row in conn.execute("PRAGMA table_info(selections)")}
            if "started_at" not in selection_columns:
                conn.execute("ALTER TABLE selections ADD COLUMN started_at REAL")
            # ---- 本次改修新增的列（全部为纯增量迁移，旧代码读到多出的列不受影响）----
            # 选择批次自身的结果统计：本批进度必须只来自本批，不能用全包历史失败数。
            # 改动前 `_finish_selection` 只找最新一条 selection，并按**全包** failed
            # 数标注 —— 上一批 A 失败、本批只选 B 且 B 全成功，本批仍被标 partial。
            for name, ddl in (
                ("finished_at", "REAL"),
                ("catalog_id", "TEXT NOT NULL DEFAULT ''"),        # 绑定提交时的目录版本
                ("batch_completed", "INTEGER NOT NULL DEFAULT 0"),
                ("batch_failed", "INTEGER NOT NULL DEFAULT 0"),
                ("batch_total", "INTEGER NOT NULL DEFAULT 0"),
                ("quality", "TEXT NOT NULL DEFAULT ''"),           # complete/partial/degraded/failed
                ("request_key", "TEXT NOT NULL DEFAULT ''"),       # 幂等键：重复点击返回同一批次
            ):
                if name not in selection_columns:
                    conn.execute("ALTER TABLE selections ADD COLUMN %s %s" % (name, ddl))
            # 包控制态与发布指针：packages.status 降级为兼容投影，不再是业务开关。
            package_columns = {row[1] for row in conn.execute("PRAGMA table_info(packages)")}
            for name, ddl in (
                ("control_state", "TEXT NOT NULL DEFAULT 'active'"),   # active/paused/cancelled
                ("control_generation", "INTEGER NOT NULL DEFAULT 0"),
                ("resume_phase", "TEXT NOT NULL DEFAULT ''"),          # 暂停前的可恢复目标
                ("resume_status", "TEXT NOT NULL DEFAULT ''"),
                ("state_revision", "INTEGER NOT NULL DEFAULT 0"),
                ("published_view_id", "TEXT NOT NULL DEFAULT ''"),
                ("workflow_version", "INTEGER NOT NULL DEFAULT 1"),
            ):
                if name not in package_columns:
                    conn.execute("ALTER TABLE packages ADD COLUMN %s %s" % (name, ddl))
            # 显式租约到期时间：回收按它判断，不再拿普通 updated_at 当心跳。
            if "lease_expires_at" not in queue_columns:
                conn.execute("ALTER TABLE queue ADD COLUMN lease_expires_at REAL")
            # 结果质量：任务"执行成功"只说明产出符合输出契约，质量仍可能是
            # partial/unsupported。两者不能混为一谈（这是 A 组问题的根）。
            file_columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
            for name, ddl in (
                ("summary_quality", "TEXT NOT NULL DEFAULT ''"),   # complete/truncated/degraded/failed/unsupported
                ("summary_coverage", "TEXT NOT NULL DEFAULT ''"),  # full/sampled/none
                ("source_changed", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in file_columns:
                    conn.execute("ALTER TABLE files ADD COLUMN %s %s" % (name, ddl))
            # ---- 版本链与统一调度所需的新表（本轮只创建，旧流程不读它们）----
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS lp_runs(
              run_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, kind TEXT NOT NULL,
              input_version TEXT NOT NULL DEFAULT '', scope_closed INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'queued', result TEXT NOT NULL DEFAULT '{}',
              started REAL NOT NULL DEFAULT 0, finished REAL NOT NULL DEFAULT 0,
              catalog_id TEXT NOT NULL DEFAULT '', evidence_id TEXT NOT NULL DEFAULT '',
              UNIQUE(package_id, kind, input_version));
            CREATE INDEX IF NOT EXISTS lp_runs_pkg ON lp_runs(package_id, kind, status);
            CREATE TABLE IF NOT EXISTS lp_tasks(
              task_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, package_id TEXT NOT NULL,
              stage TEXT NOT NULL, path TEXT NOT NULL DEFAULT '', group_id TEXT NOT NULL DEFAULT '',
              input_revision TEXT NOT NULL DEFAULT '', task_key TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
              lease_token TEXT, lease_expires_at REAL, next_at REAL NOT NULL DEFAULT 0,
              up_to_date INTEGER NOT NULL DEFAULT 0, result_ref TEXT NOT NULL DEFAULT '',
              quality TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
              created REAL NOT NULL DEFAULT 0, updated REAL NOT NULL DEFAULT 0,
              UNIQUE(package_id, task_key));
            CREATE INDEX IF NOT EXISTS lp_tasks_ready ON lp_tasks(package_id, stage, status, next_at);
            CREATE TABLE IF NOT EXISTS lp_catalog_versions(
              catalog_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, run_id TEXT NOT NULL DEFAULT '',
              input_version TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'draft',
              validation TEXT NOT NULL DEFAULT '{}', leaf_count INTEGER NOT NULL DEFAULT 0,
              topic_count INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL DEFAULT 0,
              published REAL NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS lp_catver_pkg ON lp_catalog_versions(package_id, status);
            CREATE TABLE IF NOT EXISTS lp_evidence_snapshots(
              evidence_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, selection_id TEXT NOT NULL DEFAULT '',
              catalog_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'building',
              stats TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS lp_evid_pkg ON lp_evidence_snapshots(package_id, selection_id);
            CREATE TABLE IF NOT EXISTS lp_overview_versions(
              report_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, milestone TEXT NOT NULL DEFAULT '',
              catalog_id TEXT NOT NULL DEFAULT '', evidence_id TEXT NOT NULL DEFAULT '',
              rules_version TEXT NOT NULL DEFAULT '', schema_version TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'building', payload TEXT NOT NULL DEFAULT '',
              digest TEXT NOT NULL DEFAULT '', created REAL NOT NULL DEFAULT 0,
              published INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS lp_ovver_pkg ON lp_overview_versions(package_id, status);
            CREATE TABLE IF NOT EXISTS lp_published_views(
              view_id TEXT PRIMARY KEY, package_id TEXT NOT NULL,
              catalog_id TEXT NOT NULL DEFAULT '', report_id TEXT NOT NULL DEFAULT '',
              evidence_id TEXT NOT NULL DEFAULT '', state_revision INTEGER NOT NULL DEFAULT 0,
              created REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS lp_view_pkg ON lp_published_views(package_id, created);
            CREATE TABLE IF NOT EXISTS lp_workflow_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, package_id TEXT NOT NULL,
              run_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '',
              object TEXT NOT NULL DEFAULT '', old_state TEXT NOT NULL DEFAULT '',
              new_state TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
              source TEXT NOT NULL DEFAULT '', created REAL NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS lp_events_pkg ON lp_workflow_events(package_id, created);
            """)
            # 必须**重新读一次列集**：上面的循环可能已经加过 finished_at，
            # 而 selection_columns 是在那之前取的陈旧快照 —— 用它判断会在
            # 全新数据库上重复 ALTER 并抛 duplicate column name。
            selection_columns = {row[1] for row in conn.execute("PRAGMA table_info(selections)")}
            if "finished_at" not in selection_columns:
                conn.execute("ALTER TABLE selections ADD COLUMN finished_at REAL")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_large_queue_priority ON queue(package_id,stage,status,attempts,priority)")
            # Hot-path indexes for package-scoped queue, deep-finish checks,
            # and catalog telemetry.  Without these, growth in one package
            # makes each worker poll scan rows belonging to every package.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_large_queue_package_ready ON queue(package_id,stage,status,updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_large_files_deep_updated ON files(package_id,deep_status,updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_large_files_topic ON files(package_id,topic)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_large_files_recognition ON files(package_id,recognition_status)")
            conn.commit()
            from services.large_package_topics import init
            init(conn)
            from services.large_package_semantic import init as init_semantic_catalog
            init_semantic_catalog(conn)
            from services.large_package_catalog_naming import init as init_naming
            init_naming(conn)
            # These tables are created by the semantic/naming initializers
            # above, so their indexes must be installed afterwards for fresh
            # databases as well as existing production databases.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_large_catalog_calls_package_status ON lp_catalog_calls(package_id,status,started)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_large_catalog_labels_package_revision ON lp_catalog_labels(package_id,revision,group_id)")

    @staticmethod
    def _row(row):
        if not row:
            return None
        item = dict(row)
        for key in ("counts", "local_summary", "deep_summary", "rule", "paths"):
            if key in item:
                item[key] = _loads(item[key], [] if key == "paths" else {})
        item["sensitive"] = bool(item.get("sensitive"))
        return item

    def create(self, root_path, owner_id="legacy"):
        root = str(_under_allowed_root(root_path))
        package_id = "lp-" + uuid.uuid4().hex
        now = time.time()
        with self._lock, self._connect() as conn:
            # 新包标记为本进程服务的工作流版本（预留排他机制，见 config 注释）。
            try:
                from config import Config as _Cfg
                _wfv = int(getattr(_Cfg, "LP_WORKFLOW_VERSION", 1) or 1)
            except Exception:
                _wfv = 1
            conn.execute("INSERT INTO packages(id,root_path,owner_id,status,phase,workflow_version,"
                         "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                         (package_id, root, owner_id or "legacy", "queued", "inventory", _wfv, now, now))
            # B1 导入幂等（产品口径：**允许重复导入，但必须让用户看到**）：
            # 同一个 root_path 已经有别的包时，把它们的 id/状态一并返回，
            # 由用户决定继续用新包还是回头用旧包。硬拒绝会挡住 demo_data
            # 这类需要反复导入的测试场景（现状 demo_data ×8 / wx ×6）。
            existing = [dict(r) for r in conn.execute(
                "SELECT id,status,phase,created_at FROM packages "
                "WHERE root_path=? AND owner_id=? AND id<>? ORDER BY created_at DESC LIMIT 10",
                (root, owner_id or 'legacy', package_id))]
            if existing:
                conn.execute("UPDATE packages SET message=? WHERE id=?",
                             ("同一目录已有 %d 个历史数据包（可复用）" % len(existing), package_id))
        created = self.get(package_id, owner_id)
        if existing:
            # 附加提示，不改变原有字段（纯增量，旧消费者不受影响）
            created = dict(created or {})
            created["existing_packages"] = existing
        return created

    def get(self, package_id, owner_id=None):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM packages WHERE id=?" + (" AND owner_id=?" if owner_id else ""),
                               (str(package_id), str(owner_id)) if owner_id else (str(package_id),)).fetchone()
        return self._row(row)

    def list_history(self, owner_id=None, query="", limit=8, offset=0):
        """Return lightweight history records for the large-package switcher."""
        where = []
        args = []
        if owner_id:
            where.append("owner_id=?")
            args.append(str(owner_id))
        if query:
            where.append("(root_path LIKE ? OR id LIKE ?)")
            like = "%" + str(query) + "%"
            args.extend([like, like])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        limit = max(1, min(50, int(limit)))
        offset = max(0, int(offset))
        with self._connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM packages" + clause, args).fetchone()[0])
            rows = conn.execute("SELECT * FROM packages" + clause + " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                                args + [limit, offset]).fetchall()
        items = []
        for row in rows:
            item = self._row(row)
            counts = self.counts(item["id"])
            counts_json = item.get("counts") or {}
            phase = item.get("phase") or "inventory"
            status = item.get("status") or "queued"
            if status in {"queued", "running"} and phase == "inventory": label = "scanning"
            elif status in {"queued", "running"} and phase == "quick_parse": label = "preprocessing"
            elif status == "waiting_for_selection": label = "awaiting_selection"
            elif status == "completed": label = "completed"
            else: label = status
            root = item["root_path"]
            items.append({
                "kind": "large", "scan_id": item["id"], "large_package_id": item["id"],
                "name": Path(root).name or root, "root": root,
                "status": label, "large_status": status, "phase": phase,
                "file_count": counts.get("total_files", 0),
                "total_size": counts_json.get("total_bytes", 0),
                "total_size_human": counts_json.get("total_size_human", ""),
                "analysis_ready": status in {"waiting_for_selection", "completed"},
                "usable": status in {"waiting_for_selection", "completed"},
                "created_at": datetime.fromtimestamp(float(item["created_at"]), timezone.utc).isoformat(),
                "updated_at": datetime.fromtimestamp(float(item["updated_at"]), timezone.utc).isoformat(),
                "progress": item.get("progress", 0), "message": item.get("message", ""),
            })
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def update_package(self, package_id, **fields):
        allowed = {"status", "phase", "progress", "message", "counts", "error"}
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return self.get(package_id)
        values["updated_at"] = time.time()
        columns = []
        args = []
        for key, value in values.items():
            columns.append(key + "=?")
            if key == "counts":
                current = self.get(package_id) or {}
                value = {**(current.get("counts") or {}), **value}
            args.append(_json(value) if key == "counts" else value)
        args.append(str(package_id))
        with self._lock, self._connect() as conn:
            guard = ""
            if values.get('status') not in (None, 'paused', 'cancelled'):
                guard = " AND control_state NOT IN ('paused','cancelled')"
            conn.execute("UPDATE packages SET " + ",".join(columns) + " WHERE id=?" + guard, args)
        phase = values.get("phase")
        if phase:
            self._log_phase_transition(package_id, str(phase))
        return self.get(package_id)

    def _log_phase_transition(self, package_id, phase):
        """阶段边界记一条可恢复时间线的事件（纯观测，不改变任何行为）。

        为什么需要：改动前大包 Worker 的日志没有时间戳，且状态库里没有阶段计时，
        `packages.updated_at` 会被重试/进度更新反复改写，`queue.updated_at` 同样
        不是完成时间——于是"100GiB 目录 2 小时"这类目标在事后完全无法归因，
        也无法判断某一步是慢还是卡住。这里在 phase 真正变化时打印上一次阶段的
        持续秒数与墙钟时间，阶段等待/处理时间即可从日志还原。
        """
        key = str(package_id)
        previous, started, _wall = _PHASE_CLOCK.get(key, (None, None, None))
        if previous == phase:
            return
        now_monotonic = time.monotonic()
        now_wall = time.time()
        elapsed = (now_monotonic - started) if started else 0.0
        LOGGER.info(
            "package %s phase %s -> %s | previous_phase_seconds=%.1f | at=%s",
            package_id, previous or "(none)", phase, elapsed,
            time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now_wall)),
        )
        _PHASE_CLOCK[key] = (phase, now_monotonic, now_wall)

    def upsert_file(self, package_id, item):
        self.upsert_files(package_id, [item])

    def upsert_files(self, package_id, items):
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for item in items:
                values = (str(package_id), item["path"], item["name"], item.get("extension", ""),
                          int(item.get("size") or 0), int(item.get("modified_at_ns") or 0),
                          item.get("sha256"), 1 if item.get("sensitive") else 0,
                          item.get("file_type") or item.get("extension") or "", now)
                conn.execute("""INSERT INTO files(package_id,path,name,extension,size,modified_at_ns,sha256,sensitive,file_type,updated_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(package_id,path) DO UPDATE SET
                  name=excluded.name,extension=excluded.extension,size=excluded.size,
                  modified_at_ns=excluded.modified_at_ns,sensitive=excluded.sensitive,
                  file_type=excluded.file_type,updated_at=excluded.updated_at""", values)
                conn.execute("INSERT OR IGNORE INTO queue(package_id,path,stage,status,updated_at) VALUES(?,?,?,'queued',?)",
                             (str(package_id), item["path"], "quick", now))
            conn.commit()

    def claim(self, stage, limit=1, package_id=None):
        claimed = []
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # 防御性版本闸门：只领取"不高于本进程服务版本"的包。
            # 正常调度已由 _packages() 过滤，这里再挡一层，避免任何直接调用
            # claim() 的路径绕过版本约束去消费它可能不认识的包。
            try:
                from config import Config as _Cfg
                _served = int(getattr(_Cfg, "LP_WORKFLOW_VERSION", 1) or 1)
            except Exception:
                _served = 1
            sql = ("SELECT package_id,path FROM queue WHERE stage=? AND status IN ('queued','retry') "
                   "AND EXISTS (SELECT 1 FROM packages p WHERE p.id=queue.package_id "
                   "AND COALESCE(p.workflow_version,1) <= ? "
                   "AND p.status NOT IN ('paused','cancelled') "
                   "AND p.control_state NOT IN ('paused','cancelled'))")
            args = [stage, _served]
            if package_id:
                sql += " AND package_id=?"
                args.append(str(package_id))
            sql += " ORDER BY attempts,priority,updated_at LIMIT ?" if stage == "recognize" else " ORDER BY updated_at LIMIT ?"
            args.append(int(limit))
            rows = conn.execute(sql, args).fetchall()
            for row in rows:
                # 每次领取生成一个新的租约令牌。返回值变成三元组
                # (package_id, path, lease_token)，调用方必须把令牌原样带回
                # finish_queue / requeue —— 否则写入会被拒绝。
                token = uuid.uuid4().hex
                conn.execute("UPDATE queue SET status='running',attempts=attempts+1,leased_at=?,updated_at=?,lease_token=? WHERE package_id=? AND path=? AND stage=?",
                             (now, now, token, row["package_id"], row["path"], stage))
                claimed.append((row["package_id"], row["path"], token))
            conn.commit()
        return claimed

    def finish_queue(self, package_id, path, stage, ok=True, error=None, token=None):
        """把队列项标成终态。

        带 `token` 时会校验租约：只有当前持有者才能提交。令牌不匹配说明这一项
        已经被回收（`stale_running_to_retry`）或被别人重新领取，此时**拒绝写入**
        并返回 False——旧结果不得覆盖新结果。

        `token=None` 保留改动前的无条件写入语义，供既有调用方（主要是测试）使用；
        这条路径同样会清掉令牌，避免残留的旧令牌在后续误判。
        """
        status = "completed" if ok else "failed"
        with self._lock, self._connect() as conn:
            if token is None:
                conn.execute("UPDATE queue SET status=?,error=?,updated_at=?,lease_token=NULL WHERE package_id=? AND path=? AND stage=?",
                             (status, error, time.time(), str(package_id), str(path), stage))
                return True
            cursor = conn.execute(
                "UPDATE queue SET status=?,error=?,updated_at=?,lease_token=NULL "
                "WHERE package_id=? AND path=? AND stage=? AND lease_token=?",
                (status, error, time.time(), str(package_id), str(path), stage, str(token)))
            if cursor.rowcount != 1:
                LOGGER.warning("stale lease rejected finish: package=%s path=%s stage=%s",
                               package_id, path, stage)
                return False
            return True


    # ------------------------------------------------------------------
    # 原子提交与队列口径判据（本次改修新增）
    # ------------------------------------------------------------------
    def commit_stage_result(self, package_id, path, stage, token, *, ok=True,
                            file_fields=None, error=None, require_source_hash=None,
                            drop_catalog_queue=False):
        """**原子提交**：同一个事务里校验租约 → 写文件结果 → 落队列终态。

        为什么必须合并（改动前是两段独立写入）：
          `_deep_worker` 先 `update_file(deep_status="completed", deep_summary=...)`
          再 `finish_queue(..., token=lease)`，而且**忽略后者的返回值**。
          `stale_running_to_retry()` 轮换令牌后，失联的旧任务即使被队列拒绝，
          摘要也已经写进 files —— 旧结果覆盖新结果，而调用方仍返回成功。
          本地已复现：队列明确拒绝旧令牌，文件记录却已被改写。

        返回值 `(ok, reason)`。**调用方必须处理 False，不得当作成功。**

        - `token=None` 保留无条件写入语义（仅测试与旧调用方使用）。
        - `require_source_hash` 给出时，若 files.sha256 与领取时不一致
          （源文件中途变化）则拒绝提交并返回 'source_changed'。
        """
        now = time.time()
        _pf = {} if _PROFILE_COMMIT else None
        _t0 = time.monotonic()
        with self._lock, closing(self._connect()) as conn:
            _m(_pf, 'lock+connect', _t0); _t0 = time.monotonic()
            conn.execute("BEGIN IMMEDIATE")
            _m(_pf, 'begin', _t0); _t0 = time.monotonic()
            row = conn.execute(
                "SELECT status,lease_token FROM queue WHERE package_id=? AND path=? AND stage=?",
                (str(package_id), str(path), str(stage))).fetchone()
            _m(_pf, 'queue_sel', _t0)
            if not row:
                conn.rollback()
                return False, "no_queue_row"
            if token is not None:
                if str(row["status"]) != "running":
                    conn.rollback()
                    return False, "not_running"
                if str(row["lease_token"] or "") != str(token):
                    conn.rollback()
                    LOGGER.warning("stale lease rejected commit: package=%s path=%s stage=%s",
                                   package_id, path, stage)
                    return False, "stale_lease"
            if require_source_hash:
                cur = conn.execute("SELECT sha256 FROM files WHERE package_id=? AND path=?",
                                   (str(package_id), str(path))).fetchone()
                if not cur or str(cur["sha256"] or "") != str(require_source_hash):
                    conn.rollback()
                    LOGGER.warning("source changed, result rejected: package=%s path=%s",
                                   package_id, path)
                    return False, "source_changed"
            if file_fields:
                # 与 update_file 保持**同一套语义**：字段白名单、dict 序列化、
                # prepare_update 的内容处理，以及它带出的 catalog 重排队副作用。
                # 区别只有一个：这些全部发生在**同一个事务**里，所以租约被回收时
                # 什么都不会写进去。
                allowed = {"sha256", "quick_status", "deep_status", "category", "confidence",
                           "local_summary", "deep_summary", "error", "file_type", "topic",
                           "recognition_status", "document_role",
                           "summary_quality", "summary_coverage", "source_changed"}
                fields = dict((k, v) for k, v in file_fields.items() if k in allowed)
                schedule_catalog = False
                if "local_summary" in fields or "deep_summary" in fields:
                    from services.large_package_catalog import prepare_update
                    _t0 = time.monotonic()
                    previous = conn.execute("SELECT * FROM files WHERE package_id=? AND path=?",
                                            (str(package_id), str(path))).fetchone()
                    _m(_pf, 'files_sel_2nd', _t0)
                    if previous is None:
                        conn.rollback()
                        return False, "no_file_row"
                    _t0 = time.monotonic()
                    fields, schedule_catalog = prepare_update(self._row(previous), fields)
                    _m(_pf, 'prepare_update', _t0)
                    fields = dict((k, v) for k, v in fields.items() if k in allowed or k == "updated_at")
                if "local_summary" in fields:
                    info = (fields["local_summary"] or {}).get("portrait") or {}
                    if info.get("schema"):
                        from services.large_package_topics import assign
                        _t0 = time.monotonic()
                        fields["topic"] = assign(conn, str(package_id), str(path), info)
                        _m(_pf, 'assign', _t0)
                cols, args = [], []
                _t0 = time.monotonic()
                for key, value in fields.items():
                    if key == "updated_at":
                        continue
                    cols.append(key + "=?")
                    args.append(_json(value) if key in ("local_summary", "deep_summary") else value)
                _m(_pf, 'json_build', _t0)
                if cols:
                    _t0 = time.monotonic()
                    conn.execute("UPDATE files SET " + ",".join(cols) +
                                 ",updated_at=? WHERE package_id=? AND path=?",
                                 args + [now, str(package_id), str(path)])
                    _m(_pf, 'files_update', _t0)
                if schedule_catalog:
                    _t0 = time.monotonic()
                    conn.execute("UPDATE large_catalog_groups SET content_revision=content_revision+1 "
                                 "WHERE package_id=? AND id IN (SELECT group_id FROM large_catalog_members "
                                 "WHERE package_id=? AND path=?)",
                                 (str(package_id), str(package_id), str(path)))
                    conn.execute("INSERT INTO queue(package_id,path,stage,status,updated_at) "
                                 "VALUES(?,?,'catalog','queued',?) ON CONFLICT(package_id,path,stage) "
                                 "DO UPDATE SET status='queued',attempts=0,leased_at=NULL,"
                                 "lease_token=NULL,error=NULL,updated_at=excluded.updated_at",
                                 (str(package_id), str(path), now))
                    _m(_pf, 'schedule_catalog', _t0)
            _t0 = time.monotonic()
            conn.execute("UPDATE queue SET status=?,error=?,updated_at=?,lease_token=NULL "
                         "WHERE package_id=? AND path=? AND stage=?",
                         ("completed" if ok else "failed", error, now,
                          str(package_id), str(path), str(stage)))
            if drop_catalog_queue:
                conn.execute("DELETE FROM queue WHERE package_id=? AND path=? AND stage='catalog'",
                             (str(package_id), str(path)))
            _m(_pf, 'queue_update', _t0)
            _t0 = time.monotonic()
            conn.commit()
            _m(_pf, 'commit', _t0)
        if _pf is not None:
            _total = sum(_pf.values())
            if _total * 1000 >= _PROFILE_COMMIT_MIN_MS:
                LOGGER.info("commit profile stage=%s total=%.2fms | %s", stage, _total * 1000,
                            " ".join("%s=%.2f" % (k, v * 1000)
                                     for k, v in sorted(_pf.items(), key=lambda kv: -kv[1])))
        store = None  # 缓存失效由调用方负责（与 finish_queue 语义一致）
        return True, ""

    def holds_lease(self, package_id, path, stage, token):
        """只读检查：该队列项仍由这个令牌持有且处于 running。"""
        if token is None:
            return True
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT status,lease_token FROM queue WHERE package_id=? AND path=? AND stage=?",
                (str(package_id), str(path), str(stage))).fetchone()
        if not row:
            return False
        return str(row["status"]) == "running" and str(row["lease_token"] or "") == str(token)

    def renew_lease(self, package_id, path, stage, token):
        if not token:
            return False
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "UPDATE queue SET updated_at=? WHERE package_id=? AND path=? AND stage=? "
                "AND status='running' AND lease_token=?",
                (time.time(), str(package_id), str(path), str(stage), str(token)))
            return cursor.rowcount == 1

    # ------------------------------------------------------------------
    # **阶段定义表 —— 加一个阶段只改这一处。**
    #
    # 改动前"哪些阶段是合法的、哪些可被调度、下一步去哪"散在五个地方各写一遍：
    # 这里的 SCHEDULABLE_PHASES、worker._packages() 的 SQL 白名单、
    # save_selection / request_recognition 的状态白名单、_deep_finish 的判断。
    # 加一个阶段要改五处，漏一处就是死局 —— 生产实测过的 pause/resume 死局
    # （waiting_for_selection + catalog_ready 暂停后恢复成 queued + catalog_ready，
    #  而 _packages() 不调度 catalog_ready、save_selection 又拒绝 queued）
    # 正是这么来的。
    #
    # 字段含义：
    #   schedulable —— Worker 会选中处于该阶段的包（_packages() 由它派生）
    #   terminal    —— 流程终点，不再有后继阶段
    #   next        —— 正常收尾后进入的阶段（供 advance() 判定使用）
    #   gates       —— 收尾前必须清零的事实（queue stage 名 / 'naming' / 'material'）
    # ------------------------------------------------------------------
    PHASES = {
        "inventory":           {"schedulable": True,  "next": "quick_parse",    "gates": ("inventory",)},
        "quick_parse":         {"schedulable": True,  "next": "catalog_build",  "gates": ("quick",)},
        "catalog_build":       {"schedulable": True,  "next": "recognize",      "gates": ("catalog",)},
        "recognize":           {"schedulable": True,  "next": "catalog_ready",  "gates": ()},
        "recognize_completed": {"schedulable": True,  "next": "catalog_ready",  "gates": ()},
        "catalog_naming":      {"schedulable": True,  "next": "catalog_ready",  "gates": ("naming",)},
        # waiting=True：**等待用户/等待外部输入的态**，不是"干活的态"。
        # 判据：它不是终点（后面还有 deep_parse / 报告），但**没有任何 gate 可清**，
        # 所以 `can_finish` 必须是 False —— 否则 advance() 会对一个"正在等用户选文件"
        # 的包回答"这一步可以收尾了"，语义就错了（插桩实测：catalog_ready 报了 can_finish=True）。
        "catalog_ready":       {"schedulable": False, "next": "deep_parse",     "gates": (), "waiting": True},
        "deep_parse":          {"schedulable": True,  "next": "report_ready",   "gates": ("deep",)},
        "report_ready":        {"schedulable": False, "next": "",               "gates": (), "waiting": True},
        "paused":              {"schedulable": False, "next": "",               "gates": ()},
        "cancelled":           {"schedulable": False, "next": "",               "gates": ()},
    }
    TERMINAL_PHASES = tuple(k for k, v in PHASES.items() if not v.get("next"))
    # 可以被 Worker 调度的阶段（Worker 侧由这张表派生，不再各写一份白名单）
    SCHEDULABLE_PHASES = tuple(k for k, v in PHASES.items() if v.get("schedulable"))

    # 阶段名 → queue.stage 名。两套命名历史上就不一致（deep_parse ↔ deep、
    # quick_parse ↔ quick），要判断"当前阶段还剩多少活"必须显式对齐，
    # 否则会得出"phase=deep_parse 但 deep 队列为空"这种自相矛盾的结论。
    PHASE_STAGE = {"inventory": "inventory", "quick_parse": "quick", "catalog_build": "catalog",
                   "recognize": "recognize", "recognize_completed": "recognize",
                   "catalog_naming": "catalog", "deep_parse": "deep"}

    def workflow_snapshot(self, package_id):
        """**状态一体的唯一读取口**：一次返回控制态、当前运行、已发布视图、
        目录/概览更新状态、当前选择批次、质量统计与 allowed_actions。

        为什么需要：改动前页面只能自己从 packages.status、目录待命名数、
        failures.length 里推断全流程状态，于是同一事实在不同页面可以给出不同
        答案（例如队列说完成、files 说 pending）。
        """
        pid = str(package_id)
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM packages WHERE id=?", (pid,)).fetchone()
            if not row:
                return None
            pkg = dict(row)

            def one(sql, args=()):
                return conn.execute(sql, args).fetchone()

            queue = {}
            for r in conn.execute("SELECT stage,status,COUNT(*) n FROM queue "
                                  "WHERE package_id=? GROUP BY 1,2", (pid,)).fetchall():
                queue.setdefault(str(r["stage"]), {})[str(r["status"])] = int(r["n"])

            def pend(stage):
                d = queue.get(stage) or {}
                return sum(int(d.get(k) or 0) for k in ("queued", "running", "retry"))

            quality = {}
            # 空 summary_quality 的含义是"**这一份没有深度摘要**"（绝大多数文件
            # 从未被选中深析），不是"质量未知"。用 no_summary 如实表达。
            for r in conn.execute("SELECT COALESCE(NULLIF(summary_quality,''),'no_summary') k,"
                                  "COUNT(*) n FROM files WHERE package_id=? GROUP BY 1",
                                  (pid,)).fetchall():
                quality[str(r["k"])] = int(r["n"])

            deep = {}
            for r in conn.execute("SELECT deep_status k,COUNT(*) n FROM files "
                                  "WHERE package_id=? GROUP BY 1", (pid,)).fetchall():
                deep[str(r["k"])] = int(r["n"])

            fails = {}
            for r in conn.execute("SELECT COALESCE(stage,'') k,COUNT(*) n FROM queue "
                                  "WHERE package_id=? AND status='failed' GROUP BY 1",
                                  (pid,)).fetchall():
                fails[str(r["k"])] = int(r["n"])
            naming_pending = int(one(
                "SELECT COUNT(*) FROM lp_catalog_jobs WHERE package_id=? AND status IN ('queued','running','retry')",
                (pid,))[0])

            sel = one("SELECT id,status,batch_total,batch_completed,batch_failed,quality,"
                      "started_at,finished_at,catalog_id FROM selections WHERE package_id=? "
                      "ORDER BY created_at DESC LIMIT 1", (pid,))
            run = one("SELECT run_id,kind,status,scope_closed,started FROM lp_runs "
                      "WHERE package_id=? ORDER BY started DESC LIMIT 1", (pid,))
            view = one("SELECT view_id,catalog_id,report_id,evidence_id FROM lp_published_views "
                       "WHERE package_id=? ORDER BY created DESC LIMIT 1", (pid,))
            catv = one("SELECT catalog_id,status,validation,leaf_count,topic_count "
                       "FROM lp_catalog_versions WHERE package_id=? ORDER BY created DESC LIMIT 1",
                       (pid,))
            ov = one("SELECT report_id,milestone,status,schema_version FROM lp_overview_versions "
                     "WHERE package_id=? ORDER BY created DESC LIMIT 1", (pid,))

        status = str(pkg.get("status") or "")
        phase = str(pkg.get("phase") or "")
        control = str(pkg.get("control_state") or "active")
        if status == "cancelled":
            control = "cancelled"
        elif status == "paused":
            control = "paused"
        if pend('catalog') or naming_pending:
            catalog_state = 'building'
        elif catv is not None:
            catalog_state = str(catv["status"])
        elif view is not None:
            catalog_state = "published"
        elif pend("catalog"):
            catalog_state = "building"
        else:
            catalog_state = "none"
        if ov is not None:
            overview_state = str(ov["status"])
        elif (deep.get("completed") or 0) and pend("deep") == 0:
            overview_state = "pending_update"
        else:
            overview_state = "none"

        validation = {}
        if catv is not None:
            try:
                import json as _json
                validation = _json.loads(catv["validation"] or "{}")
            except Exception:
                validation = {}
        allowed = {
            "pause": control == "active" and phase in self.SCHEDULABLE_PHASES
                     and status in ('queued','running','waiting_for_selection'),
            "resume": control == "paused",
            "cancel": control != "cancelled",
            "select": control == "active" and status in ("waiting_for_selection", "completed"),
            "recognize": control == "active" and status in ("waiting_for_selection", "completed"),
            "retry_quick": control == 'active' and status in ('completed','waiting_for_selection','failed')
                           and int(fails.get("quick") or 0) > 0,
            "retry_deep": control == 'active' and status in ('completed','waiting_for_selection','failed')
                          and int(fails.get("deep") or 0) > 0,
            "download_report": bool(view is not None and view["report_id"]),
        }
        try:
            from services.large_package_semantic import catalog_degraded as _cd
            _degraded = _cd(self, pid)
        except Exception:
            _degraded = {"degraded": False, "reason": "check_failed"}
        # 待办口径（见下方 return 处的说明）。当前阶段是可调度阶段时，只有它自己的
        # 未结束任务才阻塞推进；其余都是可选积压。
        queue_pending = dict((k, pend(k)) for k in
                             ("inventory", "quick", "recognize", "catalog", "deep"))
        pending_total = int(sum(queue_pending.values()))
        active_phase = phase if control == "active" else ""
        stage_of_phase = self.PHASE_STAGE.get(active_phase, "")
        pending_blocking = int(queue_pending.get(stage_of_phase, 0)) if stage_of_phase else 0
        optional_stages = sorted(k for k, v in queue_pending.items()
                                 if v and k != stage_of_phase)
        current_selection = None
        if sel is not None:
            current_selection = dict(sel)
        current_run = None
        if run is not None:
            current_run = dict(run)
        published_view = None
        if view is not None:
            published_view = dict(view)
        return {
            "package_id": pid,
            "control_state": control,
            "control_generation": int(pkg.get("control_generation") or 0),
            "phase": phase, "status": status, "progress": int(pkg.get("progress") or 0),
            "message": str(pkg.get("message") or ""),
            "resume_phase": str(pkg.get("resume_phase") or ""),
            "resume_status": str(pkg.get("resume_status") or ""),
            "state_revision": int(pkg.get("state_revision") or 0),
            "current_run": current_run,
            "published_view": published_view,
            "catalog_id": (str(catv["catalog_id"]) if catv is not None else ""),
            "catalog_state": catalog_state,
            "naming_pending": naming_pending,
            "catalog_validation": validation,
            "leaf_count": (int(catv["leaf_count"]) if catv is not None else 0),
            "topic_count": (int(catv["topic_count"]) if catv is not None else 0),
            "overview_state": overview_state,
            # 目录是否处于"降级模式"（从未语义聚类、被固定 cap 切碎）。
            # 这是**读取时现算的**，不落库：判据依赖 km_state 与组大小分布，
            # 重聚类后应当立刻变成 False。
            "catalog_degraded": bool((_degraded or {}).get("degraded")),
            "catalog_degraded_detail": _degraded,
            "current_selection": current_selection,
            "queue_pending": queue_pending,
            # 「待办」必须分两种口径，否则页面会同时显示"已完成"和"还有 12,821 项待办"。
            #   blocking_pending：**当前阶段自己**未结束的任务 —— 决定这个包能不能往前走；
            #   optional_pending：其它阶段留下的补识别/补命名积压 —— 不影响本包已出的结论，
            #                     只有用户主动点"补充内容识别"才会被执行。
            "pending_blocking": int(pending_blocking),
            "pending_optional": int(pending_total - pending_blocking),
            "optional_stages": optional_stages,
            "pending_total": int(pending_total),
            "quality": quality,
            "deep_status": deep,
            "failed_by_stage": fails,
            "allowed_actions": allowed,
        }

    def pending_by_stage(self, package_id, stage):
        """**队列口径**的待办计数。完成判据必须用它，不能用 files 状态。

        为什么：`files.deep_status` 与 `queue.status` 是两套写入，结果保存与队列
        提交又不是原子的。用 files 判断会漏掉 `retry` —— 那是纯队列状态
        （`stale_running_to_retry` 写进去的），永远不会出现在 files 里。
        改动前 `_deep_finish` 里那句 `deep.get("retry", 0)` 因此是**死代码**：
        `counts()` 全部来自 files 表，那个键恒为 0。
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT status,COUNT(*) n FROM queue WHERE package_id=? AND stage=? GROUP BY status",
                (str(package_id), str(stage))).fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}

    def pending_total(self, package_id, stage):
        """该阶段还没结束的任务数（queued + running + retry）。"""
        counts = self.pending_by_stage(package_id, stage)
        return sum(int(counts.get(k) or 0) for k in ("queued", "running", "retry"))

    def retry_failed_stage(self, package_id, stage):
        return self.retry_failed_stages(package_id, [stage]).get(stage, [])

    def retry_failed_stages(self, package_id, stages):
        stages = tuple(dict.fromkeys(stages))
        if any(stage not in {'quick', 'deep'} for stage in stages):
            raise ValueError("Unsupported retry stage")
        now = time.time()
        result = {}
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            package = conn.execute("SELECT status,phase,control_state FROM packages WHERE id=?",
                                   (str(package_id),)).fetchone()
            if (not package or package['control_state'] != 'active'
                    or package['status'] not in {'completed', 'waiting_for_selection', 'failed'}):
                raise ValueError("请等待当前处理结束后重试；已暂停或取消的数据包不能重试")
            if conn.execute("SELECT 1 FROM queue WHERE package_id=? AND stage IN ('quick','deep') "
                            "AND status='running' LIMIT 1", (str(package_id),)).fetchone():
                raise ValueError("仍有文件正在运行，请稍后重试")
            for stage in stages:
                column = stage + '_status'
                paths = [str(row[0]) for row in conn.execute(
                    "SELECT f.path FROM files f LEFT JOIN queue q ON q.package_id=f.package_id "
                    "AND q.path=f.path AND q.stage=? WHERE f.package_id=? "
                    "AND (q.status='failed' OR (q.path IS NULL AND f." + column + "='failed'))",
                    (stage, str(package_id)))]
                result[stage] = paths
                for path in paths:
                    conn.execute("INSERT INTO queue(package_id,path,stage,status,updated_at) "
                                 "VALUES(?,?,?,'queued',?) ON CONFLICT(package_id,path,stage) DO UPDATE "
                                 "SET status='queued',attempts=0,leased_at=NULL,lease_token=NULL,error=NULL,updated_at=excluded.updated_at",
                                 (str(package_id), path, stage, now))
                    conn.execute("UPDATE files SET " + column + "='queued',error=NULL,updated_at=? "
                                 "WHERE package_id=? AND path=?", (now, str(package_id), path))
                if stage == 'deep' and paths:
                    changed = set(paths)
                    for selection in conn.execute("SELECT id,paths FROM selections WHERE package_id=? "
                                                  "AND status<>'cancelled'", (str(package_id),)).fetchall():
                        if changed.intersection(_loads(selection['paths'], [])):
                            conn.execute("UPDATE selections SET status='running',finished_at=NULL,updated_at=? WHERE id=?",
                                         (now, selection['id']))
            if any(result.values()):
                # Fast retries must finish before pending deep work resumes.
                quick = conn.execute("SELECT 1 FROM queue WHERE package_id=? AND stage='quick' "
                                     "AND status IN ('queued','retry','running') LIMIT 1", (str(package_id),)).fetchone()
                phase = 'quick_parse' if quick else 'deep_parse'
                conn.execute("UPDATE packages SET status='queued',phase=?,progress=?,error=NULL,"
                             "message='失败文件已重新排队',updated_at=? WHERE id=?",
                             (phase, 20 if quick else 70, now, str(package_id)))
            conn.commit()
        return result
    def cancel_package(self, package_id):
        """取消耗数据包：**一个事务**里把控制态、队列、文件、selection 全部结掉。

        为什么必须是一个入口：改动前 `/cancel` 只写 `packages.status='cancelled'`，
        队列行仍是 `queued`、`files.deep_status` 仍是 `queued`。于是
          * `claim()` 照样能领到活（它只看 queue.status）；
          * `counts()` / `/workflow` 的待办数照旧非 0；
          * 一旦有人 resume 或任何代码路径重新调度，取消掉的包会接着往下跑。
        生产实测 `bb80533c5ae999` 就是这么留下 **6,743 行可被领取的活 + 260 个
        `deep_status='queued'` 的文件 + 1 条活着的 selection** 的 —— 也就是说
        "点一次取消"会原样造出一批永远没人处理的垃圾。

        队列项用独立终态 `cancelled`（不是 `failed`）：`claim()` 只领
        `queued/retry`，`pending_by_stage` / `workflow_snapshot` 只数
        `queued/running/retry`，所以 `cancelled` 天然不会被领取、也不会被算成
        "失败"，页面不会把"用户主动取消"显示成错误。
        """
        pid = str(package_id)
        now = time.time()
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status,phase,control_generation FROM packages WHERE id=?",
                               (pid,)).fetchone()
            if not row:
                conn.rollback()
                return None
            queue_rows = conn.execute(
                "UPDATE queue SET status='cancelled',leased_at=NULL,lease_token=NULL,updated_at=?,"
                "error=COALESCE(error,'所属数据包已取消') "
                "WHERE package_id=? AND status IN ('queued','running','retry')", (now, pid)).rowcount or 0
            selections = conn.execute(
                "UPDATE selections SET status='cancelled',finished_at=COALESCE(finished_at,?),"
                "updated_at=? WHERE package_id=? AND status IN ('queued','running')",
                (now, now, pid)).rowcount or 0
            deep_files = conn.execute(
                "UPDATE files SET deep_status='idle',updated_at=? "
                "WHERE package_id=? AND deep_status='queued'", (now, pid)).rowcount or 0
            quick_files = conn.execute(
                "UPDATE files SET quick_status='idle',updated_at=? "
                "WHERE package_id=? AND quick_status IN ('queued','running')", (now, pid)).rowcount or 0
            # **命名权威队列也要结掉**。`lp_catalog_jobs` 是模型命名的唯一权威队列
            # （见 naming 模块说明），不是 legacy 的 `queue` 表；原来只清了 `queue`，
            # 于是"取消"之后这个包仍然挂着成百上千条待办：
            # 生产实测 `lp-4b41a607…`（status=cancelled）留着 **4,865 条任务、
            # 3,832 条在排队**，而命名车道每 4.3 秒才做完一条 —— 相当于把
            # 4.6 小时的算力排在一个用户已经取消的包前面。
            # 用独立终态 `cancelled`（同 `queue` 的理由：不会被领、不算失败）。
            naming_jobs = conn.execute(
                "UPDATE lp_catalog_jobs SET status='cancelled',"
                "error=COALESCE(error,'所属数据包已取消') "
                "WHERE package_id=? AND status IN ('queued','running','retry')",
                (pid,)).rowcount or 0
            conn.execute(
                "UPDATE packages SET status='cancelled',control_state='cancelled',"
                "control_generation=COALESCE(control_generation,0)+1,resume_phase='',resume_status='',"
                "message='已取消；已完成的结果与目录保留',updated_at=? WHERE id=?", (now, pid))
            conn.execute("INSERT INTO lp_workflow_events(package_id,object,old_state,new_state,reason,"
                         "source,created) VALUES(?,?,?,?,?,?,?)",
                         (pid, 'package', str(row['status'] or ''), 'cancelled',
                          '用户取消：队列 %d 项作废（含命名任务 %d 条）、selection %d 条终结、文件 %d 个回 idle'
                          % (queue_rows, naming_jobs, selections, deep_files + quick_files),
                          'cancel_package', now))
            conn.commit()
        self._catalog_cache.pop(pid, None)
        return {"queue_rows": int(queue_rows), "selections": int(selections),
                "deep_files": int(deep_files), "quick_files": int(quick_files),
                "naming_jobs": int(naming_jobs)}

    def advance(self, package_id):
        """**唯一的"这一步能不能结束、下一步是什么、还差什么"判定口。**

        改动前有六处各自判断阶段收尾（`_deep_finish` / `_finish_selection` /
        `_naming_one` / `run_slice` 的定稿判定 / `_ensure_km_finalized` /
        `_build_overview`），各用各的判据。我这两轮修的全部缺陷——假完成、
        口径不一致、三处写库空转、"目录会自己变"、"空闲分支不推进"——
        **全部长在这个结构上**：判据分散，任何一处漏掉一个状态，就会得出
        自相矛盾的结论。

        这里把判据收成一张表（`PHASES[phase]['gates']`）+ 一个函数：
        输入是**事实**，输出是"能不能收尾、下一个阶段是什么、被什么挡住"。
        **只读，不写库**——调用方做完自己的那份工作，再问它。
        """
        pid = str(package_id)
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT phase,status,control_state FROM packages WHERE id=?",
                               (pid,)).fetchone()
            if not row:
                return {"ok": False, "reason": "no_such_package"}
            phase = str(row["phase"] or "")
            facts = {
                "phase": phase,
                "status": str(row["status"] or ""),
                "control_state": str(row["control_state"] or "active"),
                "queue_pending": {
                    str(r["stage"]): int(r["n"]) for r in conn.execute(
                        "SELECT stage,COUNT(*) n FROM queue WHERE package_id=? "
                        "AND status IN ('queued','running','retry') GROUP BY stage", (pid,))},
                "naming_pending": int(conn.execute(
                    "SELECT COUNT(*) FROM lp_catalog_jobs WHERE package_id=? "
                    "AND status IN ('queued','running')", (pid,)).fetchone()[0] or 0),
                "km_state": int(conn.execute(
                    "SELECT COUNT(*) FROM large_catalog_km_state WHERE package_id=?",
                    (pid,)).fetchone()[0] or 0),
                "branches": int(conn.execute(
                    "SELECT COUNT(*) FROM lp_catalog_branches WHERE package_id=?",
                    (pid,)).fetchone()[0] or 0),
            }
        spec = dict(self.PHASES.get(phase) or {})
        blocked_by = []
        for gate in tuple(spec.get("gates") or ()):
            if gate == "naming":
                if facts["naming_pending"]:
                    blocked_by.append("naming=%d" % facts["naming_pending"])
            else:
                left = int(facts["queue_pending"].get(gate) or 0)
                if left:
                    blocked_by.append("%s=%d" % (gate, left))
        terminal = not spec.get("next")
        waiting = bool(spec.get("waiting"))
        return {"ok": True, "phase": phase, "next_phase": str(spec.get("next") or ""),
                "waiting": waiting,
                "can_finish": bool(spec) and not terminal and not waiting and not blocked_by,
                "blocked_by": blocked_by, "gates": list(spec.get("gates") or ()),
                "terminal": bool(terminal), "schedulable": bool(spec.get("schedulable")),
                "facts": facts}

    def set_control_state(self, package_id, control_state, *, note="", resume_phase=None,
                          resume_status=None, control_generation=None):
        """设置包的控制态（active/paused/cancelled），并记住**恢复目标**。

        改动前的缺陷：`pause` 不限制原状态就写 status='paused'；`resume` 一律写
        status='queued' 并保留 phase。于是"等待选择 + catalog_ready"的包暂停再
        恢复会变成 queued/catalog_ready —— 而 `_packages()` 不调度 catalog_ready、
        `save_selection` 又拒绝 queued，直接形成**无消费者死局**。
        """
        now = time.time()
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT phase,status,control_state,control_generation "
                               "FROM packages WHERE id=?", (str(package_id),)).fetchone()
            if not row:
                conn.rollback()
                return None
            gen = int(row["control_generation"] or 0)
            if control_generation is not None and int(control_generation) != gen:
                conn.rollback()
                return None
            fields = {"control_state": str(control_state), "control_generation": gen + 1,
                      "updated_at": now}
            if resume_phase:
                fields["resume_phase"] = str(resume_phase)
            if resume_status:
                fields["resume_status"] = str(resume_status)
            if note:
                fields["message"] = str(note)[:500]
            if control_state == "cancelled":
                fields["status"] = "cancelled"
            sets = ",".join("%s=?" % k for k in fields)
            conn.execute("UPDATE packages SET %s WHERE id=?" % sets,
                         list(fields.values()) + [str(package_id)])
            conn.commit()
        return {"control_state": str(control_state), "control_generation": gen + 1}

    def update_file(self, package_id, path, **fields):
        allowed = {"sha256", "quick_status", "deep_status", "category", "confidence", "local_summary", "deep_summary", "error", "file_type", "topic", "recognition_status", "document_role"}
        fields = {key: value for key, value in fields.items() if key in allowed}
        if not fields:
            return
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            schedule_catalog = False
            if "local_summary" in fields or "deep_summary" in fields:
                from services.large_package_catalog import prepare_update
                previous = conn.execute("SELECT * FROM files WHERE package_id=? AND path=?", (str(package_id), str(path))).fetchone()
                if not previous:
                    conn.rollback()
                    return
                fields, schedule_catalog = prepare_update(self._row(previous), fields)
            if "local_summary" in fields:
                info = (fields["local_summary"] or {}).get("portrait") or {}
                if info.get("schema"):
                    from services.large_package_topics import assign
                    fields["topic"] = assign(conn, str(package_id), str(path), info)
            fields["updated_at"] = time.time()
            cols, args = [], []
            for key, value in fields.items():
                cols.append(key + "=?")
                args.append(_json(value) if key in {"local_summary", "deep_summary"} else value)
            args.extend([str(package_id), str(path)])
            conn.execute("UPDATE files SET " + ",".join(cols) + " WHERE package_id=? AND path=?", args)
            if schedule_catalog:
                conn.execute("UPDATE large_catalog_groups SET content_revision=content_revision+1 WHERE package_id=? AND id IN (SELECT group_id FROM large_catalog_members WHERE package_id=? AND path=?)",(str(package_id),str(package_id),str(path)))
                conn.execute("""INSERT INTO queue(package_id,path,stage,status,updated_at) VALUES(?,?,'catalog','queued',?)
                  ON CONFLICT(package_id,path,stage) DO UPDATE SET status='queued',attempts=0,leased_at=NULL,lease_token=NULL,error=NULL,updated_at=excluded.updated_at""",
                             (str(package_id), str(path), time.time()))
            conn.commit()
        self._catalog_cache.pop(str(package_id), None)

    def counts(self, package_id):
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM files WHERE package_id=?", (str(package_id),)).fetchone()[0]
            quick = dict(conn.execute("SELECT quick_status,COUNT(*) n FROM files WHERE package_id=? GROUP BY quick_status", (str(package_id),)).fetchall())
            deep = dict(conn.execute("SELECT deep_status,COUNT(*) n FROM files WHERE package_id=? GROUP BY deep_status", (str(package_id),)).fetchall())
            cats = dict(conn.execute("SELECT category,COUNT(*) n FROM files WHERE package_id=? GROUP BY category ORDER BY n DESC", (str(package_id),)).fetchall())
            recognition = dict(conn.execute("SELECT recognition_status,COUNT(*) FROM files WHERE package_id=? GROUP BY recognition_status", (str(package_id),)).fetchall())
        # 队列口径的待办：完成判据必须用它（files 状态永远看不到 retry）。
        with closing(self._connect()) as conn:
            queue_pending = {
                str(r["stage"]): int(r["n"]) for r in conn.execute(
                    "SELECT stage,COUNT(*) n FROM queue WHERE package_id=? "
                    "AND status IN ('queued','running','retry') GROUP BY stage",
                    (str(package_id),)).fetchall()}
        return {"total_files": int(total), "quick": quick, "deep": deep,
                "categories": cats, "recognition": recognition,
                # 新增（增量，不改旧键）：队列口径的未结束任务数与按阶段明细
                "queue_pending": queue_pending,
                "pending_total": int(sum(queue_pending.values()))}

    def file_page(self, package_id, limit=100, offset=0, query="", category="", topic="", remaining=False, directory=None, facet_kind="", facet_value="", node_id=""):
        where, args = ["package_id=?"], [str(package_id)]
        if query:
            where.append("(path LIKE ? OR name LIKE ? OR local_summary LIKE ? OR path IN (SELECT m.path FROM large_catalog_members m JOIN lp_catalog_labels l ON l.package_id=m.package_id AND l.group_id=m.group_id WHERE m.package_id=? AND (l.name LIKE ? OR l.description LIKE ?)))")
            args.extend(["%" + str(query) + "%"] * 3 + [str(package_id)] + ["%" + str(query) + "%"] * 2)
        if category:
            where.append("category=?")
            args.append(str(category))
        if node_id:
            if node_id.startswith(("branch:", "topic:")):
                from services.large_package_catalog_hierarchy import visible
                with closing(self._connect()) as c:
                    branch=next((b for b in visible(c,str(package_id)) if b['node_id']==node_id),None)
                if not branch:raise ValueError("该上层目录已更新，请刷新目录")
                children=branch['children']
                where.append("path IN (SELECT path FROM large_catalog_members WHERE package_id=? AND group_id IN ("+','.join('?' for _ in children)+"))")
                args.extend([str(package_id)]+children)
            elif node_id.startswith("preview:"):
                try:
                    preview_topic=bytes.fromhex(node_id[8:]).decode('utf8')
                except (ValueError,UnicodeError):
                    raise ValueError("目录节点编号无效")
                where.append("topic=? AND path NOT IN (SELECT path FROM large_catalog_members WHERE package_id=?)")
                args.extend([preview_topic,str(package_id)])
            elif node_id.startswith("unread:"):
                where.append("recognition_status=? AND path NOT IN (SELECT path FROM large_catalog_members WHERE package_id=?)")
                args.extend([node_id[7:], str(package_id)])
            else:
                where.append("path IN (SELECT path FROM large_catalog_members WHERE package_id=? AND group_id=?)")
                args.extend([str(package_id), str(node_id)])
        if topic:
            # A4 第一步：**topic 是目录派生的字段**，读取必须优先按目录成员关系判定，
            # 只对"还没进目录"的文件回落到 files.topic。
            # 改动前直接过滤 files.topic —— 而那是命名当时写下的快照，
            # 目录归一/重命名之后它就过期了，于是"按目录点进去"和"按 topic 过滤"
            # 会给出两套不同的文件集合（同一棵树两条读取路径的典型症状）。
            # files.category 保持不动：它是快速解析的分类，与目录是两件不同的事实，
            # 而且页面分类下拉框就是按它填充的，改它会两边对不上。
            where.append("((path IN (SELECT m.path FROM large_catalog_members m "
                         "JOIN lp_catalog_labels l ON l.package_id=m.package_id "
                         "AND l.group_id=m.group_id WHERE m.package_id=? AND l.name=?)) "
                         "OR (topic=? AND NOT EXISTS (SELECT 1 FROM large_catalog_members m2 "
                         "WHERE m2.package_id=? AND m2.path=files.path)))")
            args.extend([str(package_id), str(topic), str(topic), str(package_id)])
        if remaining:
            where.append("deep_status != 'completed'")
        if directory is not None:
            prefix = str(directory).strip("/") + "/" if directory else ""
            if prefix:
                where.append("substr(path,1,?)=?")
                args.extend([len(prefix), prefix])
            else:
                where.append("instr(path,'/')=0")
        if facet_kind in {"role", "project", "entity", "date"} and facet_value:
            where.append("path IN (SELECT path FROM portrait_facets WHERE package_id=? AND kind=? AND value=?)")
            args.extend([str(package_id), facet_kind, facet_value])
        clause = " AND ".join(where)
        limit, offset = max(1, min(500, int(limit))), max(0, int(offset))
        with self._connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM files WHERE " + clause, args).fetchone()[0])
            rows = conn.execute("SELECT * FROM files WHERE " + clause + " ORDER BY path LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
        return {"files": self._decorate_files(package_id, [self._row(row) for row in rows]), "total": total, "offset": offset, "limit": limit, "has_more": offset + len(rows) < total}

    def list_files(self, package_id, limit=100, offset=0, query="", category="", **filters):
        return self.file_page(package_id, limit, offset, query, category, **filters)["files"]

    def get_file(self, package_id, path):
        """Fetch one manifest row by its exact relative path."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM files WHERE package_id=? AND path=?",
                (str(package_id), str(path)),
            ).fetchone()
        return self._decorate_files(package_id, [self._row(row)])[0] if row else None

    def _decorate_files(self, package_id, files):
        from services.large_package_catalog_view import decorate_files
        return decorate_files(self, package_id, files)

    def create_user_node(self, package_id, name, paths, parent_id=None):
        unique = []
        valid = set(self.existing_paths(package_id, paths or []))
        for path in paths or []:
            path = str(path)
            if path in valid and path not in unique:
                unique.append(path)
        if not unique:
            raise ValueError("只能整理当前大数据包中的文件")
        node_id = "large-node-" + uuid.uuid4().hex[:20]
        with self._lock, self._connect() as conn:
            conn.execute("INSERT INTO user_nodes(id,package_id,name,parent_id,paths,created_at) VALUES(?,?,?,?,?,?)",
                         (node_id, str(package_id), str(name).strip()[:120], parent_id, _json(unique), time.time()))
        return self.get_user_node(package_id, node_id)

    def get_user_node(self, package_id, node_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM user_nodes WHERE id=? AND package_id=?", (str(node_id), str(package_id))).fetchone()
        return self._row(row) if row else None

    def list_user_nodes(self, package_id):
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM user_nodes WHERE package_id=? ORDER BY created_at", (str(package_id),)).fetchall()
        return [self._row(row) for row in rows]

    def catalog(self, package_id):
        """Bounded directory response and source-supported topic groups.

        Stream scalar paths; never hydrate every file/portrait. Cache the result
        briefly so polling does not repeatedly walk a large manifest.
        """
        now = time.monotonic()
        cached = self._catalog_cache.get(package_id)
        if cached and now - cached[0] < 5:
            return cached[1]
        directories, root_files = {}, []
        omitted = False
        with self._connect() as conn:
            package = conn.execute("SELECT root_path FROM packages WHERE id=?", (package_id,)).fetchone()
            if not package:
                return None
            total = int(conn.execute("SELECT COUNT(*) FROM files WHERE package_id=?", (package_id,)).fetchone()[0])
            for row in conn.execute("SELECT path,size FROM files WHERE package_id=? ORDER BY path", (package_id,)):
                path, size = row["path"], row["size"]
                parts = path.split("/")[:-1]
                if not parts and len(root_files) < 50:
                    root_files.append(path)
                for depth in range(1, min(len(parts), 32) + 1):
                    prefix = "/".join(parts[:depth])
                    if prefix not in directories and len(directories) >= 500:
                        omitted = True
                        continue
                    d = directories.setdefault(prefix, {"path": prefix, "files": 0, "bytes": 0, "sample": []})
                    d["files"] += 1
                    d["bytes"] += size
                    if len(d["sample"]) < 3:
                        d["sample"].append(path)
                omitted = omitted or len(parts) > 32
            categories = [dict(row) for row in conn.execute("SELECT category,COUNT(*) files,SUM(size) bytes,AVG(confidence) confidence FROM files WHERE package_id=? GROUP BY category ORDER BY files DESC,category LIMIT 100", (package_id,))]
            for group in categories:
                group["sample"] = [dict(row) for row in conn.execute("SELECT path,quick_status,deep_status FROM files WHERE package_id=? AND category=? ORDER BY path LIMIT 3", (package_id, group["category"]))]
            topic_count, topics = 0, []  # Stable-node query below supplies the paginated themes.
            recognition = dict(conn.execute("SELECT recognition_status,COUNT(*) FROM files WHERE package_id=? GROUP BY recognition_status", (package_id,)).fetchall())
            facets = {}
            for kind in ("role", "project", "entity", "date"):
                facets[kind] = [dict(row) for row in conn.execute("SELECT value,COUNT(*) files FROM portrait_facets WHERE package_id=? AND kind=? GROUP BY value ORDER BY files DESC,value LIMIT 30", (package_id, kind))]
            pending_groups = conn.execute("SELECT COUNT(*) FROM large_catalog_groups WHERE package_id=? AND status IN ('queued','retry')", (package_id,)).fetchone()[0]
            catalog_states = dict(conn.execute("SELECT COALESCE(json_extract(local_summary,'$.portrait.catalog_status'),'legacy'),COUNT(*) FROM files WHERE package_id=? GROUP BY 1", (package_id,)).fetchall())
            roles = [dict(row) for row in conn.execute("SELECT document_role,COUNT(*) files FROM files WHERE package_id=? AND document_role!='' GROUP BY document_role", (package_id,))]
        result = {"root_path": package["root_path"], "files_total": total,
                  "original": {"directories": list(directories.values()), "root_files": root_files, "truncated": omitted, "notice": "目录树最多显示500个节点，完整文件可按路径搜索并翻页" if omitted else ""},
                  "smart": {"categories": categories, "topics": topics, "topic_total": topic_count, "topics_truncated": topic_count > len(topics), "roles": roles, "facets": facets, "recognition": recognition, "catalog_states": catalog_states, "pending_groups": pending_groups}}
        from services.large_package_catalog_view import enhance_catalog
        result = enhance_catalog(self, package_id, result)
        if len(self._catalog_cache) > 8:
            self._catalog_cache.clear()
        self._catalog_cache[package_id] = (now, result)
        return result

    def enqueue_recognition(self, package_id, path):
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO queue(package_id,path,stage,status,updated_at,priority) "
                "SELECT ?,f.path,'recognize','queued',?,? FROM files f "
                "WHERE f.package_id=? AND f.path=? AND NOT " + _RECOGNITION_RESOLVED_SQL,
                (package_id, time.time(), int(hashlib.sha256(path.encode()).hexdigest()[:7],16),
                 package_id, path))

    def _reconcile_recognition(self, conn, package_id=None, apply=True):
        sql = ("SELECT q.rowid,q.package_id FROM queue q JOIN files f "
               "ON f.package_id=q.package_id AND f.path=q.path "
               "JOIN packages p ON p.id=q.package_id "
               "WHERE q.stage='recognize' AND q.status IN ('queued','retry') "
               "AND p.status<>'cancelled' AND p.control_state<>'cancelled' AND "
               + _RECOGNITION_RESOLVED_SQL)
        args = ()
        if package_id is not None:
            sql += " AND q.package_id=?"
            args = (str(package_id),)
        rows = conn.execute(sql, args).fetchall()
        if not apply or not rows:
            return len(rows)
        # Callers hold the write transaction, so leases cannot change between
        # selecting candidates and retiring them. Live running jobs are excluded.
        now = time.time()
        ids = [int(row[0]) for row in rows]
        for start in range(0, len(ids), 900):
            chunk = ids[start:start + 900]
            conn.execute(
                "UPDATE queue SET status='completed',error=NULL,leased_at=NULL,"
                "lease_token=NULL,updated_at=? WHERE rowid IN ("
                + ",".join("?" for _ in chunk) + ")", [now] + chunk)
        for pid, count in Counter(str(row[1]) for row in rows).items():
            conn.execute(
                "INSERT INTO lp_workflow_events(package_id,object,old_state,new_state,reason,source,created) "
                "VALUES(?,?,?,?,?,?,?)",
                (pid, "recognize_queue", "queued/retry", "completed",
                 "Reconciled %d jobs using terminal recognition or current classified content" % count,
                 "reconcile_recognition", now))
        return len(rows)

    def reconcile_recognition(self, package_id=None, apply=True):
        """Retire proven redundant jobs without changing portraits or live leases."""
        with self._lock, closing(self._connect()) as conn:
            if apply:
                conn.execute("BEGIN IMMEDIATE")
            count = self._reconcile_recognition(conn, package_id, apply=apply)
            if apply:
                conn.commit()
            return count

    def stage_counts(self, package_id, stage):
        with self._connect() as conn:
            return dict(conn.execute("SELECT status,COUNT(*) FROM queue WHERE package_id=? AND stage=? GROUP BY status", (package_id, stage)).fetchall())

    def requeue(self, package_id, path, stage, token=None):
        """把队列项放回 queued。

        与 `finish_queue` 对称：带 `token` 时只允许当前持有者回退；令牌不匹配
        （已被回收或已被他人重新领取）则拒绝并返回 False。
        不带 `token` 时保留改动前的无条件语义，同时清掉残留令牌。
        """
        with self._connect() as conn:
            if token is None:
                conn.execute("UPDATE queue SET status='queued',leased_at=NULL,lease_token=NULL,updated_at=? WHERE package_id=? AND path=? AND stage=?", (time.time(), package_id, path, stage))
                return True
            cursor = conn.execute(
                "UPDATE queue SET status='queued',leased_at=NULL,lease_token=NULL,updated_at=? "
                "WHERE package_id=? AND path=? AND stage=? AND lease_token=?",
                (time.time(), package_id, path, stage, str(token)))
            return cursor.rowcount == 1

    def request_recognition(self, package_id):
        """Explicitly upgrade old previews or continue a deferred recognition slice.
        Deep results and ordinary tables are never touched.
        """
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT status,phase FROM packages WHERE id=?", (package_id,)).fetchone()
            if not item or item["status"] not in {"waiting_for_selection", "completed"}:
                raise ValueError("请在当前处理结束后补充内容识别")
            conn.execute("UPDATE queue SET status='queued',attempts=0,error=NULL,updated_at=? WHERE package_id=? AND stage='catalog' AND error IS NOT NULL", (time.time(), package_id))
            conn.execute("UPDATE large_catalog_groups SET status='queued',attempts=0,updated_at=? WHERE package_id=? AND status IN ('deferred','retry')", (time.time(), package_id))
            # 先把"其实已经识别完成"的残留行收掉，再数还剩多少待识别。
            # 改动前直接拿 queue 里 queued 的行数当待办数，而目录阶段
            # （large_package_catalog._save）给出 identified 结论时只结束 catalog 行，
            # recognize 行会永远留在 queued —— 生产实测 12,397 个已 identified 的
            # 文件被算成"待补充识别"，一次点击就要把它们全部重跑。
            self._reconcile_recognition(conn, package_id)
            queued = int(conn.execute("SELECT COUNT(*) FROM queue WHERE package_id=? AND stage='recognize' AND status IN ('queued','retry')", (package_id,)).fetchone()[0])
            rows = conn.execute(
                "SELECT f.path FROM files f WHERE f.package_id=? AND f.sensitive=0 "
                "AND f.recognition_status IN ('legacy','needs_more','needs_ocr','metadata_only','needs_review') "
                "AND NOT " + _RECOGNITION_RESOLVED_SQL, (package_id,))
            paths = [] if queued else [row[0] for row in rows]
            for path in paths:
                conn.execute("INSERT INTO queue(package_id,path,stage,status,updated_at,priority) VALUES(?,?,'recognize','queued',?,?) ON CONFLICT(package_id,path,stage) DO UPDATE SET status='queued',attempts=0,leased_at=NULL,updated_at=excluded.updated_at,priority=excluded.priority", (package_id, path, time.time(), int(hashlib.sha256(path.encode()).hexdigest()[:7],16)))
            # Keep a completed research brief available while only its navigation
            # portraits are improved. The worker restores this phase after a slice.
            if paths or queued:
                conn.execute("UPDATE packages SET status='queued',phase=?,message='已排队补充内容识别',updated_at=? WHERE id=?", ("recognize_completed" if item["status"] == "completed" else "recognize", time.time(), package_id))
            conn.commit()
        return len(paths) + queued

    def existing_paths(self, package_id, paths):
        values = [str(path) for path in paths if str(path)]
        if not values:
            return []
        placeholders = ",".join("?" for _ in values)
        with self._connect() as conn:
            rows = conn.execute("SELECT path FROM files WHERE package_id=? AND path IN (" + placeholders + ")",
                                [str(package_id)] + values).fetchall()
        return [str(row[0]) for row in rows]

    def export_selection(self, package_id, node_ids=None, paths=None, exclude_paths=None, exclude_node_ids=None):
        """Resolve smart-catalog selections to immutable export metadata.

        `exclude_paths` 用于"勾选整个目录、再取消其中若干文件"：目录仍按 node_id
        展开，排除项在展开之后扣减，因此即使目录下的文件列表是分页读取的，
        也不会因为前端没加载到而漏掉排除。
        """
        package_id = str(package_id)
        requested_nodes = list(dict.fromkeys(str(value).strip() for value in (node_ids or []) if str(value).strip()))
        requested_paths = list(dict.fromkeys(str(value).replace("\\", "/").strip("/") for value in (paths or []) if str(value).strip()))
        group_ids = []
        node_labels = []
        with closing(self._connect()) as conn:
            package = conn.execute("SELECT id FROM packages WHERE id=?", (package_id,)).fetchone()
            if not package:
                raise ValueError("大数据包任务不存在")
            from services.large_package_catalog_hierarchy import visible
            branches = {str(item.get("node_id")): item for item in visible(conn, package_id)}
            for node_id in requested_nodes:
                branch = branches.get(node_id)
                if branch:
                    children = [str(child) for child in branch.get("children") or []]
                    group_ids.extend(children)
                    node_labels.append({"node_id": node_id, "name": branch.get("name") or node_id,
                                        "file_count": int(branch.get("files") or 0), "kind": "branch"})
                elif node_id.startswith(("preview:", "unread:")):
                    raise ValueError("该目录节点尚未形成可导出的智能目录")
                else:
                    row = conn.execute(
                        "SELECT g.id,COALESCE(l.name,'') name,"
                        "(SELECT COUNT(*) FROM large_catalog_members m WHERE m.package_id=g.package_id AND m.group_id=g.id) files "
                        "FROM large_catalog_groups g LEFT JOIN lp_catalog_labels l "
                        "ON l.package_id=g.package_id AND l.group_id=g.id WHERE g.package_id=? AND g.id=?",
                        (package_id, node_id),
                    ).fetchone()
                    if not row:
                        raise ValueError("智能目录节点不存在，请刷新后重试")
                    group_ids.append(str(row["id"]))
                    node_labels.append({"node_id": str(row["id"]), "name": row["name"] or str(row["id"]),
                                        "file_count": int(row["files"] or 0), "kind": "topic"})
            group_ids = list(dict.fromkeys(group_ids))
            if group_ids:
                placeholders = ",".join("?" for _ in group_ids)
                rows = conn.execute(
                    "SELECT DISTINCT f.* FROM files f JOIN large_catalog_members m "
                    "ON m.package_id=f.package_id AND m.path=f.path "
                    "WHERE f.package_id=? AND m.group_id IN (" + placeholders + ") ORDER BY f.path",
                    [package_id] + group_ids,
                ).fetchall()
            else:
                rows = []
            member_paths = []
            inventory = {}
            def add_item(row):
                item = self._row(row)
                path = str(item.get("path") or "")
                if not path or path in inventory:
                    return
                member_paths.append(path)
                inventory[path] = {
                    "kind": "file", "path": path, "name": item.get("name") or Path(path).name,
                    "extension": item.get("extension") or Path(path).suffix.lower(),
                    "size": int(item.get("size") or 0),
                    "modified_at_ns": int(item.get("modified_at_ns") or 0),
                    "sensitive": bool(item.get("sensitive")),
                }
            for row in rows:
                add_item(row)
            if requested_paths:
                placeholders = ",".join("?" for _ in requested_paths)
                explicit_rows = conn.execute(
                    "SELECT * FROM files WHERE package_id=? AND path IN (" + placeholders + ")",
                    [package_id] + requested_paths,
                ).fetchall()
                explicit = {str(row["path"]): row for row in explicit_rows}
                missing = [path for path in requested_paths if path not in explicit]
                if missing:
                    raise ValueError("有文件已不属于当前大数据包，请刷新后重试")
                for path in requested_paths:
                    add_item(explicit[path])
        excluded = {str(item).replace("\\", "/").strip("/") for item in (exclude_paths or []) if str(item).strip()}
        excluded_ids = list(dict.fromkeys(str(value).strip() for value in (exclude_node_ids or []) if str(value).strip()))
        if excluded_ids:
            # 与包含侧同一套解析：分支展开成子组，叶组直接取成员，再扣减路径。
            with closing(self._connect()) as conn:
                from services.large_package_catalog_hierarchy import visible
                excluded_branches = {str(item.get("node_id")): item for item in visible(conn, package_id)}
                excluded_groups = []
                for node_id in excluded_ids:
                    branch = excluded_branches.get(node_id)
                    if branch:
                        excluded_groups.extend(str(child) for child in branch.get("children") or [])
                    else:
                        excluded_groups.append(node_id)
                excluded_groups = list(dict.fromkeys(excluded_groups))
                if excluded_groups:
                    placeholders = ",".join("?" for _ in excluded_groups)
                    rows = conn.execute(
                        "SELECT DISTINCT m.path FROM large_catalog_members m"
                        " WHERE m.package_id=? AND m.group_id IN (" + placeholders + ")",
                        [package_id] + excluded_groups,
                    ).fetchall()
                    excluded.update(str(row["path"]) for row in rows)
        if excluded:
            member_paths = [path for path in member_paths if path not in excluded]
            for path in excluded:
                inventory.pop(path, None)
        if not member_paths:
            raise ValueError("排除所选文件后没有可导出的文件" if excluded else "请至少选择一个智能目录或文件")
        return {"paths": member_paths, "inventory": inventory, "nodes": node_labels}
    def save_selection(self, package_id, paths, rule=None):
        selection_id = "sel-" + uuid.uuid4().hex
        now = time.time()
        unique = list(dict.fromkeys(str(path) for path in paths if str(path)))
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT status FROM packages WHERE id=?", (str(package_id),)).fetchone()
            if not current or current[0] not in {"waiting_for_selection", "completed"}:
                raise ValueError("请等待当前处理结束后提交深度解析范围")
            # **记录本批依据的目录版本**。这一列此前 20/20 条全是空的，于是没有
            # 任何记录能回答"这批是基于哪一版目录选的"——页面、报告、下载三处的
            # 一致性只能靠"当前恰好是同一版"这个巧合。
            # 取不到版本不阻塞提交（目录可能还没发布过），只记空字符串。
            from services.large_package_catalog_naming import ensure_catalog_version
            catalog_id = str(ensure_catalog_version(self, str(package_id), connection=conn) or '')
            conn.execute("INSERT INTO selections(id,package_id,paths,rule,created_at,updated_at,status,started_at,catalog_id) VALUES(?,?,?,?,?,?,'running',?,?)",
                         (selection_id, str(package_id), _json(unique), _json(rule or {}), now, now, now, catalog_id))
            for path in unique:
                # A later supplement may intentionally select a file whose
                # previous deep queue row is already completed.  INSERT OR
                # IGNORE leaves that row completed while the file record is
                # reset to queued, so the Worker waits forever on a phantom
                # pending file.  Requeue the durable row atomically instead.
                conn.execute("INSERT OR IGNORE INTO queue(package_id,path,stage,status,updated_at) VALUES(?,?,?,'queued',?)",
                             (str(package_id), path, "deep", now))
                conn.execute("UPDATE queue SET status='queued',attempts=0,leased_at=NULL,lease_token=NULL,error=NULL,updated_at=? WHERE package_id=? AND path=? AND stage='deep'",
                             (now, str(package_id), path))
                conn.execute("UPDATE files SET deep_status='queued',updated_at=? WHERE package_id=? AND path=?",
                             (now, str(package_id), path))
            conn.execute("UPDATE packages SET status='queued',phase='deep_parse',progress=0,message=?,updated_at=? WHERE id=?",
                         ("已收到深度解析选择，共 {} 个文件".format(len(unique)), now, str(package_id)))
            conn.commit()
        return {"selection_id": selection_id, "paths": unique}

    def checkpoint_wal(self, mode="PASSIVE"):
        """把 WAL 检查点从提交路径挪出来，在空闲处按节流调用。

        `PASSIVE` 不阻塞读写、拿不到锁就立刻返回 —— 这正是它适合放在
        Worker 主循环里的原因：最坏情况是"这次没检查成"，而不是"卡住提交"。
        """
        try:
            with closing(self._connect()) as conn:
                return conn.execute("PRAGMA wal_checkpoint(%s)" % mode).fetchone()
        except Exception:
            LOGGER.exception("wal checkpoint failed")
            return None

    def wal_size(self):
        """WAL 文件的当前大小（字节）；用于给检查点设硬上限。"""
        try:
            return os.path.getsize(self.db_path + "-wal")
        except OSError:
            return 0

    def stale_running_to_retry(self, seconds=300):
        """把失联的 running 项回收成 retry。

        W-7：默认从 900s 缩到 300s。理由（问题总册 F10/B7）：深度解析一个文件
        正常只要几十秒，900s 意味着**一次 worker 崩溃会让这个包停 15 分钟**才
        被回收；而回收本身是幂等且安全的 —— 旧持有者的租约令牌会被置 NULL，
        它晚到的提交会被 `finish_queue` / `commit_stage_result` 拒绝
        （这条路径有回归测试 test_large_package_lease 钉住）。
        300s 仍远大于单文件耗时，不会误杀在跑的活。
        """
        cutoff = time.time() - max(60, int(seconds))
        with self._lock, self._connect() as conn:
            # 回收时**同时轮换掉租约令牌**（置 NULL）：被判失联的持有者即使晚到，
            # 它的令牌也已经对不上，`finish_queue` / `requeue` 会拒绝它的写入。
            # 这是"旧 worker 覆盖新结果"这条路径上唯一的闸门。
            conn.execute("UPDATE queue SET status='retry',lease_token=NULL,updated_at=? WHERE status='running' AND updated_at<?", (time.time(), cutoff))

    def retry_failed_quick(self, package_id):
        """Requeue quick-parse failures so an operator can recover a package."""
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT path FROM files WHERE package_id=? AND quick_status='failed'",
                (str(package_id),),
            ).fetchall()
            paths = [str(row[0]) for row in rows]
            for path in paths:
                conn.execute(
                    "UPDATE files SET quick_status='queued',error=NULL,local_summary='{}',updated_at=? WHERE package_id=? AND path=?",
                    (now, str(package_id), path),
                )
                conn.execute(
                    "UPDATE queue SET status='queued',attempts=0,leased_at=NULL,lease_token=NULL,error=NULL,updated_at=? WHERE package_id=? AND path=? AND stage='quick'",
                    (now, str(package_id), path),
                )
            if paths:
                conn.execute(
                    "UPDATE packages SET status='queued',phase='quick_parse',progress=20,message=?,error=NULL,updated_at=? WHERE id=?",
                    ("已重新排队 {} 个失败文件".format(len(paths)), now, str(package_id)),
                )
            conn.commit()
        return paths


def _stream_hash_preview(path, limit=65536):
    digest = hashlib.sha256()
    head = bytearray()
    tail = bytearray()
    with Path(path).open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            if len(head) < limit:
                head.extend(block[: max(0, limit - len(head))])
            tail.extend(block)
            if len(tail) > limit:
                del tail[:-limit]
    return digest.hexdigest(), bytes(head), bytes(tail)


def _pdf_preview(path, limit=12000):
    """Extract a bounded PDF text preview without exposing binary PDF bytes."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path), strict=False)
        pages = len(reader.pages)
        chunks = []
        for page in reader.pages[:2]:
            try:
                value = page.extract_text() or ""
            except Exception:
                value = ""
            if value:
                chunks.append(value)
            if sum(len(chunk) for chunk in chunks) >= limit:
                break
        text = re.sub(r"\s+", " ", " ".join(chunks)).strip()[:limit]
        if text:
            return {"pages": pages, "sample": text, "parser": "pypdf", "notice": "仅提取前两页文本预览；深度解析可获取完整正文"}
        return {"pages": pages, "sample": "", "parser": "pypdf", "notice": "PDF 没有可直接提取的文本层；深度解析时将尝试 OCR"}
    except Exception as exc:
        return {"pages": None, "sample": "", "parser": "unavailable", "notice": "PDF 快速预览失败，已隐藏原始二进制内容：{}".format(str(exc)[:180])}


def _closing_entries(entries):
    try:
        yield from entries
    finally:
        entries.close()


def iter_inventory(root, on_error=None):
    root = _under_allowed_root(root)
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            entries = os.scandir(folder)
        except OSError as exc:
            if on_error:on_error(str(folder),str(exc))
            continue
        for entry in _closing_entries(entries):
            path = Path(entry.path)
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                if on_error:on_error(str(path),str(exc))
                continue
            rel = str(path.relative_to(root)).replace("\\", "/")
            if entry.is_dir(follow_symlinks=False):
                stack.append(path)
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            yield {
                "path": rel, "name": entry.name, "extension": path.suffix.lower(),
                "size": int(stat.st_size), "modified_at_ns": int(stat.st_mtime_ns),
                "sensitive": is_sensitive_file(entry.name),
            }


def _category(path, text, extension):
    value = (str(path) + " " + str(text or "")).casefold()
    if extension in {".csv", ".tsv", ".xlsx", ".xls", ".xlsm", ".json", ".jsonl"}:
        return "结构化数据"
    if extension in ARCHIVE_EXTENSIONS:
        return "压缩包"
    if extension in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
        return "图片扫描件"
    for category, words in (("财务资料", ("财务", "销售", "成本", "预算", "finance", "sales")),
                            ("合同制度", ("合同", "协议", "制度", "contract", "policy")),
                            ("项目与技术", ("项目", "技术", "系统", "工程", "project", "technical"))):
        if any(word in value for word in words):
            return category
    return "文本资料" if extension in TEXT_EXTENSIONS or text else "未分类"


def quick_preview(root, item, sample_limit=65536):
    from services.large_package_preview import preview_file
    return preview_file(root, item)


__all__ = ["LargePackageStore", "iter_inventory", "quick_preview", "_under_allowed_root"]


# ---------------------------------------------------------------------------
# 诊断开关（默认关闭；关闭时只有一次常数判断，零额外开销）
#
# 第九轮实测：`quick_parse` 里 66~85% 的时间花在逐文件的数据库记账上，
# 其中 `commit_stage_result` 占 11.2 ms/文件。这个开关把那一趟按步骤拆开。
# **不属于功能代码**，查完即可连同补丁一起撤掉。
#   SJFX_PROFILE_COMMIT=1            打开
#   SJFX_PROFILE_COMMIT_MIN_MS=3     只打印超过该毫秒数的提交（默认 0 = 全打）
# ---------------------------------------------------------------------------
_PROFILE_COMMIT = str(os.getenv("SJFX_PROFILE_COMMIT", "") or "").lower() in {"1", "true", "on", "yes"}
try:
    _PROFILE_COMMIT_MIN_MS = float(os.getenv("SJFX_PROFILE_COMMIT_MIN_MS", "0") or 0)
except (TypeError, ValueError):
    _PROFILE_COMMIT_MIN_MS = 0.0


def _m(pf, key, t0):
    if pf is not None:
        pf[key] = pf.get(key, 0.0) + (time.monotonic() - t0)


# 见 `_connect()` 的说明：默认关掉"提交时自动检查点"，改由 Worker 按节流做。
# 默认 20000 页（约 80 MB）：**不再让检查点落在提交路径上**，但保留一道兜底上限，
# 这样即使后台检查点线程没跑（例如 Worker 停了），WAL 也不会无限增长。
# 设 1000 回到 SQLite 默认行为。0 = 完全关闭自动检查点（不推荐，除非有别的检查点来源）。
try:
    _WAL_AUTOCHECKPOINT = max(0, int(os.getenv("SJFX_LP_WAL_AUTOCHECKPOINT", "20000") or 0))
except (TypeError, ValueError):
    _WAL_AUTOCHECKPOINT = 20000
