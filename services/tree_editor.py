"""Persistent, conservative manual edits for the content analysis tree.

The generated tree remains the source of truth for discovery.  Manual edits are
stored as small operations and replayed on read, so a re-analysis can replace
the generated tree without destroying user decisions.
"""
from copy import deepcopy
import hashlib


def _walk(node):
    yield node
    for child in node.get("children") or []:
        yield from _walk(child)


def _find(root, node_id):
    return next((item for item in _walk(root) if item.get("node_id") == node_id), None)


def _find_parent(root, node_id):
    """Return the direct parent and child position for a semantic node."""
    for parent in _walk(root):
        for index, child in enumerate(parent.get("children") or []):
            if child.get("node_id") == node_id:
                return parent, index
    return None, None


def _file_leaf(path, source=None, status="manual"):
    source = source or {}
    return {
        "kind": "file", "name": str(path).replace("\\", "/").rsplit("/", 1)[-1],
        "path": path, "size": int(source.get("size") or 0),
        "size_human": source.get("size_human") or "", "classification_status": status,
        "manual_membership": True, "content_topics": [], "related_topics": [],
        "evidence_ids": [],
    }


def _manual_id(prefix, values):
    raw = "|".join(str(value) for value in values)
    return "manual-{}-{}".format(prefix, hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16])


def _clone_for_path(path, source_leaf=None, topic=None):
    leaf = deepcopy(source_leaf) if source_leaf else _file_leaf(path)
    leaf["path"] = path
    leaf["name"] = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    leaf["manual_membership"] = True
    memberships = list(leaf.get("topic_memberships") or [])
    if topic and topic not in memberships:
        memberships.append(topic)
    leaf["topic_memberships"] = memberships
    return leaf


def _set_confirmed(node, confirmed=True):
    node["manual_confirmed"] = bool(confirmed)
    node["classification_status"] = "confirmed" if confirmed else node.get("classification_status", "classified")
    node["classification_source"] = "human" if confirmed else node.get("classification_source", "automatic")
    return node


def apply_tree_edits(analysis, edits):
    """Replay validated edit records onto a copy of an analysis payload."""
    result = deepcopy(analysis or {})
    tree = result.get("analysis_tree") or {}
    result["analysis_tree"] = tree
    edits = list(edits or [])
    suppressed = set()
    for edit in edits:
        operation = str(edit.get("operation") or "").strip().lower()
        payload = edit.get("payload") or {}
        target_id = str(payload.get("edit_id") or "").strip()
        if operation == "undo" and target_id:
            suppressed.add(target_id)
        elif operation == "redo" and target_id:
            suppressed.discard(target_id)
    for edit in edits:
        operation = str(edit.get("operation") or "").strip().lower()
        if operation in {"undo", "redo"}:
            continue
        edit_id = str(edit.get("edit_id") or "").strip()
        if edit_id and edit_id in suppressed:
            continue
        payload = edit.get("payload") or {}
        if operation == "rename":
            node = _find(tree, payload.get("node_id"))
            if node and str(payload.get("name") or "").strip():
                node["name"] = str(payload["name"]).strip()[:120]
                node["manual_name"] = True
                node["classification_source"] = "human"
        elif operation == "confirm":
            node = _find(tree, payload.get("node_id"))
            if node:
                _set_confirmed(node, bool(payload.get("confirmed", True)))
        elif operation == "mount":
            node = _find(tree, payload.get("node_id"))
            path = str(payload.get("path") or "").replace("\\", "/")
            if not node or node.get("kind") != "group" or not path:
                continue
            children = node.setdefault("children", [])
            if not any(child.get("kind") == "file" and child.get("path") == path for child in children):
                source = next((item for item in _walk(tree) if item.get("kind") == "file" and item.get("path") == path), None)
                children.append(_clone_for_path(path, source, node.get("name")))
            paths = list(node.get("member_paths") or [])
            if path not in paths:
                paths.append(path)
            node["member_paths"] = sorted(set(paths))
            node["file_count"] = len(node["member_paths"])
            node["manual_membership"] = True
        elif operation == "merge":
            ids = list(dict.fromkeys(str(value) for value in payload.get("node_ids") or [] if value))
            if len(ids) < 2:
                continue
            parent, _position = _find_parent(tree, ids[0])
            if parent is None:
                continue
            children = parent.get("children") or []
            selected = [
                item for item in children
                if item.get("node_id") in ids and item.get("kind") == "group"
            ]
            if len(selected) != len(ids):
                continue
            position = min(children.index(item) for item in selected)
            members = sorted(set(path for item in selected for path in item.get("member_paths") or []))
            merged = deepcopy(selected[0])
            merged["node_id"] = _manual_id("merge", ids)
            merged["name"] = str(payload.get("name") or "合并主题").strip()[:120]
            merged["member_paths"] = members
            merged["file_count"] = len(members)
            merged["children"] = []
            for path in members:
                source = next((leaf for item in selected for leaf in item.get("children") or [] if leaf.get("path") == path), None)
                merged["children"].append(_clone_for_path(path, source, merged["name"]))
            merged["manual_merged"] = True
            merged["classification_source"] = "human"
            parent["children"] = [item for item in children if item not in selected]
            parent["children"][position:position] = [merged]
        elif operation == "split":
            node = _find(tree, payload.get("node_id"))
            groups = payload.get("groups") or []
            if not node or node.get("kind") != "group" or not isinstance(groups, list) or len(groups) < 2:
                continue
            original = set(node.get("member_paths") or [])
            requested = [
                str(path)
                for group in groups if isinstance(group, dict)
                for path in group.get("paths") or [] if path
            ]
            # A partial/stale browser projection must never make unassigned
            # files disappear from the derived tree. Invalid historical edits
            # are ignored even if they predate the API-side validation.
            if len(requested) != len(set(requested)) or set(requested) != original:
                continue
            replacements = []
            used = set()
            for index, spec in enumerate(groups, 1):
                paths = sorted(set(str(value) for value in spec.get("paths") or []) & original - used)
                if not paths:
                    continue
                used.update(paths)
                child_by_path = {leaf.get("path"): leaf for leaf in node.get("children") or []}
                replacements.append({
                    "kind": "group", "node_id": _manual_id("split", [node.get("node_id"), index, *paths]),
                    "dimension": node.get("dimension", "内容主题"), "name": str(spec.get("name") or "子主题{}".format(index)).strip()[:120],
                    "member_paths": paths, "file_count": len(paths), "children": [_clone_for_path(path, child_by_path.get(path)) for path in paths],
                    "classification_status": "confirmed", "classification_source": "human", "manual_split": True,
                    "summary": "人工拆分主题，共 {} 个文件。".format(len(paths)), "evidence_chain": [], "conclusion_evidence": [],
                })
            if len(replacements) >= 2:
                parent, position = _find_parent(tree, node.get("node_id"))
                if parent is None:
                    continue
                children = parent.get("children") or []
                parent["children"] = [
                    item for item in children
                    if item.get("node_id") != node.get("node_id")
                ]
                parent["children"][position:position] = replacements
    result["manual_tree_edits"] = edits
    return result


def filter_tree(tree, status="all"):
    """Return a tree containing files matching a review status."""
    wanted = str(status or "all").strip().lower()
    if wanted in {"", "all", "全部"}:
        return deepcopy(tree or {})

    def file_matches(node):
        state = str(node.get("classification_status") or "classified").lower()
        confidence = node.get("classification_confidence")
        if wanted in {"low_confidence", "低置信度"}:
            try:
                return state not in {"failed", "unclassified"} and float(confidence or 0) < 0.65
            except (TypeError, ValueError):
                return False
        if wanted in {"unclassified", "未分类"}:
            return state in {"unclassified", "pending"}
        if wanted in {"failed", "解析失败"}:
            return state == "failed"
        if wanted in {"confirmed", "人工确认"}:
            return bool(node.get("manual_confirmed"))
        return state == wanted

    def count_files(node):
        if node.get("kind") == "file":
            return 1
        return sum(count_files(child) for child in node.get("children") or [])

    def matches(node):
        if node.get("kind") == "file":
            return deepcopy(node) if file_matches(node) else None
        # Confirming a topic is an explicit review decision about the whole
        # topic. Keep that topic and all of its descendants in this view.
        if wanted in {"confirmed", "人工确认"} and node.get("manual_confirmed"):
            return deepcopy(node)
        children = []
        for child in node.get("children") or []:
            matched = matches(child)
            if matched:
                children.append(matched)
        if children:
            clone = deepcopy(node)
            clone["children"] = children
            clone["file_count"] = sum(count_files(child) for child in children)
            return clone
        return None

    filtered = matches(tree)
    return filtered or {"kind": "analysis_root", "name": tree.get("name", "数据包"), "path": ".", "children": [], "filter_status": wanted}
