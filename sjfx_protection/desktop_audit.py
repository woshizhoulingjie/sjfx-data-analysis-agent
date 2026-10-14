"""Native audit permissions and Linux auditd evidence. Never changes global policy.

auditd support requires administrator-installed selected-file rules and a readable
log. Reading a file is not evidence of upload. Historical byte hashes are unknown.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time


def preflight(log_path=None):
    if os.name == 'nt':
        script = "$ErrorActionPreference='Stop'; try { $e=Get-WinEvent -LogName Security -MaxEvents 1; @{security_log_readable=$true;latest_record_id=$e.RecordId}|ConvertTo-Json -Compress } catch { @{security_log_readable=$false;reason='Security log permission unavailable'}|ConvertTo-Json -Compress }"
        from .desktop_runtime import windows_powershell
        result = subprocess.run([windows_powershell(),'-NoProfile','-NonInteractive','-Command',script],
                                capture_output=True,text=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
        try: report=json.loads(result.stdout.lstrip('\ufeff'))
        except ValueError: report={'security_log_readable':False,'reason':'Security log preflight failed'}
        return {'platform':'windows','backend':'Security 4656/4663 + selected-file SACL',
                'healthy':False,'coverage_complete':False,'configuration_required':True,**report}
    path=Path(log_path or os.getenv('SJFX_LINUX_AUDIT_LOG','/var/log/audit/audit.log'))
    readable=False
    try:
        with path.open('rb'): readable=True
    except OSError: pass
    return {'platform':'linux','backend':'auditd selected-file rules',
            'auditctl_available':bool(shutil.which('auditctl')),'audit_log_readable':readable,
            'audit_log':str(path),'healthy':False,'coverage_complete':False,
            'configuration_required':True,
            'reason':'Require installed auditd, selected-file rules and readable complete event log; preflight is not coverage proof'}


_FIELD = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|[^\s]+)')
_STAMP = re.compile(r'msg=audit\((\d+(?:\.\d+)?):(\d+)\)')


def parse_auditd_records(lines, bindings, key):
    """Parse complete auditd multi-record groups, preserving native identifiers."""
    selected={str(Path(b['local_path'])):b for b in bindings}
    groups={}
    for line in lines:
        stamp=_STAMP.search(line)
        if not stamp: continue
        fields={k:v.strip('"') for k,v in _FIELD.findall(line)}
        group=groups.setdefault((stamp[1],stamp[2]),{})
        group.setdefault(fields.get('type',''),[]).append(fields)
    events=[]
    for (occurred,serial),group in groups.items():
        # Partial records at a tail are retained by the caller until EOE appears.
        if 'EOE' not in group: continue
        for syscall in group.get('SYSCALL',[]):
            if syscall.get('key') != key: continue
            for item in group.get('PATH',[]):
                path=item.get('name','')
                if re.fullmatch('[0-9a-fA-F]+',path) and len(path)%2==0:
                    try: path=bytes.fromhex(path).decode('utf-8')
                    except ValueError: pass
                if path not in selected: continue
                b=selected[path]
                events.append({'event_id':f'auditd-{occurred}-{serial}-{b["binding_id"]}',
                    'binding_id':b['binding_id'],'registration_id':b['registration_id'],
                    'action':'file.access_requested','path':path,'process':syscall.get('exe',''),
                    'process_id':syscall.get('pid',''),'user':syscall.get('uid',''),
                    'occurred':float(occurred),'gap_epoch':b['gap_epoch'],
                    'version_id':b['version_id'],'identity':b['local_identity'],'sha256':b['sha256'],
                    'version_evidence':'current_binding_at_collection_not_event_content_proof',
                    'access_mask':syscall.get('perm',''),'native_event_id':serial,
                    'detail':json.dumps({'source':'auditd','syscall':syscall.get('syscall'),
                        'arch':syscall.get('arch'),'success':syscall.get('success'),
                        'exit':syscall.get('exit'),'audit_uid':syscall.get('auid'),
                        'native_device':item.get('dev'),'native_inode':item.get('inode'),
                        'semantics':'requested syscall and path evidence; no upload or exact byte proof'})})
    return events


def linux_rule_plan(bindings, installation_id):
    """Reviewable plan; the installer applies/removes only this installation's rules."""
    key='sjfx_'+re.sub('[^a-zA-Z0-9]','',installation_id)[:24]
    paths=sorted({b['local_path'] for b in bindings})
    if any('\n' in p or '\r' in p or not Path(p).is_absolute() for p in paths):
        raise ValueError('Audit rules require literal absolute paths without newlines')
    return {'key':key,'rules':[{'path':p,'permissions':'rwa','key':key} for p in paths],
            'global_policy_change':False,'restore':'remove only matching saved rules; preserve unrelated rules',
            'requires':'administrator performs explicit installation; Web remains an ordinary user'}
