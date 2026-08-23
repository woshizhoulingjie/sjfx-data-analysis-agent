import json
import gzip
import hashlib
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from config import Config, _mount_filesystem
from services.schema import normalize_summary
from services.tree_editor import apply_tree_edits


LOGGER = logging.getLogger(__name__)


class Storage:
    def __init__(self, db_path, sidecar_dir=None, sidecar_threshold=256 * 1024):
        self.db_path = str(db_path)
        if os.name != "nt":
            filesystem = _mount_filesystem(Path(self.db_path).parent)
            network_sqlite_allowed = os.getenv("SJFX_ALLOW_NETWORK_SQLITE", "0").strip().lower() in {"1", "true", "yes"}
            if filesystem and (filesystem.startswith("nfs") or filesystem in {"cifs", "smbfs"}) and not network_sqlite_allowed:
                raise RuntimeError(
                    "拒绝在网络文件系统 {} 上运行 SQLite WAL；请将 SJFX_STATE_DIR 指向本机 ext4/xfs。".format(filesystem)
                )
        self.sidecar_dir = Path(sidecar_dir) if sidecar_dir else Path(self.db_path).parent / "document_payloads"
        self.sidecar_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            try:
                self.sidecar_dir.chmod(0o700)
            except OSError:
                pass
        self.sidecar_threshold = max(32 * 1024, int(sidecar_threshold))
        self.evidence_fts_available = False
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
        if os.name != "nt":
            try:
                Path(self.db_path).chmod(0o600)
            except OSError:
                pass

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
                    attempt_count INTEGER NOT NULL DEFAULT 0,
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
                CREATE TABLE IF NOT EXISTS tree_edits (
                    scan_id TEXT NOT NULL,
                    edit_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL DEFAULT 'legacy',
                    operation TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (scan_id, edit_id)
                );
                CREATE TABLE IF NOT EXISTS scan_overviews (
                    scan_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS analysis_overviews (
                    scan_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS analysis_progress (
                    scan_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS tree_nodes (
                    scan_id TEXT NOT NULL,
                    tree_kind TEXT NOT NULL,
                    node_key TEXT NOT NULL,
                    parent_key TEXT,
                    position INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL,
                    child_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (scan_id, tree_kind, node_key)
                );
                CREATE TABLE IF NOT EXISTS download_tickets (
                    token_hash TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    used_at REAL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_documents_scan_path ON unified_documents(scan_id, node_path);
                CREATE INDEX IF NOT EXISTS idx_summaries_scan_path ON summaries(scan_id, node_path);
                CREATE INDEX IF NOT EXISTS idx_retrieval_sessions_scan_created ON retrieval_sessions(scan_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_file_analysis_states_scan_status ON file_analysis_states(scan_id, status);
                CREATE INDEX IF NOT EXISTS idx_embedding_cache_updated ON embedding_cache(updated_at);
                CREATE INDEX IF NOT EXISTS idx_evidence_index_scan_source ON evidence_index(scan_id, source_path);
                CREATE INDEX IF NOT EXISTS idx_tree_nodes_parent
                    ON tree_nodes(scan_id, tree_kind, parent_key, position, node_key);
                CREATE INDEX IF NOT EXISTS idx_download_tickets_expiry
                    ON download_tickets(expires_at, used_at);
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
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
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
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5("
                    "scan_id UNINDEXED,index_key UNINDEXED,source_path UNINDEXED,"
                    "archive_source_path UNINDEXED,section,text,tokenize='unicode61')"
                )
                self.evidence_fts_available = True
            except sqlite3.DatabaseError as exc:
                # Some minimal Python/SQLite builds omit FTS5.  Retrieval then
                # uses a bounded LIKE candidate query, never an all-row load.
                LOGGER.warning("SQLite FTS5 不可用，将使用有界候选检索：%s", exc)
                self.evidence_fts_available = False

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
    def _bounded_projection_value(value, depth=0):
        """Bound nested metadata retained in a package-wide projection."""
        if depth >= 4:
            if isinstance(value, (dict, list, tuple)):
                return "[复杂字段已折叠]"
            return str(value)[:500] if isinstance(value, str) else value
        if isinstance(value, dict):
            output = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 60:
                    output["__omitted_fields__"] = len(value) - 60
                    break
                output[str(key)] = Storage._bounded_projection_value(item, depth + 1)
            return output
        if isinstance(value, (list, tuple)):
            kept = [Storage._bounded_projection_value(item, depth + 1) for item in value[:30]]
            if len(value) > 30:
                kept.append({"__omitted_items__": len(value) - 30})
            return kept
        if isinstance(value, str) and len(value) > 2000:
            return value[:1200] + "\n[字段已折叠]\n" + value[-600:]
        return value

    @staticmethod
    def _sample_sequence(values, limit):
        values = list(values or [])
        limit = max(0, int(limit))
        if not limit or not values:
            return []
        if len(values) <= limit:
            return values
        if limit == 1:
            return [values[0]]
        positions = sorted(set(
            round(index * (len(values) - 1) / float(limit - 1))
            for index in range(limit)
        ))
        return [values[index] for index in positions]

    @staticmethod
    def _sample_text(text, text_limit):
        text = str(text or "")
        text_limit = max(0, int(text_limit))
        if not text_limit or len(text) <= text_limit:
            return text
        marker = "\n\n[包级语义投影已折叠正文；选择文件可读取完整 sidecar]\n\n"
        budget = max(0, text_limit - len(marker))
        head = int(budget * 0.50)
        middle = int(budget * 0.25)
        tail = budget - head - middle
        middle_start = max(head, (len(text) - middle) // 2)
        return (
            text[:head] + marker + text[middle_start:middle_start + middle]
            + marker + (text[-tail:] if tail else "")
        )

    @staticmethod
    def _document_projection(payload, text_limit=None, evidence_limit=None):
        """A bounded representation used by trees, retrieval and handoff.

        Full payloads remain in sidecars for a selected document.  Package-wide
        operations must not hydrate a growing collection of full documents.
        """
        text_limit = int(text_limit or Config.LARGE_PACKAGE_OVERVIEW_CHARS_PER_FILE)
        evidence_limit = int(evidence_limit or Config.LARGE_PACKAGE_OVERVIEW_EVIDENCE_PER_FILE)
        original_text = str(payload.get("text") or "")
        original_evidence = list(payload.get("evidence") or [])
        original_data_profiles = list(payload.get("data_profiles") or [])
        text = Storage._sample_text(original_text, text_limit)
        evidence = Storage._sample_sequence(original_evidence, evidence_limit)
        projection = {
            key: Storage._bounded_projection_value(payload.get(key))
            for key in ("schema_version", "source", "parsed_at", "parser", "structure", "coverage", "archive_manifest", "warnings", "classification", "deduplication", "content_sha256", "data_profile", "data_profiles")
            if key in payload
        }
        projection["text"] = text
        projection["evidence"] = evidence
        if original_data_profiles:
            # Reset the nesting budget per archive member so the numeric
            # column summaries needed by structured QA remain dictionaries.
            projection["data_profiles"] = [
                Storage._bounded_projection_value(item)
                for item in original_data_profiles[:30]
            ]
        declared_profile_total = max(
            len(original_data_profiles),
            int(payload.get("data_profiles_total") or 0),
        )
        if declared_profile_total:
            projected_profile_count = sum(
                1 for item in (projection.get("data_profiles") or [])
                if isinstance(item, dict) and isinstance(item.get("profile"), dict)
            )
            projection["data_profiles_total"] = declared_profile_total
            projection["data_profiles_projected_count"] = projected_profile_count
            projection["data_profiles_omitted_count"] = max(
                0, declared_profile_total - projected_profile_count
            )
        projection["sidecar_projection"] = True
        coverage = dict(projection.get("coverage") or {})
        parse_complete = bool(coverage.get("parse_complete", coverage.get("complete", False)))
        coverage.update({
            # ``complete`` is retained for old clients, but a projection may
            # never advertise itself as complete semantic analysis.
            "complete": False,
            "parse_complete": parse_complete,
            "semantic_complete": False,
            "semantic_projection": True,
            "overview_sampled": True,
            "projection_text_truncated": len(original_text) > len(text),
            "projection_evidence_truncated": len(original_evidence) > len(evidence),
            "projection_source_characters": len(original_text),
            "projection_stored_characters": len(text),
            "projection_source_evidence": len(original_evidence),
            "projection_stored_evidence": len(evidence),
            "coverage_ratio_reason": "包级分析使用有界语义投影；原文件解析完整性与包级语义覆盖率分别报告。",
        })
        projection["coverage"] = coverage
        return projection

    def project_document(self, payload, text_limit=None, evidence_limit=None):
        """Return the bounded package-wide view while full text stays stored."""
        return self._document_projection(
            payload,
            text_limit=text_limit,
            evidence_limit=evidence_limit,
        )

    def _store_document_payload(self, scan_id, node_path, payload):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) <= self.sidecar_threshold:
            return raw.decode("utf-8")
        target = self._sidecar_path(scan_id, node_path)
        temporary = target.with_suffix(".tmp")
        with gzip.open(str(temporary), "wb") as stream:
            stream.write(raw)
        os.replace(str(temporary), str(target))
        if os.name != "nt":
            try:
                target.chmod(0o600)
            except OSError:
                pass
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
            # Inline rows can still contain hundreds of thousands of
            # characters.  Package-wide callers using hydrate=False must not
            # retain those full payloads merely because they fell below the
            # sidecar threshold.
            return payload if hydrate else self._document_projection(payload)
        if not hydrate:
            # Re-project old sidecars so tightened limits and corrected
            # coverage semantics apply without a full reparse.
            projection = self._document_projection(payload.get("projection") or payload)
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
            if self.evidence_fts_available:
                conn.execute("DELETE FROM evidence_fts WHERE scan_id=?", (str(scan_id),))
            if rows:
                conn.executemany(
                    "INSERT INTO evidence_index(scan_id,index_key,source_path,archive_source_path,payload) VALUES (?,?,?,?,?)",
                    rows,
                )
                if self.evidence_fts_available:
                    fts_rows = []
                    for scan_value, index_key, source_path, archive_source, payload_json in rows:
                        payload = json.loads(payload_json)
                        fts_rows.append((
                            scan_value, index_key, source_path, archive_source,
                            str(payload.get("section") or ""), str(payload.get("text") or ""),
                        ))
                    conn.executemany(
                        "INSERT INTO evidence_fts(scan_id,index_key,source_path,archive_source_path,section,text) "
                        "VALUES (?,?,?,?,?,?)",
                        fts_rows,
                    )
        return len(rows)

    def replace_document_evidence_index(self, scan_id, node_path, chunks):
        """Replace one source's evidence so large parses can commit per file."""
        scan_id = str(scan_id)
        node_path = str(node_path)
        rows = []
        for item in chunks or []:
            payload = dict(item)
            source_path = str(payload.get("source_path") or node_path)
            archive_source = str(payload.get("archive_source_path") or "")
            identity = "{}|{}|{}|{}".format(
                source_path, payload.get("evidence_id") or "",
                payload.get("content_sha256") or "", payload.get("text") or "",
            )
            index_key = hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()
            rows.append((
                scan_id, index_key, source_path, archive_source,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ))
        delete_sql = (
            "scan_id=? AND (source_path=? OR archive_source_path=? OR source_path LIKE ?)"
        )
        delete_values = (scan_id, node_path, node_path, node_path + "::%")
        with self.lock, self._connect() as conn:
            conn.execute("DELETE FROM evidence_index WHERE " + delete_sql, delete_values)
            if self.evidence_fts_available:
                conn.execute("DELETE FROM evidence_fts WHERE " + delete_sql, delete_values)
            if rows:
                conn.executemany(
                    "INSERT INTO evidence_index(scan_id,index_key,source_path,archive_source_path,payload) VALUES (?,?,?,?,?)",
                    rows,
                )
                if self.evidence_fts_available:
                    conn.executemany(
                        "INSERT INTO evidence_fts(scan_id,index_key,source_path,archive_source_path,section,text) VALUES (?,?,?,?,?,?)",
                        [
                            (row[0], row[1], row[2], row[3],
                             str(json.loads(row[4]).get("section") or ""),
                             str(json.loads(row[4]).get("text") or ""))
                            for row in rows
                        ],
                    )
        return len(rows)

    def count_evidence_index(self, scan_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS value FROM evidence_index WHERE scan_id=?", (str(scan_id),)
            ).fetchone()
        return int(row["value"] if row else 0)

    def clear_evidence_index(self, scan_id):
        with self.lock, self._connect() as conn:
            conn.execute("DELETE FROM evidence_index WHERE scan_id=?", (str(scan_id),))
            if self.evidence_fts_available:
                conn.execute("DELETE FROM evidence_fts WHERE scan_id=?", (str(scan_id),))

    @staticmethod
    def _retrieval_terms(query):
        values = re.findall(r"[A-Za-z][A-Za-z0-9_.-]{1,}|[\u4e00-\u9fff]{2,}", str(query or "").lower())
        terms = []
        for value in values:
            if re.fullmatch(r"[\u4e00-\u9fff]+", value):
                terms.append(value)
                terms.extend(value[index:index + 2] for index in range(max(0, len(value) - 1)))
            else:
                terms.append(value)
        return list(dict.fromkeys(item for item in terms if item))[:16]

    def search_evidence_index(self, scan_id, query, scope=".", source_paths=None,
                              candidate_evidence_ids=None, limit=2500):
        """Return a bounded candidate set from SQLite before local reranking."""
        scan_id = str(scan_id)
        scope = str(scope or ".")
        limit = max(50, min(5000, int(limit or 2500)))
        source_paths = sorted(set(str(item) for item in (source_paths or []) if item))
        candidate_ids = set(str(item) for item in (candidate_evidence_ids or []) if item)
        terms = self._retrieval_terms(query)
        rows = []
        with self._connect() as conn:
            if source_paths:
                conn.execute("CREATE TEMP TABLE IF NOT EXISTS retrieval_allowed_sources(path TEXT PRIMARY KEY)")
                conn.execute("DELETE FROM retrieval_allowed_sources")
                conn.executemany(
                    "INSERT OR IGNORE INTO retrieval_allowed_sources(path) VALUES (?)",
                    [(path,) for path in source_paths],
                )
            if self.evidence_fts_available and terms:
                # OR keeps Chinese bigrams useful with unicode61 while SQLite
                # bm25() orders the candidate window without Python objects.
                match = " OR ".join('"{}"'.format(term.replace('"', '""')) for term in terms)
                fts_scope = []
                fts_values = [scan_id, match]
                if source_paths:
                    fts_scope.append(
                        "(EXISTS (SELECT 1 FROM retrieval_allowed_sources a "
                        "WHERE a.path=f.source_path OR a.path=f.archive_source_path))"
                    )
                elif scope != ".":
                    fts_scope.append(
                        "(f.source_path=? OR f.source_path LIKE ? OR f.source_path LIKE ? "
                        "OR f.archive_source_path=? OR f.archive_source_path LIKE ?)"
                    )
                    fts_values.extend((scope, scope.rstrip("/") + "/%", scope + "::%", scope, scope.rstrip("/") + "/%"))
                fts_values.append(limit)
                try:
                    rows = conn.execute(
                        "SELECT e.payload FROM evidence_fts f JOIN evidence_index e "
                        "ON e.scan_id=f.scan_id AND e.index_key=f.index_key "
                        "WHERE f.scan_id=? AND evidence_fts MATCH ?{} ORDER BY bm25(evidence_fts) LIMIT ?".format(
                            " AND " + " AND ".join(fts_scope) if fts_scope else ""
                        ),
                        fts_values,
                    ).fetchall()
                except sqlite3.DatabaseError:
                    rows = []
            if not rows:
                clauses = ["scan_id=?"]
                values = [scan_id]
                if terms:
                    clauses.append("(" + " OR ".join("payload LIKE ?" for _ in terms) + ")")
                    values.extend("%{}%".format(term) for term in terms)
                if source_paths:
                    clauses.append(
                        "(EXISTS (SELECT 1 FROM retrieval_allowed_sources a "
                        "WHERE a.path=evidence_index.source_path OR a.path=evidence_index.archive_source_path))"
                    )
                elif scope != ".":
                    clauses.append(
                        "(source_path=? OR source_path LIKE ? OR source_path LIKE ? "
                        "OR archive_source_path=? OR archive_source_path LIKE ?)"
                    )
                    values.extend((scope, scope.rstrip("/") + "/%", scope + "::%", scope, scope.rstrip("/") + "/%"))
                values.append(limit)
                rows = conn.execute(
                    "SELECT payload FROM evidence_index WHERE {} ORDER BY rowid LIMIT ?".format(
                        " AND ".join(clauses)
                    ), values,
                ).fetchall()
        output = []
        scope_prefix = scope.rstrip("/") + "/"
        for row in rows:
            try:
                item = json.loads(row["payload"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            path = str(item.get("archive_source_path") or item.get("source_path") or "")
            if scope != "." and not (path == scope or path.startswith(scope_prefix) or path.startswith(scope + "::")):
                continue
            if source_paths and not any(
                path == source or path.startswith(source + "/") or path.startswith(source + "::")
                for source in source_paths
            ):
                continue
            if candidate_ids and str(item.get("evidence_id") or "") not in candidate_ids:
                continue
            output.append(item)
        return output

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

    @staticmethod
    def _scan_overview_payload(payload):
        """Return scan metadata without embedding a potentially huge tree."""
        source = dict(payload or {})
        tree = source.pop("tree", None)
        source.pop("analysis_tree", None)
        source.pop("analysis", None)
        source["tree_available"] = bool(tree)
        type_counts = list((source.get("type_counts") or {}).items())
        source["type_counts_total"] = len(type_counts)
        source["type_counts"] = dict(type_counts[:200])
        errors = list(source.get("errors") or [])
        source["errors"] = errors[:100]
        source["scan_error_count"] = int(source.get("scan_error_count", len(errors)) or 0)
        if isinstance(tree, dict):
            source["tree_root"] = {
                key: tree.get(key)
                for key in (
                    "kind", "name", "path", "size", "size_human", "file_count",
                    "directory_count", "direct_file_count", "direct_directory_count",
                )
                if tree.get(key) is not None
            }
        return source

    @staticmethod
    def _compact_coverage_payload(coverage):
        """Bound archive/path diagnostics embedded in UI coverage cards."""
        if not isinstance(coverage, dict):
            return coverage
        result = dict(coverage)
        for key, limit in (("limitations", 100), ("pending_paths", 200), ("failed_paths", 200)):
            values = list(result.get(key) or [])
            if values:
                result[key] = values[:limit]
                result[key + "_total"] = len(values)
                result[key + "_truncated"] = len(values) > limit
        manifests = list(result.pop("archive_containers", []) or [])
        if manifests:
            previews = []
            allowed_keys = (
                "container_path", "total_members", "parsed_members", "skipped_members",
                "failed_members", "truncated_members", "coverage_status",
                "member_coverage_ratio", "encrypted_members", "inventory_truncated",
                "total_members_is_lower_bound", "skip_reasons", "limits",
            )
            for manifest in manifests[:20]:
                compact = {key: manifest.get(key) for key in allowed_keys if key in manifest}
                records = list(manifest.get("member_records") or [])
                compact["member_records_total"] = len(records)
                compact["member_records_preview"] = records[:10]
                compact["member_records_truncated"] = bool(
                    manifest.get("member_records_truncated") or len(records) > 10
                )
                previews.append(compact)
            result["archive_containers_total"] = len(manifests)
            result["archive_containers_preview"] = previews
            result["archive_containers_truncated"] = len(manifests) > 20
        return result

    @staticmethod
    def _analysis_overview_payload(payload):
        """Keep UI/report statistics while excluding large indexes and trees."""
        source = payload or {}
        keys = (
            "schema_version", "scan_id", "root", "status", "started_from_scan_at",
            "completed_at", "parser_status", "statistics", "coverage", "overview",
            "value_judgment", "structured_data_overview", "policy",
            "classification_dimensions", "semantic_cluster_threshold",
            "semantic_naming_model", "subtopic_naming_model", "semantic_cluster_error",
        )
        result = {key: source.get(key) for key in keys if key in source}
        if "coverage" in result:
            result["coverage"] = Storage._compact_coverage_payload(result["coverage"])
        structured = result.get("structured_data_overview")
        if isinstance(structured, dict):
            structured = dict(structured)
            profiles = list(structured.pop("profiles", []) or [])
            structured["profile_preview_count"] = min(20, len(profiles))
            structured["profiles_preview"] = profiles[:20]
            result["structured_data_overview"] = structured
        result["analysis_tree_available"] = bool(source.get("analysis_tree"))
        failures = list(source.get("failures") or [])
        result["failures_preview"] = failures[:100]
        result["failures_total"] = len(failures)
        return result

    @staticmethod
    def _tree_key(node, tree_kind, parent_key, position):
        if tree_kind == "physical":
            path = str((node or {}).get("path") or "").strip().replace("\\", "/")
            if path:
                return path
        node_id = str((node or {}).get("node_id") or "").strip()
        if node_id:
            return "node:{}".format(node_id)
        seed = "{}\x1f{}\x1f{}\x1f{}\x1f{}".format(
            tree_kind, parent_key or "", position,
            (node or {}).get("kind") or "", (node or {}).get("path") or (node or {}).get("name") or "",
        )
        return "{}:{}".format(tree_kind, hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24])

    @classmethod
    def _replace_tree_index(cls, conn, scan_id, tree_kind, tree):
        """Normalize a nested tree in bounded batches for lazy browser access."""
        conn.execute(
            "DELETE FROM tree_nodes WHERE scan_id=? AND tree_kind=?",
            (str(scan_id), str(tree_kind)),
        )
        if not isinstance(tree, dict) or not tree:
            return 0
        written = 0
        batch = []
        stack = [(tree, None, 0)]
        while stack:
            node, parent_key, position = stack.pop()
            node_key = cls._tree_key(node, tree_kind, parent_key, position)
            children = [item for item in (node.get("children") or []) if isinstance(item, dict)]
            projection = dict(node)
            projection.pop("children", None)
            if "coverage" in projection:
                projection["coverage"] = cls._compact_coverage_payload(projection["coverage"])
            # A semantic node can represent tens of thousands of documents.
            # The server keeps the authoritative list in package_analyses; the
            # navigation projection only needs a bounded representative set.
            member_paths = list(projection.get("member_paths") or [])
            if member_paths:
                projection["member_count"] = len(member_paths)
                projection["member_paths"] = member_paths[:20]
                projection["member_paths_truncated"] = len(member_paths) > 20
            projection["_tree_key"] = node_key
            projection["has_children"] = bool(children)
            projection["child_count"] = len(children)
            batch.append((
                str(scan_id), str(tree_kind), node_key, parent_key, int(position),
                json.dumps(projection, ensure_ascii=False), len(children),
            ))
            written += 1
            if len(batch) >= 1000:
                conn.executemany(
                    "INSERT INTO tree_nodes(scan_id,tree_kind,node_key,parent_key,position,payload,child_count) "
                    "VALUES (?,?,?,?,?,?,?)",
                    batch,
                )
                batch = []
            for child_position in range(len(children) - 1, -1, -1):
                stack.append((children[child_position], node_key, child_position))
        if batch:
            conn.executemany(
                "INSERT INTO tree_nodes(scan_id,tree_kind,node_key,parent_key,position,payload,child_count) "
                "VALUES (?,?,?,?,?,?,?)",
                batch,
            )
        return written

    def refresh_tree_index(self, scan_id, tree_kind, tree):
        if tree_kind not in {"physical", "analysis"} and not str(tree_kind).startswith("analysis:"):
            raise ValueError("未知目录树类型")
        with self.lock, self._connect() as conn:
            if tree_kind == "analysis":
                conn.execute(
                    "DELETE FROM tree_nodes WHERE scan_id=? AND tree_kind LIKE 'analysis:%'",
                    (str(scan_id),),
                )
            return self._replace_tree_index(conn, scan_id, tree_kind, tree)

    def tree_index_exists(self, scan_id, tree_kind):
        """Return whether an indexed tree root already exists.

        Filtered semantic trees are derived from the authoritative analysis.
        Their indexes are invalidated whenever that analysis changes, so this
        inexpensive lookup lets repeated UI paging reuse a valid projection.
        """
        if tree_kind not in {"physical", "analysis"} and not str(tree_kind).startswith("analysis:"):
            raise ValueError("未知目录树类型")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM tree_nodes WHERE scan_id=? AND tree_kind=? "
                "AND parent_key IS NULL LIMIT 1",
                (str(scan_id), str(tree_kind)),
            ).fetchone()
        return bool(row)

    def save_scan(self, payload, scan_id=None, owner_id="legacy"):
        scan_id = scan_id or uuid.uuid4().hex[:12]
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO scans(id, root_path, payload, owner_id) VALUES (?, ?, ?, ?)",
                (scan_id, payload["root"], json.dumps(payload, ensure_ascii=False), owner_id or "legacy"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO scan_overviews(scan_id,payload,updated_at) "
                "VALUES (?,?,CURRENT_TIMESTAMP)",
                (scan_id, json.dumps(self._scan_overview_payload(payload), ensure_ascii=False)),
            )
            self._replace_tree_index(conn, scan_id, "physical", payload.get("tree") or {})
        return scan_id

    def migrate_legacy_ownership(self, owner_id, aliases=None):
        """Bind legacy/token-derived owner aliases to one stable owner id."""
        if not owner_id:
            return
        aliases = {
            str(value) for value in (aliases or ("legacy", "default"))
            if value and str(value) != str(owner_id)
        }
        if not aliases:
            return
        placeholders = ",".join("?" for _ in aliases)
        values = [str(owner_id)] + sorted(aliases)
        with self.lock, self._connect() as conn:
            conn.execute(
                "UPDATE scans SET owner_id=? WHERE owner_id IS NULL OR owner_id IN ({})".format(placeholders),
                values,
            )
            conn.execute(
                "UPDATE analysis_jobs SET owner_id=? WHERE owner_id IS NULL OR owner_id IN ({})".format(placeholders),
                values,
            )
            conn.execute(
                "UPDATE output_artifacts SET owner_id=? WHERE owner_id IS NULL OR owner_id IN ({})".format(placeholders),
                values,
            )

    def register_existing_outputs(self, output_dir, owner_id):
        """Register legacy output files so existing links remain protected."""
        if not owner_id:
            return
        output_dir = Path(output_dir)
        with self.lock, self._connect() as conn:
            for item in output_dir.iterdir() if output_dir.exists() else ():
                if item.is_symlink() or not item.is_file() or item.name.startswith("."):
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

    def create_download_ticket(self, filename, owner_id, ttl_seconds=120):
        """Issue a short-lived one-use bearer for a browser navigation download."""
        if not filename or not owner_id:
            raise ValueError("下载文件或所有者无效")
        ttl = max(30, min(600, int(ttl_seconds or 120)))
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.time()
        with self.lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM download_tickets WHERE expires_at<? OR (used_at IS NOT NULL AND used_at<?)",
                (now - 3600, now - 3600),
            )
            conn.execute(
                "INSERT INTO download_tickets(token_hash,filename,owner_id,expires_at,created_at) "
                "VALUES (?,?,?,?,?)",
                (token_hash, str(filename), str(owner_id), now + ttl, now),
            )
        return token

    def consume_download_ticket(self, token, filename, owner_id):
        """Atomically validate and burn one download ticket."""
        if not token or not filename or not owner_id:
            return False
        token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
        now = time.time()
        with self.lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT filename,owner_id,expires_at,used_at FROM download_tickets WHERE token_hash=?",
                (token_hash,),
            ).fetchone()
            if (
                not row
                or row["filename"] != str(filename)
                or row["owner_id"] != str(owner_id)
                or row["used_at"] is not None
                or float(row["expires_at"] or 0) < now
            ):
                return False
            cursor = conn.execute(
                "UPDATE download_tickets SET used_at=? WHERE token_hash=? AND used_at IS NULL",
                (now, token_hash),
            )
            return cursor.rowcount == 1

    def get_scan(self, scan_id, owner_id=None):
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT payload, owner_id FROM scans WHERE id=?", (scan_id,)).fetchone()
            if not row:
                return None
            stored_owner = row["owner_id"] or "legacy"
            if owner_id and stored_owner != owner_id:
                return None
        return json.loads(row["payload"])

    def scan_owned(self, scan_id, owner_id=None):
        """Check ownership without decoding the large scan payload."""
        with self._connect() as conn:
            row = conn.execute("SELECT owner_id FROM scans WHERE id=?", (str(scan_id),)).fetchone()
        if not row:
            return False
        return not owner_id or (row["owner_id"] or "legacy") == owner_id

    def get_scan_overview(self, scan_id, owner_id=None):
        """Load bounded scan metadata and lazily migrate pre-index releases."""
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT s.owner_id,o.payload AS overview "
                "FROM scans s LEFT JOIN scan_overviews o ON o.scan_id=s.id WHERE s.id=?",
                (str(scan_id),),
            ).fetchone()
            if not row:
                return None
            if owner_id and (row["owner_id"] or "legacy") != owner_id:
                return None
            if row["overview"]:
                result = json.loads(row["overview"])
            else:
                full_row = conn.execute(
                    "SELECT payload FROM scans WHERE id=?", (str(scan_id),)
                ).fetchone()
                full = json.loads(full_row["payload"])
                result = self._scan_overview_payload(full)
                conn.execute(
                    "INSERT OR REPLACE INTO scan_overviews(scan_id,payload,updated_at) "
                    "VALUES (?,?,CURRENT_TIMESTAMP)",
                    (str(scan_id), json.dumps(result, ensure_ascii=False)),
                )
                indexed = conn.execute(
                    "SELECT 1 FROM tree_nodes WHERE scan_id=? AND tree_kind='physical' LIMIT 1",
                    (str(scan_id),),
                ).fetchone()
                if not indexed:
                    self._replace_tree_index(conn, scan_id, "physical", full.get("tree") or {})
        result["scan_id"] = str(scan_id)
        return result

    def update_scan(self, scan_id, payload):
        with self.lock, self._connect() as conn:
            conn.execute(
                "UPDATE scans SET root_path=?, payload=? WHERE id=?",
                (payload["root"], json.dumps(payload, ensure_ascii=False), scan_id),
            )
            conn.execute(
                "INSERT OR REPLACE INTO scan_overviews(scan_id,payload,updated_at) "
                "VALUES (?,?,CURRENT_TIMESTAMP)",
                (str(scan_id), json.dumps(self._scan_overview_payload(payload), ensure_ascii=False)),
            )
            # Analysis enriches this tree with summaries, evidence and
            # deduplication metadata.  Keeping an existing index would pin the
            # paginated API to the original scan snapshot forever.
            self._replace_tree_index(conn, scan_id, "physical", payload.get("tree") or {})

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

    def list_summaries_page(self, scan_id, offset=0, limit=200, node_path=None, summary_type=None):
        """Return one bounded summary page or an exact path/type lookup."""
        offset = max(0, int(offset or 0))
        limit = max(1, min(500, int(limit or 200)))
        clauses = ["scan_id=?"]
        values = [str(scan_id)]
        if node_path is not None:
            clauses.append("node_path=?")
            values.append(str(node_path))
        if summary_type:
            clauses.append("summary_type=?")
            values.append(str(summary_type))
        where = " AND ".join(clauses)
        with self._connect() as conn:
            total = int(conn.execute(
                "SELECT COUNT(*) AS value FROM summaries WHERE {}".format(where), values
            ).fetchone()["value"])
            rows = conn.execute(
                "SELECT node_path,summary_type,payload FROM summaries WHERE {} "
                "ORDER BY node_path,summary_type LIMIT ? OFFSET ?".format(where),
                values + [limit, offset],
            ).fetchall()
        items = [
            {"path": row["node_path"], "type": row["summary_type"],
             "payload": normalize_summary(json.loads(row["payload"]))}
            for row in rows
        ]
        next_offset = offset + len(items) if offset + len(items) < total else None
        return {"items": items, "offset": offset, "limit": limit, "total": total, "next_offset": next_offset}

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

    def delete_document(self, scan_id, node_path):
        """Remove a stale parsed payload after a later parse attempt fails."""
        sidecar_relative = None
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM unified_documents WHERE scan_id=? AND node_path=?",
                (str(scan_id), str(node_path)),
            ).fetchone()
            if row:
                try:
                    wrapper = json.loads(row["payload"])
                    if wrapper.get("__sidecar_payload__"):
                        sidecar_relative = str(wrapper.get("file") or "")
                except (TypeError, ValueError, json.JSONDecodeError):
                    sidecar_relative = None
            cursor = conn.execute(
                "DELETE FROM unified_documents WHERE scan_id=? AND node_path=?",
                (str(scan_id), str(node_path)),
            )
            deleted = cursor.rowcount == 1
        if deleted and sidecar_relative:
            candidate = self.sidecar_dir / sidecar_relative
            try:
                candidate.resolve().relative_to(self.sidecar_dir.resolve())
                candidate.unlink()
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                LOGGER.warning(
                    "无法清理失效文档 sidecar：scan_id=%s path=%s",
                    scan_id, node_path,
                )
        return deleted

    def iter_documents(self, scan_id, hydrate=True, batch_size=100):
        """Yield documents from bounded SQLite batches.

        This avoids retaining both every serialized JSON row and every decoded
        document at once.  Consumers may still choose to build their own
        index, but the database cursor itself adds only one bounded batch.
        """
        batch_size = max(1, min(1000, int(batch_size or 100)))
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT node_path,payload FROM unified_documents WHERE scan_id=? ORDER BY node_path",
                (str(scan_id),),
            )
            try:
                while True:
                    rows = cursor.fetchmany(batch_size)
                    if not rows:
                        break
                    for row in rows:
                        yield {
                            "path": row["node_path"],
                            "payload": self._load_document_payload(
                                row["payload"], hydrate=hydrate
                            ),
                        }
            finally:
                cursor.close()

    def list_documents(self, scan_id, hydrate=True):
        # Compatibility API: callers still receive the same ordered list.
        return list(self.iter_documents(scan_id, hydrate=hydrate))

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

    def iter_file_states(self, scan_id, batch_size=500):
        batch_size = max(1, min(5000, int(batch_size or 500)))
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM file_analysis_states WHERE scan_id=? ORDER BY node_path",
                (str(scan_id),),
            )
            try:
                while True:
                    rows = cursor.fetchmany(batch_size)
                    if not rows:
                        break
                    for row in rows:
                        yield dict(row)
            finally:
                cursor.close()

    def list_file_states(self, scan_id):
        return list(self.iter_file_states(scan_id))

    def save_analysis(self, scan_id, payload):
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO package_analyses(scan_id,payload) VALUES (?,?)",
                (scan_id, json.dumps(payload, ensure_ascii=False)),
            )
            conn.execute(
                "INSERT OR REPLACE INTO analysis_overviews(scan_id,payload,updated_at) "
                "VALUES (?,?,CURRENT_TIMESTAMP)",
                (str(scan_id), json.dumps(self._analysis_overview_payload(payload), ensure_ascii=False)),
            )
            conn.execute(
                "DELETE FROM tree_nodes WHERE scan_id=? AND tree_kind LIKE 'analysis:%'",
                (str(scan_id),),
            )
            self._replace_tree_index(conn, scan_id, "analysis", payload.get("analysis_tree") or {})
            conn.execute("DELETE FROM analysis_progress WHERE scan_id=?", (str(scan_id),))

    def get_analysis(self, scan_id):
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM package_analyses WHERE scan_id=?", (scan_id,)).fetchone()
        if not row:
            return None
        analysis = json.loads(row["payload"])
        return apply_tree_edits(analysis, self.list_tree_edits(scan_id))

    def get_analysis_overview(self, scan_id):
        """Return report/statistics data without the semantic tree or indexes."""
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT o.payload AS overview FROM package_analyses a "
                "LEFT JOIN analysis_overviews o ON o.scan_id=a.scan_id WHERE a.scan_id=?",
                (str(scan_id),),
            ).fetchone()
            if not row:
                return None
            if row["overview"]:
                return json.loads(row["overview"])
            full_row = conn.execute(
                "SELECT payload FROM package_analyses WHERE scan_id=?", (str(scan_id),)
            ).fetchone()
            full = json.loads(full_row["payload"])
            overview = self._analysis_overview_payload(full)
            conn.execute(
                "INSERT OR REPLACE INTO analysis_overviews(scan_id,payload,updated_at) "
                "VALUES (?,?,CURRENT_TIMESTAMP)",
                (str(scan_id), json.dumps(overview, ensure_ascii=False)),
            )
            indexed = conn.execute(
                "SELECT 1 FROM tree_nodes WHERE scan_id=? AND tree_kind='analysis' LIMIT 1",
                (str(scan_id),),
            ).fetchone()
            if not indexed:
                edited = apply_tree_edits(full, self.list_tree_edits(scan_id))
                self._replace_tree_index(conn, scan_id, "analysis", edited.get("analysis_tree") or {})
            return overview

    def save_analysis_progress(self, scan_id, payload):
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO analysis_progress(scan_id,payload,updated_at) "
                "VALUES (?,?,CURRENT_TIMESTAMP)",
                (str(scan_id), json.dumps(payload or {}, ensure_ascii=False)),
            )

    def get_analysis_progress(self, scan_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload,updated_at FROM analysis_progress WHERE scan_id=?",
                (str(scan_id),),
            ).fetchone()
        if not row:
            return None
        result = json.loads(row["payload"])
        result["updated_at"] = row["updated_at"]
        return result

    def clear_analysis_progress(self, scan_id):
        with self.lock, self._connect() as conn:
            conn.execute("DELETE FROM analysis_progress WHERE scan_id=?", (str(scan_id),))

    def update_analysis_progress_status(self, scan_id, status, message, stage=None):
        """Keep a progressive card truthful after cancel, retry, or failure."""
        current = self.get_analysis_progress(scan_id) or {
            "schema_version": "analysis-progress/1.0", "progress": 0,
        }
        current.pop("updated_at", None)
        current["status"] = str(status)
        current["message"] = str(message or "")
        if stage is not None:
            current["stage"] = str(stage)
        self.save_analysis_progress(scan_id, current)
        return current

    def file_state_counts(self, scan_id):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status,COUNT(*) AS value FROM file_analysis_states "
                "WHERE scan_id=? GROUP BY status",
                (str(scan_id),),
            ).fetchall()
        return {str(row["status"]): int(row["value"]) for row in rows}

    def file_state_metrics(self, scan_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS processed_files,"
                "COALESCE(SUM(stored_characters),0) AS stored_characters,"
                "COALESCE(SUM(evidence_count),0) AS evidence_items "
                "FROM file_analysis_states WHERE scan_id=?",
                (str(scan_id),),
            ).fetchone()
        return {key: int(row[key] or 0) for key in ("processed_files", "stored_characters", "evidence_items")}

    def get_tree_page(self, scan_id, tree_kind="physical", node_key=None, offset=0, limit=200):
        """Return one node and a bounded page of its immediate children."""
        if tree_kind not in {"physical", "analysis"} and not str(tree_kind).startswith("analysis:"):
            raise ValueError("未知目录树类型")
        offset = max(0, int(offset or 0))
        limit = max(1, min(500, int(limit or 200)))
        with self._connect() as conn:
            if node_key:
                row = conn.execute(
                    "SELECT node_key,payload,child_count FROM tree_nodes "
                    "WHERE scan_id=? AND tree_kind=? AND node_key=?",
                    (str(scan_id), tree_kind, str(node_key)),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT node_key,payload,child_count FROM tree_nodes "
                    "WHERE scan_id=? AND tree_kind=? AND parent_key IS NULL "
                    "ORDER BY position,node_key LIMIT 1",
                    (str(scan_id), tree_kind),
                ).fetchone()
            if not row:
                return None
            children = conn.execute(
                "SELECT node_key,payload,child_count FROM tree_nodes "
                "WHERE scan_id=? AND tree_kind=? AND parent_key=? "
                "ORDER BY position,node_key LIMIT ? OFFSET ?",
                (str(scan_id), tree_kind, row["node_key"], limit, offset),
            ).fetchall()
        node = json.loads(row["payload"])
        node["_tree_key"] = row["node_key"]
        node["has_children"] = bool(row["child_count"])
        node["child_count"] = int(row["child_count"])
        node["children"] = []
        for child in children:
            item = json.loads(child["payload"])
            item["_tree_key"] = child["node_key"]
            item["has_children"] = bool(child["child_count"])
            item["child_count"] = int(child["child_count"])
            item["children"] = []
            node["children"].append(item)
        consumed = offset + len(children)
        node["_children_offset"] = offset
        node["_children_total"] = int(row["child_count"])
        node["_children_next_offset"] = consumed if consumed < int(row["child_count"]) else None
        return node

    def save_tree_edit(self, scan_id, edit_id, operation, payload, owner_id="legacy"):
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tree_edits(scan_id,edit_id,owner_id,operation,payload) VALUES (?,?,?,?,?)",
                (str(scan_id), str(edit_id), str(owner_id or "legacy"), str(operation), json.dumps(payload or {}, ensure_ascii=False)),
            )

    def tree_edit_count(self, scan_id, owner_id=None):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT owner_id FROM scans WHERE id=?", (str(scan_id),)
            ).fetchone()
            if not row or (owner_id and (row["owner_id"] or "legacy") != owner_id):
                return 0
            count = conn.execute(
                "SELECT COUNT(*) AS value FROM tree_edits WHERE scan_id=?",
                (str(scan_id),),
            ).fetchone()
        return int(count["value"] if count else 0)

    def list_tree_edits(self, scan_id, owner_id=None, limit=None):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT owner_id FROM scans WHERE id=?", (str(scan_id),)
            ).fetchone()
            if not row or (owner_id and (row["owner_id"] or "legacy") != owner_id):
                return []
            if limit is None:
                rows = conn.execute(
                    "SELECT edit_id,operation,payload,created_at FROM tree_edits "
                    "WHERE scan_id=? ORDER BY created_at,edit_id",
                    (str(scan_id),),
                ).fetchall()
            else:
                bounded = max(1, min(1000, int(limit)))
                rows = conn.execute(
                    "SELECT edit_id,operation,payload,created_at FROM tree_edits "
                    "WHERE scan_id=? ORDER BY created_at DESC,edit_id DESC LIMIT ?",
                    (str(scan_id), bounded),
                ).fetchall()
                rows = list(reversed(rows))
        return [{"edit_id": row["edit_id"], "operation": row["operation"], "payload": json.loads(row["payload"]), "created_at": row["created_at"]} for row in rows]

    def delete_tree_edit(self, scan_id, edit_id, owner_id=None):
        with self.lock, self._connect() as conn:
            if owner_id:
                row = conn.execute("SELECT owner_id FROM scans WHERE id=?", (str(scan_id),)).fetchone()
                if not row or (row["owner_id"] or "legacy") != owner_id:
                    return False
            result = conn.execute("DELETE FROM tree_edits WHERE scan_id=? AND edit_id=?", (str(scan_id), str(edit_id)))
        return bool(result.rowcount)

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

    def requeue_job_slice(self, job_id, message):
        """Continue a checkpointed long task in a fresh supervised process."""
        with self.lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE analysis_jobs SET status='queued', stage='queued', worker_id=NULL, "
                "progress=MIN(progress,95), message=?, error=NULL, heartbeat_at=NULL, "
                "finished_at=NULL, current_stage='等待续批', current_file='', "
                "updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='running' "
                "AND cancel_requested=0",
                (str(message), str(job_id)),
            )
        return cursor.rowcount == 1

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
                "UPDATE analysis_jobs SET status='running', stage='claimed', worker_id=?, "
                "started_at=?, heartbeat_at=?, attempt_count=attempt_count+1, updated_at=CURRENT_TIMESTAMP "
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
