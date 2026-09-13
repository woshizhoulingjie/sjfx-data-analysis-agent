"""Bounded Worker handlers for the isolated mail task types."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from config import Config

from .attachments import read_part
from .extraction import extract_attachment
from .models import SourceRef, MailMember
from .parsers.eml import parse_eml
from .research.runtime import MailResearchRuntime
from .source import AdapterRegistry


MAIL_TASK_TYPES = {"mail_import", "mail_extract", "mail_ocr", "mail_research", "mail_export"}
if Config.MAIL_ENABLE_VECTOR_INDEX:
    MAIL_TASK_TYPES.add("mail_embed")
if Config.MAIL_ENABLE_FACT_EXTRACTION:
    MAIL_TASK_TYPES.add("mail_factextract")


def _ingest_nested(services,package,source_record,member,parsed,depth=0):
    if not Config.MAIL_ENABLE_NESTED_MESSAGES or depth>=services.policy.max_nested_message_depth:return 0
    count=0
    for part in parsed.parts:
        if part.part_role!="nested_message" or not part.raw_payload:continue
        raw=part.raw_payload
        services.policy.validate_size(len(raw),limit=services.policy.max_message_bytes,label="嵌套邮件")
        child=MailMember(member.name+"!"+part.mime_path+".eml",member.ordinal,len(raw),{"kind":"nested","parent":member.locator,"mime_path":part.mime_path})
        nested=parse_eml(raw,max_parts=services.policy.max_mime_parts,max_nested_message_depth=services.policy.max_nested_message_depth-depth-1)
        services.repository.upsert_ingested_message(package_id=package["package_id"],source=source_record,member=child,raw=raw,parsed=nested)
        count+=1+_ingest_nested(services,package,source_record,child,nested,depth+1)
    return count


def _services():
    # Worker imports app only after it has claimed a task.  This keeps mail's
    # optional dependencies away from startup and preserves the established
    # supervised process boundary.
    from app import llm, llm_generation_enabled, parser, storage, translation_service
    from .bootstrap import worker_mail_services
    return worker_mail_services(
        storage, parser=parser, translation_service=translation_service,
        model=llm if llm_generation_enabled else None,
    )


def _owner(job):
    return str(job.get("owner_id") or "legacy")


def _package(services, job):
    package_id = str((job.get("options") or {}).get("package_id") or "")
    package = services.repository.get_package(package_id, _owner(job))
    if not package:
        raise ValueError("邮件资料包不存在或不属于当前任务所有者")
    return package


def _active(services, job_id):
    if services.repository.cancelled(job_id):
        raise RuntimeError("邮件任务已取消")


def _job_update(services, job, *, progress, stage, message):
    services.storage.update_job(
        job["id"], progress=progress, stage=stage, message=message,
        heartbeat=True, current_stage=stage,
    )


def _source(package):
    return SourceRef(
        package_id=package["package_id"], kind=package["source_kind"],
        path=package["source_path"], source_revision=package["source_revision"],
    )


def _import(job, services):
    package = _package(services, job)
    owner_id = _owner(job)
    source = _source(package)
    adapter = AdapterRegistry().resolve(source)
    members = list(adapter.inventory(source, services.policy))
    progress = services.repository.progress(job["id"])
    cursor = int((progress.get("cursor") or {}).get("next_ordinal", 0))
    counts = dict(progress.get("counts") or {})
    processed_bytes = 0
    processed = 0
    max_messages = max(1, int(Config.MAIL_IMPORT_SLICE_MESSAGES))
    max_bytes = max(1, int(Config.MAIL_IMPORT_SLICE_BYTES))
    services.repository.update_package(package["package_id"], owner_id, status="importing")
    source_record = {
        "locator_type": source.kind,
        "root_path": str(Path(source.path).parent if Path(source.path).is_file() else Path(source.path)),
        "container_path": str(source.path) if Path(source.path).is_file() else None,
        "container_sha256": source.source_revision,
        "source_revision": source.source_revision,
    }
    for member in members[cursor:]:
        _active(services, job["id"])
        if processed and (processed >= max_messages or processed_bytes + member.byte_size > max_bytes):
            break
        with adapter.open_message(source, member) as stream:
            raw = stream.read()
        services.policy.validate_size(len(raw), limit=services.policy.max_message_bytes, label="邮件")
        parsed = parse_eml(
            raw,
            max_parts=services.policy.max_mime_parts,
            max_nested_message_depth=services.policy.max_nested_message_depth,
        )
        row = services.repository.upsert_ingested_message(
            package_id=package["package_id"], source=source_record,
            member=member, raw=raw, parsed=parsed,
        )
        counts["nested_occurrences"] = int(counts.get("nested_occurrences", 0)) + _ingest_nested(services,package,source_record,member,parsed)
        counts["occurrences"] = int(counts.get("occurrences", 0)) + 1
        counts["logical_messages"] = int(counts.get("logical_messages", 0)) + int(row["new_message"])
        processed += 1
        processed_bytes += len(raw)
        next_ordinal = member.ordinal + 1
        if processed == 1 or processed % 5 == 0:
            services.repository.save_progress(job["id"], "importing", {"next_ordinal": next_ordinal}, counts)
            _job_update(services, job, progress=min(94, int(next_ordinal * 95 / max(1, len(members)))), stage="mail_import", message="正在导入邮件 {}/{}".format(next_ordinal, len(members)))
    next_ordinal = cursor + processed
    services.repository.save_progress(job["id"], "importing", {"next_ordinal": next_ordinal}, counts)
    if next_ordinal < len(members):
        return {"_requeue_slice": True, "_requeue_message": "邮件导入检查点已保存，继续下一批。", "package_id": package["package_id"], "processed": processed}
    if not members:
        services.repository.update_package(package["package_id"],owner_id,status="failed",error={"code":"empty_source","message":"未在来源中发现 EML 邮件"})
        raise ValueError("未在来源中发现 EML 邮件")
    services.repository.finish_import(package["package_id"], owner_id)
    services.repository.save_progress(job["id"], "completed", {"next_ordinal": len(members)}, counts)
    _job_update(services, job, progress=99, stage="mail_import_complete", message="邮件基础索引已完成")
    return {"package_id": package["package_id"], "occurrences": len(members), "counts": services.repository.overview(package["package_id"], owner_id)["counts"]}


def _extract(job, services):
    package = _package(services, job)
    owner_id = _owner(job)
    batch_size = max(1, min(100, int(Config.MAIL_IMPORT_SLICE_MESSAGES)))
    pending = services.repository.pending_attachments(package["package_id"], owner_id, limit=batch_size)
    done = 0
    for item in pending:
        _active(services, job["id"])
        part, _raw = read_part(services.repository, item["message_key"], owner_id, package["package_id"], item["mime_path"])
        services.policy.validate_size(part.byte_size, limit=services.policy.max_attachment_bytes, label="附件")
        result = extract_attachment(part)
        services.repository.update_processing(
            item["message_key"], item["mime_path"], "basic_text", result["status"],
            parser_name=result.get("parser"), text=result.get("text"),
            warnings=result.get("warnings"), error=result.get("error"),
            coverage_ratio=1.0 if result.get("status") == "ready" else 0.0,
        )
        if result.get("text"):
            services.repository.replace_attachment_segments(
                package["package_id"], owner_id, item["message_key"], item["mime_path"], result["text"]
            )
        done += 1
        _job_update(services, job, progress=min(94, 5 + done * 85 // max(1, len(pending))), stage="mail_extract", message="正在抽取附件 {}/{}".format(done, len(pending)))
    coverage = services.repository.coverage(package["package_id"], owner_id)
    services.repository.save_progress(job["id"], "extracting", {"last_batch": done}, coverage)
    if len(pending) == batch_size:
        return {"_requeue_slice": True, "_requeue_message": "附件解析检查点已保存，继续下一批。", "package_id": package["package_id"]}
    services.repository.update_package(package["package_id"], owner_id, status="attachment_index_ready")
    return {"package_id": package["package_id"], "processed": done, "coverage": coverage}


def _ocr(job, services):
    from .extraction import extract_ocr
    package=_package(services,job);owner_id=_owner(job)
    with services.repository._connection() as c:
        rows=[dict(r) for r in c.execute("SELECT DISTINCT p.message_key,p.mime_path FROM mail_parts p JOIN mail_occurrences o ON o.message_key=p.message_key JOIN mail_part_processing x ON x.message_key=p.message_key AND x.mime_path=p.mime_path AND x.stage='basic_text' WHERE o.package_id=? AND x.status='ocr_pending' LIMIT 5",(package["package_id"],))]
    for item in rows:
        _active(services,job["id"])
        part,_=read_part(services.repository,item["message_key"],owner_id,package["package_id"],item["mime_path"])
        result=extract_ocr(part,services.parser,lambda:services.repository.cancelled(job["id"]))
        for stage in ("ocr","basic_text"):
            services.repository.update_processing(item["message_key"],item["mime_path"],stage,result["status"],parser_name=result["parser"],text=result.get("text"),warnings=result.get("warnings"),error=result.get("error"))
        if result.get("text"):services.repository.replace_attachment_segments(package["package_id"],owner_id,item["message_key"],item["mime_path"],result["text"])
    return {"package_id":package["package_id"],"processed":len(rows),"coverage":services.repository.coverage(package["package_id"],owner_id),"_requeue_slice":len(rows)==5 and services.parser is not None}


def _embed(job, services):
    from .semantic import embed_segments_batch
    import time
    if not Config.MAIL_ENABLE_VECTOR_INDEX:raise ValueError("语义索引未启用")
    package=_package(services,job)
    try:
        status=embed_segments_batch(services.repository,package["package_id"],_owner(job),cancelled=lambda:services.repository.cancelled(job["id"]))
    except Exception as exc:
        _active(services,job["id"])
        previous=services.repository.progress(job["id"])
        attempts=int((previous.get("counts") or {}).get("consecutive_failures",0))+1
        services.repository.save_progress(job["id"],"embedding_retry",{}, {"consecutive_failures":attempts,"error":str(exc)[:200]})
        if attempts>=3:raise
        time.sleep(2)
        return {"package_id":package["package_id"],"_requeue_slice":True,"_requeue_message":"嵌入服务暂时忙碌，保留检查点后重试","consecutive_failures":attempts}
    services.repository.save_progress(job["id"],"paused" if status.get("paused") else "embedding",{},status)
    if status.get("pending") and not status.get("paused"):
        until=time.monotonic()+Config.MAIL_EMBED_INTERVAL_SECONDS
        while time.monotonic()<until:
            _active(services,job["id"]);time.sleep(min(.2,max(0,until-time.monotonic())))
        status.update(_requeue_slice=True,_requeue_message="邮件语义索引检查点已保存")
    return {"package_id":package["package_id"],**status}


def _factextract(job, services):
    from .facts import factextract_batch
    package=_package(services,job)
    from services.model_dispatch import background_generation
    with background_generation(lambda:services.repository.cancelled(job["id"])):
        result=factextract_batch(services.repository,package["package_id"],_owner(job),services.model,types=(job.get("options") or {}).get("fact_types"),cancelled=lambda:services.repository.cancelled(job["id"]))
    services.repository.save_progress(job["id"],result["status"],{},result)
    if result.get("pending") and result.get("status")!="waiting_for_model" and not result.get("failed"):
        result.update(_requeue_slice=True,_requeue_message="字段抽取检查点已保存")
    return {"package_id":package["package_id"],**result}


def _research(job, services):
    owner_id = _owner(job)
    turn_id = str((job.get("options") or {}).get("turn_id") or "")
    turn = services.repository.get_turn(turn_id, owner_id)
    if not turn:
        raise ValueError("邮件研究轮次不存在或不属于当前任务所有者")
    _active(services, job["id"])
    session=services.repository.get_session(turn["session_id"],owner_id)
    package=services.repository.get_package(session["package_id"],owner_id) if session else None
    if package and package.get("status") in {"inventory","importing"}:
        services.repository.update_turn(turn_id,owner_id,status="waiting_for_index")
        return {"_defer_slice":True,"_defer_seconds":10,"_defer_message":"等待邮件基础导入完成","turn_id":turn_id}
    if not package or package.get("status")=="failed":
        services.repository.update_turn(turn_id,owner_id,status="failed",sufficiency="insufficient",error={"message":"邮件资料包尚未成功导入"})
        raise ValueError("邮件资料包尚未成功导入")
    _job_update(services, job, progress=8, stage="mail_research", message="正在检索邮件证据")
    runtime = MailResearchRuntime(
        services.repository,
        model=services.model,
        context_limit=Config.MAIL_TOOL_CONTEXT_MAX_CHARS,
        cancel_check=lambda: services.repository.cancelled(job["id"]),
    )
    result = runtime.run(turn_id, owner_id)
    _active(services, job["id"])
    _job_update(services, job, progress=99, stage="mail_research_complete", message="邮件研究已完成")
    return {"turn_id": turn_id, "result_set_id": result.get("result_set_id"), "sufficiency": services.repository.get_turn(turn_id, owner_id).get("sufficiency")}


def _export(job, services):
    from .exporter import export_result_set
    owner_id = _owner(job)
    result_set_id = str((job.get("options") or {}).get("result_set_id") or "")
    _active(services, job["id"])
    _job_update(services, job, progress=10, stage="mail_export", message="正在生成邮件研究导出")
    artifact = export_result_set(services.repository, result_set_id, owner_id, Config.OUTPUT_DIR, (job.get("options") or {}).get("format", "csv"))
    services.storage.save_artifact(artifact.name, owner_id, scan_id=None, job_id=job["id"], kind="mail_export")
    # The UI obtains a fresh one-use ticket only when the user downloads.
    return {"result_set_id":result_set_id,"filename":artifact.name,"download_ticket_url":"/api/download-ticket"}


def run_claimed_mail_job(job):
    if not getattr(Config, "MAIL_FEATURE_ENABLED", False):
        raise RuntimeError("邮件研究功能已由部署配置关闭")
    task_type = str(job.get("task_type") or "")
    if task_type not in MAIL_TASK_TYPES:
        raise ValueError("未知邮件任务类型：{}".format(task_type))
    services = _services()
    handlers = {
        "mail_import": _import, "mail_extract": _extract, "mail_ocr": _ocr,
        "mail_research": _research, "mail_export": _export, "mail_embed": _embed, "mail_factextract": _factextract,
    }
    return handlers[task_type](job, services)
