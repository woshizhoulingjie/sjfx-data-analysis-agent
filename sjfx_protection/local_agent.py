"""Authenticated loopback integration with the selected-platform audit assistant.

No browser sees the shared secret, no contents are uploaded, and an unavailable
assistant cannot manufacture healthy audit coverage. State belongs to one local
OS installation/account; a differently owned assistant is not silently rebound.
"""
from __future__ import annotations
import json
import hashlib
import os
from pathlib import Path
import re
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from urllib.error import HTTPError
from .store import ProtectionError, encode
from .scanner import stable_hash

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def failure(exc):
    result={'error_type':type(exc).__name__,'code':'agent_unavailable'}
    if isinstance(exc,ProtectionError):
        result.update(code=exc.code,http_status=exc.status)
    if result['code']=='local_agent_capacity_exceeded':
        result.update(max_batch_request_bytes=1024*1024,
                      reason='完整登记超过当前助手或安装配置预算；需查看助手容量回执')
    return result


class LocalAgent:
    def __init__(self,package):
        self.package=package;self.store=package.store
        self.token_file=os.getenv('SJFX_LOCAL_AGENT_TOKEN_FILE','')
        from urllib.parse import urlsplit
        self.endpoint=os.getenv('SJFX_LOCAL_AGENT_ENDPOINT','http://127.0.0.1:18767').rstrip('/')
        endpoint=urlsplit(self.endpoint)
        if endpoint.scheme!='http' or endpoint.hostname not in {'127.0.0.1','localhost','::1'} or endpoint.path or endpoint.query or endpoint.fragment or endpoint.username:
            raise ProtectionError('助手地址必须为本机无凭据HTTP端口',400,'agent_endpoint_invalid')

    @property
    def configured(self):return bool(self.token_file)

    def request(self,path,payload,*,timeout=30):
        token=Path(self.token_file).read_text(encoding='ascii').strip()
        if not re.fullmatch('[0-9a-f]{64}',token):raise ProtectionError('本机助手凭据格式无效',423,'agent_token_invalid')
        request=Request(self.endpoint+path,data=encode(payload).encode('utf-8'),headers={
            'Authorization':'Bearer '+token,'Content-Type':'application/json'},method='POST')
        try:
            with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=timeout) as response:
                raw=response.read(8*1024*1024+1)
        except HTTPError as exc:
            # Do not retain arbitrary helper response text or credentials.
            with exc:
                raw=exc.read(65537)
            detail={}
            try:
                if len(raw)<=65536:detail=json.loads(raw)
            except (ValueError,UnicodeError):pass
            code='agent_http_error'
            if exc.code==413 and isinstance(detail,dict) and detail.get('code')=='local_agent_capacity_exceeded':
                code='local_agent_capacity_exceeded'
            raise ProtectionError('本机助手拒绝请求（HTTP '+str(exc.code)+'）',exc.code,code) from None
        if len(raw)>8*1024*1024:raise ProtectionError('本机助手响应超限',423,'agent_protocol_error')
        result=json.loads(raw)
        if not isinstance(result,dict) or result.get('ok') is False:
            raise ProtectionError('本机助手拒绝请求',423,'agent_protocol_error')
        return result

    @staticmethod
    def _manifest_digest(files):
        digest=hashlib.sha256()
        for row in files:
            values=[str(row[k]) for k in ('binding_id','file_id','local_path','sha256','version_id','size','identity')]
            if any(any(c in value for c in '\x00\r\n') for value in values):
                raise ProtectionError('文件名称含助手事务不支持的控制字符',423,'agent_filename_unsupported')
            digest.update(('\x00'.join(values)+'\n').encode('utf-8'))
        return digest.hexdigest()

    @staticmethod
    def _verify_binding(row,actual,metadata):
        if not isinstance(actual,dict):raise ProtectionError('助手绑定清单无效',423,'agent_protocol_error')
        expected={k:row[k] for k in ('file_id','version_id','size','sha256','local_path')}
        expected.update(device_id=metadata['device_id'],owner_id=metadata['owner_id'],
            registration_id=metadata['registration_id'],registration_revision=metadata['manifest_revision'],
            manifest_generation=metadata['manifest_revision'],gap_epoch=metadata['gap_epoch'])
        if ((actual.get('binding_id') or actual.get('id'))!=row['binding_id'] or
                any(actual.get(k)!=v for k,v in expected.items()) or
                (actual.get('identity') or actual.get('local_identity'))!=row['identity']):
            raise ProtectionError('助手回执不是同一本地实例版本',423,'target_stale')

    def _transaction_register(self,metadata,files):
        """Validate every page before publishing any local coverage marker."""
        digest=self._manifest_digest(files)
        total=len(files)
        budget=int(os.getenv('SJFX_LOCAL_AGENT_REGISTRATION_BYTES',str(32*1024**3)))
        if total>200000 or sum(row['size'] for row in files)>min(budget,1024**4) or any(row['size']>10*1024**3 for row in files):
            raise ProtectionError('助手事务完整集合超过文件数或字节预算',413,'local_agent_capacity_exceeded')
        timeout=max(30,self.package.policy()['inspection_profile'].get('inventory_timeout_seconds',7200))
        begun=self.request('/agent-local/v1/register/begin',{'registration':metadata,
            'expected_count':total,'expected_bytes':sum(row['size'] for row in files),'manifest_digest':digest})
        transaction=begun.get('transaction_id')
        if not isinstance(transaction,str) or not transaction or begun.get('next_offset')!=0:
            raise ProtectionError('助手事务建立回执无效',423,'agent_protocol_error')
        batch_limit=begun.get('max_batch_files')
        if isinstance(batch_limit,bool) or not isinstance(batch_limit,int) or not 1<=batch_limit<=500:
            raise ProtectionError('助手事务分批容量无效',423,'agent_protocol_error')
        committed=False
        try:
            offset=0
            while offset<total:
                batch=files[offset:offset+batch_limit]
                while batch and len(encode({'transaction_id':transaction,'offset':offset,'files':batch}).encode('utf-8'))>1024*1024:
                    batch=batch[:len(batch)//2]
                if not batch:raise ProtectionError('单个助手登记成员超过协议容量',413,'local_agent_capacity_exceeded')
                response=self.request('/agent-local/v1/register/batch',{'transaction_id':transaction,'offset':offset,'files':batch},timeout=timeout)
                if response.get('next_offset')!=offset+len(batch) or response.get('coverage_complete') is not False:
                    raise ProtectionError('助手事务分批回执无效',423,'agent_protocol_error')
                offset+=len(batch)
            response=self.request('/agent-local/v1/register/commit',{'transaction_id':transaction},timeout=timeout)
            if response.get('coverage_complete') is not True or response.get('count')!=total or response.get('manifest_digest')!=digest or response.get('gap_epoch')!=metadata['gap_epoch']:
                raise ProtectionError('助手事务提交清单不完整',423,'agent_protocol_error')
            committed=True
            offset=0
            while offset<total:
                response=self.request('/agent-local/v1/register/result',{'transaction_id':transaction,'offset':offset,'limit':500})
                rows=response.get('bindings')
                if (not isinstance(rows,list) or not rows or len(rows)>500 or response.get('count')!=total or
                        response.get('offset')!=offset or response.get('next_offset')!=offset+len(rows) or
                        response.get('next_offset')>total or response.get('manifest_digest')!=digest):
                    raise ProtectionError('助手事务分页清单不完整',423,'agent_protocol_error')
                for expected,actual in zip(files[offset:offset+len(rows)],rows):self._verify_binding(expected,actual,metadata)
                offset+=len(rows)
            return transaction
        finally:
            if not committed:
                try:self.request('/agent-local/v1/register/abort',{'transaction_id':transaction})
                except Exception:pass

    def register(self,reg):
        p=self.package;owner=reg['owner'];device=p.local_device_id()
        key='local-agent-registration:'+reg['id']
        with self.store.db() as db:
            fresh=db.execute('SELECT manifest_revision,state FROM registrations WHERE id=?',(reg['id'],)).fetchone()
            rd=db.execute('SELECT * FROM registration_devices WHERE registration_id=? AND device_id=?',(reg['id'],device)).fetchone()
            done=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
            occupied=db.execute('SELECT owner FROM devices WHERE id=?',(device,)).fetchone()
        if occupied and occupied['owner']!=owner:
            raise ProtectionError('本机助手已属于另一账号，请使用独立助手配置',403,'agent_owner_mismatch')
        if not fresh or fresh['manifest_revision']!=reg['manifest_revision']:
            raise ProtectionError('助手关联版本已变化',423,'context_stale')
        receipt=p.local_verification_receipt(fresh,rd)
        if receipt is None:
            raise ProtectionError('本地覆盖未完整核验，需完整重新检查',423,'monitoring_gap')
        epoch=receipt[1];marker=encode(receipt)
        if done and done[0]==marker:return
        manifest=p.manifest(owner,reg['id']);files=[]
        for row in manifest['files']:
            files.append({'binding_id':'local:'+row['file_id'],'file_id':row['file_id'],
                'local_path':row['path'],'sha256':row['sha256'],'version_id':row['version_id'],
                'size':row['size'],'identity':row['identity']})
        payload={'device_id':device,'owner_id':owner,'registration_id':reg['id'],
            'manifest_revision':reg['manifest_revision'],'gap_epoch':epoch,
            'local_root':reg['root'],'files':files}
        transaction_required=(len(files)>1000 or sum(row['size'] for row in files)>8*1024**3 or
                              len(encode(payload).encode('utf-8'))>1024*1024)
        for row in manifest['files']:
            info=stable_hash(row['path'])
            if any(info[k]!=row[k] for k in ('sha256','identity','size')):
                p.invalidate_local(owner,reg,'助手关联前本地实例改变')
                raise ProtectionError('关联目标已变化',423,'content_changed')
        metadata={k:v for k,v in payload.items() if k!='files'};transaction=None
        if transaction_required:transaction=self._transaction_register(metadata,files)
        else:
            result=self.request('/agent-local/v1/register',payload)
            returned=result.get('bindings',[])
            if not isinstance(returned,list) or len(returned)!=len(files) or not all(isinstance(row,dict) for row in returned):
                raise ProtectionError('助手绑定清单不完整',423,'agent_protocol_error')
            by_id={row.get('binding_id') or row.get('id'):row for row in returned}
            if len(by_id)!=len(files):raise ProtectionError('助手绑定清单不完整',423,'agent_protocol_error')
            for row in files:self._verify_binding(row,by_id.get(row['binding_id'],{}),metadata)
        p.compare_local_members(reg,manifest['files'])
        with self.store.db(True) as db:
            fresh=db.execute('SELECT manifest_revision,state FROM registrations WHERE id=?',(reg['id'],)).fetchone()
            rd=db.execute('SELECT * FROM registration_devices WHERE registration_id=? AND device_id=?',(reg['id'],device)).fetchone()
            if p.local_verification_receipt(fresh,rd)!=receipt:
                raise ProtectionError('助手关联期间完整核验回执已变化',423,'context_stale')
            db.execute('INSERT OR IGNORE INTO devices(id,owner,user_id,name,public_key,created) VALUES(?,?,?,?,?,?)',
                (device,owner,reg['user_id'],'本机读取与审计助手','local-bearer:'+device,time.time()))
            for row in files:
                db.execute('INSERT INTO bindings VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET local_path=excluded.local_path,local_identity=excluded.local_identity,sha256=excluded.sha256,state=excluded.state,updated=excluded.updated',
                    (row['binding_id'],row['file_id'],device,owner,row['local_path'],row['identity'],row['sha256'],'watching',time.time()))
                self.store._event(db,owner,'local.assistant_attached',device=device,file_id=row['file_id'],instance_id=row['binding_id'],payload={'registration_id':reg['id'],'version_id':row['version_id'],'sha256':row['sha256'],'identity':row['identity']})
            db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,marker))
        if transaction:
            try:self.request('/agent-local/v1/register/finish',{'transaction_id':transaction})
            except Exception:pass  # durable coverage exists; receipt cleanup may retry later

    def sync(self):
        if not self.configured:return {'configured':False,'healthy':False}
        p=self.package;p._ensure();device=p.local_device_id()
        registration_errors={};device_row=None
        try:
            with self.store.db() as db:
                registrations=[dict(r) for r in db.execute("SELECT * FROM registrations WHERE mode='local' AND state='ready'")]
                device_row=db.execute('SELECT * FROM devices WHERE id=?',(device,)).fetchone()
            if p.enabled:
                for reg in registrations:
                    try:self.register(reg)
                    except Exception as exc:registration_errors[reg['id']]=failure(exc)
            with self.store.db() as db:
                device_row=db.execute('SELECT * FROM devices WHERE id=?',(device,)).fetchone()
                row=db.execute("SELECT value FROM settings WHERE key='local-agent-acks'").fetchone()
                ack=json.loads(row[0]) if row else {'events':[],'results':[]}
            if not device_row:
                report={'configured':True,'connected':False,'healthy':False,'coverage_complete':False,
                        'reason':'no_associated_package','registration_errors':registration_errors}
                with self.store.db(True) as db:
                    db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('local-agent-health',encode({'at':time.time(),**report})))
                return report
            fields={'id','binding','kind','expected_sha256','local_path','local_identity',
                'restore_action_id','expected_version_id','registration_id','registration_revision',
                'manifest_generation','gap_epoch','device_id','expires_at'}
            actions=[{k:v for k,v in a.items() if k in fields} for a in self.store.pending_actions(device) if p.enabled or a['kind']=='restore']
            result=self.request('/agent-local/v1/sync',{'device_id':device,'protection_enabled':p.enabled,
                'ack_event_ids':ack['events'],'ack_result_ids':ack['results'],'actions':actions})
            events=result.get('events',[])
            accepted=self.store.ingest(dict(device_row),events)
            completed=[]
            for record in result.get('results',[]):
                self.store.action_result(dict(device_row),record['action_id'],record['result'])
                completed.append(record['action_id'])
            health=result.get('health',{})
            if not isinstance(health,dict):raise ProtectionError('助手健康响应无效',423,'agent_protocol_error')
            # Receipts are durable before acknowledging; retry duplicates are safe.
            self.store.heartbeat(device,health)
            report={'configured':True,'connected':True,'health':health,'registration_errors':registration_errors,
                    'registration_coverage_complete':not bool(registration_errors)}
            with self.store.db(True) as db:
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('local-agent-acks',encode({'events':accepted,'results':completed})))
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('local-agent-health',encode({'at':time.time(),**report})))
            return report
        except Exception as exc:
            # No plaintext token, response body or query is logged.
            with self.store.db(True) as db:
                prior=db.execute("SELECT value FROM settings WHERE key='local-agent-health'").fetchone()
                was_connected=bool(prior and json.loads(prior[0]).get('connected'))
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('local-agent-health',encode({'at':time.time(),'connected':False,'healthy':False,'registration_errors':registration_errors,**failure(exc)})))
            if was_connected and device_row:
                p.mark_gap(device_row['owner'],device,'本机助手连接中断')
            return {'configured':True,'connected':False,'healthy':False,'registration_errors':registration_errors,**failure(exc)}
