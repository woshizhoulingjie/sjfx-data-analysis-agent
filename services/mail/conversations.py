"""Versioned browsing conversations, distinct from immutable header evidence."""
from __future__ import annotations

import collections
import json
import uuid
from contextlib import nullcontext
from datetime import datetime

from config import Config
from .normalization import message_id_tokens, normalize_message_id
from .threading import rebuild_relationships, stable_id


ALGORITHM_VERSION = "mail-foundation-conversations-v3-gap-context"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(value, default=None):
    try:
        return json.loads(value) if value else ({} if default is None else default)
    except (ValueError, TypeError):
        return {} if default is None else default


def _message_sort(message):
    return (message.get("sent_at_utc") is None, message.get("sent_at_utc") or "", message["message_key"])


def _gap_contexts(messages, edges):
    """Recover browsing context across a missing explicit parent, not a reply.

    The References tail must agree with that missing In-Reply-To. Its nearest
    uniquely resolved ancestor can keep both sides in one browsing conversation
    while the direct reply tree retains the missing parent. Disputed identifiers,
    invalid cycles and contradictory ordering are never used as anchors.
    """
    by_key = {message["message_key"]: message for message in messages}
    references = collections.defaultdict(dict)
    parent_for_key, missing_parents, blocked = {}, collections.defaultdict(list), set()
    for edge in edges:
        key = edge["source_message_key"]
        if edge["relation_type"] == "reference":
            evidence = edge.get("evidence") or _load(edge.get("evidence_json"))
            references[key][evidence.get("message_id_normalized")] = edge
        elif edge["relation_type"] in {"reply_to", "reference_parent"}:
            if edge["status"] in {"exact", "inferred"}:
                parent_for_key[key] = edge.get("target_message_key")
            elif edge["status"] in {"cycle", "self_reference"}:
                blocked.add(key)
            elif edge["relation_type"] == "reply_to" and edge["status"] == "missing_target":
                missing_parents[key].append(edge)
    bridges = []
    for key, values in sorted(missing_parents.items()):
        if key not in by_key or len(values) != 1:
            continue
        edge = values[0]
        evidence = edge.get("evidence") or _load(edge.get("evidence_json"))
        missing_id = evidence.get("message_id_normalized")
        headers = _load(by_key[key].get("headers_json"))
        ids = list(dict.fromkeys(normalize_message_id(raw) for raw in message_id_tokens(headers.get("references", []))))
        if not ids or None in ids or ids[-1] != missing_id:
            continue
        prefix_edges = [references[key].get(mid) for mid in ids[:-1]]
        if any(item and item["status"] in {"ambiguous", "self_reference", "cycle"} for item in prefix_edges):
            continue
        resolved = [item["target_message_key"] for item in prefix_edges
                    if item and item["status"] == "context" and item.get("target_message_key") in by_key]
        if not resolved or any(ancestor in blocked for ancestor in resolved):
            continue
        positions = {ancestor: index for index, ancestor in enumerate(resolved)}
        # References may omit old ancestors, but cannot reverse an explicit
        # relationship or name a descendant as this message's older context.
        inconsistent = False
        for ancestor in resolved:
            cursor, visited = ancestor, set()
            while cursor in parent_for_key and cursor not in visited:
                visited.add(cursor)
                cursor = parent_for_key[cursor]
                if cursor in blocked or cursor == key or cursor in positions and positions[cursor] >= positions[ancestor]:
                    inconsistent = True
                    break
                if cursor in positions:
                    # The preceding referenced ancestor is validated in its
                    # own walk; no need to retraverse a long shared history.
                    break
            if inconsistent:
                break
        if inconsistent:
            continue
        ancestor = resolved[-1]
        bridges.append({"source_message_key": key, "ancestor_message_key": ancestor,
                        "missing_parent_message_id": missing_id,
                        "missing_node_id": edge.get("target_node_id") or "missing:" + stable_id(edge.get("package_id"), missing_id),
                        "relation_type": "historical_gap_context", "source_header": "references", "status": "context",
                        "evidence": {"references": ids, "parent_basis": "historical_context_across_missing_parent",
                                     "direct_parent_replaced": False}})
    return bridges


def _group(messages, edges, corrections):
    by_key = {message["message_key"]: message for message in messages}
    parents = {key: key for key in by_key}

    def find(key):
        parents.setdefault(key, key)
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    parent_for_key = {}
    for edge in edges:
        if edge["relation_type"] not in {"reply_to", "reference_parent"} or edge["status"] not in {"exact", "inferred", "missing_target"}:
            continue
        source = edge["source_message_key"]
        target = edge.get("target_message_key") or edge.get("target_node_id")
        if source in by_key and target:
            parents[find(source)] = find(target)
            if edge.get("target_message_key"):
                parent_for_key[source] = edge["target_message_key"]
    # This is a browsing union only. Header edges and the direct missing
    # parent remain unchanged, and manual corrections are applied afterwards.
    for bridge in _gap_contexts(messages, edges):
        parents[find(bridge["source_message_key"])] = find(bridge["ancestor_message_key"])
    grouped = collections.defaultdict(set)
    for key in by_key:
        grouped[find(key)].add(key)
    groups = list(grouped.values())
    revoked = {_load(row["payload_json"]).get("correction_id") for row in corrections if row["action"] == "revoke"}
    applied = []
    for row in corrections:
        if row["action"] == "revoke" or row["correction_id"] in revoked:
            continue
        payload = _load(row["payload_json"])
        wanted = set(payload.get("message_keys") or [])
        split_groups = [set(values) for values in payload.get("groups") or []]
        wanted.update(key for values in split_groups for key in values)
        if not wanted or not wanted.issubset(by_key):
            continue
        if row["action"] == "merge":
            affected = [group for group in groups if group & wanted]
            groups = [group for group in groups if not group & wanted]
            groups.append(set().union(*affected))
        elif row["action"] == "split":
            labels = {key: index for index, values in enumerate(split_groups) for key in values}
            affected = set().union(*(group for group in groups if group & wanted))
            # A later descendant inherits its closest explicitly assigned parent
            # group; append/backfill does not detach it merely because the old
            # correction predates that new message.
            for key in sorted(affected - wanted):
                trail, cursor, visited = [], key, set()
                while cursor not in labels and cursor in parent_for_key and cursor not in visited:
                    visited.add(cursor)
                    trail.append(cursor)
                    cursor = parent_for_key[cursor]
                if cursor in labels:
                    for member in trail:
                        labels[member] = labels[cursor]
                        split_groups[labels[cursor]].add(member)
            expanded = set(labels)
            remainder = [group - expanded for group in groups]
            groups = [group for group in remainder if group] + split_groups
        else:
            continue
        applied.append(row["correction_id"])
    return sorted(groups, key=lambda group: sorted(group)), applied


def _conversation_ids(package_id, groups, previous, parent_edges, by_key):
    """Keep the best overlapping old ID when appending or backfilling mail."""
    assigned, used, choices = {}, set(), []
    old_for_key = {key: cid for cid, old_members in previous.items() for key in old_members}
    parent_for_key = {edge["source_message_key"]: edge.get("target_message_key") for edge in parent_edges}
    for index, group in enumerate(groups):
        overlaps = collections.Counter(old_for_key[key] for key in group if key in old_for_key)
        for cid, overlap in overlaps.items():
            choices.append((-overlap, cid, min(group), index))
    for _, cid, _, index in sorted(choices):
        if index not in assigned and cid not in used:
            assigned[index] = cid
            used.add(cid)
    for index, group in enumerate(groups):
        if index in assigned:
            continue
        children = {key for key in group if parent_for_key.get(key) in group}
        roots = sorted(group - children, key=lambda key: _message_sort(by_key[key]))
        anchor = (roots or sorted(group))[0]
        cid = stable_id(package_id, "conversation", anchor)
        if cid in used:
            cid = stable_id(package_id, "conversation", sorted(group))
        assigned[index] = cid
        used.add(cid)
    return assigned


def rebuild(repository, package_id, owner_id, *, connection=None):
    if connection is None and not repository.get_package(package_id, owner_id):
        raise ValueError("邮件资料包不存在")
    if not Config.MAIL_ENABLE_CONVERSATIONS:
        return None
    context = repository._connection() if connection is None else nullcontext(connection)
    with context as connection:
        rebuild_relationships(repository, package_id, owner_id, connection=connection)
        thread_state = connection.execute("SELECT * FROM mail_foundation_state WHERE package_id=? AND stage='threads'", (package_id,)).fetchone()
        messages = [dict(row) for row in connection.execute("SELECT DISTINCT m.* FROM mail_messages m JOIN mail_visible_occurrences o ON o.message_key=m.message_key WHERE o.package_id=? ORDER BY m.message_key", (package_id,))]
        corrections = [dict(row) for row in connection.execute("SELECT * FROM mail_conversation_corrections WHERE package_id=? ORDER BY rowid", (package_id,))]
        fingerprint = stable_id(ALGORITHM_VERSION, thread_state["version"], Config.MAIL_SUBJECT_CANDIDATE_WINDOW_DAYS,
                                [(row["correction_id"], row["action"], row["payload_json"]) for row in corrections],
                                [(m["message_key"], m["subject_normalized"], m["sent_at_utc"]) for m in messages],
                                [tuple(row) for row in connection.execute("SELECT DISTINCT p.message_key,p.mime_path,p.is_explicit_attachment FROM mail_parts p JOIN mail_visible_occurrences o ON o.message_key=p.message_key WHERE o.package_id=? ORDER BY p.message_key,p.mime_path", (package_id,))])
        old = connection.execute("SELECT * FROM mail_foundation_state WHERE package_id=? AND stage='conversations'", (package_id,)).fetchone()
        if old and old["fingerprint"] == fingerprint:
            return {"generation_id": old["version"], "changed": False}
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
            return rebuild(repository, package_id, owner_id, connection=connection)
        generation = stable_id(package_id, "conversations", fingerprint)
        edges = [dict(row) for row in connection.execute("SELECT * FROM mail_thread_edge_history WHERE generation_id=? ORDER BY edge_id", (thread_state["version"],))]
        groups, applied = _group(messages, edges, corrections)
        by_key = {message["message_key"]: message for message in messages}
        previous = collections.defaultdict(set)
        for row in connection.execute("SELECT v.conversation_id,v.message_key FROM mail_conversation_members v JOIN mail_conversations c ON c.conversation_id=v.conversation_id WHERE c.package_id=?", (package_id,)):
            previous[row["conversation_id"]].add(row["message_key"])
        parent_edges = [edge for edge in edges if edge["relation_type"] in {"reply_to", "reference_parent"} and edge["status"] in {"exact", "inferred", "missing_target"}]
        parent_for_key = {edge["source_message_key"]: edge for edge in parent_edges}
        ids = _conversation_ids(package_id, groups, previous, parent_edges, by_key)
        participants = collections.defaultdict(set)
        for row in connection.execute("SELECT DISTINCT p.message_key,p.address_id FROM mail_message_participants p JOIN mail_visible_occurrences o ON o.message_key=p.message_key WHERE o.package_id=?", (package_id,)):
            participants[row["message_key"]].add(row["address_id"])
        attached = {row[0] for row in connection.execute("SELECT DISTINCT p.message_key FROM mail_parts p JOIN mail_visible_occurrences o ON o.message_key=p.message_key WHERE o.package_id=? AND p.is_explicit_attachment=1", (package_id,))}
        values, members, group_for_key = [], [], {}
        for index, group in enumerate(groups):
            cid = ids[index]
            group_for_key.update({key: cid for key in group})
            related = [parent_for_key[key] for key in group if key in parent_for_key]
            local_edges = [edge for edge in related if edge.get("target_message_key") in group]
            children, has_parent = collections.defaultdict(set), set()
            for edge in local_edges:
                children[edge["target_message_key"]].add(edge["source_message_key"])
                has_parent.add(edge["source_message_key"])
            roots = sorted(group - has_parent, key=lambda key: _message_sort(by_key[key]))
            roots = roots or sorted(group, key=lambda key: _message_sort(by_key[key]))
            root = roots[0]
            depths, queue = {key: 0 for key in roots}, collections.deque(roots)
            while queue:
                key = queue.popleft()
                for child in sorted(children[key]):
                    if child not in depths:
                        depths[child] = depths[key] + 1
                        queue.append(child)
            dates = sorted(by_key[key]["sent_at_utc"] for key in group if by_key[key]["sent_at_utc"])
            language_counts = collections.Counter(by_key[key]["body_language"] or "unknown" for key in sorted(group))
            language = sorted(language_counts, key=lambda item: (-language_counts[item], item))[0]
            basis = "exact_chain" if len(group) > 1 else "singleton"
            if any(edge["status"] == "inferred" for edge in related):
                basis = "inferred_context"
            if any(edge["status"] == "missing_target" for edge in related):
                basis = "missing_header_context"
            for row in corrections:
                if row["correction_id"] in applied:
                    payload = _load(row["payload_json"])
                    if set(payload.get("message_keys") or []) & group or any(set(item) & group for item in payload.get("groups") or []):
                        basis = "manual_browsing_group"
            value = (cid, package_id, root, by_key[root]["subject_normalized"], dates[0] if dates else None, dates[-1] if dates else None,
                     len(group), len(set().union(*(participants[key] for key in group))), int(bool(group & attached)), language, basis)
            values.append(value)
            members.extend((cid, key, depths.get(key, 0), int(key in roots)) for key in sorted(group))
        candidates, subjects, seen_pairs = [], collections.defaultdict(list), set()
        for message in messages:
            if message["subject_normalized"] and message["sent_at_utc"]:
                subjects[message["subject_normalized"]].append(message)
        for subject, rows in sorted(subjects.items()):
            rows.sort(key=_message_sort)
            # Compare neighbours for each participant rather than neighbours
            # in the entire subject bucket: unrelated people can interleave
            # their messages without hiding an otherwise plausible candidate.
            recent_for_address = {}
            for right in rows:
                b = right["message_key"]
                candidate_previous = {recent_for_address[address]["message_key"]: recent_for_address[address]
                                      for address in participants[b] if address in recent_for_address}
                for a, left in sorted(candidate_previous.items()):
                    pair = tuple(sorted((group_for_key[a], group_for_key[b])))
                    if pair[0] == pair[1] or pair in seen_pairs:
                        continue
                    try:
                        days = (datetime.fromisoformat(right["sent_at_utc"]) - datetime.fromisoformat(left["sent_at_utc"])).total_seconds() / 86400
                    except (ValueError, TypeError):
                        continue
                    if 0 <= days <= Config.MAIL_SUBJECT_CANDIDATE_WINDOW_DAYS:
                        seen_pairs.add(pair)
                        candidates.append((stable_id(package_id, "subject", a, b), package_id, b, a, "subject", "subject", None, "candidate", _json({"subject_normalized": subject, "days_apart": days, "shared_addresses": sorted(participants[a] & participants[b])})))
                for address in participants[b]:
                    recent_for_address[address] = right
        connection.execute("INSERT OR REPLACE INTO mail_relation_generations VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)", (generation, package_id, "conversations", fingerprint, ALGORITHM_VERSION, "ready"))
        for value in values:
            connection.execute("INSERT OR IGNORE INTO mail_conversation_history VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (generation,) + value)
        for value in members:
            connection.execute("INSERT OR IGNORE INTO mail_conversation_member_history VALUES(?,?,?,?,?)", (generation,) + value)
        for value in candidates:
            connection.execute("INSERT OR IGNORE INTO mail_thread_edge_history VALUES(?,?,?,?,?,?,?,?,?,?,?)", (generation, value[0], package_id, value[2], value[3], "message:" + value[3], value[4], value[5], value[6], value[7], value[8]))
        connection.execute("DELETE FROM mail_conversation_members WHERE conversation_id IN (SELECT conversation_id FROM mail_conversations WHERE package_id=?)", (package_id,))
        connection.execute("DELETE FROM mail_conversations WHERE package_id=?", (package_id,))
        connection.execute("DELETE FROM mail_thread_edges WHERE package_id=? AND relation_type='subject'", (package_id,))
        connection.executemany("INSERT INTO mail_conversations VALUES(?,?,?,?,?,?,?,?,?,?,?)", values)
        connection.executemany("INSERT INTO mail_conversation_members VALUES(?,?,?,?)", members)
        connection.executemany("INSERT INTO mail_thread_edges(edge_id,package_id,source_message_key,target_message_key,relation_type,source_header,target_message_id_raw,status,evidence_json) VALUES(?,?,?,?,?,?,?,?,?)", candidates)
        connection.execute("INSERT INTO mail_foundation_state(package_id,stage,version,fingerprint,status,cursor_json) VALUES(?,'conversations',?,?,'ready',?) "
                           "ON CONFLICT(package_id,stage) DO UPDATE SET version=excluded.version,fingerprint=excluded.fingerprint,status='ready',cursor_json=excluded.cursor_json,updated_at=CURRENT_TIMESTAMP",
                           (package_id, generation, fingerprint, _json({"thread_generation_id": thread_state["version"], "applied_corrections": applied})))
        connection.execute("INSERT OR REPLACE INTO mail_projection_state VALUES(?,'conversations',?)", (package_id, fingerprint))
    return {"generation_id": generation, "changed": True, "conversations": len(values)}


def _filters(date_from=None, date_to=None, q=None, has_attachment=None, address=None):
    return {"date_from": date_from, "date_to": date_to, "q": q, "has_attachment": has_attachment, "address": address}


def _filtered_messages(connection, package_id, filters, conversation_id=None, *, include_details=True):
    from .people import filtered_messages
    scope, args, normalized = filtered_messages(package_id, date_from=filters.get("date_from"), date_to=filters.get("date_to"),
                                                q=filters.get("q"), has_attachment=filters.get("has_attachment"), participant=filters.get("address"))
    filters.update(normalized)
    filters["address"] = normalized["participant"]
    clauses = ["m.message_key IN (" + scope + ")"]
    if conversation_id:
        clauses.append("EXISTS (SELECT 1 FROM mail_conversation_members v WHERE v.conversation_id=? AND v.message_key=m.message_key)")
        args.append(conversation_id)
    if not include_details:
        return [dict(row) for row in connection.execute("SELECT m.message_key FROM mail_messages m WHERE " + " AND ".join(clauses), args)]
    rows = [dict(row) for row in connection.execute("SELECT m.message_key,m.subject_raw,m.date_raw,m.sent_at_utc,m.timezone_offset_minutes,m.parse_status,m.warnings_json,m.headers_json,m.body_preferred_part,m.body_plain_part,m.body_html_part,"
            "(SELECT COUNT(*) FROM mail_parts p WHERE p.message_key=m.message_key AND p.is_explicit_attachment=1) attachment_count FROM mail_messages m WHERE " + " AND ".join(clauses) + " ORDER BY m.sent_at_utc IS NULL,m.sent_at_utc,m.message_key", args)]
    for message in rows:
        message["participants"] = [dict(row) for row in connection.execute("SELECT p.role,p.position,a.normalized_address address,a.display_name_sample display_name FROM mail_message_participants p JOIN mail_addresses a ON a.address_id=p.address_id WHERE p.message_key=? ORDER BY p.role,p.position,a.normalized_address", (message["message_key"],))]
        message["sender"] = ", ".join(item["address"] for item in message["participants"] if item["role"] == "from")
        message["date_status"] = "valid" if message["sent_at_utc"] else ("invalid" if message["date_raw"] else "missing")
        message["warnings"] = _load(message.pop("warnings_json"), [])
        message["headers"] = _load(message.pop("headers_json"))
        preferred = message["body_preferred_part"]
        message["body_fragments"] = [dict(row) for row in connection.execute(
            "SELECT mime_path,ordinal,fragment_kind,char_start,char_end,text,method,confidence "
            "FROM mail_body_fragments WHERE message_key=? AND mime_path=? ORDER BY ordinal",
            (message["message_key"], preferred))] if preferred else []
        # Empty and not-yet-backfilled bodies are distinct; the raw download
        # remains the authoritative source even when no reading view exists.
        message["body_status"] = "ready" if message["body_fragments"] else ("backfill_required" if preferred else "empty")
        message["attachments"] = [dict(row) for row in connection.execute(
            "SELECT p.message_key,p.mime_path,p.filename_raw,p.content_type,p.byte_size,p.payload_sha256,"
            "COALESCE(x.status,'not_started') processing_status,x.coverage_ratio,x.extracted_chars "
            "FROM mail_parts p LEFT JOIN mail_part_processing x ON x.message_key=p.message_key "
            "AND x.mime_path=p.mime_path AND x.stage='basic_text' "
            "WHERE p.message_key=? AND p.is_explicit_attachment=1 ORDER BY p.mime_path",
            (message["message_key"],))]
    return rows


def list_conversations(repository, package_id, owner_id, *, date_from=None, date_to=None, q=None, has_attachment=None, address=None):
    if not repository.get_package(package_id, owner_id):
        raise ValueError("邮件资料包不存在")
    if not Config.MAIL_ENABLE_CONVERSATIONS:
        return []
    rebuild(repository, package_id, owner_id)
    filters = _filters(date_from, date_to, q, has_attachment, address)
    with repository._connection() as connection:
        matches = {message["message_key"] for message in _filtered_messages(connection, package_id, filters, include_details=False)}
        rows = [dict(row) for row in connection.execute("SELECT * FROM mail_conversations WHERE package_id=? ORDER BY last_at IS NULL,last_at DESC,conversation_id", (package_id,))]
        state = connection.execute("SELECT version FROM mail_foundation_state WHERE package_id=? AND stage='conversations'", (package_id,)).fetchone()
        members = collections.defaultdict(set)
        for item in connection.execute("SELECT v.conversation_id,v.message_key FROM mail_conversation_members v JOIN mail_conversations c ON c.conversation_id=v.conversation_id WHERE c.package_id=?", (package_id,)):
            members[item["conversation_id"]].add(item["message_key"])
        values = []
        for row in rows:
            keys = members[row["conversation_id"]]
            if not keys & matches:
                continue
            row.update(total_message_count=row["message_count"], message_count=len(keys & matches), generation_id=state["version"] if state else None, filters=filters)
            values.append(row)
        return values


def detail(repository, conversation_id, owner_id, *, date_from=None, date_to=None, q=None, has_attachment=None, address=None):
    if not Config.MAIL_ENABLE_CONVERSATIONS:
        return None
    with repository._connection() as connection:
        row = connection.execute("SELECT v.* FROM mail_conversations v JOIN mail_packages p ON p.package_id=v.package_id WHERE v.conversation_id=? AND p.owner_id=?", (conversation_id, owner_id)).fetchone()
        if not row:
            return None
        package_id = row["package_id"]
    rebuild(repository, package_id, owner_id)
    filters = _filters(date_from, date_to, q, has_attachment, address)
    with repository._connection() as connection:
        row = connection.execute("SELECT * FROM mail_conversations WHERE conversation_id=? AND package_id=?", (conversation_id, package_id)).fetchone()
        if not row:
            return None
        result = dict(row)
        messages = _filtered_messages(connection, package_id, filters, conversation_id)
        all_keys = {row[0] for row in connection.execute("SELECT message_key FROM mail_conversation_members WHERE conversation_id=?", (conversation_id,))}
        keys = {message["message_key"] for message in messages}
        member_data = {row["message_key"]: dict(row) for row in connection.execute("SELECT * FROM mail_conversation_members WHERE conversation_id=?", (conversation_id,))}
        for message in messages:
            message.update(depth=member_data[message["message_key"]]["depth"], is_root=member_data[message["message_key"]]["is_root"])
        thread_state = connection.execute("SELECT version FROM mail_foundation_state WHERE package_id=? AND stage='threads'", (package_id,)).fetchone()
        conversation_state = connection.execute("SELECT version,cursor_json FROM mail_foundation_state WHERE package_id=? AND stage='conversations'", (package_id,)).fetchone()
        edges = []
        for row in connection.execute("SELECT * FROM mail_thread_edges WHERE package_id=? ORDER BY relation_type,edge_id", (package_id,)):
            edge = dict(row)
            if edge["source_message_key"] not in keys and edge.get("target_message_key") not in keys:
                continue
            edge["evidence"] = _load(edge.pop("evidence_json"))
            edge["source_node_id"] = "message:" + edge["source_message_key"]
            normalized = edge["evidence"].get("message_id_normalized")
            edge["target_node_id"] = ("message:" + edge["target_message_key"] if edge.get("target_message_key") else "missing:" + stable_id(package_id, normalized) if edge["status"] == "missing_target" else None)
            edge["outside_filter"] = bool(edge.get("target_message_key") and edge["target_message_key"] not in keys or edge["source_message_key"] not in keys)
            edge["outside_conversation"] = bool(edge.get("target_message_key") and edge["target_message_key"] not in all_keys or edge["source_message_key"] not in all_keys)
            edges.append(edge)
        tree_edges = [edge for edge in edges if edge["relation_type"] in {"reply_to", "reference_parent"}]
        context_edges = [edge for edge in edges if edge["relation_type"] == "reference"]
        candidate_edges = [edge for edge in edges if edge["relation_type"] == "subject"]
        missing_context = []
        for edge in edges:
            if edge["status"] != "missing_target":
                continue
            item = dict(edge)
            item["gap_kind"] = {"reply_to": "direct_parent", "reference_parent": "inferred_parent",
                                "reference": "historical_context"}.get(edge["relation_type"], "unknown")
            missing_context.append(item)
        # A single missing mail can occur in many descendants' References.
        # Expose its unique count separately from the number of header links.
        gap_summary = {"missing_message_count": len({item["target_node_id"] for item in missing_context}),
                       "direct_parent_gap_count": sum(item["gap_kind"] == "direct_parent" for item in missing_context),
                       "inferred_parent_gap_count": sum(item["gap_kind"] == "inferred_parent" for item in missing_context),
                       "historical_reference_count": sum(item["gap_kind"] == "historical_context" for item in missing_context)}
        bridge_messages = [dict(row) for row in connection.execute(
            "SELECT m.message_key,m.headers_json FROM mail_messages m JOIN mail_conversation_members v "
            "ON v.message_key=m.message_key WHERE v.conversation_id=?", (conversation_id,))]
        context_bridges = [bridge for bridge in _gap_contexts(bridge_messages, edges)
                           if bridge["source_message_key"] in keys or bridge["ancestor_message_key"] in keys]
        visible_nodes = {"message:" + key for key in keys}
        for edge in tree_edges:
            if edge.get("target_node_id"):
                visible_nodes.add(edge["target_node_id"])
            visible_nodes.add(edge["source_node_id"])
        nodes = []
        for row in connection.execute("SELECT * FROM mail_thread_nodes WHERE generation_id=? ORDER BY node_id", (thread_state["version"],)):
            node = dict(row)
            if node["node_id"] not in visible_nodes:
                continue
            node.update(missing=node["node_kind"] == "missing", label=node["message_id_normalized"] or node["message_key"], outside_filter=bool(node["message_key"] and node["message_key"] not in keys))
            nodes.append(node)
        anomalies = []
        for row in connection.execute("SELECT * FROM mail_thread_anomalies WHERE generation_id=? ORDER BY anomaly_type,anomaly_id", (thread_state["version"],)):
            item = dict(row)
            payload = _load(item.pop("payload_json"))
            if item["message_key"] not in keys and not keys.intersection(payload.get("candidates") or []) and not keys.intersection(payload.get("members") or []):
                continue
            item.update(type=item["anomaly_type"], payload=payload, detail=payload)
            anomalies.append(item)
        corrections = [dict(row) for row in connection.execute("SELECT * FROM mail_conversation_corrections WHERE package_id=? ORDER BY rowid", (package_id,))]
        revoked = {_load(item["payload_json"]).get("correction_id") for item in corrections if item["action"] == "revoke"}
        relevant_corrections = set()
        for item in corrections:
            item["payload"] = _load(item.pop("payload_json"))
            payload = item["payload"]
            affected = set(payload.get("message_keys") or [])
            affected.update(key for group in payload.get("groups") or [] for key in group)
            if affected & all_keys:
                relevant_corrections.add(item["correction_id"])
            item["revoked"] = item["correction_id"] in revoked
        corrections = [item for item in corrections if item["correction_id"] in relevant_corrections
                       or item["action"] == "revoke" and item["payload"].get("correction_id") in relevant_corrections]
        people = {}
        for message in messages:
            for person in message["participants"]:
                address = person["address"]
                entry = people.setdefault(address, {"address": address, "display_name": person["display_name"], "roles": set()})
                entry["roles"].add(person["role"])
        participants = [{**person, "roles": sorted(person["roles"])} for _, person in sorted(people.items())]
        attachments = [part for message in messages for part in message["attachments"]]
        dates = sorted(message["sent_at_utc"] for message in messages if message["sent_at_utc"])
    result.update(messages=messages, timeline=messages, edges=edges, tree_nodes=nodes, tree_edges=tree_edges, context_edges=context_edges,
                  candidate_edges=candidate_edges, anomalies=anomalies, corrections=corrections, filters=filters,
                  generation_id=conversation_state["version"], thread_generation_id=thread_state["version"],
                  total_message_count=result["message_count"], message_count=len(messages),
                  applied_corrections=sorted(set(_load(conversation_state["cursor_json"]).get("applied_corrections", [])) & relevant_corrections),
                  attachments=attachments, participants=participants, missing_context=missing_context,
                  gap_summary=gap_summary, context_bridges=context_bridges,
                  filtered_time_range={"from": dates[0] if dates else None, "to": dates[-1] if dates else None})
    return result


def apply_correction(repository, package_id, owner_id, *, action, message_keys=None, groups=None, correction_id=None, reason=None):
    """Audit merge/split/revoke; adjust browsing groups, never mail headers."""
    if not repository.get_package(package_id, owner_id):
        raise ValueError("邮件资料包不存在")
    action = str(action or "").casefold()
    if action not in {"merge", "split", "revoke"}:
        raise ValueError("会话纠正仅支持 merge、split、revoke")
    rebuild(repository, package_id, owner_id)
    with repository._connection() as connection:
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        keys = {row[0] for row in connection.execute("SELECT DISTINCT message_key FROM mail_visible_occurrences WHERE package_id=?", (package_id,))}
        requested = sorted(set(str(key) for key in message_keys or []))
        split_groups = [sorted(set(str(key) for key in values)) for values in groups or []]
        payload = {"reason": str(reason or "")[:2000]}
        if action == "revoke":
            target = connection.execute("SELECT * FROM mail_conversation_corrections WHERE correction_id=? AND package_id=?", (correction_id, package_id)).fetchone()
            if not target or target["action"] == "revoke":
                raise ValueError("找不到可撤销的会话纠正")
            payload["correction_id"] = correction_id
        elif action == "merge":
            if len(requested) < 2 or not set(requested).issubset(keys):
                raise ValueError("合并需要当前邮件包中至少两封邮件")
            payload["message_keys"] = requested
        else:
            flattened = [key for values in split_groups for key in values]
            if len(split_groups) < 2 or any(not values for values in split_groups) or len(set(flattened)) != len(flattened) or not set(flattened).issubset(keys):
                raise ValueError("拆分需要互不重叠且属于当前邮件包的至少两个邮件组")
            payload["groups"] = split_groups
        identifier = "mcc_" + uuid.uuid4().hex
        state = connection.execute("SELECT fingerprint FROM mail_foundation_state WHERE package_id=? AND stage='conversations'", (package_id,)).fetchone()
        connection.execute("INSERT INTO mail_conversation_corrections(correction_id,package_id,actor_id,action,payload_json,fingerprint) VALUES(?,?,?,?,?,?)", (identifier, package_id, owner_id, action, _json(payload), state["fingerprint"] if state else None))
        rebuild(repository, package_id, owner_id, connection=connection)
    return {"correction_id": identifier, "action": action, "payload": payload}


correct_conversation = apply_correction


def history(repository, package_id, owner_id, *, conversation_id=None, limit=100):
    if not repository.get_package(package_id, owner_id):
        raise ValueError("邮件资料包不存在")
    limit = max(1, min(1000, int(limit)))
    with repository._connection() as connection:
        clauses, args = ["g.package_id=?", "g.stage='conversations'"], [package_id]
        if conversation_id:
            clauses.append("EXISTS (SELECT 1 FROM mail_conversation_history h WHERE h.generation_id=g.generation_id AND h.conversation_id=?)")
            args.append(conversation_id)
        generations = [dict(row) for row in connection.execute("SELECT g.* FROM mail_relation_generations g WHERE " + " AND ".join(clauses) + " ORDER BY g.rowid DESC LIMIT ?", args + [limit])]
        corrections = [dict(row) for row in connection.execute("SELECT * FROM mail_conversation_corrections WHERE package_id=? ORDER BY rowid DESC LIMIT ?", (package_id, limit))]
        all_revoked = {_load(row[0]).get("correction_id") for row in connection.execute("SELECT payload_json FROM mail_conversation_corrections WHERE package_id=? AND action='revoke'", (package_id,))}
        for item in corrections:
            item["payload"] = _load(item.pop("payload_json"))
            item["revoked"] = item["correction_id"] in all_revoked
        current = connection.execute("SELECT version FROM mail_foundation_state WHERE package_id=? AND stage='conversations'", (package_id,)).fetchone()
        for generation in generations:
            generation["current"] = bool(current and current[0] == generation["generation_id"])
            generation["conversations"] = [dict(row) for row in connection.execute("SELECT * FROM mail_conversation_history WHERE generation_id=?" + (" AND conversation_id=?" if conversation_id else "") + " ORDER BY conversation_id", (generation["generation_id"], conversation_id) if conversation_id else (generation["generation_id"],))]
    return {"package_id": package_id, "conversation_id": conversation_id, "generations": generations, "corrections": corrections}


conversation_history = history
