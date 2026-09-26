"""Content-derived navigation for the isolated large-package pipeline.

Only directory fields and the catalog queue are updated here. Semantic embeddings
provide grouping; extractive labels require no generative model calls.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from contextlib import closing

VERSION = "large-semantic-catalog/2"

# 送进模型/嵌入的正文预算。原来固定 3600，且只取 text_preview 的**开头**：
# 一个 112,786 字符的 PDF 最终只有 3.2% 的内容进入模型，文档尾部的结论、
# 签署方、附件清单等完全读不到。现在改为在预算内取「头 + 中 + 尾」，
# 覆盖范围扩展到全文，成本不变（字符数相同）。
MATERIAL_CHARS = 6000
PREVIEW_CHARS = 9000

# catalog_input_hash（向量缓存指纹）的取值域。**只能增加，不能减少**：
# 减少会让已经算好的向量缓存全部失效、触发整包重新嵌入。
# 新增检索性字段（例如 keywords）时把它排除在指纹之外，缓存才不会被无谓地作废。
DIGEST_FIELDS = ("text", "deep_summary", "fields", "headings", "source")

# 目录阶段失败后的**有界**重试次数。用尽即落 failed（只能经 /retry-failed 人工重试）。
# 为什么必须有界：改动前无论成败一律把队列项标 completed，于是"识别失败"的文件
# 永远不会被自动重试（生产实测 73 个 deferred 文件停在这里，唯一出口是手工点
# "补充内容识别"）；而如果改成无条件 requeue，就会变成"每 2 秒重复同一条失败路径、
# 持续数小时"——那正是本项目已经踩过的空转。两者之间只有"有界重试"是对的。
CATALOG_MAX_ATTEMPTS = max(1, min(10, int(os.getenv("SJFX_LP_CATALOG_MAX_ATTEMPTS", "3") or 3)))


def _sample(value, limit):
    """在 limit 字符内取头部 50% / 中部 25% / 尾部 25%。"""
    text = str(value or "")
    limit = max(0, int(limit or 0))
    if not limit or len(text) <= limit:
        return text
    head = int(limit * 0.50)
    middle = int(limit * 0.25)
    tail = limit - head - middle
    middle_start = max(head, (len(text) - middle) // 2)
    return text[:head] + text[middle_start:middle_start + middle] + (text[-tail:] if tail else "")


def _text(value, limit=MATERIAL_CHARS):
    return re.sub(r"\s+", " ", _sample(value, limit)).strip()


# 混进正文的标记语言。生产实测约 5% 的文件首段证据是 `<!DOCTYPE html>...` 源码：
# 标签既占满证据预算，又让命名模型看不到任何内容。这里只删标签与脚本内容，
# 不增删真实正文；删完仍然没内容时，material() 会照旧返回 None（走"无正文"路径）。
_MARKUP_TAG = re.compile(
    r"<(?:!DOCTYPE|/?(?:html|head|body|div|span|p|a|li|ul|ol|table|thead|tbody|tr|td|th|"
    r"script|style|meta|link|br|hr|h[1-6]|img|form|input|button|nav|footer|header|"
    r"section|article|iframe|svg|noscript)\b)[^>]{0,300}>", re.I)
_MARKUP_BLOCK = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.I | re.S)


def strip_markup(value):
    """去掉正文里的 HTML/XML 源码，保留可读文字。仅当标签密度足够高时才动手。"""
    text = str(value or "")
    if not text or "<" not in text:
        return text
    if len(_MARKUP_TAG.findall(text[:4000])) < 3:
        return text
    text = _MARKUP_BLOCK.sub(" ", text)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = _MARKUP_TAG.sub(" ", text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(entity, char)
    return re.sub(r"[ \t\u3000]{2,}", " ", text).strip()


def material(row):
    local = row.get("local_summary") or {}
    info = local.get("portrait") or {}
    deep = row.get("deep_summary") or {}
    if row.get("sensitive") or info.get("recognition_status") == "restricted":
        return None
    parts = [p for p in info.get("evidence", []) if isinstance(p, dict) and p.get("kind") not in {"member_names", "attachment"}]
    if len(parts) > 3:
        positions = sorted({0, len(parts)//2, len(parts)-1})
        sampled = [parts[i] for i in positions]
    else:
        sampled = parts
    text = "\n".join(_text(p.get("text"), 1150) for p in sampled)
    if not parts and not info.get("evidence"):
        text = local.get("sample") or ""
    deep_ready = row.get("deep_status") == "completed" and deep.get("summary_type") != "local_deep_failed"
    summary = _text(deep.get("summary"), 1800) if deep_ready else ""
    full_preview = _text(deep.get("text_preview"), PREVIEW_CHARS) if deep_ready else ""
    if full_preview:
        text = full_preview
    text = _text(text, MATERIAL_CHARS)
    text = strip_markup(text)
    fields = [_text(x, 100) for x in info.get("fields", [])[:20]]
    headings = [_text(x, 140) for x in info.get("headings", [])[:8]]
    keywords = [_text(x, 40) for x in (local.get("keywords") or [])[:16]]
    if len(text) < 20 and len(summary) < 20 and len(fields) < 2:
        return None
    value = {"text": text, "deep_summary": summary, "fields": fields, "headings": headings,
             "keywords": keywords,
             "source": "deep_summary" if summary else "deep_content" if full_preview else "content_preview"}
    # File names and old rule labels are intentionally absent from model input.
    # 指纹只覆盖"决定向量内容"的字段：新增 keywords 之类的检索性字段不得让
    # 已经算好的向量缓存整包失效（否则一次低成本改造会触发全量重新嵌入）。
    fingerprint = {key: value[key] for key in DIGEST_FIELDS}
    # The selected representation can use blocks beyond the legacy three windows.
    # Changes there must invalidate the source identity too; labels/keywords do not.
    from services.large_package_evidence import row_parts
    blocks=row_parts(row)
    if blocks:
        fingerprint['source_evidence_v1']=[{k:p.get(k) for k in ('text','kind','location','truncated')} for p in blocks]
    digest = hashlib.sha256((VERSION + json.dumps(fingerprint, ensure_ascii=False, sort_keys=True)).encode()).hexdigest()
    return value, digest


def prepare_update(previous, fields):
    """Return directory changes for new content without changing parse results."""
    row = {**previous, **fields}
    local = copy.deepcopy(row.get("local_summary") or {})
    info = local.setdefault("portrait", {})
    prior_info = (previous.get("local_summary") or {}).get("portrait") or {}
    source = material(row)
    if not source:
        # No content is never permission to assign an industry from a filename.
        info.update(schema=info.get("schema") or "large-content-portrait/1", catalog_version=VERSION,
                    catalog_status="no_content", category="待识别内容", topic="待识别内容",
                    classification_basis="尚无足够可读内容，等待内容识别", confidence=0)
        if row.get("sensitive") or info.get("recognition_status") == "restricted":
            info.update(category="受限文件", topic="受限文件", recognition_status="restricted")
        else:
            info.setdefault("recognition_status", "metadata_only")
        local["category"] = info["category"]
        return {**fields, "local_summary": local, "category": info["category"], "topic": info["topic"],
                "confidence": 0, "recognition_status": info["recognition_status"]}, False
    value, digest = source
    if prior_info.get("catalog_input_hash") == digest:
        # Refreshing a preview must not replace a classification based on deeper content.
        for key in ("schema", "topic", "category", "quality", "confidence", "recognition_status", "classification_basis", "grouping_basis"):
            if key in prior_info:
                info[key] = prior_info[key]
        info.update({k: v for k, v in prior_info.items() if k.startswith("catalog_")})
        local["category"] = info["category"]
        return {**fields, "local_summary": local, "category": info["category"], "topic": info["topic"],
                "confidence": info.get("confidence", 0), "recognition_status": info.get("recognition_status", "needs_review")}, False
    clues = value["fields"][:3] or value["headings"][:1]
    clue = " / ".join(clues) or value["deep_summary"][:100] or value["text"][:100]
    # Keep a good previous topic visible until a deeper result replaces it.
    retained = prior_info.get("catalog_status") == "classified"
    info.update(schema=info.get("schema") or "large-content-portrait/1", catalog_version=VERSION,
                catalog_input_hash=digest, catalog_source=value["source"], catalog_status="pending",
                category=prior_info.get("category") if retained else "内容待归纳",
                topic=prior_info.get("topic") if retained else "内容线索：" + _text(clue, 100),
                quality="content", recognition_status="needs_review", confidence=0,
                classification_basis="已有内容线索，等待按主要内容归纳主题", grouping_basis="",
                catalog_evidence=[], catalog_error="")
    local["category"] = info["category"]
    return {**fields, "local_summary": local, "category": info["category"], "topic": info["topic"],
            "confidence": 0, "recognition_status": "needs_review"}, True


def get_file(store, package_id, path):
    with closing(store._connect()) as conn:
        row = conn.execute("SELECT * FROM files WHERE package_id=? AND path=?", (package_id,path)).fetchone()
    return store._row(row) if row else None


def _save(store, package_id, path, digest, result=None, error="", token=None):
    """Apply only navigation fields, with a content fingerprint concurrency check.

    P0（文档 §3B）：除内容 digest 外，还要**先验证本次调用仍持有该队列项的租约**。
    原实现只比 digest，然后按 (package_id,path,stage) 无条件更新队列 —— 一个晚到的
    `_save` 会覆盖别人正在进行的工作（旧 token 晚到、输入更新、取消后结果晚到）。
    现在：**先验所有权，再改任何状态**，不满足就整事务回滚、什么都不写。
    token=None 仅为兼容旧调用点；生产两条路径（embed 暂存、name_batch）都传真实 token。
    """
    with store._lock, closing(store._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        # ① 所有权/新鲜度闸门：必须先于任何写入。
        if token is not None:
            owner = conn.execute(
                "SELECT status,lease_token FROM queue "
                "WHERE package_id=? AND path=? AND stage='catalog'",
                (package_id, path)).fetchone()
            if not owner:
                conn.rollback()
                return False
            if str(owner["lease_token"] or '') != str(token):
                # 别人已经重领（或本项已收尾）：放弃，绝不覆盖。
                conn.rollback()
                return False
            if str(owner["status"] or '') not in ('running',):
                conn.rollback()
                return False
        raw = conn.execute("SELECT * FROM files WHERE package_id=? AND path=?", (package_id, path)).fetchone()
        if not raw:
            conn.execute("DELETE FROM queue WHERE package_id=? AND path=? AND stage='catalog'", (package_id, path))
            conn.commit()
            return False
        row = store._row(raw)
        local = row["local_summary"]
        info = local.get("portrait") or {}
        if info.get("catalog_input_hash") != digest:
            conn.execute("UPDATE queue SET status='queued',leased_at=NULL WHERE package_id=? AND path=? AND stage='catalog'", (package_id, path))
            conn.commit()
            return False
        if result:
            label = result["topic"]
            info.update(category=result["category"], topic=label, quality="identified", recognition_status="identified",
                        catalog_status="classified", catalog_model=result["model"], catalog_label_method="source_phrases", catalog_error="",
                        catalog_evidence=[{"source_path": path, "location": "深度摘要" if p["source"] == "deep_summary" else "内容片段",
                                           "text": p["text"], "preview_only": info["catalog_source"] == "content_preview"} for p in result["quotes"]],
                        classification_basis=result["reason"], grouping_basis="按内容语义相似性分组；目录名直接提取自组内代表内容")
        else:
            info.update(catalog_status="deferred", catalog_error=_text(error, 240), recognition_status="needs_review",
                        classification_basis="暂保留内容线索，主题归纳待补充识别" if info.get("catalog_status") != "classified" else info.get("classification_basis"))
        info["catalog_updated_at"] = time.time()
        local.update(portrait=info, category=info["category"])
        conn.execute("""UPDATE files SET local_summary=?,category=?,topic=?,recognition_status=?,confidence=0,updated_at=?
          WHERE package_id=? AND path=?""", (json.dumps(local, ensure_ascii=False), info["category"], info["topic"], info["recognition_status"], time.time(), package_id, path))
        # 目录队列项收尾：成功 → completed；失败 → 按 attempts 有界重试。
        # `attempts` 由 claim() 每次领取自增，所以它就是"已经试过几次"的真值。
        row = conn.execute("SELECT COALESCE(attempts,0) FROM queue "
                           "WHERE package_id=? AND path=? AND stage='catalog'",
                           (package_id, path)).fetchone()
        attempts = int(row[0]) if row else 0
        if result:
            queue_status = "completed"
        elif attempts < CATALOG_MAX_ATTEMPTS:
            queue_status = "queued"
        else:
            queue_status = "failed"
        # P0：收尾也带 token 条件 —— 拿不到该行说明所有权已变，本次不落任何结论。
        changed = conn.execute("UPDATE queue SET status=?,error=?,leased_at=NULL,lease_token=NULL,updated_at=? "
                               "WHERE package_id=? AND path=? AND stage='catalog' "
                               + ("AND lease_token=?" if token is not None else ""),
                               ((queue_status, _text(error, 240) or None, time.time(), package_id, path,
                                 str(token)) if token is not None
                                else (queue_status, _text(error, 240) or None, time.time(), package_id, path))
                               ).rowcount
        if not changed:
            conn.rollback()
            return False
        if result:
            # 目录阶段已经给出**确定的识别结论**（recognition_status=identified），
            # 补充识别队列里这一项的目的就达到了，同一个事务里收掉它。
            # 改动前只结束 catalog 行，recognize 行永远留在 queued：生产实测
            # 12,397 个已 identified 的文件仍被算作"待补充识别"，一次点击就要
            # 把它们全部重跑一遍（每个文件一次模型调用）。
            conn.execute("UPDATE queue SET status='completed',leased_at=NULL,lease_token=NULL,updated_at=? "
                         "WHERE package_id=? AND path=? AND stage='recognize' "
                         "AND status IN ('queued','running','retry')",
                         (time.time(), package_id, path))
        conn.commit()
    store._catalog_cache.pop(package_id, None)
    return True


def refresh_existing(store, package_id):
    """Queue a directory-only upgrade from saved previews/summaries, without reparsing."""
    after = ""
    count = 0
    while True:
        with closing(store._connect()) as conn:
            rows = conn.execute("SELECT * FROM files WHERE package_id=? AND path>? ORDER BY path LIMIT 100", (package_id, after)).fetchall()
        if not rows:
            return count
        for raw in rows:
            row = store._row(raw)
            store.update_file(package_id, row["path"], local_summary=row["local_summary"])
            count += 1
        after = rows[-1]["path"]
