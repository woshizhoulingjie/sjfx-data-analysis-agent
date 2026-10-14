"""Durable jobs with expiring leases. A lost process never implies completion."""
from __future__ import annotations
import json
import time
from .store import uid, encode, ProtectionError

JOBS_SCHEMA='''
CREATE TABLE IF NOT EXISTS protection_jobs (
 id TEXT PRIMARY KEY,owner TEXT NOT NULL,registration_id TEXT NOT NULL,kind TEXT NOT NULL,
 payload TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,
 lease_owner TEXT NOT NULL DEFAULT '',lease_until REAL NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0,max_attempts INTEGER NOT NULL DEFAULT 3,
 progress TEXT NOT NULL DEFAULT '{}',error TEXT NOT NULL DEFAULT '',result TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS protection_jobs_claim ON protection_jobs(state,lease_until,created);
'''

class Jobs:
    def __init__(self,store):self.store=store
    def enqueue(self,owner,registration_id,kind,payload):
        identifier=uid();now=time.time()
        with self.store.db(True) as db:
            db.execute('INSERT INTO protection_jobs(id,owner,registration_id,kind,payload,state,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                (identifier,owner,registration_id,kind,encode(payload),'queued',now,now))
        return self.get(owner,identifier)
    def get(self,owner,identifier):
        with self.store.db() as db:row=db.execute('SELECT * FROM protection_jobs WHERE id=? AND owner=?',(identifier,owner)).fetchone()
        if not row:raise ProtectionError('保护任务不存在',404,'job_not_found')
        result=dict(row)
        for key in ('payload','progress','result'):result[key]=json.loads(result[key])
        return result
    def claim(self,worker,identifier=None,lease_seconds=90):
        now=time.time()
        with self.store.db(True) as db:
            db.execute("UPDATE protection_jobs SET state='failed',error='任务租约过期且重试次数耗尽',updated=? WHERE state='running' AND lease_until<? AND attempts>=max_attempts",(now,now))
            where="(state='queued' OR (state='running' AND lease_until<?)) AND attempts<max_attempts"
            params=[now]
            if identifier:where+=' AND id=?';params.append(identifier)
            row=db.execute('SELECT * FROM protection_jobs WHERE '+where+' ORDER BY created LIMIT 1',params).fetchone()
            if not row:return None
            db.execute("UPDATE protection_jobs SET state='running',lease_owner=?,lease_until=?,attempts=attempts+1,updated=? WHERE id=?",(worker,now+lease_seconds,now,row['id']))
        return self.get(row['owner'],row['id'])
    def heartbeat(self,identifier,worker,progress=None,lease_seconds=90):
        with self.store.db(True) as db:
            cursor=db.execute("UPDATE protection_jobs SET lease_until=?,updated=?,progress=? WHERE id=? AND state='running' AND lease_owner=? AND lease_until>?",
                (time.time()+lease_seconds,time.time(),encode(progress or {}),identifier,worker,time.time()))
            if cursor.rowcount!=1:raise ProtectionError('任务已取消或租约已失效',409,'job_lease_lost')
    def finish(self,identifier,worker,result=None,error=None,retry=False):
        with self.store.db(True) as db:
            row=db.execute("SELECT * FROM protection_jobs WHERE id=? AND state='running' AND lease_owner=? AND lease_until>?",(identifier,worker,time.time())).fetchone()
            if not row:raise ProtectionError('任务已取消或租约已失效',409,'job_lease_lost')
            state='queued' if error and retry and row['attempts']<row['max_attempts'] else 'failed' if error else 'completed'
            db.execute('UPDATE protection_jobs SET state=?,updated=?,error=?,result=?,lease_until=0,lease_owner=\'\' WHERE id=?',
                (state,time.time(),str(error or '')[:2000],encode(result or {}),identifier))
    def cancel(self,owner,identifier):
        with self.store.db(True) as db:
            db.execute("UPDATE protection_jobs SET state='cancelled',updated=?,lease_until=0 WHERE id=? AND owner=? AND state IN ('queued','running')",(time.time(),identifier,owner))
        return self.get(owner,identifier)
