"""Server-validated tools exposed to the mail research runtime."""

from __future__ import annotations

import json
import re
import sqlite3


def _clip(value, limit):
    value = str(value or "")
    return value if len(value) <= limit else value[:limit] + "...[truncated]"


class MailResearchTools:
    def __init__(self, repository, owner_id, package_id, *, context_limit=24000,
                 allowed_keys=None, include_attachments=True):
        self.repository = repository
        self.owner_id = owner_id
        self.package_id = package_id
        self.context_limit = max(1000, int(context_limit))
        self.allowed_keys = None if allowed_keys is None else frozenset(allowed_keys)
        self.include_attachments = bool(include_attachments)
        self.continue_check = lambda: True

    def _message_allowed(self, message_key):
        return self.allowed_keys is None or message_key in self.allowed_keys

    def _scope(self, result):
        value = dict(result or {})
        value["items"] = [item for item in value.get("items") or []
                          if self._message_allowed(item.get("message_key"))
                          and (self.include_attachments or item.get("source_kind") != "attachment")]
        return value

    def _bounded_items(self, result):
        """Keep identifiers and evidence intact while bounding model-visible text."""
        value = self._scope(result)
        items = [dict(item) for item in value.get("items") or []]
        if not items:
            return value
        item_limit = max(160, self.context_limit // max(1, min(len(items), 20)))
        for item in items:
            for field in ("text", "quote_text", "subject_raw", "filename_raw"):
                if field in item:
                    item[field] = _clip(item[field], item_limit)
        value["items"] = items
        serialized = json.dumps(value, ensure_ascii=False, default=str)
        if len(serialized) <= self.context_limit:
            return value
        # Preserve a useful prefix of ranked candidates instead of returning an
        # opaque string that would prevent the runtime from validating IDs.
        # Retrieval IDs must survive a display/text cap.  The runtime selects
        # bounded JSON evidence later; dropping rows here loses find_all hits.
        value["items"] = items
        value["truncated"] = True
        value["truncation_basis"] = "candidate_text_budget; candidate_ids_preserved"
        return value

    def get_package_overview(self):
        return self.repository.overview(self.package_id, self.owner_id)

    def search_messages(self, query, limit=50):
        query = str(query or "").strip()[:500]
        if self.allowed_keys is not None or not self.include_attachments:
            return self._bounded_items(self._scoped_search(query, limit=limit))
        return self._bounded_items(self.repository.search_segments(
            self.package_id, self.owner_id, query, limit=max(1, min(100, int(limit)))
        ))

    def search_attachments(self, query, limit=50):
        if not self.include_attachments:
            return {"items": [], "excluded_by_scope": True}
        query = str(query or "").strip()[:500]
        if self.allowed_keys is not None:
            return self._bounded_items(self._scoped_search(query, attachment_only=True, limit=limit))
        return self._bounded_items(self.repository.search_segments(
            self.package_id, self.owner_id, query, attachment_only=True,
            limit=max(1, min(100, int(limit))),
        ))

    def _scoped_search(self, query, *, attachment_only=False, limit=50):
        """Filter before ranking/LIMIT and before creating any source evidence.

        The legacy hybrid API has no scope argument.  A scoped lexical fallback
        is explicit rather than leaking outside-scope Top-N into this turn.
        """
        if not self.repository.get_package(self.package_id, self.owner_id):
            raise ValueError("邮件资料包不存在或不属于当前用户")
        if not query or self.allowed_keys == frozenset():
            return {"items": [], "retrieval": {"scope_applied_before_limit": True}}
        limit = max(1, min(100, int(limit)))
        keys = sorted(self.allowed_keys) if self.allowed_keys is not None else []
        batches = [keys[n:n + 400] for n in range(0, len(keys), 400)] if keys else [None]
        found = {}
        boundary_truncated = False
        like = "%" + query.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        columns = "s.*,m.subject_raw,m.sent_at_utc"
        for batch in batches:
            if not self.continue_check():
                break
            clauses, args = ["s.package_id=?"], [self.package_id]
            if batch is not None:
                clauses.append("s.message_key IN (" + ",".join("?" for _ in batch) + ")")
                args.extend(batch)
            if attachment_only:
                clauses.append("s.source_kind='attachment'")
            elif not self.include_attachments:
                clauses.append("s.source_kind!='attachment'")
            where = " AND ".join(clauses)
            with self.repository._connection() as c:
                try:
                    rows = c.execute(
                        "SELECT " + columns + " FROM mail_search_fts JOIN mail_text_segments s "
                        "ON s.segment_id=mail_search_fts.segment_id JOIN mail_messages m ON m.message_key=s.message_key "
                        "WHERE " + where + " AND mail_search_fts MATCH ? ORDER BY bm25(mail_search_fts),m.sent_at_utc DESC LIMIT ?",
                        args + ['"' + query.replace('"', '""') + '"', limit]).fetchall()
                except sqlite3.Error:
                    rows = []
                phrase = c.execute(
                    "SELECT " + columns + " FROM mail_text_segments s JOIN mail_messages m ON m.message_key=s.message_key "
                    "WHERE " + where + " AND (s.text LIKE ? ESCAPE '!' OR m.subject_raw LIKE ? ESCAPE '!' "
                    "OR EXISTS (SELECT 1 FROM mail_parts p WHERE p.message_key=s.message_key AND p.filename_raw LIKE ? ESCAPE '!')) "
                    "ORDER BY m.sent_at_utc DESC,s.segment_id LIMIT ?", args + [like, like, like, limit]).fetchall()
                exact_rows = bool(rows or phrase)
                if not rows and not phrase:
                    terms = list(dict.fromkeys(re.findall(r"[^\s,.;:!?，。；：！？]{2,}", query)))[:12]
                    if len(terms) > 1:
                        rows = c.execute(
                            "SELECT " + columns + " FROM mail_text_segments s JOIN mail_messages m ON m.message_key=s.message_key "
                            "WHERE " + where + " AND (" + " OR ".join("s.text LIKE ? ESCAPE '!'" for _ in terms) + ") "
                            "ORDER BY m.sent_at_utc DESC LIMIT ?",
                            args + ["%" + term.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%" for term in terms] + [limit]).fetchall()
                if not exact_rows:
                    # The scoped fallback also preserves literal phrases split
                    # by a configured segment boundary.  Read only eligible
                    # rows; the finite scan cap is exposed as a coverage gap.
                    fallback_rows, rows = rows, []
                    scan = c.execute(
                        "SELECT " + columns + " FROM mail_text_segments s JOIN mail_messages m ON m.message_key=s.message_key "
                        "WHERE " + where + " ORDER BY s.message_key,s.mime_path,s.source_kind,s.page_number,s.sheet_name,s.ordinal LIMIT 10001",
                        args).fetchall()
            if not exact_rows:
                # Job/cancellation callbacks re-enter Storage. The bounded
                # scan is fully fetched above, so no repository lock is held.
                boundary_truncated = boundary_truncated or len(scan) > 10000
                tail, previous = "", None
                for position, value in enumerate(scan[:10000]):
                    if position % 256 == 0 and not self.continue_check():
                        boundary_truncated = True
                        break
                    current = dict(value)
                    group = tuple(current.get(k) for k in ("message_key", "mime_path", "source_kind", "page_number", "sheet_name"))
                    contiguous = previous and previous[0] == group and previous[1] == current["ordinal"] - 1 and previous[2] == current["char_start"]
                    if not contiguous:
                        tail = ""
                    text = tail + str(current.get("text") or "")
                    start = int(current.get("char_start") or 0) - len(tail)
                    location = text.casefold().find(query.casefold())
                    if location >= 0:
                        begin, end = max(0, location - 200), min(len(text), location + len(query) + 800)
                        current.update(text=text[begin:end], char_start=start + begin, char_end=start + end)
                        rows.append(current)
                        if len(rows) >= limit:
                            break
                    tail = text[-max(1, len(query) + 200):]
                    previous = (group, value["ordinal"], value["char_end"])
                rows.extend(fallback_rows)
            for row in list(rows) + list(phrase):
                item = dict(row)
                found.setdefault(item["segment_id"], item)
        rows = list(found.values())[:limit]
        for item in rows:
            item.update(self.repository.create_evidence(self.package_id, self.owner_id, item))
            item["retrieval"] = {"lexical": {"scope_applied_before_limit": True}}
        from config import Config
        vector_meta={"status":"disabled"}
        if Config.MAIL_ENABLE_VECTOR_INDEX:
            from ..semantic import vector_search
            from ..retrieval import rrf_fuse
            try:
                vector,vector_meta=vector_search(self.repository,self.package_id,self.owner_id,query,
                    limit=limit,attachment_only=attachment_only,allowed_keys=self.allowed_keys,
                    include_attachments=self.include_attachments)
                rows=rrf_fuse([("lexical",rows),("vector",vector)],Config.MAIL_RRF_K)[:limit]
                for item in rows:
                    if not item.get("evidence_id"):
                        item.update(self.repository.create_evidence(self.package_id,self.owner_id,item))
            except Exception as error:
                vector_meta={"status":"unavailable","reason":str(error)[:200]}
        return {"items": rows, "retrieval": {"scope_applied_before_limit": True,
                 "boundary_scan_truncated": boundary_truncated,"vector_index":vector_meta,
                 "scoped_semantic_unavailable": bool(Config.MAIL_ENABLE_VECTOR_INDEX and
                    (vector_meta.get("status") in {"unavailable","disabled"} or not vector_meta.get("ready")))}}

    def read_message(self, message_key):
        if not self._message_allowed(message_key):
            raise ValueError("邮件不在当前研究范围")
        message = self.repository.get_message(message_key, self.owner_id, self.package_id)
        if not message:
            raise ValueError("邮件不存在或不在当前资料范围")
        value = dict(message)
        value["headers"] = {
            _clip(key, 120): [_clip(item, 500) for item in (items or [])[:10]]
            for key, items in (value.get("headers") or {}).items()
        }
        value["warnings"] = [_clip(item, 300) for item in (value.get("warnings") or [])[:20]]
        value["participants"] = (value.get("participants") or [])[:100]
        value["parts"] = (value.get("parts") or [])[:100]
        value["occurrences"] = (value.get("occurrences") or [])[:20]
        from config import Config
        if Config.MAIL_ENABLE_AGENT_MINING:
            with self.repository._connection() as c:
                rows=[dict(r) for r in c.execute("SELECT s.*,m.subject_raw,m.sent_at_utc FROM mail_text_segments s JOIN mail_messages m ON m.message_key=s.message_key WHERE s.package_id=? AND s.message_key=? ORDER BY s.source_kind,s.mime_path,s.ordinal LIMIT 20", (self.package_id,message_key))]
            value["items"]=[{**row,**self.repository.create_evidence(self.package_id,self.owner_id,row)} for row in rows]
        return self._bounded_items(value)

    def read_context(self, message_key, *, max_depth=8, check=None):
        from .context import read_context
        return read_context(self, message_key, max_depth=max_depth, check=check or self.continue_check)

    def trace_attachment_versions(self, chain_key=None, message_key=None):
        if not self.include_attachments:
            return {"items": [], "excluded_by_scope": True}
        if message_key and not self._message_allowed(message_key):
            raise ValueError("邮件不在当前研究范围")
        if not self.repository.get_package(self.package_id, self.owner_id):
            raise ValueError("邮件资料包不存在或不属于当前用户")
        # Research reads the version projection; rebuilding it belongs to
        # ingestion/backfill, which has its own checkpoints and cancellation.
        with self.repository._connection() as c:
            sql = ("SELECT v.*,l.message_key,l.mime_path,m.sent_at_utc,m.subject_raw FROM mail_attachment_version_links l "
                   "JOIN mail_attachment_versions v ON v.package_id=l.package_id AND v.chain_key=l.chain_key "
                   "AND v.payload_sha256=l.payload_sha256 JOIN mail_messages m ON m.message_key=l.message_key WHERE l.package_id=?")
            args = [self.package_id]
            if chain_key:
                sql += " AND l.chain_key=?"
                args.append(chain_key)
            if message_key:
                sql += " AND l.chain_key IN (SELECT chain_key FROM mail_attachment_version_links WHERE package_id=? AND message_key=?)"
                args.extend([self.package_id, message_key])
            if self.allowed_keys is not None:
                from .scope import scope_clause
                sql += scope_clause(c,self.allowed_keys,'l.message_key')
            rows = [dict(r) for r in c.execute(sql + " ORDER BY l.chain_key,l.version_ordinal,m.sent_at_utc LIMIT 41", args)]
        value = {"items": rows, "version_basis": "同名家族仅为候选版本；读取导入投影，不推断最终版本", "truncated": len(rows) > 40,
                 'scope_applied_before_limit': True}
        enriched = []
        for item in (value.get("items") or [])[:40]:
            if not self.continue_check():
                break
            if not self._message_allowed(item.get("message_key")):
                continue
            row = dict(item)
            with self.repository._connection() as c:
                segment = c.execute(
                    "SELECT * FROM mail_text_segments WHERE package_id=? AND message_key=? AND mime_path=? "
                    "ORDER BY ordinal LIMIT 1",
                    (self.package_id, row.get("message_key"), row.get("mime_path")),
                ).fetchone()
            if segment:
                row.update(self.repository.create_evidence(self.package_id, self.owner_id, dict(segment)))
            enriched.append(row)
        return self._bounded_items({**value, "items": enriched})

    def read_attachment_segment(self, segment_id):
        segment = self.repository.get_text_segment(str(segment_id or ""), self.owner_id, self.package_id)
        if not segment or segment.get("source_kind") != "attachment":
            raise ValueError("附件文本片段不存在或不在当前资料范围")
        if not self.include_attachments or not self._message_allowed(segment.get("message_key")):
            raise ValueError("附件不在当前研究范围")
        evidence = self.repository.create_evidence(self.package_id, self.owner_id, segment)
        value = dict(segment)
        value["text"] = _clip(value.get("text"), self.context_limit)
        value["evidence_id"] = evidence["evidence_id"]
        value["quote_text"] = _clip(evidence["quote_text"], min(1000, self.context_limit))
        return value

    def get_communication_stats(self):
        if self.allowed_keys is not None:
            return {"items": [], "unavailable": "scoped_communication_stats_not_available"}
        return self._bounded_items(self.repository.communication(self.package_id, self.owner_id))

    def expand_thread(self, message_key, max_depth=8):
        max_depth = max(1, min(20, int(max_depth)))
        value = self.read_context(message_key, max_depth=max_depth)
        return {**value, "edges": value["review"]["edges"], "max_depth": max_depth}
