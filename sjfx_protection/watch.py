"""Optional, separate-process version observer for registered server collections.

This is a snapshot verifier, NOT a kernel access auditor. No web/model processes
are loaded. Invalidated collections require re-registration and are not auto-approved.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from .store import Store
from .worker import verify_inventory


def observe_once(store, *, interval=60, limit=20):
    from .policy import protection_enabled
    if not protection_enabled(store.directory):return 0
    interval = max(30, interval)
    with store.db() as db:
        rows = db.execute(
            "SELECT c.id FROM collections c LEFT JOIN settings s ON s.key='verified:'||c.id "
            "WHERE c.state='ready' AND COALESCE(CAST(s.value AS REAL),0)<? "
            "ORDER BY COALESCE(CAST(s.value AS REAL),0),c.created LIMIT ?",
            (time.time()-interval, min(max(limit, 1), 100))).fetchall()
    for row in rows:
        verify_inventory(store, row['id'])
    with store.db(True) as db:
        db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('package-watch-heartbeat',str(time.time())))
        # NAS failures are reported as unavailable, not inferred deletions.
        store._event(db,'system','server.watch_tick',payload={'verified_count':len(rows),'coverage':'registered_server_inventory'})
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--packages',action='store_true',help='Run durable package job supervisor and inventory watch')
    parser.add_argument('--native-registration',help='Verify one native file or directory in this isolated process')
    parser.add_argument('--allowed-root',action='append',default=[])
    args = parser.parse_args()
    if not (Path(args.state) / 'protection.db').is_file():
        parser.error('必须指定现有文件保护数据库目录；不会创建生产数据库')
    store = Store(args.state)
    interval = max(30, args.interval)
    if args.native_registration:
        from .registration import PackageProtection
        from .business import BusinessAdapter
        from config import Config
        adapter=BusinessAdapter(Config.DB_PATH,Config.DATA_DIR/'large_package.db',Config.OUTPUT_DIR)
        service=PackageProtection(store,args.allowed_root or Config.SCAN_ALLOWED_ROOTS,authorize_root=adapter.authorize_root,authorize_owner=adapter.authorize_owner)
        with store.db() as db:reg=db.execute('SELECT * FROM registrations WHERE id=?',(args.native_registration,)).fetchone()
        if reg and reg['state']=='ready':
            try:
                service.validate_local_manifest(reg['owner'],dict(reg))
                with store.db(True) as db:db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('verified:'+reg['collection_id'],str(time.time())))
            except Exception:logging.getLogger(__name__).info('Native source needs recheck',exc_info=True)
        return
    if args.packages:
        package_service(store,args,interval)
        return
    while True:
        count = observe_once(store, interval=interval, limit=args.limit)
        logging.getLogger(__name__).info('Verified %s registered collections', count)
        if args.once:
            return
        time.sleep(interval)

def package_service(store,args,interval):
    """Independent process supervisor; never imports models or the SJFX app."""
    from .policy import read_policy
    from .registration import PackageProtection
    from .business import BusinessAdapter
    from .inspection import refresh_runtime_engines
    from config import Config
    adapter=BusinessAdapter(Config.DB_PATH,Config.DATA_DIR/'large_package.db',Config.OUTPUT_DIR)
    allowed=args.allowed_root or Config.SCAN_ALLOWED_ROOTS
    service=PackageProtection(store,allowed,authorize_root=adapter.authorize_root,authorize_owner=adapter.authorize_owner)
    from .local_agent import LocalAgent
    local_agent=LocalAgent(service)
    if args.once:
        if service.enabled:
            service.process_job();observe_once(store,interval=interval,limit=args.limit)
        return
    children={};next_verify=0;next_prune=0;next_engines=0
    kwargs={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
    try:
        while service.enabled or local_agent.configured:
            local_agent.sync()
            if not service.enabled:
                for key,(process,started,timeout) in list(children.items()):
                    if process.poll() is None:process.terminate()
                    children.pop(key)
                time.sleep(2)
                continue
            policy=service._active();now=time.time()
            # Both OS helpers and the inventory verifier invalidate coverage.
            # Restart recreates a durable recheck instead of granting old work
            # a fresh context or leaving new directory members unknown forever.
            with store.db() as db:
                stale=db.execute("SELECT DISTINCT r.id,r.owner FROM registrations r JOIN registration_devices d ON d.registration_id=r.id WHERE r.mode='local' AND (r.state='changed' OR (r.state='unavailable' AND r.updated<?) OR (r.state='ready' AND (d.state!='verified' OR d.gap_epoch!=d.verified_epoch)))",(now-interval,)).fetchall()
            for row in stale:service.schedule_local_recheck(row['owner'],row['id'])
            if now>=next_engines:refresh_runtime_engines(store);next_engines=now+60
            with store.db(True) as db:
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('package-watch-heartbeat',str(now)))
            for key,(process,started,timeout) in list(children.items()):
                if process.poll() is not None:children.pop(key);continue
                if key.startswith('job:'):
                    with store.db() as db:job=db.execute('SELECT state,lease_until FROM protection_jobs WHERE id=?',(key[4:],)).fetchone()
                    if job and (job['state']=='cancelled' or (job['state']=='running' and job['lease_until']<now)):
                        process.terminate();children.pop(key)
                        with store.db(True) as db:
                            db.execute("UPDATE protection_jobs SET state=CASE WHEN attempts<max_attempts THEN 'queued' ELSE 'failed' END,error='子进程租约过期，需恢复核验',lease_until=0,lease_owner='',updated=? WHERE id=? AND state='running'",(now,key[4:]))
                        continue
                if now-started>timeout:
                    process.terminate();children.pop(key)
                    with store.db(True) as db:
                        if key.startswith('job:'):
                            db.execute("UPDATE protection_jobs SET state=CASE WHEN attempts<max_attempts THEN 'queued' ELSE 'failed' END,error='子进程超过核验时间预算',lease_until=0,lease_owner='',updated=? WHERE id=? AND state='running'",(now,key[4:]))
                        else:
                            db.execute("UPDATE collections SET state='failed',error='服务器核验超时',updated=? WHERE id=?",(now,key[7:]))
                            db.execute("UPDATE registrations SET state='unavailable',error='服务器核验超时',updated=? WHERE collection_id=?",(now,key[7:]))
            slots=max(1,min(4,int(policy['inspection_profile'].get('worker_concurrency',2))))-len(children)
            if slots>0:
                with store.db() as db:
                    jobs=db.execute("SELECT id FROM protection_jobs WHERE state='queued' OR (state='running' AND lease_until<? AND attempts<max_attempts) ORDER BY created LIMIT ?",(now,slots)).fetchall()
                for job in jobs:
                    key='job:'+job['id']
                    if key in children:continue
                    command=[sys.executable,'-m','sjfx_protection.worker','--state',str(store.directory),'--job',job['id']]
                    for root in allowed:command.extend(['--allowed-root',str(root)])
                    children[key]=(subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**kwargs),now,policy['inspection_profile'].get('inventory_timeout_seconds',7200));slots-=1
            if now>=next_verify and slots>0:
                with store.db() as db:
                    rows=db.execute("SELECT c.id,r.id AS registration_id,r.mode FROM collections c JOIN registrations r ON r.collection_id=c.id LEFT JOIN settings s ON s.key='verified:'||c.id WHERE c.state='ready' AND r.state='ready' AND COALESCE(CAST(s.value AS REAL),0)<? ORDER BY COALESCE(CAST(s.value AS REAL),0) LIMIT ?",(now-interval,slots)).fetchall()
                for row in rows:
                    key='verify:'+row['id']
                    if key in children:continue
                    command=([sys.executable,'-m','sjfx_protection.watch','--state',str(store.directory),'--native-registration',row['registration_id'],'--once'] if row['mode']=='local' else [sys.executable,'-m','sjfx_protection.worker','--state',str(store.directory),'--collection',row['id'],'--verify'])
                    for root in allowed:command.extend(['--allowed-root',str(root)])
                    children[key]=(subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**kwargs),now,policy['inspection_profile'].get('inventory_timeout_seconds',7200))
                next_verify=now+5
            if now>=next_prune:service.prune_snapshots();next_prune=now+300
            if args.once:return
            time.sleep(2)
    finally:
        for process,started,timeout in children.values():
            if process.poll() is None:process.terminate()


if __name__ == '__main__':
    main()
