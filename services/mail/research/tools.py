"""Server-validated tools exposed to the mail research runtime."""

from __future__ import annotations

import json


def _clip(value, limit):
    value = str(value or "")
    return value if len(value) <= limit else value[:limit] + "...[truncated]"


class MailResearchTools:
    def __init__(self, repository, owner_id, package_id, *, context_limit=24000):
        self.repository = repository
        self.owner_id = owner_id
        self.package_id = package_id
        self.context_limit = max(1000, int(context_limit))

    def _bounded_items(self, result):
        """Keep identifiers and evidence intact while bounding model-visible text."""
        value = dict(result or {})
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
        kept = []
        for item in items:
            candidate = dict(value, items=kept + [item])
            if len(json.dumps(candidate, ensure_ascii=False, default=str)) > self.context_limit:
                break
            kept.append(item)
        value["items"] = kept
        value["truncated"] = len(kept) < len(items)
        return value

    def get_package_overview(self):
        return self.repository.overview(self.package_id, self.owner_id)

    def search_messages(self, query, limit=50):
        query = str(query or "").strip()[:500]
        return self._bounded_items(self.repository.search_segments(
            self.package_id, self.owner_id, query, limit=max(1, min(100, int(limit)))
        ))

    def search_attachments(self, query, limit=50):
        query = str(query or "").strip()[:500]
        return self._bounded_items(self.repository.search_segments(
            self.package_id, self.owner_id, query, attachment_only=True,
            limit=max(1, min(100, int(limit))),
        ))

    def read_message(self, message_key):
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
        return value

    def trace_attachment_versions(self, chain_key=None, message_key=None):
        from ..versions import trace
        return self._bounded_items(trace(self.repository,self.package_id,self.owner_id,chain_key,message_key))

    def read_attachment_segment(self, segment_id):
        segment = self.repository.get_text_segment(str(segment_id or ""), self.owner_id, self.package_id)
        if not segment or segment.get("source_kind") != "attachment":
            raise ValueError("附件文本片段不存在或不在当前资料范围")
        evidence = self.repository.create_evidence(self.package_id, self.owner_id, segment)
        value = dict(segment)
        value["text"] = _clip(value.get("text"), self.context_limit)
        value["evidence_id"] = evidence["evidence_id"]
        value["quote_text"] = _clip(evidence["quote_text"], min(1000, self.context_limit))
        return value

    def get_communication_stats(self):
        return self._bounded_items(self.repository.communication(self.package_id, self.owner_id))

    def expand_thread(self, message_key, max_depth=8):
        max_depth = max(1, min(20, int(max_depth)))
        edges = self.repository.threads(self.package_id, self.owner_id, status="exact")
        selected = []
        frontier = {message_key}
        seen_edges = set()
        seen_nodes = set(frontier)
        for _ in range(max_depth):
            next_frontier = set()
            for edge in edges:
                if edge["source_message_key"] in frontier or edge.get("target_message_key") in frontier:
                    if edge["edge_id"] not in seen_edges:
                        selected.append(edge)
                        seen_edges.add(edge["edge_id"])
                    next_frontier.update([edge["source_message_key"], edge.get("target_message_key")])
            frontier = {value for value in next_frontier if value and value not in seen_nodes}
            seen_nodes.update(frontier)
            if not frontier:
                break
        return {"items": selected, "max_depth": max_depth}
