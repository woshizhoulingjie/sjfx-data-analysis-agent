"""Isolated streaming large-package workflow.

This module deliberately does not use the standard import task tables.  It
stores a durable manifest and per-file queues in ``large_package.db`` so a
long-running package cannot change the ordinary import workflow.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from config import Config
from services.scanner import human_size, is_sensitive_file


TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl",
    ".xml", ".html", ".htm", ".log", ".ini", ".cfg", ".yaml", ".yml",
}
ARCHIVE_EXTENSIONS = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".tbz", ".7z", ".rar"}
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,8}")
STOPWORDS = {"the", "and", "for", "with", "from", "this", "that", "数据", "资料", "文件", "分析", "报告"}


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
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
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
            conn.execute("INSERT INTO packages(id,root_path,owner_id,status,phase,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                         (package_id, root, owner_id or "legacy", "queued", "inventory", now, now))
        return self.get(package_id, owner_id)

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
            args.append(_json(value) if key == "counts" else value)
        args.append(str(package_id))
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE packages SET " + ",".join(columns) + " WHERE id=?", args)
        return self.get(package_id)

    def upsert_file(self, package_id, item):
        now = time.time()
        values = (str(package_id), item["path"], item["name"], item.get("extension", ""),
                  int(item.get("size") or 0), int(item.get("modified_at_ns") or 0),
                  item.get("sha256"), 1 if item.get("sensitive") else 0,
                  item.get("file_type") or item.get("extension") or "")
        with self._lock, self._connect() as conn:
            conn.execute("""INSERT INTO files(package_id,path,name,extension,size,modified_at_ns,sha256,sensitive,file_type,updated_at)
              VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(package_id,path) DO UPDATE SET
              name=excluded.name,extension=excluded.extension,size=excluded.size,
              modified_at_ns=excluded.modified_at_ns,sha256=COALESCE(excluded.sha256,files.sha256),
              sensitive=excluded.sensitive,file_type=excluded.file_type,updated_at=excluded.updated_at""", values + (now,))
            conn.execute("INSERT OR IGNORE INTO queue(package_id,path,stage,status,updated_at) VALUES(?,?,?,'queued',?)",
                         (str(package_id), item["path"], "quick", now))

    def claim(self, stage, limit=1, package_id=None):
        claimed = []
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            sql = "SELECT package_id,path FROM queue WHERE stage=? AND status IN ('queued','retry')"
            args = [stage]
            if package_id:
                sql += " AND package_id=?"
                args.append(str(package_id))
            sql += " ORDER BY updated_at LIMIT ?"
            args.append(int(limit))
            rows = conn.execute(sql, args).fetchall()
            for row in rows:
                conn.execute("UPDATE queue SET status='running',attempts=attempts+1,leased_at=?,updated_at=? WHERE package_id=? AND path=? AND stage=?",
                             (now, now, row["package_id"], row["path"], stage))
                claimed.append((row["package_id"], row["path"]))
            conn.commit()
        return claimed

    def finish_queue(self, package_id, path, stage, ok=True, error=None):
        status = "completed" if ok else "failed"
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE queue SET status=?,error=?,updated_at=? WHERE package_id=? AND path=? AND stage=?",
                         (status, error, time.time(), str(package_id), str(path), stage))

    def update_file(self, package_id, path, **fields):
        allowed = {"sha256", "quick_status", "deep_status", "category", "confidence", "local_summary", "deep_summary", "error", "file_type"}
        fields = {key: value for key, value in fields.items() if key in allowed}
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols, args = [], []
        for key, value in fields.items():
            cols.append(key + "=?")
            args.append(_json(value) if key in {"local_summary", "deep_summary"} else value)
        args.extend([str(package_id), str(path)])
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE files SET " + ",".join(cols) + " WHERE package_id=? AND path=?", args)

    def counts(self, package_id):
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM files WHERE package_id=?", (str(package_id),)).fetchone()[0]
            quick = dict(conn.execute("SELECT quick_status,COUNT(*) n FROM files WHERE package_id=? GROUP BY quick_status", (str(package_id),)).fetchall())
            deep = dict(conn.execute("SELECT deep_status,COUNT(*) n FROM files WHERE package_id=? GROUP BY deep_status", (str(package_id),)).fetchall())
            cats = dict(conn.execute("SELECT category,COUNT(*) n FROM files WHERE package_id=? GROUP BY category ORDER BY n DESC", (str(package_id),)).fetchall())
        return {"total_files": int(total), "quick": quick, "deep": deep, "categories": cats}

    def list_files(self, package_id, limit=100, offset=0, query="", category=""):
        where = ["package_id=?"]
        args = [str(package_id)]
        if query:
            where.append("(path LIKE ? OR name LIKE ? OR local_summary LIKE ?)")
            like = "%" + str(query) + "%"
            args.extend([like, like, like])
        if category:
            where.append("category=?")
            args.append(str(category))
        args.extend([max(1, min(500, int(limit))), max(0, int(offset))])
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM files WHERE " + " AND ".join(where) + " ORDER BY path LIMIT ? OFFSET ?", args).fetchall()
        return [self._row(row) for row in rows]

    def get_file(self, package_id, path):
        """Fetch one manifest row by its exact relative path."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM files WHERE package_id=? AND path=?",
                (str(package_id), str(path)),
            ).fetchone()
        return self._row(row) if row else None

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
        """Build compact original-directory and smart-category views.

        This is read-only and belongs exclusively to ``large_package.db``.
        The response contains directory/category rollups plus small samples,
        so the UI does not need to load every file in a large package at once.
        """
        with self._connect() as conn:
            package = conn.execute("SELECT root_path FROM packages WHERE id=?", (str(package_id),)).fetchone()
            rows = conn.execute(
                "SELECT path,size,category,confidence,quick_status,deep_status FROM files "
                "WHERE package_id=? ORDER BY path", (str(package_id),)
            ).fetchall()
        if not package:
            return None
        directories = {}
        categories = {}
        for row in rows:
            path = str(row["path"] or "")
            parts = [part for part in Path(path).parts if part not in (".", "")]
            directory_parts = parts[:-1]
            prefixes = []
            for part in directory_parts:
                prefixes.append(part)
                directory = "/".join(prefixes)
                item = directories.setdefault(directory, {"path": directory, "files": 0, "bytes": 0, "sample": []})
                item["files"] += 1
                item["bytes"] += int(row["size"] or 0)
                if len(item["sample"]) < 12 and path not in item["sample"]:
                    item["sample"].append(path)
            category = str(row["category"] or "未分类")
            item = categories.setdefault(category, {"category": category, "files": 0, "bytes": 0, "confidence_total": 0.0, "sample": []})
            item["files"] += 1
            item["bytes"] += int(row["size"] or 0)
            item["confidence_total"] += float(row["confidence"] or 0)
            if len(item["sample"]) < 12:
                item["sample"].append({"path": path, "quick_status": row["quick_status"], "deep_status": row["deep_status"]})
        for item in categories.values():
            item["confidence"] = round(item.pop("confidence_total") / max(1, item["files"]), 3)
        return {
            "root_path": str(package["root_path"]),
            "files_total": len(rows),
            "original": {"directories": sorted(directories.values(), key=lambda value: value["path"]),
                         "root_files": [row["path"] for row in rows if len(Path(str(row["path"] or "")).parts) <= 1][:50]},
            "smart": {"categories": sorted(categories.values(), key=lambda value: (-value["files"], value["category"]))},
        }

    def existing_paths(self, package_id, paths):
        values = [str(path) for path in paths if str(path)]
        if not values:
            return []
        placeholders = ",".join("?" for _ in values)
        with self._connect() as conn:
            rows = conn.execute("SELECT path FROM files WHERE package_id=? AND path IN (" + placeholders + ")",
                                [str(package_id)] + values).fetchall()
        return [str(row[0]) for row in rows]

    def save_selection(self, package_id, paths, rule=None):
        selection_id = "sel-" + uuid.uuid4().hex
        now = time.time()
        unique = list(dict.fromkeys(str(path) for path in paths if str(path)))
        with self._lock, self._connect() as conn:
            conn.execute("INSERT INTO selections(id,package_id,paths,rule,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                         (selection_id, str(package_id), _json(unique), _json(rule or {}), now, now))
            for path in unique:
                # A later supplement may intentionally select a file whose
                # previous deep queue row is already completed.  INSERT OR
                # IGNORE leaves that row completed while the file record is
                # reset to queued, so the Worker waits forever on a phantom
                # pending file.  Requeue the durable row atomically instead.
                conn.execute("INSERT OR IGNORE INTO queue(package_id,path,stage,status,updated_at) VALUES(?,?,?,'queued',?)",
                             (str(package_id), path, "deep", now))
                conn.execute("UPDATE queue SET status='queued',attempts=0,leased_at=NULL,error=NULL,updated_at=? WHERE package_id=? AND path=? AND stage='deep'",
                             (now, str(package_id), path))
                conn.execute("UPDATE files SET deep_status='queued',updated_at=? WHERE package_id=? AND path=?",
                             (now, str(package_id), path))
            conn.execute("UPDATE packages SET status='queued',phase='deep_parse',progress=0,message=?,updated_at=? WHERE id=?",
                         ("已收到深度解析选择，共 {} 个文件".format(len(unique)), now, str(package_id)))
        return {"selection_id": selection_id, "paths": unique}

    def stale_running_to_retry(self, seconds=900):
        cutoff = time.time() - max(60, int(seconds))
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE queue SET status='retry',updated_at=? WHERE status='running' AND updated_at<?", (time.time(), cutoff))

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
                    "UPDATE queue SET status='queued',attempts=0,leased_at=NULL,error=NULL,updated_at=? WHERE package_id=? AND path=? AND stage='quick'",
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


def iter_inventory(root):
    root = _under_allowed_root(root)
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            entries = sorted(os.scandir(folder), key=lambda entry: entry.name.casefold())
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
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
    path = Path(root) / item["path"]
    if item.get("sensitive"):
        return {"status": "restricted", "title": item["name"], "notice": "敏感文件，仅保留元数据", "sample": ""}, None
    try:
        sha256, head, tail = _stream_hash_preview(path, sample_limit)
        extension = str(item.get("extension") or Path(item["name"]).suffix).lower()
        metadata = {}
        if extension == ".pdf":
            metadata = _pdf_preview(path)
            text = metadata.get("sample") or ""
        elif extension in TEXT_EXTENSIONS:
            text = head.decode("utf-8", errors="replace")
            if len(tail) and tail != head:
                text += "\n…\n" + tail.decode("utf-8", errors="replace")
            text = re.sub(r"\s+", " ", text).strip()
        else:
            text = ""
            metadata = {"notice": "该文件不是纯文本，快速阶段仅保留元数据；深度解析时再提取正文"}
        keywords = []
        for token in WORD_RE.findall(text):
            token = token.lower()
            if token not in STOPWORDS and token not in keywords:
                keywords.append(token)
            if len(keywords) >= 20:
                break
        category = _category(item["path"], text, item.get("extension", ""))
        summary = {
            "schema_version": "large-local-summary/1.0", "summary_type": "local",
            "title": Path(item["name"]).stem, "category": category,
            "keywords": keywords, "sample": text[:4000],
            "sampled_bytes": min(int(item.get("size") or 0), sample_limit * 2),
            "size": int(item.get("size") or 0), "size_human": human_size(item.get("size") or 0),
            "complete": False, "generated_at": utc_now(),
        }
        summary.update({key: value for key, value in metadata.items() if value not in (None, "")})
        return summary, sha256
    except Exception as exc:
        return {"status": "failed", "title": item.get("name"), "error": str(exc)[:500]}, None


__all__ = ["LargePackageStore", "iter_inventory", "quick_preview", "_under_allowed_root"]
