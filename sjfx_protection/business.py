"""Explicit authorization and immutable content reads across HTTP and workers."""
from __future__ import annotations
import contextlib
import contextvars
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from .policy import protection_enabled, file_protection_requested
from .store import ProtectionError
execution=contextvars.ContextVar("sjfx_package_execution",default=None)
task_identity=contextvars.ContextVar("sjfx_business_task_identity",default="")
result_receipts_enabled=contextvars.ContextVar("sjfx_business_result_receipts_enabled",default=True)
metadata_projection=contextvars.ContextVar("sjfx_protection_metadata_projection",default=False)
_configured=None
_background_services={}

def configure(protection):
    global _configured
    _configured=protection

def enabled():
    if not file_protection_requested():return False
    if _configured is not None:return _configured.enabled
    from config import Config
    return protection_enabled(Config.DATA_DIR/"file_protection")

def protection():
    if not enabled():return None
    if _configured is not None:return _configured
    from config import Config
    key=(str(Path(Config.DB_PATH).resolve()),str(Path(Config.DATA_DIR).resolve()),
         str(Path(Config.OUTPUT_DIR).resolve()),tuple(str(v) for v in Config.SCAN_ALLOWED_ROOTS))
    if key not in _background_services:
        from .registration import PackageProtection
        from .store import Store
        adapter=BusinessAdapter(Config.DB_PATH,Config.DATA_DIR/"large_package.db",Config.OUTPUT_DIR)
        store=Store(Config.DATA_DIR/"file_protection")
        package=PackageProtection(store,Config.SCAN_ALLOWED_ROOTS,
            authorize_root=adapter.authorize_root,authorize_owner=adapter.authorize_owner)
        class BackendProtection:
            @property
            def enabled(self):return package.enabled
        service=BackendProtection()
        service.store=store;service.package=package
        _background_services[key]=service
    return _background_services[key]

def context():
    value=execution.get()
    if value is not None:return value
    module=sys.modules.get('sjfx_protection.integration')
    return module.current.get() if module is not None else None

def decision_context(decision):
    return dict(decision.get("context") or decision.get("protection_context") or decision)

@contextlib.contextmanager
def activate(value,*,task_id=None):
    # Task attribution is separate from the frozen authorization receipt.
    # None inherits this thread's job; an explicit empty ID clears IPC state.
    task_token=task_identity.set(str(task_id or '')) if task_id is not None else None
    token=execution.set(value)
    try:yield
    finally:
        execution.reset(token)
        if task_token is not None:task_identity.reset(task_token)

def current_task_id():
    return task_identity.get()

def select_context(p,ctx,owner,root=None,registration_id=None):
    candidates=ctx.get("package_contexts") or [ctx]
    if len(candidates)==1:return candidates[0]
    reg=p.package.resolve(owner,root=root,registration_id=registration_id)
    selected=next((item for item in candidates if item.get("registration_id")==reg["id"]),None)
    if not selected:raise ProtectionError("目标资料不在本任务核准集合内",423,"context_target_mismatch")
    return selected

def require_root(owner,root,operation="read",saved=None):
    p=protection()
    if p is None:return None
    if not owner:raise ProtectionError("任务缺少明确资料归属",423,"protection_context_required")
    ctx=saved or context() or {}
    if ctx:ctx=select_context(p,ctx,owner,root=root)
    decision=p.package.require_operation(str(owner),operation=operation,root=str(root),
        device_id=str(ctx.get("device_id") or ctx.get("device") or ""),context=ctx if ctx.get("registration_id") else None,
        approval_id=ctx.get("approval_id"))
    return decision_context(decision)

def capture_job(owner,root,options,operation="analyze"):
    if not enabled():return options
    value=dict(options or {})
    value["protection_context"]=require_root(owner,root,operation)
    return value

def capture_import(owner,root,options=None,user_id=None):
    if not enabled():return options
    value=dict(options or {});p=protection()
    if not user_id:
        account=p.package._owner(owner,'')
        user_id=account.get('user_id','') if isinstance(account,dict) else ''
    value['protection_intake']=p.package.begin_local(owner,user_id,root)
    value.pop('protection_context',None)
    return value

def admit_import(owner,root,options,*,persist=None,progress=None,cancel=None,wait=True):
    """Run/await durable inventory before any business parser or cache lookup."""
    if not enabled():return options
    value=dict(options or {});p=protection();receipt=value.get('protection_intake')
    if value.get('protection_context'):
        require_root(owner,root,'analyze',value['protection_context'])
        return value
    if not receipt:raise ProtectionError('历史导入任务未保存安全阶段，请重新选择导入',423,'protection_context_required')
    while True:
        if cancel and cancel():raise ProtectionError('导入任务已取消',409,'import_cancelled')
        job=p.package.jobs.get(owner,receipt['job_id'])
        if progress:progress(job)
        if job['state']=='queued':
            if not wait:raise ProtectionError('安全检查待执行',423,'inspection_pending')
            p.package.process_job(job['id'])
            continue
        if job['state']=='running':
            if not wait:raise ProtectionError('安全检查正在执行',423,'inspection_pending')
            if job['lease_until']<time.time():p.package.process_job(job['id'])
            else:time.sleep(1)
            continue
        frozen=p.package.complete_local(owner,receipt)
        if cancel and cancel():raise ProtectionError('导入任务已取消',409,'import_cancelled')
        value['protection_context']=frozen
        if persist:persist(value)
        return value

def job_operation(task_type):
    value=str(task_type or '')
    if 'export' in value or 'download' in value:return 'export'
    if 'translate' in value or 'translation' in value:return 'translate'
    return 'analyze'

def capture_resource_job(owner,identifier,options=None,task_type="analyze"):
    if not enabled():return options
    from config import Config
    adapter=BusinessAdapter(Config.DB_PATH,Config.DATA_DIR/"large_package.db",Config.OUTPUT_DIR)
    if task_type=='mail_import':
        packages=adapter.rows('SELECT source_path,config_json FROM mail_packages WHERE package_id=? AND owner_id=?',(identifier,owner))
        if packages:
            package=packages[0];stored=json.loads(package['config_json'] or '{}')
            intake=stored.get('protection_intake')
            if intake:
                value=dict(options or {});value.update(protection_intake=intake,root_path=package['source_path'])
                # A resumed slice preserves its checked context; it cannot bind
                # previously parsed mail to a newer revision.
                if stored.get('protection_context'):value['protection_context']=stored['protection_context']
                return value
    roots=adapter.resolve_job(owner,{"scan_id":identifier,"options":options or {},"task_type":task_type})
    if not roots:raise ProtectionError("任务没有可核验来源",423,"protection_target_required")
    contexts=[require_root(owner,root,job_operation(task_type)) for root in roots]
    value=dict(options or {})
    value["protection_context"]=contexts[0] if len(contexts)==1 else {"owner":owner,"package_contexts":contexts}
    return value

@contextlib.contextmanager
def job_execution(job,*,record_result=True):
    if not enabled():
        yield
        return
    from config import Config
    adapter=BusinessAdapter(Config.DB_PATH,Config.DATA_DIR/"large_package.db",Config.OUTPUT_DIR)
    owner=str(job.get("owner_id") or "")
    if (job.get('options') or {}).get('protection_intake'):
        from services.storage import Storage
        storage=Storage(Config.DB_PATH)
        def progress(check):
            storage.update_job(job['id'],stage='security_inspection',progress=1,
                message='正在检查本地资料：'+check['state'],current_stage='安全检查',heartbeat=True)
            storage.update_import_task(job['id'],status='scanning',phase='security_inspection')
        job['options']=admit_import(owner,job['options']['root_path'],job['options'],
            persist=lambda value:storage.save_protection_intake(job['id'],value),progress=progress,
            cancel=lambda:storage.is_job_cancel_requested(job['id']))
        frozen=job['options']['protection_context']
        protection().package.bind_business(owner,frozen['registration_id'],'mail_package' if job.get('task_type')=='mail_import' else 'scan',job.get('scan_id') or job['id'])
        if job.get('task_type')=='mail_import':
            with storage.lock,storage._connect() as db:
                row=db.execute('SELECT config_json FROM mail_packages WHERE package_id=? AND owner_id=?',(job['scan_id'],owner)).fetchone()
                stored=json.loads(row[0] or '{}');stored['protection_context']=frozen
                db.execute('UPDATE mail_packages SET config_json=? WHERE package_id=? AND owner_id=?',(json.dumps(stored,ensure_ascii=False),job['scan_id'],owner))
    saved=(job.get("options") or {}).get("protection_context")
    if not isinstance(saved,dict):raise ProtectionError("历史任务需核验后重新提交",423,"protection_context_required")
    roots=adapter.resolve_job(owner,job)
    if not roots:raise ProtectionError("任务没有可核验来源",423,"protection_target_required")
    contexts=[require_root(owner,root,job_operation(job.get('task_type')),saved) for root in roots]
    value=contexts[0] if len(contexts)==1 else {"owner":owner,"package_contexts":contexts}
    # A Worker publishes its actual terminal status after committing the result.
    # Nested scopes (including mail jobs) cannot override that suppression.
    recording=bool(record_result and result_receipts_enabled.get())
    receipt_token=result_receipts_enabled.set(recording)
    try:
        with activate(value,task_id=job.get('id','')):
            try:
                yield
                recheck_active_sources(job_operation(job.get('task_type')))
            except BaseException as exc:
                if recording:
                    record_execution_result(value,job_operation(job.get('task_type')),'cancelled' if isinstance(exc,ProtectionError) and exc.code=='import_cancelled' else 'failed',job.get('id',''))
                raise
            else:
                if recording:record_execution_result(value,job_operation(job.get('task_type')),'completed',job.get('id',''))
    finally:result_receipts_enabled.reset(receipt_token)

def record_execution_result(value,operation,result,task_id='',*,phase='execution'):
    p=protection()
    if p is None:return
    for source in value.get('package_contexts') or [value]:
        p.package.record_operation(source['owner'],source['registration_id'],operation,result,
            context=source,targets=source.get('versions'),task_id=task_id,source='local_business',trusted=True,phase=phase)

def _snapshot_value(result):
    if isinstance(result,dict):return result.get("snapshot_path") or result.get("snapshot") or result.get("path")
    return result

def _matched_file(files,path):
    resolved=Path(path).resolve()
    return next((f for f in files if any(candidate and Path(candidate).resolve()==resolved
        for candidate in (f.get("path"),f.get("snapshot"),f.get("snapshot_path")))),None)

def protected_path(path,*,owner=None,operation="read",derived=False,member_name=None):
    p=protection()
    if p is None:return Path(path)
    ctx=context() or {}
    owner=owner or ctx.get("owner")
    if not owner:raise ProtectionError("内容读取缺少任务或会话保护信息",423,"protection_context_required")
    path=Path(path)
    known=ctx.get("derived_files",{}).get(str(path.resolve()))
    if known:
        artifact=p.package.require_artifact(owner,known["artifact_id"],device_id=ctx.get("device_id") or ctx.get("device",""),operation=operation,approval_id=ctx.get('approval_id'))
        return Path(artifact["path"])
    if derived:
        result=p.package.inspect_derived(owner,ctx["registration_id"],ctx.get("version_id") or "",
            member_name or path.name,path=path,context=ctx)
        artifact=p.package.require_artifact(owner,result['artifact_id'],device_id=ctx.get('device_id') or ctx.get('device',''),operation=operation,approval_id=ctx.get('approval_id'))
        return Path(_snapshot_value(artifact))
    candidate=None;selected=None
    for option in ctx.get("package_contexts") or [ctx]:
        if option.get("registration_id"):
            files=p.package.manifest(owner,option["registration_id"])["files"]
            candidate=_matched_file(files,path)
            if candidate:selected=option;break
    original=candidate["path"] if candidate else str(path)
    selected=selected or select_context(p,ctx,owner,root=original)
    decision=p.package.require_operation(owner,root=original,operation=operation,
        device_id=selected.get("device_id") or selected.get("device",""),context=selected if selected.get("registration_id") else None,
        approval_id=selected.get("approval_id"))
    fresh=decision_context(decision)
    files=p.package.manifest(owner,fresh["registration_id"])["files"]
    row=_matched_file(files,original)
    if not row:raise ProtectionError("文件不在当前获准清单中",423,"unregistered_file")
    return Path(_snapshot_value(p.package.verified_snapshot(owner,fresh["registration_id"],row.get("file_id") or row["id"],context=fresh)))

@contextlib.contextmanager
def file_execution(path,*,derived=False,member_name=None,operation="parse"):
    if not enabled():
        yield Path(path)
        return
    p=protection();ctx=context() or {};owner=ctx.get("owner")
    selected_path=protected_path(path,derived=derived,member_name=member_name,operation=operation)
    row=None;selected=None
    for option in ctx.get("package_contexts") or [ctx]:
        if option.get("registration_id"):
            row=_matched_file(p.package.manifest(owner,option["registration_id"])["files"],path)
            if row:selected=dict(option);break
    selected=selected or dict(ctx)
    entry=ctx.get("derived_files",{}).get(str(Path(path).resolve()))
    if row:selected["version_id"]=row["version_id"]
    if entry:selected["version_id"]=entry["version_id"]
    payload={"registration_id":selected.get("registration_id"),"version_id":selected.get("version_id"),
        "source_path":str(path),"derived":derived or bool(entry),"member_name":member_name,
        "task_id":current_task_id()}
    with activate(selected):
        try:
            yield selected_path
            recheck_active_sources(operation)
        except Exception:
            p.store.event(owner,"file."+operation+".failed",file_id=(row or {}).get("file_id",""),payload=payload)
            raise
        else:p.store.event(owner,"file."+operation+".completed",file_id=(row or {}).get("file_id",""),payload=payload)

def inspect_bytes(raw,name,parent_version_id=None):
    if not enabled():return raw
    p=protection();ctx=context() or {}
    parent=parent_version_id or ctx.get("version_id")
    if not ctx.get("owner") or not ctx.get("registration_id") or not parent:
        raise ProtectionError("派生字节缺少核准父版本",423,"provenance_missing")
    p.package.inspect_derived(ctx["owner"],ctx["registration_id"],parent,name,raw=raw,context=ctx)
    return raw

def inspected_derived_path(path,name=None):
    if not enabled():return Path(path)
    p=protection();ctx=context() or {}
    result=p.package.inspect_derived(ctx["owner"],ctx["registration_id"],ctx["version_id"],name or Path(path).name,path=path,context=ctx)
    snapshot=Path(_snapshot_value(result))
    ctx.setdefault("derived_files",{})[str(snapshot.resolve())]={"artifact_id":result["artifact_id"],"version_id":result["version_id"]}
    return snapshot

def validated_cached_bytes(raw,name,owner=None):
    if not enabled():return raw
    p=protection();ctx=context() or {};owner=owner or ctx.get("owner")
    if not ctx.get("registration_id") or not owner:raise ProtectionError("缓存缺少核准来源",423,"provenance_missing")
    rid=ctx["registration_id"];content=hashlib.sha256(raw).hexdigest()
    decision=p.package.require_operation(owner,registration_id=rid,device_id=ctx.get("device_id") or ctx.get("device",""),context=ctx,record=False)
    matches=[f for f in p.package.manifest(owner,rid)["files"] if f["sha256"]==content]
    if matches:return raw
    with p.store.db() as db:
        rows=db.execute("SELECT id,sources FROM protection_artifacts WHERE owner=? AND sha256=? AND risk='no_detection'",(owner,content)).fetchall()
    for row in rows:
        if not any(source.get("registration_id")==rid for source in json.loads(row["sources"])):continue
        p.package.require_artifact(owner,row["id"],device_id=ctx.get("device_id") or ctx.get("device",""),operation="read")
        return raw
    raise ProtectionError("历史缓存版本尚未与核准来源关联，请重新生成",423,"cached_version_unverified")

def record_business_artifact(filename,owner):
    if not enabled():return
    from config import Config
    p=protection();ctx=context() or {}
    if ctx.get("owner")!=owner:raise ProtectionError("导出产物缺少账号来源",423,"provenance_missing")
    path=next((candidate for candidate in [Config.OUTPUT_DIR/owner/filename,Config.OUTPUT_DIR/filename]
        if candidate.is_file() and not candidate.is_symlink()),None)
    if path is None:raise ProtectionError("导出产物不存在",423,"artifact_missing")
    contexts=ctx.get("package_contexts") or [ctx]
    sources=[{"registration_id":source["registration_id"],"versions":source["versions"],"context":source} for source in contexts]
    result=p.package.record_artifact(owner,path,sources)
    with p.store.db(True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)",("package-artifact:"+owner+":"+filename,result["id"]))


def recheck_active_sources(operation=None):
    """Recheck after model admission waits and before accepting model output."""
    if not enabled():return
    ctx=context() or {}
    candidates=ctx.get('package_contexts') or [ctx]
    sources=[source for source in candidates if source.get('registration_id')]
    if not sources:return
    p=protection()
    for source in sources:
        p.package.require_operation(source['owner'],registration_id=source['registration_id'],
            operation=operation or source.get('operation','analyze'),device_id=source.get('device_id') or source.get('device',''),
            context=source,versions=source['versions'],approval_id=source.get('approval_id'),record=False)

class BusinessAdapter:
    """Only stored ownership/locators map a business ID to registered sources."""
    def __init__(self, db_path, large_db_path=None, output_dir=None):
        self.db_path=Path(db_path)
        self.large_db_path=Path(large_db_path) if large_db_path else None
        self.output_dir=Path(output_dir) if output_dir else None

    def rows(self, sql, values=(), large=False):
        path=self.large_db_path if large else self.db_path
        if path is None or not path.is_file():return []
        db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
        db.row_factory=sqlite3.Row
        try:
            return [dict(r) for r in db.execute(sql,values)]
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):return []
            raise
        finally:db.close()

    def authorize_owner(self,owner,user_id=''):
        rows=self.rows('SELECT user_id,status,must_change_pw,role FROM users WHERE owner_id=?',(owner,))
        if not rows or rows[0]['status']!='active' or rows[0]['must_change_pw']:
            raise ProtectionError('账号不可使用受保护资料',403,'account_disabled')
        if user_id and rows[0]['user_id']!=user_id:
            raise ProtectionError('账号归属不匹配',403,'owner_mismatch')
        return rows[0]

    def authorize_root(self,owner,root):
        self.authorize_owner(owner)
        from config import Config
        # The existing new-import contract permits native paths under this runtime's allow-list.
        # Existing owner-bound scans/packages and output roots remain independently checked.
        roots=[str(value) for value in Config.SCAN_ALLOWED_ROOTS]
        roots.extend(r['root_path'] for r in self.rows('SELECT root_path FROM scans WHERE owner_id=?',(owner,)))
        roots.extend(r['source_path'] for r in self.rows('SELECT source_path FROM mail_packages WHERE owner_id=?',(owner,)))
        roots.extend(r['root_path'] for r in self.rows('SELECT root_path FROM packages WHERE owner_id=?',(owner,),large=True))
        if self.output_dir:
            roots.append(str(self.output_dir/'_uploads'/'assembled'/owner))
        state_db=self.db_path.parent/'file_protection'/'protection.db'
        if state_db.is_file():
            db=sqlite3.connect(state_db.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
            try:row=db.execute('SELECT value FROM settings WHERE key=?',('package-root-grants:'+owner,)).fetchone()
            finally:db.close()
            if row:roots.extend(json.loads(row[0]))
        requested=Path(root).resolve()
        if not any(requested==Path(r).resolve() or requested.is_relative_to(Path(r).resolve()) for r in roots if r):
            raise ProtectionError('该账号未获授权访问本地资料目录，请管理员授权资料位置',403,'root_access_denied')
        return True

    def _scan(self,owner,identifier):
        rows=self.rows('SELECT root_path FROM scans WHERE id=? AND owner_id=?',(identifier,owner))
        return [r['root_path'] for r in rows]

    def _package(self,owner,identifier):
        rows=self.rows('SELECT source_path FROM mail_packages WHERE package_id=? AND owner_id=?',(identifier,owner))
        rows+=self.rows('SELECT root_path AS source_path FROM packages WHERE id=? AND owner_id=?',(identifier,owner),large=True)
        return [r['source_path'] for r in rows]

    def _identifier_roots(self,owner,values):
        roots=[]
        if values.get('scan_id'):roots.extend(self._scan(owner,values['scan_id']))
        if values.get('package_id'):roots.extend(self._package(owner,values['package_id']))
        queries=(
            ('turn_id','SELECT scan_id FROM conversation_turns WHERE id=? AND owner_id=?','scan_id'),
            ('turn_id','SELECT s.package_id FROM mail_research_turns t JOIN mail_research_sessions s ON s.session_id=t.session_id WHERE t.turn_id=? AND t.owner_id=?','package_id'),
            ('session_id','SELECT scan_id FROM conversations WHERE id=? AND owner_id=?','scan_id'),
            ('session_id','SELECT package_id FROM mail_research_sessions WHERE session_id=? AND owner_id=?','package_id'),
            ('conversation_id','SELECT v.package_id FROM mail_conversations v JOIN mail_packages p ON p.package_id=v.package_id WHERE v.conversation_id=? AND p.owner_id=?','package_id'),
            ('target_conversation_id','SELECT v.package_id FROM mail_conversations v JOIN mail_packages p ON p.package_id=v.package_id WHERE v.conversation_id=? AND p.owner_id=?','package_id'),
            ('result_set_id','SELECT package_id FROM mail_result_sets WHERE result_set_id=? AND owner_id=?','package_id'),
            ('evidence_id','SELECT e.package_id FROM mail_evidence e JOIN mail_packages p ON p.package_id=e.package_id WHERE e.evidence_id=? AND p.owner_id=?','package_id'),
            ('message_key','SELECT DISTINCT o.package_id FROM mail_occurrences o JOIN mail_packages p ON p.package_id=o.package_id WHERE o.message_key=? AND p.owner_id=?','package_id'))
        for key,sql,target in queries:
            if values.get(key):
                for row in self.rows(sql,(values[key],owner)):
                    roots.extend(self._scan(owner,row[target]) if target=='scan_id' else self._package(owner,row[target]))
        return roots

    def resolve_job(self,owner,job):
        options=job.get('options') or {}
        roots=self._identifier_roots(owner,options)
        # scan_id also holds mail/large package IDs in the existing job queue.
        identifier=job.get('scan_id')
        if identifier:roots.extend(self._scan(owner,identifier) or self._package(owner,identifier))
        if job.get('task_type')=='scan_and_analyze':
            root=options.get('root_path') or options.get('root')
            if root:roots.append(root)
        return sorted(set(roots))

    def resolve(self,owner,path,method,query,data):
        values={**query,**(data if isinstance(data,dict) else {})}
        roots=[]
        imports={'/api/scan','/api/large-packages','/api/mail/packages'}
        if method=='POST' and path in imports:
            if path=='/api/mail/packages' and isinstance(values.get('source'),dict):
                raw=values['source'].get('path')
                if isinstance(raw,str) and raw:roots.append(raw)
            for key in ('path','root','root_path','source_path'):
                if isinstance(values.get(key),str) and values[key]:roots.append(values[key])
        patterns={
            'scan_id':r'^/api/(?:scans?|package-processing|package|import-tasks|file-workflow|tree|tree-edits|summaries|file-conclusions|analysis-node-members|analysis|document|package-overview|homogeneous-analysis|homogeneous-record|relationships|conversations|translations?|translate-package|file-summaries|retry-failed)/([^/]+)',
            'package_id':r'^/api/(?:mail/)?(?:large-packages|packages)/([^/]+)',
            'job_id':r'^/api/jobs/([^/]+)',
            'turn_id':r'^/api/(?:mail/)?(?:turns|research-turns|person-analyses|intent-analyses)/([^/]+)',
            'session_id':r'^/api/(?:conversation|mail/research-sessions)/([^/]+)',
            'conversation_id':r'^/api/mail/conversations/([^/]+)',
            'result_set_id':r'^/api/mail/result-sets/([^/]+)',
            'evidence_id':r'^/api/mail/evidence/([^/]+)',
            'message_key':r'^/api/mail/messages/([^/]+)'}
        for key,pattern in patterns.items():
            match=re.search(pattern,path)
            if match:
                actual=match.group(1)
                if values.get(key) and values[key]!=actual:
                    raise ProtectionError('请求目标与路径标识不一致',409,'context_target_mismatch')
                values[key]=actual
        roots.extend(self._identifier_roots(owner,values))
        if values.get('job_id'):
            for row in self.rows('SELECT scan_id,task_type,options FROM analysis_jobs WHERE id=? AND owner_id=?',(values['job_id'],owner)):
                row['options']=json.loads(row['options'] or '{}');roots.extend(self.resolve_job(owner,row))
        if path.startswith('/outputs/') or path=='/api/download-ticket':
            filename=path.removeprefix('/outputs/') if path.startswith('/outputs/') else values.get('filename','')
            for row in self.rows('SELECT scan_id,job_id FROM output_artifacts WHERE filename=? AND owner_id=?',(filename,owner)):
                if row['scan_id']:roots.extend(self._scan(owner,row['scan_id']) or self._package(owner,row['scan_id']))
                if row['job_id']:
                    for job in self.rows('SELECT scan_id,task_type,options FROM analysis_jobs WHERE id=? AND owner_id=?',(row['job_id'],owner)):
                        job['options']=json.loads(job['options'] or '{}');roots.extend(self.resolve_job(owner,job))
        return sorted(set(roots))


@contextlib.contextmanager
def saved_execution(owner,roots,saved,operation='read'):
    if not enabled():
        yield
        return
    if not isinstance(saved,dict):raise ProtectionError('缓存或后台任务缺少版本来源，请重新生成',423,'protection_context_required')
    contexts=[require_root(owner,root,operation,saved) for root in roots]
    value=contexts[0] if len(contexts)==1 else {'owner':owner,'package_contexts':contexts}
    with activate(value):
        try:
            yield
            recheck_active_sources(operation)
        except BaseException:
            record_execution_result(value,operation,'failed')
            raise
        else:record_execution_result(value,operation,'completed')
