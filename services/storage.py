import json
import gzip
import hashlib
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from services.schema import normalize_summary


LOGGER = logging.getLogger(__name__)


class Storage:
    def __init__(self, db_path, sidecar_dir=None, sidecar_threshold=256 * 1024):
        self.db_path = str(db_path)
        self.sidecar_dir = Path(sidecar_dir) if sidecar_dir else Path(self.db_path).parent / "document_payloads"
        self.sidecar_dir.mkdir(parents=True, exist_ok=True)
        self.sidecar_threshold = max(32 * 1024, int(sidecar_threshold))
        self.lock = threading.Lock()
        self._checkpoint_lock = threading.Lock()
        self._last_checkpoint = 0.0
        self.sqlite_busy_timeout_ms = self._env_int(
            "SJFX_SQLITE_BUSY_TIMEOUT_MS", 30000, minimum=1000, maximum=300000
        )
        self.sqlite_wal_autocheckpoint = self._env_int(
            "SJFX_SQLITE_WAL_AUTOCHECKPOINT", 1000, minimum=100, maximum=100000
        )
        self.sqlite_journal_size_limit = self._env_int(
            "SJFX_SQLITE_JOURNAL_SIZE_LIMIT", 64 * 1024 * 1024,
            minimum=1024 * 1024, maximum=4 * 1024 * 1024 * 1024,
        )
        self.sqlite_checkpoint_interval = self._env_int(
            "SJFX_SQLITE_CHECKPOINT_INTERVAL", 60, minimum=10, maximum=3600
        )
        self._init_db()

    @staticmethod
    def _env_int(name, default, minimum=None, maximum=None):
        try:
            value = int(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            value = int(default)
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(
            self.db_path, timeout=self.sqlite_busy_timeout_ms / 1000.0
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(
                "PRAGMA busy_timeout={}".format(self.sqlite_busy_timeout_ms)
            )
            # These two pragmas are connection-local.  Applying them here
            # prevents a new API/Worker connection from silently reverting to
            # SQLite's tiny default checkpoint threshold.
            connection.execute(
                "PRAGMA wal_autocheckpoint={}".format(self.sqlite_wal_autocheckpoint)
            )
            connection.execute(
                "PRAGMA journal_size_limit={}".format(self.sqlite_journal_size_limit)
            )
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
            conn.execute(
                "PRAGMA wal_autocheckpoint={}".format(self.sqlite_wal_autocheckpoint)
            )
            conn.execute(
                "PRAGMA journal_size_limit={}".format(self.sqlite_journal_size_limit)
            )
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS scans (
                    id TEXT PRIMARY KEY,
                    root_path TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    owner_id TEXT NOT NULL DEFAULT 'legacy',
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
                    owner_id TEXT NOT NULL DEFAULT 'legacy',
                    worker_id TEXT,
                    heartbeat_at REAL,
                    started_at REAL,
                    finished_at REAL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    current_stage TEXT,
                    current_file TEXT,
                    priority INTEGER NOT NULL DEFAULT 50,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
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
                CREATE TABLE IF NOT EXISTS output_artifacts (
                    filename TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    scan_id TEXT,
                    job_id TEXT,
                    kind TEXT NOT NULL DEFAULT 'result',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS embedding_cache (
                    cache_key TEXT NOT NULL,
                    model TEXT NOT NULL,
                    vector TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (cache_key, model)
                );
                CREATE TABLE IF NOT EXISTS evidence_index (
                    scan_id TEXT NOT NULL,
                    index_key TEXT NOT NULL,
                    source_path TEXT,
                    archive_source_path TEXT,
                    payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (scan_id, index_key)
                );
                CREATE INDEX IF NOT EXISTS idx_documents_scan_path ON unified_documents(scan_id, node_path);
                CREATE INDEX IF NOT EXISTS idx_summaries_scan_path ON summaries(scan_id, node_path);
                CREATE INDEX IF NOT EXISTS idx_retrieval_sessions_scan_created ON retrieval_sessions(scan_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_file_analysis_states_scan_status ON file_analysis_states(scan_id, status);
                CREATE INDEX IF NOT EXISTS idx_embedding_cache_updated ON embedding_cache(updated_at);
                CREATE INDEX IF NOT EXISTS idx_evidence_index_scan_source ON evidence_index(scan_id, source_path);
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
                "current_stage": "TEXT",
                "current_file": "TEXT",
                "priority": "INTEGER NOT NULL DEFAULT 50",
                "created_at": "TEXT",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    conn.execute("ALTER TABLE analysis_jobs ADD COLUMN {} {}".format(name, definition))
            job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(analysis_jobs)").fetchall()}
            if "owner_id" not in job_columns:
                conn.execute("ALTER TABLE analysis_jobs ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'legacy'")
            conn.execute(
                "UPDATE analysis_jobs SET created_at=COALESCE(created_at, updated_at, CURRENT_TIMESTAMP)"
            )
            scan_columns = {row["name"] for row in conn.execute("PRAGMA table_info(scans)").fetchall()}
            if "owner_id" not in scan_columns:
                conn.execute("ALTER TABLE scans ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'legacy'")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_analysis_jobs_queue "
                "ON analysis_jobs(status, priority DESC, created_at)"
            )

    def checkpoint_wal(self, force=False):
        """Bound the SQLite WAL without deleting a live database.

        ``PASSIVE`` is used for periodic maintenance so readers are never
        blocked.  ``TRUNCATE`` is reserved for Worker startup (or an explicit
        forced call) and only removes frames SQLite has already checkpointed.
        The return value is intentionally diagnostic and safe to expose in
        health checks.
        """
        now = time.monotonic()
        if not force and now - self._last_checkpoint < self.sqlite_checkpoint_interval:
            return {"skipped": True, "reason": "interval", "wal_bytes": self._wal_size()}
        with self._checkpoint_lock:
            now = time.monotonic()
            if not force and now - self._last_checkpoint < self.sqlite_checkpoint_interval:
                return {"skipped": True, "reason": "interval", "wal_bytes": self._wal_size()}
            wal_bytes = self._wal_size()
            mode = "TRUNCATE" if force or wal_bytes >= self.sqlite_journal_size_limit else "PASSIVE"
            try:
                with self._connect() as conn:
                    row = conn.execute("PRAGMA wal_checkpoint({})".format(mode)).fetchone()
                values = list(row) if row is not None else []
                result = {
                    "skipped": False,
                    "mode": mode,
                    "busy": int(values[0]) if len(values) > 0 else 0,
                    "log_frames": int(values[1]) if len(values) > 1 else 0,
                    "checkpointed_frames": int(values[2]) if len(values) > 2 else 0,
                    "wal_bytes_before": wal_bytes,
                    "wal_bytes": self._wal_size(),
                }
                self._last_checkpoint = now
                return result
            except sqlite3.DatabaseError as exc:
                LOGGER.warning("SQLite WAL checkpoint 失败：%s", exc)
                self._last_checkpoint = now
                return {
                    "skipped": False,
                    "mode": mode,
                    "error": str(exc),
                    "wal_bytes": self._wal_size(),
                }

    def _wal_size(self):
        try:
            return int(Path(self.db_path + "-wal").stat().st_size)
        except (OSError, ValueError):
            return 0

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
            for key in ("schema_version", "source", "parsed_at", "parser", "structure", "coverage", "archive_manifest", "warnings", "classification", "deduplication", "content_sha256", "data_profile", "data_profiles")
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
            "archive_manifest": payload.get("archive_manifest"),
            "deduplication": payload.get("deduplication", {}),
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

    def recover_orphaned_jobs_after_lock(self):
        """Recover every unfinished claim after acquiring the Worker lock.

        The caller must hold the project's exclusive Worker process lock. At
        that point no healthy queue owner can still exist, even when its final
        heartbeat is only a few seconds old. Keeping this takeover separate
        from time-based stale recovery prevents a second process from
        requeueing work that is still legitimately running.
        """
        now = time.time()
        with self.lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE analysis_jobs SET status='queued', stage='queued', worker_id=NULL, "
                "progress=MIN(progress,95), message=?, error=NULL, heartbeat_at=NULL, "
                "cancel_requested=0, finished_at=NULL, updated_at=CURRENT_TIMESTAMP "
                "WHERE status='running'",
                ("检测到 Worker 已重启，任务已重新排队，将从已保存的检查点继续。",),
            )
            changed = cursor.rowcount
            cursor.close()
            cursor = conn.execute(
                "UPDATE analysis_jobs SET status='cancelled', stage='cancelled', worker_id=NULL, "
                "message=?, error=NULL, finished_at=?, heartbeat_at=NULL, current_stage=?, "
                "current_file='', updated_at=CURRENT_TIMESTAMP WHERE status='cancelling'",
                ("Worker 重启前已请求取消，任务现已安全结束。", now, "已取消"),
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

    def get_embeddings(self, cache_keys, model):
        """Return reusable local embedding vectors without loading all rows."""
        keys = list(dict.fromkeys(str(value) for value in cache_keys if value))
        if not keys or not model:
            return {}
        output = {}
        with self._connect() as conn:
            for start in range(0, len(keys), 400):
                batch = keys[start:start + 400]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    "SELECT cache_key,vector FROM embedding_cache WHERE model=? AND cache_key IN ({})".format(placeholders),
                    [str(model)] + batch,
                ).fetchall()
                for row in rows:
                    try:
                        vector = json.loads(row["vector"])
                        if isinstance(vector, list) and vector:
                            output[row["cache_key"]] = vector
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
        return output

    def save_embeddings(self, model, vectors):
        """Atomically upsert a bounded batch of local embedding vectors."""
        rows = []
        for cache_key, vector in (vectors or {}).items():
            if not cache_key or not isinstance(vector, list) or not vector:
                continue
            rows.append((
                str(cache_key), str(model), json.dumps(vector, ensure_ascii=False, separators=(",", ":")), len(vector)
            ))
        if not rows:
            return 0
        with self.lock, self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO embedding_cache(cache_key,model,vector,dimensions,updated_at) "
                "VALUES (?,?,?,?,CURRENT_TIMESTAMP)",
                rows,
            )
        return len(rows)

    def replace_evidence_index(self, scan_id, chunks):
        """Publish one canonical evidence catalog in a single transaction."""
        rows = []
        for item in chunks or []:
            payload = dict(item)
            source_path = str(payload.get("source_path") or "")
            archive_source = str(payload.get("archive_source_path") or "")
            identity = "{}|{}|{}|{}".format(
                source_path,
                payload.get("evidence_id") or "",
                payload.get("content_sha256") or "",
                payload.get("text") or "",
            )
            index_key = hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()
            rows.append((
                str(scan_id), index_key, source_path, archive_source,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ))
        with self.lock, self._connect() as conn:
            conn.execute("DELETE FROM evidence_index WHERE scan_id=?", (str(scan_id),))
            if rows:
                conn.executemany(
                    "INSERT INTO evidence_index(scan_id,index_key,source_path,archive_source_path,payload) VALUES (?,?,?,?,?)",
                    rows,
                )
        return len(rows)

    def list_evidence_index(self, scan_id):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM evidence_index WHERE scan_id=? ORDER BY rowid",
                (str(scan_id),),
            ).fetchall()
        output = []
        for row in rows:
            try:
                item = json.loads(row["payload"])
                if isinstance(item, dict):
                    output.append(item)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return output

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

    def save_scan(self, payload, scan_id=None, owner_id="legacy"):
        scan_id = scan_id or uuid.uuid4().hex[:12]
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO scans(id, root_path, payload, owner_id) VALUES (?, ?, ?, ?)",
                (scan_id, payload["root"], json.dumps(payload, ensure_ascii=False), owner_id or "legacy"),
            )
        return scan_id

    def migrate_legacy_ownership(self, owner_id):
        """Bind pre-authentication records to the configured owner once."""
        if not owner_id:
            return
        with self.lock, self._connect() as conn:
            conn.execute(
                "UPDATE scans SET owner_id=? WHERE owner_id IS NULL OR owner_id IN ('legacy','default')",
                (owner_id,),
            )
            conn.execute(
                "UPDATE analysis_jobs SET owner_id=? WHERE owner_id IS NULL OR owner_id IN ('legacy','default')",
                (owner_id,),
            )
            conn.execute(
                "UPDATE output_artifacts SET owner_id=? WHERE owner_id IS NULL OR owner_id IN ('legacy','default')",
                (owner_id,),
            )

    def register_existing_outputs(self, output_dir, owner_id):
        """Register legacy output files so existing links remain protected."""
        if not owner_id:
            return
        output_dir = Path(output_dir)
        with self.lock, self._connect() as conn:
            for item in output_dir.iterdir() if output_dir.exists() else ():
                if not item.is_file() or item.name.startswith("."):
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO output_artifacts(filename,owner_id,kind) VALUES (?,?,?)",
                    (item.name, owner_id, "legacy_result"),
                )

    def save_artifact(self, filename, owner_id, scan_id=None, job_id=None, kind="result"):
        if not filename or not owner_id:
            return
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO output_artifacts(filename,owner_id,scan_id,job_id,kind) VALUES (?,?,?,?,?)",
                (str(filename), str(owner_id), scan_id, job_id, kind or "result"),
            )

    def artifact_owned(self, filename, owner_id=None):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT owner_id FROM output_artifacts WHERE filename=?",
                (str(filename),),
            ).fetchone()
        if not row:
            return False
        return not owner_id or row["owner_id"] == owner_id

    def artifact_owner(self, filename):
        """Return the registered owner, or None when the artifact is unregistered."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT owner_id FROM output_artifacts WHERE filename=?",
                (str(filename),),
            ).fetchone()
        return row["owner_id"] if row else None

    def get_scan(self, scan_id, owner_id=None):
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT payload, owner_id FROM scans WHERE id=?", (scan_id,)).fetchone()
            if not row:
                return None
            stored_owner = row["owner_id"] or "legacy"
            if owner_id and stored_owner != owner_id:
                return None
        return json.loads(row["payload"])

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

    def save_summaries(self, scan_id, summaries):
        """Publish many local summaries in one transaction.

        Large inventories can contain tens of thousands of nodes. Committing
        one SQLite transaction per node makes the 82% stage look stalled and
        needlessly grows the WAL. The caller still builds bounded batches so
        cancellation and progress remain visible between commits.
        """
        rows = []
        for node_path, summary_type, payload in summaries or []:
            rows.append((
                str(scan_id), str(node_path), str(summary_type),
                json.dumps(payload, ensure_ascii=False),
            ))
        if not rows:
            return 0
        with self.lock, self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO summaries(scan_id,node_path,summary_type,payload) VALUES (?,?,?,?)",
                rows,
            )
        return len(rows)

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

    def save_documents(self, scan_id, documents):
        rows = []
        for node_path, payload in documents or []:
            rows.append((
                str(scan_id), str(node_path),
                self._store_document_payload(scan_id, node_path, payload),
            ))
        if not rows:
            return 0
        with self.lock, self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO unified_documents(scan_id,node_path,payload) VALUES (?,?,?)",
                rows,
            )
        return len(rows)

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

    @staticmethod
    def _job_priority(task_type):
        # New package imports and explicit supplement analysis are the primary
        # workflow. Optional summaries/reports must not indefinitely hide them
        # behind a long FIFO tail. A running task is never pre-empted.
        return {
            "scan_and_analyze": 100,
            "analyze_package": 80,
            "export_package": 60,
            "generate_summary": 40,
            "generate_report": 30,
        }.get(str(task_type or ""), 50)

    def create_job(self, scan_id, options=None, task_type="analyze_package", owner_id="legacy"):
        job_id = uuid.uuid4().hex[:12]
        priority = self._job_priority(task_type)
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO analysis_jobs(id,scan_id,task_type,status,stage,progress,message,options,owner_id,priority,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (job_id, scan_id, task_type, "queued", "queued", 0, "等待开始", json.dumps(options or {}, ensure_ascii=False), owner_id or "legacy", priority),
            )
        return job_id

    def create_or_get_job(self, scan_id, options=None, owner_id="legacy"):
        return self.create_or_get_typed_job(scan_id, "analyze_package", options=options, owner_id=owner_id)

    def create_or_get_typed_job(self, scan_id, task_type, options=None, owner_id="legacy"):
        """Deduplicate only equivalent active work for the same data package."""
        with self.lock, self._connect() as conn:
            options_json = json.dumps(options or {}, ensure_ascii=False, sort_keys=True)
            # Scope-aware deduplication: two different topic selections must
            # never reuse one another's active supplement task.
            scope = (options or {}).get("target_paths") or []
            scope_key = (
                json.dumps(sorted(set(str(item) for item in scope)), ensure_ascii=False)
                if task_type == "analyze_package" else options_json
            )
            rows = conn.execute(
                "SELECT id, options, owner_id FROM analysis_jobs WHERE scan_id=? AND task_type=? "
                "AND status IN ('queued','running') AND cancel_requested=0 ORDER BY updated_at DESC",
                (scan_id, task_type),
            ).fetchall()
            row = None
            for candidate in rows:
                try:
                    candidate_options = json.loads(candidate["options"] or "{}")
                except (TypeError, ValueError):
                    candidate_options = {}
                candidate_scope = (
                    json.dumps(sorted(set(str(item) for item in (candidate_options.get("target_paths") or []))), ensure_ascii=False)
                    if task_type == "analyze_package"
                    else json.dumps(candidate_options, ensure_ascii=False, sort_keys=True)
                )
                candidate_owner = candidate["owner_id"] or "legacy"
                if candidate_scope == scope_key and (not owner_id or candidate_owner == owner_id):
                    row = candidate
                    break
            if row:
                return row["id"], False
            job_id = uuid.uuid4().hex[:12]
            priority = self._job_priority(task_type)
            conn.execute(
                "INSERT INTO analysis_jobs(id,scan_id,task_type,status,stage,progress,message,options,owner_id,priority,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (job_id, scan_id, task_type, "queued", "queued", 1, "已进入本地任务队列", options_json, owner_id or "legacy", priority),
            )
        return job_id, True

    def create_scan_job(self, root_path, max_files, parse_mode, max_depth, owner_id="legacy"):
        """Create a scan-and-analyze workflow without touching the filesystem yet."""
        job_id = uuid.uuid4().hex[:12]
        options = {
            "root_path": str(root_path),
            "max_files": int(max_files),
            "parse_mode": str(parse_mode),
            "max_depth": int(max_depth),
            "owner_id": owner_id or "legacy",
        }
        priority = self._job_priority("scan_and_analyze")
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO analysis_jobs(id,scan_id,task_type,status,stage,progress,message,options,owner_id,priority,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (job_id, job_id, "scan_and_analyze", "queued", "queued", 0, "等待扫描目录", json.dumps(options, ensure_ascii=False), owner_id or "legacy", priority),
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
                "SELECT * FROM analysis_jobs WHERE status IN ('running','cancelling') "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return self._decode_job(dict(row))

    def get_queue_position(self, job_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT rowid AS queue_order, priority FROM analysis_jobs WHERE id=? AND status='queued'",
                (job_id,),
            ).fetchone()
            if not row:
                return None
            ahead = conn.execute(
                "SELECT COUNT(*) AS value FROM analysis_jobs "
                "WHERE status='queued' AND cancel_requested=0 AND "
                "(priority > ? OR (priority = ? AND rowid < ?))",
                (row["priority"], row["priority"], row["queue_order"]),
            ).fetchone()["value"]
        return int(ahead) + 1

    def update_job(self, job_id, status=None, progress=None, message=None, result=None, error=None, stage=None,
                   heartbeat=False, current_stage=None, current_file=None):
        fields = ["updated_at=CURRENT_TIMESTAMP"]
        values = []
        if current_stage is None and stage is not None:
            current_stage = stage
        for name, value in (("status", status), ("stage", stage), ("message", message), ("result", result), ("error", error), ("current_stage", current_stage), ("current_file", current_file)):
            if value is not None:
                fields.append(name + "=?")
                values.append(json.dumps(value, ensure_ascii=False) if name == "result" else value)
        if progress is not None:
            # A stage transition must never make a live progress bar move
            # backwards (for example scan 15% -> analysis 2%).
            fields.append("progress=MAX(progress,?)")
            values.append(max(0, min(100, int(progress))))
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

    def heartbeat_job(self, job_id, worker_id=None):
        """Refresh liveness without overwriting child-stage progress text."""
        now = time.time()
        values = [now]
        worker_clause = ""
        if worker_id:
            worker_clause = ", worker_id=COALESCE(worker_id,?)"
            values.append(str(worker_id))
        values.append(job_id)
        with self.lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE analysis_jobs SET heartbeat_at=?{} "
                "WHERE id=? AND status IN ('running','cancelling')".format(worker_clause),
                values,
            )
            return cursor.rowcount == 1

    def is_job_cancel_requested(self, job_id):
        """Cheap cooperative cancellation probe for parser/model loops."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status, cancel_requested FROM analysis_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        return bool(row and (row["cancel_requested"] or row["status"] in {"cancelled", "cancelling"}))

    def cancel_job(self, job_id):
        """Cancel queued work immediately or request termination of live work."""
        now = time.time()
        with self.lock, self._connect() as conn:
            conn.execute(
                "UPDATE analysis_jobs SET status='cancelled', stage='cancelled', cancel_requested=1, "
                "message=?, current_stage=?, current_file='', worker_id=NULL, heartbeat_at=NULL, "
                "finished_at=?, error=NULL, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status='queued'",
                ("排队任务已取消。", "已取消", now, job_id),
            )
            conn.execute(
                "UPDATE analysis_jobs SET status='cancelling', stage='cancelling', cancel_requested=1, "
                "message=?, current_stage=?, error=NULL, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status IN ('running','cancelling')",
                ("正在停止当前步骤，已完成的检查点会保留。", "正在取消", job_id),
            )
            row = conn.execute("SELECT * FROM analysis_jobs WHERE id=?", (job_id,)).fetchone()
        return self._decode_job(dict(row)) if row else None

    def finalize_job(self, job_id, result=None, error=None):
        """Atomically resolve completion/cancellation races.

        A cancel request arriving after execution returns but before the final
        write must win. Otherwise the old guarded update silently leaves a job
        in ``cancelling`` forever.
        """
        now = time.time()
        with self.lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status,cancel_requested FROM analysis_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if not row:
                return None
            cancelled = bool(row["cancel_requested"] or row["status"] in {"cancelled", "cancelling"})
            if cancelled:
                status, stage, message, stored_error, progress = (
                    "cancelled", "cancelled", "任务已取消", None, None,
                )
            elif error is not None:
                status, stage, message, stored_error, progress = (
                    "failed", "failed", "任务失败", str(error), 100,
                )
            else:
                status, stage, message, stored_error, progress = (
                    "completed", "completed", "任务已完成", None, 100,
                )
            fields = [
                "status=?", "stage=?", "message=?", "error=?", "finished_at=?",
                "heartbeat_at=?", "worker_id=NULL", "current_stage=?", "current_file=''",
                "updated_at=CURRENT_TIMESTAMP",
            ]
            values = [status, stage, message, stored_error, now, now, "已取消" if cancelled else stage]
            if progress is not None:
                fields.append("progress=MAX(progress,?)")
                values.append(progress)
            if result is not None and not cancelled and error is None:
                fields.append("result=?")
                values.append(json.dumps(result, ensure_ascii=False))
            values.append(job_id)
            conn.execute(
                "UPDATE analysis_jobs SET {} WHERE id=?".format(",".join(fields)),
                values,
            )
            final = conn.execute("SELECT * FROM analysis_jobs WHERE id=?", (job_id,)).fetchone()
        return self._decode_job(dict(final)) if final else None

    def claim_next_job(self, worker_id):
        """Atomically claim the oldest queued job for one independent worker."""
        with self.lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id FROM analysis_jobs WHERE status='queued' AND cancel_requested=0 "
                "ORDER BY priority DESC, rowid LIMIT 1"
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

    def list_jobs(self, owner_id=None, statuses=None, limit=50):
        """Return an owner-scoped task history for the product task center."""
        limit = max(1, min(200, int(limit or 50)))
        clauses = []
        values = []
        if owner_id:
            clauses.append("owner_id=?")
            values.append(owner_id)
        statuses = [str(item) for item in (statuses or []) if item]
        if statuses:
            clauses.append("status IN ({})".format(",".join("?" for _ in statuses)))
            values.extend(statuses)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM analysis_jobs{} ORDER BY "
                "CASE WHEN status IN ('running','cancelling') THEN 0 "
                "WHEN status='queued' THEN 1 ELSE 2 END, "
                "COALESCE(started_at,0) DESC, rowid DESC LIMIT ?".format(where),
                values,
            ).fetchall()
        return [self._decode_job(dict(row)) for row in rows]

    @staticmethod
    def _decode_job(result):
        result["result"] = json.loads(result["result"]) if result.get("result") else None
        result["options"] = json.loads(result["options"]) if result.get("options") else {}
        result["cancel_requested"] = bool(result.get("cancel_requested"))
        return result

    def get_job(self, job_id, owner_id=None):
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM analysis_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            stored_owner = row["owner_id"] or "legacy"
            if owner_id and stored_owner != owner_id:
                return None
        return self._decode_job(dict(row))
