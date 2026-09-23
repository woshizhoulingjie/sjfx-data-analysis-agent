"""Bounded source selection used only by ordinary preliminary summaries."""
import json
import re

INPUT_VERSION = "selective-6500-v2-source-ids"


def _clean(value):
    return " ".join(str(value or "").split())


def _boundary(text, budget, tail=False):
    if len(text) <= budget:
        return text
    part = text[-budget:] if tail else text[:budget]
    stops = list(re.finditer(r"[。！？!?\n]|\.(?=\s)", part))
    if stops:
        if tail and stops[0].end() < budget // 2:
            return part[stops[0].end():].strip()
        if not tail and stops[-1].end() > budget // 2:
            return part[:stops[-1].end()].strip()
    # No sentence boundary (e.g. a long record): prefer a whitespace boundary.
    at = part.find(" ") if tail else part.rfind(" ")
    if 0 < at < len(part) - 1:
        return part[at + 1:] if tail else part[:at]
    return part


def build_preliminary_input(document, max_chars=6500):
    limit = max(2000, min(6500, int(max_chars)))
    raw = _clean(document.get("text"))
    structure = document.get("structure") or {}
    source = document.get("source") or {}
    title = _clean(structure.get("title") or source.get("name"))
    headings = [_clean(h) for h in structure.get("headings") or [] if _clean(h)]
    parts, selected = [], []

    def add(label, value, budget, tail=False, exact=False):
        text = _clean(value)
        # Exact containment only: different numbers/negations are never fuzzy-deduped.
        if not text or any(text in previous for previous in selected):
            return
        remaining = limit - sum(len(p) + 2 for p in parts) - len(label) - 2
        if remaining < 40:
            return
        if exact and len(text) > min(budget, remaining):
            return
        text = _boundary(text, min(budget, remaining), tail=tail)
        if not text or any(text in previous for previous in selected):
            return
        selected.append(text)
        parts.append(label + "：" + text)

    add("标题与章节", title + ("；" + "、".join(headings[:24]) if headings else ""), 500)
    if raw and len(raw) + sum(len(p) + 2 for p in parts) + 4 <= limit:
        add("正文", raw, limit)
        return "\n\n".join(parts)
    add("正文开头", raw, 1200)
    add("正文结尾", raw, 1300, tail=True)
    evidence = [e for e in document.get("evidence") or []
                if isinstance(e, dict) and _clean(e.get("text"))]
    evidence.sort(key=lambda e: (int(e.get("page") or 0), int(e.get("char_start") or 0)))
    # Four separated positions supplement the head/tail even with no evidence units.
    mid_sources = []
    if evidence:
        for ratio in (0.2, 0.4, 0.6, 0.8):
            e = evidence[min(len(evidence) - 1, int((len(evidence) - 1) * ratio))]
            evidence_id = _clean(e.get("evidence_id"))
            label = "原文证据" + ("(ID=" + evidence_id + ")" if evidence_id else "")
            label += ("第%s页" % e["page"] if e.get("page") else _clean(e.get("section"))[:50])
            mid_sources.append((label, e["text"]))
    else:
        for ratio in (0.2, 0.4, 0.6, 0.8):
            start = int(len(raw) * ratio)
            mid_sources.append(("正文中部", raw[start:start + 850]))
    for label, text in mid_sources:
        add(label, text, 550)
    profile = document.get("data_profile") or {}
    if profile:
        compact = {k: profile[k] for k in ("row_count", "column_count", "coverage", "limits") if k in profile}
        columns = {}
        for name, value in list((profile.get("columns") or {}).items())[:8]:
            columns[name] = {k: value[k] for k in ("inferred_type", "count", "min", "max", "sum", "mean") if k in (value or {})}
        compact["columns"] = columns
        # Include complete records only; never truncate a structured number or JSON object.
        table_lines = []
        for key, value in compact.items():
            line = json.dumps({key: value}, ensure_ascii=False)
            if len("; ".join(table_lines + [line])) <= 1000:
                table_lines.append(line)
        add("表格口径", "; ".join(table_lines), 1000, exact=True)
    else:
        numeric = [e for e in evidence if re.search(r"\d", str(e.get("text") or ""))]
        for e in numeric[:8]:
            before = sum(len(p) for p in parts)
            evidence_id = _clean(e.get("evidence_id"))
            add("数值原文" + ("(ID=" + evidence_id + ")" if evidence_id else ""), e["text"], 500)
            if sum(len(p) for p in parts) - before > 0:
                break
    # Reassign unused budgets to distinct middle evidence, without repeating text.
    for ratio in (0.5, 0.3, 0.7, 0.1, 0.9):
        if evidence:
            e = evidence[min(len(evidence) - 1, int((len(evidence) - 1) * ratio))]
            evidence_id = _clean(e.get("evidence_id"))
            add("补充原文" + ("(ID=" + evidence_id + ")" if evidence_id else ""), e["text"], 900)
        elif raw:
            start = int(len(raw) * ratio)
            add("补充原文", raw[start:start + 1000], 900)
    text = "\n\n".join(parts)
    assert len(text) <= limit
    return text
