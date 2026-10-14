"""Desktop protection installation identity and supervisor-owned executor."""
from __future__ import annotations
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler
import uuid


def windows_powershell():
    """Use the Windows component even with an application-only PATH."""
    return str(Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32'/'WindowsPowerShell'/'v1.0'/'powershell.exe')


def protect_private_path(path):
    """Restrict an installation-owned path to its current OS user."""
    path=Path(path)
    if os.name=='nt':
        script="$ErrorActionPreference='Stop'; $p=$env:SJFX_PRIVATE_PATH; $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User; $dir=[IO.Directory]::Exists($p); if($dir){$a=[IO.Directory]::GetAccessControl($p)}else{$a=[IO.File]::GetAccessControl($p)}; $a.SetAccessRuleProtection($true,$false); foreach($r in @($a.Access)){$a.RemoveAccessRuleSpecific($r)}; if($dir){$r=[Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')}else{$r=[Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','Allow')}; $a.AddAccessRule($r); if($dir){[IO.Directory]::SetAccessControl($p,$a)}else{[IO.File]::SetAccessControl($p,$a)}"
        env={**os.environ,'SJFX_PRIVATE_PATH':str(path)}
        encoded=base64.b64encode(script.encode('utf-16le')).decode('ascii')
        result=subprocess.run([windows_powershell(),'-NoProfile','-NonInteractive','-EncodedCommand',encoded],env=env,capture_output=True,text=True,errors='replace',timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode: raise RuntimeError('Cannot restrict installation credentials to current Windows user')
    else:
        if path.stat().st_uid!=os.getuid(): raise RuntimeError('Installation state belongs to another OS user')
        os.chmod(path,0o700 if path.is_dir() else 0o600)


def current_owner():
    if os.name!='nt': return 'uid:'+str(os.getuid())
    result=subprocess.run([windows_powershell(),'-NoProfile','-NonInteractive','-Command','[Security.Principal.WindowsIdentity]::GetCurrent().User.Value'],capture_output=True,text=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode or not result.stdout.strip().startswith('S-1-'): raise RuntimeError('Cannot establish Windows installation owner')
    return result.stdout.strip()


def prepare_environment(config):
    """Fresh formal state; never adopt a hand-run acceptance helper directory."""
    # A test-shell override must never reach the formally managed installation.
    os.environ.pop('SJFX_PROTECTION_TEST_STATIC_ONLY',None)
    root=Path(config.DATA_DIR)/'desktop_protection';root.mkdir(parents=True,exist_ok=True,mode=0o700)
    if root.is_symlink(): raise RuntimeError('Desktop protection state cannot be a symbolic link')
    protect_private_path(root)
    identity_path=root/'installation.json';owner=current_owner()
    if identity_path.exists():
        identity=json.loads(identity_path.read_text(encoding='utf-8'))
        if identity.get('os_owner')!=owner or identity.get('purpose')!='sjfx-formal-desktop': raise RuntimeError('Formal desktop installation owner/purpose mismatch')
    else:
        helper_state=root/'helper'
        if helper_state.exists() and any(helper_state.iterdir()): raise RuntimeError('Refuse adoption of pre-existing helper acceptance state')
        identity={'installation_id':uuid.uuid4().hex,'purpose':'sjfx-formal-desktop','os_owner':owner,'created':time.time()}
        with identity_path.open('x',encoding='utf-8') as stream: json.dump(identity,stream)
        protect_private_path(identity_path)
    token=root/'local-process-token'
    if not token.exists():
        with token.open('x',encoding='ascii') as stream: stream.write(secrets.token_hex(32))
    protect_private_path(token)
    helper=root/'helper';helper.mkdir(exist_ok=True,mode=0o700);protect_private_path(helper)
    core=Path(config.DATA_DIR)/'file_protection';core.mkdir(exist_ok=True,mode=0o700);protect_private_path(core)
    from .policy import policy_path,write_policy
    if not policy_path(core).exists():
        write_policy(core,'desktop-installer:'+identity['installation_id'],True,{'enabled':True},'New desktop installation: protection enabled by default')
    endpoint=os.getenv('SJFX_LOCAL_AGENT_ENDPOINT','http://127.0.0.1:18767')
    parsed=urlsplit(endpoint)
    if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or parsed.path not in ('','/') or parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.port:
        raise RuntimeError('Desktop helper endpoint must be an explicit IPv4 loopback HTTP port')
    os.environ.update(SJFX_LOCAL_AGENT_TOKEN_FILE=str(token),SJFX_LOCAL_AGENT_ENDPOINT=endpoint.rstrip('/'),
        SJFX_DESKTOP_MANAGED_PROTECTION='1',SJFX_PROTECTION_MODE='enforce',SJFX_PROTECTION_ALLOW_ENFORCEMENT='1',
        HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
    stop=root/'helper.stop';stop.unlink(missing_ok=True)
    scan_work=Path(os.getenv('SJFX_PROTECTION_SCAN_WORKDIR',str(root/'scan-work')))
    scan_work.mkdir(parents=True,exist_ok=True,mode=0o700);protect_private_path(scan_work)
    os.environ['SJFX_PROTECTION_SCAN_WORKDIR']=str(scan_work)
    return {'installation_id':identity['installation_id'],'state':str(root),'helper_state':str(helper),
        'core_state':str(core),'token_file':str(token),'endpoint':endpoint.rstrip('/'),'stop_file':str(stop)}


def helper_command(paths):
    port=urlsplit(paths['endpoint']).port
    flags=['--data',paths['helper_state'],'--local-token-file',paths['token_file'],'--listen',f'127.0.0.1:{port}','--stop-file',paths['stop_file']]
    if os.getenv('SJFX_DESKTOP_ACCESS_AUDIT','1')=='1': flags.append('--access-audit')
    if os.name=='nt':
        binary=os.getenv('SJFX_DESKTOP_HELPER_BINARY') or str(Path(__file__).resolve().parents[2]/'agent'/'bin'/'sjfx-assistant.exe')
        if not Path(binary).is_file(): raise RuntimeError('Windows protection helper is not installed; set SJFX_DESKTOP_HELPER_BINARY')
        return [binary,'--local',*flags]
    if not sys.platform.startswith('linux'): raise RuntimeError('Desktop protection currently supports Windows and Linux')
    return [sys.executable,'-B','-X','utf8','-m','sjfx_protection.linux_observer',*flags]


def helper_health(paths):
    token=Path(paths['token_file']).read_text(encoding='ascii').strip()
    request=Request(paths['endpoint']+'/agent-local/v1/health',data=b'{}',headers={'Content-Type':'application/json','Authorization':'Bearer '+token},method='POST')
    from .local_agent import NoRedirect
    with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=2) as response:
        raw=response.read(8*1024*1024+1)
        if len(raw)>8*1024*1024: raise RuntimeError('Helper health response exceeds bounded capacity')
        result=json.loads(raw)
    if not isinstance(result,dict) or result.get('ok') is not True: raise RuntimeError('Helper startup health protocol rejected')
    return result


def health_report(config,paths=None):
    root=Path(config.DATA_DIR)/'desktop_protection'
    paths=paths or {'token_file':str(root/'local-process-token'),'endpoint':os.getenv('SJFX_LOCAL_AGENT_ENDPOINT','http://127.0.0.1:18767'),'core_state':str(Path(config.DATA_DIR)/'file_protection')}
    from .policy import read_policy
    report={'enabled':read_policy(paths['core_state'])['enabled'],'supported_platform':os.name=='nt' or sys.platform.startswith('linux'),'model_auto_download':False}
    try:
        health=helper_health(paths)
        report['helper']={'running':True,'directory_monitor_healthy':health.get('directory_monitor_healthy',False),'access_audit_healthy':health.get('access_audit_healthy',False),'access_audit_requested':health.get('access_audit_requested',False),'access_audit_error':health.get('access_audit_error',''),'has_unresolved_gaps':health.get('has_unresolved_gaps',False),'registration_limits':health.get('registration_limits',{}),'coverage_limits':health.get('coverage_limits',[]),'active_dispositions':health.get('active_dispositions',0)}
    except (OSError,ValueError,RuntimeError): report['helper']={'running':False,'directory_monitor_healthy':False,'access_audit_healthy':False}
    ready=root/'executor-health.json'
    try:
        state=json.loads(ready.read_text());report['executor']={**state,'healthy':time.time()-state.get('heartbeat',0)<10}
    except (OSError,ValueError): report['executor']={'healthy':False}
    return report


def executor(args):
    from .store import Store
    from .registration import PackageProtection
    from .watch import package_service
    from config import Config
    if os.name!='nt' and os.getenv('SJFX_CONTROLLER_PID'):
        from services.managed_entry import _watch_parent
        _watch_parent()
    gate=Path(os.environ['SJFX_PROCESS_GATE']);token=os.environ['SJFX_PROCESS_TOKEN']
    deadline=time.monotonic()+30
    while not gate.exists() or gate.read_text()!=token:
        if time.monotonic()>deadline: raise RuntimeError('Controller did not acquire executor ownership')
        time.sleep(.05)
    store=Store(args.state);PackageProtection(store,Config.SCAN_ALLOWED_ROOTS)._ensure()
    health_path=Path(Config.DATA_DIR)/'desktop_protection'/'executor-health.json'
    import threading
    def heartbeat():
        engines={};next_engines=0
        while True:
            from .inspection import refresh_runtime_engines
            try:
                if time.time()>=next_engines:
                    engines=refresh_runtime_engines(store);next_engines=time.time()+60
                payload={'heartbeat':time.time(),'pid':os.getpid(),'engine_observation':engines,'engine_observation_scope':'runtime/database metadata only; completed real scan receipt is required for content health','scope':'protection executor; model readiness independent'}
                temp=health_path.with_suffix('.tmp');temp.write_text(json.dumps(payload));temp.replace(health_path)
            except Exception: pass
            time.sleep(3)
    threading.Thread(target=heartbeat,daemon=True).start()
    Path(os.environ['SJFX_ROLE_READY_FILE']).write_text(token,encoding='ascii')
    args.allowed_root=[str(p) for p in Config.SCAN_ALLOWED_ROOTS];args.limit=20;args.once=False
    package_service(store,args,60)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('role',choices=['executor']);parser.add_argument('--state',required=True)
    executor(parser.parse_args())
