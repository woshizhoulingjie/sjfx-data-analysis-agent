"""Native local byte authority, version invalidation and exact operation names.

The path is opened by this runtime on this computer. A Windows path supplied to
a Linux runtime is rejected; this is not a network upload or a remote path map.
Directory observations are after-the-event evidence, never a kernel filter.
"""
from __future__ import annotations

import getpass
import json
import math
import ntpath
import os
import time
from pathlib import Path

from .scanner import stable_hash
from .store import ProtectionError, encode, uid

OPERATIONS = {'read','analyze','parse','preview','translate','search','export',
              'delete','bulk_delete','rewrite','bulk_rewrite','open'}


def canonical_operation(value):
    # A download is the outward transfer of an already produced export. One
    # identity is used by request, approval, ticket issue and ticket consumption.
    result = {'sensitive_export':'export','download':'export'}.get(value,value)
    if result not in OPERATIONS:
        raise ProtectionError('未知保护操作',400,'invalid_operation')
    return result


def approval_for(value, registration_id):
    if isinstance(value,str) and value.lstrip().startswith('{'):
        try:value=json.loads(value)
        except ValueError:raise ProtectionError('多包审批凭据格式无效',400,'invalid_approvals')
    if isinstance(value,dict):
        if any(not isinstance(k,str) or not isinstance(v,str) for k,v in value.items()):
            raise ProtectionError('审批映射必须由登记ID指向审批ID',400,'invalid_approvals')
        return value.get(registration_id,'')
    if value is None:return ''
    if not isinstance(value,str):raise ProtectionError('审批凭据格式无效',400,'invalid_approvals')
    return value


def route_operation(path, method, payload):
    if any(v in path for v in ('/export','/download','/outputs/')):return 'export'
    if method=='DELETE' or '/delete' in path:
        # Count concrete supplied targets, never rely on a client boolean.
        targets=payload.get('paths') or payload.get('file_ids') or payload.get('ids') or []
        return 'bulk_delete' if isinstance(targets,list) and len(set(map(str,targets)))>1 else 'delete'
    if '/rewrite' in path:
        targets=payload.get('paths') or payload.get('file_ids') or []
        return 'bulk_rewrite' if isinstance(targets,list) and len(set(map(str,targets)))>1 else 'rewrite'
    if '/translate' in path or '/translation' in path:return 'translate'
    if '/preview' in path:return 'preview'
    if '/search' in path or '/retrieve' in path:return 'search'
    return 'analyze' if method=='POST' else 'read'


class LocalSourceMixin:
    def local_device_id(self):
        cached=getattr(self,'_local_device',None)
        if cached:return cached
        with self.store.db(True) as db:
            row=db.execute("SELECT value FROM settings WHERE key='local-runtime-device'").fetchone()
            if row:value=row[0]
            else:
                value='local-runtime:'+uid()
                db.execute('INSERT INTO settings VALUES(?,?)',('local-runtime-device',value))
        self._local_device=value
        return value

    @staticmethod
    def validate_native_path(value):
        if not isinstance(value,str) or not value or '\x00' in value:
            raise ProtectionError('必须选择电脑本地绝对路径',400,'local_path_required')
        if os.name!='nt' and (ntpath.splitdrive(value)[0] or '\\' in value):
            raise ProtectionError('当前读取进程不在该Windows电脑上；请使用电脑本地SJFX入口',409,'reader_host_mismatch')
        path=Path(value).expanduser()
        if not path.is_absolute():raise ProtectionError('必须提供本地绝对路径',400,'local_path_required')
        for item in [path,*path.parents]:
            if item.is_symlink() or (hasattr(item,'is_junction') and item.is_junction()):
                raise ProtectionError('本地资料路径不能经过符号链接或目录联接',423,'manifest_incomplete')

    def local_epoch(self,reg,require_verified=True):
        with self.store.db() as db:
            row=db.execute('SELECT * FROM registration_devices WHERE registration_id=? AND device_id=?',
                           (reg['id'],self.local_device_id())).fetchone()
        if not row:raise ProtectionError('本地读取设备身份缺失',423,'reader_host_mismatch')
        if require_verified and (row['verified_epoch']!=row['gap_epoch'] or row['state']!='verified'):
            raise ProtectionError('本地覆盖存在缺口，需完整重新检查',423,'monitoring_gap')
        return row['gap_epoch']

    def local_paths(self,reg):
        root=Path(reg['root'])
        self.validate_native_path(str(root))
        if root.is_file():return [str(root)]
        if not root.is_dir():raise ProtectionError('本地资料目录不可访问',423,'root_unavailable')
        paths=[]
        def fail(exc):raise exc
        for directory,dirs,files in os.walk(root,followlinks=False,onerror=fail):
            for name in dirs+files:
                path=Path(directory)/name
                if path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):
                    raise ProtectionError('资料范围出现链接或目录联接',423,'content_changed')
            paths.extend(str(Path(directory)/name) for name in files)
            if len(paths)>self.policy()['inspection_profile'].get('inventory_max_files',200000):
                raise ProtectionError('资料成员超过检查预算',423,'manifest_incomplete')
        return sorted(paths)

    def compare_local_members(self,reg,members):
        expected={m['path']:m for m in members}
        if self.local_paths(reg)!=sorted(expected):
            raise ProtectionError('本地成员集合发生变化',423,'content_changed')
        for path,row in expected.items():
            info=stable_hash(path)
            if any(info[k]!=row[k] for k in ('sha256','size','identity','mtime_ns')):
                raise ProtectionError('本地实例或内容版本发生变化',423,'content_changed')
        if self.local_paths(reg)!=sorted(expected):
            raise ProtectionError('检查期间本地成员集合发生变化',423,'content_changed')

    def invalidate_local(self,owner,reg,reason,observed=None):
        now=time.time();device=self.local_device_id()
        observations=[]
        for member in self.manifest(owner,reg['id'],check_access=False)['files']:
            try:actual=stable_hash(member['path'])
            except (OSError,ValueError):actual=None
            if actual is None or any(actual[k]!=member[k] for k in ('sha256','identity','mtime_ns')):
                observations.append((member,actual))
        with self.store.db(True) as db:
            row=db.execute('SELECT state,gap_epoch FROM registration_devices WHERE registration_id=? AND device_id=?',(reg['id'],device)).fetchone()
            if not row:return
            if row['state']!='unverified':
                epoch=row['gap_epoch']+1
                db.execute("UPDATE registration_devices SET gap_epoch=?,state='unverified' WHERE registration_id=? AND device_id=?",(epoch,reg['id'],device))
                db.execute('INSERT INTO monitoring_epochs VALUES(?,?,?,?,?,NULL)',(reg['id'],device,epoch,reason,now))
            db.execute("UPDATE registrations SET state='changed',error=?,updated=? WHERE id=? AND state!='cancelled'",(reason,now,reg['id']))
            db.execute("UPDATE file_instances SET state='unverified' WHERE registration_id=? AND kind='local'",(reg['id'],))
            for member,actual in observations:
                self.store._event(db,owner,'local.file.changed',device=device,file_id=member['file_id'],instance_id='local:'+member['file_id'],payload={
                    'registration_id':reg['id'],'old_version_id':member['version_id'],
                    'version_id':actual['sha256'] if actual else None,'observed':actual,
                    'source':'local_runtime','timing':'observed_after_event','actor':'unknown_external_actor'})
            self.store._event(db,owner,'local.version_invalidated',device=device,payload={
                'registration_id':reg['id'],'reason':reason,'source':'local_runtime',
                'timing':'observed_after_event','pid':os.getpid(),'observer_user':getpass.getuser(),
                'actor':'unknown_external_actor','observed':observed or {}})
        # Old decisions already fail closed before this asynchronous work is
        # scheduled. A completed recheck never refreshes an older saved task.
        self.schedule_local_recheck(owner,reg['id'])

    def validate_local_manifest(self,owner,reg):
        if reg['mode']!='local':raise ProtectionError('旧双端登记需改为本地来源重新登记',423,'local_source_required')
        if reg['state']!='ready':raise ProtectionError('本地版本未完成检查，请重新检查',423,'manifest_not_ready')
        members=self.manifest(owner,reg['id'])['files']
        try:self.compare_local_members(reg,members)
        except (OSError,ValueError,ProtectionError) as exc:
            self.invalidate_local(owner,reg,str(exc))
            raise ProtectionError('本地资料已经改变或暂不可读，旧准入已失效，请重新检查',423,'content_changed') from exc

    def prepare_local(self,owner,user_id,root):
        """Single local selection path; no server_path or second manual upload.

        Call only when beginning a NEW analysis. Existing jobs/caches must keep
        their original context, and must never use this to relabel old output.
        """
        if not self.enabled:return None
        try:
            reg=self.resolve(owner,root=root)
            self.validate_local_manifest(owner,reg)
            self.local_epoch(reg)
        except ProtectionError as exc:
            if exc.code=='registration_required':
                work=self.register(owner,user_id,root)
                identifier=work['job_id'];rid=work['registration_id']
            elif exc.code in {'content_changed','manifest_not_ready','monitoring_gap'}:
                rid=reg['id'];identifier=self.recheck(owner,rid)['id']
            else:raise
            job=self.process_job(identifier,worker_id='local-selection:'+uid())
            if not job or job['state']!='completed':
                raise ProtectionError('本地资料检查未完成：'+str((job or {}).get('error','')),423,'inspection_incomplete')
            reg=self._registration(owner,rid)
        return self.require_operation(owner,registration_id=reg['id'],operation='analyze')['context']

    def begin_local(self,owner,user_id,root):
        """Persist a selection without enumerating or inspecting its contents.

        A receipt belongs to one inventory job. Restored business work may only
        admit that receipt's committed revision, never an opportunistic new one.
        """
        policy=self._active();self._owner(owner,user_id)
        root=self._root(owner,root)
        with self.store.db() as db:
            row=db.execute('SELECT * FROM registrations WHERE owner=? AND root=?',(owner,root)).fetchone()
        if not row:
            try:work=self.register(owner,user_id,root)
            except ProtectionError as exc:
                if exc.code!='registration_exists':raise
                return self.begin_local(owner,user_id,root)
            reg=work['registration'];job=work['job']
        else:
            reg=dict(row)
            with self.store.db() as db:
                row=db.execute("SELECT id FROM protection_jobs WHERE owner=? AND registration_id=? AND kind='inventory' AND state IN ('queued','running') ORDER BY created DESC LIMIT 1",(owner,reg['id'])).fetchone()
                done=db.execute("SELECT id FROM protection_jobs WHERE owner=? AND registration_id=? AND kind='inventory' AND state='completed' ORDER BY created DESC LIMIT 1",(owner,reg['id'])).fetchone()
            if row:job=self.jobs.get(owner,row['id'])
            # New selections are checked in the worker even when the stored
            # package says ready. The request never hashes a large directory.
            elif reg['state']=='ready' and done:job=self.recheck(owner,reg['id'])
            else:job=self.recheck(owner,reg['id'])
        return {'owner':owner,'root':root,'registration_id':reg['id'],'job_id':job['id'],
                'policy_revision':policy['revision'],'selection_id':uid(),'selected_at':time.time()}

    def complete_local(self,owner,receipt,operation='analyze'):
        """Admit only the completed inventory referenced by this selection."""
        if not isinstance(receipt,dict) or receipt.get('owner')!=owner:
            raise ProtectionError('导入任务缺少本账号持久选择凭据',423,'protection_context_required')
        job=self.jobs.get(owner,receipt.get('job_id',''))
        if job['registration_id']!=receipt.get('registration_id') or job['kind']!='inventory':
            raise ProtectionError('导入检查任务与资料登记不一致',423,'context_target_mismatch')
        if job['state'] in {'queued','running'}:
            raise ProtectionError('安全检查正在排队或执行',423,'inspection_pending')
        if job['state']=='cancelled':raise ProtectionError('导入安全检查已取消',409,'import_cancelled')
        if job['state']!='completed':raise ProtectionError('安全检查未完成：'+job.get('error',''),423,'inspection_incomplete')
        reg=self._registration(owner,receipt['registration_id'])
        if reg['root']!=receipt.get('root') or reg['manifest_revision']!=job['result'].get('manifest_revision') or self.policy()['revision']!=receipt.get('policy_revision'):
            raise ProtectionError('恢复任务的选择版本或策略已变化，请重新导入',423,'context_stale')
        return self.require_operation(owner,registration_id=reg['id'],operation=operation)['context']

    def schedule_local_recheck(self,owner,registration_id):
        """One durable recheck per registration, including observer changes."""
        self._active()
        with self.store.db() as db:
            row=db.execute("SELECT id FROM protection_jobs WHERE owner=? AND registration_id=? AND kind='inventory' AND state IN ('queued','running') ORDER BY created DESC LIMIT 1",(owner,registration_id)).fetchone()
        return self.jobs.get(owner,row['id']) if row else self.recheck(owner,registration_id)

    @staticmethod
    def local_verification_receipt(reg,rd):
        """Identify the complete inspection that an assistant may attach to."""
        if not reg or not rd or reg['state']!='ready' or rd['state']!='verified':return None
        revision=reg['manifest_revision']
        if (rd['gap_epoch']!=rd['verified_epoch'] or rd['verified_revision']!=revision or
                rd['manifest_generation']!=revision):return None
        completed=rd['last_complete']
        if not isinstance(completed,(int,float)) or isinstance(completed,bool) or not math.isfinite(completed) or completed<=0:return None
        return [revision,rd['gap_epoch'],completed]

    def local_coverage(self,owner,registration_id):
        reg=self._registration(owner,registration_id,check_access=False)
        with self.store.db() as db:
            fresh=db.execute('SELECT manifest_revision,state FROM registrations WHERE id=?',(reg['id'],)).fetchone()
            rd=db.execute('SELECT * FROM registration_devices WHERE registration_id=? AND device_id=?',(reg['id'],self.local_device_id())).fetchone()
            heartbeat=db.execute("SELECT value FROM settings WHERE key='package-watch-heartbeat'").fetchone()
            agent=db.execute("SELECT value FROM settings WHERE key='local-agent-health'").fetchone()
            attached=db.execute('SELECT value FROM settings WHERE key=?',('local-agent-registration:'+reg['id'],)).fetchone()
        live=bool(heartbeat and time.time()-float(heartbeat[0])<90)
        report=json.loads(agent[0]) if agent else {};health=report.get('health',{})
        connected=report.get('connected') is True and time.time()-report.get('at',0)<60
        receipt=self.local_verification_receipt(fresh,rd)
        valid=receipt is not None
        linked=bool(valid and attached and attached[0]==encode(receipt))
        error=report.get('registration_errors',{}).get(reg['id'])
        directory=connected and linked and not error and health.get('directory_monitor_healthy') is True and valid
        access=directory and health.get('access_audit_requested') is True and health.get('access_audit_healthy') is True
        return {'device_id':self.local_device_id(),'source':'native_local_path',
                'read_boundary':'SJFX runtime on this computer','content_admission':'every covered operation',
                'directory_watch_healthy':bool(directory),'runtime_observer_live':live,'access_audit_healthy':bool(access),
                'gap_epoch':rd['gap_epoch'] if rd else None,'verified_epoch':rd['verified_epoch'] if rd else None,
                'third_party_preoperation_filter':False,'healthy':bool(directory and access),
                'assistant_registration_error':error,
                'reason':('本包助手关联未完成：'+error['code']) if error else '程序访问审计须由当前平台助手逐根证明；直接读取核验不证明操作系统审计完整'}
