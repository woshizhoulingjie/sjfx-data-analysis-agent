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
from .source import AdapterRegistry, open_locator


MAIL_TASK_TYPES = {"mail_import", "mail_extract", "mail_ocr", "mail_research", "mail_export", "mail_foundation"}
if Config.MAIL_ENABLE_VECTOR_INDEX:
    MAIL_TASK_TYPES.add("mail_embed")
if Config.MAIL_ENABLE_FACT_EXTRACTION:
    MAIL_TASK_TYPES.add("mail_factextract")


def _ingest_nested(services,package,source_record,member,parsed,depth=0):
    if not package.get("config",{}).get("include_nested_messages",False) or not Config.MAIL_ENABLE_NESTED_MESSAGES or depth>=services.policy.max_nested_message_depth:return 0
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
    return services.storage.update_job(
        job["id"], progress=progress, stage=stage, message=message,
        heartbeat=True, current_stage=stage,
    )


def _queue_derivatives(services,package,*,attachments=False,ocr=False):
    factory=getattr(services.storage,'create_or_get_typed_job',None)
    if factory is None:return []
    tasks=[]
    if attachments:tasks.append(('mail_extract',40))
    if ocr:tasks.append(('mail_ocr',10))
    if Config.MAIL_ENABLE_VECTOR_INDEX:tasks.append(('mail_embed',5))
    result=[]
    for task,priority in tasks:
        jid,created=factory(package['package_id'],task,options={'package_id':package['package_id'],'workflow_source':'mail_foundation_derived'},owner_id=package['owner_id'])
        if created:
            with services.repository._connection() as c:c.execute('UPDATE analysis_jobs SET priority=? WHERE id=?',(priority,jid))
        result.append({'job_id':jid,'task_type':task,'created':created})
    return result


def _source(package):
    return SourceRef(
        package_id=package["package_id"], kind=package["source_kind"],
        path=package["source_path"], source_revision=package["source_revision"],
    )


def _import(job, services):
    from .foundation import source_inventory,inventory_entry,set_state
    package=_package(services,job); owner_id=_owner(job)
    # Re-authorize the persisted source in the worker process.  A package
    # owner check alone is insufficient because the source path may point at
    # another user's upload tree after a crafted create request.
    source_path = services.policy.resolve_source(package["source_path"], owner_id=owner_id)
    source=SourceRef(
        package_id=package["package_id"], kind=package["source_kind"],
        path=str(source_path), source_revision=package["source_revision"],
    )
    try:
        adapter=AdapterRegistry().resolve(source)
        entries=list(source_inventory(source,services.policy,adapter,owner_id))
    except Exception as exc:
        set_state(services.repository,package['package_id'],'inventory','terminal_failed',error={'message':str(exc)[:500]})
        services.repository.update_package(package['package_id'],owner_id,status='failed',error={'message':str(exc)[:500]})
        raise
    progress=services.repository.progress(job['id']); cursor=0 if progress.get('phase')=='failed' else int((progress.get('cursor') or {}).get('next_ordinal',0))
    counts=dict(progress.get('counts') or {}); processed=0; processed_bytes=0; next_index=cursor
    max_messages=max(1,int(Config.MAIL_IMPORT_SLICE_MESSAGES));max_bytes=max(1,int(Config.MAIL_IMPORT_SLICE_BYTES))
    services.repository.update_package(package['package_id'],owner_id,status='importing',error=None)
    set_state(services.repository,package['package_id'],'mail_parse','running',cursor={'next_ordinal':cursor,'total':len(entries)})
    source_record={'locator_type':source.kind,'root_path':str(Path(source.path).parent if Path(source.path).is_file() else Path(source.path)), 'container_path':str(source.path) if Path(source.path).is_file() else None,'container_sha256':source.source_revision,'source_revision':source.source_revision}
    # Fill the complete inventory once; slice checkpoints never overwrite completed rows.
    with services.repository._connection() as c:
        from .foundation import dumps
        c.executemany("INSERT OR IGNORE INTO mail_import_entries(package_id,member_name,member_ordinal,byte_size,status,error_json,locator_json) VALUES (?,?,?,?,?,?,?)",[(package['package_id'],m.name,m.ordinal,m.byte_size,status,dumps({'message':error}) if error else None,dumps(m.locator)) for m,status,error in entries])
    set_state(services.repository,package['package_id'],'inventory','ready',cursor={'total':len(entries)},fingerprint=source.source_revision)
    for index,(member,status,error) in enumerate(entries[cursor:],cursor):
        _active(services,job['id'])
        if processed and (processed>=max_messages or processed_bytes+member.byte_size>max_bytes):break
        if status=='pending':
            try:
                # Re-check immediately before opening each member so a
                # replaced upload path is rejected during long imports too.
                source_path = services.policy.resolve_source(source.path, owner_id=owner_id)
                source = SourceRef(package_id=source.package_id, kind=source.kind,
                                   path=str(source_path), source_revision=source.source_revision)
                locator = services.policy.resolve_locator(
                    member.locator, owner_id=owner_id, source_kind=source.kind,
                    source_revision=source.source_revision,
                )
                member = MailMember(member.name, member.ordinal, member.byte_size, locator)
                with open_locator(member.locator, policy=services.policy, owner_id=owner_id,
                                  source_kind=source.kind, source_revision=source.source_revision) as stream:
                    raw=stream.read(services.policy.max_message_bytes+1)
                services.policy.validate_size(len(raw),limit=services.policy.max_message_bytes,label='邮件')
                parsed=parse_eml(raw,max_parts=services.policy.max_mime_parts,max_nested_message_depth=services.policy.max_nested_message_depth)
                row=services.repository.upsert_ingested_message(package_id=package['package_id'],source=source_record,member=member,raw=raw,parsed=parsed)
                _ingest_nested(services,package,source_record,member,parsed)
                processed_bytes+=len(raw)
            except Exception as exc:
                inventory_entry(services.repository,package['package_id'],member,'terminal_failed',error={'type':type(exc).__name__,'message':str(exc)[:500]})
        next_index=index+1;processed+=1
        services.repository.save_progress(job['id'],'importing',{'next_ordinal':next_index},counts)
    set_state(services.repository,package['package_id'],'mail_parse','running',cursor={'next_ordinal':next_index,'total':len(entries)})
    _job_update(services,job,progress=min(94,int(next_index*95/max(1,len(entries)))),stage='mail_import',message='正在处理来源条目 {}/{}'.format(next_index,len(entries)))
    if next_index<len(entries):return {'_requeue_slice':True,'_requeue_message':'邮件导入检查点已保存，继续下一批。','package_id':package['package_id']}
    with services.repository._connection() as c:
        statuses={r[0]:r[1] for r in c.execute('SELECT status,COUNT(*) FROM mail_import_entries WHERE package_id=? GROUP BY status',(package['package_id'],))}
        logical=c.execute('SELECT COUNT(DISTINCT message_key) FROM mail_visible_occurrences WHERE package_id=?',(package['package_id'],)).fetchone()[0]
    if not logical:
        message='来源中没有成功解析的 EML 邮件；详见导入清单'
        if source.kind=='directory' and any(m.name.casefold().endswith('.zip') for m,_,_ in entries):
            message+='。目录内的 ZIP 不会自动展开，请选择其中的 ZIP 文件分别导入'
        services.repository.update_package(package['package_id'],owner_id,status='failed',error={'message':message,'inventory':statuses})
        set_state(services.repository,package['package_id'],'mail_parse','terminal_failed',error={'inventory':statuses})
        services.repository.save_progress(job['id'],'failed',{'next_ordinal':next_index},statuses)
        raise ValueError(message)
    services.repository.finish_import(package['package_id'],owner_id)
    set_state(services.repository,package['package_id'],'mail_parse','partial' if statuses.get('terminal_failed') or statuses.get('restricted') else 'ready',cursor={'next_ordinal':next_index,'total':len(entries)})
    services.repository.save_progress(job['id'],'completed',{'next_ordinal':next_index},statuses)
    _queue_derivatives(services,package,attachments=True)
    return {'package_id':package['package_id'],'counts':services.repository.overview(package['package_id'],owner_id)['counts'],'inventory':statuses}


def _extract(job, services):
    package=_package(services,job);owner_id=_owner(job);pid=package['package_id']
    services.repository.schedule_attachment_upgrade(pid,owner_id)
    batch_size=max(1,min(100,int(Config.MAIL_IMPORT_SLICE_MESSAGES)))
    pending=services.repository.pending_attachments(pid,owner_id,limit=batch_size)
    for item in pending:
        _active(services,job['id'])
        try:
            part,_=read_part(services.repository,item['message_key'],owner_id,pid,item['mime_path'])
            services.policy.validate_size(part.byte_size,limit=services.policy.max_attachment_bytes,label='附件')
            result=extract_attachment(part)
        except Exception as exc:
            result={'status':'failed','parser':'mail-source','error':{'type':type(exc).__name__,'message':str(exc)[:500]}}
        services.repository.store_extraction(pid,owner_id,item['message_key'],item['mime_path'],result)
    coverage=services.repository.coverage(pid,owner_id)
    services.repository.save_progress(job['id'],'extracting',{'last_batch':len(pending)},coverage)
    if services.repository.pending_attachments(pid,owner_id,limit=1):
        return {'_requeue_slice':True,'_requeue_message':'附件解析检查点已保存，继续下一批。','package_id':pid}
    with services.repository._connection() as c:
        waiting=c.execute("SELECT MIN(x.next_retry_at) FROM mail_part_processing x JOIN mail_visible_occurrences o ON o.message_key=x.message_key WHERE o.package_id=? AND x.stage='basic_text' AND x.status='waiting_retry'",(pid,)).fetchone()[0]
    if waiting:return {'_defer_slice':True,'_defer_seconds':30,'_defer_message':'附件失败已记录，等待有限重试','package_id':pid}
    services.repository.update_package(pid,owner_id,status='attachment_index_ready')
    _queue_derivatives(services,package,ocr=True)
    return {'package_id':pid,'processed':len(pending),'coverage':coverage}


def _ocr(job, services):
    from .extraction import extract_ocr
    package=_package(services,job);owner_id=_owner(job);pid=package['package_id']
    with services.repository._connection() as c:
        rows=[dict(r) for r in c.execute("SELECT DISTINCT p.message_key,p.mime_path FROM mail_parts p JOIN mail_visible_occurrences o ON o.message_key=p.message_key JOIN mail_part_processing x ON x.message_key=p.message_key AND x.mime_path=p.mime_path AND x.stage='basic_text' LEFT JOIN mail_part_processing ox ON ox.message_key=p.message_key AND ox.mime_path=p.mime_path AND ox.stage='ocr' WHERE o.package_id=? AND x.status='ocr_pending' AND (ox.status IS NULL OR ox.status='pending' OR (ox.status IN ('waiting_retry','failed') AND ox.retry_count<3 AND (ox.next_retry_at IS NULL OR ox.next_retry_at<=CURRENT_TIMESTAMP))) LIMIT 5",(pid,))]
    for item in rows:
        _active(services,job['id'])
        try:
            part,_=read_part(services.repository,item['message_key'],owner_id,pid,item['mime_path'])
            result=extract_ocr(part,services.parser,lambda:services.repository.cancelled(job['id']))
        except Exception as exc:
            _active(services,job['id'])
            result={'status':'failed','parser':'mail-source','error':{'message':str(exc)[:500]}}
        services.repository.store_extraction(pid,owner_id,item['message_key'],item['mime_path'],result,stage='ocr')
    if len(rows)==5:return {'package_id':pid,'_requeue_slice':True,'_requeue_message':'OCR 检查点已保存'}
    with services.repository._connection() as c:
        waiting=c.execute("SELECT COUNT(*) FROM mail_part_processing x JOIN mail_visible_occurrences o ON o.message_key=x.message_key WHERE o.package_id=? AND x.stage='ocr' AND x.status='waiting_retry'",(pid,)).fetchone()[0]
    if waiting:return {'package_id':pid,'_defer_slice':True,'_defer_seconds':30,'_defer_message':'OCR 等待有限重试'}
    return {'package_id':pid,'processed':len(rows),'coverage':services.repository.coverage(pid,owner_id)}


def _foundation(job,services):
    from .foundation import backfill_slice
    package=_package(services,job)
    result=backfill_slice(services.repository,package['package_id'],_owner(job),job_id=job['id'],cancelled=lambda:services.repository.cancelled(job['id']))
    if not result.get('_requeue_slice') and result.get('status')!='skipped':_queue_derivatives(services,package,attachments=True)
    return result


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
    if turn.get('task_spec', {}).get('mode') in {'message_dossier','pair_dossier','intent_screen'}:
        from .analysis import run_slice
        try:
            return run_slice(services.repository,turn_id,owner_id,services.model,
                cancelled=lambda:services.repository.cancelled(job['id']))
        except Exception as exc:
            current=services.repository.get_turn(turn_id,owner_id)
            if current and current['status'] not in {'cancelled','cancelling','completed'}:
                services.repository.update_turn(turn_id,owner_id,status='failed',sufficiency='partial',error={'message':str(exc)[:500]})
            raise
    if turn.get('task_spec', {}).get('mode') == 'person_correspondence':
        from .person_research import run_person_slice
        v3=turn.get('task_spec',{}).get('analysis_version')==3
        deep=v3 and turn.get('task_spec',{}).get('purpose')=='deep_analysis'
        _job_update(services, job, progress=10, stage='mail_person_analysis', message='基于已完成事实报告分析目标，核对条件与反证' if deep else
                    '逐批扫描整个资料包，生成事实报告或目标深度分析' if v3 else '逐批阅读该联系人的全部往来正文')
        return run_person_slice(services.repository, turn_id, owner_id, services.model,
                                cancelled=lambda: services.repository.cancelled(job['id']))
    _job_update(services, job, progress=8, stage="mail_research", message="正在检索邮件证据")
    runtime = MailResearchRuntime(
        services.repository,
        model=services.model,
        context_limit=Config.MAIL_TOOL_CONTEXT_MAX_CHARS,
        cancel_check=lambda: services.repository.cancelled(job["id"]),
        progress_callback=lambda **progress: _job_update(services,job,**progress),
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
        "mail_foundation": _foundation, "mail_import": _import, "mail_extract": _extract, "mail_ocr": _ocr,
        "mail_research": _research, "mail_export": _export, "mail_embed": _embed, "mail_factextract": _factextract,
    }
    return handlers[task_type](job, services)
