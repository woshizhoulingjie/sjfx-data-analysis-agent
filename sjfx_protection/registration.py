"""Owner-scoped package registry and immutable version admission service.

HTTP handlers must supply the authenticated owner and a real account/root permission
callback. Background workers carry the returned explicit protection context.
"""
from __future__ import annotations
import base64
import hashlib
import json
import ntpath
import os
import secrets
import time
from pathlib import Path
from .store import ProtectionError,uid,encode,digest
from .policy import effective_policy,write_policy,mode_allowed
from .jobs import Jobs,JOBS_SCHEMA
from .inspection import create_snapshot,evidence,profile_key,effective_profile,refresh_runtime_engines
from .scanner import stable_hash
from .validation import local_path,identity,sha256
from .local_source import LocalSourceMixin, canonical_operation, approval_for

PACKAGE_SCHEMA='''
CREATE TABLE IF NOT EXISTS registrations (
 id TEXT PRIMARY KEY,owner TEXT NOT NULL,user_id TEXT NOT NULL,name TEXT NOT NULL,mode TEXT NOT NULL,
 root TEXT NOT NULL,policy_revision INTEGER NOT NULL,manifest_revision INTEGER NOT NULL DEFAULT 0,
 collection_id TEXT NOT NULL DEFAULT '',state TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',
 sensitivity TEXT NOT NULL DEFAULT 'normal',created REAL NOT NULL,updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS registration_root ON registrations(owner,root,updated);
CREATE TABLE IF NOT EXISTS registration_devices (
 registration_id TEXT NOT NULL,device_id TEXT NOT NULL,local_root TEXT NOT NULL,
 manifest_generation INTEGER NOT NULL DEFAULT 0,gap_epoch INTEGER NOT NULL DEFAULT 0,
 verified_epoch INTEGER NOT NULL DEFAULT -1,verified_revision INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'unverified',last_complete REAL NOT NULL DEFAULT 0,
 PRIMARY KEY(registration_id,device_id));
CREATE TABLE IF NOT EXISTS manifest_revisions (
 registration_id TEXT NOT NULL,revision INTEGER NOT NULL,digest TEXT NOT NULL,members TEXT NOT NULL,
 complete INTEGER NOT NULL,created REAL NOT NULL,PRIMARY KEY(registration_id,revision));
CREATE TABLE IF NOT EXISTS logical_files (
 id TEXT PRIMARY KEY,registration_id TEXT NOT NULL,relative_path TEXT NOT NULL,identity TEXT NOT NULL,
 current_version_id TEXT NOT NULL,available INTEGER NOT NULL DEFAULT 1,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS file_versions (
 id TEXT PRIMARY KEY,sha256 TEXT NOT NULL,size INTEGER NOT NULL,created REAL NOT NULL,UNIQUE(sha256,size));
CREATE TABLE IF NOT EXISTS file_instances (
 id TEXT PRIMARY KEY,logical_file_id TEXT NOT NULL,registration_id TEXT NOT NULL,kind TEXT NOT NULL,
 device_id TEXT NOT NULL,path TEXT NOT NULL,identity TEXT NOT NULL,version_id TEXT NOT NULL,
 state TEXT NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS version_snapshots (
 registration_id TEXT NOT NULL,file_id TEXT NOT NULL,version_id TEXT NOT NULL,path TEXT NOT NULL,
 created REAL NOT NULL,expires REAL NOT NULL,PRIMARY KEY(registration_id,file_id,version_id));
CREATE TABLE IF NOT EXISTS inspections (
 id TEXT PRIMARY KEY,version_id TEXT NOT NULL,cache_key TEXT NOT NULL,format_context TEXT NOT NULL,
 risk TEXT NOT NULL,coverage TEXT NOT NULL,engines TEXT NOT NULL,findings TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL);
CREATE INDEX IF NOT EXISTS inspections_version ON inspections(version_id,cache_key,expires);
CREATE TABLE IF NOT EXISTS local_manifest_tasks (
 id TEXT PRIMARY KEY,owner TEXT NOT NULL,registration_id TEXT NOT NULL,device_id TEXT NOT NULL,
 local_root TEXT NOT NULL,manifest_revision INTEGER NOT NULL,manifest_generation INTEGER NOT NULL,
 gap_epoch INTEGER NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,
 result TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS decisions (
 id TEXT PRIMARY KEY,owner TEXT NOT NULL,registration_id TEXT NOT NULL,operation TEXT NOT NULL,
 targets TEXT NOT NULL,policy_revision INTEGER NOT NULL,manifest_revision INTEGER NOT NULL,
 device_id TEXT NOT NULL,gap_epoch INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS approvals (
 id TEXT PRIMARY KEY,owner TEXT NOT NULL,registration_id TEXT NOT NULL,operation TEXT NOT NULL,
 target_digest TEXT NOT NULL,policy_revision INTEGER NOT NULL,manifest_revision INTEGER NOT NULL,
 reviewer TEXT NOT NULL,reason TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS protection_artifacts (
 id TEXT PRIMARY KEY,owner TEXT NOT NULL,path TEXT NOT NULL,sha256 TEXT NOT NULL,size INTEGER NOT NULL,
 sources TEXT NOT NULL,risk TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS artifact_snapshots (
 artifact_id TEXT PRIMARY KEY,path TEXT NOT NULL,version_id TEXT NOT NULL,cache_key TEXT NOT NULL,
 format_context TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS package_download_tickets (
 token_hash TEXT PRIMARY KEY,owner TEXT NOT NULL,artifact_id TEXT NOT NULL,sha256 TEXT NOT NULL,
 sources TEXT NOT NULL,policy_revision INTEGER NOT NULL,device_id TEXT NOT NULL,expires REAL NOT NULL,
 approval_id TEXT NOT NULL DEFAULT '',operation TEXT NOT NULL DEFAULT 'download');
CREATE TABLE IF NOT EXISTS monitoring_epochs (
 registration_id TEXT NOT NULL,device_id TEXT NOT NULL,epoch INTEGER NOT NULL,reason TEXT NOT NULL,
 created REAL NOT NULL,resolved REAL,PRIMARY KEY(registration_id,device_id,epoch));
CREATE TABLE IF NOT EXISTS protection_adapters (
 owner TEXT NOT NULL,kind TEXT NOT NULL,business_id TEXT NOT NULL,registration_id TEXT NOT NULL,
 PRIMARY KEY(owner,kind,business_id));
CREATE TABLE IF NOT EXISTS approval_requests (
 id TEXT PRIMARY KEY,owner TEXT NOT NULL,registration_id TEXT NOT NULL,operation TEXT NOT NULL,
 targets TEXT NOT NULL,policy_revision INTEGER NOT NULL,manifest_revision INTEGER NOT NULL,
 reason TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,
 approval_id TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS protection_alerts (
 id TEXT PRIMARY KEY,owner TEXT NOT NULL,registration_id TEXT NOT NULL,device_id TEXT NOT NULL,
 kind TEXT NOT NULL,reason TEXT NOT NULL,evidence TEXT NOT NULL,created REAL NOT NULL,resolved REAL);
CREATE TABLE IF NOT EXISTS file_labels (
 file_id TEXT PRIMARY KEY,owner TEXT NOT NULL,sensitivity TEXT NOT NULL,actor TEXT NOT NULL,
 reason TEXT NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS protection_schema_migrations (
 name TEXT PRIMARY KEY,version INTEGER NOT NULL,migrated_at REAL NOT NULL);
'''

def _within_local(path,root):
    if ntpath.isabs(root):
        try:return ntpath.commonpath([ntpath.normcase(path),ntpath.normcase(root)])==ntpath.normcase(root)
        except ValueError:return False
    try:return Path(path).is_relative_to(Path(root))
    except ValueError:return False

class PackageProtection(LocalSourceMixin):
    def __init__(self,store,allowed_roots,authorize_root=None,authorize_owner=None):
        self.store=store;self.allowed_roots=list(allowed_roots)
        self.authorize_root=authorize_root;self.authorize_owner=authorize_owner;self._schema_ready=False
        self.jobs=Jobs(store)
    def policy(self):return effective_policy(self.store.directory)
    @property
    def enabled(self):return self.policy()['enabled']
    def update_policy(self,actor,is_admin,changes,reason):
        value=write_policy(self.store.directory,actor,is_admin,changes,reason)
        with self.store.db(True) as db:db.execute('DELETE FROM risk_reviews')
        self.store.event(str(actor),'package.policy_changed',payload=value)
        return value
    def _ensure(self):
        if not self._schema_ready:
            with self.store.db() as db:
                db.executescript(PACKAGE_SCHEMA+JOBS_SCHEMA)
                columns={r['name'] for r in db.execute('PRAGMA table_info(package_download_tickets)')}
                for name,default in [('approval_id',''),('operation','download')]:
                    if name not in columns:db.execute('ALTER TABLE package_download_tickets ADD COLUMN '+name+" TEXT NOT NULL DEFAULT '"+default+"'")
                db.execute('INSERT OR IGNORE INTO protection_schema_migrations VALUES(?,?,?)',('package-protection-20261009',2,time.time()))
            self._schema_ready=True
    def _active(self):
        policy=self.policy()
        if not policy['enabled']:raise ProtectionError('数据包保护尚未启用',409,'protection_disabled')
        if policy.get('invalid'):raise ProtectionError('保护策略损坏，需要管理员修复',423,'policy_invalid')
        if not {'static','clamav'}.issubset(policy['inspection_profile'].get('required_engines',[])) and os.getenv('SJFX_PROTECTION_TEST_STATIC_ONLY')!='1':
            raise ProtectionError('当前测试检查配置不能作为正式完整准入',423,'inspection_incomplete')
        self._ensure();policy['inspection_profile']=effective_profile(self.store,policy['inspection_profile']);return policy
    def _configured(self):
        policy=self.policy()
        if policy.get('invalid'):raise ProtectionError('保护策略损坏，需要管理员修复',423,'policy_invalid')
        self._ensure();policy['inspection_profile']=effective_profile(self.store,policy['inspection_profile']);return policy
    def _root(self,owner,root):
        self.validate_native_path(root)
        candidate=Path(root).expanduser()
        if candidate.is_symlink():raise ProtectionError('符号链接不能作为核验目录或成员',423,'manifest_incomplete')
        root=candidate.resolve()
        state=self.store.directory.resolve()
        if root==state or root.is_relative_to(state) or state.is_relative_to(root):
            raise ProtectionError('资料范围必须与保护状态和快照目录分开',403,'state_source_overlap')
        if not any(root.is_relative_to(Path(item).expanduser().resolve()) for item in self.allowed_roots):
            raise ProtectionError('本地目录不在允许范围',403,'root_forbidden')
        if self.authorize_root is None:
            raise ProtectionError('尚未配置账号资料目录授权检查',403,'root_authorization_required')
        if self.authorize_root(owner,str(root)) is False:
            raise ProtectionError('账号无权访问该资料目录',403,'root_forbidden')
        return str(root)
    def _owner(self,owner,user_id):
        if self.authorize_owner is None:
            raise ProtectionError('尚未配置最新账号授权检查',403,'account_authorization_required')
        value=self.authorize_owner(owner,user_id)
        if value is False:
            raise ProtectionError('账号已停用或授权已撤销',403,'account_disabled')
        return value
    def _registration(self,owner,identifier,check_access=True):
        self._ensure()
        with self.store.db() as db:row=db.execute('SELECT * FROM registrations WHERE id=? AND owner=?',(identifier,owner)).fetchone()
        if not row:raise ProtectionError('数据包未登记或不属于当前账号',423,'registration_required')
        if check_access:self._owner(owner,row['user_id']);self._root(owner,row['root'])
        return dict(row)
    def resolve(self,owner,root=None,registration_id=None,kind=None,business_id=None):
        self._ensure()
        if registration_id:
            registration=self._registration(owner,registration_id)
            if root:
                actual=self.resolve(owner,root=root)
                if actual['id']!=registration_id:raise ProtectionError('操作实际目录与冻结数据包不一致',423,'context_target_mismatch')
                normalized=Path(self._root(owner,root))
                if normalized.is_file():
                    with self.store.db() as db:member=db.execute("SELECT 1 FROM file_instances WHERE registration_id=? AND kind='local' AND path=? AND state='available'",(registration_id,str(normalized))).fetchone()
                    if not member:raise ProtectionError('文件不属于当前数据包成员',423,'unregistered_file')
                elif not normalized.is_dir():raise ProtectionError('操作目标目录或文件不可访问',423,'root_unavailable')
            return registration
        with self.store.db() as db:
            if kind and business_id:
                row=db.execute('SELECT registration_id FROM protection_adapters WHERE owner=? AND kind=? AND business_id=?',(owner,kind,str(business_id))).fetchone()
                if row:return self._registration(owner,row[0])
            normalized=self._root(owner,root) if root else ''
            rows=db.execute('SELECT * FROM registrations WHERE owner=? ORDER BY updated DESC',(owner,)).fetchall()
        candidates=[r for r in rows if normalized==r['root'] or (normalized and Path(normalized).is_relative_to(Path(r['root'])))]
        if not candidates:raise ProtectionError('该资料须先登记数据包',423,'registration_required')
        # Deepest registered package wins; a passing parent never covers another registered child.
        row=max(candidates,key=lambda r:len(r['root']));registration=self._registration(owner,row['id'])
        normalized_path=Path(normalized)
        if normalized_path.is_file():
            with self.store.db() as db:member=db.execute("SELECT 1 FROM file_instances WHERE registration_id=? AND kind='local' AND path=? AND state='available'",(registration['id'],str(normalized_path))).fetchone()
            if not member:raise ProtectionError('文件不属于当前数据包成员',423,'unregistered_file')
        elif not normalized_path.is_dir():raise ProtectionError('操作目标目录或文件不可访问',423,'root_unavailable')
        return registration
    def bind_business(self,owner,registration_id,kind,business_id):
        self._active();self._registration(owner,registration_id)
        with self.store.db(True) as db:db.execute('INSERT OR REPLACE INTO protection_adapters VALUES(?,?,?,?)',(owner,kind,str(business_id),registration_id))
    def register(self,owner,user_id,root,name='',mode='local',device_id='',local_root='',sensitivity='normal'):
        policy=self._configured();self._owner(owner,user_id);root=self._root(owner,root)
        if mode!='local' or local_root:raise ProtectionError('只接受实际读取进程所在电脑的本地唯一路径',400,'local_source_required')
        device_id=self.local_device_id()
        if not mode_allowed(policy,owner,mode):raise ProtectionError('管理员策略不允许该登记模式',403,'registration_mode_denied')
        if not (Path(root).is_dir() or Path(root).is_file()) or Path(root).is_symlink():raise ProtectionError('本地文件或目录不存在或不能核验',400,'root_unavailable')
        planned_root=''
        if mode=='dual':
            if device_id:self._device(owner,device_id)
            if local_root:planned_root=local_path(local_root)
        identifier=uid();now=time.time()
        job_id=uid()
        with self.store.db(True) as db:
            if db.execute('SELECT 1 FROM registrations WHERE owner=? AND root=?',(owner,root)).fetchone():
                raise ProtectionError('该目录已有登记，请使用重新核验入口',409,'registration_exists')
            db.execute('INSERT INTO registrations(id,owner,user_id,name,mode,root,policy_revision,state,sensitivity,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (identifier,owner,str(user_id),str(name or Path(root).name)[:200],mode,root,policy['revision'],'queued',sensitivity,now,now))
            if mode=='local':
                db.execute("INSERT INTO registration_devices(registration_id,device_id,local_root,state) VALUES(?,?,?,'awaiting_selection')",(identifier,str(device_id),root))
            self.store._event(db,owner,'package.registered',payload={'registration_id':identifier,'mode':mode,'root':root,'user_id':user_id})
            db.execute('INSERT INTO protection_jobs(id,owner,registration_id,kind,payload,state,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                (job_id,owner,identifier,'inventory',encode({'policy_revision':policy['revision']}),'queued',now,now))
        job=self.jobs.get(owner,job_id)
        return {'registration':self._registration(owner,identifier),'job':job,'registration_id':identifier,'job_id':job['id']}
    def list(self,owner):
        if not self.enabled:
            with self.store.db() as db:
                if not self.store._has_table(db,'registrations'):return []
        else:self._ensure()
        with self.store.db() as db:rows=db.execute('SELECT * FROM registrations WHERE owner=? ORDER BY created DESC',(owner,)).fetchall()
        return [dict(row) for row in rows]
    def recheck(self,owner,registration_id,device_id=''):
        policy=self._configured();row=self._registration(owner,registration_id)
        with self.store.db(True) as db:
            active=db.execute("SELECT id FROM protection_jobs WHERE owner=? AND registration_id=? AND kind='inventory' AND state IN ('queued','running') ORDER BY created DESC LIMIT 1",(owner,registration_id)).fetchone()
            if active:identifier=active[0]
            else:
                identifier=uid();now=time.time()
                db.execute("UPDATE registrations SET state='queued',error='',updated=? WHERE id=?",(now,registration_id))
                db.execute("UPDATE registration_devices SET state='unverified' WHERE registration_id=? AND state!='awaiting_selection'",(registration_id,))
                db.execute("UPDATE actions SET state='stale' WHERE binding IN (SELECT id FROM file_instances WHERE registration_id=?) AND state IN ('pending','running')",(registration_id,))
                db.execute('INSERT INTO protection_jobs(id,owner,registration_id,kind,payload,state,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                    (identifier,owner,registration_id,'inventory',encode({'policy_revision':policy['revision'],'previous_revision':row['manifest_revision']}),'queued',now,now))
        return self.jobs.get(owner,identifier)
    def cancel(self,owner,registration_id):
        self._ensure();self._registration(owner,registration_id,check_access=False)
        with self.store.db() as db:ids=[r[0] for r in db.execute("SELECT id FROM protection_jobs WHERE owner=? AND registration_id=? AND state IN ('queued','running')",(owner,registration_id))]
        for identifier in ids:self.jobs.cancel(owner,identifier)
        with self.store.db(True) as db:db.execute("UPDATE registrations SET state='cancelled',updated=? WHERE id=?",(time.time(),registration_id))
        return {'cancelled':True,'jobs':ids}
    def manifest(self,owner,registration_id,check_access=True):
        row=self._registration(owner,registration_id,check_access=check_access)
        with self.store.db() as db:revision=db.execute('SELECT * FROM manifest_revisions WHERE registration_id=? AND revision=?',(registration_id,row['manifest_revision'])).fetchone()
        return {'registration':row,'manifest_revision':row['manifest_revision'],'complete':bool(revision and revision['complete']),
                'digest':revision['digest'] if revision else '', 'files':json.loads(revision['members']) if revision else []}
    def status(self,owner,registration_id,device_id=''):
        result=self.manifest(owner,registration_id,check_access=False)
        with self.store.db() as db:
            devices=[dict(r) for r in db.execute('SELECT * FROM registration_devices WHERE registration_id=?',(registration_id,))]
            jobs=[dict(r) for r in db.execute('SELECT * FROM protection_jobs WHERE registration_id=? ORDER BY created DESC LIMIT 20',(registration_id,))]
            local_tasks=[dict(r) for r in db.execute('SELECT * FROM local_manifest_tasks WHERE registration_id=? ORDER BY created DESC LIMIT 20',(registration_id,))]
            instances=[dict(r) for r in db.execute("SELECT * FROM file_instances WHERE registration_id=? AND kind='local'",(registration_id,))]
            alerts=[dict(r) for r in db.execute('SELECT * FROM protection_alerts WHERE registration_id=? ORDER BY created DESC LIMIT 20',(registration_id,))]
            watch=db.execute("SELECT value FROM settings WHERE key='package-watch-heartbeat'").fetchone()
            verification=db.execute('SELECT value FROM settings WHERE key=?',('verified:'+result['registration']['collection_id'],)).fetchone()
        for task in local_tasks:task['result']=json.loads(task['result'])
        for job in jobs:
            for key in ('payload','progress','result'):job[key]=json.loads(job[key])
        result.update(devices=devices,jobs=jobs,local_tasks=local_tasks,alerts=alerts,coverage='native_local_runtime',can_use=False,
            monitor={'last_service_heartbeat':float(watch[0]) if watch else None,'last_server_verification':float(verification[0]) if verification else None},
            queue_depth=sum(j['state'] in {'queued','running'} for j in jobs),oldest_queued_age=max([time.time()-j['created'] for j in jobs if j['state']=='queued'] or [0]))
        try:
            self.require_operation(owner,registration_id=registration_id,device_id=device_id,record=False)
            result['can_use']=True
        except ProtectionError as exc:result.update(blocking_code=exc.code,blocking_reason=str(exc))
        result['counts']={'total':len(result['files']),'dangerous':sum(f.get('risk')=='dangerous' for f in result['files']),
            'review':sum(f.get('risk')=='review' for f in result['files']), 'incomplete':sum(f.get('risk') in {'unchecked','incomplete','failed'} for f in result['files'])}
        for rd in devices:
            rows=[i for i in instances if i['device_id']==rd['device_id']]
            matched=sum(any(i['logical_file_id']==m['file_id'] and i['version_id']==m['version_id'] and i['state'] in {'available','watching','read_locked','delete_locked'} and _within_local(i['path'],rd['local_root']) for i in rows) for m in result['files'])
            changed=sum(any(i['logical_file_id']==m['file_id'] and i['version_id']!=m['version_id'] for i in rows) for m in result['files'])
            rd['counts']={'matched':matched,'content_different':changed,'missing_or_unverified':len(result['files'])-matched}
            recent=next((t for t in local_tasks if t['device_id']==rd['device_id']),None)
            if recent:rd['differences']=recent['result']
        result['local_coverage']=self.local_coverage(owner,registration_id)
        return result
    def _device(self,owner,device_id,healthy=False):
        if not device_id:raise ProtectionError('该双端包需要当前设备助手证明',423,'device_proof_required')
        with self.store.db() as db:row=db.execute('SELECT * FROM devices WHERE id=? AND owner=? AND revoked IS NULL',(device_id,owner)).fetchone()
        if not row:raise ProtectionError('设备未授权或已撤销',423,'device_revoked')
        if healthy and (time.time()-row['heartbeat']>60 or json.loads(row['health']).get('healthy') is not True):
            raise ProtectionError('本地助手离线或监视异常',423,'device_unhealthy')
        return dict(row)
    def start_local_manifest(self,owner,device_id,registration_id=None,local_root='',collection_id=None):
        self._active();self._device(owner,device_id)
        if registration_id:
            with self.store.db() as db:exists=db.execute('SELECT 1 FROM registrations WHERE id=? AND owner=?',(registration_id,owner)).fetchone()
            if not exists:collection_id=collection_id or registration_id;registration_id=None
        if not registration_id:
            with self.store.db() as db:row=db.execute('SELECT id FROM registrations WHERE collection_id=? AND owner=?',(collection_id,owner)).fetchone()
            if not row:raise ProtectionError('清单未关联数据包',423,'registration_required')
            registration_id=row[0]
        reg=self._registration(owner,registration_id);root=local_path(local_root)
        if reg['mode']!='dual' or reg['state']!='ready':raise ProtectionError('本地清单尚未完成或模式无需助手',409,'manifest_not_ready')
        now=time.time();identifier=uid()
        with self.store.db(True) as db:
            db.execute("DELETE FROM registration_devices WHERE registration_id=? AND device_id='' AND state='awaiting_selection'",(registration_id,))
            old=db.execute('SELECT * FROM registration_devices WHERE registration_id=? AND device_id=?',(registration_id,device_id)).fetchone()
            generation=(old['manifest_generation'] if old else 0)+1;epoch=old['gap_epoch'] if old else 0
            db.execute("INSERT INTO registration_devices(registration_id,device_id,local_root,manifest_generation,gap_epoch,state) VALUES(?,?,?,?,?,'unverified') ON CONFLICT(registration_id,device_id) DO UPDATE SET local_root=excluded.local_root,manifest_generation=excluded.manifest_generation,state='unverified'",(registration_id,device_id,root,generation,epoch))
            db.execute("UPDATE local_manifest_tasks SET state='stale' WHERE registration_id=? AND device_id=? AND state='pending'",(registration_id,device_id))
            db.execute('INSERT INTO local_manifest_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(identifier,owner,registration_id,device_id,root,reg['manifest_revision'],generation,epoch,'pending',now,now+3600,'{}'))
        return self.local_manifest_page(owner,device_id,identifier)
    def local_manifest_page(self,owner,device_id,task_id,offset=0,limit=500):
        self._active();self._device(owner,device_id)
        with self.store.db() as db:task=db.execute('SELECT * FROM local_manifest_tasks WHERE id=? AND owner=? AND device_id=?',(task_id,owner,device_id)).fetchone()
        if not task or task['state']!='pending' or task['expires']<time.time():raise ProtectionError('完整核验任务已失效',409,'manifest_task_stale')
        manifest=self.manifest(owner,task['registration_id']);members=manifest['files']
        return {**dict(task),'task_id':task['id'],'collection_id':manifest['registration']['collection_id'],
                'registration_revision':task['manifest_revision'],
                'files':members[max(0,int(offset)):max(0,int(offset))+min(500,max(1,int(limit)))], 'total':len(members),'offset':max(0,int(offset))}
    def local_manifest_task(self,owner,device_id,data):
        if data.get('task_id'):
            return self.local_manifest_page(owner,device_id,data['task_id'],data.get('offset',0),data.get('limit',500))
        if int(data.get('offset',0))!=0:raise ProtectionError('首次完整核验请求须从零开始')
        return self.start_local_manifest(owner,device_id,data.get('registration_id'),data.get('local_root',''),data.get('collection_id'))
    def registration_tasks(self,owner,device_id):
        if not self.enabled:return []
        self._ensure();self._device(owner,device_id)
        with self.store.db() as db:rows=db.execute("SELECT *,id AS task_id FROM local_manifest_tasks WHERE owner=? AND device_id=? AND state='pending' AND expires>?",(owner,device_id,time.time())).fetchall()
        return [dict(r) for r in rows]
    def submit_manifest(self,owner,device_id,report):
        try:return self._submit_manifest(owner,device_id,report)
        except ProtectionError as exc:
            if isinstance(report,dict) and report.get('task_id'):
                self._ensure()
                result={'complete':False,'blocking_code':exc.code,'blocking_reason':str(exc),
                    'reported_member_count':len(report.get('files',[])) if isinstance(report.get('files'),list) else 0}
                for key in ('errors','unmatched','extra','candidates'):
                    if isinstance(report.get(key),list):result[key]=report[key][:500]
                with self.store.db(True) as db:db.execute('UPDATE local_manifest_tasks SET result=? WHERE id=? AND owner=? AND device_id=? AND state=\'pending\'',(encode(result),report['task_id'],owner,device_id))
            raise
    def _submit_manifest(self,owner,device_id,report):
        self._active();self._device(owner,device_id)
        if not isinstance(report,dict):raise ProtectionError('完整清单报告必须为对象')
        task_id=report.get('task_id');now=time.time()
        with self.store.db(True) as db:
            task=db.execute('SELECT * FROM local_manifest_tasks WHERE id=? AND owner=? AND device_id=?',(task_id,owner,device_id)).fetchone()
            if not task or task['state']!='pending' or task['expires']<now:raise ProtectionError('完整核验任务已过期',409,'manifest_task_stale')
            reg=db.execute('SELECT * FROM registrations WHERE id=? AND owner=?',(task['registration_id'],owner)).fetchone()
            device=db.execute('SELECT * FROM registration_devices WHERE registration_id=? AND device_id=?',(task['registration_id'],device_id)).fetchone()
            for key in ('registration_id','manifest_revision','manifest_generation','gap_epoch','local_root'):
                if report.get(key)!=task[key]:raise ProtectionError('签名清单任务或代次不一致',409,'manifest_task_stale')
            if report.get('device_id',device_id)!=device_id or reg['manifest_revision']!=task['manifest_revision'] or device['gap_epoch']!=task['gap_epoch'] or device['manifest_generation']!=task['manifest_generation'] or reg['state']!='ready':
                raise ProtectionError('核验期间内容或监控代次已变化',409,'manifest_task_stale')
            revision=db.execute('SELECT * FROM manifest_revisions WHERE registration_id=? AND revision=?',(reg['id'],reg['manifest_revision'])).fetchone()
            members=json.loads(revision['members']);expected={m['file_id']:m for m in members};records=report.get('files')
            if report.get('complete') is not True or report.get('errors') or not isinstance(records,list):
                db.execute('UPDATE local_manifest_tasks SET result=? WHERE id=?',(encode(report),task_id))
                raise ProtectionError('本地核验未完成，不能放行',423,'binding_incomplete')
            if len(records)>self.policy()['inspection_profile'].get('inventory_max_files',200000)*2:raise ProtectionError('报告超过实例数量预算',413,'manifest_limit')
            mapped={};instance_keys=set()
            for rec in records:
                member=expected.get(rec.get('file_id'))
                if not member:raise ProtectionError('报告包含非当前本地成员',409,'manifest_member_invalid')
                path=local_path(rec.get('local_path'));fsid=identity(rec.get('local_identity'));content=sha256(rec.get('sha256'))
                if not _within_local(path,task['local_root']) or content!=member['sha256'] or rec.get('size')!=member['size']:
                    raise ProtectionError('本地根范围、大小或内容不对应',409,'binding_incomplete')
                instance_key=(path,fsid)
                if instance_key in instance_keys:raise ProtectionError('同一本地实例不能充当多个成员',409,'ambiguous_mapping')
                instance_keys.add(instance_key);mapped.setdefault(member['file_id'],[]).append((path,fsid,member))
            if set(mapped)!=set(expected) or not members:raise ProtectionError('本地成员未全部对应',423,'binding_incomplete')
            db.execute("UPDATE file_instances SET state='unverified' WHERE registration_id=? AND device_id=? AND kind='local'",(reg['id'],device_id))
            for file_id,items in mapped.items():
                for path,fsid,member in items:
                    prior=db.execute('SELECT id FROM bindings WHERE file_id=? AND device=? AND local_path=?',(file_id,device_id,path)).fetchone()
                    bid=prior[0] if prior else uid()
                    db.execute("INSERT INTO bindings VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(device,file_id,local_path) DO UPDATE SET local_identity=excluded.local_identity,sha256=excluded.sha256,state='watching',updated=excluded.updated",(bid,file_id,device_id,owner,path,fsid,member['sha256'],'watching',now))
                    db.execute('INSERT OR REPLACE INTO file_instances VALUES(?,?,?,?,?,?,?,?,?,?)',(bid,file_id,reg['id'],'local',device_id,path,fsid,member['version_id'],'watching',now))
                    self.store._event(db,owner,'file.bound',device=device_id,file_id=file_id,instance_id=bid,payload={'task_id':task_id,'sha256':member['sha256'],'basis':'full_signed_manifest'})
                    self.store._queue_known_threat(db,bid)
            db.execute("UPDATE registration_devices SET verified_epoch=?,verified_revision=?,state='verified',last_complete=? WHERE registration_id=? AND device_id=?",(task['gap_epoch'],task['manifest_revision'],now,reg['id'],device_id))
            db.execute('UPDATE monitoring_epochs SET resolved=? WHERE registration_id=? AND device_id=? AND epoch<=?',(now,reg['id'],device_id,task['gap_epoch']))
            db.execute("UPDATE local_manifest_tasks SET state='completed',result=? WHERE id=?",(encode({'members':len(mapped),'extra':report.get('extra',[]),'candidates':report.get('candidates',[])}),task_id))
            self.store._event(db,owner,'package.local_verified',device=device_id,payload={'registration_id':reg['id'],'task_id':task_id,'manifest_revision':task['manifest_revision'],'gap_epoch':task['gap_epoch']})
        with self.store.db() as db:
            bindings=[dict(r) for r in db.execute('SELECT b.*,i.version_id FROM bindings b JOIN file_instances i ON i.id=b.id WHERE i.registration_id=? AND b.device=? AND i.state=\'watching\'',(task['registration_id'],device_id))]
        return {'complete':True,'bindings':[{**b,'binding_id':b['id'],'registration_id':task['registration_id'],
            'registration_revision':task['manifest_revision'],'manifest_revision':task['manifest_revision'],
            'manifest_generation':task['manifest_generation'],'gap_epoch':task['gap_epoch'],
            'device_id':device_id,'local_root':task['local_root']} for b in bindings],
            'status':self.status(owner,task['registration_id'],device_id)}

    def require_operation(self,owner,operation='read',root=None,registration_id=None,device_id='',versions=None,context=None,approval_id=None,record=True):
        # No table migrations, records, watch activity or device checks on the OFF path.
        operation=canonical_operation(operation)
        policy=self.policy()
        if not policy['enabled']:return {'state':'allow','bypass':True,'context':None,'enabled':False}
        policy=self._active()
        if context:
            if not isinstance(context,dict) or context.get('owner')!=owner:raise ProtectionError('后台保护上下文账号不一致',403,'context_invalid')
            registration_id=registration_id or context.get('registration_id');device_id=device_id or context.get('device_id','')
        reg=self.resolve(owner,root,registration_id)
        approval_id=approval_for(approval_id,reg['id'])
        if reg['mode']=='local':
            self.validate_local_manifest(owner,reg)
            device_id=self.local_device_id()
        if not mode_allowed(policy,owner,reg['mode']):raise ProtectionError('登记模式已不被当前策略允许',423,'registration_mode_denied')
        if reg['state']!='ready':raise ProtectionError('本地清单未完成或已变化，请重新核验',423,'manifest_not_ready')
        manifest=self.manifest(owner,reg['id']);members=manifest['files']
        if not manifest['complete'] or not members:raise ProtectionError('本地成员清单不完整',423,'manifest_incomplete')
        with self.store.db() as db:
            verification=db.execute('SELECT value FROM settings WHERE key=?',('verified:'+reg['collection_id'],)).fetchone()
            alert=db.execute('SELECT 1 FROM protection_alerts WHERE registration_id=? AND resolved IS NULL',(reg['id'],)).fetchone()
        if alert:raise ProtectionError('发现异常批量操作，数据包已暂停，需完整核验',423,'anomaly_paused')
        if reg['mode']!='local' and (not verification or time.time()-float(verification[0])>max(180,policy['inspection_profile'].get('watch_interval_seconds',60)*3)):
            raise ProtectionError('本地监控核验过期或服务异常',423,'server_monitor_unhealthy')
        targets=[{'file_id':m['file_id'],'version_id':m['version_id'],'sha256':m['sha256']} for m in members]
        epoch=self.local_epoch(reg) if reg['mode']=='local' else 0
        if context:
            if context.get('manifest_revision')!=reg['manifest_revision'] or context.get('policy_revision')!=policy['revision'] or context.get('gap_epoch',0)!=epoch or context.get('versions')!=targets:
                raise ProtectionError('排队任务的版本、策略或监控代次已经变化',423,'context_stale')
        if versions is not None:
            supplied=versions if isinstance(versions,list) else [versions]
            allowed_ids={t['version_id'] for t in targets}
            allowed_hashes={t['sha256'] for t in targets}
            for version in supplied:
                if isinstance(version,dict):
                    if not set(version)&{'file_id','version_id','sha256'}:raise ProtectionError('操作目标版本标识缺失',423,'target_stale')
                    if not any(all(t.get(k)==v for k,v in version.items() if k in {'file_id','version_id','sha256'}) for t in targets):
                        raise ProtectionError('操作目标版本不属于当前清单',423,'target_stale')
                elif version not in allowed_ids|allowed_hashes:raise ProtectionError('操作目标版本不属于当前清单',423,'target_stale')
        now=time.time()
        with self.store.db() as db:
            for member in members:
                inspection=db.execute('SELECT * FROM inspections WHERE version_id=? AND cache_key=? AND expires>? ORDER BY created DESC LIMIT 1',
                    (member['version_id'],profile_key(policy['inspection_profile'],member['path']),now)).fetchone()
                if not inspection:raise ProtectionError('当前版本的必要检查缺失或已过期',423,'inspection_incomplete')
                coverage=json.loads(inspection['coverage'])
                if inspection['risk']=='dangerous':raise ProtectionError('数据包包含已确认危险内容',423,'dangerous_content')
                if coverage.get('complete') is not True or inspection['risk'] in {'unchecked','incomplete','failed'}:
                    raise ProtectionError('必要检查未完成，不能用复核替代未知覆盖',423,'inspection_incomplete')
                if inspection['risk']=='review':
                    review=db.execute('SELECT 1 FROM risk_reviews WHERE file_id=? AND owner=? AND sha256=?',(member['file_id'],owner,member['sha256'])).fetchone()
                    if not review:raise ProtectionError('当前内容版本需要风险复核',423,'risk_review_required')
                snapshot=db.execute('SELECT * FROM version_snapshots WHERE registration_id=? AND file_id=? AND version_id=? AND expires>?',(reg['id'],member['file_id'],member['version_id'],now)).fetchone()
                if not snapshot or not Path(snapshot['path']).is_file():raise ProtectionError('核准快照缺失或已过期，请重新检查',423,'snapshot_missing')
        rule=policy['operation_policy'].get(operation,'allow')
        with self.store.db() as db:label=db.execute("SELECT 1 FROM file_labels l JOIN logical_files f ON f.id=l.file_id WHERE f.registration_id=? AND l.sensitivity!='normal'",(reg['id'],)).fetchone()
        if operation in {'export','download'} and (reg['sensitivity']!='normal' or label):rule=policy['operation_policy'].get('sensitive_export','approval')
        authorization=self._owner(owner,reg['user_id']);role=authorization.get('role','user') if isinstance(authorization,dict) else 'user'
        role_key='sensitive_export' if operation in {'export','download'} and (reg['sensitivity']!='normal' or label) else operation
        rule=policy['operation_policy'].get('role_rules',{}).get(role_key,{}).get(role,rule)
        decision_state='allow';reason='登记、当前版本检查和操作规则通过'
        if rule=='warn':reason='操作获准，管理员规则要求记录告警'
        if rule=='deny':decision_state='deny';reason='管理员操作规则禁止该操作'
        if rule=='approval':
            with self.store.db() as db:
                approval=db.execute('SELECT 1 FROM approvals WHERE id=? AND owner=? AND registration_id=? AND operation=? AND target_digest=? AND policy_revision=? AND manifest_revision=? AND expires>?',
                    (approval_id or '',owner,reg['id'],operation,digest(encode(targets)),policy['revision'],reg['manifest_revision'],now)).fetchone()
            if not approval:decision_state='approval';reason='该操作需要管理员对当前目标版本集合审批'
        identifier=uid();expires=now+300
        frozen={'owner':owner,'user_id':reg['user_id'],'registration_id':reg['id'],'manifest_revision':reg['manifest_revision'],
                'policy_revision':policy['revision'],'device_id':device_id,
                'gap_epoch':epoch,'versions':targets,'decision_id':identifier,'operation':operation,'approval_id':approval_id or ''}
        # No concurrent inventory/policy update may combine a prior registration
        # revision with a later manifest's bytes or approvals.
        with self.store.db() as db:
            head=db.execute('SELECT state,manifest_revision FROM registrations WHERE id=?',(reg['id'],)).fetchone()
        if not head or head['state']!='ready' or head['manifest_revision']!=reg['manifest_revision'] or self.policy()['revision']!=policy['revision'] or self.local_epoch(reg)!=epoch:
            raise ProtectionError('准入期间版本、策略或覆盖代次发生变化',423,'context_stale')
        if record:
            with self.store.db(True) as db:
                db.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (identifier,owner,reg['id'],operation,encode(targets),policy['revision'],reg['manifest_revision'],frozen['device_id'],epoch,decision_state,reason,now,expires))
                self.store._event(db,owner,'operation.allowed' if decision_state=='allow' else 'operation.blocked',device=frozen['device_id'],payload={'registration_id':reg['id'],'operation':operation,'decision_id':identifier,'target_digest':digest(encode(targets)),'scope':'package','reason':reason})
                if rule=='warn':self.store._event(db,owner,'operation.warning',device=frozen['device_id'],payload={'registration_id':reg['id'],'operation':operation,'decision_id':identifier,'target_digest':digest(encode(targets)),'role':role,'reason':reason,'scope':'package'})
        if decision_state!='allow':raise ProtectionError(reason,423,'operation_approval_required' if decision_state=='approval' else 'operation_denied')
        return {'state':'allow','id':identifier,'registration_id':reg['id'],'context':frozen,'versions':targets,'expires':expires,'mode':reg['mode']}
    def worker_context(self,owner,**kwargs):return self.require_operation(owner,**kwargs)['context']
    def _approve_targets(self,owner,registration_id,operation,reviewer,reason,is_admin=False,ttl=600,versions=None):
        operation=canonical_operation(operation)
        policy=self._active()
        if not is_admin:raise ProtectionError('操作审批需要管理员',403,'admin_required')
        if not str(reason).strip():raise ProtectionError('审批必须提供理由')
        reg=self._registration(owner,registration_id);members=self.manifest(owner,registration_id)['files']
        targets=[{'file_id':m['file_id'],'version_id':m['version_id'],'sha256':m['sha256']} for m in members]
        if versions is not None and versions!=targets:raise ProtectionError('审批目标已变化',409,'target_stale')
        identifier=uid();now=time.time()
        with self.store.db(True) as db:
            db.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,?,?)',(identifier,owner,registration_id,operation,digest(encode(targets)),policy['revision'],reg['manifest_revision'],str(reviewer),str(reason)[:2000],now,now+min(max(1,ttl),3600)))
            self.store._event(db,owner,'operation.approved',payload={'approval_id':identifier,'operation':operation,'registration_id':registration_id,'target_digest':digest(encode(targets)),'reviewer':reviewer,'reason':reason})
        return {'id':identifier,'approval_id':identifier,'expires':now+min(max(1,ttl),3600),'versions':targets}
    def request_approval(self,owner,registration_id,operation,reason,context=None):
        operation=canonical_operation(operation)
        policy=self._configured();reg=self._registration(owner,registration_id)
        self.validate_local_manifest(owner,reg)
        if not str(reason).strip():raise ProtectionError('申请审批需填写理由')
        targets=[{'file_id':m['file_id'],'version_id':m['version_id'],'sha256':m['sha256']} for m in self.manifest(owner,registration_id)['files']]
        if not targets or (context and (context.get('manifest_revision')!=reg['manifest_revision'] or context.get('versions')!=targets)):
            raise ProtectionError('审批申请目标未完成或已变化',423,'target_stale')
        identifier=uid();now=time.time()
        with self.store.db(True) as db:
            db.execute('INSERT INTO approval_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(identifier,owner,registration_id,operation,encode(targets),policy['revision'],reg['manifest_revision'],str(reason)[:2000],'pending',now,now+3600,''))
            self.store._event(db,owner,'operation.approval_requested',payload={'request_id':identifier,'registration_id':registration_id,'operation':operation,'reason':reason,'target_digest':digest(encode(targets))})
        return {'id':identifier,'approval_request_id':identifier,'state':'pending','targets':targets}
    def approve(self,owner,approval_id,actor,approved,reason,is_admin=False):
        policy=self._configured()
        if not is_admin:raise ProtectionError('操作审批需要管理员',403,'admin_required')
        if not isinstance(approved,bool) or not str(reason).strip():raise ProtectionError('审批决定需布尔结果与理由')
        with self.store.db() as db:request=db.execute("SELECT * FROM approval_requests WHERE id=? AND owner=? AND state='pending' AND expires>?",(approval_id,owner,time.time())).fetchone()
        if not request:raise ProtectionError('审批申请已失效',409,'approval_stale')
        reg=self._registration(owner,request['registration_id'])
        if request['policy_revision']!=policy['revision'] or request['manifest_revision']!=reg['manifest_revision']:
            raise ProtectionError('审批依据的策略或清单已变化',409,'approval_stale')
        result=self._approve_targets(owner,reg['id'],request['operation'],actor,reason,True,versions=json.loads(request['targets'])) if approved else {'state':'rejected','approval_id':''}
        with self.store.db(True) as db:
            db.execute('UPDATE approval_requests SET state=?,approval_id=? WHERE id=?',('approved' if approved else 'rejected',result['approval_id'],approval_id))
        return result
    def approvals(self,owner):
        if not self.enabled:
            with self.store.db() as db:
                if not self.store._has_table(db,'approval_requests'):return []
        else:self._ensure()
        with self.store.db() as db:return [dict(r) for r in db.execute('SELECT * FROM approval_requests WHERE owner=? ORDER BY created DESC LIMIT 200',(owner,))]
    def change_mode(self,owner,registration_id,mode,reviewer,reason,is_admin=False):
        policy=self._active()
        if not is_admin or not str(reason).strip():raise ProtectionError('模式变更需管理员审批并填写理由',403,'admin_required')
        self._registration(owner,registration_id)
        if not mode_allowed(policy,owner,mode):raise ProtectionError('该模式不被策略允许',403,'registration_mode_denied')
        with self.store.db(True) as db:
            db.execute("UPDATE registrations SET mode=?,policy_revision=?,state='queued',updated=? WHERE id=?",(mode,policy['revision'],time.time(),registration_id))
            self.store._event(db,owner,'package.mode_changed',payload={'registration_id':registration_id,'mode':mode,'reviewer':reviewer,'reason':reason})
        return self.recheck(owner,registration_id)
    def verified_snapshot(self,owner,registration_id,file_id,context=None):
        if not self.enabled:raise ProtectionError('OFF 路径应继续使用原业务文件',409,'protection_disabled')
        decision=self.require_operation(owner,registration_id=registration_id,context=context,operation=(context or {}).get('operation','read'),approval_id=(context or {}).get('approval_id'),record=False)
        # Use the exact admitted version, never a second read of current manifest.
        member=next((m for m in decision['versions'] if m['file_id']==file_id),None)
        if not member:raise ProtectionError('文件不属于当前清单',423,'target_stale')
        with self.store.db() as db:row=db.execute('SELECT * FROM version_snapshots WHERE registration_id=? AND file_id=? AND version_id=?',(registration_id,member['file_id'],member['version_id'])).fetchone()
        if not row or row['expires']<time.time():raise ProtectionError('受限快照已失效',423,'snapshot_missing')
        current=stable_hash(row['path'])
        if current['sha256']!=member['sha256']:raise ProtectionError('受限快照内容已变化',423,'snapshot_changed')
        self.require_operation(owner,registration_id=registration_id,context=decision['context'],operation=decision['context']['operation'],approval_id=decision['context'].get('approval_id'),record=False)
        return row['path']
    def snapshot_root(self,owner,registration_id,context=None):
        admission=self.require_operation(owner,registration_id=registration_id,context=context,
            operation=(context or {}).get('operation','read'),approval_id=(context or {}).get('approval_id'),record=False)
        frozen=admission.get('context')
        if not frozen:raise ProtectionError('快照树的准入已失效',423,'context_stale')
        from .capacity import assert_managed_path,reserve
        root=assert_managed_path(self.store,self.store.directory/'snapshot-trees'/registration_id/str(frozen['manifest_revision']))
        manifest=self.manifest(owner,registration_id)
        if manifest['manifest_revision']!=frozen['manifest_revision'] or [{'file_id':m['file_id'],'version_id':m['version_id'],'sha256':m['sha256']} for m in manifest['files']]!=admission['versions']:
            raise ProtectionError('建立快照树前清单已变化',423,'context_stale')
        for member in manifest['files']:
            source=Path(self.verified_snapshot(owner,registration_id,member['file_id'],frozen));relative=Path(member['relative_path'])
            if relative.is_absolute() or '..' in relative.parts:raise ProtectionError('清单相对路径无效',423,'target_stale')
            target=assert_managed_path(self.store,root/relative);target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            if target.exists():
                if stable_hash(target)['sha256']!=member['sha256']:raise ProtectionError('快照树被篡改',423,'snapshot_changed')
            else:
                import shutil
                with reserve(self.store,target,source.stat().st_size,self.policy()['inspection_profile']):
                    assert_managed_path(self.store,target)
                    try:shutil.copyfile(source,target)
                    except Exception:
                        assert_managed_path(self.store,target);target.unlink(missing_ok=True)
                        raise
                if os.name!='nt':os.chmod(target,0o400)
        self._recheck_admission(owner,admission)
        return str(root)
    def _recheck_admission(self,owner,admission):
        frozen=admission['context']
        current=self.require_operation(owner,registration_id=frozen['registration_id'],context=frozen,
            operation=frozen['operation'],approval_id=frozen.get('approval_id'),record=False)
        if not current.get('context'):raise ProtectionError('操作期间保护准入已失效',423,'context_stale')
        return current
    def stage_version(self,owner,registration_id,binding_id,raw,expected_sha256,device_id=''):
        raise ProtectionError('本地唯一路径不接受第二份暂存上传，请重新检查原本地路径',410,'legacy_transfer_removed')
    def _version(self,content,size):
        with self.store.db(True) as db:
            row=db.execute('SELECT id,size FROM file_versions WHERE sha256=?',(content,)).fetchone()
            if row:
                if row['size']!=size:db.execute('UPDATE file_versions SET size=? WHERE id=?',(size,row['id']))
                return row[0]
            identifier=content;db.execute('INSERT INTO file_versions VALUES(?,?,?,?)',(identifier,content,size,time.time()));return identifier
    def inspect_derived(self,owner,registration_id,parent_version_id,member_name,raw=None,path=None,context=None):
        policy=self._active();admission=self.require_operation(owner,registration_id=registration_id,context=context,record=False)
        parents={m['version_id'] for m in self.manifest(owner,registration_id)['files']}
        if parent_version_id not in parents:
            with self.store.db() as db:
                rows=db.execute('SELECT id,sources FROM protection_artifacts WHERE owner=? AND risk=\'no_detection\'',(owner,)).fetchall()
            parent_verified=False
            for row in rows:
                if not any(s.get('registration_id')==registration_id and s.get('derived_version_id')==parent_version_id for s in json.loads(row['sources'])):continue
                try:self.require_artifact(owner,row['id'],operation='read')
                except ProtectionError:continue
                parent_verified=True;break
            if not parent_verified:
                raise ProtectionError('派生内容缺少已核准父版本',423,'provenance_missing')
        if raw is not None:
            if not isinstance(raw,bytes) or len(raw)>policy['inspection_profile']['static_max_bytes']:
                raise ProtectionError('派生成员字节超过检查容量',423,'inspection_incomplete')
            from .capacity import assert_managed_path,reserve
            directory=assert_managed_path(self.store,self.store.directory/'derived');directory.mkdir(parents=True,exist_ok=True,mode=0o700)
            source=directory/(uid()+Path(member_name).suffix)
            with reserve(self.store,source,len(raw),policy['inspection_profile']):
                assert_managed_path(self.store,source);source.write_bytes(raw)
        elif path:source=Path(path)
        else:raise ProtectionError('派生成员没有内容')
        try:info,snapshot=create_snapshot(self.store,source,policy['inspection_profile'])
        finally:
            if raw is not None:assert_managed_path(self.store,source);source.unlink(missing_ok=True)
        version=self._version(info['sha256'],info['size']);result=evidence(self.store,snapshot,member_name,version,policy['inspection_profile'])
        if result['risk']!='no_detection' or json.loads(result['coverage']).get('complete') is not True:
            raise ProtectionError('派生成员尚未通过必要检查',423,'derived_inspection_required')
        self._recheck_admission(owner,admission)
        provenance=[{'registration_id':registration_id,'parent_version_id':parent_version_id,'member_name':member_name,
                     'derived_version_id':version,'sha256':info['sha256'],'context':admission['context'],'versions':admission['versions']}]
        identifier=uid()
        with self.store.db(True) as db:db.execute('INSERT INTO protection_artifacts VALUES(?,?,?,?,?,?,?,?)',(identifier,owner,snapshot,info['sha256'],info['size'],encode(provenance),'no_detection',time.time()))
        with self.store.db(True) as db:db.execute('INSERT INTO artifact_snapshots VALUES(?,?,?,?,?,?,?)',(identifier,snapshot,version,profile_key(policy['inspection_profile'],member_name),member_name,time.time(),time.time()+policy['inspection_profile']['snapshot_retention_seconds']))
        self._recheck_admission(owner,admission)
        return {'artifact_id':identifier,'version_id':version,'sha256':info['sha256'],'path':snapshot,'snapshot':snapshot,'sources':provenance}
    def record_artifact(self,owner,path,sources,risk='pending'):
        self._active()
        if not sources:raise ProtectionError('产物缺少来源版本集合',423,'provenance_missing')
        for source in sources:self.require_operation(owner,registration_id=source['registration_id'],context=source.get('context'),versions=source.get('versions'),record=False)
        info=stable_hash(path);identifier=uid()
        with self.store.db(True) as db:db.execute('INSERT INTO protection_artifacts VALUES(?,?,?,?,?,?,?,?)',(identifier,owner,str(Path(path).resolve()),info['sha256'],info['size'],encode(sources),risk,time.time()))
        self._inspect_artifact(owner,identifier,self._active())
        self.store.event(owner,'artifact.created',payload={'artifact_id':identifier,'sources':sources,'sha256':info['sha256'],'scope':'artifact'})
        return self.provenance(owner,identifier)
    def provenance(self,owner,artifact_id):
        self._ensure()
        with self.store.db() as db:row=db.execute('SELECT * FROM protection_artifacts WHERE id=? AND owner=?',(artifact_id,owner)).fetchone()
        if not row:raise ProtectionError('历史产物缺少保护来源，请补核验',423,'provenance_missing')
        result=dict(row);result['sources']=json.loads(result['sources']);return result
    def require_artifact(self,owner,artifact_id,device_id='',operation='download',approval_id=None):
        policy=self._active();artifact=self.provenance(owner,artifact_id)
        for source in artifact['sources']:
            self.require_operation(owner,registration_id=source['registration_id'],operation=operation,device_id=device_id,
                versions=source.get('versions'),context=source.get('context'),approval_id=approval_id,record=False)
        info=stable_hash(artifact['path'])
        if info['sha256']!=artifact['sha256']:raise ProtectionError('产物内容已变化',423,'artifact_changed')
        with self.store.db() as db:
            frozen=db.execute('SELECT * FROM artifact_snapshots WHERE artifact_id=? AND expires>?',(artifact_id,time.time())).fetchone()
            if frozen:
                inspection=db.execute('SELECT * FROM inspections WHERE version_id=? AND cache_key=? AND expires>? ORDER BY created DESC LIMIT 1',(frozen['version_id'],profile_key(policy['inspection_profile'],frozen['format_context']),time.time())).fetchone()
            else:inspection=None
        if not inspection or inspection['risk']!='no_detection' or json.loads(inspection['coverage']).get('complete') is not True:
            raise ProtectionError('派生产物未通过内容检查',423,'artifact_inspection_required')
        snapshot=frozen['path'];frozen_info=stable_hash(snapshot)
        if frozen_info['sha256']!=artifact['sha256'] or frozen_info['size']!=artifact['size']:
            raise ProtectionError('核准产物快照已变化',423,'snapshot_changed')
        return {**artifact,'source_path':artifact['path'],'path':snapshot,'snapshot':snapshot,'approved_download_path':snapshot,'snapshot_path':snapshot}
    def _inspect_artifact(self,owner,artifact_id,policy):
        artifact=self.provenance(owner,artifact_id)
        for source in artifact['sources']:self.require_operation(owner,registration_id=source['registration_id'],context=source.get('context'),versions=source.get('versions'),record=False)
        info=stable_hash(artifact['path'])
        if info['sha256']!=artifact['sha256']:raise ProtectionError('产物待检查版本已变化',423,'artifact_changed')
        _,snapshot=create_snapshot(self.store,artifact['path'],policy['inspection_profile'],expected=info)
        version=self._version(info['sha256'],info['size']);result=evidence(self.store,snapshot,artifact['path'],version,policy['inspection_profile']);now=time.time()
        with self.store.db(True) as db:
            db.execute('INSERT OR REPLACE INTO artifact_snapshots VALUES(?,?,?,?,?,?,?)',(artifact_id,snapshot,version,profile_key(policy['inspection_profile'],artifact['path']),artifact['path'],now,now+policy['inspection_profile']['snapshot_retention_seconds']))
            db.execute('UPDATE protection_artifacts SET risk=? WHERE id=?',(result['risk'],artifact_id))
            self.store._event(db,owner,'artifact.inspected',payload={'artifact_id':artifact_id,'sha256':info['sha256'],'risk':result['risk'],'coverage':json.loads(result['coverage'])})
        return {'artifact_id':artifact_id,'risk':result['risk'],'snapshot':snapshot,'coverage':json.loads(result['coverage'])}
    def recheck_artifact(self,owner,artifact_id):
        policy=self._active();artifact=self.provenance(owner,artifact_id)
        if not artifact['sources']:raise ProtectionError('产物缺少来源',423,'provenance_missing')
        return self.jobs.enqueue(owner,artifact['sources'][0]['registration_id'],'artifact_inspection',{'artifact_id':artifact_id,'policy_revision':policy['revision']})
    def issue_ticket(self,owner,artifact_id,device_id='',ttl=60,approval_id=None,operation='download'):
        operation=canonical_operation(operation)
        artifact=self.require_artifact(owner,artifact_id,device_id,operation=operation,approval_id=approval_id);token=secrets.token_urlsafe(32)
        with self.store.db(True) as db:
            db.execute('INSERT INTO package_download_tickets(token_hash,owner,artifact_id,sha256,sources,policy_revision,device_id,expires,approval_id,operation) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (digest(token),owner,artifact_id,artifact['sha256'],encode(artifact['sources']),self.policy()['revision'],device_id,time.time()+min(max(1,ttl),60),encode(approval_id) if isinstance(approval_id,dict) else approval_id or '',operation))
        return token
    def consume_ticket(self,owner,artifact_id,token):
        policy=self._active()
        with self.store.db() as db:row=db.execute('SELECT * FROM package_download_tickets WHERE token_hash=? AND owner=? AND artifact_id=? AND expires>?',(digest(token),owner,artifact_id,time.time())).fetchone()
        if not row or row['policy_revision']!=policy['revision']:raise ProtectionError('下载凭据已失效',423,'download_ticket_invalid')
        artifact=self.require_artifact(owner,artifact_id,row['device_id'],operation=row['operation'],approval_id=row['approval_id'])
        if row['sha256']!=artifact['sha256'] or row['sources']!=encode(artifact['sources']):raise ProtectionError('下载目标或来源已变化',423,'download_ticket_invalid')
        with self.store.db(True) as db:
            cursor=db.execute('DELETE FROM package_download_tickets WHERE token_hash=?',(digest(token),))
            if cursor.rowcount!=1:raise ProtectionError('下载凭据已使用',423,'download_ticket_invalid')
        return artifact
    def record_operation(self,owner,registration_id,operation,result,context=None,targets=None,task_id='',request_id='',source='server_business',trusted=False,phase=''):
        self._active();self._registration(owner,registration_id,check_access=not trusted)
        if trusted and (not isinstance(context,dict) or context.get('owner')!=owner or context.get('registration_id')!=registration_id):
            raise ProtectionError('操作回执缺少真实冻结归属',403,'context_invalid')
        return self.store.event(owner,'operation.'+operation,device=(context or {}).get('device_id',''),
            payload={'registration_id':registration_id,'result':result,'targets':targets or [],'scope':'files' if targets else 'package',
                     'task_id':task_id,'request_id':request_id,'source':source,'phase':phase,'context':context or {}})

    def process_job(self,job_id=None,worker_id=None):
        self._active();refresh_runtime_engines(self.store)
        policy=self._active();worker_id=worker_id or uid()
        job=self.jobs.claim(worker_id,job_id,lease_seconds=max(600,policy['inspection_profile']['large_timeout_seconds']+120))
        if not job:return None
        reg=None
        try:
            reg=self._registration(job['owner'],job['registration_id'])
            if job['payload'].get('policy_revision')!=policy['revision']:raise ProtectionError('任务策略已变化，请重新提交',423,'context_stale')
            if job['kind']=='artifact_inspection':
                result=self._inspect_artifact(job['owner'],job['payload']['artifact_id'],policy)
                self.jobs.finish(job['id'],worker_id,result);return self.jobs.get(job['owner'],job['id'])
            if job['kind']!='inventory':raise ProtectionError('未知保护任务类型')
            inventory_epoch=self.local_epoch(reg,require_verified=False)
            root=Path(reg['root']);previous=self.manifest(reg['owner'],reg['id'])['files']
            old_by_path={r['relative_path']:r for r in previous};old_by_identity={r['identity']:r for r in previous}
            members=[];paths=[];profile=policy['inspection_profile']
            started=time.monotonic()
            def beat(progress):
                if not self.enabled:raise ProtectionError('保护已关闭，停止新增后台检查',409,'protection_disabled')
                if time.monotonic()-started>profile.get('inventory_timeout_seconds',7200):raise ProtectionError('核验任务超过时间预算',423,'job_timeout')
                self._owner(reg['owner'],reg['user_id']);self._root(reg['owner'],reg['root'])
                self.jobs.heartbeat(job['id'],worker_id,progress,lease_seconds=max(600,profile['large_timeout_seconds']+120))
            def inaccessible(exc):raise exc
            if root.is_symlink() or not (root.is_dir() or root.is_file()):raise ProtectionError('本地资料不可访问',423,'root_unavailable')
            listing=[(str(root.parent),[],[root.name])] if root.is_file() else os.walk(root,followlinks=False,onerror=inaccessible)
            for directory,dirs,files in listing:
                for name in dirs+files:
                    if (Path(directory)/name).is_symlink():raise ProtectionError('清单出现符号链接，核验不完整',423,'manifest_incomplete')
                for name in files:
                    paths.append(Path(directory)/name)
                    if len(paths)>profile.get('inventory_max_files',200000):raise ProtectionError('目录成员超过核验容量',423,'manifest_incomplete')
            for index,path in enumerate(sorted(paths)):
                if not path.resolve().is_relative_to(root):raise ProtectionError('清单路径越界',423,'manifest_incomplete')
                beat({'files_complete':index,'files_total':len(paths),'path':str(path)})
                info,snapshot=create_snapshot(self.store,path,profile,heartbeat=beat)
                relative=root.name if path==root else str(path.relative_to(root));old=old_by_path.get(relative)
                if old and old['identity']!=info['identity']:old=None
                if not old:
                    candidate=old_by_identity.get(info['identity'])
                    if candidate and not Path(candidate['path']).exists():old=candidate
                file_id=old['file_id'] if old else uid();version=self._version(info['sha256'],info['size'])
                beat({'files_complete':index,'files_total':len(paths),'path':str(path),'phase':'inspection'})
                inspection=evidence(self.store,snapshot,str(path),version,profile)
                members.append({'id':file_id,'file_id':file_id,'version_id':version,'path':str(path),'relative_path':relative,
                    **info,'risk':inspection['risk'],'findings':json.loads(inspection['findings']),
                    'scan_status':json.loads(inspection['engines']),'coverage':json.loads(inspection['coverage']),'snapshot':snapshot})
                # Snapshot creation and scanning never justify a verdict for changed source bytes.
                if stable_hash(path)!=info:raise ProtectionError('检查期间源文件发生变化',423,'content_changed')
            after_paths=self.local_paths(reg)
            if set(after_paths)!=set(str(p) for p in paths):raise ProtectionError('检查期间成员集合发生变化',423,'content_changed')
            # Recheck every earlier member after the final scan, not only the path set.
            self.compare_local_members(reg,members)
            beat({'files_complete':len(paths),'files_total':len(paths),'phase':'commit'})
            signature_keys=('file_id','version_id','path','relative_path','sha256','size','identity','mtime_ns')
            same=bool(previous) and [{k:m[k] for k in signature_keys} for m in previous]==[{k:m[k] for k in signature_keys} for m in members]
            revision=reg['manifest_revision'] if same else reg['manifest_revision']+1
            collection=uid();now=time.time()
            with self.store.db(True) as db:
                current=db.execute('SELECT * FROM registrations WHERE id=?',(reg['id'],)).fetchone()
                lease=db.execute("SELECT 1 FROM protection_jobs WHERE id=? AND state='running' AND lease_owner=? AND lease_until>?",(job['id'],worker_id,now)).fetchone()
                current_epoch=db.execute('SELECT gap_epoch FROM registration_devices WHERE registration_id=? AND device_id=?',(reg['id'],self.local_device_id())).fetchone()
                if not current_epoch or current_epoch[0]!=inventory_epoch:raise ProtectionError('核验期间发生监控缺口',409,'monitoring_gap')
                if not lease or current['manifest_revision']!=reg['manifest_revision'] or current['state']=='cancelled' or self.policy()['revision']!=policy['revision']:
                    raise ProtectionError('任务租约、登记或策略已失效',409,'context_stale')
                db.execute('INSERT INTO collections(id,owner,root,state,created,updated,file_count) VALUES(?,?,?,?,?,?,?)',(collection,reg['owner'],reg['root'],'ready',now,now,len(members)))
                db.execute('INSERT OR REPLACE INTO manifest_revisions VALUES(?,?,?,?,?,?)',(reg['id'],revision,digest(encode(members)),encode(members),1,now))
                db.execute('UPDATE logical_files SET available=0 WHERE registration_id=?',(reg['id'],))
                for member in members:
                    # Reviews are bound to this completed manifest, not to a hash forever.
                    db.execute('DELETE FROM risk_reviews WHERE file_id=?',(member['file_id'],))
                    db.execute('INSERT INTO files(id,collection,owner,path,relative_path,size,sha256,mtime_ns,identity,risk,findings,scan_status,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET collection=excluded.collection,path=excluded.path,relative_path=excluded.relative_path,size=excluded.size,sha256=excluded.sha256,mtime_ns=excluded.mtime_ns,identity=excluded.identity,risk=excluded.risk,findings=excluded.findings,scan_status=excluded.scan_status,updated=excluded.updated',
                        (member['file_id'],collection,reg['owner'],member['path'],member['relative_path'],member['size'],member['sha256'],member['mtime_ns'],member['identity'],member['risk'],encode(member['findings']),encode({**member['scan_status'],'coverage':member['coverage']}),now))
                    db.execute('INSERT INTO logical_files VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET relative_path=excluded.relative_path,identity=excluded.identity,current_version_id=excluded.current_version_id,available=1,updated=excluded.updated',
                        (member['file_id'],reg['id'],member['relative_path'],member['identity'],member['version_id'],1,now))
                    db.execute('INSERT OR REPLACE INTO file_instances VALUES(?,?,?,?,?,?,?,?,?,?)',('local:'+member['file_id'],member['file_id'],reg['id'],'local',self.local_device_id(),member['path'],member['identity'],member['version_id'],'available',now))
                    db.execute('INSERT OR REPLACE INTO version_snapshots VALUES(?,?,?,?,?,?)',(reg['id'],member['file_id'],member['version_id'],member['snapshot'],now,now+profile['snapshot_retention_seconds']))
                    self.store._event(db,reg['owner'],'file.inspected',file_id=member['file_id'],instance_id='local:'+member['file_id'],device=self.local_device_id(),payload={'registration_id':reg['id'],'version_id':member['version_id'],'sha256':member['sha256'],'risk':member['risk'],'coverage':member['coverage']})
                db.execute("UPDATE registrations SET manifest_revision=?,collection_id=?,state='ready',error='',policy_revision=?,updated=? WHERE id=?",(revision,collection,policy['revision'],now,reg['id']))
                db.execute("UPDATE registration_devices SET state='verified',verified_epoch=gap_epoch,verified_revision=?,manifest_generation=?,last_complete=? WHERE registration_id=? AND device_id=?",(revision,revision,now,reg['id'],self.local_device_id()))
                db.execute('UPDATE protection_alerts SET resolved=? WHERE registration_id=?',(now,reg['id']))
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('verified:'+collection,str(now)))
                db.execute('UPDATE monitoring_epochs SET resolved=? WHERE registration_id=? AND device_id=? AND resolved IS NULL',(now,reg['id'],self.local_device_id()))
                self.store._event(db,reg['owner'],'package.manifest_complete',payload={'registration_id':reg['id'],'manifest_revision':revision,'file_count':len(members),'job_id':job['id']})
            self.jobs.finish(job['id'],worker_id,{'registration_id':reg['id'],'manifest_revision':revision,'collection_id':collection,'members':len(members)})
            return self.jobs.get(job['owner'],job['id'])
        except Exception as exc:
            try:self.jobs.finish(job['id'],worker_id,error=str(exc),retry=isinstance(exc,OSError))
            except ProtectionError:pass
            fresh=self.jobs.get(job['owner'],job['id'])
            if reg and job['kind']=='inventory' and fresh['state'] not in {'completed','cancelled'} and fresh['lease_owner'] in {'',worker_id}:
                with self.store.db(True) as db:
                    state='changed' if isinstance(exc,ProtectionError) and exc.code in {'content_changed','monitoring_gap'} else 'unavailable' if isinstance(exc,OSError) or isinstance(exc,ProtectionError) and exc.code=='root_unavailable' else 'failed'
                    db.execute("UPDATE registrations SET state=?,error=?,updated=? WHERE id=? AND state!='cancelled'",(state,str(exc)[:2000],time.time(),reg['id']))
                    self.store._event(db,reg['owner'],'package.inspection_failed',payload={'registration_id':reg['id'],'job_id':job['id'],'error':str(exc)[:2000]})
            return self.jobs.get(job['owner'],job['id'])

    def mark_gap(self,owner,device_id,reason='监控中断',registration_id=None):
        self._configured()
        with self.store.db(True) as db:
            where='device_id=?';params=[device_id]
            if registration_id:where+=' AND registration_id=?';params.append(registration_id)
            rows=db.execute('SELECT rd.* FROM registration_devices rd JOIN registrations r ON r.id=rd.registration_id WHERE r.owner=? AND '+where,[owner]+params).fetchall()
            for rd in rows:
                epoch=rd['gap_epoch']+1
                db.execute("UPDATE registration_devices SET gap_epoch=?,state='unverified' WHERE registration_id=? AND device_id=?",(epoch,rd['registration_id'],device_id))
                db.execute('INSERT INTO monitoring_epochs VALUES(?,?,?,?,?,NULL)',(rd['registration_id'],device_id,epoch,str(reason)[:1000],time.time()))
                db.execute("UPDATE file_instances SET state='unverified' WHERE registration_id=? AND device_id=?",(rd['registration_id'],device_id))
                db.execute("UPDATE local_manifest_tasks SET state='stale' WHERE registration_id=? AND device_id=? AND state='pending'",(rd['registration_id'],device_id))
                self.store._event(db,owner,'package.monitoring_gap',device=device_id,payload={'registration_id':rd['registration_id'],'gap_epoch':epoch,'reason':reason})
            db.execute("UPDATE actions SET state='stale' WHERE device=? AND owner=? AND kind!='restore' AND state IN ('pending','running')",(device_id,owner))
        return len(rows)

    def prune_snapshots(self):
        from .capacity import prune
        return prune(self.store,self._configured()['inspection_profile'])
    def set_sensitivity(self,owner,registration_id,sensitivity,actor,reason,is_admin=False,file_ids=None):
        self._configured();self._registration(owner,registration_id)
        if not is_admin or not str(reason).strip():raise ProtectionError('敏感标签需要管理员和理由',403,'admin_required')
        if sensitivity not in {'normal','sensitive','restricted'}:raise ProtectionError('未知敏感分类')
        with self.store.db(True) as db:
            if file_ids:
                for file_id in file_ids:
                    if not db.execute('SELECT 1 FROM logical_files WHERE id=? AND registration_id=?',(file_id,registration_id)).fetchone():raise ProtectionError('标签文件不属于此包',403,'target_forbidden')
                    db.execute('INSERT OR REPLACE INTO file_labels VALUES(?,?,?,?,?,?)',(file_id,owner,sensitivity,actor,reason,time.time()))
            else:db.execute('UPDATE registrations SET sensitivity=?,updated=? WHERE id=?',(sensitivity,time.time(),registration_id))
            self.store._event(db,owner,'package.sensitivity_changed',payload={'registration_id':registration_id,'sensitivity':sensitivity,'file_ids':file_ids or [],'actor':actor,'reason':reason})
        # Classification is an authorization revision: previously issued contexts/tickets expire.
        return self.update_policy(actor,True,{},'敏感分类变更：'+reason)
