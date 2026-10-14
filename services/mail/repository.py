"""Owner-scoped persistence gateway for every ``mail_*`` table."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager

from config import Config

from .normalization import normalize_address, normalize_message_id, normalized_subject
from .schema import migrate
from .text_provenance import (OCR_NOTICE,decode_processing_warnings,legacy_origin,
    location,processing_metadata,provenance_for_unit)

_UNSET = object()

def _id(prefix):
    return "{}_{}".format(prefix, uuid.uuid4().hex)


def _json(value):
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _load(value, default=None):
    try:
        return json.loads(value) if value else ({} if default is None else default)
    except (TypeError, ValueError):
        return {} if default is None else default


def _row(value):
    return dict(value) if value is not None else None


class MailRepository:
    """Mail-only adapter over the established SQLite connection settings."""

    def __init__(self, storage, *, text_segment_chars=None):
        self.storage = storage.initialize() if hasattr(storage, "initialize") else storage
        self.text_segment_chars = max(
            1,
            int(
                text_segment_chars
                if text_segment_chars is not None
                else getattr(Config, "MAIL_TEXT_SEGMENT_CHARS", 1800)
            ),
        )

    @contextmanager
    def _connection(self):
        lock = getattr(self.storage, "lock", None)
        if lock:
            with lock, self.storage._connect() as connection:
                yield connection
        else:
            with self.storage._connect() as connection:
                yield connection

    def migrate(self):
        with self._connection() as connection:
            return migrate(connection)


    def close(self):
        """Release this database's process caches when its instance shuts down."""
        from .network_service import close_database
        close_database(self.storage.db_path)

    def create_package(self, *, owner_id, display_name, source_kind, source_path, source_revision, config, idempotency_key=None):
        with self._connection() as connection:
            if idempotency_key:
                existing = connection.execute(
                    "SELECT * FROM mail_packages WHERE owner_id=? AND idempotency_key=?", (owner_id, idempotency_key)
                ).fetchone()
                if existing:
                    return _row(existing), False
            package_id = _id("mp")
            connection.execute(
                "INSERT INTO mail_packages(package_id,owner_id,display_name,source_kind,source_path,source_revision,status,config_json,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?)",
                (package_id, owner_id, display_name, source_kind, source_path, source_revision, "inventory", _json(config), idempotency_key),
            )
            return self._package(connection, package_id, owner_id), True

    def _package(self, connection, package_id, owner_id):
        row = connection.execute("SELECT * FROM mail_packages WHERE package_id=? AND owner_id=?", (package_id, owner_id)).fetchone()
        return self._decode_package(_row(row))

    def _decode_package(self, package):
        if not package:
            return None
        package["config"] = _load(package.pop("config_json", "{}"))
        package["error"] = _load(package.pop("error_json", None), None) if package.get("error_json") else None
        return package

    def get_package(self, package_id, owner_id):
        with self._connection() as connection:
            return self._package(connection, package_id, owner_id)

    def list_packages(self, owner_id):
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM mail_packages WHERE owner_id=? ORDER BY updated_at DESC", (owner_id,)).fetchall()
        return [self._decode_package(_row(item)) for item in rows]

    def update_package(self, package_id, owner_id, *, status=None, error=_UNSET, imported=False, counts=False, index_epoch=None):
        fields = ["updated_at=CURRENT_TIMESTAMP"]
        values = []
        if status is not None:
            fields.append("status=?")
            values.append(status)
        if error is not _UNSET:
            fields.append("error_json=?")
            values.append(_json(error) if error is not None else None)
        if imported:
            fields.append("imported_at=CURRENT_TIMESTAMP")
        if index_epoch is not None:
            fields.append("index_epoch=?")
            values.append(int(index_epoch))
        with self._connection() as connection:
            if counts:
                fields.extend([
                    "occurrence_count=(SELECT COUNT(*) FROM mail_visible_occurrences WHERE package_id=mail_packages.package_id)",
                    "logical_message_count=(SELECT COUNT(DISTINCT message_key) FROM mail_visible_occurrences WHERE package_id=mail_packages.package_id)",
                ])
            values.extend([package_id, owner_id])
            connection.execute("UPDATE mail_packages SET {} WHERE package_id=? AND owner_id=?".format(",".join(fields)), values)

    def package_owner(self, package_id):
        with self._connection() as connection:
            row = connection.execute("SELECT owner_id FROM mail_packages WHERE package_id=?", (package_id,)).fetchone()
        return row["owner_id"] if row else None

    def progress(self, job_id):
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM mail_job_progress WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return {"phase": "inventory", "cursor": {}, "counts": {}}
        value = _row(row)
        return {"phase": value["phase"], "cursor": _load(value["cursor_json"]), "counts": _load(value["counts_json"]), "updated_at": value["updated_at"]}

    def save_progress(self, job_id, phase, cursor=None, counts=None):
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO mail_job_progress(job_id,phase,cursor_json,counts_json,updated_at) VALUES (?,?,?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(job_id) DO UPDATE SET phase=excluded.phase,cursor_json=excluded.cursor_json,counts_json=excluded.counts_json,updated_at=CURRENT_TIMESTAMP",
                (job_id, phase, _json(cursor or {}), _json(counts or {})),
            )

    def cancelled(self, job_id):
        with self._connection() as connection:
            row = connection.execute("SELECT cancel_requested,status FROM analysis_jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and (row["cancel_requested"] or row["status"] in {"cancelled", "cancelling"}))

    def upsert_ingested_message(self, *, package_id, source, member, raw, parsed):
        """Atomically retain occurrence, exact logical message, parts and body index."""
        raw_sha = parsed.raw_sha256
        if hashlib.sha256(raw).hexdigest() != raw_sha:
            raise ValueError('原始邮件内容哈希不一致')
        message_id = parsed.message_id_raw[0] if parsed.message_id_raw else None
        preferred = next((part.mime_path for part in parsed.parts if part.part_role == "body_plain" and (part.text or "").strip()), None)
        if not preferred:
            preferred = next((part.mime_path for part in parsed.parts if part.part_role == "body_html" and (part.text or "").strip()), None)
        plain = next((part.mime_path for part in parsed.parts if part.part_role == "body_plain"), None)
        html = next((part.mime_path for part in parsed.parts if part.part_role == "body_html"), None)
        with self._connection() as connection:
            source_id = _id("ms")
            connection.execute(
                "INSERT OR IGNORE INTO mail_sources(source_id,package_id,locator_type,root_path,container_path,container_sha256,member_name,member_ordinal,source_size,source_revision,locator_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (source_id, package_id, source["locator_type"], source["root_path"], source.get("container_path"), source.get("container_sha256"), member.name, member.ordinal, member.byte_size, source["source_revision"], _json(member.locator)),
            )
            source_row = connection.execute("SELECT source_id FROM mail_sources WHERE package_id=? AND member_name=? AND member_ordinal=?", (package_id, member.name, member.ordinal)).fetchone()
            source_id = source_row["source_id"]
            existing = connection.execute("SELECT message_key,raw_sha256 FROM mail_messages WHERE raw_sha256=?", (raw_sha,)).fetchone()
            if not existing:
                # A transport/export copy can differ in Received or mailbox
                # flags. Never collapse a Message-ID conflict on that ID alone.
                from .deduplication import copy_fingerprint
                fingerprint = copy_fingerprint(parsed)
                if fingerprint:
                    from .parsers.eml import parse_eml
                    candidates = connection.execute(
                        "SELECT m.message_key,m.raw_sha256,r.raw_bytes FROM mail_messages m "
                        "JOIN mail_raw_store r ON r.raw_sha256=m.raw_sha256 "
                        "WHERE m.canonical_message_id_normalized=? AND EXISTS ("
                        "SELECT 1 FROM mail_occurrences co JOIN mail_packages cp ON cp.package_id=co.package_id "
                        "WHERE co.message_key=m.message_key AND cp.owner_id=(SELECT owner_id FROM mail_packages WHERE package_id=?)) LIMIT 21",
                        (normalize_message_id(message_id), package_id),
                    ).fetchall()
                    if len(candidates) <= 20:
                        matching = [candidate for candidate in candidates
                                    if copy_fingerprint(parse_eml(bytes(candidate['raw_bytes']))) == fingerprint]
                        if len(matching) == 1:
                            existing = matching[0]
            new_message = not bool(existing)
            message_key = existing["message_key"] if existing else _id("mm")
            if new_message:
                connection.execute(
                    "INSERT INTO mail_messages(message_key,raw_sha256,canonical_message_id_raw,canonical_message_id_normalized,subject_raw,subject_normalized,date_raw,sent_at_utc,timezone_offset_minutes,body_preferred_part,body_plain_part,body_html_part,body_language,parse_status,parser_version,source_revision,headers_json,warnings_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (message_key, raw_sha, message_id, normalize_message_id(message_id), self._header(parsed, "subject"), normalized_subject(self._header(parsed, "subject")), parsed.date_raw, parsed.sent_at_utc, parsed.timezone_offset_minutes, preferred, plain, html, self._language("\n".join(part.text or "" for part in parsed.parts if part.part_role.startswith("body_"))), parsed.parse_status, "mail-eml-v3", source["source_revision"], _json(parsed.headers_raw), _json(parsed.warnings)),
                )
                self._insert_participants(connection, message_key, parsed.participants)
                self._insert_parts(connection, message_key, parsed.parts)
            occurrence_id = _id("mo")
            connection.execute(
                "INSERT OR IGNORE INTO mail_occurrences(occurrence_id,package_id,source_id,message_key,source_member_name,source_member_ordinal,raw_sha256) VALUES (?,?,?,?,?,?,?)",
                (occurrence_id, package_id, source_id, message_key, member.name, member.ordinal, raw_sha),
            )
            occurrence = connection.execute("SELECT occurrence_id,raw_sha256 FROM mail_occurrences WHERE package_id=? AND source_id=? AND source_member_name=?", (package_id, source_id, member.name)).fetchone()
            if occurrence['raw_sha256'] != raw_sha:
                raise ValueError('邮件来源条目内容已变化；请作为新的资料包版本导入')
            from .foundation import retain_content
            if not existing or existing['raw_sha256'] == raw_sha:
                retain_content(connection, message_key, raw, parsed)
            else:
                # Reading projection uses the representative message. Every
                # copy's transport headers remain available in its own EML.
                connection.execute("INSERT OR IGNORE INTO mail_raw_store(raw_sha256,raw_bytes) VALUES (?,?)", (raw_sha, raw))
            connection.execute("INSERT INTO mail_import_entries(package_id,member_name,member_ordinal,byte_size,status,message_key,locator_json) VALUES (?,?,?,?,?,?,?) ON CONFLICT(package_id,member_name,member_ordinal) DO UPDATE SET status=excluded.status,message_key=excluded.message_key,error_json=NULL,updated_at=CURRENT_TIMESTAMP", (package_id, member.name, member.ordinal, member.byte_size, 'ready', message_key, _json(member.locator)))
            # Replaying an import must not relabel the original as a duplicate.
            # Select the same first source member even after cancellation or a
            # new job, and normalize existing copies if ingestion order changes.
            first = connection.execute('SELECT source_member_name,source_member_ordinal FROM mail_visible_occurrences WHERE package_id=? AND message_key=? ORDER BY source_member_ordinal,source_member_name LIMIT 1', (package_id, message_key)).fetchone()
            if first:
                connection.execute("UPDATE mail_import_entries SET status=CASE WHEN member_name=? AND member_ordinal=? THEN 'ready' ELSE 'duplicate' END WHERE package_id=? AND message_key=? AND status IN ('ready','duplicate')", (first['source_member_name'], first['source_member_ordinal'], package_id, message_key))
            if new_message:
                self._insert_body_segments(connection, package_id, message_key, parsed.parts, source["source_revision"])
            elif not connection.execute(
                "SELECT 1 FROM mail_text_segments WHERE package_id=? AND message_key=? LIMIT 1",
                (package_id, message_key),
            ).fetchone():
                # ``mail_messages`` is intentionally deduplicated across packages,
                # while text segments are package-scoped for owner isolation and
                # immutable source revisions.  Reuse completed text only by
                # projecting it into this package's own index rows.
                copied = self._copy_segments_to_package(connection, package_id, message_key)
                if not copied:
                    self._insert_body_segments(connection, package_id, message_key, parsed.parts, source["source_revision"])
            return {"message_key": message_key, "occurrence_id": occurrence["occurrence_id"], "new_message": new_message,
                    "dedup_basis": 'new' if new_message else 'exact_bytes' if existing['raw_sha256'] == raw_sha else 'transport_copy'}

    @staticmethod
    def _header(parsed, name):
        return (parsed.headers_raw.get(name) or [None])[0]

    @staticmethod
    def _language(value):
        thai = sum(1 for char in value if "\u0e00" <= char <= "\u0e7f")
        chinese = sum(1 for char in value if "\u4e00" <= char <= "\u9fff")
        letters = sum(1 for char in value if char.isalpha())
        if thai > 8 and thai >= chinese:
            return "th"
        if chinese > 8:
            return "zh"
        if letters:
            return "und"
        return "unknown"

    def _insert_participants(self, connection, message_key, participants):
        for role, raw_address, position, display_name in participants:
            normalized = normalize_address(raw_address)
            if not normalized:
                continue
            row = connection.execute("SELECT address_id FROM mail_addresses WHERE normalized_address=?", (normalized,)).fetchone()
            address_id = row["address_id"] if row else _id("ma")
            if not row:
                connection.execute("INSERT INTO mail_addresses(address_id,normalized_address,raw_address_sample,display_name_sample) VALUES (?,?,?,?)", (address_id, normalized, raw_address, display_name))
            connection.execute("INSERT OR IGNORE INTO mail_message_participants(message_key,address_id,role,position) VALUES (?,?,?,?)", (message_key, address_id, role, position))

    def _insert_parts(self, connection, message_key, parts):
        paths = {part.mime_path for part in parts}
        # Parser upgrades can stop exposing children inside an attached MIME
        # document. Retain historical evidence, but retire obsolete projections
        # from current attachment counts and browsing.
        for row in connection.execute('SELECT mime_path FROM mail_parts WHERE message_key=?', (message_key,)).fetchall():
            if row['mime_path'] not in paths:
                connection.execute("UPDATE mail_parts SET part_role='retired',is_explicit_attachment=0,is_inline_resource=0 WHERE message_key=? AND mime_path=?", (message_key, row['mime_path']))
        for part in parts:
            connection.execute(
                "INSERT INTO mail_parts(part_id,message_key,mime_path,parent_mime_path,content_type,content_disposition,content_id,filename_raw,transfer_encoding,byte_size,payload_sha256,part_role,is_explicit_attachment,is_inline_resource,locator_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(message_key,mime_path) DO UPDATE SET content_type=excluded.content_type,content_disposition=excluded.content_disposition,filename_raw=excluded.filename_raw,byte_size=excluded.byte_size,payload_sha256=excluded.payload_sha256,part_role=excluded.part_role,is_explicit_attachment=excluded.is_explicit_attachment,is_inline_resource=excluded.is_inline_resource",
                (_id("mpart"), message_key, part.mime_path, part.parent_mime_path, part.content_type, part.content_disposition, part.content_id, part.filename_raw, part.transfer_encoding, part.byte_size, part.payload_sha256, part.part_role, int(part.is_explicit_attachment), int(part.is_inline_resource), _json({})),
            )

    def _insert_body_segments(self, connection, package_id, message_key, parts, revision):
        length = self.text_segment_chars
        # Plain and HTML are alternate representations, not duplicate messages.
        selected = [p for p in parts if p.part_role == 'body_plain' and (p.text or '').strip()]
        if not selected:
            selected = [p for p in parts if p.part_role == 'body_html' and (p.text or '').strip()]
        for part in parts:
            if part not in selected:
                continue
            text = part.text
            for ordinal, start in enumerate(range(0, len(text), length)):
                fragment = text[start:start + length]
                segment_id = 'mseg_' + hashlib.sha256(_json([package_id,message_key,part.mime_path,'body',ordinal,fragment]).encode()).hexdigest()
                connection.execute(
                    "INSERT OR IGNORE INTO mail_text_segments(segment_id,package_id,message_key,mime_path,source_kind,language,ordinal,page_number,sheet_name,char_start,char_end,text,text_sha256,source_revision,index_epoch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (segment_id, package_id, message_key, part.mime_path, "body", self._language(fragment), ordinal, None, None, start, start + len(fragment), fragment, hashlib.sha256(fragment.encode("utf-8")).hexdigest(), revision, 0),
                )
                if not connection.execute('SELECT 1 FROM mail_search_fts WHERE segment_id=?',(segment_id,)).fetchone():
                    connection.execute("INSERT INTO mail_search_fts(segment_id,package_id,message_key,mime_path,field,language,text) VALUES (?,?,?,?,?,?,?)", (segment_id, package_id, message_key, part.mime_path, "body", self._language(fragment), fragment))

    def _copy_segments_to_package(self, connection, package_id, message_key):
        """Copy existing derived text into a package-local search projection."""
        package = connection.execute(
            "SELECT source_revision,index_epoch FROM mail_packages WHERE package_id=?",
            (package_id,),
        ).fetchone()
        if not package:
            return 0
        rows = connection.execute(
            "SELECT DISTINCT mime_path,source_kind,language,ordinal,page_number,sheet_name,cell_range,"
            "char_start,char_end,text,text_sha256 FROM mail_text_segments "
            "WHERE message_key=? AND package_id=(SELECT package_id FROM mail_text_segments WHERE message_key=? AND package_id<>? ORDER BY package_id LIMIT 1) ORDER BY source_kind,mime_path,ordinal",
            (message_key, message_key, package_id),
        ).fetchall()
        copied = 0
        for row in rows:
            identity=[package_id,message_key,row['mime_path'],row['source_kind'],row['ordinal']]
            if row['source_kind']=='attachment':identity.extend([row['page_number'],row['sheet_name'],row['cell_range']])
            identity.append(row['text'])
            segment_id='mseg_'+hashlib.sha256(_json(identity).encode()).hexdigest()
            connection.execute(
                "INSERT INTO mail_text_segments(segment_id,package_id,message_key,mime_path,source_kind,language,ordinal,page_number,sheet_name,cell_range,char_start,char_end,text,text_sha256,source_revision,index_epoch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (segment_id, package_id, message_key, row["mime_path"], row["source_kind"], row["language"], row["ordinal"], row["page_number"], row["sheet_name"], row["cell_range"], row["char_start"], row["char_end"], row["text"], row["text_sha256"], package["source_revision"], package["index_epoch"]),
            )
            connection.execute(
                "INSERT INTO mail_search_fts(segment_id,package_id,message_key,mime_path,field,language,text) VALUES (?,?,?,?,?,?,?)",
                (segment_id, package_id, message_key, row["mime_path"], row["source_kind"], row["language"], row["text"]),
            )
            copied += 1
        return copied

    def finish_import(self, package_id, owner_id):
        self.rebuild_relationships(package_id, owner_id)
        self.update_package(package_id, owner_id, status="basic_index_ready", error=None, imported=True, counts=True)
        if Config.MAIL_ENABLE_GRAPH_METRICS:
            with self._connection() as c:
                c.execute("DELETE FROM mail_communication_daily WHERE package_id=?",(package_id,))
                c.execute("INSERT INTO mail_communication_daily SELECT package_id,substr(sent_at_utc,1,10),from_address_id,to_address_id,recipient_role,COUNT(*),SUM(occurrence_count) FROM mail_communication_edges WHERE package_id=? AND sent_at_utc IS NOT NULL GROUP BY package_id,substr(sent_at_utc,1,10),from_address_id,to_address_id,recipient_role",(package_id,))
        if Config.MAIL_ENABLE_CONVERSATIONS:
            from .conversations import rebuild
            rebuild(self, package_id, owner_id)
        if Config.MAIL_ENABLE_ATTACHMENT_VERSIONS:
            from .versions import rebuild
            rebuild(self, package_id, owner_id)

    def rebuild_relationships(self, package_id, owner_id):
        from .threading import rebuild_relationships
        return rebuild_relationships(self, package_id, owner_id)

    def overview(self, package_id, owner_id):
        package = self.get_package(package_id, owner_id)
        if not package:
            return None
        with self._connection() as connection:
            values = connection.execute(
                "SELECT COUNT(DISTINCT o.message_key) logical_count,COUNT(DISTINCT o.occurrence_id) occurrence_count,MIN(m.sent_at_utc) first_sent,MAX(m.sent_at_utc) last_sent,COUNT(DISTINCT CASE WHEN m.body_preferred_part IS NOT NULL THEN m.message_key END) body_ready,COUNT(DISTINCT CASE WHEN mp.role='from' THEN mp.address_id END) sender_count,COUNT(DISTINCT mp.address_id) participant_count FROM mail_visible_occurrences o JOIN mail_messages m ON m.message_key=o.message_key LEFT JOIN mail_message_participants mp ON mp.message_key=m.message_key WHERE o.package_id=?", (package_id,)).fetchone()
            attachment = connection.execute("SELECT COUNT(DISTINCT p.message_key || char(0) || p.mime_path) value FROM mail_parts p JOIN mail_visible_occurrences o ON o.message_key=p.message_key WHERE o.package_id=? AND p.is_explicit_attachment=1", (package_id,)).fetchone()["value"]
            attachment_occurrences = connection.execute("SELECT COUNT(*) value FROM mail_parts p JOIN mail_visible_occurrences o ON o.message_key=p.message_key WHERE o.package_id=? AND p.is_explicit_attachment=1", (package_id,)).fetchone()["value"]
            inline_occurrences = connection.execute("SELECT COUNT(*) value FROM mail_parts p JOIN mail_visible_occurrences o ON o.message_key=p.message_key WHERE o.package_id=? AND p.is_inline_resource=1", (package_id,)).fetchone()["value"]
            body_occurrences = connection.execute("SELECT COUNT(*) value FROM mail_visible_occurrences o JOIN mail_messages m ON m.message_key=o.message_key WHERE o.package_id=? AND m.body_preferred_part IS NOT NULL", (package_id,)).fetchone()["value"]
            processing = connection.execute(
                "SELECT x.status,COUNT(DISTINCT x.message_key || char(0) || x.mime_path) value FROM mail_part_processing x "
                "JOIN mail_visible_occurrences o ON o.message_key=x.message_key "
                "WHERE o.package_id=? GROUP BY x.status", (package_id,)
            ).fetchall()
            thread = connection.execute("SELECT status,COUNT(*) value FROM mail_thread_edges WHERE package_id=? GROUP BY status", (package_id,)).fetchall()
            thread_relation = connection.execute("SELECT relation_type,status,COUNT(*) value FROM mail_thread_edges WHERE package_id=? GROUP BY relation_type,status", (package_id,)).fetchall()
        return {"package": package, "counts": {"logical_messages": int(values["logical_count"] or 0), "occurrences": int(values["occurrence_count"] or 0), "messages_with_body": int(values["body_ready"] or 0), "occurrences_with_body": int(body_occurrences or 0), "explicit_attachments": int(attachment or 0), "explicit_attachment_occurrences": int(attachment_occurrences or 0), "inline_resource_occurrences": int(inline_occurrences or 0), "sender_addresses": int(values["sender_count"] or 0), "participant_addresses": int(values["participant_count"] or 0)}, "time_range": {"from": values["first_sent"], "to": values["last_sent"]}, "processing": {item["status"]: item["value"] for item in processing}, "thread_edges": {item["status"]: item["value"] for item in thread}, "thread_relations": {"{}:{}".format(item["relation_type"], item["status"]): item["value"] for item in thread_relation}}

    def list_messages(self, package_id, owner_id, *, offset=0, limit=100, query="", participant=None, date_from=None, date_to=None, has_attachment=None):
        if not self.get_package(package_id, owner_id):
            raise ValueError("邮件资料包不存在或不属于当前用户")
        from .people import filtered_messages
        scope,args,filters=filtered_messages(package_id,date_from=date_from,date_to=date_to,q=query,has_attachment=has_attachment,participant=participant)
        limit=max(1,min(200,int(limit))); offset=max(0,int(offset))
        with self._connection() as c:
            total=c.execute("SELECT COUNT(*) FROM ("+scope+")",args).fetchone()[0]
            from .status import message_status_columns
            rows=c.execute("SELECT m.message_key,m.subject_raw,m.date_raw,m.sent_at_utc,m.parse_status,m.body_language,"+message_status_columns()+",(SELECT COUNT(*) FROM mail_parts p WHERE p.message_key=m.message_key AND p.is_explicit_attachment=1) attachment_count,(SELECT GROUP_CONCAT(a.normalized_address, ', ') FROM mail_message_participants p JOIN mail_addresses a ON a.address_id=p.address_id WHERE p.message_key=m.message_key AND p.role='from') sender FROM mail_messages m WHERE m.message_key IN ("+scope+") ORDER BY m.sent_at_utc IS NULL,julianday(m.sent_at_utc) DESC,m.message_key LIMIT ? OFFSET ?",args+[limit,offset]).fetchall()
        return {"items":[dict(r) for r in rows],"total":total,"offset":offset,"limit":limit,"scope":filters}

    def get_message(self, message_key, owner_id, package_id=None):
        with self._connection() as connection:
            clauses = ["m.message_key=?", "p.owner_id=?"]
            args = [message_key, owner_id]
            if package_id:
                clauses.append("o.package_id=?")
                args.append(package_id)
            message = connection.execute("SELECT DISTINCT m.*,o.package_id FROM mail_messages m JOIN mail_occurrences o ON o.message_key=m.message_key JOIN mail_packages p ON p.package_id=o.package_id WHERE " + " AND ".join(clauses), args).fetchone()
            if not message:
                return None
            details = _row(message)
            details["headers"] = _load(details.pop("headers_json"))
            details["warnings"] = _load(details.pop("warnings_json"), [])
            details["participants"] = [_row(item) for item in connection.execute("SELECT a.normalized_address,a.raw_address_sample,a.display_name_sample,p.role,p.position FROM mail_message_participants p JOIN mail_addresses a ON a.address_id=p.address_id WHERE p.message_key=? ORDER BY p.role,p.position", (message_key,)).fetchall()]
            details["parts"] = [_row(item) for item in connection.execute(
                "SELECT p.mime_path,p.parent_mime_path,p.content_type,p.content_disposition,p.content_id,"
                "p.filename_raw,p.byte_size,p.payload_sha256,p.part_role,p.is_explicit_attachment,p.is_inline_resource,"
                "COALESCE(x.status,'not_started') AS processing_status,x.retry_count,x.next_retry_at,x.error_json,x.warnings_json,x.coverage_ratio,x.extracted_chars "
                "FROM mail_parts p LEFT JOIN mail_part_processing x ON x.message_key=p.message_key "
                "AND x.mime_path=p.mime_path AND x.stage='basic_text' WHERE p.message_key=? AND p.part_role!='retired' ORDER BY p.mime_path",
                (message_key,),
            ).fetchall()]
            for part in details['parts']:
                part['error']=_load(part.pop('error_json',None),None)
                part['warnings'],part['text_provenance']=decode_processing_warnings(part.pop('warnings_json',None))
                part['processing']=[dict(r) for r in connection.execute('SELECT stage,status,parser_name,parser_version,retry_count,next_retry_at,coverage_ratio,extracted_chars,warnings_json,error_json FROM mail_part_processing WHERE message_key=? AND mime_path=? ORDER BY stage',(message_key,part['mime_path']))]
                for state in part['processing']:
                    state['warnings'],state['text_provenance']=decode_processing_warnings(state.pop('warnings_json'))
                    state['error']=_load(state.pop('error_json'),None)
                basic=next((state for state in part['processing'] if state['stage']=='basic_text'),{})
                part['text_origin']=part['text_provenance'].get('text_origin') or legacy_origin(basic.get('parser_name'))
                if part['text_origin'] in {'ocr','mixed'}:part['source_text_notice']=OCR_NOTICE
            details["occurrences"] = [_row(item) for item in connection.execute("SELECT occurrence_id,source_member_name,source_member_ordinal,raw_sha256,is_countable,imported_at FROM mail_occurrences WHERE message_key=? AND package_id=? ORDER BY source_member_ordinal", (message_key, details["package_id"])).fetchall()]
            if details['occurrences'] and not any(o['is_countable'] for o in details['occurrences']):
                details['warnings'].append('此邮件按嵌套来源保留，默认不作为独立邮件计数；旧研究引用仍可回查')
        return details

    def message_source(self, message_key, owner_id, package_id):
        with self._connection() as connection:
            row = connection.execute("SELECT s.locator_json,s.locator_type,s.root_path,s.container_path,s.source_revision FROM mail_occurrences o JOIN mail_sources s ON s.source_id=o.source_id JOIN mail_packages p ON p.package_id=o.package_id WHERE o.message_key=? AND o.package_id=? AND p.owner_id=? ORDER BY o.source_member_ordinal LIMIT 1", (message_key, package_id, owner_id)).fetchone()
        if not row:
            return None
        value = _row(row)
        value["locator"] = _load(value.pop("locator_json"))
        return value

    def attachments(self, package_id, owner_id, *, offset=0, limit=100, include_inline=False):
        if not self.get_package(package_id, owner_id):
            raise ValueError("邮件资料包不存在或不属于当前用户")
        role_clause = "p.is_explicit_attachment=1" if not include_inline else "(p.is_explicit_attachment=1 OR p.is_inline_resource=1)"
        with self._connection() as connection:
            total = connection.execute("SELECT COUNT(DISTINCT p.message_key || char(0) || p.mime_path) value FROM mail_parts p JOIN mail_visible_occurrences o ON o.message_key=p.message_key WHERE o.package_id=? AND " + role_clause, (package_id,)).fetchone()["value"]
            rows = connection.execute("SELECT p.message_key,p.mime_path,p.filename_raw,p.content_type,p.byte_size,p.is_explicit_attachment,p.is_inline_resource,m.subject_raw,m.sent_at_utc,COALESCE(x.status,'not_started') processing_status FROM mail_parts p JOIN mail_visible_occurrences o ON o.message_key=p.message_key JOIN mail_messages m ON m.message_key=p.message_key LEFT JOIN mail_part_processing x ON x.message_key=p.message_key AND x.mime_path=p.mime_path AND x.stage='basic_text' WHERE o.package_id=? AND " + role_clause + " GROUP BY p.message_key,p.mime_path ORDER BY m.sent_at_utc DESC LIMIT ? OFFSET ?", (package_id, limit, offset)).fetchall()
        return {"items": [_row(item) for item in rows], "total": int(total), "offset": offset, "limit": limit}

    def attachment_segments(self, package_id, owner_id, message_key, mime_path, *, limit=50, offset=0):
        if not self.get_message(message_key, owner_id, package_id):
            raise ValueError("附件邮件不存在或不属于当前资料包")
        with self._connection() as c:
            rows=c.execute("SELECT segment_id,mime_path,page_number,sheet_name,cell_range,char_start,char_end,text FROM mail_text_segments WHERE package_id=? AND message_key=? AND mime_path=? AND source_kind='attachment' ORDER BY ordinal LIMIT ? OFFSET ?",(package_id,message_key,mime_path,max(1,min(200,int(limit))),max(0,int(offset)))).fetchall()
            state=c.execute("SELECT parser_name,warnings_json FROM mail_part_processing WHERE message_key=? AND mime_path=? AND stage='basic_text'",(message_key,mime_path)).fetchone()
            _,metadata=decode_processing_warnings(state['warnings_json'] if state else None)
            return [{**dict(row),**provenance_for_unit(metadata,dict(row),state['parser_name'] if state else None)} for row in rows]

    def schedule_attachment_upgrade(self, package_id, owner_id):
        """Upgrade legacy extraction once; keep its readable projection until replacement."""
        if not self.get_package(package_id, owner_id):
            raise ValueError("邮件资料包不存在或不属于当前用户")
        with self._connection() as c:
            result=c.execute("UPDATE mail_part_processing SET status='pending',parser_version='mail-extract-v2',retry_count=0,next_retry_at=NULL,error_json=NULL,warnings_json=? WHERE stage='basic_text' AND COALESCE(parser_version,'')!='mail-extract-v2' AND EXISTS(SELECT 1 FROM mail_visible_occurrences o JOIN mail_parts p ON p.message_key=o.message_key WHERE o.package_id=? AND p.message_key=mail_part_processing.message_key AND p.mime_path=mail_part_processing.mime_path AND p.is_explicit_attachment=1)",(_json(['解析器升级排队中；旧文本保留至新版抽取成功']),package_id))
            return result.rowcount

    def pending_attachments(self, package_id, owner_id, *, offset=0, limit=50):
        if not self.get_package(package_id, owner_id):
            raise ValueError("邮件资料包不存在或不属于当前用户")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT p.message_key,p.mime_path,p.filename_raw,p.content_type FROM mail_parts p "
                "JOIN mail_visible_occurrences o ON o.message_key=p.message_key "
                "LEFT JOIN mail_part_processing x ON x.message_key=p.message_key AND x.mime_path=p.mime_path AND x.stage='basic_text' "
                "WHERE o.package_id=? AND p.is_explicit_attachment=1 AND (x.status IS NULL OR x.status='pending' OR (x.status IN ('failed','waiting_retry') AND x.retry_count<3 AND (x.next_retry_at IS NULL OR x.next_retry_at<=CURRENT_TIMESTAMP))) "
                "GROUP BY p.message_key,p.mime_path ORDER BY p.message_key,p.mime_path LIMIT ? OFFSET ?", (package_id, limit, offset)
            ).fetchall()
        return [_row(row) for row in rows]

    def communication(self, package_id, owner_id, *, count_basis="logical", date_from=None, date_to=None, roles=None, q='', has_attachment=None, participant=None):
        if count_basis not in {"logical", "occurrence"}:
            raise ValueError("count_basis 仅支持 logical 或 occurrence")
        if not self.get_package(package_id, owner_id):
            raise ValueError("邮件资料包不存在或不属于当前用户")
        clauses = ["e.package_id=?"]
        args = [package_id]
        from .people import filtered_messages
        scope,scope_args,filters=filtered_messages(package_id,date_from=date_from,date_to=date_to,q=q,has_attachment=has_attachment,participant=participant)
        clauses.append('e.message_key IN ('+scope+')');args.extend(scope_args)
        valid_roles = [role for role in (roles or ["to", "cc", "bcc"]) if role in {"to", "cc", "bcc"}]
        if valid_roles:
            clauses.append("e.recipient_role IN ({})".format(",".join("?" for _ in valid_roles))); args.extend(valid_roles)
        count_expression = "COUNT(DISTINCT e.message_key)" if count_basis == "logical" else "SUM(e.occurrence_count)"
        with self._connection() as connection:
            rows = connection.execute("SELECT af.normalized_address AS from_address,at.normalized_address AS to_address,e.recipient_role," + count_expression + " AS message_count,MIN(e.sent_at_utc) AS first_sent,MAX(e.sent_at_utc) AS last_sent FROM mail_communication_edges e JOIN mail_addresses af ON af.address_id=e.from_address_id JOIN mail_addresses at ON at.address_id=e.to_address_id WHERE " + " AND ".join(clauses) + " GROUP BY e.from_address_id,e.to_address_id,e.recipient_role ORDER BY message_count DESC,from_address,to_address", args).fetchall()
        return {"count_basis": count_basis, "roles": valid_roles, "items": [_row(item) for item in rows]}

    def threads(self, package_id, owner_id, *, status=None, relation_type=None, date_from=None, date_to=None, q='', has_attachment=None, participant=None):
        if not self.get_package(package_id, owner_id):
            raise ValueError("邮件资料包不存在或不属于当前用户")
        clauses = ["e.package_id=?"]
        args = [package_id]
        from .people import filtered_messages
        scope,scope_args,filters=filtered_messages(package_id,date_from=date_from,date_to=date_to,q=q,has_attachment=has_attachment,participant=participant)
        clauses.append('e.source_message_key IN ('+scope+')');args.extend(scope_args)
        if status:
            clauses.append("e.status=?"); args.append(status)
        if relation_type:
            if relation_type not in {"reply_to", "reference", "reference_parent", "subject"}:
                raise ValueError("未知的回复关系类型")
            clauses.append("e.relation_type=?"); args.append(relation_type)
        with self._connection() as connection:
            rows = connection.execute("SELECT e.*,s.subject_raw source_subject,t.subject_raw target_subject FROM mail_thread_edges e JOIN mail_messages s ON s.message_key=e.source_message_key LEFT JOIN mail_messages t ON t.message_key=e.target_message_key WHERE " + " AND ".join(clauses) + " ORDER BY e.created_at", args).fetchall()
        result=[_row(item) for item in rows]
        for edge in result:edge['evidence']=_load(edge['evidence_json'])
        return result

    def replace_turn_relations(self, package_id, owner_id, turn_id, relations):
        """Replace one research turn's validated relations so retries stay idempotent."""
        if not self.get_package(package_id, owner_id):
            raise ValueError("邮件资料包不存在或不属于当前用户")
        created_by = "turn:{}".format(turn_id)
        accepted = []
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM mail_business_relations WHERE package_id=? AND created_by=?",
                (package_id, created_by),
            )
            for relation in relations or []:
                evidence_ids = []
                for evidence_id in relation.get("evidence_ids") or []:
                    row = connection.execute(
                        "SELECT evidence_id FROM mail_evidence WHERE evidence_id=? AND package_id=?",
                        (evidence_id, package_id),
                    ).fetchone()
                    if row and evidence_id not in evidence_ids:
                        evidence_ids.append(evidence_id)
                if not evidence_ids:
                    continue
                confidence = relation.get("confidence")
                try:
                    confidence = max(0.0, min(1.0, float(confidence)))
                except (TypeError, ValueError):
                    confidence = 0.75 if relation.get("state") == "asserted" else 0.45
                value = {
                    "relation_id": _id("mbr"),
                    "package_id": package_id,
                    "subject_ref": str(relation.get("subject_ref") or "")[:240],
                    "predicate": str(relation.get("predicate") or "")[:64],
                    "object_ref": str(relation.get("object_ref") or "")[:500],
                    "state": str(relation.get("state") or "inferred")[:32],
                    "confidence": confidence,
                    "method": str(relation.get("method") or "agent_evidence_validated")[:128],
                    "evidence_ids": evidence_ids,
                    "model_version": str(relation.get("model_version") or "")[:200] or None,
                    "created_by": created_by,
                }
                if not value["subject_ref"] or not value["object_ref"]:
                    continue
                connection.execute(
                    "INSERT INTO mail_business_relations(relation_id,package_id,subject_ref,predicate,object_ref,state,confidence,method,evidence_ids_json,model_version,created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (value["relation_id"], value["package_id"], value["subject_ref"], value["predicate"], value["object_ref"], value["state"], value["confidence"], value["method"], _json(value["evidence_ids"]), value["model_version"], value["created_by"]),
                )
                accepted.append(value)
        return accepted

    def record_claims(self, turn_id, claims):
        """Keep claim calibration beside the turn even when it is not a graph edge."""
        values = []
        with self._connection() as connection:
            connection.execute("DELETE FROM mail_claims WHERE turn_id=?", (turn_id,))
            for claim in claims or []:
                text = str(claim.get("claim_text") or "").strip()[:1000]
                if not text:
                    continue
                value = {
                    "claim_id": _id("mcl"),
                    "claim_text": text,
                    "claim_type": str(claim.get("predicate") or "statement")[:64],
                    "state": str(claim.get("state") or "insufficient")[:32],
                    "support_status": str(claim.get("support_status") or "missing_evidence")[:64],
                    "evidence_ids": list(claim.get("evidence_ids") or [])[:8],
                    "counter_evidence_ids": list(claim.get("counter_evidence_ids") or [])[:8],
                    "verification": dict(claim.get("verification") or {}),
                }
                connection.execute(
                    "INSERT INTO mail_claims(claim_id,turn_id,claim_text,claim_type,state,support_status,evidence_ids_json,counter_evidence_ids_json,verification_json) VALUES (?,?,?,?,?,?,?,?,?)",
                    (value["claim_id"], turn_id, value["claim_text"], value["claim_type"], value["state"], value["support_status"], _json(value["evidence_ids"]), _json(value["counter_evidence_ids"]), _json(value["verification"])),
                )
                values.append(value)
        return values

    def business_relations(self, package_id, owner_id, *, include_unreviewed=False):
        """Return only persisted, evidence-carrying business assertions."""
        if not self.get_package(package_id, owner_id):
            raise ValueError("邮件资料包不存在或不属于当前用户")
        query = (
            "SELECT relation_id,subject_ref,predicate,object_ref,state,confidence,method,"
            "evidence_ids_json,model_version,created_by,created_at,reviewed_by,reviewed_at,review_note "
            "FROM mail_business_relations WHERE package_id=?"
        )
        if not include_unreviewed:
            query += " AND state IN ('asserted','confirmed')"
        query += " ORDER BY created_at DESC,relation_id"
        with self._connection() as connection:
            rows = connection.execute(
                query,
                (package_id,),
            ).fetchall()
        values = []
        for row in rows:
            value = _row(row)
            value["evidence_ids"] = _load(value.pop("evidence_ids_json"), [])
            values.append(value)
        return values

    def search_segments(self, package_id, owner_id, query, *, attachment_only=False, limit=50):
        if not Config.MAIL_ENABLE_VECTOR_INDEX:
            return self.search_segments_lexical(package_id, owner_id, query, attachment_only=attachment_only, limit=limit)
        from .retrieval import hybrid_search
        return hybrid_search(self, package_id, owner_id, query, attachment_only=attachment_only, limit=limit)

    def search_segments_lexical(self, package_id, owner_id, query, *, attachment_only=False, limit=50):
        if not self.get_package(package_id, owner_id):
            raise ValueError("邮件资料包不存在或不属于当前用户")
        query = str(query or "").strip()
        if not query:
            return {"items": [], "coverage": self.coverage(package_id, owner_id)}
        role_clause = "s.source_kind='attachment'" if attachment_only else "1=1"
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        # A quoted phrase keeps user text data rather than FTS syntax.  Some
        # scripts and substring queries are not tokenized by unicode61, so a
        # LIKE fallback remains necessary for complete lexical recall.
        fts_query = '"{}"'.format(query.replace('"', '""'))
        with self._connection() as connection:
            try:
                rows = connection.execute(
                    "SELECT s.segment_id,s.message_key,s.mime_path,s.source_kind,s.language,"
                    "s.page_number,s.sheet_name,s.char_start,s.char_end,s.text,m.subject_raw,m.sent_at_utc "
                    "FROM mail_search_fts JOIN mail_text_segments s ON s.segment_id=mail_search_fts.segment_id "
                    "JOIN mail_messages m ON m.message_key=s.message_key "
                    "WHERE mail_search_fts.package_id=? AND mail_search_fts MATCH ? AND " + role_clause +
                    " ORDER BY bm25(mail_search_fts),m.sent_at_utc DESC LIMIT ?",
                    (package_id, fts_query, limit),
                ).fetchall()
            except sqlite3.Error:
                rows = []
            if not rows:
                rows = connection.execute(
                    "SELECT s.segment_id,s.message_key,s.mime_path,s.source_kind,s.language,"
                    "s.page_number,s.sheet_name,s.char_start,s.char_end,s.text,m.subject_raw,m.sent_at_utc "
                    "FROM mail_text_segments s JOIN mail_messages m ON m.message_key=s.message_key "
                    "WHERE s.package_id=? AND " + role_clause +
                    " AND s.text LIKE ? ESCAPE '\\' ORDER BY m.sent_at_utc DESC LIMIT ?",
                    (package_id, like, limit),
                ).fetchall()
            if not rows:
                terms = []
                for term in re.findall(r"[^\s,.;:!?，。；：！？]{2,}", query):
                    if term not in terms:
                        terms.append(term)
                if len(terms) > 1:
                    clauses = " OR ".join("s.text LIKE ? ESCAPE '\\'" for _ in terms[:12])
                    term_values = [
                        "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                        for term in terms[:12]
                    ]
                    rows = connection.execute(
                        "SELECT s.segment_id,s.message_key,s.mime_path,s.source_kind,s.language,"
                        "s.page_number,s.sheet_name,s.char_start,s.char_end,s.text,m.subject_raw,m.sent_at_utc "
                        "FROM mail_text_segments s JOIN mail_messages m ON m.message_key=s.message_key "
                        "WHERE s.package_id=? AND " + role_clause + " AND (" + clauses + ") "
                        "ORDER BY m.sent_at_utc DESC LIMIT ?",
                        [package_id] + term_values + [limit],
                    ).fetchall()
            if not rows:
                rows = self._search_across_segment_boundaries(
                    connection, package_id, query, attachment_only, limit
                )
        if Config.MAIL_ENABLE_AGENT_MINING:
            with self._connection() as connection:
                metadata=connection.execute(
                    "SELECT DISTINCT s.*,m.subject_raw,m.sent_at_utc FROM mail_text_segments s JOIN mail_messages m ON m.message_key=s.message_key "
                    "WHERE s.package_id=? AND "+role_clause+" AND (m.subject_raw LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM mail_parts p WHERE p.message_key=s.message_key AND p.filename_raw LIKE ? ESCAPE '\\')) ORDER BY m.sent_at_utc DESC LIMIT ?",
                    (package_id,like,like,limit)).fetchall()
            seen={row["segment_id"] for row in rows}
            rows=list(rows)+[r for r in metadata if r["segment_id"] not in seen]
            rows=rows[:limit]
        items = []
        for row in rows:
            item = _row(row)
            evidence = self.create_evidence(package_id, owner_id, item)
            item["evidence_id"] = evidence["evidence_id"]
            item["quote_text"] = evidence["quote_text"]
            items.append(item)
        return {"items": items, "coverage": self.coverage(package_id, owner_id)}

    @staticmethod
    def _search_across_segment_boundaries(connection, package_id, query, attachment_only, limit):
        """Find exact lexical matches split by the configured segment boundary.

        FTS and ordinary LIKE queries only see individual index rows.  Joining
        adjacent rows here is a rare fallback, so normal searches keep their
        indexed path while a Thai word or a long attachment term is not lost
        merely because it straddles a segment boundary.
        """
        role_clause = "AND s.source_kind='attachment'" if attachment_only else ""
        rows = connection.execute(
            "SELECT s.segment_id,s.message_key,s.mime_path,s.source_kind,s.language,"
            "s.page_number,s.sheet_name,s.ordinal,s.char_start,s.char_end,s.text,"
            "m.subject_raw,m.sent_at_utc "
            "FROM mail_text_segments s JOIN mail_messages m ON m.message_key=s.message_key "
            "WHERE s.package_id=? " + role_clause + " "
            "ORDER BY m.sent_at_utc DESC,s.message_key,s.mime_path,s.source_kind,"
            "s.page_number,s.sheet_name,s.ordinal",
            (package_id,),
        ).fetchall()
        groups = {}
        for row in rows:
            key = (
                row["message_key"], row["mime_path"], row["source_kind"],
                row["page_number"], row["sheet_name"],
            )
            groups.setdefault(key, []).append(row)
        matches = []
        needle = query.casefold()
        for segments in groups.values():
            # Separate pages or parser-generated discontinuities rather than
            # fabricating evidence that joins unrelated source text.
            contiguous = []
            previous = None
            for row in segments:
                if previous is not None and (
                    row["ordinal"] != previous["ordinal"] + 1
                    or row["char_start"] != previous["char_end"]
                ):
                    contiguous = []
                contiguous.append(row)
                previous = row
            if len(contiguous) < 2:
                continue
            text = "".join(str(row["text"] or "") for row in contiguous)
            match_start = text.casefold().find(needle)
            if match_start < 0:
                continue
            match_end = match_start + len(query)
            context_start = max(0, match_start - 200)
            context_end = min(len(text), match_end + 800)
            consumed = 0
            anchor = contiguous[0]
            for row in contiguous:
                next_consumed = consumed + len(str(row["text"] or ""))
                if match_start < next_consumed:
                    anchor = row
                    break
                consumed = next_consumed
            item = dict(anchor)
            item["text"] = text[context_start:context_end]
            item["char_start"] = contiguous[0]["char_start"] + context_start
            item["char_end"] = contiguous[0]["char_start"] + context_end
            matches.append(item)
            if len(matches) >= limit:
                break
        return matches

    def create_evidence(self, package_id, owner_id, segment):
        return self.create_evidence_batch(package_id, owner_id, [segment])[0]

    def create_evidence_batch(self, package_id, owner_id, segments):
        package = self.get_package(package_id, owner_id)
        if not package:
            raise ValueError("邮件资料包不存在或不属于当前用户")
        # Bounded transactions preserve ownership and original provenance rules.
        values = []
        for offset in range(0, len(segments), 100):
            with self._connection() as connection:
                for segment in segments[offset:offset + 100]:
                    values.append(self._create_evidence(connection, package_id, package, segment))
        return values

    def _create_evidence(self, connection, package_id, package, segment):
        occurrence = connection.execute("SELECT occurrence_id,raw_sha256 FROM mail_visible_occurrences WHERE package_id=? AND message_key=? ORDER BY source_member_ordinal LIMIT 1", (package_id, segment["message_key"])).fetchone()
        evidence_id = _id("mev")
        text = str(segment.get("text") or "")
        if not occurrence:
            raise ValueError("证据邮件不在当前资料包")
        quote = text[:2000] if Config.MAIL_ENABLE_AGENT_MINING else text[:1000]
        connection.execute("INSERT INTO mail_evidence(evidence_id,package_id,message_key,occurrence_id,mime_path,segment_id,page_number,sheet_name,char_start,char_end,quote_text,source_revision,index_epoch,source_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (evidence_id, package_id, segment["message_key"], occurrence["occurrence_id"] if occurrence else None, segment["mime_path"], segment.get("segment_id"), segment.get("page_number"), segment.get("sheet_name"), segment.get("char_start"), (segment.get("char_start") or 0) + len(quote), quote, package["source_revision"], package["index_epoch"], occurrence["raw_sha256"] if occurrence else ""))
        part=connection.execute('SELECT payload_sha256 FROM mail_parts WHERE message_key=? AND mime_path=?',(segment['message_key'],segment['mime_path'])).fetchone()
        connection.execute('UPDATE mail_evidence SET cell_range=?,payload_sha256=?,text_sha256=? WHERE evidence_id=?',(segment.get('cell_range'),part[0] if part else None,segment.get('text_sha256') or hashlib.sha256(text.encode()).hexdigest(),evidence_id))
        provenance=self._text_provenance(connection,segment)
        return {"evidence_id": evidence_id, "quote_text": quote,**provenance}

    def _text_provenance(self,connection,segment):
        if segment.get('source_kind')=='body':return {'text_origin':'native','extraction_method':'mail-body'}
        part=connection.execute('SELECT is_explicit_attachment FROM mail_parts WHERE message_key=? AND mime_path=?',
            (segment['message_key'],segment['mime_path'])).fetchone()
        if not part:return {'text_origin':'unknown'}
        if not part[0]:return {'text_origin':'native','extraction_method':'mail-body'}
        state=connection.execute("SELECT parser_name,warnings_json FROM mail_part_processing WHERE message_key=? AND mime_path=? AND stage='basic_text'",
            (segment['message_key'],segment['mime_path'])).fetchone()
        _,metadata=decode_processing_warnings(state['warnings_json'] if state else None)
        return provenance_for_unit(metadata,segment,state['parser_name'] if state else None)

    def get_evidence(self, evidence_id, owner_id):
        with self._connection() as connection:
            row = connection.execute("SELECT e.* FROM mail_evidence e JOIN mail_packages p ON p.package_id=e.package_id WHERE e.evidence_id=? AND p.owner_id=?", (evidence_id, owner_id)).fetchone()
            if not row:return None
            value=dict(row)
            current=connection.execute('SELECT * FROM mail_text_segments WHERE segment_id=? AND package_id=?',
                (value.get('segment_id'),value['package_id'])).fetchone()
            if current and str(value['quote_text'] or '') in str(current['text'] or ''):
                value.update(self._text_provenance(connection,dict(current)))
            else:
                value.update(text_origin='unknown',source_text_notice='历史证据的文本来源版本无法与当前抽取结果对应，请回查保存的引文和原文件。')
            return value

    def get_text_segment(self, segment_id, owner_id, package_id):
        """Return a single package-scoped segment for a controlled mail tool."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT s.* FROM mail_text_segments s JOIN mail_packages p ON p.package_id=s.package_id "
                "WHERE s.segment_id=? AND s.package_id=? AND p.owner_id=?",
                (segment_id, package_id, owner_id),
            ).fetchone()
        return _row(row)

    def coverage(self, package_id, owner_id):
        if not self.get_package(package_id, owner_id):
            raise ValueError("邮件资料包不存在或不属于当前用户")
        with self._connection() as connection:
            counts = connection.execute("SELECT (SELECT COUNT(DISTINCT message_key) FROM mail_visible_occurrences WHERE package_id=?) AS messages,(SELECT COUNT(DISTINCT p.message_key || char(0) || p.mime_path) FROM mail_parts p JOIN mail_visible_occurrences o ON o.message_key=p.message_key WHERE o.package_id=? AND p.is_explicit_attachment=1) AS attachments,(SELECT COUNT(DISTINCT x.message_key || char(0) || x.mime_path) FROM mail_part_processing x JOIN mail_visible_occurrences o ON o.message_key=x.message_key WHERE o.package_id=? AND x.stage='basic_text' AND x.status IN ('ready','partial','ocr_pending') AND x.extracted_chars>0) AS extracted,(SELECT COUNT(DISTINCT x.message_key || char(0) || x.mime_path) FROM mail_part_processing x JOIN mail_visible_occurrences o ON o.message_key=x.message_key WHERE o.package_id=? AND x.stage='ocr' AND x.status IN ('pending','running')) AS ocr_pending,(SELECT COUNT(DISTINCT x.message_key || char(0) || x.mime_path) FROM mail_part_processing x JOIN mail_visible_occurrences o ON o.message_key=x.message_key WHERE o.package_id=? AND x.status IN ('failed','waiting_retry','terminal_failed')) AS failed", (package_id, package_id, package_id, package_id, package_id)).fetchone()
        result = {"logical_messages": int(counts["messages"] or 0), "explicit_attachments": int(counts["attachments"] or 0), "attachments_with_text": int(counts["extracted"] or 0), "ocr_pending": int(counts["ocr_pending"] or 0), "failed_parts": int(counts["failed"] or 0)}
        if Config.MAIL_ENABLE_AGENT_MINING:
            from .coverage import coverage_details
            details=coverage_details(self,package_id,owner_id,limit=100)
            result.update(attachments_without_text=details.get("attachments_without_text",details["total"]),gaps=details["items"],gap_counts=details["by_status"],fact_extraction=details["facts"])
            result["ocr_pending"]=max(result["ocr_pending"],details["by_status"].get("ocr_pending",0))
        return result

    def update_processing(self, message_key, mime_path, stage, status, *, parser_name=None, text=None, warnings=None, error=None, coverage_ratio=None, connection=None):
        if connection is None:
            with self._connection() as c:
                return self.update_processing(message_key,mime_path,stage,status,parser_name=parser_name,text=text,warnings=warnings,error=error,coverage_ratio=coverage_ratio,connection=c)
        old=connection.execute("SELECT retry_count FROM mail_part_processing WHERE message_key=? AND mime_path=? AND stage=?",(message_key,mime_path,stage)).fetchone()
        attempts=(old[0] if old else 0) + (1 if status not in {'pending','running','skipped'} else 0)
        next_retry=None
        if status == 'failed':
            if attempts >= 3: status='terminal_failed'
            else:
                status='waiting_retry'
                from datetime import datetime,timedelta,timezone
                next_retry=(datetime.now(timezone.utc)+timedelta(seconds=30*attempts)).strftime('%Y-%m-%d %H:%M:%S')
        connection.execute("INSERT INTO mail_part_processing(message_key,mime_path,stage,status,parser_name,parser_version,text_sha256,extracted_chars,coverage_ratio,warnings_json,started_at,finished_at,error_json,retry_count,next_retry_at) VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,?,?,?) ON CONFLICT(message_key,mime_path,stage) DO UPDATE SET status=excluded.status,parser_name=excluded.parser_name,parser_version=excluded.parser_version,text_sha256=excluded.text_sha256,extracted_chars=excluded.extracted_chars,coverage_ratio=excluded.coverage_ratio,warnings_json=excluded.warnings_json,finished_at=CURRENT_TIMESTAMP,error_json=excluded.error_json,retry_count=excluded.retry_count,next_retry_at=excluded.next_retry_at",(message_key,mime_path,stage,status,parser_name,'mail-extract-v2',hashlib.sha256((text or '').encode()).hexdigest() if text is not None else None,len(text or ''),coverage_ratio,_json(warnings or []),_json(error) if error else None,attempts,next_retry))
        return status

    def replace_attachment_segments(self, package_id, owner_id, message_key, mime_path, text, *, page_number=None, sheet_name=None, units=None, connection=None):
        if connection is None:
            if not self.get_message(message_key,owner_id,package_id):
                raise ValueError("附件邮件不存在或不属于当前资料包")
            with self._connection() as c:
                return self.replace_attachment_segments(package_id,owner_id,message_key,mime_path,text,page_number=page_number,sheet_name=sheet_name,units=units,connection=c)
        targets=connection.execute("SELECT DISTINCT p.package_id,p.source_revision,p.index_epoch FROM mail_packages p JOIN mail_visible_occurrences o ON o.package_id=p.package_id JOIN mail_parts part ON part.message_key=o.message_key WHERE o.message_key=? AND part.mime_path=? AND part.is_explicit_attachment=1",(message_key,mime_path)).fetchall()
        for target in targets:
            self._replace_attachment_segments_for_package(connection,target,message_key,mime_path,text,page_number=page_number,sheet_name=sheet_name,units=units)

    def store_extraction(self, package_id, owner_id, message_key, mime_path, result, *, stage='basic_text'):
        if not self.get_message(message_key,owner_id,package_id):
            raise ValueError("附件邮件不存在或不属于当前资料包")
        with self._connection() as c:
            result=dict(result)
            if stage=='basic_text' and not result.get('text') and result.get('status') not in {'ready','partial'}:
                # A normal attachment retry first probes the native PDF layer.
                # An empty probe must not relabel retained old OCR as native.
                previous=c.execute("SELECT parser_name,warnings_json FROM mail_part_processing WHERE message_key=? AND mime_path=? AND stage='basic_text'",(message_key,mime_path)).fetchone()
                _,previous_metadata=decode_processing_warnings(previous['warnings_json'] if previous else None)
                retained={}
                for row in c.execute("SELECT page_number,sheet_name,cell_range,text FROM mail_text_segments WHERE package_id=? AND message_key=? AND mime_path=? AND source_kind='attachment' ORDER BY ordinal",(package_id,message_key,mime_path)):
                    retained.setdefault(location(dict(row)),{**dict(row),'text':''})['text']+=row['text']
                if retained:
                    if not previous_metadata:
                        previous_metadata=processing_metadata({'parser':previous['parser_name'] if previous else None,'units':list(retained.values())},'basic_text')
                    previous_metadata={**previous_metadata,'retained_previous_text':True}
                    warnings,_=decode_processing_warnings(result.get('warnings') or [])
                    if any(unit.get('text_origin')=='ocr' for unit in previous_metadata.get('units') or []) and OCR_NOTICE not in warnings:warnings.append(OCR_NOTICE)
                    result['warnings']=warnings+[previous_metadata]
            if stage=='ocr' and result.get('text'):
                # OCR augments missing pages. It must not erase readable native pages
                # when its time budget ends before reaching the rest of a PDF.
                old_units={}
                old_state=c.execute("SELECT parser_name,warnings_json FROM mail_part_processing WHERE message_key=? AND mime_path=? AND stage='basic_text'",(message_key,mime_path)).fetchone()
                _,old_metadata=decode_processing_warnings(old_state['warnings_json'] if old_state else None)
                old_locations={location(unit):unit for unit in old_metadata.get('units') or []}
                for row in c.execute("SELECT page_number,sheet_name,cell_range,text FROM mail_text_segments WHERE package_id=? AND message_key=? AND mime_path=? AND source_kind='attachment' ORDER BY ordinal",(package_id,message_key,mime_path)):
                    key=location(dict(row))
                    old_units.setdefault(key,{**old_locations.get(key,{}),'page_number':row['page_number'],
                        'sheet_name':row['sheet_name'],'cell_range':row['cell_range'],'text':'',
                        'text_origin':old_locations.get(key,{}).get('text_origin') or legacy_origin(old_state['parser_name'] if old_state else None),
                        'extraction_method':old_locations.get(key,{}).get('extraction_method') or (old_state['parser_name'] if old_state else None)})['text']+=row['text']
                new_units=[{**unit,'text_origin':'ocr','extraction_method':result.get('parser')} for unit in result.get('units') or [{'text':result['text']}]]
                if any(u.get('page_number') is not None for u in new_units):
                    for unit in new_units:
                        key=location(unit)
                        # A successful new OCR version replaces old OCR on the
                        # SAME page. Native text remains preferred. An empty or
                        # failed retry never deletes a readable previous page.
                        if str(unit.get('text') or '').strip() and (key not in old_units or old_units[key].get('text_origin')=='ocr'):
                            old_units[key]=unit
                    result['units']=sorted(old_units.values(),key=lambda u:(u.get('page_number') is None,u.get('page_number') or 0,u.get('sheet_name') or ''))
                elif old_units:
                    result['units']=list(old_units.values())+new_units
                    result['status']='partial'
                    result['warnings']=(result.get('warnings') or [])+['OCR 无逐页定位；原抽取文本独立保留，可能存在重叠内容']
                result['text']='\n\n'.join(u.get('text') or '' for u in result.get('units',new_units))
            # Store typed provenance alongside existing warnings without a
            # schema migration; API readers project it as a separate field.
            if result.get('text'):
                metadata=processing_metadata(result,stage)
                warnings,_=decode_processing_warnings(result.get('warnings') or [])
                if any(unit.get('text_origin')=='ocr' for unit in metadata['units']) and OCR_NOTICE not in warnings:warnings.append(OCR_NOTICE)
                result['warnings']=warnings+[metadata]
            status=self.update_processing(message_key,mime_path,stage,result['status'],parser_name=result.get('parser'),text=result.get('text'),warnings=result.get('warnings'),error=result.get('error'),coverage_ratio=result.get('coverage_ratio'),connection=c)
            if stage=='basic_text' and status not in {'waiting_retry','terminal_failed'}:
                legacy=c.execute("SELECT 1 FROM mail_part_processing WHERE message_key=? AND mime_path=? AND stage='ocr' AND COALESCE(parser_version,'')!='mail-extract-v2'",(message_key,mime_path)).fetchone()
                if legacy:
                    needs_ocr=status=='ocr_pending'
                    c.execute("UPDATE mail_part_processing SET status=?,parser_version='mail-extract-v2',retry_count=0,next_retry_at=NULL,error_json=NULL,warnings_json=? WHERE message_key=? AND mime_path=? AND stage='ocr'",('pending' if needs_ocr else 'skipped',_json(['新版抽取需要重新进行 OCR' if needs_ocr else '新版抽取无需 OCR']),message_key,mime_path))
            if stage=='ocr':
                # Keep first-pass text when OCR cannot recover any page.
                text=result.get('text')
                if text:
                    self.update_processing(message_key,mime_path,'basic_text',status,parser_name=result.get('parser'),text=text,warnings=result.get('warnings'),coverage_ratio=result.get('coverage_ratio'),connection=c)
                elif status not in {'waiting_retry','running'}:
                    previous=c.execute("SELECT warnings_json FROM mail_part_processing WHERE message_key=? AND mime_path=? AND stage='basic_text'",(message_key,mime_path)).fetchone()
                    old_warnings,old_metadata=decode_processing_warnings(previous['warnings_json'] if previous else None)
                    new_warnings,_=decode_processing_warnings(result.get('warnings') or ["OCR 未恢复缺失页面"])
                    kept=old_warnings+[warning for warning in new_warnings if warning not in old_warnings]
                    if old_metadata:kept.append(old_metadata)
                    c.execute("UPDATE mail_part_processing SET status='partial',warnings_json=? WHERE message_key=? AND mime_path=? AND stage='basic_text' AND extracted_chars>0",(_json(kept),message_key,mime_path))
            if result.get('text') or (stage!='ocr' and result['status'] in {'ready','partial'}):
                self.replace_attachment_segments(package_id,owner_id,message_key,mime_path,result.get('text') or '',units=result.get('units'),connection=c)
            return status

    def _replace_attachment_segments_for_package(self, connection, package, message_key, mime_path, text, *, page_number=None, sheet_name=None, units=None):
        units=units if units is not None else [{"text":text,"page_number":page_number,"sheet_name":sheet_name}]
        previous={r[0] for r in connection.execute("SELECT segment_id FROM mail_text_segments WHERE package_id=? AND message_key=? AND mime_path=? AND source_kind='attachment'",(package['package_id'],message_key,mime_path))}
        # FTS metadata columns are unindexed: scan this projection once, not once
        # per segment of a large workbook or PDF while holding the write lock.
        indexed={r[0] for r in connection.execute("SELECT segment_id FROM mail_search_fts WHERE package_id=? AND message_key=? AND mime_path=? AND field='attachment'",(package['package_id'],message_key,mime_path))}
        current=set(); ordinal=0
        for unit in units:
            value=str(unit.get('text') or '')
            for start in range(0,len(value),self.text_segment_chars):
                fragment=value[start:start+self.text_segment_chars]
                sid='mseg_'+hashlib.sha256(_json([package['package_id'],message_key,mime_path,'attachment',ordinal,unit.get('page_number'),unit.get('sheet_name'),unit.get('cell_range'),fragment]).encode()).hexdigest()
                current.add(sid); language=self._language(fragment)
                connection.execute("INSERT OR IGNORE INTO mail_text_segments(segment_id,package_id,message_key,mime_path,source_kind,language,ordinal,page_number,sheet_name,cell_range,char_start,char_end,text,text_sha256,source_revision,index_epoch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(sid,package['package_id'],message_key,mime_path,'attachment',language,ordinal,unit.get('page_number'),unit.get('sheet_name'),unit.get('cell_range'),start,start+len(fragment),fragment,hashlib.sha256(fragment.encode()).hexdigest(),package['source_revision'],package['index_epoch']))
                if sid not in indexed:
                    connection.execute("INSERT INTO mail_search_fts(segment_id,package_id,message_key,mime_path,field,language,text) VALUES (?,?,?,?,?,?,?)",(sid,package['package_id'],message_key,mime_path,'attachment',language,fragment))
                    indexed.add(sid)
                ordinal+=1
        obsolete=sorted(previous-current)
        for start in range(0,len(obsolete),400):
            batch=obsolete[start:start+400]
            for table in ('mail_search_fts','mail_embeddings','mail_embedding_buckets','mail_text_segments'):
                connection.execute('DELETE FROM '+table+' WHERE segment_id IN ('+','.join('?' for _ in batch)+')',batch)

    def record_research_step(self, turn_id, tool_name, input_value, *, status="completed", output_ref=None, error=None):
        with self._connection() as connection:
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 value FROM mail_research_steps WHERE turn_id=?",
                (turn_id,),
            ).fetchone()["value"]
            connection.execute(
                "INSERT INTO mail_research_steps(step_id,turn_id,sequence,tool_name,input_json,output_ref,status,started_at,finished_at,error_json) VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,?)",
                (_id("mrs"), turn_id, sequence, tool_name, _json(input_value), output_ref, status, _json(error) if error else None),
            )

    def record_claim(self, turn_id, claim_text, claim_type, verification, *, evidence_ids=None, counter_evidence_ids=None):
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO mail_claims(claim_id,turn_id,claim_text,claim_type,state,support_status,evidence_ids_json,counter_evidence_ids_json,verification_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (_id("mcl"), turn_id, str(claim_text)[:4000], str(claim_type or "observation")[:80], verification.get("state") or "insufficient", verification.get("support_status") or "missing_evidence", _json(evidence_ids or []), _json(counter_evidence_ids or []), _json(verification)),
            )

    def create_session(self, package_id, owner_id, title, scope=None):
        package = self.get_package(package_id, owner_id)
        if not package:
            raise ValueError("邮件资料包不存在或不属于当前用户")
        session_id = _id("mrs")
        with self._connection() as connection:
            connection.execute("INSERT INTO mail_research_sessions(session_id,package_id,owner_id,title,scope_json,source_revision) VALUES (?,?,?,?,?,?)", (session_id, package_id, owner_id, str(title or "邮件研究")[:200], _json(scope or {}), package["source_revision"]))
        return self.get_session(session_id, owner_id)

    def get_session(self, session_id, owner_id):
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM mail_research_sessions WHERE session_id=? AND owner_id=?", (session_id, owner_id)).fetchone()
        value = _row(row)
        if value:
            value["scope"] = _load(value.pop("scope_json"))
        return value

    def list_sessions(self, package_id, owner_id, *, limit=100):
        if not self.get_package(package_id, owner_id):
            raise ValueError("邮件资料包不存在或不属于当前用户")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM mail_research_sessions WHERE package_id=? AND owner_id=? "
                "ORDER BY updated_at DESC LIMIT ?", (package_id, owner_id, max(1, min(200, int(limit))))
            ).fetchall()
        values = []
        for row in rows:
            value = _row(row)
            value["scope"] = _load(value.pop("scope_json"))
            values.append(value)
        return values

    def create_turn(self, session_id, owner_id, question, task_spec):
        session = self.get_session(session_id, owner_id)
        if not session:
            raise ValueError("研究会话不存在或不属于当前用户")
        package = self.get_package(session["package_id"], owner_id)
        turn_id = _id("mrt")
        with self._connection() as connection:
            connection.execute("INSERT INTO mail_research_turns(turn_id,session_id,owner_id,question,task_spec_json,status,source_revision,index_epoch) VALUES (?,?,?,?,?,?,?,?)", (turn_id, session_id, owner_id, question, _json(task_spec), "queued", package["source_revision"], package["index_epoch"]))
        return self.get_turn(turn_id, owner_id)

    def get_turn(self, turn_id, owner_id, *, include_events=True):
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM mail_research_turns WHERE turn_id=? AND owner_id=?", (turn_id, owner_id)).fetchone()
            if not row:
                return None
            value = _row(row)
            value["task_spec"] = _load(value.pop("task_spec_json"))
            value["final"] = _load(value.pop("final_json"), None) if value.get("final_json") else None
            value["error"] = _load(value.pop("error_json"), None) if value.get("error_json") else None
            value["events"] = ([_row(item) for item in connection.execute("SELECT sequence,event_type,payload_json,created_at FROM mail_research_events WHERE turn_id=? ORDER BY sequence", (turn_id,)).fetchall()]
                               if include_events else [])
            for event in value["events"]:
                event["payload"] = _load(event.pop("payload_json"))
        return value

    def update_turn(self, turn_id, owner_id, *, status=None, sufficiency=None, result_set_id=None, job_id=None, final=None, error=None):
        fields = ["updated_at=CURRENT_TIMESTAMP"]
        values = []
        for column, value, encoder in (("status", status, lambda x: x), ("sufficiency", sufficiency, lambda x: x), ("result_set_id", result_set_id, lambda x: x), ("job_id", job_id, lambda x: x), ("final_json", final, _json), ("error_json", error, _json)):
            if value is not None:
                fields.append(column + "=?"); values.append(encoder(value))
        with self._connection() as connection:
            values.extend([turn_id, owner_id])
            connection.execute("UPDATE mail_research_turns SET {} WHERE turn_id=? AND owner_id=?".format(",".join(fields)), values)

    def event(self, turn_id, event_type, payload):
        with self._connection() as connection:
            sequence = connection.execute("SELECT COALESCE(MAX(sequence),0)+1 value FROM mail_research_events WHERE turn_id=?", (turn_id,)).fetchone()["value"]
            connection.execute("INSERT INTO mail_research_events(event_id,turn_id,sequence,event_type,payload_json) VALUES (?,?,?,?,?)", (_id("mre"), turn_id, sequence, event_type, _json(payload)))

    def create_result_set(self, package_id, owner_id, items, definition, coverage, parent_result_set_id=None):
        package = self.get_package(package_id, owner_id)
        if not package:
            raise ValueError("邮件资料包不存在或不属于当前用户")
        result_set_id = _id("mrslt")
        with self._connection() as connection:
            connection.execute("INSERT INTO mail_result_sets(result_set_id,package_id,owner_id,parent_result_set_id,source_revision,index_epoch,definition_json,item_count,coverage_json) VALUES (?,?,?,?,?,?,?,?,?)", (result_set_id, package_id, owner_id, parent_result_set_id, package["source_revision"], package["index_epoch"], _json(definition), len(items), _json(coverage)))
            for rank, item in enumerate(items):
                connection.execute("INSERT INTO mail_result_set_items(result_set_id,item_type,item_id,rank,status,rationale_json) VALUES (?,?,?,?,?,?)", (result_set_id, item.get("item_type", "message"), item["item_id"], rank, item.get("status"), _json(item.get("rationale") or {})))
        return result_set_id

    def filter_result_set(self, result_set_id, owner_id, *, has_attachment=None):
        with self._connection() as connection:
            parent = connection.execute("SELECT * FROM mail_result_sets WHERE result_set_id=? AND owner_id=?", (result_set_id, owner_id)).fetchone()
            if not parent:
                raise ValueError("结果集不存在或不属于当前用户")
            query = "SELECT i.item_id,i.status,i.rationale_json FROM mail_result_set_items i WHERE i.result_set_id=? AND i.item_type='message'"
            rows = connection.execute(query, (result_set_id,)).fetchall()
            ids = [row["item_id"] for row in rows]
            if has_attachment is not None:
                kept = []
                for message_key in ids:
                    exists = connection.execute("SELECT 1 FROM mail_parts WHERE message_key=? AND is_explicit_attachment=1", (message_key,)).fetchone() is not None
                    if exists == has_attachment:
                        kept.append(message_key)
                ids = kept
            parent_value = _row(parent)
            items = [{"item_id": row["item_id"], "item_type": "message", "status": row["status"], "rationale": _load(row["rationale_json"])} for row in rows if row["item_id"] in set(ids)]
        definition=_load(parent_value["definition_json"])
        definition.update(filter={"has_attachment":has_attachment},fields=[f for f in definition.get("fields",[]) if f.get("message_key") in ids])
        return self.create_result_set(parent_value["package_id"], owner_id, items, definition, _load(parent_value["coverage_json"]), parent_result_set_id=result_set_id)

    def get_result_set(self, result_set_id, owner_id):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM mail_result_sets WHERE result_set_id=? AND owner_id=?",
                (result_set_id, owner_id),
            ).fetchone()
            if not row:
                return None
            value = _row(row)
            value["definition"] = _load(value.pop("definition_json"))
            value["coverage"] = _load(value.pop("coverage_json"))
            value["items"] = [_row(item) for item in connection.execute(
                "SELECT item_type,item_id,rank,status,rationale_json FROM mail_result_set_items "
                "WHERE result_set_id=? ORDER BY rank", (result_set_id,)
            ).fetchall()]
        for item in value["items"]:
            item["rationale"] = _load(item.pop("rationale_json"))
        with self._connection() as connection:
            final = connection.execute("SELECT final_json,turn_id FROM mail_research_turns WHERE result_set_id=? AND owner_id=? AND status='completed' ORDER BY created_at DESC LIMIT 1", (result_set_id, owner_id)).fetchone()
        value["final"] = _load(final[0], None) if final and final[0] else None
        value["source_turn_id"] = final[1] if final else None
        return value

    def save_result_fields(self,result_set_id,owner_id,fields,output_schema):
        with self._connection() as c:
            row=c.execute("SELECT definition_json FROM mail_result_sets WHERE result_set_id=? AND owner_id=?",(result_set_id,owner_id)).fetchone()
            if not row:raise ValueError("结果集不存在")
            definition=_load(row[0]);definition.update(fields=fields,output_schema=output_schema)
            c.execute("UPDATE mail_result_sets SET definition_json=? WHERE result_set_id=? AND owner_id=?",(_json(definition),result_set_id,owner_id))

    def list_result_sets(self,package_id,owner_id):
        if not self.get_package(package_id,owner_id):raise ValueError("邮件资料包不存在")
        with self._connection() as c:
            rows=[dict(r) for r in c.execute("SELECT result_set_id,parent_result_set_id,item_count,coverage_json,created_at FROM mail_result_sets WHERE package_id=? AND owner_id=? ORDER BY created_at DESC",(package_id,owner_id))]
        for row in rows:row["coverage"]=_load(row.pop("coverage_json"))
        return rows
