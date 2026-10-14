"""Chinese presentation of mail sources; original evidence is never overwritten."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from queue import PriorityQueue
from itertools import count
import copy
import time
from pathlib import Path
from config import Config
from .attachments import read_part
from .html_safety import html_to_translation_text
from .translation_quality import VERSION, GLOSSARY_VERSION, SegmentMemory, chinese_text, needs_translation, translation_steps, translate_entries

_LOCK = threading.RLock()
_JOBS = OrderedDict()
_QUEUE = PriorityQueue()
_SEQUENCE = count()
_THREAD = None
_VERSION = VERSION


def translate_text(service, source):
    """Compatibility wrapper: never present fallback original text as a translation."""
    source = str(source or "").strip()
    if not needs_translation(source):
        return {"text": source, "status": "not_required"}
    if service is None:
        return {"text": None, "status": "failed", "error": "中文翻译服务尚未配置"}
    result = service.translate_document({"source": {"path": "mail-chinese:" + hashlib.sha256(source.encode()).hexdigest()}, "structure": {"title": ""}, "text": source})
    translated = str(result.get("translated_text") or "").strip()
    if result.get("status") != "completed" or not translated or not chinese_text(translated):
        return {"text": None, "status": "failed", "error": "该段译文尚未通过中文质量检查，请查看原文或重试"}
    return {"text": translated, "status": "completed"}


def source_bundle(services, owner_id, payload):
    r = services.repository
    package_id = str(payload.get("package_id") or "")
    if not r.get_package(package_id, owner_id):
        raise LookupError("邮件资料包不存在或不属于当前用户")
    kind = payload.get("kind")
    entries = {}
    metadata = {}
    contexts = {}
    offset = max(0, int(payload.get("offset") or 0))
    if kind == "titles":
        keys = payload.get("message_keys") or []
        if not isinstance(keys, list) or len(keys) > 25:
            raise ValueError("每次最多翻译 25 封邮件的主题")
        for key in dict.fromkeys(str(k) for k in keys):
            message = r.get_message(key, owner_id, package_id)
            if not message:
                raise LookupError("邮件不在当前资料包内")
            entries[key] = message.get("subject_raw") or "无主题"
    elif kind in {"message", "attachment"}:
        key = str(payload.get("message_key") or "")
        message = r.get_message(key, owner_id, package_id)
        if not message:
            raise LookupError("邮件不在当前资料包内")
        if kind == "message":
            mime_path = message.get("body_preferred_part")
            source = ""
            if mime_path:
                part, _ = read_part(r, key, owner_id, package_id, mime_path)
                if part.part_role not in {"body_plain", "body_html"}:
                    raise ValueError("所选部件不是邮件正文")
                source = html_to_translation_text(part.text) if part.part_role == "body_html" else str(part.text or "")
            entries["title"] = message.get("subject_raw") or "无主题"
            contexts["body"] = {"subject": entries["title"][:400]}
        else:
            mime_path = str(payload.get("mime_path") or "")
            part = next((p for p in message.get("parts", []) if p["mime_path"] == mime_path), None)
            if not part or not part.get("is_explicit_attachment"):
                raise LookupError("附件不在当前邮件内")
            with r._connection() as c:
                rows = c.execute("SELECT text FROM mail_text_segments WHERE package_id=? AND message_key=? AND mime_path=? AND source_kind='attachment' ORDER BY ordinal", (package_id, key, mime_path)).fetchall()
            source = "\n".join(row[0] for row in rows)
            entries["title"] = part.get("filename_raw") or "附件"
            contexts["body"] = {"subject": str(message.get("subject_raw") or "")[:400], "attachment": entries["title"][:300]}
        end = min(len(source), offset + 6000)
        if end < len(source):
            boundary = max(source.rfind("\n", offset + 3500, end), source.rfind(" ", offset + 4500, end))
            if boundary > offset:
                end = boundary + 1
        chunk = source[offset:end]
        entries["body"] = chunk
        metadata = {"total_chars": len(source), "offset": offset, "next_offset": offset + len(chunk) if offset + len(chunk) < len(source) else None, "source_text": chunk, "mime_path": mime_path}
        if kind=='attachment':
            metadata['text_origin']=part.get('text_origin','unknown')
            if part.get('source_text_notice'):metadata['notice']=part['source_text_notice']
        if not source:
            metadata["notice"] = "该部件尚无可翻译文本，请先完成附件解析或 OCR"
    elif kind == "evidence":
        evidence = r.get_evidence(str(payload.get("evidence_id") or ""), owner_id)
        if not evidence or evidence["package_id"] != package_id:
            raise LookupError("证据不在当前资料包内")
        entries["body"] = evidence.get("quote_text") or ""
        message = r.get_message(evidence["message_key"], owner_id, package_id) or {}
        contexts["body"] = {"subject": str(message.get("subject_raw") or "")[:400]}
        metadata = {"source_text": entries["body"], "mime_path": evidence.get("mime_path"), "next_offset": None}
        metadata['text_origin']=evidence.get('text_origin','unknown')
        if evidence.get('source_text_notice'):metadata['notice']=evidence['source_text_notice']
    elif kind == "result":
        result = r.get_result_set(str(payload.get("result_set_id") or ""), owner_id)
        if not result or result["package_id"] != package_id:
            raise LookupError("研究结果不在当前资料包内")
        fields = (result.get("definition") or {}).get("fields") or []
        for i, f in enumerate(fields[:80]):
            entries[f"field:{i}:subject"] = f.get("subject_ref") or f.get("subject_raw") or "原文未明确事项"
            entries[f"field:{i}:value"] = f.get("value_raw") or ""
            contexts[f"field:{i}:value"] = {"field_type": f.get("fact_type"), "source_quote": str(f.get("quote") or "")[:450]}
        final = result.get("final") or {}
        for e in final.get("evidence", [])[:20]:
            entries["evidence:" + e["evidence_id"]] = str(e.get("quote_text") or "")[:500]
        for item in result.get("items", [])[:200]:
            m = r.get_message(item["item_id"], owner_id, package_id)
            if m:
                entries["title:" + item["item_id"]] = m.get("subject_raw") or "无主题"
    else:
        raise ValueError("不支持的中文展示类型")
    # Page result translation too: a single request never queues the whole corpus.
    if kind == "result":
        rows = list(entries.items())
        selected = []; chars = 0
        for key, source in rows[offset:]:
            if selected and (len(selected) >= 16 or chars + len(source) > 8000):
                break
            selected.append((key, source)); chars += len(source)
        entries = dict(selected)
        metadata = {"next_offset": offset + len(selected) if offset + len(selected) < len(rows) else None, "total_items": len(rows)}
    metadata["contexts"] = {k: v for k, v in contexts.items() if k in entries}
    return package_id, entries, metadata


def _snapshot(job, metadata):
    result = {k: copy.deepcopy(v) for k,v in job.items() if not k.startswith("_")}
    result["metadata"] = {k:v for k,v in metadata.items() if k != "contexts"}
    return result


def _worker():
    while True:
        _priority, _sequence, key = _QUEUE.get()
        try:
            with _LOCK:
                job = _JOBS.get(key)
                if not job or job["status"] != "pending":
                    continue
                if time.monotonic() - job["_seen"] > 120:
                    job["status"]="paused";job["phase"]="paused"
                    continue
                job["phase"]="translating"
            # One iterator step is at most one bounded model call. Requeue afterwards.
            from sjfx_protection.business import saved_execution
            with saved_execution(job.get('_owner'),[job.get('_root')],job.get('_protection_context'),'analyze'):
                try:
                    values = next(job["_steps"])
                except StopIteration:
                    values = job["values"]
                from sjfx_protection import cache
                cache.record_payload(job['_cache_key'], values, job.get('_cache_sources'), owner=job['_owner'])
            with _LOCK:
                job["values"] = values
                done = sum(v["status"] in {"completed","not_required"} for v in values.values())
                failed = sum(v["status"] in {"failed","partial"} for v in values.values())
                pending = sum(v["status"] == "pending" for v in values.values())
                job.update(completed=done,failed=failed,completed_segments=sum(v.get("completed_segments",0) for v in values.values()),total_segments=sum(v.get("total_segments",0) for v in values.values()))
                if pending:
                    job["phase"]="queued"
                    _QUEUE.put((job["_priority"],next(_SEQUENCE),key))
                else:
                    job["status"] = "completed" if not failed else "failed" if failed == len(values) else "partial"
                    job["phase"]="done"
        except Exception:
            with _LOCK:
                if key in _JOBS:
                    _JOBS[key].update(status="failed",phase="done",error="翻译任务暂时中断，可重试；已完成译文已保留")
        finally:
            _QUEUE.task_done()


def request_presentation(services, owner_id, payload):
    global _THREAD
    from sjfx_protection.business import enabled,require_root
    package=services.repository.get_package(str(payload.get('package_id') or ''),owner_id)
    protected=require_root(owner_id,package['source_path'],'analyze') if enabled() and package else None
    package_id, entries, metadata = source_bundle(services, owner_id, payload)
    model = getattr(services,"model",None)
    transport = getattr(model,"transport",model)
    model_id = str(getattr(transport,"model",None) or type(model).__name__)
    context = metadata.get("contexts") or {}
    raw = json.dumps([_VERSION,GLOSSARY_VERSION,owner_id,package_id,model_id,entries,context], ensure_ascii=False, sort_keys=True)
    if protected:
        stable = {k:v for k,v in protected.items() if k != 'decision_id'}
        raw += json.dumps(stable,sort_keys=True,ensure_ascii=False)
    key = hashlib.sha256(raw.encode()).hexdigest()
    priority = {"message":0,"evidence":0,"attachment":1,"result":2,"titles":5}.get(payload.get("kind"),5)
    with _LOCK:
        current = _JOBS.get(key)
        if current and not (payload.get("retry") is True and current["status"] in {"failed","partial"}):
            from sjfx_protection import cache
            cache.require_payload(current['_cache_key'], current['values'], owner=owner_id)
            current["_seen"] = time.monotonic()
            if current["status"] == "paused":
                current.update(status="pending",phase="queued")
                _QUEUE.put((priority,next(_SEQUENCE),key))
            return _snapshot(current,metadata)
        if sum(j["status"] == "pending" for j in _JOBS.values()) >= 32:
            raise RuntimeError("翻译队列繁忙，请稍后重试")
        while len(_JOBS) >= 256:
            completed = next((k for k,j in _JOBS.items() if j["status"] != "pending"),None)
            if completed is None:break
            _JOBS.pop(completed)
        memory = SegmentMemory(Path(Config.DATA_DIR)/"mail_chinese_cache"/"segments-v3.sqlite3", [owner_id,package_id], model_id)
        steps = translation_steps(services,entries,contexts=context,memory=memory)
        # First step only resolves cache and original-Chinese segments: no model work.
        from sjfx_protection import cache
        from sjfx_protection.business import saved_execution
        with saved_execution(owner_id, [(package or {}).get('source_path')], protected, 'analyze'):
            values = next(steps)
            sources = cache.capture_sources()
        proof_key = cache.resource_key(Config.DATA_DIR / 'mail_chinese_cache', 'mail-presentation', package_id, key)
        cache.record_payload(proof_key, values, sources, owner=owner_id)
        done=sum(v["status"] in {"completed","not_required"} for v in values.values())
        job={"status":"completed" if done==len(entries) else "pending","phase":"cached" if done==len(entries) else "queued","values":values,"completed":done,"total":len(entries),"target_language":"zh-CN","completed_segments":sum(v.get("completed_segments",0) for v in values.values()),"total_segments":sum(v.get("total_segments",0) for v in values.values()),"_priority":priority,"_seen":time.monotonic(),"_steps":steps}
        job.update(_owner=owner_id,_root=(package or {}).get('source_path'),_protection_context=protected,
                   _cache_key=proof_key, _cache_sources=sources)
        _JOBS[key]=job
        if job["status"] == "pending":_QUEUE.put((priority,next(_SEQUENCE),key))
        if _THREAD is None or not _THREAD.is_alive():
            _THREAD=threading.Thread(target=_worker,name="mail-chinese",daemon=True);_THREAD.start()
        return _snapshot(job,metadata)
