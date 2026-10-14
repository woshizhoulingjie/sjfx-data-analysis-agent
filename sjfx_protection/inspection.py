"""Persistent bounded snapshots and inspection evidence, distinct from human review."""
from __future__ import annotations
import hashlib
import json
import os
import re
import time
from pathlib import Path
from .scanner import stable_hash, inspect_file
from .store import ProtectionError, digest, encode, uid

def profile_key(profile,name):
    from .scanner import STREAMING_REVISION
    from sjfx_local_protection.risk import REVISION
    from sjfx_local_protection.mail_risk import REVISION as MAIL_REVISION
    from sjfx_local_protection.antivirus import runtime_identity
    # Actual installed rules and database content, not administrator labels.
    return digest(encode({'profile':profile,'suffix':Path(name).suffix.lower(),
        'format_context':Path(name).name,'static_revision':REVISION,'streaming_revision':STREAMING_REVISION,'mail_revision':MAIL_REVISION,
        'clamav_runtime':runtime_identity()}))

def effective_profile(store,profile):
    result=dict(profile)
    with store.db() as db:row=db.execute("SELECT value FROM settings WHERE key='inspection-engine-revision'").fetchone()
    result['runtime_engines']=json.loads(row[0]) if row else {'clamav':'not_observed','yara_rules':'not_observed'}
    return result

def refresh_runtime_engines(store):
    """Background-only engine/database version discovery; web only queries evidence."""
    from sjfx_local_protection.antivirus import runtime_identity
    import subprocess
    actual=runtime_identity()
    signature={'clamav':'unavailable','clamav_identity':actual,
               'clamd_socket':os.getenv('SJFX_PROTECTION_CLAMD_SOCKET',''),
               'clamd_database_revision':'unsupported_receipt'}
    if actual['status']=='available':
        try:
            command=[actual['executable'],'--database=.','--version']
            if actual.get('certificates'):command.append('--cvdcertsdir='+actual['certificates'])
            output=subprocess.run(command,
                cwd=actual['database'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,timeout=10,check=False)
            text=output.stdout.decode('utf-8','replace').strip()
            signature['clamav']=text if output.returncode==0 and len(output.stdout)<=4000 and re.fullmatch(r'ClamAV\s+[0-9][\w.-]*/[0-9]+/[^\r\n]+',text) else 'version_unavailable'
        except (OSError,subprocess.TimeoutExpired):signature['clamav']='version_unavailable'
    rules=os.getenv('SJFX_PROTECTION_YARA_RULES')
    if rules and Path(rules).is_file():
        hasher=hashlib.sha256()
        with Path(rules).open('rb') as incoming:
            for block in iter(lambda:incoming.read(1024*1024),b''):hasher.update(block)
        signature['yara_rules']=hasher.hexdigest()
    else:signature['yara_rules']='unconfigured'
    with store.db(True) as db:db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('inspection-engine-revision',encode(signature)))
    return signature

def create_snapshot(store,source,profile,expected=None,heartbeat=None):
    source=Path(source)
    if source.is_symlink():raise ProtectionError('符号链接不能完成核验',423,'inspection_incomplete')
    if source.stat(follow_symlinks=False).st_size>profile['hash_max_bytes']:raise ProtectionError('文件超过哈希容量',423,'hash_limit')
    before=stable_hash(source)
    if before['size']>profile['hash_max_bytes']:raise ProtectionError('文件超过哈希容量',423,'hash_limit')
    if expected and (before['sha256']!=expected['sha256'] or before['identity']!=expected['identity']):
        raise ProtectionError('文件版本已变化',423,'content_changed')
    from .capacity import assert_managed_path
    directory=assert_managed_path(store,store.directory/'snapshots');directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=assert_managed_path(store,directory/(before['sha256']+'-'+digest(source.suffix.lower())[:8]+source.suffix.lower()))
    if target.exists():
        if stable_hash(target)['sha256']!=before['sha256']:
            raise ProtectionError('受限快照已损坏',423,'snapshot_changed')
        return before,str(target)
    temp=assert_managed_path(store,directory/('.pending-'+uid()))
    reservation='snapshot-reservation:'+temp.name
    with store.db(True) as db:
        reservations=[(r['key'],json.loads(r['value'])) for r in db.execute("SELECT * FROM settings WHERE key LIKE 'snapshot-reservation:%' OR key LIKE 'capacity:%'")]
        reserved=0
        for key,value in reservations:
            if value['expires']<time.time():db.execute('DELETE FROM settings WHERE key=?',(key,));continue
            pending=Path(value['path'])
            occupied=pending.stat().st_size if pending.is_file() else 0
            reserved+=max(0,value['bytes']-occupied)
        from .capacity import managed_files
        total=sum(p.stat().st_size for p in managed_files(store))
        if total+reserved+before['size']>profile['snapshot_quota_bytes']:
            raise ProtectionError('受限快照配额不足',423,'snapshot_quota')
        db.execute('INSERT INTO settings VALUES(?,?)',(reservation,encode({'path':str(temp),'bytes':before['size'],'expires':time.time()+600})))
    flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_BINARY',0)
    try:
        hasher=hashlib.sha256();size=0;last=time.monotonic()
        assert_managed_path(store,temp)
        with os.fdopen(os.open(source,flags),'rb') as incoming, temp.open('xb') as output:
            for block in iter(lambda:incoming.read(1024*1024),b''):
                output.write(block);hasher.update(block);size+=len(block)
                if size>profile['hash_max_bytes']:raise ProtectionError('哈希容量超限',423,'hash_limit')
                if time.monotonic()-last>5:
                    if heartbeat:heartbeat({'bytes':size})
                    with store.db(True) as db:db.execute('UPDATE settings SET value=? WHERE key=?',(encode({'path':str(temp),'bytes':before['size'],'expires':time.time()+600}),reservation))
                    last=time.monotonic()
            output.flush();os.fsync(output.fileno())
        after=stable_hash(source)
        if before!=after or hasher.hexdigest()!=before['sha256'] or size!=before['size']:
            raise ProtectionError('建立快照期间文件发生变化',423,'content_changed')
        assert_managed_path(store,temp);assert_managed_path(store,target)
        os.replace(temp,target)
        if os.name!='nt':os.chmod(target,0o400)
        return before,str(target)
    finally:
        try:
            if temp.exists():assert_managed_path(store,temp);temp.unlink()
        finally:
            with store.db(True) as db:db.execute('DELETE FROM settings WHERE key=?',(reservation,))

def inspect_snapshot(path,profile,name=None):
    static_keys=('max_expanded_bytes','max_members','max_depth','max_html_bytes','max_findings',
                 'max_ratio','max_decoder_memory_bytes','mail_brand_domains')
    return inspect_file(path,name=name,max_bytes=profile['static_max_bytes'],
        static_policy={key:profile[key] for key in static_keys if key in profile},
        virus_max_bytes=profile['virus_max_bytes'],timeout=profile['large_timeout_seconds'],
        required_engines=profile['required_engines'],
        clam=os.getenv('SJFX_PROTECTION_CLAM'),yara=os.getenv('SJFX_PROTECTION_YARA'),
        yara_rules=os.getenv('SJFX_PROTECTION_YARA_RULES'),rspamd=os.getenv('SJFX_PROTECTION_RSPAMD'))

def evidence(store,path,name,version,profile):
    key=profile_key(profile,name);now=time.time()
    with store.db() as db:
        row=db.execute('SELECT * FROM inspections WHERE version_id=? AND cache_key=? AND expires>? ORDER BY created DESC LIMIT 1',(version,key,now)).fetchone()
    if row:return dict(row)
    result=inspect_snapshot(path,profile,name)
    av=result['engines'].get('clamav_receipt',{})
    inspected_hash=result.get('sha256') or av.get('input_sha256')
    if inspected_hash is None:inspected_hash=stable_hash(path)['sha256']
    if inspected_hash!=version or (av.get('input_sha256') and av['input_sha256']!=version):
        raise ProtectionError('检查快照已变化',423,'snapshot_changed')
    result.setdefault('coverage',{})['engine_versions']=profile.get('runtime_engines',{})
    result['coverage']['configured_virus_db_revision']=profile.get('virus_db_revision','unknown')
    result['coverage']['actual_virus_db_revision']=av.get('revision','unknown')
    result['coverage']['actual_database_fingerprint']=av.get('database_identity',{}).get('fingerprint','unknown')
    result['coverage']['actual_certificate_fingerprint']=av.get('certificate_identity',{}).get('fingerprint','engine_default')
    from sjfx_local_protection.antivirus import receipt_complete
    if ('clamav' in profile.get('required_engines',[]) or result['engines'].get('clamav')=='completed') and not receipt_complete(av):
        result['coverage']['complete']=False
        result['coverage'].setdefault('incomplete_reasons',[]).append('antivirus_receipt_incomplete')
        if result['risk']!='dangerous':result['risk']='incomplete'
    if profile_key(profile,name)!=key:
        result['coverage']['complete']=False
        result['coverage'].setdefault('incomplete_reasons',[]).append('inspection_configuration_changed')
        if result['risk']!='dangerous':result['risk']='incomplete'
    # All format and original filename checks above are version-bound.
    identifier=uid();expires=now+profile['inspection_ttl_seconds']
    with store.db(True) as db:
        db.execute('INSERT INTO inspections VALUES(?,?,?,?,?,?,?,?,?,?)',
            (identifier,version,key,name,result['risk'],encode({**result.get('coverage',{}),'members':result.get('members',[])}),encode(result['engines']),encode(result['findings']),now,expires))
    return {'id':identifier,'version_id':version,'cache_key':key,'risk':result['risk'],
            'coverage':encode({**result.get('coverage',{}),'members':result.get('members',[])}),'engines':encode(result['engines']),
            'findings':encode(result['findings']),'created':now,'expires':expires}
