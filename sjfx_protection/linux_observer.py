"""Ordinary-user Linux observer with authenticated loopback process API.

Polling observes stable current versions, not every intermediate write. auditd
program evidence is optional and never claimed without readable native records.
No lock/quarantine action is implemented by this observer.
"""
from __future__ import annotations
import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import shlex
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
import uuid
from .desktop_audit import preflight, parse_auditd_records, linux_rule_plan

REQUEST_LIMIT=1024*1024
FILE_LIMIT=1000
SNAPSHOT_LIMIT=10*1024**3
REGISTRATION_LIMIT=8*1024**3
EVENT_LIMIT=50000
TRANSACTION_FILE_LIMIT=200000


def transaction_byte_limit():
    try: value=int(os.getenv('SJFX_LOCAL_AGENT_REGISTRATION_BYTES',str(32*1024**3)))
    except ValueError: value=32*1024**3
    return value if 0<value<=1024**4 else 32*1024**3


def manifest_digest(files):
    digest=hashlib.sha256()
    for f in files:
        fields=[f['binding_id'],f['file_id'],f['local_path'],f['sha256'],f['version_id'],str(f['size']),f['identity']]
        if any(any(c in field for c in ('\x00','\r','\n')) for field in fields): raise ValueError('Manifest fields contain invalid delimiters')
        digest.update(('\x00'.join(fields)+'\n').encode('utf-8'))
    return digest.hexdigest()


def snapshot(path):
    path=Path(path)
    # Reject symlink ancestors as well as the final object.
    if any(p.is_symlink() for p in (path,*path.parents)): raise ValueError('Symbolic link source rejected')
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size>SNAPSHOT_LIMIT: raise ValueError('Unsupported file/capacity')
        digest=hashlib.sha256()
        with os.fdopen(fd,'rb',closefd=False) as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''): digest.update(chunk)
        after=os.fstat(fd);current=path.stat()
        if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns) or (current.st_dev,current.st_ino)!=(before.st_dev,before.st_ino):
            raise ValueError('File changed during stable read')
        return {'identity':f'{before.st_dev}:{before.st_ino}','sha256':digest.hexdigest(),'size':before.st_size}
    finally: os.close(fd)


class Observer:
    def __init__(self,state,token_file,audit=False):
        self.state=Path(state);self.state.mkdir(parents=True,exist_ok=True,mode=0o700)
        token_path=Path(token_file)
        st=token_path.stat()
        if os.name!='nt' and (st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)&0o077): raise ValueError('Token must belong only to current user')
        self.token=token_path.read_text(encoding='ascii').strip()
        if not re.fullmatch('[0-9a-f]{64}',self.token): raise ValueError('Invalid local token')
        self.lock=threading.RLock();self.audit=audit;self.audit_health=False;self.audit_error='Not configured'
        self.db=sqlite3.connect(self.state/'observer.db',check_same_thread=False)
        self.db.executescript('CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE IF NOT EXISTS bindings(id TEXT PRIMARY KEY,payload TEXT); CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,payload TEXT); CREATE TABLE IF NOT EXISTS results(id TEXT PRIMARY KEY,payload TEXT); CREATE TABLE IF NOT EXISTS registration_parts(transaction_id TEXT,position INTEGER,payload TEXT,PRIMARY KEY(transaction_id,position));')
        mode=self.get('mode','')
        if mode and mode!='linux-desktop-v1': raise ValueError('Foreign assistant state cannot be reused')
        self.set('mode','linux-desktop-v1');self.gap('Observer restart; interrupted history cannot be reconstructed')

    def get(self,key,default=None):
        row=self.db.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self,key,value):
        self.db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',(key,json.dumps(value)));self.db.commit()

    def bindings(self): return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM bindings ORDER BY id')]

    def enqueue(self,event):
        if self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]>=EVENT_LIMIT:
            self.set('queue_gap',True);raise ValueError('Persistent event queue full; coverage incomplete')
        event.setdefault('event_id',uuid.uuid4().hex);event.setdefault('occurred',time.time())
        self.db.execute('INSERT OR IGNORE INTO events(id,payload) VALUES(?,?)',(event['event_id'],json.dumps(event)));self.db.commit()

    def gap(self,reason):
        epochs=self.get('epochs',[])
        for epoch in epochs:
            if not epoch.get('unresolved'): epoch['gap_epoch']+=1
            epoch.update(unresolved=True,reason=reason)
        self.set('epochs',epochs)

    def register(self,payload,transaction=None):
        required={'device_id','owner_id','registration_id','manifest_revision','gap_epoch','local_root','files'}
        if set(payload)!=required: raise ValueError('Invalid registration fields')
        files=payload['files']
        count_limit=TRANSACTION_FILE_LIMIT if transaction else FILE_LIMIT
        byte_limit=transaction_byte_limit() if transaction else REGISTRATION_LIMIT
        if not isinstance(files,list) or not files or len(files)>count_limit or sum(f['size'] for f in files)>byte_limit:
            raise OverflowError('Complete registration capacity exceeded; no partial coverage accepted')
        for key in ('device_id','owner_id','registration_id'):
            if not isinstance(payload[key],str) or not payload[key]: raise ValueError('Missing identity')
        if type(payload['manifest_revision']) is not int or type(payload['gap_epoch']) is not int or payload['manifest_revision']<1 or payload['gap_epoch']<0: raise ValueError('Invalid revision/epoch')
        for key in ('device_id','owner_id'):
            if self.get(key) and self.get(key)!=payload[key]: raise ValueError('Assistant belongs to another device/account')
        root=Path(payload['local_root'])
        if not root.is_absolute() or root.is_symlink() or not (root.is_dir() or root.is_file()): raise ValueError('Invalid selected root')
        root=root.resolve();seen=set();seen_paths=set();out=[]
        if root.is_file() and (len(files)!=1 or Path(files[0]['local_path'])!=root): raise ValueError('Single-file selection requires its only exact member')
        epochs=self.get('epochs',[])
        for epoch in epochs:
            if epoch['registration_id']==payload['registration_id'] and payload['gap_epoch']<epoch['gap_epoch']: raise ValueError('Stale gap epoch')
        existing={b['binding_id']:b for b in self.bindings()}
        for f in files:
            if set(f)!={'binding_id','file_id','local_path','sha256','version_id','size','identity'}: raise ValueError('Invalid file fields')
            if type(f['size']) is not int or f['size']<0 or not isinstance(f['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',f['sha256']): raise ValueError('Invalid stable size/hash')
            path=Path(f['local_path'])
            if not path.is_absolute() or not path.is_relative_to(root) or str(path) in seen_paths: raise ValueError('Path out of selected root or duplicate')
            if not f['file_id'] or f['binding_id']!='local:'+f['file_id'] or f['binding_id'] in seen or f['version_id']!=f['sha256']: raise ValueError('Invalid file instance/version')
            old=existing.get(f['binding_id'])
            if old and (old['registration_id']!=payload['registration_id'] or old['registration_revision']>payload['manifest_revision']): raise ValueError('Conflicting binding/revision')
            seen.add(f['binding_id']);seen_paths.add(str(path));actual=snapshot(path)
            if any(actual[k]!=f[k] for k in ('identity','sha256','size')): raise ValueError('Actual bytes/instance mismatch')
            out.append({**f,'local_identity':f['identity'],'registration_id':payload['registration_id'],
                        'registration_revision':payload['manifest_revision'],'manifest_generation':payload['manifest_revision'],
                        'gap_epoch':payload['gap_epoch'],'device_id':payload['device_id'],'owner_id':payload['owner_id'],
                        'local_root':str(root),'state':'watching'})
        for b in out:
            if snapshot(b['local_path'])!={k:b[k] for k in ('identity','sha256','size')}: raise ValueError('Changed before commit')
        epochs=[e for e in epochs if e['registration_id']!=payload['registration_id']]
        epochs.append({'registration_id':payload['registration_id'],'gap_epoch':payload['gap_epoch'],'local_root':str(root),'unresolved':False,'last_complete':time.time()})
        with self.db:
            for b in out:
                self.db.execute('INSERT OR REPLACE INTO bindings VALUES(?,?)',(b['binding_id'],json.dumps(b)))
                self.db.execute('DELETE FROM state WHERE key=?',('observed:'+b['binding_id'],))
            for key,value in {'device_id':payload['device_id'],'owner_id':payload['owner_id'],'epochs':epochs}.items(): self.db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',(key,json.dumps(value)))
            if transaction:
                identifier,receipt=transaction
                for i,b in enumerate(out): self.db.execute('UPDATE registration_parts SET payload=? WHERE transaction_id=? AND position=?',(json.dumps(b),identifier,i))
                receipt['committed']=True
                self.db.execute('UPDATE state SET value=? WHERE key=?',(json.dumps(receipt),'tx:'+identifier))
        return {'ok':True,'bindings':out,'gap_epoch':payload['gap_epoch'],'coverage_complete':True}

    def registration_transaction(self,path,payload):
        action=path.rsplit('/',1)[1]
        if action=='begin':
            if set(payload)!={'registration','expected_count','expected_bytes','manifest_digest'} or set(payload['registration'])!={'device_id','owner_id','registration_id','manifest_revision','gap_epoch','local_root'}: raise ValueError('Invalid transaction fields')
            if type(payload['expected_count']) is not int or type(payload['expected_bytes']) is not int: raise ValueError('Invalid transaction capacity types')
            if not 1<=payload['expected_count']<=TRANSACTION_FILE_LIMIT or not 0<=payload['expected_bytes']<=transaction_byte_limit(): raise OverflowError('Transaction total capacity exceeded')
            if not re.fullmatch('[0-9a-f]{64}',payload['manifest_digest']): raise ValueError('Invalid manifest digest')
            reg=payload['registration']
            if any(not isinstance(reg[k],str) or not reg[k] for k in ('device_id','owner_id','registration_id')) or reg['manifest_revision']<1 or reg['gap_epoch']<0: raise ValueError('Invalid registration identity')
            for field in ('device_id','owner_id'):
                if self.get(field) and self.get(field)!=reg[field]: raise ValueError('Foreign device/account')
            root=Path(reg['local_root'])
            if not root.is_absolute() or root.is_symlink() or not (root.is_dir() or root.is_file()): raise ValueError('Invalid selected root')
            if self.db.execute("SELECT COUNT(*) FROM state WHERE key LIKE 'tx:%'").fetchone()[0]>=8: raise OverflowError('Retained transaction capacity exceeded; finish or abort old receipts')
            identifier=uuid.uuid4().hex;self.set('tx:'+identifier,{**payload,'created':time.time(),'next_offset':0,'committed':False})
            return {'ok':True,'transaction_id':identifier,'next_offset':0,'coverage_complete':False,'max_batch_files':500,'max_request_bytes':REQUEST_LIMIT,'max_total_bytes':transaction_byte_limit()}
        identifier=payload.get('transaction_id','')
        if not isinstance(identifier,str) or not identifier or len(identifier)>128: raise ValueError('Invalid transaction id')
        transaction=self.get('tx:'+identifier)
        if not transaction: raise ValueError('Unknown registration transaction')
        if action in ('abort','finish'):
            if transaction['committed']!=(action=='finish'): raise ValueError('Only committed receipts may finish; abort cannot undo formal registration')
            with self.db:
                self.db.execute('DELETE FROM registration_parts WHERE transaction_id=?',(identifier,));self.db.execute('DELETE FROM state WHERE key=?',('tx:'+identifier,))
            return {'ok':True,'coverage_complete':transaction['committed'],'aborted':action=='abort','finished':action=='finish'}
        if not transaction['committed'] and time.time()-transaction['created']>86400: raise ValueError('Transaction expired; abort and reverify')
        if action=='batch':
            if set(payload)!={'transaction_id','offset','files'}: raise ValueError('Invalid batch fields')
            files=payload['files'];offset=payload['offset']
            if transaction['committed'] or offset!=transaction['next_offset'] or not 1<=len(files)<=500 or offset+len(files)>transaction['expected_count']: raise ValueError('Incomplete/out-of-order/oversized batch')
            manifest_digest(files)
            with self.db:
                for i,f in enumerate(files): self.db.execute('INSERT INTO registration_parts VALUES(?,?,?)',(identifier,offset+i,json.dumps(f)))
                transaction['next_offset']+=len(files);self.db.execute('UPDATE state SET value=? WHERE key=?',(json.dumps(transaction),'tx:'+identifier))
            return {'ok':True,'transaction_id':identifier,'next_offset':transaction['next_offset'],'coverage_complete':False}
        if action=='commit':
            if set(payload)!={'transaction_id'}: raise ValueError('Invalid commit fields')
            if transaction['next_offset']!=transaction['expected_count']: raise ValueError('Cannot commit partial member coverage')
            if not transaction['committed']:
                files=[json.loads(row[0]) for row in self.db.execute('SELECT payload FROM registration_parts WHERE transaction_id=? ORDER BY position',(identifier,))]
                if len(files)!=transaction['expected_count'] or sum(f['size'] for f in files)!=transaction['expected_bytes'] or manifest_digest(files)!=transaction['manifest_digest']: raise ValueError('Complete count, total bytes or manifest digest mismatch')
                self.register({**transaction['registration'],'files':files},(identifier,transaction))
            else:
                epoch=next((e for e in self.get('epochs',[]) if e['registration_id']==transaction['registration']['registration_id']),None)
                if not epoch or epoch.get('unresolved') or epoch['gap_epoch']!=transaction['registration']['gap_epoch']: raise ValueError('Committed receipt is historical; current monitoring gap requires re-registration')
            return {'ok':True,'coverage_complete':True,'transaction_id':identifier,'count':transaction['expected_count'],'manifest_digest':transaction['manifest_digest'],'gap_epoch':transaction['registration']['gap_epoch']}
        if action=='result':
            if set(payload)!={'transaction_id','offset','limit'}: raise ValueError('Invalid result fields')
            offset=payload['offset'];limit=payload['limit']
            if not transaction['committed'] or not 0<=offset<=transaction['expected_count'] or not 1<=limit<=500: raise ValueError('Only committed receipt can be paged')
            bindings=[json.loads(row[0]) for row in self.db.execute('SELECT payload FROM registration_parts WHERE transaction_id=? AND position>=? ORDER BY position LIMIT ?',(identifier,offset,limit))]
            return {'ok':True,'coverage_complete':True,'transaction_id':identifier,'offset':offset,'next_offset':offset+len(bindings),'count':transaction['expected_count'],'manifest_digest':transaction['manifest_digest'],'bindings':bindings}
        raise ValueError('Unknown registration transaction interface')

    def observe(self):
        if not self.get('enabled',False): return
        for b in self.bindings():
            try:
                actual=snapshot(b['local_path'])
                if all(actual[k]==b[k] for k in ('identity','sha256','size')): continue
                action='file.replaced' if actual['identity']!=b['identity'] else 'file.modified'
                fingerprint=json.dumps(actual,sort_keys=True)
            except (OSError,ValueError):
                actual={};action='file.unavailable';fingerprint='unavailable'
            key='observed:'+b['binding_id']
            if self.get(key)==fingerprint: continue
            self.enqueue({'binding_id':b['binding_id'],'registration_id':b['registration_id'],'action':action,
                'path':b['local_path'],'identity':actual.get('identity',b['identity']),
                'sha256':actual.get('sha256',''),'version_id':actual.get('sha256',''),
                'gap_epoch':b['gap_epoch'],'detail':'Polling observed current instance; intermediate changes may be unobserved'})
            self.set(key,fingerprint)
        # Discover newly added directory members without accepting them as verified.
        for epoch in self.get('epochs',[]):
            try:
                root=Path(epoch['local_root'])
                if root.is_file(): continue
                known={b['local_path'] for b in self.bindings() if b['registration_id']==epoch['registration_id']}
                for directory,dirs,files in os.walk(root,followlinks=False,onerror=lambda e:(_ for _ in ()).throw(e)):
                    if any((Path(directory)/name).is_symlink() for name in dirs+files): raise ValueError('Symlink member cannot be covered')
                    for name in files:
                        path=str(Path(directory)/name)
                        if path in known or self.get('new:'+path): continue
                        self.enqueue({'registration_id':epoch['registration_id'],'action':'file.created','path':path,'gap_epoch':epoch['gap_epoch'],'detail':'New member requires full core inspection'})
                        self.set('new:'+path,True)
            except (OSError,ValueError) as exc: self.gap(str(exc))
        if self.audit: self.collect_audit()

    def collect_audit(self):
        path=Path(os.getenv('SJFX_LINUX_AUDIT_LOG','/var/log/audit/audit.log'))
        receipt={}
        try: receipt=json.loads((self.state/'auditd-installation.json').read_text())
        except (OSError,ValueError): pass
        key=os.getenv('SJFX_LINUX_AUDIT_KEY',receipt.get('key',''))
        self.audit_health=False
        if not key:
            self.audit_error='No selected-file auditd rule key configured';return
        try:
            current=path.stat();cursor=self.get('audit_cursor',{})
            identity=f'{current.st_dev}:{current.st_ino}'
            if not cursor:
                self.set('audit_cursor',{'identity':identity,'offset':current.st_size});self.audit_error='Native log cursor initialized; previous interval unproven';return
            if identity!=cursor['identity'] or current.st_size<cursor['offset']:
                self.gap('auditd log rotated/truncated; access history gap')
                self.set('audit_cursor',{'identity':identity,'offset':0});self.audit_error='Native log gap';return
            with path.open('rb') as stream:
                stream.seek(cursor['offset']);raw=stream.read(4*1024*1024)
            # Consume only complete native groups; incomplete suffix remains durable.
            end=raw.rfind(b'type=EOE ')
            end=raw.find(b'\n',end) if end>=0 else -1
            if end>=0:
                complete=raw[:end+1].decode('utf-8',errors='strict')
                if any(marker in complete for marker in ('type=DAEMON_LOST ','type=DAEMON_ABORT ')):
                    self.gap('Native audit daemon reported event loss');self.audit_error='Native event loss';return
                events=parse_auditd_records(complete.splitlines(),self.bindings(),key)
                for event in events: self.enqueue(event)
                self.set('audit_cursor',{'identity':identity,'offset':cursor['offset']+end+1})
            elif len(raw)==4*1024*1024:
                self.gap('Native audit group exceeds bounded reader capacity');self.audit_error='Incomplete native audit records';return
            expected={(b['local_path'],b['local_identity']) for b in self.bindings()}
            covered={(r.get('path'),r.get('identity')) for r in receipt.get('rules',[])}
            boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            if expected and expected==covered and receipt.get('rules_applied') is True and receipt.get('backend')=='auditd' and receipt.get('key')==key and receipt.get('boot_id')==boot:
                auditctl=shutil.which('auditctl')
                if not auditctl:
                    self.audit_error='auditctl unavailable; cannot verify current selected-file rule coverage';return
                probe=subprocess.run([auditctl,'-l'],capture_output=True,text=True,timeout=3)
                if probe.returncode:
                    self.audit_error='Permission to query current auditd rules unavailable; installed receipt alone is not current coverage proof';return
                native=set()
                for line in probe.stdout.splitlines():
                    parts=shlex.split(line)
                    if '-w' in parts and '-p' in parts and '-k' in parts:
                        native.add((parts[parts.index('-w')+1],frozenset(parts[parts.index('-p')+1]),parts[parts.index('-k')+1]))
                if any((path,frozenset('rwa'),key) not in native for path,_ in expected):
                    self.audit_error='Current native auditd rules do not cover every selected file';return
                for path,identity in expected:
                    info=os.stat(path,follow_symlinks=False)
                    if f'{info.st_dev}:{info.st_ino}'!=identity or not stat.S_ISREG(info.st_mode):
                        self.audit_error='Selected native file instance changed; audit rule receipt is stale';return
                self.audit_health=not self.status()['has_unresolved_gaps']
                self.audit_error='' if self.audit_health else 'Native access coverage has unresolved gaps'
            else:
                self.audit_error='Native log readable; selected-file rule coverage requires current verified installation receipt'
        except (OSError,ValueError,subprocess.TimeoutExpired) as exc: self.audit_error=type(exc).__name__+': native audit evidence unavailable'

    def status(self):
        epochs=self.get('epochs',[]);gap=bool(self.get('queue_gap',False) or any(e.get('unresolved') for e in epochs))
        enabled=self.get('enabled',False)
        return {'ok':True,'local_mode':True,'agent_version':'0.5.0-linux-desktop','device_id':self.get('device_id',''),
            'healthy':enabled and not gap,'directory_monitor_healthy':enabled and not gap,
            'protection_enabled':enabled,'collection_enabled':enabled,'has_unresolved_gaps':gap,
            'monitoring_epochs':epochs,'binding_count':self.db.execute('SELECT COUNT(*) FROM bindings').fetchone()[0],
            'queue_depth':self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],
            'access_audit_requested':self.audit,'access_audit_healthy':self.audit_health,'access_audit_error':self.audit_error,
            'access_audit_backend':'auditd','server_connected':True,'capabilities':['polling_current_version_observation','native_dev_inode','persistent_queue','authenticated_local_process_api','auditd_native_evidence_when_configured'],
            'registration_limits':{'max_request_bytes':REQUEST_LIMIT,'max_files_per_complete_registration':FILE_LIMIT,'max_snapshot_bytes_per_registration':REGISTRATION_LIMIT,'max_files_per_transaction':TRANSACTION_FILE_LIMIT,'max_snapshot_bytes_per_transaction':transaction_byte_limit(),'max_file_bytes':SNAPSHOT_LIMIT,'max_batch_files':500,'partial_registration_is_complete':False,'transaction_registration_supported':True},
            'coverage_limits':['Polling may miss intermediate changes between observations','Native Linux access evidence requires auditd and verified selected-file rules','Read/syscall evidence never establishes upload','File locks and quarantine actions are unsupported by this Linux observer']}

    def sync(self,payload):
        if payload.get('device_id')!=self.get('device_id') or not self.get('device_id'): raise ValueError('Device identity mismatch')
        if set(payload)!={'device_id','protection_enabled','ack_event_ids','ack_result_ids','actions'}: raise ValueError('Invalid sync fields')
        if not isinstance(payload['protection_enabled'],bool) or len(payload['actions'])>100 or any(len(payload[k])>1000 for k in ('ack_event_ids','ack_result_ids')): raise ValueError('Invalid sync capacity')
        if self.get('enabled',False) and not payload['protection_enabled']: self.gap('Protection explicitly disabled')
        self.set('enabled',payload['protection_enabled'])
        for identifier in payload['ack_event_ids']: self.db.execute('DELETE FROM events WHERE id=?',(identifier,))
        for identifier in payload['ack_result_ids']: self.db.execute('DELETE FROM results WHERE id=?',(identifier,))
        self.db.commit();self.observe()
        for action in payload['actions']:
            identifier=action.get('id','')
            if not identifier or len(identifier)>128: raise ValueError('Invalid action id')
            row=self.db.execute('SELECT payload FROM results WHERE id=?',(identifier,)).fetchone()
            if row:
                if json.loads(row[0])['action']!=action: raise ValueError('Action identity reused with different target')
                continue
            result={'action_id':identifier,'action':action,'result':{'status':'rejected','reason':'unsupported_linux_observer_action','performed':False}}
            self.db.execute('INSERT INTO results VALUES(?,?)',(identifier,json.dumps(result)))
        self.db.commit()
        events=[]
        for seq,raw in self.db.execute('SELECT seq,payload FROM events ORDER BY seq LIMIT 1000'):
            events.append({**json.loads(raw),'sequence':seq})
        results=[{'action_id':v['action_id'],'result':v['result']} for (raw,) in self.db.execute('SELECT payload FROM results LIMIT 100') for v in [json.loads(raw)]]
        return {'ok':True,'events':events,'results':results,'health':self.status()}

    def handler(self):
        observer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                code=200
                try:
                    if not ipaddress.ip_address(self.client_address[0]).is_loopback or 'Origin' in self.headers or not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+observer.token): raise PermissionError('Local process authentication failed')
                    length=int(self.headers.get('Content-Length','0'))
                    if length>REQUEST_LIMIT: raise OverflowError('Request exceeds complete-registration capacity')
                    if length<=0 or self.headers.get('Content-Type','').split(';')[0]!='application/json': raise ValueError('Invalid request')
                    payload=json.loads(self.rfile.read(length))
                    with observer.lock:
                        if self.path=='/agent-local/v1/register': reply=observer.register(payload)
                        elif self.path.startswith('/agent-local/v1/register/'): reply=observer.registration_transaction(self.path,payload)
                        elif self.path=='/agent-local/v1/sync': reply=observer.sync(payload)
                        elif self.path=='/agent-local/v1/health': reply=observer.status()
                        else: code=404;reply={'ok':False,'error':'Unknown local interface'}
                except PermissionError: code=403;reply={'ok':False,'error':'Local process authentication failed'}
                except OverflowError: code=413;reply={'ok':False,'code':'local_agent_capacity_exceeded','coverage_complete':False,'limit_files':FILE_LIMIT,'limit_bytes':REQUEST_LIMIT}
                except (OSError,ValueError,KeyError,TypeError): code=409;reply={'ok':False,'error':'Local protocol, identity or stable version rejected'}
                raw=json.dumps(reply).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        return Handler


def main():
    p=argparse.ArgumentParser();p.add_argument('--data',required=True);p.add_argument('--local-token-file',required=True)
    p.add_argument('--listen',default='127.0.0.1:18767');p.add_argument('--access-audit',action='store_true');p.add_argument('--stop-file');p.add_argument('--audit-plan',action='store_true')
    args=p.parse_args()
    if os.name=='nt': raise SystemExit('Linux observer is only for Linux; use native Windows helper')
    if os.getenv('SJFX_CONTROLLER_PID'):
        from services.managed_entry import _watch_parent
        _watch_parent()
    if os.getenv('SJFX_PROCESS_GATE'):
        gate=Path(os.environ['SJFX_PROCESS_GATE']);deadline=time.monotonic()+30
        while not gate.exists() or gate.read_text()!=os.environ['SJFX_PROCESS_TOKEN']:
            if time.monotonic()>deadline: raise RuntimeError('Controller did not acquire helper ownership')
            time.sleep(.05)
    observer=Observer(args.data,args.local_token_file,args.access_audit)
    if args.audit_plan:
        print(json.dumps(linux_rule_plan(observer.bindings(),observer.get('device_id',uuid.uuid4().hex))));return
    host,port=args.listen.rsplit(':',1)
    if host!='127.0.0.1': raise SystemExit('Observer must bind IPv4 loopback')
    server=ThreadingHTTPServer((host,int(port)),observer.handler());server.daemon_threads=True
    threading.Thread(target=server.serve_forever,daemon=True).start();stopping=threading.Event()
    for name in ('SIGTERM','SIGINT'): signal.signal(getattr(signal,name),lambda *_:stopping.set())
    try:
        while not stopping.wait(2):
            if args.stop_file and Path(args.stop_file).exists(): break
            with observer.lock:
                try: observer.observe()
                except (OSError,ValueError): observer.gap('Observation incomplete')
    finally:
        server.shutdown();server.server_close();observer.db.close()


if __name__=='__main__': main()
