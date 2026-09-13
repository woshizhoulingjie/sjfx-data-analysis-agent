"""Mail-only queue consumer; shares audited supervision but never claims other tasks."""
from __future__ import annotations
import os,signal,socket,time
from config import Config
from .jobs import MAIL_TASK_TYPES


def claim_mail_job(storage,worker_id):
    types=sorted(MAIL_TASK_TYPES)
    if not Config.MAIL_FEATURE_ENABLED:return None
    with storage.lock,storage._connect() as c:
        c.execute('BEGIN IMMEDIATE');now=time.time()
        row=c.execute("SELECT id FROM analysis_jobs WHERE status='queued' AND cancel_requested=0 AND available_at<=? AND task_type IN ("+','.join('?' for _ in types)+') ORDER BY priority DESC,rowid LIMIT 1',[now]+types).fetchone()
        if not row:return None
        changed=c.execute("UPDATE analysis_jobs SET status='running',stage='claimed',worker_id=?,started_at=?,heartbeat_at=?,available_at=0,attempt_count=attempt_count+1,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='queued'",(worker_id,now,now,row['id']))
        if changed.rowcount!=1:return None
        value=dict(c.execute('SELECT * FROM analysis_jobs WHERE id=?',(row['id'],)).fetchone())
    return storage._decode_job(value)


def main():
    if not Config.MAIL_FEATURE_ENABLED:return
    from services.storage import Storage
    import worker as supervisor
    storage=Storage(Config.DB_PATH,Config.DOCUMENT_CACHE_DIR,Config.SIDECAR_PAYLOAD_BYTES)
    identifier='mail:'+socket.gethostname()+':'+str(os.getpid());stopping=False
    def stop(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    while not stopping:
        job=claim_mail_job(storage,identifier)
        if not job:time.sleep(1);continue
        try:
            result=supervisor.execute_supervised(job)
            if isinstance(result,dict) and result.pop('_defer_slice',False):storage.defer_running_job(job['id'],result.get('_defer_message','等待邮件索引'),delay_seconds=int(result.get('_defer_seconds',10)))
            elif isinstance(result,dict) and result.pop('_requeue_slice',False):storage.requeue_job_slice(job['id'],result.pop('_requeue_message','邮件检查点已保存，继续处理'))
            else:storage.finalize_job(job['id'],result=result)
        except Exception as exc:
            storage.finalize_job(job['id'],error=str(exc)[:1000])

if __name__=='__main__':main()
