"""Package-scoped, conservative header relations and atomic projections.

References retain historical context; only a unique In-Reply-To identifies an
explicit parent. Missing and disputed parents remain visible rather than being
silently selected. Raw-message identity is deliberately left to the importer.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from contextlib import nullcontext

from .normalization import message_id_tokens, normalize_message_id, message_id_format


ALGORITHM_VERSION = "mail-foundation-threads-v3-conflicted-parents"


def stable_id(*parts):
    return hashlib.sha256("\0".join(map(str, parts)).encode("utf-8")).hexdigest()[:32]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(value):
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def build_relations(package_id, messages):
    """Pure graph construction, suitable for historical backfill and tests."""
    messages = sorted(messages, key=lambda item: item["message_key"])
    nodes, edges, anomalies = {}, [], []
    id_map = defaultdict(set)
    headers_by_key = {}
    conflicted_ids = set()

    def anomaly(kind, key, payload):
        anomalies.append({"anomaly_id": stable_id(package_id, kind, key, _json(payload)),
                          "message_key": key, "anomaly_type": kind, "payload": payload})

    def identifiers(values, header, key):
        tokens = message_id_tokens(values)
        valid = [token for token in tokens if normalize_message_id(token)]
        if any(str(value).strip() for value in values or []) and not valid:
            anomaly("invalid_identifier", key, {"header": header, "raw_values": values})
        elif len(valid) != len(tokens):
            anomaly("invalid_identifier", key, {"header": header, "raw_values": values})
        opaque = [token for token in valid if message_id_format(token) == 'nonstandard_opaque']
        if opaque:
            anomaly("nonstandard_identifier", key, {"header": header, "message_ids": opaque,
                    "identifier_format": "nonstandard_opaque", "matching_basis": "exact_bracketed_token",
                    "note": "该邮件标识缺少 @，不符合 RFC 5322 格式；仅按完整角括号标识精确匹配，保留大小写，唯一匹配才建立关联。"})
        return valid

    for message in messages:
        key = message["message_key"]
        headers = _load(message.get("headers_json"))
        headers_by_key[key] = headers
        values = headers.get("message-id") or ([message.get("canonical_message_id_raw")] if message.get("canonical_message_id_raw") else [])
        ids = identifiers(values, "message-id", key)
        normalized_ids = sorted({normalize_message_id(token) for token in ids})
        if not any(str(value).strip() for value in values or []):
            anomaly("missing_message_id", key, {"header": "message-id"})
        if len(normalized_ids) > 1:
            anomaly("multiple_message_ids", key, {"message_ids": normalized_ids})
            conflicted_ids.add(key)
        for normalized in normalized_ids:
            id_map[normalized].add(key)
        node_id = "message:" + key
        nodes[node_id] = {"node_id": node_id, "message_key": key,
                          "message_id_normalized": normalized_ids[0] if len(normalized_ids) == 1 else None,
                          "node_kind": "message"}
    for normalized, keys in sorted(id_map.items()):
        if len(keys) > 1:
            anomaly("duplicate_message_id", None, {"message_id": normalized, "candidates": sorted(keys)})

    def make_edge(source, raw_id, relation, header, multiple=False):
        normalized = normalize_message_id(raw_id)
        candidates = sorted(id_map.get(normalized, ()))
        target = candidates[0] if len(candidates) == 1 else None
        if multiple:
            status = "ambiguous"
            target = None
        elif not candidates:
            status = "missing_target"
        elif len(candidates) > 1 or target in conflicted_ids:
            status = "ambiguous"
            target = None
        elif source == target:
            status = "self_reference"
            anomaly("self_reference", source, {"relation_type": relation, "message_id": normalized})
        else:
            status = {"reply_to": "exact", "reference": "context", "reference_parent": "inferred"}[relation]
        target_node = "message:" + target if target else None
        if not candidates:
            target_node = "missing:" + stable_id(package_id, normalized)
            nodes.setdefault(target_node, {"node_id": target_node, "message_key": None,
                                          "message_id_normalized": normalized, "node_kind": "missing"})
        edge = {"edge_id": stable_id(package_id, source, relation, normalized), "package_id": package_id,
                "source_message_key": source, "target_message_key": target, "target_node_id": target_node,
                "relation_type": relation, "source_header": header, "target_message_id_raw": raw_id,
                "status": status, "evidence": {"header": header, "raw_message_id": raw_id,
                "message_id_normalized": normalized, "candidates": candidates,
                "identifier_format": message_id_format(raw_id),
                "matching_basis": "exact_bracketed_token" if message_id_format(raw_id) == 'nonstandard_opaque' else "normalized_message_id",
                "parent_basis": "explicit_header" if relation == "reply_to" else "historical_context"}}
        conflicting_candidates = sorted(set(candidates) & conflicted_ids)
        if conflicting_candidates:
            edge["evidence"]["conflicting_identity_candidates"] = conflicting_candidates
            anomaly("conflicting_target_identity", source, {"relation_type": relation,
                    "message_id": normalized, "candidates": conflicting_candidates})
        if edge['evidence']['identifier_format'] == 'nonstandard_opaque':
            edge['evidence']['note'] = "非标准邮件标识；按完整角括号标识精确匹配并保留大小写，重复标识保留歧义，不按主题推断。"
        if relation == "reference_parent":
            edge["evidence"]["parent_basis"] = "last_reference_inferred"
        edges.append(edge)
        return edge

    for message in messages:
        key = message["message_key"]
        headers = headers_by_key[key]
        raw_reply_values = headers.get("in-reply-to", [])
        reply_ids = identifiers(raw_reply_values, "in-reply-to", key)
        refs = identifiers(headers.get("references", []), "references", key)
        # Case changes in the domain do not create extra putative parents.
        reply_ids = list({normalize_message_id(raw): raw for raw in reply_ids}.values())
        multiple = len(reply_ids) > 1
        if multiple:
            anomaly("multiple_parent", key, {"message_ids": reply_ids,
                    "candidates": sorted({parent for raw in reply_ids for parent in id_map.get(normalize_message_id(raw), ())})})
        for raw in reply_ids:
            make_edge(key, raw, "reply_to", "in_reply_to", multiple)
        refs = list({normalize_message_id(raw): raw for raw in refs}.values())
        for raw in refs:
            make_edge(key, raw, "reference", "references")
        # An explicitly present but invalid parent cannot be silently replaced
        # by the final References token. Retain the latter as history only.
        if not reply_ids and refs and not any(str(value).strip() for value in raw_reply_values):
            make_edge(key, refs[-1], "reference_parent", "references")

    # Functional parent graph: no recursion or arbitrary parent selection.
    parents = {edge["source_message_key"]: edge for edge in edges
               if edge["relation_type"] in {"reply_to", "reference_parent"}
               and edge["status"] in {"exact", "inferred"}}
    done = set()
    for start in sorted(parents):
        if start in done:
            continue
        trail, positions, cursor = [], {}, start
        while cursor in parents and cursor not in done:
            if cursor in positions:
                cycle = trail[positions[cursor]:]
                for key in cycle:
                    parents[key]["status"] = "cycle"
                    anomaly("cycle", key, {"members": sorted(cycle)})
                break
            positions[cursor] = len(trail)
            trail.append(cursor)
            cursor = parents[cursor]["target_message_key"]
        done.update(trail)
    # Context self-edges are preserved as anomalies, but never used as parents.
    return list(nodes.values()), edges, anomalies


def rebuild_relationships(repository, package_id, owner_id, *, connection=None):
    if connection is None and not repository.get_package(package_id, owner_id):
        raise ValueError("邮件资料包不存在或不属于当前用户")
    context = repository._connection() if connection is None else nullcontext(connection)
    with context as connection:
        if not connection.execute("SELECT 1 FROM mail_packages WHERE package_id=? AND owner_id=?", (package_id, owner_id)).fetchone():
            raise ValueError("邮件资料包不存在或不属于当前用户")
        messages = [dict(row) for row in connection.execute(
            "SELECT DISTINCT m.* FROM mail_messages m JOIN mail_visible_occurrences o ON o.message_key=m.message_key "
            "WHERE o.package_id=? ORDER BY m.message_key", (package_id,))]
        participants = [tuple(row) for row in connection.execute(
            "SELECT DISTINCT p.message_key,p.address_id,p.role FROM mail_message_participants p "
            "JOIN mail_visible_occurrences o ON o.message_key=p.message_key WHERE o.package_id=? "
            "ORDER BY p.message_key,p.address_id,p.role", (package_id,))]
        occurrences = dict(connection.execute("SELECT message_key,COUNT(*) FROM mail_visible_occurrences WHERE package_id=? GROUP BY message_key", (package_id,)))
        fingerprint = stable_id(ALGORITHM_VERSION, [(m["message_key"], m["headers_json"], m["sent_at_utc"]) for m in messages], participants, sorted(occurrences.items()))
        state = connection.execute("SELECT version,fingerprint FROM mail_foundation_state WHERE package_id=? AND stage='threads'", (package_id,)).fetchone()
        if state and state["fingerprint"] == fingerprint:
            return {"generation_id": state["version"], "changed": False}
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
            # Recheck under the publication lock; unchanged readers never take it.
            return rebuild_relationships(repository, package_id, owner_id, connection=connection)
        generation = stable_id(package_id, "threads", fingerprint)
        nodes, edges, anomalies = build_relations(package_id, messages)
        connection.execute("INSERT OR REPLACE INTO mail_relation_generations(generation_id,package_id,stage,fingerprint,algorithm_version,status) VALUES(?,?,?,?,?,?)",
                           (generation, package_id, "threads", fingerprint, ALGORITHM_VERSION, "ready"))
        for node in nodes:
            connection.execute("INSERT OR IGNORE INTO mail_thread_nodes VALUES(?,?,?,?,?,?)", (package_id, generation, node["node_id"], node["message_key"], node["message_id_normalized"], node["node_kind"]))
        for edge in edges:
            connection.execute("INSERT OR IGNORE INTO mail_thread_edge_history VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (generation, edge["edge_id"], package_id, edge["source_message_key"], edge["target_message_key"], edge["target_node_id"], edge["relation_type"], edge["source_header"], edge["target_message_id_raw"], edge["status"], _json(edge["evidence"])) )
        for item in anomalies:
            connection.execute("INSERT OR IGNORE INTO mail_thread_anomalies VALUES(?,?,?,?,?,?)", (generation, item["anomaly_id"], package_id, item["message_key"], item["anomaly_type"], _json(item["payload"])))
        # Prepare complete new data first, then publish compatibility projections
        # and the version pointer in one SQLite transaction.
        connection.execute("DELETE FROM mail_thread_edges WHERE package_id=?", (package_id,))
        for edge in edges:
            connection.execute("INSERT INTO mail_thread_edges(edge_id,package_id,source_message_key,target_message_key,relation_type,source_header,target_message_id_raw,status,evidence_json) VALUES(?,?,?,?,?,?,?,?,?)",
                               (edge["edge_id"], package_id, edge["source_message_key"], edge["target_message_key"], edge["relation_type"], edge["source_header"], edge["target_message_id_raw"], edge["status"], _json(edge["evidence"])))
        connection.execute("DELETE FROM mail_communication_edges WHERE package_id=?", (package_id,))
        senders, receivers = defaultdict(set), defaultdict(set)
        for key, address_id, role in participants:
            if role == "from":
                senders[key].add(address_id)
            elif role in {"to", "cc", "bcc"}:
                receivers[key].add((address_id, role))
        for message in messages:
            key = message["message_key"]
            for sender in sorted(senders[key]):
                for recipient, role in sorted(receivers[key]):
                    connection.execute("INSERT INTO mail_communication_edges VALUES(?,?,?,?,?,?,?)",
                                       (package_id, sender, recipient, role, key, message["sent_at_utc"], occurrences.get(key, 0)))
        connection.execute("INSERT INTO mail_foundation_state(package_id,stage,version,fingerprint,status) VALUES(?,'threads',?,?,'ready') "
                           "ON CONFLICT(package_id,stage) DO UPDATE SET version=excluded.version,fingerprint=excluded.fingerprint,status='ready',updated_at=CURRENT_TIMESTAMP",
                           (package_id, generation, fingerprint))
    return {"generation_id": generation, "changed": True, "nodes": len(nodes), "edges": len(edges), "anomalies": len(anomalies)}
