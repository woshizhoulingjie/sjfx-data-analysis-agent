"""Bounded context reads and durable, scope-checked research checkpoints.

Context is separate from retrieval: a reply can qualify a hit without itself
being a hit.  Nothing here rebuilds projections or calls a model.
"""
from __future__ import annotations

import json
import re


CHANGE_MARKERS = re.compile(
    r"撤回|撤销|取消|作废|修改|修订|变更|调整|更新|尚未|未确认|暂缓|"
    r"如果|除非|前提|待批准|待确认|待审批|审批后|批准后|内部审批|等.{0,30}审批|"
    r"审核通过|审批通过|仅当|只有|需经|须经|拒绝|否认|不(?:批准|确认|同意|支持|认可|接受)|"
    r"未.{0,4}(?:批准|确认|同意)|没有.{0,4}(?:批准|确认|同意)|"
    r"\bcancel(?:led)?\b|\bwithdraw(?:n)?\b|"
    r"\brevis\w*\b|\bmodif\w*\b|\bupdated?\b|not confirmed|subject to|"
    r"provided that|conditional|\b(?:if|unless)\b|pending approval|awaiting approval|"
    r"not (?:yet )?(?:confirmed|approved|agreed|accepted)|do not (?:confirm|approve|agree|accept|support)|"
    r"cannot approve|no longer|ยกเลิก|แก้ไข|หาก|เงื่อนไข|ไม่ยืนยัน|ยังไม่|เว้นแต่", re.I,
)


def excerpt_attribution(source, excerpt=None):
    """Check the exact cited span, rather than attributing quoted replies to sender."""
    if source.get("source_kind") == "attachment":
        return "attachment"
    text = str(source.get("quote_text") or source.get("text") or "")
    excerpt = text if excerpt is None else str(excerpt)
    start = text.find(excerpt)
    if start < 0:
        return "unknown"
    # Repeated identical text in authored and quoted regions is ambiguous.
    if excerpt and text.find(excerpt, start + 1) >= 0:
        return "unknown"
    start += int(source.get("char_start") or 0)
    end = start + len(excerpt)
    fragments = source.get("body_fragments") or []
    kinds = {row["fragment_kind"] for row in fragments
             if int(row["char_start"]) < end and int(row["char_end"]) > start}
    covered = sum(max(0, min(end, int(row["char_end"])) - max(start, int(row["char_start"])))
                  for row in fragments)
    if covered < len(excerpt) or not kinds:
        return "unknown"
    return next(iter(kinds)) if len(kinds) == 1 else "mixed"


def read_context(tools, message_key, *, max_depth=8, max_messages=16, max_chunks=40, check=None):
    """Read the anchor, both directions of header links and current conversation.

    Missing targets, scope exclusions and every read cap are explicit gaps.
    Same-name attachment families are only candidates, never proved revisions.
    """
    repo, package_id, owner_id = tools.repository, tools.package_id, tools.owner_id
    check = check or (lambda: True)
    if not tools._message_allowed(message_key):
        raise ValueError("邮件不存在或不在当前资料范围")
    anchor = repo.get_message(message_key, owner_id, package_id)
    if not anchor:
        raise ValueError("邮件不存在或不在当前资料范围")
    gaps, edges, selected = [], [], {message_key}
    frontier = {message_key}
    def add_gap(reason):
        if reason not in gaps:
            gaps.append(reason)
    def select(key):
        if not key or key in selected:
            return
        if not tools._message_allowed(key):
            add_gap("related_messages_outside_scope")
        elif len(selected) >= max_messages:
            add_gap("message_limit")
        else:
            selected.add(key)
    # A check may read the job through the same non-reentrant Storage lock.
    # Materialize each bounded query before invoking checks or scope callbacks.
    for _ in range(max_depth):
        if not frontier or not check():
            if frontier:
                add_gap("budget_or_cancelled")
            break
        placeholders = ",".join("?" for _ in frontier)
        keys = sorted(frontier)
        with repo._connection() as c:
            rows = c.execute(
                "SELECT * FROM mail_thread_edges WHERE package_id=? AND "
                "(source_message_key IN (" + placeholders + ") OR target_message_key IN (" + placeholders + ")) "
                "ORDER BY edge_id LIMIT 201", [package_id] + keys + keys,
            ).fetchall()
        if len(rows) > 200:
            add_gap("edge_limit")
        before = set(selected)
        for row in rows[:200]:
            edge = dict(row)
            if edge["status"] == "exact":
                select(edge.get("source_message_key"))
                select(edge.get("target_message_key"))
            elif edge["status"] == "missing_target":
                add_gap("missing_reply_target")
            else:
                add_gap("uncertain_thread_links")
            if edge["edge_id"] not in {e["edge_id"] for e in edges}:
                edges.append({k: edge.get(k) for k in (
                    "edge_id", "source_message_key", "target_message_key", "status", "relation_type")})
        frontier = selected - before
    else:
        if frontier:
            add_gap("depth_limit")
    # Current browsing groups can include user corrections. Their member
    # links remain context candidates; header evidence above is retained.
    with repo._connection() as c:
        rows = c.execute(
            "SELECT v.message_key FROM mail_conversation_members v JOIN mail_conversations g "
            "ON g.conversation_id=v.conversation_id WHERE g.package_id=? AND v.conversation_id IN "
            "(SELECT x.conversation_id FROM mail_conversation_members x JOIN mail_conversations y "
            "ON y.conversation_id=x.conversation_id WHERE y.package_id=? AND x.message_key=?) "
            "ORDER BY v.message_key LIMIT 41", (package_id, package_id, message_key),
        ).fetchall()
    if len(rows) > 40:
        add_gap("conversation_limit")
    for row in rows[:40]:
        select(row[0])
    if tools.include_attachments:
        # The version projection is maintained by ingestion.  Reading it
        # must not trigger a whole-package rebuild during every answer.
        version_sql = ("SELECT DISTINCT l.message_key,l.mime_path,l.chain_key FROM mail_attachment_version_links l "
            "WHERE l.package_id=? AND l.chain_key IN (SELECT chain_key FROM mail_attachment_version_links "
            "WHERE package_id=? AND message_key=?)")
        version_args = [package_id, package_id, message_key]
        with repo._connection() as c:
            if tools.allowed_keys is not None:
                from .scope import scope_clause
                version_sql += scope_clause(c,tools.allowed_keys,'l.message_key')
            versions = c.execute(version_sql + ' ORDER BY l.message_key,l.mime_path LIMIT 41', version_args).fetchall()
        if len(versions) > 40:
            add_gap("attachment_version_limit")
        for row in versions[:40]:
            select(row["message_key"])
        if any(p.get("is_explicit_attachment") for p in anchor.get("parts") or []) and not versions:
            add_gap("attachment_versions_unavailable")
    else:
        versions = []
        if any(p.get("is_explicit_attachment") for p in anchor.get("parts") or []):
            add_gap("attachments_excluded_by_scope")
    # Group corrections and attachment families may introduce nodes that
    # the anchor's exact-header walk did not visit. Inspect their boundary
    # rather than falsely certifying their unseen neighbouring replies.
    placeholders = ",".join("?" for _ in selected)
    keys = sorted(selected)
    with repo._connection() as c:
        boundary = c.execute(
            "SELECT * FROM mail_thread_edges WHERE package_id=? AND (source_message_key IN ("
            + placeholders + ") OR target_message_key IN (" + placeholders + ")) ORDER BY edge_id LIMIT 201",
            [package_id] + keys + keys).fetchall()
    if len(boundary) > 200:
        add_gap("edge_limit")
    for row in boundary[:200]:
        if row["status"] == "missing_target":
            add_gap("missing_reply_target")
        elif row["status"] != "exact":
            add_gap("uncertain_thread_links")
        elif any(key and key not in selected for key in (row["source_message_key"], row["target_message_key"])):
            add_gap("context_group_neighbours_not_expanded")
    messages = []
    for key in sorted(selected):
        if not check():
            add_gap("budget_or_cancelled")
            break
        message = anchor if key == message_key else repo.get_message(key, owner_id, package_id)
        if not message:
            add_gap("related_message_unavailable")
            continue
        messages.append(message)
    # The anchor is always included; newest replies are read before older ones
    # when the evidence budget is tight, so withdrawals are less likely to hide.
    messages = [m for m in messages if m["message_key"] == message_key] + sorted(
        (m for m in messages if m["message_key"] != message_key),
        key=lambda m: (m.get("sent_at_utc") or "", m["message_key"]), reverse=True)
    items, metadata, remaining = [], [], tools.context_limit
    for message in messages:
        if not check():
            add_gap("budget_or_cancelled")
            break
        key = message["message_key"]
        metadata.append({"message_key": key, "sent_at_utc": message.get("sent_at_utc"),
                         "subject_raw": message.get("subject_raw"), "parse_status": message.get("parse_status")})
        if message.get("parse_status") == "failed":
            add_gap("message_parse_failed")
        parts = message.get("parts") or []
        with repo._connection() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM mail_text_segments WHERE package_id=? AND message_key=? "
                + ("" if tools.include_attachments else "AND source_kind!='attachment' ")
                + "ORDER BY source_kind DESC,mime_path,ordinal LIMIT 81", (package_id, key))]
            fragments = [dict(r) for r in c.execute(
                "SELECT mime_path,fragment_kind,char_start,char_end,method,confidence FROM mail_body_fragments "
                "WHERE message_key=? ORDER BY mime_path,ordinal LIMIT 2001", (key,))]
        if len(fragments) > 2000:
            fragments = fragments[:2000]
            add_gap("fragment_limit")
        if len(rows) > 80:
            add_gap("segment_limit")
        if not any(r.get("source_kind") == "body" for r in rows):
            add_gap("body_text_unavailable")
        if tools.include_attachments:
            for part in parts:
                if not part.get("is_explicit_attachment"):
                    continue
                if not any(r["mime_path"] == part["mime_path"] and r["source_kind"] == "attachment" for r in rows):
                    add_gap("attachment_text_unavailable")
                if part.get("processing_status") not in {"ready", "skipped"}:
                    add_gap("attachment_processing_incomplete")
        for row in rows[:80]:
            text = str(row.get("text") or "")
            for offset in range(0, len(text), 1000):
                if not check():
                    add_gap("budget_or_cancelled")
                    break
                if len(items) >= max_chunks or remaining <= 0:
                    add_gap("context_text_limit")
                    break
                snippet = text[offset:offset + min(1000, remaining)]
                value = {**row, "text": snippet, "char_start": int(row.get("char_start") or 0) + offset}
                value["char_end"] = value["char_start"] + len(snippet)
                value.update(repo.create_evidence(package_id, owner_id, value))
                value.update(sent_at_utc=message.get("sent_at_utc"), subject_raw=message.get("subject_raw"),
                             context_anchor=message_key, context_role="anchor" if key == message_key else "related",
                             body_fragments=[f for f in fragments if f["mime_path"] == row["mime_path"]
                                             and f["char_start"] < value["char_end"] and f["char_end"] > value["char_start"]])
                value["attribution"] = excerpt_attribution(value)
                items.append(value)
                remaining -= len(snippet)
                if len(snippet) < min(1000, len(text) - offset):
                    add_gap("context_text_limit")
            if remaining <= 0 or len(items) >= max_chunks:
                if rows.index(row) < len(rows) - 1:
                    add_gap("context_text_limit")
                break
    if len(metadata) < len(selected):
        add_gap("messages_not_read")
    return {"items": items, "review": {
        "message_key": message_key, "message_keys": [m["message_key"] for m in metadata],
        "messages": metadata, "edges": edges[:80],
        "attachment_version_candidates": [{k: r[k] for k in ("message_key", "mime_path", "chain_key")}
                                          for r in versions[:40] if tools._message_allowed(r["message_key"])],
        "version_basis": "filename families are candidates; no content or chronology is inferred",
        "gaps": gaps, "complete": not gaps, "read_chars": tools.context_limit - remaining,
        "evidence_ids": [i["evidence_id"] for i in items],
        "change_evidence_ids": [i["evidence_id"] for i in items if CHANGE_MARKERS.search(i["quote_text"])],
    }}


def load_memory(repository, session_id, turn_id, owner_id, package_id, allowed_keys, source_revision,
                *, research_version=None, index_epoch=None, scope=None):
    """Only a prior checkpoint in this owned session is eligible as a hint.

    Never treat a checkpoint as source evidence.  Changed sources or excluded
    message keys make its facts unusable for this turn.
    """
    with repository._connection() as c:
        row = c.execute(
            "SELECT s.input_json,s.step_id,s.sequence,t.turn_id FROM mail_research_steps s JOIN mail_research_turns t ON t.turn_id=s.turn_id "
            "JOIN mail_research_sessions r ON r.session_id=t.session_id WHERE t.session_id=? AND t.turn_id!=? "
            "AND t.owner_id=? AND r.owner_id=? AND r.package_id=? AND s.tool_name='research_checkpoint' "
            "AND t.rowid<(SELECT rowid FROM mail_research_turns WHERE turn_id=? AND owner_id=?) "
            "AND s.status='completed' ORDER BY t.rowid DESC,s.sequence DESC LIMIT 1",
            (session_id, turn_id, owner_id, owner_id, package_id, turn_id, owner_id),
        ).fetchone()
    if not row:
        return {"facts": [], "conditions": [], "contradictions": [], "open_questions": []}
    try:
        value = json.loads(row["input_json"])
    except (ValueError, TypeError):
        return {"facts": [], "conditions": [], "contradictions": [], "open_questions": ["上轮检查点无法读取"]}
    if str(value.get("source_revision")) != str(source_revision):
        return {"facts": [], "conditions": [], "contradictions": [], "open_questions": ["资料版本已变化，上轮结论需重新核验"],
                "prior_turn_id": row["turn_id"], "stale": True}
    if research_version is not None and (value.get('research_version') != research_version or
            (index_epoch is not None and value.get('index_epoch') != index_epoch)):
        return {'facts': [], 'conditions': [], 'contradictions': [],
                'open_questions': ['研究或索引版本已变化，上轮结论需重新核验'],
                'prior_turn_id': row['turn_id'], 'stale': True}
    for field in ("facts", "conditions", "contradictions"):
        value[field] = [item for item in value.get(field) or []
                        if isinstance(item, dict) and item.get("message_keys")
                        and (allowed_keys is None or set(item["message_keys"]).issubset(allowed_keys))
                        and (not scope or scope.get('include_explicit_attachments', True)
                             or not item.get('includes_attachment', True))][:20]
    value["prior_turn_id"] = row["turn_id"]
    value["checkpoint_ref"] = {"turn_id": row["turn_id"], "step_id": row["step_id"], "sequence": row["sequence"]}
    if allowed_keys is not None or (scope and not scope.get('include_explicit_attachments', True)):
        # Old plain-string questions lack per-item provenance.  A narrowed
        # scope may not receive their content as model-visible memory.
        value["open_questions"] = ["历史待核实问题需在本轮选定范围内重新核验"] if value.get("open_questions") else []
        value["context_reviews"] = [r for r in value.get("context_reviews") or []
                                    if isinstance(r,dict) and (allowed_keys is None or
                                       set(r.get("message_keys") or []).issubset(allowed_keys))]
    value["hint_only"] = True
    return value


def checkpoint(spec, claims, evidence, reviews, prior=None):
    by_id = {item["evidence_id"]: item for item in evidence}
    facts, conditions, contradictions, questions = [], [], [], []
    for claim in claims:
        ids = list(dict.fromkeys((claim.get("evidence_ids") or []) + (claim.get("counter_evidence_ids") or [])))
        item = {"text": claim.get("claim_text"), "state": claim.get("state"),
                "subject_ref": claim.get("subject_ref"), "object_ref": claim.get("object_ref"),
                "evidence_ids": ids, "message_keys": list(dict.fromkeys(by_id[e]["message_key"] for e in ids if e in by_id)),
                'includes_attachment': any(by_id[e].get('source_kind') == 'attachment' for e in ids if e in by_id),
                "support_status": claim.get("support_status"), "verification": claim.get("verification")}
        if claim.get("support_status") == "supported":
            facts.append(item)
        elif claim.get("state") == "conflicting":
            contradictions.append(item)
        else:
            questions.append(str(claim.get("uncertainty") or claim.get("claim_text") or "主张待核实")[:500])
    for item in evidence:
        text = str(item.get("quote_text") or "")
        if CHANGE_MARKERS.search(text):
            conditions.append({"text": text[:1000], "evidence_ids": [item["evidence_id"]],
                               'includes_attachment': item.get('source_kind') == 'attachment',
                               "message_keys": [item["message_key"]], "attribution": item.get("attribution")})
    for review in reviews.values():
        questions.extend("{}: {}".format(review["message_key"], reason) for reason in review.get("gaps") or [])
    # Carry forward scoped, revision-valid items, retaining their original IDs.
    prior = prior or {}
    def merge(old, new):
        selected = {}
        for item in old + new:
            key = (item.get("text"), tuple(item.get("message_keys") or []), item.get("state"), item.get("support_status"))
            selected[key] = item
        return list(selected.values())
    all_values = {"facts": merge(prior.get("facts") or [], facts),
                  "conditions": merge(prior.get("conditions") or [], conditions),
                  "contradictions": merge(prior.get("contradictions") or [], contradictions),
                  "open_questions": list(dict.fromkeys((prior.get("open_questions") or []) + questions))}
    limits = {"facts": 20, "conditions": 20, "contradictions": 20, "open_questions": 30, "context_reviews": 16}
    omitted = {field: max(0, len(values) - limits[field]) for field, values in all_values.items()}
    omitted["context_reviews"] = max(0, len(reviews) - limits["context_reviews"])
    return {"version": 2, "source_revision": spec.get("source_revision"),
            'research_version': spec.get('research_version'), 'index_epoch': spec.get('index_epoch'),
            'scope': dict(spec.get('scope') or {}), 'as_of': (spec.get('research_plan') or {}).get('as_of'),
            **{field: values[-limits[field]:] for field, values in all_values.items()},
            "summary_limits": limits, "omitted_counts": omitted,
            "summary_truncated": any(omitted.values()) or bool(prior.get("summary_truncated")),
            "historical_summary_truncated": bool(prior.get("summary_truncated")),
            "prior_checkpoint_ref": prior.get("checkpoint_ref"),
            "history_note": "这是有限检查点摘要；完整逐轮 evidence、claims、context_review 留在历史 turn.final_json，检查点留在 research_steps。",
            "context_reviews": [{k: r.get(k) for k in ("message_key", "message_keys", "complete", "gaps", "evidence_ids")}
                                for r in reviews.values()][:16], "hint_only": True}
