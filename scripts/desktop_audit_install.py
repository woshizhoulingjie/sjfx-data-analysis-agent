"""Explicit Linux auditd installation plan/apply/restore; no elevation or downloads.

Run plan/preflight as the SJFX owner. Run reviewed apply/restore in an already
authorized administrator terminal. Rules are live-kernel rules, not distribution
specific persistent boot configuration. Restart invalidates the saved receipt.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sjfx_protection.desktop_audit import preflight,linux_rule_plan
from sjfx_protection.linux_observer import snapshot


def run(command):
    result=subprocess.run(command,capture_output=True,text=True,timeout=15)
    if result.returncode: raise RuntimeError('Native auditctl rejected maintenance request')
    return result.stdout


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['preflight','plan','apply','restore']);p.add_argument('--state',required=True)
    args=p.parse_args();state=Path(args.state).resolve()
    if not sys.platform.startswith('linux'): raise RuntimeError('Linux auditd maintenance is only supported on Linux')
    if args.action=='preflight': print(json.dumps(preflight()));return
    identity=json.loads((state/'installation.json').read_text())
    uid=int(identity['os_owner'].split(':',1)[1])
    db_path=state/'helper'/'observer.db'
    with sqlite3.connect(db_path.as_uri()+'?mode=ro',uri=True) as db:
        bindings=[json.loads(row[0]) for row in db.execute('SELECT payload FROM bindings')]
    plan=linux_rule_plan(bindings,identity['installation_id'])
    if args.action=='plan': print(json.dumps(plan,indent=2));return
    if os.geteuid()!=0: raise RuntimeError('Requires an already authorized administrator terminal; no automatic elevation')
    receipt_path=state/'helper'/'auditd-installation.json';backup=state/'helper'/'auditd-original-rules.json'
    existing=run(['auditctl','-l']).splitlines()
    if args.action=='apply':
        if not plan['rules']: raise RuntimeError('No completely registered files; no audit rules applied')
        expected={f'-w {r["path"]} -p {r["permissions"]} -k {plan["key"]}' for r in plan['rules']}
        old_matching=[line for line in existing if plan['key'] in shlex.split(line)]
        if old_matching and set(old_matching)!=expected: raise RuntimeError('Existing installation key rules differ; refuse unrelated overwrite')
        if not backup.exists():
            fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as stream: json.dump({'original_rules':existing,'plan':plan,'created':time.time()},stream);stream.flush();os.fsync(stream.fileno())
            os.chown(backup,uid,-1)
        # Durable planned receipt precedes native changes so partial install can be restored.
        receipt={'backend':'auditd','key':plan['key'],'rules_applied':False,'rules':[],
                 'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'created':time.time()}
        for b in bindings:
            if snapshot(b['local_path'])['identity']!=b['local_identity']: raise RuntimeError('Selected instance changed before native rule install')
            receipt['rules'].append({'path':b['local_path'],'identity':b['local_identity']})
        receipt_path.write_text(json.dumps(receipt));os.chmod(receipt_path,0o600);os.chown(receipt_path,uid,-1)
        for rule in plan['rules']:
            if f'-w {rule["path"]} -p {rule["permissions"]} -k {plan["key"]}' not in existing:
                run(['auditctl','-w',rule['path'],'-p',rule['permissions'],'-k',rule['key']])
        current=run(['auditctl','-l']).splitlines()
        if not expected.issubset(set(current)): raise RuntimeError('Native selected-file rule read-back incomplete; receipt stays incomplete')
        receipt['rules_applied']=True;receipt_path.write_text(json.dumps(receipt));os.chmod(receipt_path,0o600)
        print(json.dumps({'applied':True,'key':plan['key'],'receipt':str(receipt_path),'log_access_preflight':preflight(),'persistence':'live kernel only; boot restart requires reinstallation'}))
    else:
        if not backup.exists(): print(json.dumps({'restored':True,'changed':False}));return
        saved=json.loads(backup.read_text());original=saved['original_rules']
        for rule in saved['plan']['rules']:
            exact=f'-w {rule["path"]} -p {rule["permissions"]} -k {rule["key"]}'
            if exact in original or exact not in existing: continue
            run(['auditctl','-W',rule['path'],'-p',rule['permissions'],'-k',rule['key']])
        if receipt_path.exists(): receipt_path.rename(receipt_path.with_suffix('.restored.json'))
        backup.rename(backup.with_suffix('.restored.json'))
        print(json.dumps({'restored':True,'preserved_unrelated_rules':True}))


if __name__=='__main__':
    try: main()
    except (RuntimeError,OSError,ValueError) as exc: print(json.dumps({'ok':False,'error':str(exc)}));raise SystemExit(1)
