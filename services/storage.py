import json
import gzip
import hashlib
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from services.schema import normalize_summary


class Storage:
    def __init__(self, db_path, sidecar_dir=None, sidecar_threshold=256 * 1024):
        self.db_path = str(db_path)
        self.sidecar_dir = Path(sidecar_dir) if sidecar_dir else Path(self.db_path).parent / "document_payloads"
        self.sidecar_dir.mkdir(parents=True, exist_ok=True)
        self.sidecar_threshold = max(32 * 1024, int(sidecar_threshold))
        self.lock = threading.Lock()
        self._init_db()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS scans (
                    id TEXT PRIMARY KEY,
                    root_path TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS summaries (
                    scan_id TEXT NOT NULL,
                    node_path TEXT NOT NULL,
                    summary_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (scan_id, node_path, summary_type)
                );
                CREATE TABLE IF NOT EXISTS unified_documents (
                    scan_id TEXT NOT NULL,
                    node_path TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (scan_id, node_path)
                );
                CREATE TABLE IF NOT EXISTS package_analyses (
                    scan_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS analysis_jobs (
                    id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL,
                    task_type TEXT NOT NULL DEFAULT 'analyze_package',
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT 'queued',
                    progress INTEGER NOT NULL DEFAULT 0,
                    message TEXT,
                    result TEXT,
                    error TEXT,
                    options TEXT,
                    worker_id TEXT,
                    heartbeat_at REAL,
                    started_at REAL,
                    finished_at REAL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS file_analysis_states (
                    scan_id TEXT NOT NULL,
                    node_path TEXT NOT NULL,
                    fingerprint TEXT,
                    status TEXT NOT NULL,
                    parser TEXT,
                    stored_characters INTEGER NOT NULL DEFAULT 0,
                    evidence_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (scan_id, node_path)
                );
                CREATE TABLE IF NOT EXISTS retrieval_sessions (
                    result_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    evidence_ids TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_documents_scan_path ON unified_documents(scan_id, node_path);
                CREATE INDEX IF NOT EXISTS idx_summaries_scan_path ON summaries(scan_id, node_path);
                CREATE INDEX IF NOT EXISTS idx_retrieval_sessions_scan_created ON retrieval_sessions(scan_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_file_analysis_states_scan_status ON file_analysis_states(scan_id, status);
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(analysis_jobs)").fetchall()}
            migrations = {
                "options": "TEXT",
                "task_type": "TEXT NOT NULL DEFAULT 'analyze_package'",
                "stage": "TEXT NOT NULL DEFAULT 'queued'",
                "worker_id": "TEXT",
                "heartbeat_at": "REAL",
                "started_at": "REAL",
                "finished_at": "REAL",
                "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    conn.execute("ALTER TABLE analysis_jobs ADD COLUMN {} {}".format(name, definition))
            conn.execute("CREATE INDEX IF NOT EXISTS idx_analysis_jobs_queue ON analysis_jobs(status, updated_at)")

    def _sidecar_path(self, scan_id, node_path):
        digest = hashlib.sha256("{}|{}".format(scan_id, node_path).encode("utf-8")).hexdigest()
        folder = self.sidecar_dir / str(scan_id)
        folder.mkdir(parents=True, exist_ok=True)
        return folder / (digest + ".json.gz")

    @staticmethod
    def _document_projection(payload, text_limit=30000, evidence_limit=40):
        """A bounded representation used by trees, retrieval and handoff.

        Full payloads remain in sidecars for a selected document.  Package-wide
        operations must not hydrate a growing collection of full documents.
        """
        text = str(payload.get("text") or "")
        if len(text) > text_limit:
            head = int(text_limit * 0.72)
            tail = text_limit - head
            text = text[:head] + "\n\n[正文已折叠；选择文件可读取完整解析缓存]\n\n" + text[-tail:]
        projection = {
            key: payload.get(key)
            for key in ("schema_version", "source", "parsed_at", "parser", "structure", "coverage", "warnings", "classification", "content_sha256")
            if key in payload
        }
        projection["text"] = text
        projection["evidence"] = list(payload.get("evidence") or [])[:evidence_limit]
        projection["sidecar_projection"] = True
        return projection

    def _store_document_payload(self, scan_id, node_path, payload):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) <= self.sidecar_threshold:
            return raw.decode("utf-8")
        target = self._sidecar_path(scan_id, node_path)
        temporary = target.with_suffix(".tmp")
        with gzip.open(str(temporary), "wb") as stream:
            stream.write(raw)
        os.replace(str(temporary), str(target))
        return json.dumps({
            "__sidecar_payload__": True,
            "file": str(target.relative_to(self.sidecar_dir)).replace("\\", "/"),
            "source": payload.get("source", {}),
            "parser": payload.get("parser", {}),
            "coverage": payload.get("coverage", {}),
            "warnings": payload.get("warnings", []),
            "projection": self._document_projection(payload),
        }, ensure_ascii=False)

    def _load_document_payload(self, stored, hydrate=True):
        payload = json.loads(stored)
        if not payload.get("__sidecar_payload__"):
            return payload
        if not hydrate:
            projection = dict(payload.get("projection") or {})
            projection["sidecar_stored"] = True
            return projection
        relative = payload.get("file") or ""
        target = (self.sidecar_dir / relative).resolve()
        try:
            target.relative_to(self.sidecar_dir.resolve())
            with gzip.open(str(target), "rb") as stream:
                return json.loads(stream.read().decode("utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return {
                "source": payload.get("source", {}),
                "parser": payload.get("parser", {}),
                "coverage": payload.get("coverage", {}),
                "warnings": list(payload.get("warnings", [])) + ["解析缓存文件不可用：{}".format(exc)],
                "text": "",
                "evidence": [],
            }

    def recover_stale_jobs(self, stale_after_seconds=900):
        """Requeue only abandoned work, never a healthy active worker."""
        cutoff = time.time() - max(30, int(stale_after_seconds))
        with self.lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE analysis_jobs SET status='queued', stage='queued', worker_id=NULL, "
                "progress=MIN(progress,95), message=?, error=NULL, heartbeat_at=NULL, "
                "cancel_requested=0, updated_at=CURRENT_TIMESTAMP WHERE status='running' "
                "AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
                ("检测到中断的 Worker，任务已重新排队，将从检查点继续。", cutoff),
            )
            changed = cursor.rowcount
            cursor.close()
            cursor = conn.execute(
                "UPDATE analysis_jobs SET status='cancelled', stage='cancelled', worker_id=NULL, "
                "message=?, error=NULL, finished_at=?, heartbeat_at=NULL, updated_at=CURRENT_TIMESTAMP "
                "WHERE status='cancelling' AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
                ("Worker 中断时任务已处于取消状态，已安全结束。", time.time(), cutoff),
            )
            changed += cursor.rowcount
            cursor.close()
        return changed

    def list_queued_scan_ids(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT scan_id FROM analysis_jobs WHERE status='queued' ORDER BY rowid"
            ).fetchall()
        return [row["scan_id"] for row in rows]

    def save_retrieval_result(self, result_id, scan_id, scope, evidence_ids):
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO retrieval_sessions(result_id,scan_id,scope,evidence_ids) VALUES (?,?,?,?)",
                (result_id, scan_id, scope, json.dumps(list(evidence_ids), ensure_ascii=False)),
            )
            conn.execute(
                "DELETE FROM retrieval_sessions WHERE created_at < datetime('now', '-7 day')"
            )
            conn.execute(
                "DELETE FROM retrieval_sessions WHERE rowid IN ("
                "SELECT rowid FROM retrieval_sessions ORDER BY created_at DESC LIMIT -1 OFFSET 1000)"
            )

    def get_retrieval_result(self, result_id, scan_id=None):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT scan_id,scope,evidence_ids FROM retrieval_sessions WHERE result_id=?",
                (result_id,),
            ).fetchone()
        if not row or (scan_id and row["scan_id"] != scan_id):
            return None
        return {
            "scan_id": row["scan_id"],
            "scope": row["scope"],
            "evidence_ids": json.loads(row["evidence_ids"]),
        }

    def get_active_job(self, scan_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM analysis_jobs WHERE scan_id=? AND status IN ('queued','running') ORDER BY updated_at DESC LIMIT 1",
                (scan_id,),
            ).fetchone()
        if not row:
            return None
        return self._decode_job(dict(row))

    def save_scan(self, payload, scan_id=None):
        scan_id = scan_id or uuid.uuid4().hex[:12]
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO scans(id, root_path, payload) VALUES (?, ?, ?)",
                (scan_id, payload["root"], json.dumps(payload, ensure_ascii=False)),
            )
        return scan_id

    def get_scan(self, scan_id):
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM scans WHERE id=?", (scan_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def update_scan(self, scan_id, payload):
        with self.lock, self._connect() as conn:
            conn.execute(
                "UPDATE scans SET root_path=?, payload=? WHERE id=?",
                (payload["root"], json.dumps(payload, ensure_ascii=False), scan_id),
            )

    def save_summary(self, scan_id, node_path, summary_type, payload):
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO summaries(scan_id,node_path,summary_type,payload) VALUES (?,?,?,?)",
                (scan_id, node_path, summary_type, json.dumps(payload, ensure_ascii=False)),
            )

    def get_summary(self, scan_id, node_path, summary_type):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM summaries WHERE scan_id=? AND node_path=? AND summary_type=?",
                (scan_id, node_path, summary_type),
            ).fetchone()
        return normalize_summary(json.loads(row["payload"])) if row else None

    def list_summaries(self, scan_id):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT node_path,summary_type,payload FROM summaries WHERE scan_id=?",
                (scan_id,),
            ).fetchall()
        return [
            {"path": row["node_path"], "type": row["summary_type"], "payload": normalize_summary(json.loads(row["payload"]))}
            for row in rows
        ]

    def save_document(self, scan_id, node_path, payload):
        stored = self._store_document_payload(scan_id, node_path, payload)
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO unified_documents(scan_id,node_path,payload) VALUES (?,?,?)",
                (scan_id, node_path, stored),
            )

    def get_document(self, scan_id, node_path):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM unified_documents WHERE scan_id=? AND node_path=?",
                (scan_id, node_path),
            ).fetchone()
        return self._load_document_payload(row["payload"]) if row else None

    def list_documents(self, scan_id, hydrate=True):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT node_path,payload FROM unified_documents WHERE scan_id=? ORDER BY node_path",
                (scan_id,),
            ).fetchall()
        return [{"path": row["node_path"], "payload": self._load_document_payload(row["payload"], hydrate=hydrate)} for row in rows]

    def set_file_state(self, scan_id, node_path, fingerprint, status, document=None, error=None):
        document = document or {}
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO file_analysis_states("
                "scan_id,node_path,fingerprint,status,parser,stored_characters,evidence_count,error,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (
                    scan_id, node_path, fingerprint, status,
                    (document.get("parser") or {}).get("name"),
                    len(document.get("text") or ""),
                    len(document.get("evidence") or []),
                    error,
                ),
            )

    def get_file_state(self, scan_id, node_path):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM file_analysis_states WHERE scan_id=? AND node_path=?",
                (scan_id, node_path),
            ).fetchone()
        return dict(row) if row else None

    def list_file_states(self, scan_id):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM file_analysis_states WHERE scan_id=? ORDER BY node_path",
                (scan_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_analysis(self, scan_id, payload):
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO package_analyses(scan_id,payload) VALUES (?,?)",
                (scan_id, json.dumps(payload, ensure_ascii=False)),
            )

    def get_analysis(self, scan_id):
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM package_analyses WHERE scan_id=?", (scan_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def create_job(self, scan_id, options=None, task_type="analyze_package"):
        job_id = uuid.uuid4().hex[:12]
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO analysis_jobs(id,scan_id,task_type,status,stage,progress,message,options) VALUES (?,?,?,?,?,?,?,?)",
                (job_id, scan_id, task_type, "queued", "queued", 0, "等待开始", json.dumps(options or {}, ensure_ascii=False)),
            )
        return job_id

    def create_or_get_job(self, scan_id, options=None):
        return self.create_or_get_typed_job(scan_id, "analyze_package", options=options)

    def create_or_get_typed_job(self, scan_id, task_type, options=None):
        """Deduplicate only equivalent active work for the same data package."""
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM analysis_jobs WHERE scan_id=? AND task_type=? "
                "AND status IN ('queued','running','cancelling') ORDER BY updated_at DESC LIMIT 1",
                (scan_id, task_type),
            ).fetchone()
            if row:
                return row["id"], False
            job_id = uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO analysis_jobs(id,scan_id,task_type,status,stage,progress,message,options) VALUES (?,?,?,?,?,?,?,?)",
                (job_id, scan_id, task_type, "queued", "queued", 1, "已进入本地任务队列", json.dumps(options or {}, ensure_ascii=False)),
            )
        return job_id, True

    def create_scan_job(self, root_path, max_files, parse_mode, max_depth):
        """Create a scan-and-analyze workflow without touching the filesystem yet."""
        job_id = uuid.uuid4().hex[:12]
        options = {
            "root_path": str(root_path),
            "max_files": int(max_files),
            "parse_mode": str(parse_mode),
            "max_depth": int(max_depth),
        }
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO analysis_jobs(id,scan_id,task_type,status,stage,progress,message,options) VALUES (?,?,?,?,?,?,?,?)",
                (job_id, job_id, "scan_and_analyze", "queued", "queued", 0, "等待扫描目录", json.dumps(options, ensure_ascii=False)),
            )
        return job_id

    def cancel_queued_jobs(self, except_scan_id=None):
        """Deprecated compatibility method; a new scan must never cancel another.

        Jobs are retained in the local FIFO queue.  Explicit cancellation is a
        separate product action and cannot be inferred from a later upload.
        """
        return 0

    def get_running_job(self):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM analysis_jobs WHERE status='running' ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return self._decode_job(dict(row))

    def get_queue_position(self, job_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT rowid AS queue_order FROM analysis_jobs WHERE id=? AND status='queued'",
                (job_id,),
            ).fetchone()
            if not row:
                return None
            ahead = conn.execute(
                "SELECT COUNT(*) AS value FROM analysis_jobs "
                "WHERE status='queued' AND rowid < ?",
                (row["queue_order"],),
            ).fetchone()["value"]
        return int(ahead) + 1

    def update_job(self, job_id, status=None, progress=None, message=None, result=None, error=None, stage=None, heartbeat=False):
        fields = ["updated_at=CURRENT_TIMESTAMP"]
        values = []
        for name, value in (("status", status), ("stage", stage), ("progress", progress), ("message", message), ("result", result), ("error", error)):
            if value is not None:
                fields.append(name + "=?")
                values.append(json.dumps(value, ensure_ascii=False) if name == "result" else value)
        if heartbeat:
            fields.append("heartbeat_at=?")
            values.append(time.time())
        if status in {"completed", "failed", "cancelled"}:
            fields.append("finished_at=?")
            values.append(time.time())
        values.append(job_id)
        with self.lock, self._connect() as conn:
            condition = "id=?"
            if status not in {"cancelled", "cancelling"}:
                condition += " AND status NOT IN ('cancelled','cancelling')"
            conn.execute("UPDATE analysis_jobs SET {} WHERE {}".format(",".join(fields), condition), values)

    def cancel_job(self, job_id):
        with self.lock, self._connect() as conn:
            conn.execute(
                "UPDATE analysis_jobs SET status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END, "
                "cancel_requested=1, message=?, error=NULL, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status IN ('queued','running','cancelling')",
                ("已请求取消任务，当前步骤结束后停止。", job_id),
            )

    def claim_next_job(self, worker_id):
        """Atomically claim the oldest queued job for one independent worker."""
        with self.lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id FROM analysis_jobs WHERE status='queued' ORDER BY rowid LIMIT 1"
            ).fetchone()
            if not row:
                return None
            now = time.time()
            cursor = conn.execute(
                "UPDATE analysis_jobs SET status='running', stage='claimed', worker_id=?, started_at=COALESCE(started_at,?), heartbeat_at=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status='queued'",
                (str(worker_id), now, now, row["id"]),
            )
            if cursor.rowcount != 1:
                return None
            claimed = conn.execute("SELECT * FROM analysis_jobs WHERE id=?", (row["id"],)).fetchone()
        return self._decode_job(dict(claimed)) if claimed else None

    @staticmethod
    def _decode_job(result):
        result["result"] = json.loads(result["result"]) if result.get("result") else None
        result["options"] = json.loads(result["options"]) if result.get("options") else {}
        result["cancel_requested"] = bool(result.get("cancel_requested"))
        return result

    def get_job(self, job_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM analysis_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        return self._decode_job(dict(row))
