import json
import calendar
import gzip
import hashlib
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from config import Config, _mount_filesystem
from services.schema import normalize_summary
from services.tree_editor import apply_tree_edits
from services.translation import document_translation_fingerprint


LOGGER = logging.getLogger(__name__)


class LazyStorage:
    """Thread-safe proxy that opens SQLite only on explicit first use.

    Web and Worker modules are imported by test discovery and by spawned
    processes.  Keeping construction lazy prevents those imports from running
    schema migrations or ownership updates against the configured live state.
    """

    def __init__(self, factory):
        self._factory = factory
        self._instance = None
        self._instance_lock = threading.Lock()

    def initialize(self):
        if self._instance is None:
            with self._instance_lock:
                if self._instance is None:
                    self._instance = self._factory()
        return self._instance

    @property
    def initialized(self):
        return self._instance is not None

    def __getattr__(self, name):
        return getattr(self.initialize(), name)


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
                    available_at REAL NOT NULL DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS package_processing_controls (
                    scan_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL DEFAULT 'running',
                    pause_requested INTEGER NOT NULL DEFAULT 0,
                    reason TEXT,
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
                    error_class TEXT,
                    retryable INTEGER NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (scan_id, node_path)
                );
                CREATE TABLE IF NOT EXISTS file_workflow_states (
                    scan_id TEXT NOT NULL,
                    node_path TEXT NOT NULL,
                    workflow_state TEXT NOT NULL,
                    selection_state TEXT NOT NULL,
                    selection_score REAL NOT NULL DEFAULT 0,
                    score_components TEXT NOT NULL DEFAULT '{}',
                    reasons TEXT NOT NULL DEFAULT '[]',
                    safety_status TEXT NOT NULL DEFAULT 'unknown',
                    light_index_status TEXT NOT NULL DEFAULT 'pending',
                    language_code TEXT NOT NULL DEFAULT 'unknown',
                    ocr_candidate INTEGER NOT NULL DEFAULT 0,
                    parse_status TEXT NOT NULL DEFAULT 'pending',
                    evidence_status TEXT NOT NULL DEFAULT 'pending',
                    promotion_allowed INTEGER NOT NULL DEFAULT 1,
                    priority_source TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (scan_id, node_path)
                );
                CREATE TABLE IF NOT EXISTS inventory_entries (
                    scan_id TEXT NOT NULL,
                    node_path TEXT NOT NULL,
                    parent_path TEXT,
                    position INTEGER NOT NULL DEFAULT 0,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (scan_id, node_path)
                );
                CREATE TABLE IF NOT EXISTS inventory_scan_states (
                    scan_id TEXT PRIMARY KEY,
                    root_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cursor TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
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
                CREATE TABLE IF NOT EXISTS search_index_states (
                    scan_id TEXT PRIMARY KEY,
                    generation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expected_documents INTEGER NOT NULL DEFAULT 0,
                    processed_documents INTEGER NOT NULL DEFAULT 0,
                    failed_documents INTEGER NOT NULL DEFAULT 0,
                    empty_documents INTEGER NOT NULL DEFAULT 0,
                    evidence_records INTEGER NOT NULL DEFAULT 0,
                    last_node_path TEXT,
                    error TEXT,
                    started_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL
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
                CREATE TABLE IF NOT EXISTS file_previews (
                    scan_id TEXT NOT NULL,
                    node_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sample_sha256 TEXT,
                    preview_fingerprint TEXT,
                    source_size INTEGER,
                    source_modified_at_ns INTEGER,
                    language_code TEXT,
                    document_type TEXT,
                    payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (scan_id, node_path)
                );
                CREATE TABLE IF NOT EXISTS file_relation_features (
                    scan_id TEXT NOT NULL,
                    node_path TEXT NOT NULL,
                    feature_kind TEXT NOT NULL,
                    feature_value TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 1,
                    PRIMARY KEY (scan_id, node_path, feature_kind, feature_value)
                );
                CREATE TABLE IF NOT EXISTS package_content_maps (
                    scan_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS relationship_catalog (
                    scan_id TEXT NOT NULL,
                    relation_key TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (scan_id, relation_key)
                );
                CREATE INDEX IF NOT EXISTS idx_relationship_catalog_source
                    ON relationship_catalog(scan_id, source_path);
                CREATE INDEX IF NOT EXISTS idx_relationship_catalog_target
                    ON relationship_catalog(scan_id, target_path);
                CREATE TABLE IF NOT EXISTS package_overviews (
                    scan_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS homogeneous_analyses (
                    scan_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS homogeneous_records (
                    scan_id TEXT NOT NULL,
                    node_path TEXT NOT NULL,
                    document_date TEXT,
                    document_number TEXT,
                    sender TEXT,
                    recipient TEXT,
                    subject TEXT,
                    matter_id TEXT,
                    payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (scan_id, node_path)
                );
                CREATE TABLE IF NOT EXISTS homogeneous_relations (
                    scan_id TEXT NOT NULL,
                    relation_id TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (scan_id, relation_id)
                );
                CREATE TABLE IF NOT EXISTS homogeneous_cases (
                    scan_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    document_count INTEGER NOT NULL DEFAULT 0,
                    start_date TEXT,
                    end_date TEXT,
                    payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (scan_id, case_id)
                );
                CREATE TABLE IF NOT EXISTS homogeneous_anomalies (
                    scan_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    anomaly_type TEXT NOT NULL,
                    node_path TEXT,
                    severity TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (scan_id, position)
                );
                CREATE TABLE IF NOT EXISTS document_translations (
                    scan_id TEXT NOT NULL,
                    node_path TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_language TEXT,
                    provider_id TEXT,
                    payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (scan_id, node_path)
                );
                CREATE TABLE IF NOT EXISTS translation_memory (
                    memory_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    title TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    revision INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    session_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    turn_id TEXT,
                    sequence INTEGER,
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (session_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    scan_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    idempotency_key TEXT,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    question TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    plan TEXT,
                    result TEXT,
                    verification TEXT,
                    error TEXT,
                    job_id TEXT,
                    promotion_job_id TEXT,
                    continuation_depth INTEGER NOT NULL DEFAULT 0,
                    user_message_id TEXT NOT NULL,
                    assistant_message_id TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS conversation_analysis_steps (
                    turn_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    tool TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (turn_id, step_id)
                );
                CREATE TABLE IF NOT EXISTS conversation_turn_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    turn_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    stage TEXT,
                    progress INTEGER,
                    message TEXT,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS conversation_turn_evidence (
                    turn_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    citation_index INTEGER NOT NULL,
                    source_path TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (turn_id, evidence_id)
                );
                CREATE TABLE IF NOT EXISTS conversation_claims (
                    turn_id TEXT NOT NULL,
                    claim_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    evidence_ids TEXT NOT NULL DEFAULT '[]',
                    payload TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (turn_id, claim_id)
                );
                CREATE TABLE IF NOT EXISTS conversation_research_memory (
                    session_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_documents_scan_path ON unified_documents(scan_id, node_path);
                CREATE INDEX IF NOT EXISTS idx_summaries_scan_path ON summaries(scan_id, node_path);
                CREATE INDEX IF NOT EXISTS idx_retrieval_sessions_scan_created ON retrieval_sessions(scan_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_file_analysis_states_scan_status ON file_analysis_states(scan_id, status);
                CREATE INDEX IF NOT EXISTS idx_package_processing_controls_state
                    ON package_processing_controls(state, updated_at);
                CREATE INDEX IF NOT EXISTS idx_file_workflow_states_scan_selection
                    ON file_workflow_states(scan_id, selection_state, node_path);
                CREATE INDEX IF NOT EXISTS idx_file_workflow_states_scan_workflow
                    ON file_workflow_states(scan_id, workflow_state, node_path);
                CREATE INDEX IF NOT EXISTS idx_inventory_entries_parent
                    ON inventory_entries(scan_id, parent_path, position, node_path);
                CREATE INDEX IF NOT EXISTS idx_inventory_entries_kind
                    ON inventory_entries(scan_id, kind, node_path);
                CREATE INDEX IF NOT EXISTS idx_embedding_cache_updated ON embedding_cache(updated_at);
                CREATE INDEX IF NOT EXISTS idx_evidence_index_scan_source ON evidence_index(scan_id, source_path);
                CREATE INDEX IF NOT EXISTS idx_search_index_states_status
                    ON search_index_states(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_tree_nodes_parent
                    ON tree_nodes(scan_id, tree_kind, parent_key, position, node_key);
                CREATE INDEX IF NOT EXISTS idx_download_tickets_expiry
                    ON download_tickets(expires_at, used_at);
                CREATE INDEX IF NOT EXISTS idx_file_previews_scan_status
                    ON file_previews(scan_id, status, node_path);
                CREATE INDEX IF NOT EXISTS idx_file_previews_scan_language
                    ON file_previews(scan_id, language_code, node_path);
                CREATE INDEX IF NOT EXISTS idx_file_relation_features_lookup
                    ON file_relation_features(scan_id, feature_kind, feature_value, node_path);
                CREATE INDEX IF NOT EXISTS idx_document_translations_scan_status
                    ON document_translations(scan_id, status, node_path);
                CREATE INDEX IF NOT EXISTS idx_homogeneous_records_date
                    ON homogeneous_records(scan_id, document_date, node_path);
                CREATE INDEX IF NOT EXISTS idx_homogeneous_records_number
                    ON homogeneous_records(scan_id, document_number);
                CREATE INDEX IF NOT EXISTS idx_homogeneous_records_matter
                    ON homogeneous_records(scan_id, matter_id, document_date);
                CREATE INDEX IF NOT EXISTS idx_homogeneous_relations_source
                    ON homogeneous_relations(scan_id, source_path, relation_type);
                CREATE INDEX IF NOT EXISTS idx_homogeneous_relations_target
                    ON homogeneous_relations(scan_id, target_path, relation_type);
                CREATE INDEX IF NOT EXISTS idx_homogeneous_cases_size
                    ON homogeneous_cases(scan_id, document_count DESC, title);
                CREATE INDEX IF NOT EXISTS idx_homogeneous_anomalies_type
                    ON homogeneous_anomalies(scan_id, anomaly_type, severity);
                CREATE INDEX IF NOT EXISTS idx_conversations_scan_owner
                    ON conversations(scan_id, owner_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_conversation_messages_session
                    ON conversation_messages(session_id, created_at, message_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_turn_sequence
                    ON conversation_turns(session_id, sequence);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_turn_idempotency
                    ON conversation_turns(session_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_conversation_turn_status
                    ON conversation_turns(session_id, status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_conversation_turn_events
                    ON conversation_turn_events(turn_id, event_id);
                CREATE INDEX IF NOT EXISTS idx_conversation_step_status
                    ON conversation_analysis_steps(turn_id, position, status);
                CREATE TRIGGER IF NOT EXISTS trg_scans_delete_search_index_state
                AFTER DELETE ON scans
                BEGIN
                    DELETE FROM search_index_states WHERE scan_id=OLD.id;
                END;
                CREATE TRIGGER IF NOT EXISTS trg_scans_delete_cascade
                BEFORE DELETE ON scans
                BEGIN
                    DELETE FROM conversation_analysis_steps
                        WHERE turn_id IN (SELECT id FROM conversation_turns WHERE scan_id=OLD.id);
                    DELETE FROM conversation_turn_events
                        WHERE turn_id IN (SELECT id FROM conversation_turns WHERE scan_id=OLD.id);
                    DELETE FROM conversation_turn_evidence
                        WHERE turn_id IN (SELECT id FROM conversation_turns WHERE scan_id=OLD.id);
                    DELETE FROM conversation_claims
                        WHERE turn_id IN (SELECT id FROM conversation_turns WHERE scan_id=OLD.id);
                    DELETE FROM conversation_messages
                        WHERE session_id IN (SELECT id FROM conversations WHERE scan_id=OLD.id);
                    DELETE FROM conversation_research_memory
                        WHERE session_id IN (SELECT id FROM conversations WHERE scan_id=OLD.id);
                    DELETE FROM conversation_turns WHERE scan_id=OLD.id;
                    DELETE FROM conversations WHERE scan_id=OLD.id;
                    DELETE FROM download_tickets
                        WHERE filename IN (
                            SELECT filename FROM output_artifacts WHERE scan_id=OLD.id
                        );
                    DELETE FROM output_artifacts WHERE scan_id=OLD.id;
                    DELETE FROM summaries WHERE scan_id=OLD.id;
                    DELETE FROM unified_documents WHERE scan_id=OLD.id;
                    DELETE FROM package_analyses WHERE scan_id=OLD.id;
                    DELETE FROM analysis_jobs WHERE scan_id=OLD.id;
                    DELETE FROM file_analysis_states WHERE scan_id=OLD.id;
                    DELETE FROM file_workflow_states WHERE scan_id=OLD.id;
                    DELETE FROM inventory_entries WHERE scan_id=OLD.id;
                    DELETE FROM inventory_scan_states WHERE scan_id=OLD.id;
                    DELETE FROM retrieval_sessions WHERE scan_id=OLD.id;
                    DELETE FROM evidence_index WHERE scan_id=OLD.id;
                    DELETE FROM search_index_states WHERE scan_id=OLD.id;
                    DELETE FROM tree_edits WHERE scan_id=OLD.id;
                    DELETE FROM scan_overviews WHERE scan_id=OLD.id;
                    DELETE FROM analysis_overviews WHERE scan_id=OLD.id;
                    DELETE FROM analysis_progress WHERE scan_id=OLD.id;
                    DELETE FROM tree_nodes WHERE scan_id=OLD.id;
                    DELETE FROM file_previews WHERE scan_id=OLD.id;
                    DELETE FROM file_relation_features WHERE scan_id=OLD.id;
                    DELETE FROM package_content_maps WHERE scan_id=OLD.id;
                    DELETE FROM relationship_catalog WHERE scan_id=OLD.id;
                    DELETE FROM package_overviews WHERE scan_id=OLD.id;
                    DELETE FROM homogeneous_analyses WHERE scan_id=OLD.id;
                    DELETE FROM homogeneous_records WHERE scan_id=OLD.id;
                    DELETE FROM homogeneous_relations WHERE scan_id=OLD.id;
                    DELETE FROM homogeneous_cases WHERE scan_id=OLD.id;
                    DELETE FROM homogeneous_anomalies WHERE scan_id=OLD.id;
                    DELETE FROM document_translations WHERE scan_id=OLD.id;
                END;
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
                "available_at": "REAL NOT NULL DEFAULT 0",
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
            conversation_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(conversations)").fetchall()
            }
            if "revision" not in conversation_columns:
                conn.execute("ALTER TABLE conversations ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
            message_columns = {
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(conversation_messages)"
                ).fetchall()
            }
            message_migrations = {
                "turn_id": "TEXT",
                "sequence": "INTEGER",
                "updated_at": "TEXT",
            }
            for name, definition in message_migrations.items():
                if name not in message_columns:
                    conn.execute(
                        "ALTER TABLE conversation_messages ADD COLUMN {} {}".format(
                            name, definition
                        )
                    )
            conn.execute(
                "UPDATE conversation_messages SET updated_at=COALESCE(updated_at,created_at,CURRENT_TIMESTAMP)"
            )
            conn.execute("DROP INDEX IF EXISTS idx_conversation_messages_session")
            conn.execute(
                "CREATE INDEX idx_conversation_messages_session "
                "ON conversation_messages(session_id, sequence, created_at, message_id)"
            )
            preview_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(file_previews)").fetchall()
            }
            preview_migrations = {
                "preview_fingerprint": "TEXT",
                "source_size": "INTEGER",
                "source_modified_at_ns": "INTEGER",
            }
            for name, definition in preview_migrations.items():
                if name not in preview_columns:
                    conn.execute("ALTER TABLE file_previews ADD COLUMN {} {}".format(name, definition))
            file_state_columns = {
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(file_analysis_states)"
                ).fetchall()
            }
            file_state_migrations = {
                "error_class": "TEXT",
                "retryable": "INTEGER NOT NULL DEFAULT 0",
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "next_retry_at": "REAL",
            }
            for name, definition in file_state_migrations.items():
                if name not in file_state_columns:
                    conn.execute(
                        "ALTER TABLE file_analysis_states ADD COLUMN {} {}".format(
                            name, definition
                        )
                    )
            conn.execute("DROP INDEX IF EXISTS idx_analysis_jobs_queue")
            conn.execute(
                "CREATE INDEX idx_analysis_jobs_queue "
                "ON analysis_jobs(status, available_at, priority DESC, created_at)"
            )
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5("
                    "scan_id UNINDEXED,index_key UNINDEXED,source_path UNINDEXED,"
                    "archive_source_path UNINDEXED,section,text,tokenize='unicode61')"
                )
                self.evidence_fts_available = True
                conn.execute(
                    "CREATE TRIGGER IF NOT EXISTS trg_scans_delete_evidence_fts "
                    "AFTER DELETE ON scans BEGIN "
                    "DELETE FROM evidence_fts WHERE scan_id=OLD.id; END"
                )
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

    def _preview_sidecar_path(self, scan_id, node_path):
        digest = hashlib.sha256(
            "preview|{}|{}".format(scan_id, node_path).encode("utf-8")
        ).hexdigest()
        folder = self.sidecar_dir / str(scan_id) / "previews"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / (digest + ".json.gz")

    def _store_preview_payload(self, scan_id, node_path, payload):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) <= 16 * 1024:
            return raw.decode("utf-8")
        target = self._preview_sidecar_path(scan_id, node_path)
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
            "__preview_sidecar__": True,
            "file": str(target.relative_to(self.sidecar_dir)).replace("\\", "/"),
            "schema_version": payload.get("schema_version"),
            "path": payload.get("path"),
            "status": payload.get("status"),
            "size": payload.get("size"),
            "modified_at_ns": payload.get("modified_at_ns"),
        }, ensure_ascii=False, separators=(",", ":"))

    def _load_preview_payload(self, stored):
        payload = json.loads(stored)
        if not payload.get("__preview_sidecar__"):
            return payload
        target = (self.sidecar_dir / str(payload.get("file") or "")).resolve()
        try:
            target.relative_to(self.sidecar_dir.resolve())
            with gzip.open(str(target), "rb") as stream:
                return json.loads(stream.read().decode("utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return {
                "schema_version": payload.get("schema_version"),
                "path": payload.get("path"), "status": "failed",
                "size": payload.get("size"),
                "modified_at_ns": payload.get("modified_at_ns"),
                "preview_text": "", "preview_windows": [],
                "warnings": ["轻量预览缓存不可用：{}".format(exc)],
                "coverage": {"preview_only": True, "parse_complete": False},
            }

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
            for key in (
                "schema_version", "source", "parsed_at", "parser", "structure", "coverage",
                "archive_manifest", "warnings", "classification", "deduplication",
                "content_sha256", "data_profile", "data_profiles", "preview", "language",
                "languages", "entities", "named_entities", "temporal", "document_date",
                "file_relationships", "related_files", "topics", "content_topics", "translation",
            )
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

    @staticmethod
    def _translation_projection(payload):
        payload = dict(payload or {})
        units = []
        for unit in payload.get("units") or []:
            units.append({
                key: unit.get(key)
                for key in (
                    "unit_id", "kind", "start", "end", "source_language",
                    "status", "attempts", "model", "qa", "error", "retryable",
                )
                if key in unit
            })
            if len(units) >= 200:
                break
        projection = {
            key: payload.get(key)
            for key in (
                "schema_version", "contract_version", "source_fingerprint",
                "source_path", "source_language", "language_detection",
                "target_language", "provider_id", "glossary_fingerprint",
                "status", "translation_required", "cancelled", "progress",
                "errors", "updated_at", "original_title", "working_title",
                "translated_title", "source_level",
                "full_translation",
            )
            if key in payload
        }
        projection.update({
            "units": units,
            "units_projected": len(units),
            "units_total": len(payload.get("units") or []),
            "sidecar_projection": True,
            "original_text_available": payload.get("original_text") is not None,
            "translated_text_available": payload.get("translated_text") is not None,
        })
        return projection

    def _store_translation_payload(self, scan_id, node_path, payload):
        raw = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) <= self.sidecar_threshold:
            return raw.decode("utf-8")
        target = self._sidecar_path(scan_id, "translation:" + str(node_path))
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
            "__translation_sidecar__": True,
            "file": str(target.relative_to(self.sidecar_dir)).replace("\\", "/"),
            "projection": self._translation_projection(payload),
        }, ensure_ascii=False)

    def _load_translation_payload(self, stored, hydrate=True):
        payload = json.loads(stored)
        if not payload.get("__translation_sidecar__"):
            return payload if hydrate else self._translation_projection(payload)
        if not hydrate:
            return dict(payload.get("projection") or {})
        target = (self.sidecar_dir / str(payload.get("file") or "")).resolve()
        try:
            target.relative_to(self.sidecar_dir.resolve())
            with gzip.open(str(target), "rb") as stream:
                return json.loads(stream.read().decode("utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            projection = dict(payload.get("projection") or {})
            projection["status"] = "cache_unavailable"
            projection["errors"] = list(projection.get("errors") or []) + [{
                "code": "translation_cache_unavailable", "message": str(exc)[:300],
            }]
            return projection

    def _invalidate_translation_if_changed(self, conn, scan_id, node_path, document):
        """Atomically withdraw a translation when its parsed source changed."""
        scan_id = str(scan_id)
        node_path = str(node_path)
        row = conn.execute(
            "SELECT source_fingerprint,payload FROM document_translations "
            "WHERE scan_id=? AND node_path=?",
            (scan_id, node_path),
        ).fetchone()
        if not row:
            return None
        current_fingerprint = document_translation_fingerprint(document)
        if str(row["source_fingerprint"] or "") == current_fingerprint:
            return None

        return self._delete_translation_state(conn, scan_id, node_path, row=row)

    def _delete_translation_state(self, conn, scan_id, node_path, row=None):
        scan_id = str(scan_id)
        node_path = str(node_path)
        if row is None:
            row = conn.execute(
                "SELECT payload FROM document_translations WHERE scan_id=? AND node_path=?",
                (scan_id, node_path),
            ).fetchone()
        sidecar_relative = None
        try:
            if row:
                wrapper = json.loads(row["payload"])
                if wrapper.get("__translation_sidecar__"):
                    sidecar_relative = str(wrapper.get("file") or "") or None
        except (TypeError, ValueError, json.JSONDecodeError):
            sidecar_relative = None

        conn.execute(
            "DELETE FROM document_translations WHERE scan_id=? AND node_path=?",
            (scan_id, node_path),
        )
        conn.execute(
            "DELETE FROM evidence_index WHERE scan_id=? AND source_path=? "
            "AND index_key LIKE 'translation:%'",
            (scan_id, node_path),
        )
        if self.evidence_fts_available:
            conn.execute(
                "DELETE FROM evidence_fts WHERE scan_id=? AND source_path=? "
                "AND index_key LIKE 'translation:%'",
                (scan_id, node_path),
            )
        return sidecar_relative

    def _unlink_translation_sidecar(self, relative_path):
        if not relative_path:
            return
        candidate = self.sidecar_dir / str(relative_path)
        try:
            candidate.resolve().relative_to(self.sidecar_dir.resolve())
            candidate.unlink()
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            LOGGER.warning("无法清理失效翻译 sidecar：file=%s", relative_path)

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

    def reconcile_conversation_turn_jobs(self):
        """Make durable turn state agree with its authoritative Worker job."""
        changed = 0
        message_updates = []
        with self.lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE analysis_jobs SET status='completed',stage='completed',progress=100,"
                "message='逻辑分析轮次已经完成，无需重复执行',cancel_requested=0,"
                "worker_id=NULL,heartbeat_at=NULL,finished_at=COALESCE(finished_at,?),"
                "updated_at=CURRENT_TIMESTAMP WHERE task_type='conversation_turn' "
                "AND status IN ('queued','running') AND EXISTS ("
                "SELECT 1 FROM conversation_turns t WHERE t.job_id=analysis_jobs.id "
                "AND t.status='completed')",
                (time.time(),),
            )
            changed += cursor.rowcount
            cursor.close()
            rows = conn.execute(
                "SELECT t.id AS turn_id,t.status AS turn_status,j.status AS job_status "
                "FROM conversation_turns t JOIN analysis_jobs j ON j.id=t.job_id "
                "WHERE j.task_type='conversation_turn' AND ("
                "(j.status='queued' AND t.status NOT IN ('queued','completed','cancelled')) OR "
                "(j.status='cancelled' AND t.status!='cancelled') OR "
                "(j.status='failed' AND t.status NOT IN ('failed','completed','cancelled')))"
            ).fetchall()
            for row in rows:
                if row["job_status"] == "queued":
                    status, stage, progress, message, event_type = (
                        "queued", "queued", 0,
                        "检测到 Worker 重启，正在从持久化状态恢复本轮分析。", "recovered",
                    )
                elif row["job_status"] == "cancelled":
                    status, stage, progress, message, event_type = (
                        "cancelled", "cancelled", 100, "本轮分析已取消。", "cancelled",
                    )
                else:
                    status, stage, progress, message, event_type = (
                        "failed", "failed", 100, "交互式分析任务异常终止。", "failed",
                    )
                conn.execute(
                    "UPDATE conversation_turns SET status=?,stage=?,progress=?,"
                    "finished_at=CASE WHEN ? IN ('failed','cancelled') THEN CURRENT_TIMESTAMP ELSE NULL END,"
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (status, stage, progress, status, row["turn_id"]),
                )
                conn.execute(
                    "INSERT INTO conversation_turn_events("
                    "turn_id,event_type,stage,progress,message,payload) VALUES (?,?,?,?,?,?)",
                    (row["turn_id"], event_type, stage, progress, message, "{}"),
                )
                message_updates.append((row["turn_id"], message, status, stage, progress))
                changed += 1
        for values in message_updates:
            self.set_conversation_turn_message(*values)
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
            # Package-wide original-evidence rebuilds must not discard a valid
            # completed translation index. Per-document deep re-parses use
            # replace_document_evidence_index(), which removes both variants
            # for that source before the new source fingerprint is translated.
            conn.execute(
                "DELETE FROM evidence_index WHERE scan_id=? AND index_key NOT LIKE 'translation:%'",
                (str(scan_id),),
            )
            if self.evidence_fts_available:
                conn.execute(
                    "DELETE FROM evidence_fts WHERE scan_id=? AND index_key NOT LIKE 'translation:%'",
                    (str(scan_id),),
                )
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

    def replace_document_evidence_index(
        self, scan_id, node_path, chunks, preserve_translations=False
    ):
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
        delete_sql = "scan_id=? AND (source_path=? OR archive_source_path=? OR source_path LIKE ?)"
        if preserve_translations:
            delete_sql += " AND index_key NOT LIKE 'translation:%'"
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

    def get_search_index_state(self, scan_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM search_index_states WHERE scan_id=?",
                (str(scan_id),),
            ).fetchone()
        return dict(row) if row else None

    def begin_search_index_rebuild(self, scan_id, expected_documents):
        """Start a rebuild generation or resume its last durable checkpoint."""
        scan_id = str(scan_id)
        expected_documents = max(0, int(expected_documents or 0))
        now = time.time()
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM search_index_states WHERE scan_id=?", (scan_id,)
            ).fetchone()
            if (
                row
                and row["status"] in {"rebuilding", "interrupted"}
                and int(row["expected_documents"] or 0) == expected_documents
            ):
                conn.execute(
                    "UPDATE search_index_states SET status='rebuilding',error=NULL,updated_at=? "
                    "WHERE scan_id=?",
                    (now, scan_id),
                )
            else:
                generation = uuid.uuid4().hex
                conn.execute(
                    "INSERT OR REPLACE INTO search_index_states("
                    "scan_id,generation,status,expected_documents,processed_documents,"
                    "failed_documents,empty_documents,evidence_records,last_node_path,error,"
                    "started_at,updated_at,completed_at) VALUES (?,?, 'rebuilding', ?,0,0,0,0,NULL,NULL,?,?,NULL)",
                    (scan_id, generation, expected_documents, now, now),
                )
        return self.get_search_index_state(scan_id)

    def checkpoint_search_index_rebuild(
        self, scan_id, processed_documents, empty_documents, last_node_path
    ):
        with self.lock, self._connect() as conn:
            conn.execute(
                "UPDATE search_index_states SET status='rebuilding',processed_documents=?,"
                "empty_documents=?,last_node_path=?,error=NULL,updated_at=? WHERE scan_id=?",
                (
                    max(0, int(processed_documents or 0)),
                    max(0, int(empty_documents or 0)),
                    str(last_node_path or "") or None,
                    time.time(),
                    str(scan_id),
                ),
            )

    def interrupt_search_index_rebuild(self, scan_id, error):
        with self.lock, self._connect() as conn:
            conn.execute(
                "UPDATE search_index_states SET status='interrupted',error=?,updated_at=? "
                "WHERE scan_id=? AND status='rebuilding'",
                (str(error or "")[:1000], time.time(), str(scan_id)),
            )

    def complete_search_index_rebuild(
        self, scan_id, processed_documents, empty_documents, last_node_path
    ):
        """Atomically publish a generation only after every document committed."""
        scan_id = str(scan_id)
        now = time.time()
        with self.lock, self._connect() as conn:
            state = conn.execute(
                "SELECT * FROM search_index_states WHERE scan_id=?", (scan_id,)
            ).fetchone()
            if not state or state["status"] != "rebuilding":
                raise RuntimeError("证据索引重建状态不存在或已失效")
            actual_documents = int(conn.execute(
                "SELECT COUNT(*) FROM unified_documents WHERE scan_id=?", (scan_id,)
            ).fetchone()[0])
            expected_documents = int(state["expected_documents"] or 0)
            processed_documents = max(0, int(processed_documents or 0))
            if actual_documents != expected_documents or processed_documents != expected_documents:
                raise RuntimeError(
                    "重建期间文档集合发生变化或处理未完成：预期 {}，当前 {}，已处理 {}".format(
                        expected_documents, actual_documents, processed_documents
                    )
                )
            evidence_records = int(conn.execute(
                "SELECT COUNT(*) FROM evidence_index WHERE scan_id=?", (scan_id,)
            ).fetchone()[0])
            conn.execute(
                "UPDATE search_index_states SET status='ready',processed_documents=?,"
                "failed_documents=0,empty_documents=?,evidence_records=?,last_node_path=?,"
                "error=NULL,updated_at=?,completed_at=? WHERE scan_id=?",
                (
                    processed_documents,
                    max(0, int(empty_documents or 0)),
                    evidence_records,
                    str(last_node_path or "") or None,
                    now,
                    now,
                    scan_id,
                ),
            )
        return self.get_search_index_state(scan_id)

    def clear_evidence_index(self, scan_id, preserve_translations=False):
        with self.lock, self._connect() as conn:
            suffix = " AND index_key NOT LIKE 'translation:%'" if preserve_translations else ""
            conn.execute(
                "DELETE FROM evidence_index WHERE scan_id=?" + suffix,
                (str(scan_id),),
            )
            if self.evidence_fts_available:
                conn.execute(
                    "DELETE FROM evidence_fts WHERE scan_id=?" + suffix,
                    (str(scan_id),),
                )

    @staticmethod
    def _retrieval_terms(query):
        """Build bounded lexical terms for the evidence index.

        Evidence may be indexed before a Chinese working translation exists,
        so retrieval must also work directly on non-Latin scripts.  For
        space-delimited scripts we keep words; for scripts commonly written
        without spaces we add short n-grams.  Returning no terms is meaningful
        and is handled by ``search_evidence_index`` without leaking arbitrary
        rows from the beginning of the index.
        """
        text = str(query or "").lower()
        # Latin, Han, Arabic, Thai, Devanagari, Hebrew, Greek/Cyrillic,
        # Japanese kana and Korean syllables.  Punctuation is deliberately
        # excluded so it cannot become an FTS/LIKE wildcard-like term.
        values = re.findall(
            r"[a-z][a-z0-9_.-]{1,}|"
            r"[\u4e00-\u9fff]{2,}|"
            r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]{2,}|"
            r"[\u0e00-\u0e7f]{2,}|"
            r"[\u0900-\u097f]{2,}|"
            r"[\u0590-\u05ff]{2,}|"
            r"[\u0370-\u03ff\u0400-\u04ff]{2,}|"
            r"[\u3040-\u30ff\uac00-\ud7af]{2,}",
            text,
        )
        terms = []
        for value in values:
            if re.fullmatch(
                r"[\u4e00-\u9fff\u0e00-\u0e7f\u3040-\u30ff\uac00-\ud7af]+",
                value,
            ):
                terms.append(value)
                terms.extend(value[index:index + 2] for index in range(max(0, len(value) - 1)))
            elif re.fullmatch(
                r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\u0900-\u097f\u0590-\u05ff\u0370-\u03ff\u0400-\u04ff]+",
                value,
            ):
                # Keep the complete word and a couple of prefixes.  Prefixes
                # make morphology-heavy languages more searchable while
                # keeping the query bounded.
                terms.append(value)
                if len(value) >= 4:
                    terms.extend((value[:3], value[:4]))
            else:
                terms.append(value)
        # Deterministic concept expansion keeps natural-language recall useful
        # even when the optional embedding service is unavailable. These are
        # retrieval candidates only; final answers still require source
        # evidence and therefore cannot be manufactured by the expansion.
        concept_groups = (
            ("逾期", "未支付", "拖欠", "欠款", "没有按时付款", "付款延迟", "支付延迟"),
            ("解除合同", "终止合同", "合同终止", "解约", "撤销合同"),
            ("欺诈", "诈骗", "虚假陈述", "伪造", "造假"),
            ("违约", "违反约定", "未履行", "不履行", "履约失败"),
            ("赔偿", "损害赔偿", "补偿", "损失承担", "赔付"),
            ("发票", "票据", "账单", "付款凭证", "收据"),
        )
        compact_query = re.sub(r"\s+", "", text)
        for group in concept_groups:
            if any(re.sub(r"\s+", "", item) in compact_query for item in group):
                for item in group:
                    terms.append(item)
                    if re.fullmatch(r"[\u4e00-\u9fff]+", item):
                        terms.extend(item[index:index + 2] for index in range(len(item) - 1))
        return list(dict.fromkeys(item for item in terms if item))[:64]

    def search_evidence_index(self, scan_id, query, scope=".", source_paths=None,
                              candidate_evidence_ids=None, limit=2500):
        """Return a bounded candidate set from SQLite before local reranking."""
        scan_id = str(scan_id)
        scope = str(scope or ".")
        limit = max(50, min(5000, int(limit or 2500)))
        source_paths = sorted(set(str(item) for item in (source_paths or []) if item))
        candidate_ids = set(str(item) for item in (candidate_evidence_ids or []) if item)
        terms = self._retrieval_terms(query)
        # An empty/whitespace query must never degrade into "first N rows";
        # callers can explicitly request a scope listing through the inventory
        # APIs instead.
        if not terms and not candidate_ids:
            return []
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
            "value_judgment", "structured_data_overview", "model_telemetry", "policy",
            "classification_dimensions", "semantic_cluster_threshold",
            "semantic_naming_model", "subtopic_naming_model", "semantic_cluster_error",
            "analysis_tree_version", "analysis_tree_identity_contract",
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

    @staticmethod
    def _inventory_size_text(value):
        size = float(max(0, int(value or 0)))
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return "{:.1f} {}".format(size, unit)
            size /= 1024.0

    def get_inventory_cursor(self, scan_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status,cursor FROM inventory_scan_states WHERE scan_id=?",
                (str(scan_id),),
            ).fetchone()
        if not row:
            return None
        result = json.loads(row["cursor"])
        result["status"] = row["status"]
        return result

    def save_inventory_slice(self, scan_id, root_path, cursor, records,
                             owner_id="legacy", parse_mode="fast", complete=False):
        """Atomically commit inventory rows, lazy tree nodes and resume cursor."""
        scan_id = str(scan_id)
        root_path = str(root_path)
        cursor = dict(cursor or {})
        inventory_rows = []
        tree_rows = []
        parent_paths = set()
        for record in records or []:
            payload = dict(record.get("payload") or {})
            node_path = str(record.get("path") or payload.get("path") or "")
            if not node_path:
                continue
            parent_path = record.get("parent_path")
            parent_path = str(parent_path) if parent_path is not None else None
            position = int(record.get("position") or 0)
            kind = str(payload.get("kind") or "unknown")
            stored = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            inventory_rows.append((
                scan_id, node_path, parent_path, position, kind, stored,
            ))
            tree_rows.append((
                scan_id, "physical", node_path, parent_path, position, stored, 0,
            ))
            if parent_path is not None:
                parent_paths.add(parent_path)

        status = "complete" if complete else "scanning"
        placeholder_tree = {
            "id": hashlib.sha1(root_path.encode("utf-8")).hexdigest()[:16],
            "name": Path(root_path).name or root_path, "path": ".",
            "kind": "directory", "children": [],
            "file_count": int(cursor.get("file_count") or 0),
            "directory_count": max(0, int(cursor.get("directory_count") or 1) - 1),
            "total_size": int(cursor.get("total_size") or 0),
        }
        scan_payload = {
            "root": root_path, "scan_id": scan_id,
            "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "parse_mode": "accurate" if parse_mode == "accurate" else "fast",
            "inventory_mode": "durable_paged_v1",
            "inventory_complete": bool(complete),
            "file_count": int(cursor.get("file_count") or 0),
            "directory_count": max(0, int(cursor.get("directory_count") or 1) - 1),
            "scanned_node_count": int(cursor.get("node_count") or 0),
            "total_size": int(cursor.get("total_size") or 0),
            "total_size_human": self._inventory_size_text(cursor.get("total_size") or 0),
            "type_counts": dict(cursor.get("type_counts") or {}),
            "symlink_count": int(cursor.get("symlink_count") or 0),
            "skipped_symlink_count": int(cursor.get("symlink_count") or 0),
            "ignored_file_count": int(cursor.get("ignored_file_count") or 0),
            "ignored_directory_count": int(cursor.get("ignored_directory_count") or 0),
            "depth_limited_directory_count": int(
                cursor.get("depth_limited_directory_count") or 0
            ),
            "scan_error_count": len(cursor.get("errors") or []),
            "errors": list(cursor.get("errors") or [])[:100],
            "truncated": False,
            "tree": placeholder_tree,
        }
        with self.lock, self._connect() as conn:
            if inventory_rows:
                conn.executemany(
                    "INSERT INTO inventory_entries(scan_id,node_path,parent_path,position,kind,payload,updated_at) "
                    "VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(scan_id,node_path) DO UPDATE SET "
                    "parent_path=excluded.parent_path,position=excluded.position,kind=excluded.kind,"
                    "payload=excluded.payload,updated_at=CURRENT_TIMESTAMP",
                    inventory_rows,
                )
                conn.executemany(
                    "INSERT INTO tree_nodes(scan_id,tree_kind,node_key,parent_key,position,payload,child_count) "
                    "VALUES (?,?,?,?,?,?,?) ON CONFLICT(scan_id,tree_kind,node_key) DO UPDATE SET "
                    "parent_key=excluded.parent_key,position=excluded.position,payload=excluded.payload",
                    tree_rows,
                )
            for parent_path in parent_paths:
                child_count = int(conn.execute(
                    "SELECT COUNT(*) AS value FROM inventory_entries WHERE scan_id=? AND parent_path=?",
                    (scan_id, parent_path),
                ).fetchone()["value"])
                conn.execute(
                    "UPDATE tree_nodes SET child_count=? WHERE scan_id=? AND tree_kind='physical' AND node_key=?",
                    (child_count, scan_id, parent_path),
                )
            conn.execute(
                "INSERT OR REPLACE INTO inventory_scan_states(scan_id,root_path,status,cursor,updated_at) "
                "VALUES (?,?,?,?,CURRENT_TIMESTAMP)",
                (scan_id, root_path, status, json.dumps(cursor, ensure_ascii=False)),
            )
            conn.execute(
                "INSERT OR REPLACE INTO scans(id,root_path,payload,owner_id) VALUES (?,?,?,?)",
                (scan_id, root_path, json.dumps(scan_payload, ensure_ascii=False), owner_id or "legacy"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO scan_overviews(scan_id,payload,updated_at) VALUES (?,?,CURRENT_TIMESTAMP)",
                (scan_id, json.dumps(self._scan_overview_payload(scan_payload), ensure_ascii=False)),
            )
        if complete:
            scan_payload = self.finalize_inventory_scan(scan_id, scan_payload)
        return scan_payload

    def finalize_inventory_scan(self, scan_id, scan_payload=None):
        """Compute directory aggregates without materialising a nested tree."""
        scan_id = str(scan_id)
        scan_payload = dict(scan_payload or self.get_scan(scan_id) or {})
        aggregates = {}
        directory_payloads = {}
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT node_path,parent_path,kind,payload FROM inventory_entries "
                "WHERE scan_id=? ORDER BY node_path", (scan_id,),
            )
            for row in cursor:
                payload = json.loads(row["payload"])
                if row["kind"] == "directory":
                    directory_payloads[row["node_path"]] = payload
                    aggregates.setdefault(row["node_path"], {
                        "file_count": 0, "directory_count": 0, "total_size": 0,
                        "direct_file_count": 0, "direct_directory_count": 0,
                        "type_counts": {},
                    })
                    if row["parent_path"] is not None:
                        aggregates.setdefault(row["parent_path"], {
                            "file_count": 0, "directory_count": 0, "total_size": 0,
                            "direct_file_count": 0, "direct_directory_count": 0,
                            "type_counts": {},
                        })["direct_directory_count"] += 1
                elif row["kind"] == "file":
                    parent = str(row["parent_path"] or ".")
                    direct = aggregates.setdefault(parent, {
                        "file_count": 0, "directory_count": 0, "total_size": 0,
                        "direct_file_count": 0, "direct_directory_count": 0,
                        "type_counts": {},
                    })
                    direct["direct_file_count"] += 1
                    size = int(payload.get("size") or 0)
                    extension = payload.get("extension") or "[无扩展名]"
                    current = parent
                    while True:
                        item = aggregates.setdefault(current, {
                            "file_count": 0, "directory_count": 0, "total_size": 0,
                            "direct_file_count": 0, "direct_directory_count": 0,
                            "type_counts": {},
                        })
                        item["file_count"] += 1
                        item["total_size"] += size
                        item["type_counts"][extension] = item["type_counts"].get(extension, 0) + 1
                        if current == ".":
                            break
                        current = current.rsplit("/", 1)[0] if "/" in current else "."

        for directory in directory_payloads:
            current = directory
            while current != ".":
                parent = current.rsplit("/", 1)[0] if "/" in current else "."
                aggregates.setdefault(parent, {
                    "file_count": 0, "directory_count": 0, "total_size": 0,
                    "direct_file_count": 0, "direct_directory_count": 0,
                    "type_counts": {},
                })["directory_count"] += 1
                current = parent

        updates = []
        for path, payload in directory_payloads.items():
            stats = aggregates.get(path) or {}
            payload.update(stats)
            payload["size_human"] = self._inventory_size_text(stats.get("total_size") or 0)
            top_types = sorted(
                (stats.get("type_counts") or {}).items(), key=lambda item: (-item[1], item[0])
            )
            payload["type_counts"] = dict(top_types)
            payload["simple_summary"] = (
                "本文件夹当前层有 {} 个文件、{} 个子目录；递归范围共 {} 个文件、{} 个目录，总大小 {}。"
            ).format(
                stats.get("direct_file_count", 0), stats.get("direct_directory_count", 0),
                stats.get("file_count", 0), stats.get("directory_count", 0),
                payload["size_human"],
            )
            updates.append((json.dumps(payload, ensure_ascii=False), scan_id, path))
        with self.lock, self._connect() as conn:
            if updates:
                conn.executemany(
                    "UPDATE inventory_entries SET payload=?,updated_at=CURRENT_TIMESTAMP "
                    "WHERE scan_id=? AND node_path=?", updates,
                )
                conn.executemany(
                    "UPDATE tree_nodes SET payload=? WHERE scan_id=? AND tree_kind='physical' AND node_key=?",
                    updates,
                )
            root_row = conn.execute(
                "SELECT payload FROM inventory_entries WHERE scan_id=? AND node_path='.'",
                (scan_id,),
            ).fetchone()
            root_payload = json.loads(root_row["payload"]) if root_row else scan_payload.get("tree", {})
            scan_payload["tree"] = dict(root_payload, children=[])
            scan_payload["inventory_complete"] = True
            scan_payload["inventory_mode"] = "durable_paged_v1"
            conn.execute(
                "UPDATE scans SET payload=? WHERE id=?",
                (json.dumps(scan_payload, ensure_ascii=False), scan_id),
            )
            conn.execute(
                "INSERT OR REPLACE INTO scan_overviews(scan_id,payload,updated_at) VALUES (?,?,CURRENT_TIMESTAMP)",
                (scan_id, json.dumps(self._scan_overview_payload(scan_payload), ensure_ascii=False)),
            )
        return scan_payload

    def iter_inventory_entries(self, scan_id, kind=None, batch_size=1000):
        batch_size = max(1, min(5000, int(batch_size or 1000)))
        where = "scan_id=?" + (" AND kind=?" if kind else "")
        values = (str(scan_id), str(kind)) if kind else (str(scan_id),)
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT node_path,parent_path,position,kind,payload FROM inventory_entries "
                "WHERE {} ORDER BY node_path".format(where), values,
            )
            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break
                for row in rows:
                    yield {
                        "path": row["node_path"], "parent_path": row["parent_path"],
                        "position": int(row["position"]), "kind": row["kind"],
                        "payload": json.loads(row["payload"]),
                    }

    def replace_logical_inventory_entries(self, scan_id, units, batch_size=5000):
        """Replace derived logical children without changing physical totals."""
        scan_id = str(scan_id)
        batch_size = max(100, min(20000, int(batch_size or 5000)))
        inserted = 0
        containers = set()
        with self.lock, self._connect() as conn:
            conn.execute("DELETE FROM inventory_entries WHERE scan_id=? AND kind='logical_file'", (scan_id,))
            batch = []
            for position, unit in enumerate(units or []):
                unit = dict(unit or {})
                node_path = str(unit.get("path") or "")
                container = str(unit.get("container_path") or "")
                if not node_path or not container:
                    continue
                containers.add(container)
                batch.append((
                    scan_id, node_path, container, position, "logical_file",
                    json.dumps(unit, ensure_ascii=False, separators=(",", ":")),
                ))
                if len(batch) >= batch_size:
                    conn.executemany(
                        "INSERT OR REPLACE INTO inventory_entries("
                        "scan_id,node_path,parent_path,position,kind,payload,updated_at) "
                        "VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)", batch,
                    )
                    inserted += len(batch)
                    batch = []
            if batch:
                conn.executemany(
                    "INSERT OR REPLACE INTO inventory_entries("
                    "scan_id,node_path,parent_path,position,kind,payload,updated_at) "
                    "VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)", batch,
                )
                inserted += len(batch)
            row = conn.execute("SELECT payload FROM scans WHERE id=?", (scan_id,)).fetchone()
            if row:
                payload = json.loads(row["payload"])
                payload["logical_file_count"] = inserted
                payload["logical_container_count"] = len(containers)
                conn.execute(
                    "UPDATE scans SET payload=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), scan_id),
                )
        return {"logical_file_count": inserted, "logical_container_count": len(containers)}

    def count_logical_inventory_entries(self, scan_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS value FROM inventory_entries WHERE scan_id=? AND kind='logical_file'",
                (str(scan_id),),
            ).fetchone()
        return int(row["value"] or 0) if row else 0

    def count_inventory_files(self, scan_id, scope=".", source_paths=None,
                              extension=None):
        """Count the physical files in a conversation scope without loading JSON."""
        scan_id = str(scan_id)
        scope = str(scope or ".").replace("\\", "/")
        source_paths = sorted(set(
            str(item).replace("\\", "/")
            for item in (source_paths or []) if item
        ))
        clauses = ["scan_id=?", "kind='file'"]
        values = [scan_id]
        with self._connect() as conn:
            durable_inventory_exists = bool(conn.execute(
                "SELECT 1 FROM inventory_entries WHERE scan_id=? AND kind='file' LIMIT 1",
                (scan_id,),
            ).fetchone())
            if source_paths:
                conn.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS inventory_allowed_sources("
                    "path TEXT PRIMARY KEY)"
                )
                conn.execute("DELETE FROM inventory_allowed_sources")
                conn.executemany(
                    "INSERT OR IGNORE INTO inventory_allowed_sources(path) VALUES (?)",
                    [(path,) for path in source_paths],
                )
                clauses.append(
                    "EXISTS (SELECT 1 FROM inventory_allowed_sources a "
                    "WHERE inventory_entries.node_path=a.path "
                    "OR inventory_entries.node_path LIKE a.path || '/%')"
                )
            elif scope != ".":
                clauses.append("(node_path=? OR node_path LIKE ?)")
                values.extend((scope, scope.rstrip("/") + "/%"))
            if extension:
                suffix = str(extension).lower().strip()
                if suffix and not suffix.startswith("."):
                    suffix = "." + suffix
                clauses.append("LOWER(node_path) LIKE ?")
                values.append("%" + suffix)
            row = conn.execute(
                "SELECT COUNT(*) AS value FROM inventory_entries WHERE {}".format(
                    " AND ".join(clauses)
                ),
                values,
            ).fetchone()
            if durable_inventory_exists:
                return int(row["value"] or 0)

            # Standard-size scans predate the paged inventory table and keep
            # the same physical paths in the lightweight tree index.
            tree_clauses = [
                "scan_id=?", "tree_kind='physical'",
                "json_extract(payload,'$.kind')='file'",
            ]
            tree_values = [scan_id]
            if source_paths:
                tree_clauses.append(
                    "EXISTS (SELECT 1 FROM inventory_allowed_sources a "
                    "WHERE tree_nodes.node_key=a.path "
                    "OR tree_nodes.node_key LIKE a.path || '/%')"
                )
            elif scope != ".":
                tree_clauses.append("(node_key=? OR node_key LIKE ?)")
                tree_values.extend((scope, scope.rstrip("/") + "/%"))
            if extension:
                suffix = str(extension).lower().strip()
                if suffix and not suffix.startswith("."):
                    suffix = "." + suffix
                tree_clauses.append("LOWER(node_key) LIKE ?")
                tree_values.append("%" + suffix)
            row = conn.execute(
                "SELECT COUNT(*) AS value FROM tree_nodes WHERE {}".format(
                    " AND ".join(tree_clauses)
                ),
                tree_values,
            ).fetchone()
        return int(row["value"] or 0)

    def count_documents(self, scan_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS value FROM unified_documents WHERE scan_id=?",
                (str(scan_id),),
            ).fetchone()
        return int(row["value"] or 0)

    def get_inventory_entry(self, scan_id, node_path):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM inventory_entries WHERE scan_id=? AND node_path=?",
                (str(scan_id), str(node_path)),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def inventory_paths_under(self, scan_id, node_path="."):
        node_path = str(node_path or ".")
        with self._connect() as conn:
            if node_path == ".":
                rows = conn.execute(
                    "SELECT node_path FROM inventory_entries WHERE scan_id=? AND kind='file' ORDER BY node_path",
                    (str(scan_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT node_path FROM inventory_entries WHERE scan_id=? AND kind='file' "
                    "AND (node_path=? OR node_path LIKE ?) ORDER BY node_path",
                    (str(scan_id), node_path, node_path.rstrip("/") + "/%"),
                ).fetchall()
            if not rows and not conn.execute(
                "SELECT 1 FROM inventory_entries WHERE scan_id=? AND kind='file' LIMIT 1",
                (str(scan_id),),
            ).fetchone():
                clauses = [
                    "scan_id=?", "tree_kind='physical'",
                    "json_extract(payload,'$.kind')='file'",
                ]
                values = [str(scan_id)]
                if node_path != ".":
                    clauses.append("(node_key=? OR node_key LIKE ?)")
                    values.extend((node_path, node_path.rstrip("/") + "/%"))
                rows = conn.execute(
                    "SELECT node_key AS node_path FROM tree_nodes WHERE {} "
                    "ORDER BY node_key".format(" AND ".join(clauses)),
                    values,
                ).fetchall()
        return [row["node_path"] for row in rows]

    def build_inventory_tree(self, scan_id):
        """Materialise the legacy full tree only for an explicit full request."""
        nodes = {}
        parents = {}
        for item in self.iter_inventory_entries(scan_id):
            node = dict(item["payload"])
            node["children"] = []
            nodes[item["path"]] = node
            parents[item["path"]] = item["parent_path"]
        for path in sorted(nodes, key=lambda value: (value.count("/"), value)):
            parent = parents.get(path)
            if parent is not None and parent in nodes:
                nodes[parent]["children"].append(nodes[path])
        for node in nodes.values():
            node["children"].sort(
                key=lambda item: (item.get("kind") != "directory", str(item.get("name") or "").casefold())
            )
        return nodes.get(".")

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
            if payload.get("inventory_mode") != "durable_paged_v1":
                self._replace_tree_index(conn, scan_id, "physical", payload.get("tree") or {})
            conn.execute("DELETE FROM package_overviews WHERE scan_id=?", (str(scan_id),))
        return scan_id

    def delete_scan(self, scan_id, owner_id=None, output_dir=None):
        """Atomically delete one inactive scan and all database descendants.

        SQLite triggers provide the database-level cascade for both this method
        and maintenance tools.  Filesystem sidecars and registered artifacts
        are removed afterwards with strict root checks; failures are reported
        without ever deleting a path outside application-owned directories.
        """
        scan_id = str(scan_id or "").strip()
        if not scan_id:
            raise ValueError("扫描标识不能为空")
        with self.lock, self._connect() as conn:
            clauses = ["id=?"]
            values = [scan_id]
            if owner_id is not None:
                clauses.append("owner_id=?")
                values.append(str(owner_id))
            row = conn.execute(
                "SELECT id FROM scans WHERE {}".format(" AND ".join(clauses)),
                values,
            ).fetchone()
            if not row:
                return {"deleted": False, "scan_id": scan_id}
            active = conn.execute(
                "SELECT COUNT(*) FROM analysis_jobs WHERE scan_id=? "
                "AND status IN ('queued','running','cancelling')",
                (scan_id,),
            ).fetchone()[0]
            if active:
                raise RuntimeError("扫描仍有排队或运行中任务，请先取消并等待任务结束")
            artifacts = [
                item["filename"]
                for item in conn.execute(
                    "SELECT filename FROM output_artifacts WHERE scan_id=?",
                    (scan_id,),
                ).fetchall()
            ]
            deleted = conn.execute(
                "DELETE FROM scans WHERE id=?", (scan_id,)
            ).rowcount == 1

        removed_sidecars = False
        sidecar_root = self.sidecar_dir.resolve()
        sidecar_target = (sidecar_root / scan_id).resolve()
        try:
            sidecar_target.relative_to(sidecar_root)
            if sidecar_target != sidecar_root and sidecar_target.is_dir():
                shutil.rmtree(str(sidecar_target))
                removed_sidecars = True
        except (OSError, RuntimeError, ValueError) as exc:
            LOGGER.warning("无法清理扫描 sidecar：scan_id=%s error=%s", scan_id, exc)

        removed_artifacts = 0
        if output_dir:
            output_root = Path(output_dir).resolve()
            for filename in artifacts:
                if Path(str(filename)).name != str(filename):
                    continue
                candidate = (output_root / str(filename)).resolve()
                try:
                    candidate.relative_to(output_root)
                    if candidate.is_file() or candidate.is_symlink():
                        candidate.unlink()
                        removed_artifacts += 1
                except (OSError, RuntimeError, ValueError) as exc:
                    LOGGER.warning(
                        "无法清理扫描产物：scan_id=%s file=%s error=%s",
                        scan_id, filename, exc,
                    )
        return {
            "deleted": deleted,
            "scan_id": scan_id,
            "sidecars_removed": removed_sidecars,
            "artifacts_registered": len(artifacts),
            "artifacts_removed": removed_artifacts,
        }

    def cleanup_history(
        self,
        *,
        owner_id=None,
        retention_days=0,
        max_scans=0,
        output_dir=None,
        dry_run=True,
        limit=100,
    ):
        """Plan or apply bounded retention cleanup for inactive scans."""
        retention_days = max(0, int(retention_days or 0))
        max_scans = max(0, int(max_scans or 0))
        limit = max(1, min(1000, int(limit or 100)))
        if not retention_days and not max_scans:
            return {"dry_run": bool(dry_run), "candidate_scan_ids": [], "deleted": []}
        clauses = [
            "NOT EXISTS (SELECT 1 FROM analysis_jobs j WHERE j.scan_id=scans.id "
            "AND j.status IN ('queued','running','cancelling'))"
        ]
        values = []
        if owner_id is not None:
            clauses.append("owner_id=?")
            values.append(str(owner_id))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,created_at FROM scans WHERE {} "
                "ORDER BY created_at DESC,id DESC".format(" AND ".join(clauses)),
                values,
            ).fetchall()
        cutoff = time.time() - retention_days * 86400 if retention_days else None
        candidates = []
        for position, row in enumerate(rows):
            expired = False
            if cutoff is not None:
                try:
                    expired = calendar.timegm(
                        time.strptime(str(row["created_at"])[:19], "%Y-%m-%d %H:%M:%S")
                    ) < cutoff
                except (TypeError, ValueError, OverflowError):
                    expired = False
            overflow = bool(max_scans and position >= max_scans)
            if expired or overflow:
                candidates.append(str(row["id"]))
            if len(candidates) >= limit:
                break
        result = {
            "dry_run": bool(dry_run),
            "candidate_scan_ids": candidates,
            "deleted": [],
        }
        if dry_run:
            return result
        for candidate in candidates:
            deleted = self.delete_scan(
                candidate, owner_id=owner_id, output_dir=output_dir
            )
            if deleted.get("deleted"):
                result["deleted"].append(deleted)
        return result

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

    def latest_artifact(self, scan_id, owner_id, kind=None):
        clauses = ["scan_id=?", "owner_id=?"]
        values = [str(scan_id), str(owner_id)]
        if kind:
            clauses.append("kind=?")
            values.append(str(kind))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT filename,scan_id,job_id,kind FROM output_artifacts WHERE {} "
                "ORDER BY rowid DESC LIMIT 1".format(" AND ".join(clauses)), values,
            ).fetchone()
        return dict(row) if row else None

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
            # Durable inventories already own a normalized lazy tree. Replacing
            # it with the root-only compatibility projection would discard all
            # children after every analysis batch.
            if payload.get("inventory_mode") != "durable_paged_v1":
                self._replace_tree_index(conn, scan_id, "physical", payload.get("tree") or {})
            conn.execute("DELETE FROM package_overviews WHERE scan_id=?", (str(scan_id),))

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
        translation_sidecar = None
        with self.lock, self._connect() as conn:
            translation_sidecar = self._invalidate_translation_if_changed(
                conn, scan_id, node_path, payload
            )
            conn.execute(
                "INSERT OR REPLACE INTO unified_documents(scan_id,node_path,payload) VALUES (?,?,?)",
                (scan_id, node_path, stored),
            )
            conn.execute("DELETE FROM package_overviews WHERE scan_id=?", (str(scan_id),))
        self._unlink_translation_sidecar(translation_sidecar)

    def save_documents(self, scan_id, documents):
        rows = []
        for node_path, payload in documents or []:
            rows.append((
                str(scan_id), str(node_path),
                self._store_document_payload(scan_id, node_path, payload),
                payload,
            ))
        if not rows:
            return 0
        translation_sidecars = []
        with self.lock, self._connect() as conn:
            for row_scan_id, row_node_path, _stored, payload in rows:
                sidecar = self._invalidate_translation_if_changed(
                    conn, row_scan_id, row_node_path, payload
                )
                if sidecar:
                    translation_sidecars.append(sidecar)
            conn.executemany(
                "INSERT OR REPLACE INTO unified_documents(scan_id,node_path,payload) VALUES (?,?,?)",
                [row[:3] for row in rows],
            )
            conn.execute("DELETE FROM package_overviews WHERE scan_id=?", (str(scan_id),))
        for sidecar in translation_sidecars:
            self._unlink_translation_sidecar(sidecar)
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
        translation_sidecar = None
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
            translation_sidecar = self._delete_translation_state(
                conn, scan_id, node_path
            )
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
        self._unlink_translation_sidecar(translation_sidecar)
        return deleted

    def iter_documents(self, scan_id, hydrate=True, batch_size=100, start_after=None):
        """Yield documents from bounded SQLite batches.

        This avoids retaining both every serialized JSON row and every decoded
        document at once.  Consumers may still choose to build their own
        index, but the database cursor itself adds only one bounded batch.
        """
        batch_size = max(1, min(1000, int(batch_size or 100)))
        with self._connect() as conn:
            if start_after:
                cursor = conn.execute(
                    "SELECT node_path,payload FROM unified_documents "
                    "WHERE scan_id=? AND node_path>? ORDER BY node_path",
                    (str(scan_id), str(start_after)),
                )
            else:
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

    def iter_structured_documents(
        self, scan_id, hydrate=False, batch_size=100, source_paths=None
    ):
        """Yield only documents with structured profiles.

        SQLite JSON1 is preferred for the large-package path.  Some supported
        Python/SQLite builds omit that extension, so retain a streaming
        payload filter rather than failing every structured-data query.
        """
        batch_size = max(1, min(500, int(batch_size or 100)))
        source_paths = sorted(set(
            str(item) for item in (source_paths or []) if item
        ))
        with self._connect() as conn:
            source_filter = ""
            if source_paths:
                conn.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS structured_allowed_sources("
                    "path TEXT PRIMARY KEY)"
                )
                conn.execute("DELETE FROM structured_allowed_sources")
                conn.executemany(
                    "INSERT OR IGNORE INTO structured_allowed_sources(path) VALUES (?)",
                    [(path,) for path in source_paths],
                )
                source_filter = (
                    "AND EXISTS (SELECT 1 FROM structured_allowed_sources a "
                    "WHERE d.node_path=a.path OR d.node_path LIKE a.path || '/%' "
                    "OR d.node_path LIKE a.path || '::%') "
                )
            fallback_payload_filter = False
            try:
                cursor = conn.execute(
                    "SELECT d.node_path,d.payload FROM unified_documents d WHERE d.scan_id=? "
                    "AND (json_type(payload,'$.data_profile') IS NOT NULL "
                    "OR json_array_length(COALESCE(json_extract(payload,'$.data_profiles'),'[]'))>0) "
                    + source_filter + "ORDER BY d.node_path",
                    (str(scan_id),),
                )
            except sqlite3.OperationalError as exc:
                if "json_" not in str(exc).lower() and "no such function: json" not in str(exc).lower():
                    raise
                # Preserve source-path filtering in SQL.  Only the structured
                # profile predicate is evaluated per row below.
                fallback_payload_filter = True
                cursor = conn.execute(
                    "SELECT d.node_path,d.payload FROM unified_documents d WHERE d.scan_id=? "
                    + source_filter + "ORDER BY d.node_path",
                    (str(scan_id),),
                )
            try:
                while True:
                    rows = cursor.fetchmany(batch_size)
                    if not rows:
                        break
                    for row in rows:
                        payload = (
                            self._load_document_payload(row["payload"])
                            if hydrate else json.loads(row["payload"])
                        )
                        if fallback_payload_filter and not (
                            payload.get("data_profile") or payload.get("data_profiles")
                        ):
                            continue
                        yield {"path": row["node_path"], "payload": payload}
            finally:
                cursor.close()

    def list_documents(self, scan_id, hydrate=True):
        # Compatibility API: callers still receive the same ordered list.
        return list(self.iter_documents(scan_id, hydrate=hydrate))

    def save_file_preview(self, scan_id, node_path, payload):
        """Persist one bounded first-pass preview independently of deep parses."""
        payload = dict(payload or {})
        language = payload.get("language") or {}
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO file_previews("
                "scan_id,node_path,status,sample_sha256,preview_fingerprint,source_size,"
                "source_modified_at_ns,language_code,document_type,payload,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (
                    str(scan_id), str(node_path), str(payload.get("status") or "unknown"),
                    str(payload.get("sample_sha256") or ""),
                    str(payload.get("preview_fingerprint") or ""),
                    int(payload.get("size") or 0),
                    int(payload.get("modified_at_ns") or 0),
                    str(language.get("code") or "unknown"),
                    str(payload.get("document_type") or "其他文件"),
                    self._store_preview_payload(scan_id, node_path, payload),
                ),
            )
            conn.execute("DELETE FROM package_overviews WHERE scan_id=?", (str(scan_id),))

    def save_exploration_batch(
        self, scan_id, previews, documents, states, evidence_by_path=None,
        remove_document_paths=None,
    ):
        """Atomically publish one bounded exploration checkpoint batch.

        Preview, projected document and file-state rows form one logical
        checkpoint.  Publishing them in separate transactions can leave a
        permanently incomplete resume state after a worker crash.
        """
        preview_rows = []
        for node_path, payload in previews or []:
            payload = dict(payload or {})
            language = payload.get("language") or {}
            preview_rows.append((
                str(scan_id), str(node_path), str(payload.get("status") or "unknown"),
                str(payload.get("sample_sha256") or ""),
                str(payload.get("preview_fingerprint") or ""),
                int(payload.get("size") or 0), int(payload.get("modified_at_ns") or 0),
                str(language.get("code") or "unknown"),
                str(payload.get("document_type") or "其他文件"),
                self._store_preview_payload(scan_id, node_path, payload),
            ))
        document_rows = [
            (str(scan_id), str(node_path), self._store_document_payload(scan_id, node_path, payload))
            for node_path, payload in (documents or [])
        ]
        state_rows = []
        for node_path, fingerprint, status, document, error in states or []:
            document = document or {}
            state_rows.append((
                str(scan_id), str(node_path), str(fingerprint or ""), str(status),
                (document.get("parser") or {}).get("name"), len(document.get("text") or ""),
                len(document.get("evidence") or []), str(error)[:2000] if error else None,
            ))
        evidence_rows = []
        evidence_paths = []
        for node_path, chunks in evidence_by_path or []:
            node_path = str(node_path)
            evidence_paths.append(node_path)
            for item in chunks or []:
                payload = dict(item)
                source_path = str(payload.get("source_path") or node_path)
                archive_source = str(payload.get("archive_source_path") or "")
                identity = "{}|{}|{}|{}".format(
                    source_path, payload.get("evidence_id") or "",
                    payload.get("content_sha256") or "", payload.get("text") or "",
                )
                evidence_rows.append((
                    str(scan_id),
                    hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest(),
                    source_path, archive_source,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ))
        if not (preview_rows or document_rows or state_rows or evidence_paths):
            return 0
        with self.lock, self._connect() as conn:
            if preview_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO file_previews("
                    "scan_id,node_path,status,sample_sha256,preview_fingerprint,source_size,"
                    "source_modified_at_ns,language_code,document_type,payload,updated_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)", preview_rows,
                )
            if document_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO unified_documents(scan_id,node_path,payload) VALUES (?,?,?)",
                    document_rows,
                )
            if remove_document_paths:
                conn.executemany(
                    "DELETE FROM unified_documents WHERE scan_id=? AND node_path=?",
                    [(str(scan_id), str(path)) for path in remove_document_paths],
                )
            if state_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO file_analysis_states("
                    "scan_id,node_path,fingerprint,status,parser,stored_characters,evidence_count,error,updated_at"
                    ") VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)", state_rows,
                )
            for node_path in evidence_paths:
                delete_sql = (
                    "scan_id=? AND (source_path=? OR archive_source_path=? OR source_path LIKE ?) "
                    "AND index_key NOT LIKE 'translation:%'"
                )
                delete_values = (
                    str(scan_id), node_path, node_path, node_path + "::%",
                )
                conn.execute("DELETE FROM evidence_index WHERE " + delete_sql, delete_values)
                if self.evidence_fts_available:
                    conn.execute("DELETE FROM evidence_fts WHERE " + delete_sql, delete_values)
            if evidence_rows:
                conn.executemany(
                    "INSERT INTO evidence_index(scan_id,index_key,source_path,archive_source_path,payload) "
                    "VALUES (?,?,?,?,?)", evidence_rows,
                )
                if self.evidence_fts_available:
                    conn.executemany(
                        "INSERT INTO evidence_fts(scan_id,index_key,source_path,archive_source_path,section,text) "
                        "VALUES (?,?,?,?,?,?)",
                        [
                            (
                                row[0], row[1], row[2], row[3],
                                str(json.loads(row[4]).get("section") or ""),
                                str(json.loads(row[4]).get("text") or ""),
                            )
                            for row in evidence_rows
                        ],
                    )
            conn.execute("DELETE FROM package_overviews WHERE scan_id=?", (str(scan_id),))
        return max(len(preview_rows), len(document_rows), len(state_rows), len(evidence_paths))

    def save_file_previews(self, scan_id, previews):
        rows = []
        for node_path, payload in previews or []:
            payload = dict(payload or {})
            language = payload.get("language") or {}
            rows.append((
                str(scan_id), str(node_path), str(payload.get("status") or "unknown"),
                str(payload.get("sample_sha256") or ""),
                str(payload.get("preview_fingerprint") or ""),
                int(payload.get("size") or 0),
                int(payload.get("modified_at_ns") or 0),
                str(language.get("code") or "unknown"),
                str(payload.get("document_type") or "其他文件"),
                self._store_preview_payload(scan_id, node_path, payload),
            ))
        if not rows:
            return 0
        with self.lock, self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO file_previews("
                "scan_id,node_path,status,sample_sha256,preview_fingerprint,source_size,"
                "source_modified_at_ns,language_code,document_type,payload,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                rows,
            )
            conn.execute("DELETE FROM package_overviews WHERE scan_id=?", (str(scan_id),))
        return len(rows)

    def get_file_preview(self, scan_id, node_path):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM file_previews WHERE scan_id=? AND node_path=?",
                (str(scan_id), str(node_path)),
            ).fetchone()
        return self._load_preview_payload(row["payload"]) if row else None

    def iter_file_previews(self, scan_id, statuses=None, batch_size=500):
        batch_size = max(1, min(2000, int(batch_size or 500)))
        values = [str(scan_id)]
        where = "scan_id=?"
        statuses = [str(value) for value in (statuses or []) if value]
        if statuses:
            where += " AND status IN ({})".format(",".join("?" for _ in statuses))
            values.extend(statuses)
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT node_path,payload FROM file_previews WHERE {} ORDER BY node_path".format(where),
                values,
            )
            try:
                while True:
                    rows = cursor.fetchmany(batch_size)
                    if not rows:
                        break
                    for row in rows:
                        yield {
                            "path": row["node_path"],
                            "payload": self._load_preview_payload(row["payload"]),
                        }
            finally:
                cursor.close()

    def iter_file_preview_states(self, scan_id, statuses=None, batch_size=500):
        """Yield payload-free preview checkpoints in bounded SQLite batches.

        Resume logic must never deserialize ``preview_text`` for every file.
        These explicit columns are deliberately sufficient to validate the
        inventory size/mtime checkpoint and to schedule language work.
        """
        batch_size = max(1, min(5000, int(batch_size or 500)))
        values = [str(scan_id)]
        where = "scan_id=?"
        statuses = [str(value) for value in (statuses or []) if value]
        if statuses:
            where += " AND status IN ({})".format(",".join("?" for _ in statuses))
            values.extend(statuses)
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT node_path,status,sample_sha256,preview_fingerprint,source_size,"
                "source_modified_at_ns,language_code,document_type "
                "FROM file_previews WHERE {} ORDER BY node_path".format(where),
                values,
            )
            try:
                while True:
                    rows = cursor.fetchmany(batch_size)
                    if not rows:
                        break
                    for row in rows:
                        yield {
                            "path": row["node_path"],
                            "status": row["status"],
                            "sample_sha256": row["sample_sha256"],
                            "preview_fingerprint": row["preview_fingerprint"],
                            "size": int(row["source_size"] or 0),
                            "modified_at_ns": int(row["source_modified_at_ns"] or 0),
                            "language_code": row["language_code"] or "unknown",
                            "document_type": row["document_type"] or "其他文件",
                        }
            finally:
                cursor.close()

    def file_preview_counts(self, scan_id):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status,COUNT(*) AS value FROM file_previews WHERE scan_id=? GROUP BY status",
                (str(scan_id),),
            ).fetchall()
        return {row["status"]: int(row["value"]) for row in rows}

    def rebuild_file_relation_features(self, scan_id, rows, batch_size=5000):
        """Replace the scalable all-file relation feature index.

        The user-facing content map remains intentionally small. This table is
        the complete recall substrate and is queried in SQLite, so relation
        recall is not limited by the graph preview's 1,200-edge display cap.
        """
        scan_id = str(scan_id)
        batch_size = max(100, min(20000, int(batch_size or 5000)))
        inserted = 0
        with self.lock, self._connect() as conn:
            conn.execute("DELETE FROM file_relation_features WHERE scan_id=?", (scan_id,))
            batch = []
            for node_path, kind, value, weight in rows or []:
                node_path = str(node_path or "")
                kind = str(kind or "")[:40]
                value = re.sub(r"\s+", " ", str(value or "")).strip().casefold()[:240]
                if not node_path or not kind or not value:
                    continue
                batch.append((scan_id, node_path, kind, value, float(weight or 1.0)))
                if len(batch) >= batch_size:
                    conn.executemany(
                        "INSERT OR REPLACE INTO file_relation_features("
                        "scan_id,node_path,feature_kind,feature_value,weight) VALUES (?,?,?,?,?)",
                        batch,
                    )
                    inserted += len(batch)
                    batch = []
            if batch:
                conn.executemany(
                    "INSERT OR REPLACE INTO file_relation_features("
                    "scan_id,node_path,feature_kind,feature_value,weight) VALUES (?,?,?,?,?)",
                    batch,
                )
                inserted += len(batch)
        return inserted

    def clear_file_relation_features(self, scan_id):
        with self.lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM file_relation_features WHERE scan_id=?", (str(scan_id),)
            )

    def append_file_relation_features(self, scan_id, rows):
        prepared = []
        for node_path, kind, value, weight in rows or []:
            node_path = str(node_path or "")
            kind = str(kind or "")[:40]
            value = re.sub(r"\s+", " ", str(value or "")).strip().casefold()[:240]
            if node_path and kind and value:
                prepared.append((str(scan_id), node_path, kind, value, float(weight or 1.0)))
        if not prepared:
            return 0
        with self.lock, self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO file_relation_features("
                "scan_id,node_path,feature_kind,feature_value,weight) VALUES (?,?,?,?,?)",
                prepared,
            )
        return len(prepared)

    def recall_file_relation_features(self, scan_id, completed_paths,
                                      eligible_paths, limit=5000):
        """Recall all-file neighbours with inverse-frequency feature scoring."""
        completed = sorted({str(path) for path in completed_paths or [] if path})
        eligible = sorted({str(path) for path in eligible_paths or [] if path})
        if not completed or not eligible:
            return []
        limit = max(1, min(5000, int(limit or 5000)))
        with self._connect() as conn:
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS relation_completed(path TEXT PRIMARY KEY)")
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS relation_eligible(path TEXT PRIMARY KEY)")
            conn.execute("DELETE FROM relation_completed")
            conn.execute("DELETE FROM relation_eligible")
            conn.executemany("INSERT OR IGNORE INTO relation_completed(path) VALUES (?)", [(p,) for p in completed])
            conn.executemany("INSERT OR IGNORE INTO relation_eligible(path) VALUES (?)", [(p,) for p in eligible])
            rows = conn.execute(
                "WITH frequencies AS ("
                " SELECT feature_kind,feature_value,COUNT(*) AS n"
                " FROM file_relation_features WHERE scan_id=?"
                " GROUP BY feature_kind,feature_value HAVING n BETWEEN 2 AND 10000"
                "), source_features AS ("
                " SELECT DISTINCT f.feature_kind,f.feature_value"
                " FROM file_relation_features f JOIN relation_completed c ON c.path=f.node_path"
                " WHERE f.scan_id=?"
                ")"
                " SELECT target.node_path,"
                " SUM((target.weight*1.0)/frequencies.n) AS score,"
                " GROUP_CONCAT(target.feature_kind || ':' || target.feature_value, ' | ') AS reasons"
                " FROM source_features source"
                " JOIN frequencies ON frequencies.feature_kind=source.feature_kind"
                "  AND frequencies.feature_value=source.feature_value"
                " JOIN file_relation_features target ON target.scan_id=?"
                "  AND target.feature_kind=source.feature_kind"
                "  AND target.feature_value=source.feature_value"
                " JOIN relation_eligible e ON e.path=target.node_path"
                " GROUP BY target.node_path ORDER BY score DESC,target.node_path LIMIT ?",
                (str(scan_id), str(scan_id), str(scan_id), limit),
            ).fetchall()
        output = []
        for row in rows:
            reasons = list(dict.fromkeys(
                item.strip() for item in str(row["reasons"] or "").split(" | ") if item.strip()
            ))[:8]
            output.append({
                "path": row["node_path"],
                "score": round(float(row["score"] or 0.0), 6),
                "reasons": reasons,
            })
        return output

    def count_file_relation_features(self, scan_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS value FROM file_relation_features WHERE scan_id=?",
                (str(scan_id),),
            ).fetchone()
        return int(row["value"] or 0) if row else 0

    @staticmethod
    def _normalize_relationship(item, source_kind):
        """Convert all relationship producers to one auditable edge contract.

        Feature-index rows deliberately do not enter here: they are retrieval
        hints, not asserted edges between two files.  Keeping that distinction
        prevents a shared keyword from being rendered as a confirmed relation.
        """
        item = dict(item or {})
        source = str(item.get("source_path") or item.get("source") or "").strip()
        target = str(item.get("target_path") or item.get("target") or "").strip()
        if not source or not target or source == target:
            return None
        relation_type = str(item.get("relation_type") or item.get("type") or "related").strip()[:80]
        try:
            confidence = float(item.get("confidence", item.get("weight", 0)) or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        # Preview weights are counts rather than calibrated probabilities.
        if source_kind == "preview_content_map":
            confidence = min(0.69, 0.30 + 0.08 * max(0.0, confidence))
        confidence = max(0.0, min(1.0, confidence))
        reasons = item.get("reasons") or item.get("reason") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        evidence = item.get("evidence") or item.get("evidence_ids") or []
        if isinstance(evidence, dict):
            evidence = [evidence]
        elif isinstance(evidence, str):
            evidence = [evidence]
        key = hashlib.sha256(
            "\\0".join((source, target, relation_type)).encode("utf-8", errors="replace")
        ).hexdigest()
        return {
            "relation_id": str(item.get("relation_id") or key[:20]),
            "relation_key": key,
            "source_path": source,
            "target_path": target,
            "relation_type": relation_type or "related",
            "confidence": round(confidence, 4),
            "status": str(item.get("status") or "derived"),
            "calibration": "validated" if source_kind == "homogeneous_analysis" else "derived",
            "source_kinds": [source_kind],
            "reasons": list(reasons)[:20],
            "evidence": list(evidence)[:30],
        }

    def rebuild_relationship_catalog(self, scan_id):
        """Merge edge-producing analyses into the shared relationship contract.

        The operation is intentionally idempotent and bounded by persisted
        analysis output.  Consumers use this catalog rather than silently
        mixing validated edges with preview hints on their own.
        """
        scan_id = str(scan_id)
        with self.lock, self._connect() as conn:
            homogeneous = conn.execute(
                "SELECT payload FROM homogeneous_relations WHERE scan_id=?", (scan_id,)
            ).fetchall()
            package_row = conn.execute(
                "SELECT payload FROM package_analyses WHERE scan_id=?", (scan_id,)
            ).fetchone()
            map_row = conn.execute(
                "SELECT payload FROM package_content_maps WHERE scan_id=?", (scan_id,)
            ).fetchone()
            candidates = []
            for row in homogeneous:
                candidates.append((json.loads(row["payload"]), "homogeneous_analysis"))
            if package_row:
                package = json.loads(package_row["payload"])
                for key in ("file_relationships", "relationships"):
                    value = package.get(key) or []
                    if isinstance(value, dict):
                        value = value.get("items") or value.get("relationships") or []
                    candidates.extend((item, "package_analysis") for item in value if isinstance(item, dict))
                graph = package.get("relationship_graph") or {}
                candidates.extend((item, "package_analysis") for item in (graph.get("edges") or []) if isinstance(item, dict))
            if map_row:
                content_map = json.loads(map_row["payload"])
                candidates.extend((item, "preview_content_map") for item in (content_map.get("relationships") or []) if isinstance(item, dict))

            merged = {}
            for item, source_kind in candidates:
                relation = self._normalize_relationship(item, source_kind)
                if not relation:
                    continue
                current = merged.get(relation["relation_key"])
                if current is None:
                    merged[relation["relation_key"]] = relation
                    continue
                current["confidence"] = max(current["confidence"], relation["confidence"])
                current["source_kinds"] = sorted(set(current["source_kinds"] + relation["source_kinds"]))
                current["reasons"] = (current["reasons"] + [x for x in relation["reasons"] if x not in current["reasons"]])[:20]
                current["evidence"] = (current["evidence"] + [x for x in relation["evidence"] if x not in current["evidence"]])[:30]
                if "validated" in (current["calibration"], relation["calibration"]):
                    current["calibration"] = "validated"
            conn.execute("DELETE FROM relationship_catalog WHERE scan_id=?", (scan_id,))
            conn.executemany(
                "INSERT INTO relationship_catalog(scan_id,relation_key,source_path,target_path,relation_type,confidence,payload,updated_at) "
                "VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                [(scan_id, item["relation_key"], item["source_path"], item["target_path"],
                  item["relation_type"], item["confidence"], json.dumps(item, ensure_ascii=False))
                 for item in merged.values()],
            )
            conn.execute("DELETE FROM package_overviews WHERE scan_id=?", (scan_id,))
        return len(merged)

    def get_relationship_catalog(self, scan_id, source_path=None, limit=2000, refresh=False):
        scan_id = str(scan_id)
        limit = max(1, min(10000, int(limit or 2000)))
        if refresh:
            self.rebuild_relationship_catalog(scan_id)
        with self._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS value FROM relationship_catalog WHERE scan_id=?", (scan_id,)
            ).fetchone()
        if not int(count["value"] or 0):
            self.rebuild_relationship_catalog(scan_id)
        clauses, values = ["scan_id=?"], [scan_id]
        if source_path:
            clauses.append("(source_path=? OR target_path=?)")
            values.extend((str(source_path), str(source_path)))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM relationship_catalog WHERE {} ORDER BY confidence DESC,relation_key LIMIT ?".format(" AND ".join(clauses)),
                values + [limit],
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) AS value FROM relationship_catalog WHERE {}".format(" AND ".join(clauses)), values,
            ).fetchone()
        return {
            "schema_version": "relationship-catalog/1.0",
            "items": [json.loads(row["payload"]) for row in rows],
            "relationship_count": int(total["value"] or 0),
            "truncated": int(total["value"] or 0) > len(rows),
            "contract": {"feature_index_is_recall_only": True, "sources": ["homogeneous_analysis", "package_analysis", "preview_content_map"]},
        }

    def save_content_map(self, scan_id, payload):
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO package_content_maps(scan_id,payload,updated_at) "
                "VALUES (?,?,CURRENT_TIMESTAMP)",
                (str(scan_id), json.dumps(payload or {}, ensure_ascii=False)),
            )
            conn.execute("DELETE FROM package_overviews WHERE scan_id=?", (str(scan_id),))
            conn.execute("DELETE FROM relationship_catalog WHERE scan_id=?", (str(scan_id),))

    def get_content_map(self, scan_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM package_content_maps WHERE scan_id=?", (str(scan_id),)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_package_overview(self, scan_id, payload):
        payload = dict(payload or {})
        schema_version = str(payload.get("schema_version") or "package-overview/1.0")
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO package_overviews(scan_id,schema_version,payload,updated_at) "
                "VALUES (?,?,?,CURRENT_TIMESTAMP)",
                (str(scan_id), schema_version, json.dumps(payload, ensure_ascii=False)),
            )

    def get_package_overview(self, scan_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM package_overviews WHERE scan_id=?", (str(scan_id),)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def invalidate_package_overview(self, scan_id):
        with self.lock, self._connect() as conn:
            conn.execute("DELETE FROM package_overviews WHERE scan_id=?", (str(scan_id),))

    def iter_tree_records(self, scan_id, tree_kind="physical", batch_size=500):
        """Stream flattened tree projections without hydrating the nested tree."""
        if tree_kind not in {"physical", "analysis"} and not str(tree_kind).startswith("analysis:"):
            raise ValueError("未知目录树类型")
        batch_size = max(1, min(5000, int(batch_size or 500)))
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT payload FROM tree_nodes WHERE scan_id=? AND tree_kind=? ORDER BY node_key",
                (str(scan_id), str(tree_kind)),
            )
            try:
                while True:
                    rows = cursor.fetchmany(batch_size)
                    if not rows:
                        break
                    for row in rows:
                        yield json.loads(row["payload"])
            finally:
                cursor.close()

    def save_translation(self, scan_id, node_path, payload):
        payload = dict(payload or {})
        stored = self._store_translation_payload(scan_id, node_path, payload)
        scan_id = str(scan_id)
        node_path = str(node_path)
        index_rows = []
        if payload.get("status") in {"completed", "not_required", "partial"}:
            for unit in payload.get("units") or []:
                if not isinstance(unit, dict) or unit.get("status") not in {"completed", "not_required"}:
                    continue
                original = str(unit.get("source_text") or "")
                translated = str(unit.get("target_text") or "")
                if not original.strip() or not translated.strip():
                    continue
                evidence_id = "TR-{}".format(unit.get("unit_id") or hashlib.sha256(
                    original.encode("utf-8", errors="replace")
                ).hexdigest()[:16])
                indexed = {
                    "evidence_id": evidence_id,
                    "source_path": node_path,
                    "section": unit.get("section") or unit.get("block_kind") or unit.get("kind"),
                    "label": "translation_unit",
                    "text": original,
                    "original_text": original,
                    "translated_text": translated,
                    "source_language": unit.get("source_language") or payload.get("source_language"),
                    "target_language": payload.get("target_language") or "zh-CN",
                    "char_start": unit.get("start") if unit.get("kind") == "body" else None,
                    "char_end": unit.get("end") if unit.get("kind") == "body" else None,
                    "paragraph_index": unit.get("paragraph_index"),
                    "block_kind": unit.get("block_kind") or unit.get("kind"),
                    "index_kind": "translation",
                    "translation_source_fingerprint": payload.get("source_fingerprint"),
                    "content_sha256": hashlib.sha256(
                        (original + "\0" + translated).encode("utf-8", errors="replace")
                    ).hexdigest(),
                }
                index_key = "translation:" + hashlib.sha256(
                    (node_path + "\0" + evidence_id).encode("utf-8", errors="replace")
                ).hexdigest()
                index_rows.append((
                    scan_id, index_key, node_path, "",
                    json.dumps(indexed, ensure_ascii=False, separators=(",", ":")),
                    str(indexed.get("section") or ""),
                    original + "\n" + translated,
                ))
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO document_translations("
                "scan_id,node_path,source_fingerprint,status,source_language,provider_id,payload,updated_at"
                ") VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (
                    scan_id, node_path, str(payload.get("source_fingerprint") or ""),
                    str(payload.get("status") or "unknown"),
                    str(payload.get("source_language") or "unknown"),
                    str(payload.get("provider_id") or ""), stored,
                ),
            )
            conn.execute(
                "DELETE FROM evidence_index WHERE scan_id=? AND source_path=? "
                "AND index_key LIKE 'translation:%'",
                (scan_id, node_path),
            )
            if self.evidence_fts_available:
                conn.execute(
                    "DELETE FROM evidence_fts WHERE scan_id=? AND source_path=? "
                    "AND index_key LIKE 'translation:%'",
                    (scan_id, node_path),
                )
            if index_rows:
                conn.executemany(
                    "INSERT INTO evidence_index(scan_id,index_key,source_path,archive_source_path,payload) "
                    "VALUES (?,?,?,?,?)",
                    [row[:5] for row in index_rows],
                )
                if self.evidence_fts_available:
                    conn.executemany(
                        "INSERT INTO evidence_fts(scan_id,index_key,source_path,archive_source_path,section,text) "
                        "VALUES (?,?,?,?,?,?)",
                        [(row[0], row[1], row[2], row[3], row[5], row[6]) for row in index_rows],
                    )

    def get_translation(self, scan_id, node_path, hydrate=True):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM document_translations WHERE scan_id=? AND node_path=?",
                (str(scan_id), str(node_path)),
            ).fetchone()
        return self._load_translation_payload(row["payload"], hydrate=hydrate) if row else None

    def iter_translations(self, scan_id, statuses=None, hydrate=False, batch_size=200):
        batch_size = max(1, min(1000, int(batch_size or 200)))
        values = [str(scan_id)]
        where = "scan_id=?"
        statuses = [str(value) for value in (statuses or []) if value]
        if statuses:
            where += " AND status IN ({})".format(",".join("?" for _ in statuses))
            values.extend(statuses)
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT node_path,payload FROM document_translations WHERE {} ORDER BY node_path".format(where),
                values,
            )
            try:
                while True:
                    rows = cursor.fetchmany(batch_size)
                    if not rows:
                        break
                    for row in rows:
                        yield {
                            "path": row["node_path"],
                            "payload": self._load_translation_payload(row["payload"], hydrate=hydrate),
                        }
            finally:
                cursor.close()

    @staticmethod
    def _translation_list_query():
        """Return the compact, durable source for the translation workbench.

        Translation rows alone are not a complete work list: a large package
        can contain many language-classified files which have not been
        translated yet.  The union intentionally contains only persisted
        metadata keys, never document or preview bodies, so filtering and
        paging remain bounded even when the package has large sidecars.
        """
        return """
            WITH candidate_paths AS (
                SELECT node_path FROM document_translations WHERE scan_id=?
                UNION SELECT node_path FROM file_workflow_states WHERE scan_id=?
                UNION SELECT node_path FROM file_previews WHERE scan_id=?
                UNION SELECT node_path FROM unified_documents WHERE scan_id=?
            ), joined AS (
                SELECT
                    paths.node_path,
                    translation.status AS translation_status,
                    translation.source_language AS translation_language,
                    translation.payload AS translation_payload,
                    translation.updated_at AS translation_updated_at,
                    workflow.language_code AS workflow_language,
                    workflow.updated_at AS workflow_updated_at,
                    preview.language_code AS preview_language,
                    preview.status AS preview_status,
                    preview.updated_at AS preview_updated_at,
                    document.created_at AS document_created_at,
                    CASE
                        WHEN document.node_path IS NOT NULL THEN 'document'
                        WHEN preview.node_path IS NOT NULL THEN 'preview'
                        ELSE 'metadata'
                    END AS source_availability
                FROM candidate_paths paths
                LEFT JOIN document_translations translation
                    ON translation.scan_id=? AND translation.node_path=paths.node_path
                LEFT JOIN file_workflow_states workflow
                    ON workflow.scan_id=? AND workflow.node_path=paths.node_path
                LEFT JOIN file_previews preview
                    ON preview.scan_id=? AND preview.node_path=paths.node_path
                LEFT JOIN unified_documents document
                    ON document.scan_id=? AND document.node_path=paths.node_path
            ), projected AS (
                SELECT
                    node_path,
                    COALESCE(NULLIF(LOWER(translation_status), ''), 'not_started') AS status,
                    CASE
                        WHEN LOWER(COALESCE(translation_language, '')) LIKE 'zh%' THEN 'zh'
                        WHEN LOWER(COALESCE(translation_language, '')) NOT IN ('', 'unknown')
                            THEN LOWER(translation_language)
                        WHEN LOWER(COALESCE(workflow_language, '')) LIKE 'zh%' THEN 'zh'
                        WHEN LOWER(COALESCE(workflow_language, '')) NOT IN ('', 'unknown')
                            THEN LOWER(workflow_language)
                        WHEN LOWER(COALESCE(preview_language, '')) LIKE 'zh%' THEN 'zh'
                        WHEN LOWER(COALESCE(preview_language, '')) NOT IN ('', 'unknown')
                            THEN LOWER(preview_language)
                        ELSE 'unknown'
                    END AS source_language,
                    translation_payload,
                    preview_status,
                    source_availability,
                    COALESCE(
                        translation_updated_at, workflow_updated_at,
                        preview_updated_at, document_created_at
                    ) AS updated_at
                FROM joined
            )
        """

    @staticmethod
    def _translation_list_filter(status=None, statuses=None, language=None, query=None):
        """Build a validated SQL predicate for the translation work list."""
        valid_statuses = {
            'all', 'completed', 'partial', 'failed', 'not_required',
            'not_started', 'pending', 'running', 'cancelled',
            'cache_unavailable',
        }
        raw_statuses = statuses if statuses is not None else [status]
        if isinstance(raw_statuses, str):
            raw_statuses = [raw_statuses]
        normalized_statuses = []
        for value in raw_statuses or []:
            value = str(value or '').strip().lower()
            if not value or value == 'all':
                continue
            if value not in valid_statuses:
                raise ValueError('未知翻译状态筛选条件')
            if value not in normalized_statuses:
                normalized_statuses.append(value)

        language = str(language or 'all').strip().lower()
        if language != 'all' and language != 'foreign' and not re.match(
            r'^[a-z0-9][a-z0-9_-]{0,31}$', language
        ):
            raise ValueError('未知源语言筛选条件')

        query = str(query or '').strip()
        if len(query) > 500:
            raise ValueError('文件搜索条件不能超过 500 个字符')

        clauses = []
        values = []
        if normalized_statuses:
            clauses.append('status IN ({})'.format(','.join('?' for _ in normalized_statuses)))
            values.extend(normalized_statuses)
        if language == 'foreign':
            clauses.append("source_language NOT IN ('zh', 'unknown')")
        elif language != 'all':
            clauses.append('source_language=?')
            values.append(language)
        if query:
            # Treat `%`, `_` and `\\` as literal user input.  A file-name
            # search must never turn into an unexpectedly broad SQL pattern.
            # Translation payloads are kept as compact projections for large
            # sidecars, so this also preserves the established ability to
            # find a translated title without hydrating document-sized text.
            escaped = query.casefold().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            pattern = '%{}%'.format(escaped)
            clauses.append("(LOWER(node_path) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(translation_payload, '')) LIKE ? ESCAPE '\\')")
            values.extend((pattern, pattern))
        return (
            (' WHERE ' + ' AND '.join(clauses)) if clauses else '',
            values, normalized_statuses, language, query,
        )

    def list_translation_page(self, scan_id, offset=0, limit=100,
                              status=None, statuses=None, language=None, query=None):
        """Page the complete translation work list without hydrating bodies.

        The returned total refers to the active filters.  ``status_counts``
        and ``language_counts`` describe the complete list, so the client can
        show honest filter choices without loading every item into the browser.
        """
        offset = max(0, int(offset or 0))
        limit = max(1, min(500, int(limit or 100)))
        where, filter_values, statuses, language, query = self._translation_list_filter(
            status=status, statuses=statuses, language=language, query=query,
        )
        sql = self._translation_list_query()
        source_values = [str(scan_id)] * 8
        with self._connect() as conn:
            count_rows = conn.execute(
                sql + " SELECT status,source_language,COUNT(*) AS value FROM projected "
                "GROUP BY status,source_language",
                source_values,
            ).fetchall()
            total = int(conn.execute(
                sql + ' SELECT COUNT(*) AS value FROM projected' + where,
                source_values + filter_values,
            ).fetchone()['value'] or 0)
            rows = conn.execute(
                sql + ' SELECT * FROM projected' + where +
                ' ORDER BY node_path LIMIT ? OFFSET ?',
                source_values + filter_values + [limit, offset],
            ).fetchall()

        status_counts = {}
        language_counts = {}
        for row in count_rows:
            item_status = str(row['status'] or 'not_started')
            item_language = str(row['source_language'] or 'unknown')
            value = int(row['value'] or 0)
            status_counts[item_status] = status_counts.get(item_status, 0) + value
            language_counts[item_language] = language_counts.get(item_language, 0) + value

        items = []
        for row in rows:
            payload = self._load_translation_payload(
                row['translation_payload'], hydrate=False,
            ) if row['translation_payload'] else {}
            payload = dict(payload or {})
            original_title = str(
                payload.get('original_title') or payload.get('working_title') or ''
            ).strip()
            translated_title = str(payload.get('translated_title') or '').strip()
            path = str(row['node_path'])
            payload.update({
                'path': path,
                'status': str(row['status'] or 'not_started'),
                'source_language': str(row['source_language'] or 'unknown'),
                'source_availability': str(row['source_availability'] or 'metadata'),
                'preview_status': row['preview_status'],
                'titles': {
                    'original': original_title or Path(path).name or path,
                    'translated': translated_title or None,
                },
            })
            items.append(payload)

        next_offset = offset + len(items)
        return {
            'items': items,
            'offset': offset,
            'limit': limit,
            'total': total,
            'next_offset': next_offset if next_offset < total else None,
            'has_more': next_offset < total,
            'status': statuses[0] if len(statuses) == 1 else None,
            'statuses': statuses,
            'language': language,
            'query': query,
            'unfiltered_total': sum(status_counts.values()),
            'status_counts': status_counts,
            'language_counts': language_counts,
        }

    def translation_counts(self, scan_id):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status,COUNT(*) AS value FROM document_translations "
                "WHERE scan_id=? GROUP BY status", (str(scan_id),)
            ).fetchall()
        return {row["status"]: int(row["value"]) for row in rows}

    def get_translation_memory(self, memory_key):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM translation_memory WHERE memory_key=?", (str(memory_key),)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_translation_memory(self, memory_key, payload):
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO translation_memory(memory_key,payload,updated_at) "
                "VALUES (?,?,CURRENT_TIMESTAMP)",
                (str(memory_key), json.dumps(payload or {}, ensure_ascii=False)),
            )

    def save_conversation(self, payload, owner_id):
        payload = dict(payload or {})
        session_id = str(payload.get("session_id") or "")
        scan_id = str(payload.get("scan_id") or "")
        if not session_id or not scan_id:
            raise ValueError("会话必须包含 session_id 和 scan_id")
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO conversations(id,scan_id,owner_id,title,status,revision,payload) "
                "VALUES (?,?,?,?,?,0,?) ON CONFLICT(id) DO UPDATE SET "
                "title=excluded.title,status=excluded.status,payload=excluded.payload,"
                "revision=conversations.revision+1,updated_at=CURRENT_TIMESTAMP "
                "WHERE conversations.scan_id=excluded.scan_id AND conversations.owner_id=excluded.owner_id",
                (
                    session_id, scan_id, str(owner_id), str(payload.get("title") or "资料问答")[:200],
                    str(payload.get("status") or "active"), json.dumps(payload, ensure_ascii=False),
                ),
            )
            for position, message in enumerate(payload.get("messages") or [], 1):
                message_id = str(message.get("message_id") or "")
                if not message_id:
                    continue
                metadata = dict(message.get("metadata") or {})
                conn.execute(
                    "INSERT INTO conversation_messages("
                    "session_id,message_id,role,turn_id,sequence,payload,updated_at"
                    ") VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP) "
                    "ON CONFLICT(session_id,message_id) DO UPDATE SET "
                    "role=excluded.role,turn_id=COALESCE(excluded.turn_id,conversation_messages.turn_id),"
                    "sequence=COALESCE(conversation_messages.sequence,excluded.sequence),"
                    "payload=excluded.payload,updated_at=CURRENT_TIMESTAMP",
                    (
                        session_id, message_id, str(message.get("role") or "unknown"),
                        str(metadata.get("turn_id") or "") or None, position,
                        json.dumps(message, ensure_ascii=False),
                    ),
                )
        return session_id

    def get_conversation(self, session_id, owner_id, scan_id=None,
                         message_limit=100, before_sequence=None,
                         context_only=False):
        clauses = ["id=?", "owner_id=?"]
        values = [str(session_id), str(owner_id)]
        if scan_id is not None:
            clauses.append("scan_id=?")
            values.append(str(scan_id))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM conversations WHERE {}".format(" AND ".join(clauses)), values
            ).fetchone()
            if not row:
                return None
            payload = json.loads(row["payload"])
            total_messages = int(conn.execute(
                "SELECT COUNT(*) AS value FROM conversation_messages WHERE session_id=?",
                (str(session_id),),
            ).fetchone()["value"])
            limit = max(2, min(500, int(message_limit or 100)))
            message_values = [str(session_id)]
            message_where = "session_id=?"
            if context_only:
                summarized = max(0, int(payload.get("summarized_message_count") or 0))
                if summarized:
                    message_where += " AND sequence>?"
                    message_values.append(summarized)
            if before_sequence is not None:
                message_where += " AND sequence<?"
                message_values.append(max(1, int(before_sequence)))
            message_values.append(limit)
            message_rows = conn.execute(
                "SELECT sequence,payload FROM conversation_messages WHERE {} "
                "ORDER BY CASE WHEN sequence IS NULL THEN 1 ELSE 0 END DESC,"
                "sequence DESC,created_at DESC,message_id DESC LIMIT ?".format(
                    message_where
                ),
                message_values,
            ).fetchall()
        if message_rows:
            ordered_rows = list(reversed(message_rows))
            payload["messages"] = [json.loads(item["payload"]) for item in ordered_rows]
            first_sequence = ordered_rows[0]["sequence"]
        else:
            payload["messages"] = []
            first_sequence = None
        payload["message_page"] = {
            "limit": limit,
            "total": total_messages,
            "returned": len(message_rows),
            "before_sequence": first_sequence if first_sequence and first_sequence > 1 else None,
            "has_more": bool(first_sequence and first_sequence > 1),
        }
        return payload

    def save_homogeneous_analysis(self, scan_id, payload):
        """Atomically replace the derived ledger, relationship graph and cases."""
        payload = dict(payload or {})
        records = list(payload.pop("records", []) or [])
        relations = list(payload.pop("relations", []) or [])
        cases = list(payload.pop("cases", []) or [])
        anomalies = list(payload.pop("anomalies", []) or [])
        scan_id = str(scan_id)
        with self.lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM homogeneous_records WHERE scan_id=?", (scan_id,))
            conn.execute("DELETE FROM homogeneous_relations WHERE scan_id=?", (scan_id,))
            conn.execute("DELETE FROM homogeneous_cases WHERE scan_id=?", (scan_id,))
            conn.execute("DELETE FROM homogeneous_anomalies WHERE scan_id=?", (scan_id,))
            conn.execute(
                "INSERT OR REPLACE INTO homogeneous_analyses("
                "scan_id,schema_version,status,payload,updated_at) "
                "VALUES (?,?,?,?,CURRENT_TIMESTAMP)",
                (
                    scan_id,
                    str(payload.get("schema_version") or "homogeneous-documents/1.0"),
                    str(payload.get("status") or "completed"),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            record_rows = []
            for record in records:
                fields = record.get("fields") or {}
                record_rows.append((
                    scan_id, str(record.get("path") or ""),
                    str(fields.get("date") or ""),
                    str(fields.get("document_number") or ""),
                    str(fields.get("sender") or ""),
                    str(fields.get("recipient") or ""),
                    str(fields.get("subject") or ""),
                    str(fields.get("matter_id") or ""),
                    json.dumps(record, ensure_ascii=False),
                ))
            conn.executemany(
                "INSERT INTO homogeneous_records("
                "scan_id,node_path,document_date,document_number,sender,recipient,"
                "subject,matter_id,payload,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                record_rows,
            )
            conn.executemany(
                "INSERT INTO homogeneous_relations("
                "scan_id,relation_id,source_path,target_path,relation_type,confidence,"
                "payload,updated_at) VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                [(
                    scan_id, str(item.get("relation_id") or ""),
                    str(item.get("source_path") or ""),
                    str(item.get("target_path") or ""),
                    str(item.get("relation_type") or "related"),
                    float(item.get("confidence") or 0),
                    json.dumps(item, ensure_ascii=False),
                ) for item in relations],
            )
            conn.executemany(
                "INSERT INTO homogeneous_cases("
                "scan_id,case_id,title,document_count,start_date,end_date,payload,"
                "updated_at) VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                [(
                    scan_id, str(item.get("case_id") or ""),
                    str(item.get("title") or "未命名事项"),
                    int(item.get("document_count") or 0),
                    str(item.get("start_date") or ""), str(item.get("end_date") or ""),
                    json.dumps(item, ensure_ascii=False),
                ) for item in cases],
            )
            conn.executemany(
                "INSERT INTO homogeneous_anomalies("
                "scan_id,position,anomaly_type,node_path,severity,payload,updated_at) "
                "VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                [(
                    scan_id, position, str(item.get("type") or "unknown"),
                    str(item.get("path") or ""), str(item.get("severity") or "low"),
                    json.dumps(item, ensure_ascii=False),
                ) for position, item in enumerate(anomalies)],
            )
            conn.execute("DELETE FROM relationship_catalog WHERE scan_id=?", (scan_id,))
            conn.execute("DELETE FROM package_overviews WHERE scan_id=?", (scan_id,))
        return {
            "records": len(records), "relations": len(relations),
            "cases": len(cases), "anomalies": len(anomalies),
        }

    def get_homogeneous_analysis(
        self, scan_id, offset=0, limit=100, query="", relation_type="",
        relation_limit=500, case_limit=100, anomaly_limit=200,
    ):
        offset = max(0, int(offset or 0))
        limit = max(1, min(500, int(limit or 100)))
        relation_limit = max(1, min(2000, int(relation_limit or 500)))
        case_limit = max(1, min(500, int(case_limit or 100)))
        anomaly_limit = max(1, min(1000, int(anomaly_limit or 200)))
        query = str(query or "").strip()[:200]
        relation_type = str(relation_type or "").strip()[:40]
        scan_id = str(scan_id)
        with self._connect() as conn:
            analysis_row = conn.execute(
                "SELECT payload,updated_at FROM homogeneous_analyses WHERE scan_id=?",
                (scan_id,),
            ).fetchone()
            if not analysis_row:
                return None
            summary = json.loads(analysis_row["payload"])
            summary["updated_at"] = analysis_row["updated_at"]
            clauses, values = ["scan_id=?"], [scan_id]
            if query:
                clauses.append(
                    "(node_path LIKE ? OR document_number LIKE ? OR sender LIKE ? "
                    "OR recipient LIKE ? OR subject LIKE ? OR matter_id LIKE ?)"
                )
                values.extend(["%{}%".format(query)] * 6)
            where = " AND ".join(clauses)
            total = int(conn.execute(
                "SELECT COUNT(*) AS value FROM homogeneous_records WHERE {}".format(where),
                values,
            ).fetchone()["value"])
            record_rows = conn.execute(
                "SELECT payload FROM homogeneous_records WHERE {} "
                "ORDER BY CASE WHEN document_date='' THEN 1 ELSE 0 END,"
                "document_date,node_path LIMIT ? OFFSET ?".format(where),
                values + [limit, offset],
            ).fetchall()
            relation_clauses, relation_values = ["scan_id=?"], [scan_id]
            if relation_type:
                relation_clauses.append("relation_type=?")
                relation_values.append(relation_type)
            relation_rows = conn.execute(
                "SELECT payload FROM homogeneous_relations WHERE {} "
                "ORDER BY confidence DESC,relation_id LIMIT ?".format(
                    " AND ".join(relation_clauses)
                ), relation_values + [relation_limit],
            ).fetchall()
            case_rows = conn.execute(
                "SELECT payload FROM homogeneous_cases WHERE scan_id=? "
                "ORDER BY document_count DESC,title LIMIT ?",
                (scan_id, case_limit),
            ).fetchall()
            anomaly_rows = conn.execute(
                "SELECT payload FROM homogeneous_anomalies WHERE scan_id=? "
                "ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,"
                "position LIMIT ?", (scan_id, anomaly_limit),
            ).fetchall()
        records = [json.loads(row["payload"]) for row in record_rows]
        return {
            "summary": summary,
            "records": {
                "items": records, "offset": offset, "limit": limit, "total": total,
                "next_offset": offset + len(records) if offset + len(records) < total else None,
            },
            "relations": [json.loads(row["payload"]) for row in relation_rows],
            "cases": [json.loads(row["payload"]) for row in case_rows],
            "anomalies": [json.loads(row["payload"]) for row in anomaly_rows],
        }

    def get_homogeneous_record(self, scan_id, node_path):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM homogeneous_records WHERE scan_id=? AND node_path=?",
                (str(scan_id), str(node_path)),
            ).fetchone()
            if not row:
                return None
            relations = conn.execute(
                "SELECT payload FROM homogeneous_relations WHERE scan_id=? "
                "AND (source_path=? OR target_path=?) ORDER BY confidence DESC LIMIT 500",
                (str(scan_id), str(node_path), str(node_path)),
            ).fetchall()
        return {
            "record": json.loads(row["payload"]),
            "relations": [json.loads(item["payload"]) for item in relations],
        }

    def list_conversations(self, scan_id, owner_id, limit=50):
        limit = max(1, min(200, int(limit or 50)))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT c.id,c.title,c.status,c.payload,c.created_at,c.updated_at,"
                "(SELECT COUNT(*) FROM conversation_messages m WHERE m.session_id=c.id) AS message_count "
                "FROM conversations c "
                "WHERE scan_id=? AND owner_id=? ORDER BY updated_at DESC LIMIT ?",
                (str(scan_id), str(owner_id), limit),
            ).fetchall()
        items = []
        for row in rows:
            payload = json.loads(row["payload"])
            items.append({
                "session_id": row["id"], "title": row["title"], "status": row["status"],
                "scope": payload.get("scope"),
                "message_count": int(row["message_count"] or len(payload.get("messages") or [])),
                "created_at": row["created_at"], "updated_at": row["updated_at"],
            })
        return items

    @staticmethod
    def _decode_conversation_turn(row):
        if not row:
            return None
        result = dict(row)
        for key in ("scope", "plan", "result", "verification"):
            try:
                result[key] = json.loads(result[key]) if result.get(key) else None
            except (TypeError, ValueError, json.JSONDecodeError):
                result[key] = None
        return result

    @staticmethod
    def _conversation_timestamp():
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def create_conversation_turn(self, session_id, scan_id, owner_id, question,
                                 scope, persist_scope=False, idempotency_key=None):
        """Atomically append one user turn, placeholder and durable Worker job."""
        session_id = str(session_id or "")
        scan_id = str(scan_id or "")
        owner_id = str(owner_id or "legacy")
        question = str(question or "").strip()
        idempotency_key = str(idempotency_key or "").strip()[:200] or None
        if not session_id or not scan_id or not question:
            raise ValueError("分析轮次缺少会话、数据包或问题")
        scope = dict(scope or {"kind": "package"})
        created_at = self._conversation_timestamp()
        with self.lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conversation = conn.execute(
                "SELECT payload FROM conversations WHERE id=? AND scan_id=? AND owner_id=?",
                (session_id, scan_id, owner_id),
            ).fetchone()
            if not conversation:
                raise ValueError("会话不存在")
            if idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM conversation_turns WHERE session_id=? AND idempotency_key=?",
                    (session_id, idempotency_key),
                ).fetchone()
                if existing:
                    return self._decode_conversation_turn(existing), False
            sequence = int(conn.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 AS value FROM conversation_turns WHERE session_id=?",
                (session_id,),
            ).fetchone()["value"])
            message_sequence = int(conn.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 AS value FROM conversation_messages WHERE session_id=?",
                (session_id,),
            ).fetchone()["value"])
            turn_id = uuid.uuid4().hex
            user_message_id = uuid.uuid4().hex[:16]
            assistant_message_id = uuid.uuid4().hex[:16]
            job_id = uuid.uuid4().hex[:12]
            user_message = {
                "message_id": user_message_id,
                "role": "user",
                "content": question,
                "created_at": created_at,
                "intent": None,
                "resolved_query": None,
                "evidence_ids": [],
                "metadata": {"turn_id": turn_id},
            }
            assistant_message = {
                "message_id": assistant_message_id,
                "role": "assistant",
                "content": "分析任务已进入队列，正在理解你的指令。",
                "created_at": created_at,
                "intent": "analysis",
                "resolved_query": question,
                "evidence_ids": [],
                "metadata": {
                    "turn_id": turn_id,
                    "status": "queued",
                    "stage": "queued",
                    "progress": 0,
                },
            }
            conn.execute(
                "INSERT INTO conversation_turns("
                "id,session_id,scan_id,owner_id,sequence,idempotency_key,status,stage,progress,"
                "question,scope,job_id,user_message_id,assistant_message_id"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    turn_id, session_id, scan_id, owner_id, sequence, idempotency_key,
                    "queued", "queued", 0, question,
                    json.dumps(scope, ensure_ascii=False), job_id,
                    user_message_id, assistant_message_id,
                ),
            )
            for offset, message in enumerate((user_message, assistant_message)):
                conn.execute(
                    "INSERT INTO conversation_messages("
                    "session_id,message_id,role,turn_id,sequence,payload,updated_at"
                    ") VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                    (
                        session_id, message["message_id"], message["role"], turn_id,
                        message_sequence + offset,
                        json.dumps(message, ensure_ascii=False),
                    ),
                )
            session_payload = json.loads(conversation["payload"])
            if persist_scope:
                session_payload["scope"] = scope
            session_payload["updated_at"] = created_at
            conn.execute(
                "UPDATE conversations SET payload=?,revision=revision+1,updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND scan_id=? AND owner_id=?",
                (json.dumps(session_payload, ensure_ascii=False), session_id, scan_id, owner_id),
            )
            options = json.dumps({"turn_id": turn_id}, ensure_ascii=False)
            conn.execute(
                "INSERT INTO analysis_jobs("
                "id,scan_id,task_type,status,stage,progress,message,options,owner_id,priority,created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (
                    job_id, scan_id, "conversation_turn", "queued", "queued", 0,
                    "等待执行交互式分析", options, owner_id,
                    self._job_priority("conversation_turn", {"turn_id": turn_id}),
                ),
            )
            conn.execute(
                "INSERT INTO conversation_turn_events("
                "turn_id,event_type,stage,progress,message,payload"
                ") VALUES (?,?,?,?,?,?)",
                (turn_id, "status", "queued", 0, "分析任务已进入队列", "{}"),
            )
            stored = conn.execute(
                "SELECT * FROM conversation_turns WHERE id=?", (turn_id,)
            ).fetchone()
        return self._decode_conversation_turn(stored), True

    def get_conversation_turn(self, turn_id, owner_id=None):
        clauses = ["id=?"]
        values = [str(turn_id)]
        if owner_id:
            clauses.append("owner_id=?")
            values.append(str(owner_id))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_turns WHERE {}".format(" AND ".join(clauses)),
                values,
            ).fetchone()
        return self._decode_conversation_turn(row)

    def list_conversation_turns(self, session_id, owner_id, limit=100):
        limit = max(1, min(500, int(limit or 100)))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM conversation_turns WHERE session_id=? AND owner_id=? "
                "ORDER BY sequence LIMIT ?",
                (str(session_id), str(owner_id), limit),
            ).fetchall()
        return [self._decode_conversation_turn(row) for row in rows]

    def update_conversation_turn(self, turn_id, status=None, stage=None, progress=None,
                                 message=None, plan=None, result=None, verification=None,
                                 error=None, job_id=None, promotion_job_id=None,
                                 continuation_depth=None, event_type="status"):
        fields = ["updated_at=CURRENT_TIMESTAMP"]
        values = []
        updates = {
            "status": status, "stage": stage, "error": error, "job_id": job_id,
            "promotion_job_id": promotion_job_id,
            "continuation_depth": continuation_depth,
        }
        for name, value in updates.items():
            if value is not None:
                fields.append("{}=?".format(name))
                values.append(value)
        if progress is not None:
            fields.append("progress=?")
            values.append(max(0, min(100, int(progress))))
        for name, value in (("plan", plan), ("result", result), ("verification", verification)):
            if value is not None:
                fields.append("{}=?".format(name))
                values.append(json.dumps(value, ensure_ascii=False))
        if status in {"completed", "failed", "cancelled"}:
            fields.append("finished_at=CURRENT_TIMESTAMP")
        values.append(str(turn_id))
        with self.lock, self._connect() as conn:
            conn.execute(
                "UPDATE conversation_turns SET {} WHERE id=?".format(",".join(fields)),
                values,
            )
            row = conn.execute(
                "SELECT * FROM conversation_turns WHERE id=?", (str(turn_id),)
            ).fetchone()
            if row and message is not None:
                conn.execute(
                    "INSERT INTO conversation_turn_events("
                    "turn_id,event_type,stage,progress,message,payload"
                    ") VALUES (?,?,?,?,?,?)",
                    (
                        str(turn_id), str(event_type or "status"),
                        stage or row["stage"],
                        max(0, min(100, int(progress if progress is not None else row["progress"] or 0))),
                        str(message), "{}",
                    ),
                )
        return self._decode_conversation_turn(row)

    def replace_conversation_turn_steps(self, turn_id, steps):
        with self.lock, self._connect() as conn:
            conn.execute("DELETE FROM conversation_analysis_steps WHERE turn_id=?", (str(turn_id),))
            for position, step in enumerate(steps or [], 1):
                conn.execute(
                    "INSERT INTO conversation_analysis_steps("
                    "turn_id,step_id,position,tool,action,status,progress,payload"
                    ") VALUES (?,?,?,?,?,?,?,?)",
                    (
                        str(turn_id), str(step.get("step_id") or "step-{}".format(position)),
                        position, str(step.get("tool") or "evidence_search"),
                        str(step.get("action") or ""), str(step.get("status") or "pending"),
                        int(step.get("progress") or 0),
                        json.dumps(step.get("payload") or {}, ensure_ascii=False),
                    ),
                )

    def update_conversation_turn_step(self, turn_id, step_id, status, progress=0,
                                      payload=None, error=None):
        terminal = str(status) in {"completed", "failed", "cancelled", "skipped"}
        with self.lock, self._connect() as conn:
            conn.execute(
                "UPDATE conversation_analysis_steps SET status=?,progress=?,payload=?,error=?,"
                "started_at=CASE WHEN started_at IS NULL THEN CURRENT_TIMESTAMP ELSE started_at END,"
                "finished_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE finished_at END,"
                "updated_at=CURRENT_TIMESTAMP WHERE turn_id=? AND step_id=?",
                (
                    str(status), max(0, min(100, int(progress or 0))),
                    json.dumps(payload or {}, ensure_ascii=False), error,
                    1 if terminal else 0, str(turn_id), str(step_id),
                ),
            )

    def list_conversation_turn_steps(self, turn_id):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM conversation_analysis_steps WHERE turn_id=? ORDER BY position",
                (str(turn_id),),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.get("payload") or "{}")
            output.append(item)
        return output

    def get_conversation_turn_events(self, turn_id, owner_id, after=0, limit=200):
        limit = max(1, min(500, int(limit or 200)))
        with self._connect() as conn:
            owned = conn.execute(
                "SELECT 1 FROM conversation_turns WHERE id=? AND owner_id=?",
                (str(turn_id), str(owner_id)),
            ).fetchone()
            if not owned:
                return None
            rows = conn.execute(
                "SELECT * FROM conversation_turn_events WHERE turn_id=? AND event_id>? "
                "ORDER BY event_id LIMIT ?",
                (str(turn_id), max(0, int(after or 0)), limit),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.get("payload") or "{}")
            output.append(item)
        return output

    def complete_conversation_turn(self, turn_id, turn_result, verification, claims,
                                   session_state=None):
        turn_result = dict(turn_result or {})
        citations = list(turn_result.get("citations") or [])
        claims = list(claims or [])
        with self.lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM conversation_turns WHERE id=?", (str(turn_id),)
            ).fetchone()
            if not row:
                raise ValueError("分析轮次不存在")
            conn.execute("DELETE FROM conversation_turn_evidence WHERE turn_id=?", (str(turn_id),))
            for index, citation in enumerate(citations, 1):
                evidence_id = str(citation.get("evidence_id") or "citation-{}".format(index))
                conn.execute(
                    "INSERT INTO conversation_turn_evidence("
                    "turn_id,evidence_id,citation_index,source_path,payload"
                    ") VALUES (?,?,?,?,?)",
                    (
                        str(turn_id), evidence_id, index,
                        str(citation.get("source_path") or "未知来源"),
                        json.dumps(citation, ensure_ascii=False),
                    ),
                )
            conn.execute("DELETE FROM conversation_claims WHERE turn_id=?", (str(turn_id),))
            for position, claim in enumerate(claims, 1):
                conn.execute(
                    "INSERT INTO conversation_claims("
                    "turn_id,claim_id,position,text,status,evidence_ids,payload"
                    ") VALUES (?,?,?,?,?,?,?)",
                    (
                        str(turn_id), str(claim.get("claim_id") or "claim-{}".format(position)),
                        position, str(claim.get("text") or ""),
                        str(claim.get("status") or "unverified"),
                        json.dumps(claim.get("evidence_ids") or [], ensure_ascii=False),
                        json.dumps(claim, ensure_ascii=False),
                    ),
                )
            conn.execute(
                "UPDATE conversation_turns SET status='completed',stage='completed',progress=100,"
                "result=?,verification=?,error=NULL,finished_at=CURRENT_TIMESTAMP,"
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (
                    json.dumps(turn_result, ensure_ascii=False),
                    json.dumps(verification or {}, ensure_ascii=False), str(turn_id),
                ),
            )
            message_row = conn.execute(
                "SELECT payload FROM conversation_messages WHERE session_id=? AND message_id=?",
                (row["session_id"], row["assistant_message_id"]),
            ).fetchone()
            message_payload = json.loads(message_row["payload"]) if message_row else {}
            message_payload.update({
                "message_id": row["assistant_message_id"],
                "role": "assistant",
                "content": str(turn_result.get("answer") or "分析已完成。"),
                "intent": (turn_result.get("intent") or {}).get("name") or "analysis",
                "resolved_query": turn_result.get("resolved_query") or row["question"],
                "evidence_ids": [
                    str(item.get("evidence_id")) for item in citations if item.get("evidence_id")
                ],
                "metadata": {
                    "turn_id": str(turn_id), "status": "completed", "stage": "completed",
                    "progress": 100, "evidence_status": turn_result.get("evidence_status"),
                    "verification": verification or {},
                },
            })
            conn.execute(
                "UPDATE conversation_messages SET role='assistant',payload=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE session_id=? AND message_id=?",
                (
                    json.dumps(message_payload, ensure_ascii=False),
                    row["session_id"], row["assistant_message_id"],
                ),
            )
            if session_state:
                conversation_row = conn.execute(
                    "SELECT payload FROM conversations WHERE id=?",
                    (row["session_id"],),
                ).fetchone()
                conversation_payload = json.loads(conversation_row["payload"])
                for key in (
                    "scope", "rolling_summary", "summarized_message_count",
                    "updated_at", "title", "status",
                ):
                    if key in session_state:
                        conversation_payload[key] = session_state[key]
                # Full messages remain in conversation_messages as the audit log.
                conversation_payload["messages"] = []
                conn.execute(
                    "UPDATE conversations SET payload=?,revision=revision+1,"
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (
                        json.dumps(conversation_payload, ensure_ascii=False),
                        row["session_id"],
                    ),
                )
            else:
                conn.execute(
                    "UPDATE conversations SET revision=revision+1,"
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (row["session_id"],),
                )
            conn.execute(
                "INSERT INTO conversation_turn_events("
                "turn_id,event_type,stage,progress,message,payload"
                ") VALUES (?,?,?,?,?,?)",
                (str(turn_id), "completed", "completed", 100, "分析完成", "{}"),
            )
            stored = conn.execute(
                "SELECT * FROM conversation_turns WHERE id=?", (str(turn_id),)
            ).fetchone()
        return self._decode_conversation_turn(stored)

    def set_conversation_turn_message(self, turn_id, content, status, stage,
                                      progress, error=None):
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT session_id,assistant_message_id FROM conversation_turns WHERE id=?",
                (str(turn_id),),
            ).fetchone()
            if not row:
                return False
            message_row = conn.execute(
                "SELECT payload FROM conversation_messages WHERE session_id=? AND message_id=?",
                (row["session_id"], row["assistant_message_id"]),
            ).fetchone()
            payload = json.loads(message_row["payload"]) if message_row else {}
            payload["content"] = str(content or "")
            metadata = dict(payload.get("metadata") or {})
            metadata.update({
                "turn_id": str(turn_id), "status": str(status), "stage": str(stage),
                "progress": max(0, min(100, int(progress or 0))),
            })
            if error:
                metadata["error"] = str(error)
            payload["metadata"] = metadata
            conn.execute(
                "UPDATE conversation_messages SET payload=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE session_id=? AND message_id=?",
                (json.dumps(payload, ensure_ascii=False), row["session_id"], row["assistant_message_id"]),
            )
            conn.execute(
                "UPDATE conversations SET revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (row["session_id"],),
            )
        return True

    def replace_conversation_turn_job(self, turn_id, owner_id, message="等待继续分析",
                                      force=False):
        """Queue the same logical turn again without appending another question."""
        with self.lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM conversation_turns WHERE id=? AND owner_id=?",
                (str(turn_id), str(owner_id)),
            ).fetchone()
            if not row:
                return None
            if row["status"] == "completed" or (row["status"] == "cancelled" and not force):
                return None
            active = conn.execute(
                "SELECT id FROM analysis_jobs WHERE id=? AND status IN ('queued','running','cancelling')",
                (row["job_id"],),
            ).fetchone()
            if active:
                return active["id"]
            job_id = uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO analysis_jobs("
                "id,scan_id,task_type,status,stage,progress,message,options,owner_id,priority,created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (
                    job_id, row["scan_id"], "conversation_turn", "queued", "queued", 0,
                    str(message), json.dumps({"turn_id": str(turn_id)}, ensure_ascii=False),
                    str(owner_id), self._job_priority("conversation_turn", {"turn_id": str(turn_id)}),
                ),
            )
            conn.execute(
                "UPDATE conversation_turns SET status='queued',stage='queued',progress=0,job_id=?,"
                "promotion_job_id=NULL,error=NULL,finished_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (job_id, str(turn_id)),
            )
            conn.execute(
                "INSERT INTO conversation_turn_events("
                "turn_id,event_type,stage,progress,message,payload"
                ") VALUES (?,?,?,?,?,?)",
                (str(turn_id), "resumed", "queued", 0, str(message), "{}"),
            )
        self.set_conversation_turn_message(
            turn_id, "补充证据已准备完成，正在继续原分析任务。", "queued", "queued", 0
        )
        return job_id

    def get_conversation_research_memory(self, session_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT revision,payload,updated_at FROM conversation_research_memory WHERE session_id=?",
                (str(session_id),),
            ).fetchone()
        if not row:
            return {"revision": 0, "payload": {}}
        return {
            "revision": int(row["revision"] or 0),
            "payload": json.loads(row["payload"] or "{}"),
            "updated_at": row["updated_at"],
        }

    def save_conversation_research_memory(self, session_id, payload):
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO conversation_research_memory(session_id,revision,payload) VALUES (?,1,?) "
                "ON CONFLICT(session_id) DO UPDATE SET revision=revision+1,payload=excluded.payload,"
                "updated_at=CURRENT_TIMESTAMP",
                (str(session_id), json.dumps(payload or {}, ensure_ascii=False)),
            )
        return self.get_conversation_research_memory(session_id)

    def set_file_state(self, scan_id, node_path, fingerprint, status, document=None,
                       error=None, error_class=None, retryable=False,
                       next_retry_at=None):
        document = document or {}
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO file_analysis_states("
                "scan_id,node_path,fingerprint,status,parser,stored_characters,evidence_count,error,"
                "error_class,retryable,attempt_count,next_retry_at,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(scan_id,node_path) DO UPDATE SET "
                "fingerprint=excluded.fingerprint,status=excluded.status,parser=excluded.parser,"
                "stored_characters=excluded.stored_characters,evidence_count=excluded.evidence_count,"
                "error=excluded.error,error_class=excluded.error_class,retryable=excluded.retryable,"
                "attempt_count=file_analysis_states.attempt_count+excluded.attempt_count,"
                "next_retry_at=excluded.next_retry_at,updated_at=CURRENT_TIMESTAMP",
                (
                    scan_id, node_path, fingerprint, status,
                    (document.get("parser") or {}).get("name"),
                    len(document.get("text") or ""),
                    len(document.get("evidence") or []),
                    error, error_class, int(bool(retryable)),
                    1 if str(status) == "failed" else 0, next_retry_at,
                ),
            )

    def set_file_states(self, scan_id, states):
        rows = []
        for node_path, fingerprint, status, document, error in states or []:
            document = document or {}
            rows.append((
                str(scan_id), str(node_path), str(fingerprint or ""), str(status),
                (document.get("parser") or {}).get("name"),
                len(document.get("text") or ""),
                len(document.get("evidence") or []),
                str(error)[:2000] if error else None,
            ))
        if not rows:
            return 0
        with self.lock, self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO file_analysis_states("
                "scan_id,node_path,fingerprint,status,parser,stored_characters,evidence_count,error,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                rows,
            )
        return len(rows)

    def get_file_state(self, scan_id, node_path):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM file_analysis_states WHERE scan_id=? AND node_path=?",
                (scan_id, node_path),
            ).fetchone()
        return dict(row) if row else None

    def get_file_states(self, scan_id, node_paths):
        """Return status rows for a bounded path set without one query per hit."""
        paths = list(dict.fromkeys(str(path) for path in (node_paths or []) if path))
        output = {}
        with self._connect() as conn:
            for start in range(0, len(paths), 800):
                batch = paths[start:start + 800]
                if not batch:
                    continue
                rows = conn.execute(
                    "SELECT * FROM file_analysis_states WHERE scan_id=? AND node_path IN ({})".format(
                        ",".join("?" for _ in batch)
                    ),
                    [str(scan_id)] + batch,
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    output[item["node_path"]] = item
        return output

    def request_file_reanalysis(self, scan_id, node_paths, reason=None):
        """Explicitly re-open retryable work without silently losing history.

        Automatic retries retain the original attempt budget. A user asking to
        retry a known file is a new operator decision, so it receives a fresh
        bounded retry budget and is returned to the durable queue. Completed
        files are intentionally excluded here; a separate reanalysis action
        should be used when the user wants to invalidate a healthy result.
        """
        paths = list(dict.fromkeys(
            str(path) for path in (node_paths or []) if str(path).strip()
        ))
        if not paths:
            return 0
        note = str(reason or "用户请求重新分析").strip()[:1800]
        updated = 0
        with self.lock, self._connect() as conn:
            for start in range(0, len(paths), 500):
                batch = paths[start:start + 500]
                placeholders = ",".join("?" for _ in batch)
                eligible = conn.execute(
                    "SELECT node_path FROM file_analysis_states WHERE scan_id=? "
                    "AND node_path IN ({}) AND status IN "
                    "('failed','needs_attention','overview','preview_failed','preview_deferred')".format(
                        placeholders
                    ),
                    [str(scan_id)] + batch,
                ).fetchall()
                retry_paths = [str(row["node_path"]) for row in eligible]
                if not retry_paths:
                    continue
                conn.executemany(
                    "UPDATE file_analysis_states SET status='failed',retryable=1,"
                    "attempt_count=0,next_retry_at=0,error=?,error_class='manual_retry',"
                    "updated_at=CURRENT_TIMESTAMP WHERE scan_id=? AND node_path=?",
                    [(note, str(scan_id), path) for path in retry_paths],
                )
                conn.executemany(
                    "UPDATE file_workflow_states SET workflow_state='manual_retry_queued',"
                    "selection_state='priority',parse_status='queued',evidence_status='pending',"
                    "priority_source='manual_retry',updated_at=CURRENT_TIMESTAMP "
                    "WHERE scan_id=? AND node_path=?",
                    [(str(scan_id), path) for path in retry_paths],
                )
                updated += len(retry_paths)
        return updated

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

    def save_file_workflow_states(self, scan_id, states):
        rows = []
        for state in states or []:
            state = dict(state or {})
            node_path = str(state.get("path") or state.get("node_path") or "")
            if not node_path:
                continue
            rows.append((
                str(scan_id), node_path,
                str(state.get("workflow_state") or "discovered"),
                str(state.get("selection_state") or "pending"),
                float(state.get("score") or state.get("selection_score") or 0.0),
                json.dumps(state.get("score_components") or {}, ensure_ascii=False),
                json.dumps(list(state.get("reasons") or []), ensure_ascii=False),
                str(state.get("safety_status") or "unknown"),
                str(state.get("light_index_status") or "pending"),
                str(state.get("language_code") or "unknown"),
                1 if state.get("ocr_candidate") else 0,
                str(state.get("parse_status") or "pending"),
                str(state.get("evidence_status") or "pending"),
                1 if state.get("promotion_allowed", True) else 0,
                str(state.get("priority_source") or "") or None,
            ))
        if not rows:
            return 0
        with self.lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO file_workflow_states("
                "scan_id,node_path,workflow_state,selection_state,selection_score,"
                "score_components,reasons,safety_status,light_index_status,language_code,"
                "ocr_candidate,parse_status,evidence_status,promotion_allowed,priority_source,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(scan_id,node_path) DO UPDATE SET "
                "workflow_state=excluded.workflow_state,selection_state=excluded.selection_state,"
                "selection_score=excluded.selection_score,score_components=excluded.score_components,"
                "reasons=excluded.reasons,safety_status=excluded.safety_status,"
                "light_index_status=excluded.light_index_status,language_code=excluded.language_code,"
                "ocr_candidate=excluded.ocr_candidate,promotion_allowed=excluded.promotion_allowed,"
                "updated_at=CURRENT_TIMESTAMP",
                rows,
            )
        return len(rows)

    @staticmethod
    def _decode_file_workflow_state(row):
        item = dict(row)
        item["score_components"] = json.loads(item.get("score_components") or "{}")
        item["reasons"] = json.loads(item.get("reasons") or "[]")
        item["ocr_candidate"] = bool(item.get("ocr_candidate"))
        item["promotion_allowed"] = bool(item.get("promotion_allowed"))
        return item

    def get_file_workflow_state(self, scan_id, node_path):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM file_workflow_states WHERE scan_id=? AND node_path=?",
                (str(scan_id), str(node_path)),
            ).fetchone()
        return self._decode_file_workflow_state(row) if row else None

    def iter_file_workflow_states(self, scan_id, selection_states=None, batch_size=500):
        batch_size = max(1, min(5000, int(batch_size or 500)))
        values = [str(scan_id)]
        where = "scan_id=?"
        selection_states = [str(value) for value in (selection_states or []) if value]
        if selection_states:
            where += " AND selection_state IN ({})".format(
                ",".join("?" for _ in selection_states)
            )
            values.extend(selection_states)
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM file_workflow_states WHERE {} "
                "ORDER BY selection_score DESC,node_path".format(where), values,
            )
            try:
                while True:
                    rows = cursor.fetchmany(batch_size)
                    if not rows:
                        break
                    for row in rows:
                        yield self._decode_file_workflow_state(row)
            finally:
                cursor.close()

    def list_file_workflow_states_page(self, scan_id, offset=0, limit=100,
                                       selection_state=None):
        offset = max(0, int(offset or 0))
        limit = max(1, min(500, int(limit or 100)))
        values = [str(scan_id)]
        where = "scan_id=?"
        if selection_state:
            where += " AND selection_state=?"
            values.append(str(selection_state))
        with self._connect() as conn:
            total = int(conn.execute(
                "SELECT COUNT(*) FROM file_workflow_states WHERE {}".format(where),
                values,
            ).fetchone()[0])
            rows = conn.execute(
                "SELECT * FROM file_workflow_states WHERE {} "
                "ORDER BY selection_score DESC,node_path LIMIT ? OFFSET ?".format(where),
                values + [limit, offset],
            ).fetchall()
        return {
            "items": [self._decode_file_workflow_state(row) for row in rows],
            "offset": offset, "limit": limit, "total": total,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None,
        }

    @staticmethod
    def _json_object(value):
        """Decode a persisted object without allowing one bad legacy row to
        make the operational status screen unavailable.

        The status projection intentionally reads only compact metadata
        columns.  It must remain usable for multi-gigabyte sidecar documents
        and for scans created by older releases.
        """
        if isinstance(value, dict):
            return dict(value)
        try:
            decoded = json.loads(value or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    @classmethod
    def _file_status_item(cls, row, now=None):
        """Project independent persistence records into one user-facing state.

        ``file_workflow_states`` describes scheduling, ``file_analysis_states``
        describes deep parsing, previews make the full inventory searchable,
        and translations have their own durable lifecycle.  Rendering any one
        of those in isolation previously made a file look complete when it was
        only previewed, waiting for retry, or deliberately outside scope.
        """
        raw = dict(row)
        now = time.time() if now is None else float(now)
        inventory = cls._json_object(raw.pop("inventory_payload", None))
        reasons = cls._json_object(raw.pop("workflow_reasons", None))
        # ``reasons`` is stored as an array rather than object.  Keep the
        # tolerant decoder above for old rows, then decode the normal shape.
        try:
            reason_values = json.loads(raw.get("workflow_reasons_json") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            reason_values = []
        if not isinstance(reason_values, list):
            reason_values = []
        # A malformed historical array can be represented by the fallback
        # object, but never let it leak implementation details to the UI.
        if not reason_values and isinstance(reasons.get("items"), list):
            reason_values = reasons["items"]
        reason_values = [str(value) for value in reason_values if str(value).strip()][:20]

        analysis_status = str(raw.get("analysis_status") or "").strip().lower()
        workflow_state = str(raw.get("workflow_state") or "").strip().lower()
        parse_status = str(raw.get("parse_status") or "").strip().lower()
        selection_state = str(raw.get("selection_state") or "").strip().lower()
        preview_status = str(raw.get("preview_status") or "").strip().lower()
        retryable = bool(raw.get("retryable"))
        attempts = max(0, int(raw.get("attempt_count") or 0))
        next_retry_at = raw.get("next_retry_at")
        try:
            next_retry_at = float(next_retry_at) if next_retry_at is not None else None
        except (TypeError, ValueError):
            next_retry_at = None
        retry_waiting = bool(
            analysis_status == "failed" and retryable and attempts < 6
            and next_retry_at is not None and next_retry_at > now
        )
        logical_container = workflow_state == "logical_container" or (
            "logical_container_replaced_by_children" in reason_values
        )
        policy_excluded = bool(
            raw.get("promotion_allowed") == 0
            or str(raw.get("safety_status") or "").lower() in {"restricted", "rejected"}
            or analysis_status == "out_of_scope"
        )

        if analysis_status == "completed" or (
            not analysis_status and parse_status == "completed"
        ):
            display_status = "completed"
            coverage_ratio = 1.0
            coverage_known = True
        elif retry_waiting:
            display_status = "retry_waiting"
            coverage_ratio = None
            coverage_known = False
        elif analysis_status == "failed":
            display_status = "failed"
            coverage_ratio = None
            coverage_known = False
        elif logical_container:
            # The physical container is retained for auditability, while its
            # independently schedulable members carry the real deep-analysis
            # coverage.  It must not be presented as a terminal exclusion.
            display_status = "partial"
            coverage_ratio = None
            coverage_known = False
        elif policy_excluded:
            display_status = "out_of_scope"
            coverage_ratio = 0.0
            coverage_known = True
        elif analysis_status in {
            "needs_attention", "overview", "previewed", "preview_failed",
            "preview_deferred",
        } or parse_status in {"partial", "source_unavailable"} or preview_status in {
            "previewed", "restricted", "deferred", "failed",
        }:
            display_status = "partial"
            coverage_ratio = None
            coverage_known = False
        elif parse_status in {"processing", "parsing", "running"} or workflow_state in {
            "deep_parsing", "deep_parse_running", "processing",
        }:
            display_status = "processing"
            coverage_ratio = 0.0
            coverage_known = True
        else:
            display_status = "pending"
            coverage_ratio = 0.0
            coverage_known = True

        node_path = str(raw.get("node_path") or "")
        container_path = str(
            inventory.get("container_path") or raw.get("container_path") or ""
        )
        original_path = container_path or node_path.split("::", 1)[0]
        message = str(raw.get("analysis_error") or "").strip()
        if not message:
            if display_status == "completed":
                message = "已完成深度解析并建立可回查证据"
            elif display_status == "retry_waiting":
                message = "解析失败后等待自动重试"
            elif display_status == "failed":
                message = "解析失败，需要人工重试或检查文件"
            elif display_status == "out_of_scope":
                message = "当前处理范围外或受安全策略限制"
            elif logical_container:
                message = "容器已拆分为独立逻辑文件；请查看其成员状态"
            elif reason_values:
                message = reason_values[0]
            elif display_status == "partial":
                message = "已有基础内容或部分结果，尚未完成完整深析"
            elif display_status == "processing":
                message = "正在执行深度解析"
            else:
                message = "等待进入深度处理队列"

        return {
            "node_path": node_path,
            "path": node_path,
            "name": str(inventory.get("name") or Path(node_path).name or node_path),
            "extension": str(inventory.get("extension") or Path(original_path).suffix).lower(),
            "size": int(inventory.get("size") or raw.get("source_size") or 0),
            "inventory_kind": raw.get("inventory_kind") or "file",
            "logical": bool(raw.get("inventory_kind") == "logical_file" or inventory.get("logical_unit")),
            "logical_kind": inventory.get("logical_kind"),
            "container_path": container_path or None,
            "original_path": original_path,
            "display_status": display_status,
            "status": display_status,
            "reason": message[:2000],
            "workflow_state": raw.get("workflow_state") or "discovered",
            "selection_state": raw.get("selection_state") or "pending",
            "selection_score": float(raw.get("selection_score") or 0.0),
            "priority_source": raw.get("priority_source"),
            "workflow_reasons": reason_values,
            "safety_status": raw.get("safety_status") or "unknown",
            "promotion_allowed": bool(raw.get("promotion_allowed", 1)),
            "light_index_status": raw.get("light_index_status") or "pending",
            "preview_status": raw.get("preview_status") or None,
            "analysis_status": raw.get("analysis_status") or None,
            "parse_status": raw.get("parse_status") or "pending",
            "evidence_status": raw.get("evidence_status") or "pending",
            "parser": raw.get("parser") or None,
            "attempt_count": attempts,
            "retryable": retryable,
            "next_retry_at": next_retry_at,
            "error_class": raw.get("error_class") or None,
            "analysis_level": (
                "deep" if display_status == "completed" else
                "preview" if preview_status in {"previewed", "restricted"} or analysis_status in {"overview", "previewed"}
                else "metadata"
            ),
            "coverage_ratio": coverage_ratio,
            "coverage_known": coverage_known,
            "translation_status": raw.get("translation_status") or "not_started",
            "translation_source_language": raw.get("translation_source_language") or None,
            "updated_at": raw.get("updated_at") or raw.get("analysis_updated_at") or raw.get("preview_updated_at"),
            "accounting_role": "container_only" if logical_container else "logical_file",
        }

    @staticmethod
    def _file_status_projection_query(status=None):
        """Return the bounded SQL projection shared by status-page and count APIs."""
        status = str(status or "all").strip().lower()
        valid_statuses = {
            "all", "pending", "processing", "completed", "partial", "failed",
            "retry_waiting", "out_of_scope",
        }
        if status not in valid_statuses:
            raise ValueError("未知文件状态筛选条件")
        # A union of compact keys means old scans, in-progress imports, and
        # logical children all participate even before every workflow record
        # exists. No document/preview sidecar body is selected here.
        source = """
            WITH all_paths AS (
                SELECT node_path FROM file_workflow_states WHERE scan_id=?
                UNION SELECT node_path FROM file_analysis_states WHERE scan_id=?
                UNION SELECT node_path FROM file_previews WHERE scan_id=?
                UNION SELECT node_path FROM document_translations WHERE scan_id=?
                UNION SELECT node_path FROM unified_documents WHERE scan_id=?
                UNION SELECT node_path FROM inventory_entries
                    WHERE scan_id=? AND kind IN ('file','logical_file')
            ), projected AS (
                SELECT paths.node_path,
                    inventory.kind AS inventory_kind, inventory.payload AS inventory_payload,
                    workflow.workflow_state, workflow.selection_state, workflow.selection_score,
                    workflow.reasons AS workflow_reasons_json, workflow.safety_status,
                    workflow.light_index_status, workflow.parse_status, workflow.evidence_status,
                    workflow.promotion_allowed, workflow.priority_source,
                    analysis.status AS analysis_status, analysis.parser, analysis.error AS analysis_error,
                    analysis.error_class, analysis.retryable, analysis.attempt_count,
                    analysis.next_retry_at, analysis.updated_at AS analysis_updated_at,
                    preview.status AS preview_status, preview.source_size,
                    preview.updated_at AS preview_updated_at,
                    translation.status AS translation_status,
                    translation.source_language AS translation_source_language,
                    workflow.updated_at AS updated_at,
                    CASE
                        WHEN analysis.status='completed' OR
                            (analysis.status IS NULL AND workflow.parse_status='completed') THEN 'completed'
                        WHEN analysis.status='failed' AND analysis.retryable=1
                            AND analysis.attempt_count<6
                            AND COALESCE(analysis.next_retry_at,0)>? THEN 'retry_waiting'
                        WHEN analysis.status='failed' THEN 'failed'
                        WHEN workflow.workflow_state='logical_container'
                            OR workflow.reasons LIKE '%logical_container_replaced_by_children%' THEN 'partial'
                        WHEN analysis.status='out_of_scope' OR workflow.promotion_allowed=0
                            OR workflow.safety_status IN ('restricted','rejected') THEN 'out_of_scope'
                        WHEN analysis.status IN ('needs_attention','overview','previewed','preview_failed','preview_deferred')
                            OR workflow.parse_status IN ('partial','source_unavailable')
                            OR preview.status IN ('previewed','restricted','deferred','failed') THEN 'partial'
                        WHEN workflow.parse_status IN ('processing','parsing','running')
                            OR workflow.workflow_state IN ('deep_parsing','deep_parse_running','processing') THEN 'processing'
                        ELSE 'pending'
                    END AS status_key
                FROM all_paths paths
                LEFT JOIN inventory_entries inventory
                    ON inventory.scan_id=? AND inventory.node_path=paths.node_path
                LEFT JOIN file_workflow_states workflow
                    ON workflow.scan_id=? AND workflow.node_path=paths.node_path
                LEFT JOIN file_analysis_states analysis
                    ON analysis.scan_id=? AND analysis.node_path=paths.node_path
                LEFT JOIN file_previews preview
                    ON preview.scan_id=? AND preview.node_path=paths.node_path
                LEFT JOIN document_translations translation
                    ON translation.scan_id=? AND translation.node_path=paths.node_path
            )
        """
        # Every scan-id placeholder is explicit, making the query safe even
        # when SQL statement caching is disabled by a deployment.
        return source, status

    def list_file_status_page(self, scan_id, offset=0, limit=100, status=None):
        """Return a paged, user-facing projection of every logical file.

        This is deliberately a database page, not a Python list of the whole
        package. It therefore remains bounded for hundreds of thousands of
        files while still including legacy physical inventory rows.
        """
        offset = max(0, int(offset or 0))
        limit = max(1, min(500, int(limit or 100)))
        query, status = self._file_status_projection_query(status)
        # Six CTE scans, then five joins, plus the current timestamp used to
        # distinguish a delayed automatic retry from an immediately queueable
        # retry.  Keep the values adjacent to the SQL for auditability.
        base_values = [str(scan_id)] * 6 + [time.time()] + [str(scan_id)] * 5
        where = "" if status == "all" else " WHERE status_key=?"
        values = list(base_values)
        if status != "all":
            values.append(status)
        with self._connect() as conn:
            total = int(conn.execute(
                query + " SELECT COUNT(*) AS value FROM projected" + where,
                values,
            ).fetchone()["value"] or 0)
            rows = conn.execute(
                query + " SELECT * FROM projected" + where +
                " ORDER BY CASE status_key WHEN 'failed' THEN 0 WHEN 'retry_waiting' THEN 1 "
                "WHEN 'processing' THEN 2 WHEN 'pending' THEN 3 WHEN 'partial' THEN 4 "
                "WHEN 'completed' THEN 5 ELSE 6 END, COALESCE(updated_at,analysis_updated_at,preview_updated_at) DESC,node_path "
                "LIMIT ? OFFSET ?",
                values + [limit, offset],
            ).fetchall()
        items = [self._file_status_item(row) for row in rows]
        return {
            "items": items,
            "offset": offset,
            "limit": limit,
            "total": total,
            "next_offset": offset + len(items) if offset + len(items) < total else None,
            "status": status,
        }

    def get_file_status(self, scan_id, node_path):
        """Look up one projected logical or physical file without path tricks."""
        # Use the same projection with a path predicate; never hydrate or page
        # through a whole package merely to open one evidence source.
        query, _status = self._file_status_projection_query("all")
        values = [str(scan_id)] * 6 + [time.time()] + [str(scan_id)] * 5
        with self._connect() as conn:
            row = conn.execute(
                query + " SELECT * FROM projected WHERE node_path=? LIMIT 1",
                values + [str(node_path)],
            ).fetchone()
        return self._file_status_item(row) if row else None

    def file_status_counts(self, scan_id, include_container_only=True):
        """Count canonical file states without making archive containers work.

        Archive containers remain visible in the paged status inventory, but
        once they have been replaced by independently schedulable members they
        must not become an extra incomplete item in the logical-file coverage
        denominator.  ``include_container_only`` is retained for diagnostic
        callers that explicitly want the physical container count.
        """
        query, _status = self._file_status_projection_query("all")
        values = [str(scan_id)] * 6 + [time.time()] + [str(scan_id)] * 5
        container_condition = (
            "(workflow_state='logical_container' OR "
            "COALESCE(workflow_reasons_json,'') LIKE '%logical_container_replaced_by_children%')"
        )
        where = "" if include_container_only else " WHERE NOT " + container_condition
        with self._connect() as conn:
            rows = conn.execute(
                query + " SELECT status_key,COUNT(*) AS value,"
                "SUM(CASE WHEN light_index_status='ready' THEN 1 ELSE 0 END) AS light_ready "
                "FROM projected" + where + " GROUP BY status_key",
                values,
            ).fetchall()
            container_row = conn.execute(
                query + " SELECT COUNT(*) AS value FROM projected WHERE " + container_condition,
                values,
            ).fetchone()
        result = {
            "pending": 0, "processing": 0, "completed": 0, "partial": 0,
            "failed": 0, "retry_waiting": 0, "out_of_scope": 0,
        }
        light_ready = 0
        for row in rows:
            result[str(row["status_key"])] = int(row["value"] or 0)
            light_ready += int(row["light_ready"] or 0)
        result["total"] = sum(result.values())
        result["light_ready"] = light_ready
        result["container_only"] = int(container_row["value"] or 0)
        result["incomplete"] = (
            result["pending"] + result["processing"] + result["partial"]
            + result["failed"] + result["retry_waiting"]
        )
        return result

    def file_workflow_counts(self, scan_id):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT selection_state,COUNT(*) AS value "
                "FROM file_workflow_states WHERE scan_id=? GROUP BY selection_state",
                (str(scan_id),),
            ).fetchall()
            aggregate = conn.execute(
                "SELECT COUNT(*) AS total,"
                "SUM(CASE WHEN safety_status IN ('checked','restricted','rejected') THEN 1 ELSE 0 END) AS safety_checked,"
                "SUM(CASE WHEN light_index_status='ready' THEN 1 ELSE 0 END) AS light_ready,"
                "SUM(CASE WHEN parse_status='completed' THEN 1 ELSE 0 END) AS parse_completed,"
                "SUM(CASE WHEN evidence_status='ready' THEN 1 ELSE 0 END) AS evidence_ready "
                "FROM file_workflow_states WHERE scan_id=?",
                (str(scan_id),),
            ).fetchone()
        return {
            "selection_states": {row["selection_state"]: int(row["value"]) for row in rows},
            **{key: int(aggregate[key] or 0) for key in (
                "total", "safety_checked", "light_ready", "parse_completed", "evidence_ready",
            )},
        }

    def package_processing_counts(self, scan_id):
        """Aggregate queue states without treating delayed/failed work as done.

        ``pending`` means work that may be leased immediately. ``retry_waiting``
        remains incomplete but is deliberately hidden from the ready queue until
        its backoff expires. ``needs_attention`` is also incomplete and requires
        an operator/capability change. Only policy exclusions are terminal.
        """
        now = time.time()
        # Early releases persisted duplicate/cache exclusions with
        # ``promotion_allowed=1``.  The scheduler has always excluded their
        # reason codes, so counts must apply the same rule until a legacy scan
        # is rebuilt.  Otherwise such files can leave a package permanently
        # stuck at "incomplete" even though no worker will ever claim them.
        terminal_exclusion = (
            "w.promotion_allowed=0 OR w.safety_status IN ('restricted','rejected') "
            "OR w.reasons LIKE '%\"exact_duplicate_non_primary\"%' "
            "OR w.reasons LIKE '%\"cache_temporary_or_dependency_file\"%'"
        )
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS workflow_total,"
                "COALESCE(SUM(CASE WHEN w.light_index_status='ready' THEN 1 ELSE 0 END),0) AS light_ready,"
                "COALESCE(SUM(CASE WHEN f.status='completed' OR w.parse_status='completed' THEN 1 ELSE 0 END),0) AS completed,"
                "COALESCE(SUM(CASE WHEN f.status='failed' AND f.retryable=1 "
                "AND f.attempt_count<6 AND COALESCE(f.next_retry_at,0)>? THEN 1 ELSE 0 END),0) AS retry_waiting,"
                "COALESCE(SUM(CASE WHEN f.status='needs_attention' OR "
                "(f.status='failed' AND (f.retryable=0 OR f.attempt_count>=6)) "
                "THEN 1 ELSE 0 END),0) AS needs_attention,"
                "COALESCE(SUM(CASE WHEN ({terminal_exclusion}) THEN 1 ELSE 0 END),0) AS policy_excluded,"
                "COALESCE(SUM(CASE WHEN NOT (COALESCE(f.status,'')='completed' OR w.parse_status='completed') "
                "AND NOT ({terminal_exclusion}) "
                "AND COALESCE(f.status,'')!='needs_attention' "
                "AND (f.status IS NULL OR f.status!='failed' OR "
                "(f.retryable=1 AND f.attempt_count<6 AND COALESCE(f.next_retry_at,0)<=?)) "
                "THEN 1 ELSE 0 END),0) AS pending "
                "FROM file_workflow_states w LEFT JOIN file_analysis_states f "
                "ON f.scan_id=w.scan_id AND f.node_path=w.node_path WHERE w.scan_id=?".format(
                    terminal_exclusion=terminal_exclusion
                ),
                (now, now, str(scan_id)),
            ).fetchone()
        result = {key: int(row[key] or 0) for key in (
            "workflow_total", "light_ready", "completed", "pending",
            "retry_waiting", "needs_attention", "policy_excluded",
        )}
        # Backwards-compatible field used by existing UI. It now means only a
        # deliberate terminal policy exclusion, never a delayed retry/failure.
        result["excluded"] = result["policy_excluded"]
        result["incomplete"] = max(
            0, result["workflow_total"] - result["completed"] - result["policy_excluded"]
        )
        return result

    def get_active_package_job(self, scan_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM analysis_jobs WHERE scan_id=? "
                "AND task_type IN ('scan_and_analyze','analyze_package') "
                "AND status IN ('running','cancelling','queued') "
                "ORDER BY CASE WHEN status IN ('running','cancelling') THEN 0 ELSE 1 END,"
                "priority DESC,rowid LIMIT 1",
                (str(scan_id),),
            ).fetchone()
        return self._decode_job(dict(row)) if row else None

    def update_file_workflow_stage(self, scan_id, node_path, workflow_state,
                                   parse_status=None, evidence_status=None,
                                   priority_source=None, light_index_status=None):
        assignments = ["workflow_state=?", "updated_at=CURRENT_TIMESTAMP"]
        values = [str(workflow_state)]
        if parse_status is not None:
            assignments.append("parse_status=?")
            values.append(str(parse_status))
        if evidence_status is not None:
            assignments.append("evidence_status=?")
            values.append(str(evidence_status))
        if priority_source is not None:
            assignments.append("priority_source=?")
            values.append(str(priority_source))
        if light_index_status is not None:
            assignments.append("light_index_status=?")
            values.append(str(light_index_status))
        values.extend([str(scan_id), str(node_path)])
        with self.lock, self._connect() as conn:
            result = conn.execute(
                "UPDATE file_workflow_states SET {} WHERE scan_id=? AND node_path=?".format(
                    ",".join(assignments)
                ), values,
            )
        return bool(result.rowcount)

    def prioritize_file_workflow_states(self, scan_id, paths, priority_source,
                                        reason, score_boost=1000.0):
        """Move user/recall-selected files ahead without removing other work."""
        paths = list(dict.fromkeys(str(path) for path in (paths or []) if path))
        if not paths:
            return 0
        source = str(priority_source or "manual_selection")
        reason = str(reason or source)[:500]
        updated = 0
        with self.lock, self._connect() as conn:
            for start in range(0, len(paths), 500):
                batch = paths[start:start + 500]
                rows = conn.execute(
                    "SELECT node_path,reasons FROM file_workflow_states "
                    "WHERE scan_id=? AND node_path IN ({})".format(
                        ",".join("?" for _ in batch)
                    ),
                    [str(scan_id)] + batch,
                ).fetchall()
                for row in rows:
                    try:
                        reasons = json.loads(row["reasons"] or "[]")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        reasons = []
                    reasons = [reason] + [item for item in reasons if item != reason]
                    result = conn.execute(
                        "UPDATE file_workflow_states SET selection_state='priority',"
                        "workflow_state='priority_queued',selection_score=MAX(selection_score,?),"
                        "reasons=?,priority_source=?,updated_at=CURRENT_TIMESTAMP "
                        "WHERE scan_id=? AND node_path=? AND promotion_allowed=1 "
                        "AND safety_status NOT IN ('restricted','rejected')",
                        (
                            float(score_boost),
                            json.dumps(reasons[:12], ensure_ascii=False),
                            source, str(scan_id), row["node_path"],
                        ),
                    )
                    updated += int(result.rowcount or 0)
        return updated

    def ensure_package_processing_control(self, scan_id, state="running"):
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO package_processing_controls("
                "scan_id,state,pause_requested,reason) VALUES (?,?,0,NULL)",
                (str(scan_id), str(state or "running")),
            )
        return self.get_package_processing_control(scan_id)

    def get_package_processing_control(self, scan_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM package_processing_controls WHERE scan_id=?",
                (str(scan_id),),
            ).fetchone()
        if not row:
            return {
                "scan_id": str(scan_id), "state": "running",
                "pause_requested": False, "reason": None,
            }
        item = dict(row)
        item["pause_requested"] = bool(item.get("pause_requested"))
        return item

    def set_package_processing_state(self, scan_id, state, reason=None):
        state = str(state or "running")
        if state not in {"running", "paused", "completed"}:
            raise ValueError("未知数据包处理状态")
        pause_requested = 1 if state == "paused" else 0
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO package_processing_controls("
                "scan_id,state,pause_requested,reason,updated_at) "
                "VALUES (?,?,?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(scan_id) DO UPDATE SET state=excluded.state,"
                "pause_requested=excluded.pause_requested,reason=excluded.reason,"
                "updated_at=CURRENT_TIMESTAMP",
                (str(scan_id), state, pause_requested, str(reason)[:1000] if reason else None),
            )
        return self.get_package_processing_control(scan_id)

    def package_processing_paused(self, scan_id):
        control = self.get_package_processing_control(scan_id)
        return bool(control.get("pause_requested") or control.get("state") == "paused")

    def pause_package_analysis_jobs(self, scan_id, reason=None):
        """Pause the full-package chain while preserving per-file checkpoints."""
        message = str(reason or "用户结束本次运行；已完成检查点保留。")
        now = time.time()
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO package_processing_controls("
                "scan_id,state,pause_requested,reason,updated_at) "
                "VALUES (?,'paused',1,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(scan_id) DO UPDATE SET state='paused',pause_requested=1,"
                "reason=excluded.reason,updated_at=CURRENT_TIMESTAMP",
                (str(scan_id), message[:1000]),
            )
            queued = conn.execute(
                "UPDATE analysis_jobs SET status='cancelled',stage='cancelled',"
                "cancel_requested=1,message=?,current_stage='已暂停',current_file='',"
                "worker_id=NULL,heartbeat_at=NULL,finished_at=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE scan_id=? AND task_type IN ('scan_and_analyze','analyze_package') "
                "AND status='queued'",
                (message, now, str(scan_id)),
            ).rowcount
            running = conn.execute(
                "UPDATE analysis_jobs SET status='cancelling',stage='cancelling',"
                "cancel_requested=1,message=?,current_stage='正在安全暂停',"
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE scan_id=? AND task_type IN ('scan_and_analyze','analyze_package') "
                "AND status IN ('running','cancelling')",
                (message, str(scan_id)),
            ).rowcount
        return {
            "control": self.get_package_processing_control(scan_id),
            "queued_cancelled": int(queued or 0),
            "running_stopping": int(running or 0),
        }

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
            conn.execute("DELETE FROM relationship_catalog WHERE scan_id=?", (str(scan_id),))
            conn.execute("DELETE FROM package_overviews WHERE scan_id=?", (str(scan_id),))

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
    def _job_priority(task_type, options=None):
        # New package imports and explicit supplement analysis are the primary
        # workflow. Optional summaries/reports must not indefinitely hide them
        # behind a long FIFO tail. A running task is never pre-empted.
        options = options or {}
        if str(task_type or "") == "analyze_package":
            source = str(options.get("workflow_source") or "")
            if (
                source == "question_promotion"
                or options.get("conversation_session_id")
                or options.get("conversation_turn_id")
            ):
                return 130
            if source == "manual_selection":
                return 110
            if source == "index_rebuild":
                return 110
            if source == "initial_overview":
                return 85
            if source == "background_backfill":
                return 20
        return {
            "scan_and_analyze": 100,
            "conversation_turn": 125,
            "rebuild_search_index": 110,
            # Explicit single-document translation is a manual action. Bulk
            # package translation remains the lowest-priority backfill.
            "translate_document": 110,
            "homogeneous_analysis": 90,
            "analyze_package": 80,
            "export_package": 60,
            "translate_package": 10,
            "generate_summary": 40,
            "generate_report": 30,
        }.get(str(task_type or ""), 50)

    def create_job(self, scan_id, options=None, task_type="analyze_package", owner_id="legacy"):
        job_id = uuid.uuid4().hex[:12]
        priority = self._job_priority(task_type, options)
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
            # never reuse one another's active supplement task. Priority source
            # and conversation identity are also part of equivalence: a live
            # question must not get trapped behind an existing background job
            # merely because both currently reference the same files.
            scope = (options or {}).get("target_paths") or []
            scope_key = (
                json.dumps({
                    "paths": sorted(set(str(item) for item in scope)),
                    "workflow_source": str((options or {}).get("workflow_source") or ""),
                    "conversation_session_id": str(
                        (options or {}).get("conversation_session_id") or ""
                    ),
                    "conversation_turn_id": str(
                        (options or {}).get("conversation_turn_id") or ""
                    ),
                }, ensure_ascii=False, sort_keys=True)
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
                    json.dumps({
                        "paths": sorted(set(str(item) for item in (
                            candidate_options.get("target_paths") or []
                        ))),
                        "workflow_source": str(candidate_options.get("workflow_source") or ""),
                        "conversation_session_id": str(
                            candidate_options.get("conversation_session_id") or ""
                        ),
                        "conversation_turn_id": str(
                            candidate_options.get("conversation_turn_id") or ""
                        ),
                    }, ensure_ascii=False, sort_keys=True)
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
            priority = self._job_priority(task_type, options)
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
        priority = self._job_priority("scan_and_analyze", options)
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO analysis_jobs(id,scan_id,task_type,status,stage,progress,message,options,owner_id,priority,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (job_id, job_id, "scan_and_analyze", "queued", "queued", 0, "等待扫描目录", json.dumps(options, ensure_ascii=False), owner_id or "legacy", priority),
            )
            conn.execute(
                "INSERT OR IGNORE INTO package_processing_controls("
                "scan_id,state,pause_requested,reason) VALUES (?,'running',0,NULL)",
                (job_id,),
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

    def defer_running_job(self, job_id, message, delay_seconds=0):
        """Return a claimed background job to its queue without using an attempt."""
        available_at = time.time() + max(0, int(delay_seconds or 0))
        with self.lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE analysis_jobs SET status='queued', stage='queued', worker_id=NULL, "
                "started_at=NULL, heartbeat_at=NULL, attempt_count=MAX(0,attempt_count-1), "
                "message=?, available_at=?, current_stage='等待资源', current_file='', "
                "updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='running' "
                "AND cancel_requested=0",
                (str(message), available_at, str(job_id)),
            )
        return cursor.rowcount == 1

    def has_queued_job_above_priority(self, priority):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM analysis_jobs WHERE status='queued' "
                "AND cancel_requested=0 AND available_at<=? AND priority>? LIMIT 1",
                (time.time(), int(priority or 0)),
            ).fetchone()
        return bool(row)

    def claim_next_job(self, worker_id):
        """Atomically claim the oldest queued job for one independent worker."""
        with self.lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            row = conn.execute(
                "SELECT id FROM analysis_jobs WHERE status='queued' AND cancel_requested=0 "
                "AND available_at<=? ORDER BY priority DESC, rowid LIMIT 1",
                (now,),
            ).fetchone()
            if not row:
                return None
            cursor = conn.execute(
                "UPDATE analysis_jobs SET status='running', stage='claimed', worker_id=?, "
                "started_at=?, heartbeat_at=?, available_at=0, "
                "attempt_count=attempt_count+1, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status='queued'",
                (str(worker_id), now, now, row["id"]),
            )
            if cursor.rowcount != 1:
                return None
            claimed = conn.execute("SELECT * FROM analysis_jobs WHERE id=?", (row["id"],)).fetchone()
        return self._decode_job(dict(claimed)) if claimed else None

    def wake_due_file_retries(self, limit=200):
        """Turn due retryable file failures into queued package jobs.

        The operation is idempotent: a scan with an active analysis job is not
        re-enqueued, and due timestamps are cleared in the same transaction.
        """
        now = time.time()
        limit = max(1, min(2000, int(limit or 200)))
        created = 0
        with self.lock, self._connect() as conn:
            scans = conn.execute(
                "SELECT DISTINCT f.scan_id,s.owner_id FROM file_analysis_states f "
                "JOIN scans s ON s.id=f.scan_id "
                "WHERE f.status='failed' AND f.retryable=1 AND f.attempt_count<6 "
                "AND COALESCE(f.next_retry_at,0)<=? LIMIT ?", (now, limit),
            ).fetchall()
            for scan in scans:
                scan_id = str(scan["scan_id"])
                active = conn.execute(
                    "SELECT 1 FROM analysis_jobs WHERE scan_id=? "
                    "AND task_type='analyze_package' AND status IN ('queued','running','cancelling') "
                    "AND cancel_requested=0 LIMIT 1", (scan_id,)
                ).fetchone()
                if active:
                    continue
                rows = conn.execute(
                    "SELECT node_path FROM file_analysis_states WHERE scan_id=? "
                    "AND status='failed' AND retryable=1 AND attempt_count<6 "
                    "AND COALESCE(next_retry_at,0)<=? ORDER BY node_path LIMIT ?",
                    (scan_id, limit),
                ).fetchall()
                paths = [str(row["node_path"]) for row in rows]
                if not paths:
                    continue
                options = {"workflow_source": "retry_scheduler", "target_paths": paths}
                job_id = uuid.uuid4().hex[:12]
                options_json = json.dumps(options, ensure_ascii=False)
                owner_id = str(scan["owner_id"] or "legacy")
                conn.execute(
                    "INSERT INTO analysis_jobs(id,scan_id,task_type,status,stage,progress,message,options,owner_id,priority,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                    (job_id, scan_id, "analyze_package", "queued", "queued", 1,
                     "检测到到期失败文件，已自动安排重试", options_json, owner_id, 105),
                )
                conn.executemany(
                    "UPDATE file_analysis_states SET next_retry_at=NULL,updated_at=CURRENT_TIMESTAMP "
                    "WHERE scan_id=? AND node_path=? AND status='failed'",
                    [(scan_id, path) for path in paths],
                )
                created += 1
        return created

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
