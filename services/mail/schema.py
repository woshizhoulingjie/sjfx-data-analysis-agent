"""Idempotent SQLite migration for the isolated mail domain."""

from __future__ import annotations

import hashlib


SCHEMA_VERSION = 6

DDL = r"""
CREATE TABLE IF NOT EXISTS mail_schema_migrations (
  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, checksum TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mail_packages (
  package_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, display_name TEXT NOT NULL,
  source_kind TEXT NOT NULL, source_path TEXT NOT NULL, source_revision TEXT NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, imported_at TEXT,
  logical_message_count INTEGER NOT NULL DEFAULT 0, occurrence_count INTEGER NOT NULL DEFAULT 0,
  index_epoch INTEGER NOT NULL DEFAULT 0, config_json TEXT NOT NULL DEFAULT '{}',
  error_json TEXT, idempotency_key TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mail_packages_owner_idempotency ON mail_packages(owner_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mail_packages_owner_updated ON mail_packages(owner_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS mail_sources (
  source_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, locator_type TEXT NOT NULL,
  root_path TEXT NOT NULL, container_path TEXT, container_sha256 TEXT,
  member_name TEXT, member_ordinal INTEGER, source_size INTEGER NOT NULL DEFAULT 0,
  source_revision TEXT NOT NULL, locator_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(package_id) REFERENCES mail_packages(package_id),
  UNIQUE(package_id, member_name, member_ordinal)
);
CREATE INDEX IF NOT EXISTS idx_mail_sources_package ON mail_sources(package_id, member_ordinal);
CREATE TABLE IF NOT EXISTS mail_messages (
  message_key TEXT PRIMARY KEY, raw_sha256 TEXT NOT NULL UNIQUE, canonical_message_id_raw TEXT,
  canonical_message_id_normalized TEXT, subject_raw TEXT, subject_normalized TEXT,
  date_raw TEXT, sent_at_utc TEXT, timezone_offset_minutes INTEGER,
  body_preferred_part TEXT, body_plain_part TEXT, body_html_part TEXT, body_language TEXT,
  parse_status TEXT NOT NULL, parser_version TEXT NOT NULL, source_revision TEXT NOT NULL,
  headers_json TEXT NOT NULL DEFAULT '{}', warnings_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mail_messages_date ON mail_messages(sent_at_utc, message_key);
CREATE INDEX IF NOT EXISTS idx_mail_messages_message_id ON mail_messages(canonical_message_id_normalized);
CREATE TABLE IF NOT EXISTS mail_occurrences (
  occurrence_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, source_id TEXT NOT NULL,
  message_key TEXT NOT NULL, source_member_name TEXT NOT NULL, source_member_ordinal INTEGER NOT NULL,
  raw_sha256 TEXT NOT NULL, imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(package_id, source_id, source_member_name),
  FOREIGN KEY(package_id) REFERENCES mail_packages(package_id),
  FOREIGN KEY(source_id) REFERENCES mail_sources(source_id), FOREIGN KEY(message_key) REFERENCES mail_messages(message_key)
);
CREATE INDEX IF NOT EXISTS idx_mail_occurrences_package ON mail_occurrences(package_id, message_key);
CREATE TABLE IF NOT EXISTS mail_addresses (
  address_id TEXT PRIMARY KEY, normalized_address TEXT NOT NULL UNIQUE, raw_address_sample TEXT,
  display_name_sample TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS mail_message_participants (
  message_key TEXT NOT NULL, address_id TEXT NOT NULL, role TEXT NOT NULL, position INTEGER NOT NULL,
  PRIMARY KEY(message_key, address_id, role, position),
  FOREIGN KEY(message_key) REFERENCES mail_messages(message_key), FOREIGN KEY(address_id) REFERENCES mail_addresses(address_id)
);
CREATE INDEX IF NOT EXISTS idx_mail_participants_message ON mail_message_participants(message_key, role, address_id);
CREATE TABLE IF NOT EXISTS mail_parts (
  part_id TEXT PRIMARY KEY, message_key TEXT NOT NULL, mime_path TEXT NOT NULL, parent_mime_path TEXT,
  content_type TEXT NOT NULL, content_disposition TEXT, content_id TEXT, filename_raw TEXT,
  transfer_encoding TEXT, byte_size INTEGER NOT NULL, payload_sha256 TEXT NOT NULL,
  part_role TEXT NOT NULL, is_explicit_attachment INTEGER NOT NULL DEFAULT 0,
  is_inline_resource INTEGER NOT NULL DEFAULT 0, locator_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(message_key, mime_path), FOREIGN KEY(message_key) REFERENCES mail_messages(message_key)
);
CREATE INDEX IF NOT EXISTS idx_mail_parts_message ON mail_parts(message_key, part_role, is_explicit_attachment);
CREATE TABLE IF NOT EXISTS mail_part_processing (
  message_key TEXT NOT NULL, mime_path TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
  parser_name TEXT, parser_version TEXT, text_sha256 TEXT, extracted_chars INTEGER NOT NULL DEFAULT 0,
  coverage_ratio REAL, warnings_json TEXT NOT NULL DEFAULT '[]', started_at TEXT, finished_at TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0, error_json TEXT,
  PRIMARY KEY(message_key, mime_path, stage)
);
CREATE INDEX IF NOT EXISTS idx_mail_processing_status ON mail_part_processing(stage, status, finished_at);
CREATE TABLE IF NOT EXISTS mail_text_segments (
  segment_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, message_key TEXT NOT NULL, mime_path TEXT NOT NULL,
  source_kind TEXT NOT NULL, language TEXT, ordinal INTEGER NOT NULL, page_number INTEGER, sheet_name TEXT,
  char_start INTEGER NOT NULL, char_end INTEGER NOT NULL, text TEXT NOT NULL, text_sha256 TEXT NOT NULL,
  source_revision TEXT NOT NULL, index_epoch INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(package_id) REFERENCES mail_packages(package_id), FOREIGN KEY(message_key) REFERENCES mail_messages(message_key)
);
CREATE INDEX IF NOT EXISTS idx_mail_segments_source ON mail_text_segments(message_key, mime_path, ordinal);
CREATE TABLE IF NOT EXISTS mail_thread_edges (
  edge_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, source_message_key TEXT NOT NULL,
  target_message_key TEXT, relation_type TEXT NOT NULL, source_header TEXT NOT NULL,
  target_message_id_raw TEXT, status TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mail_thread_source ON mail_thread_edges(package_id, source_message_key, status);
CREATE TABLE IF NOT EXISTS mail_communication_edges (
  package_id TEXT NOT NULL, from_address_id TEXT NOT NULL, to_address_id TEXT NOT NULL,
  recipient_role TEXT NOT NULL, message_key TEXT NOT NULL, sent_at_utc TEXT,
  occurrence_count INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(package_id, from_address_id, to_address_id, recipient_role, message_key)
);
CREATE INDEX IF NOT EXISTS idx_mail_communication_package ON mail_communication_edges(package_id, from_address_id, to_address_id, sent_at_utc);
CREATE TABLE IF NOT EXISTS mail_business_relations (
  relation_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, subject_ref TEXT NOT NULL, predicate TEXT NOT NULL,
  object_ref TEXT NOT NULL, state TEXT NOT NULL, confidence REAL, method TEXT NOT NULL,
  evidence_ids_json TEXT NOT NULL DEFAULT '[]', model_version TEXT, created_by TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, reviewed_by TEXT, reviewed_at TEXT, review_note TEXT
);
CREATE TABLE IF NOT EXISTS mail_evidence (
  evidence_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, message_key TEXT NOT NULL, occurrence_id TEXT,
  mime_path TEXT NOT NULL, segment_id TEXT, page_number INTEGER, sheet_name TEXT,
  char_start INTEGER, char_end INTEGER, quote_text TEXT NOT NULL, source_revision TEXT NOT NULL,
  index_epoch INTEGER NOT NULL DEFAULT 0, source_sha256 TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mail_evidence_message ON mail_evidence(package_id, message_key, mime_path);
CREATE TABLE IF NOT EXISTS mail_index_epochs (
  package_id TEXT NOT NULL, index_epoch INTEGER NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
  item_total INTEGER NOT NULL DEFAULT 0, item_ready INTEGER NOT NULL DEFAULT 0, item_failed INTEGER NOT NULL DEFAULT 0,
  item_pending INTEGER NOT NULL DEFAULT 0, config_fingerprint TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT, PRIMARY KEY(package_id, index_epoch, stage)
);
CREATE TABLE IF NOT EXISTS mail_result_sets (
  result_set_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, owner_id TEXT NOT NULL, parent_result_set_id TEXT,
  source_revision TEXT NOT NULL, index_epoch INTEGER NOT NULL, definition_json TEXT NOT NULL,
  item_count INTEGER NOT NULL DEFAULT 0, coverage_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, expires_at TEXT
);
CREATE TABLE IF NOT EXISTS mail_result_set_items (
  result_set_id TEXT NOT NULL, item_type TEXT NOT NULL, item_id TEXT NOT NULL, rank INTEGER NOT NULL,
  status TEXT, rationale_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(result_set_id, item_type, item_id)
);
CREATE TABLE IF NOT EXISTS mail_research_sessions (
  session_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, owner_id TEXT NOT NULL, title TEXT NOT NULL,
  active_result_set_id TEXT, scope_json TEXT NOT NULL DEFAULT '{}', source_revision TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mail_sessions_owner ON mail_research_sessions(owner_id, package_id, updated_at);
CREATE TABLE IF NOT EXISTS mail_research_turns (
  turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, owner_id TEXT NOT NULL, question TEXT NOT NULL,
  task_spec_json TEXT NOT NULL, status TEXT NOT NULL, sufficiency TEXT, result_set_id TEXT, job_id TEXT,
  source_revision TEXT NOT NULL, index_epoch INTEGER NOT NULL, final_json TEXT, error_json TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mail_turns_session ON mail_research_turns(session_id, created_at);
CREATE TABLE IF NOT EXISTS mail_research_steps (
  step_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, sequence INTEGER NOT NULL, tool_name TEXT NOT NULL,
  input_json TEXT NOT NULL, output_ref TEXT, status TEXT NOT NULL, started_at TEXT, finished_at TEXT, error_json TEXT
);
CREATE TABLE IF NOT EXISTS mail_research_events (
  event_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS mail_claims (
  claim_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, claim_text TEXT NOT NULL, claim_type TEXT NOT NULL,
  state TEXT NOT NULL, support_status TEXT NOT NULL, evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  counter_evidence_ids_json TEXT NOT NULL DEFAULT '[]', verification_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS mail_job_progress (
  job_id TEXT PRIMARY KEY, phase TEXT NOT NULL, cursor_json TEXT NOT NULL DEFAULT '{}',
  counts_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

FTS_DDL = r"""
CREATE VIRTUAL TABLE IF NOT EXISTS mail_search_fts USING fts5(
  segment_id UNINDEXED, package_id UNINDEXED, message_key UNINDEXED, mime_path UNINDEXED,
  field UNINDEXED, language UNINDEXED, text, tokenize='unicode61'
);
"""

FTS_FALLBACK_DDL = r"""
CREATE TABLE IF NOT EXISTS mail_search_fts (
  segment_id TEXT NOT NULL, package_id TEXT NOT NULL, message_key TEXT NOT NULL, mime_path TEXT NOT NULL,
  field TEXT NOT NULL, language TEXT, text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mail_search_fallback_package ON mail_search_fts(package_id, message_key);
"""


def _ensure_search_index(connection):
    """FTS5 accelerates recall when available; correctness never depends on it."""
    try:
        connection.executescript(FTS_DDL)
    except Exception as exc:
        if "fts5" not in str(exc).casefold():
            raise
        connection.executescript(FTS_FALLBACK_DDL)


# Keep the complete version-2 DDL above byte-for-byte: existing ledgers hash it.
DDL_V3 = r"""
CREATE TABLE IF NOT EXISTS mail_embeddings (
 package_id TEXT NOT NULL, segment_id TEXT NOT NULL, model TEXT NOT NULL, dim INTEGER NOT NULL,
 vector BLOB NOT NULL, text_sha256 TEXT NOT NULL, index_epoch INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(package_id,segment_id,model));
CREATE INDEX IF NOT EXISTS idx_mail_embeddings_hash ON mail_embeddings(package_id,model,text_sha256);
CREATE TABLE IF NOT EXISTS mail_embedding_buckets (
 package_id TEXT NOT NULL, model TEXT NOT NULL, band INTEGER NOT NULL,bucket INTEGER NOT NULL,segment_id TEXT NOT NULL,
 PRIMARY KEY(package_id,model,band,bucket,segment_id));
CREATE TABLE IF NOT EXISTS mail_conversations (
 conversation_id TEXT PRIMARY KEY,package_id TEXT NOT NULL,root_message_key TEXT,subject_normalized TEXT,
 started_at TEXT,last_at TEXT,message_count INTEGER NOT NULL,participant_count INTEGER NOT NULL,
 has_attachment INTEGER NOT NULL,dominant_language TEXT,merge_basis TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_mail_conversations_package ON mail_conversations(package_id,last_at);
CREATE TABLE IF NOT EXISTS mail_conversation_members (
 conversation_id TEXT NOT NULL,message_key TEXT NOT NULL,depth INTEGER NOT NULL,is_root INTEGER NOT NULL,
 PRIMARY KEY(conversation_id,message_key));
CREATE TABLE IF NOT EXISTS mail_projection_state (
 package_id TEXT NOT NULL,stage TEXT NOT NULL,fingerprint TEXT NOT NULL,PRIMARY KEY(package_id,stage));
CREATE TABLE IF NOT EXISTS mail_facts (
 fact_id TEXT PRIMARY KEY,package_id TEXT NOT NULL,message_key TEXT NOT NULL,evidence_id TEXT NOT NULL,segment_id TEXT,
 fact_type TEXT NOT NULL,fact_key TEXT NOT NULL,subject_ref TEXT NOT NULL,value_raw TEXT NOT NULL,
 value_normalized TEXT,unit TEXT,confidence REAL,method TEXT NOT NULL,source_revision TEXT NOT NULL,
 index_epoch INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'mentioned',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_mail_facts_scope ON mail_facts(package_id,subject_ref,fact_type,fact_key);
CREATE TABLE IF NOT EXISTS mail_fact_conflicts (
 conflict_id TEXT PRIMARY KEY,package_id TEXT NOT NULL,fact_type TEXT NOT NULL,fact_key TEXT NOT NULL,
 subject_ref TEXT NOT NULL,fact_ids_json TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mail_fact_processing (
 package_id TEXT NOT NULL,message_key TEXT NOT NULL,schema_key TEXT NOT NULL,fingerprint TEXT NOT NULL,
 status TEXT NOT NULL,error TEXT,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(package_id,message_key,schema_key));
CREATE TABLE IF NOT EXISTS mail_entities (
 entity_id TEXT PRIMARY KEY,package_id TEXT NOT NULL,entity_type TEXT NOT NULL,display_name TEXT NOT NULL,
 basis TEXT NOT NULL,confirmed_by TEXT,confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS mail_entity_addresses (
 package_id TEXT NOT NULL,entity_id TEXT NOT NULL,address_id TEXT NOT NULL,basis TEXT NOT NULL,
 PRIMARY KEY(package_id,entity_id,address_id),UNIQUE(package_id,address_id));
CREATE TABLE IF NOT EXISTS mail_communication_daily (
 package_id TEXT NOT NULL,day TEXT NOT NULL,from_address_id TEXT NOT NULL,to_address_id TEXT NOT NULL,
 recipient_role TEXT NOT NULL,message_count INTEGER NOT NULL,occurrence_count INTEGER NOT NULL,
 PRIMARY KEY(package_id,day,from_address_id,to_address_id,recipient_role));
CREATE TABLE IF NOT EXISTS mail_attachment_versions (
 version_id TEXT PRIMARY KEY,package_id TEXT NOT NULL,chain_key TEXT NOT NULL,payload_sha256 TEXT NOT NULL,
 filename_normalized TEXT NOT NULL,version_ordinal INTEGER NOT NULL,first_message_key TEXT,first_sent_at TEXT,
 last_message_key TEXT,last_sent_at TEXT,revision_count INTEGER NOT NULL,
 UNIQUE(package_id,chain_key,payload_sha256));
CREATE TABLE IF NOT EXISTS mail_attachment_version_links (
 package_id TEXT NOT NULL,chain_key TEXT NOT NULL,version_ordinal INTEGER NOT NULL,message_key TEXT NOT NULL,
 mime_path TEXT NOT NULL,payload_sha256 TEXT NOT NULL,
 PRIMARY KEY(package_id,chain_key,message_key,mime_path));
"""


# Append migrations; never alter the historical v2/v3 source hashed above.
DDL_V4 = r"""
CREATE TABLE IF NOT EXISTS mail_raw_store (
 raw_sha256 TEXT PRIMARY KEY,raw_bytes BLOB NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS mail_body_fragments (
 message_key TEXT NOT NULL,mime_path TEXT NOT NULL,ordinal INTEGER NOT NULL,fragment_kind TEXT NOT NULL,
 char_start INTEGER NOT NULL,char_end INTEGER NOT NULL,text TEXT NOT NULL,method TEXT NOT NULL,confidence TEXT NOT NULL,
 PRIMARY KEY(message_key,mime_path,ordinal));
CREATE TABLE IF NOT EXISTS mail_import_entries (
 package_id TEXT NOT NULL,member_name TEXT NOT NULL,member_ordinal INTEGER NOT NULL,byte_size INTEGER,
 status TEXT NOT NULL,message_key TEXT,error_json TEXT,locator_json TEXT,updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(package_id,member_name,member_ordinal));
CREATE TABLE IF NOT EXISTS mail_foundation_state (
 package_id TEXT NOT NULL,stage TEXT NOT NULL,version TEXT,fingerprint TEXT,status TEXT NOT NULL,
 cursor_json TEXT DEFAULT '{}',error_json TEXT,updated_at TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(package_id,stage));
CREATE TABLE IF NOT EXISTS mail_entity_audit (
 audit_id TEXT PRIMARY KEY,package_id TEXT NOT NULL,entity_id TEXT NOT NULL,actor_id TEXT NOT NULL,
 action TEXT NOT NULL,payload_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_mail_entity_audit_scope ON mail_entity_audit(package_id,entity_id,created_at);
CREATE TABLE IF NOT EXISTS mail_relation_generations (
 generation_id TEXT PRIMARY KEY,package_id TEXT NOT NULL,stage TEXT NOT NULL,fingerprint TEXT NOT NULL,
 algorithm_version TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_mail_relation_generations_scope ON mail_relation_generations(package_id,stage,created_at);
CREATE TABLE IF NOT EXISTS mail_thread_nodes (
 package_id TEXT NOT NULL,generation_id TEXT NOT NULL,node_id TEXT NOT NULL,message_key TEXT,
 message_id_normalized TEXT,node_kind TEXT NOT NULL,PRIMARY KEY(generation_id,node_id));
CREATE TABLE IF NOT EXISTS mail_thread_edge_history (
 generation_id TEXT NOT NULL,edge_id TEXT NOT NULL,package_id TEXT NOT NULL,source_message_key TEXT NOT NULL,
 target_message_key TEXT,target_node_id TEXT,relation_type TEXT NOT NULL,source_header TEXT NOT NULL,
 target_message_id_raw TEXT,status TEXT NOT NULL,evidence_json TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(generation_id,edge_id));
CREATE TABLE IF NOT EXISTS mail_thread_anomalies (
 generation_id TEXT NOT NULL,anomaly_id TEXT NOT NULL,package_id TEXT NOT NULL,message_key TEXT,
 anomaly_type TEXT NOT NULL,payload_json TEXT NOT NULL DEFAULT '{}',PRIMARY KEY(generation_id,anomaly_id));
CREATE TABLE IF NOT EXISTS mail_conversation_history (
 generation_id TEXT NOT NULL,conversation_id TEXT NOT NULL,package_id TEXT NOT NULL,root_message_key TEXT,
 subject_normalized TEXT,started_at TEXT,last_at TEXT,message_count INTEGER NOT NULL,participant_count INTEGER NOT NULL,
 has_attachment INTEGER NOT NULL,dominant_language TEXT,merge_basis TEXT NOT NULL,PRIMARY KEY(generation_id,conversation_id));
CREATE TABLE IF NOT EXISTS mail_conversation_member_history (
 generation_id TEXT NOT NULL,conversation_id TEXT NOT NULL,message_key TEXT NOT NULL,depth INTEGER NOT NULL,
 is_root INTEGER NOT NULL,PRIMARY KEY(generation_id,conversation_id,message_key));
CREATE TABLE IF NOT EXISTS mail_conversation_corrections (
 correction_id TEXT PRIMARY KEY,package_id TEXT NOT NULL,actor_id TEXT NOT NULL,action TEXT NOT NULL,
 payload_json TEXT NOT NULL,fingerprint TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_mail_conversation_corrections_scope ON mail_conversation_corrections(package_id,created_at);
"""
DDL_V4_COLUMNS = r"""
ALTER TABLE mail_text_segments ADD COLUMN cell_range TEXT;
ALTER TABLE mail_part_processing ADD COLUMN next_retry_at TEXT;
ALTER TABLE mail_evidence ADD COLUMN cell_range TEXT;
ALTER TABLE mail_evidence ADD COLUMN payload_sha256 TEXT;
ALTER TABLE mail_evidence ADD COLUMN text_sha256 TEXT;
"""


def _apply_v4(connection):
    # execute() keeps SQLite DDL inside the caller's savepoint; executescript()
    # would commit pending work before applying the new schema.
    for statement in DDL_V4.split(';'):
        if statement.strip():
            connection.execute(statement)
    for table, column in (("mail_text_segments", "cell_range"), ("mail_part_processing", "next_retry_at"),
                          ("mail_evidence", "cell_range"), ("mail_evidence", "payload_sha256"), ("mail_evidence", "text_sha256")):
        columns = {row[1] for row in connection.execute("PRAGMA table_info({})".format(table))}
        if column not in columns:
            connection.execute("ALTER TABLE {} ADD COLUMN {} TEXT".format(table, column))


DDL_V5 = r"""
ALTER TABLE mail_occurrences ADD COLUMN is_countable INTEGER NOT NULL DEFAULT 1;
CREATE VIEW IF NOT EXISTS mail_visible_occurrences AS SELECT * FROM mail_occurrences WHERE is_countable=1;
"""


def _apply_v5(connection):
    columns = {row[1] for row in connection.execute("PRAGMA table_info(mail_occurrences)")}
    if "is_countable" not in columns:
        connection.execute("ALTER TABLE mail_occurrences ADD COLUMN is_countable INTEGER NOT NULL DEFAULT 1")
    connection.execute("CREATE VIEW IF NOT EXISTS mail_visible_occurrences AS SELECT * FROM mail_occurrences WHERE is_countable=1")


DDL_V6 = r"""
CREATE TABLE IF NOT EXISTS mail_semantic_generations (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, generation_id TEXT NOT NULL UNIQUE,
 package_id TEXT NOT NULL, owner_id TEXT NOT NULL, slot_key TEXT NOT NULL,
 scope_fingerprint TEXT NOT NULL, source_fingerprint TEXT NOT NULL,
 algorithm_version TEXT NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_mail_semantic_generation_scope ON mail_semantic_generations(package_id,slot_key,status,sequence);
CREATE TABLE IF NOT EXISTS mail_communication_acts (
 generation_id TEXT NOT NULL, act_id TEXT NOT NULL, package_id TEXT NOT NULL,
 message_key TEXT NOT NULL, actor TEXT NOT NULL, act_type TEXT NOT NULL,
 object_identity TEXT NOT NULL, object_version TEXT NOT NULL, source_hash TEXT NOT NULL,
 asserted_at TEXT, effective_at TEXT, review_status TEXT NOT NULL, payload_json TEXT NOT NULL,
 PRIMARY KEY(generation_id,act_id));
CREATE INDEX IF NOT EXISTS idx_mail_acts_scope ON mail_communication_acts(package_id,message_key,actor,act_id);
CREATE TABLE IF NOT EXISTS mail_issues (
 issue_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, title TEXT NOT NULL,
 anchor_key TEXT NOT NULL, link_status TEXT NOT NULL, payload_json TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_mail_issues_scope ON mail_issues(package_id,anchor_key);
CREATE TABLE IF NOT EXISTS mail_issue_memberships (
 generation_id TEXT NOT NULL, package_id TEXT NOT NULL, act_id TEXT NOT NULL,
 issue_id TEXT NOT NULL, event_ref_id TEXT, method TEXT NOT NULL,
 review_status TEXT NOT NULL, decision_json TEXT NOT NULL,
 PRIMARY KEY(generation_id,act_id));
CREATE INDEX IF NOT EXISTS idx_mail_membership_scope ON mail_issue_memberships(package_id,issue_id,act_id);
CREATE TABLE IF NOT EXISTS mail_assertion_links (
 generation_id TEXT NOT NULL, link_id TEXT NOT NULL, package_id TEXT NOT NULL,
 source_act_id TEXT NOT NULL, target_act_id TEXT, relation_type TEXT NOT NULL,
 review_status TEXT NOT NULL, payload_json TEXT NOT NULL,
 PRIMARY KEY(generation_id,link_id));
CREATE TABLE IF NOT EXISTS mail_issue_states (
 state_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, issue_id TEXT NOT NULL,
 scope_fingerprint TEXT NOT NULL, dependency_fingerprint TEXT NOT NULL,
 as_of TEXT NOT NULL, engine_version TEXT NOT NULL, payload_json TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS mail_act_extraction_cache (
 cache_key TEXT PRIMARY KEY, package_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 message_key TEXT NOT NULL, source_hash TEXT NOT NULL, scope_fingerprint TEXT NOT NULL,
 slice_json TEXT NOT NULL, extraction_version TEXT NOT NULL, model_version TEXT NOT NULL,
 attribution_fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL,
 completion TEXT NOT NULL DEFAULT 'slice_only', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_mail_act_cache_scope ON mail_act_extraction_cache(package_id,message_key,extraction_version,source_hash);
"""


def _apply_v6(connection):
    for statement in DDL_V6.split(';'):
        if statement.strip():
            connection.execute(statement)


def migrate(connection):
    connection.execute("CREATE TABLE IF NOT EXISTS mail_schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, checksum TEXT NOT NULL)")
    migrations = [(2, DDL, DDL + FTS_DDL + FTS_FALLBACK_DDL), (3, DDL_V3, DDL_V3),
                  (4, DDL_V4, DDL_V4 + DDL_V4_COLUMNS), (5, DDL_V5, DDL_V5), (6, DDL_V6, DDL_V6)]
    changed = False
    for version, ddl, source in migrations:
        checksum = hashlib.sha256(source.encode("utf-8")).hexdigest()
        row = connection.execute("SELECT checksum FROM mail_schema_migrations WHERE version=?", (version,)).fetchone()
        if row:
            if row[0] != checksum:
                raise RuntimeError("邮件数据库迁移校验和不一致：版本 {}".format(version))
            continue
        if version in (4, 5, 6):
            savepoint = "mail_schema_v{}".format(version)
            connection.execute("SAVEPOINT " + savepoint)
            try:
                {4: _apply_v4, 5: _apply_v5, 6: _apply_v6}[version](connection)
                connection.execute("INSERT INTO mail_schema_migrations(version,checksum) VALUES (?,?)", (version,checksum))
                connection.execute("RELEASE " + savepoint)
            except Exception:
                connection.execute("ROLLBACK TO " + savepoint)
                connection.execute("RELEASE " + savepoint)
                raise
            changed = True
            continue
        connection.executescript(ddl)
        if version == 2:
            _ensure_search_index(connection)
        connection.execute("INSERT INTO mail_schema_migrations(version,checksum) VALUES (?,?)", (version,checksum))
        changed = True
    return changed
