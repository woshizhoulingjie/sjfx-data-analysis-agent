"""Small durable budgets for difficult catalog evidence; never full-document enrichment."""
from services.large_package_protection import package_lane, recheck_active, guarded_model_call, current_provenance, stamp_fields, validate_cached_rows, record_report_artifact, require_report_artifact, preview_context_kwargs
from sjfx_protection.store import ProtectionError
import hashlib
import json
import time
from contextlib import closing
from services.large_package_resources import pressure, resource_code


def source_key(row):
    info=(row.get('local_summary') or {}).get('portrait') or {}
    return hashlib.sha256(json.dumps([row.get('size'),row.get('modified_at_ns'),info.get('evidence'),
         row.get('deep_summary'),row.get('deep_status')],ensure_ascii=False,sort_keys=True).encode()).hexdigest()


@package_lane
def request(store,package_id,group_id,material,result):
    if not result.get('needs_review') and result.get('purity')=='same':return 0
    profile=material.get('profile') or {}
    if material.get('route')=='clear' and result.get('purity')!='unsure':return 0
    from services.large_package_preview import settings
    budget=settings(True)
    candidates=[]
    # Prefer previously sampled weak evidence, with at most three files per group.
    for entry in material.get('evidence',[]):
        row=store.get_file(package_id,entry.get('source_path',''))
        if not row or row.get('sensitive') or row.get('deep_status')=='completed':continue
        info=(row.get('local_summary') or {}).get('portrait') or {}
        if info.get('recognition_status') in {'restricted','unsupported'}:continue
        if not info.get('needs_refine') and not entry.get('truncated'):continue
        candidates.append((row,source_key(row)))
        if len(candidates)>=3:break
    if not candidates:return 0
    with store._lock,closing(store._connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        p=c.execute('SELECT status,control_state FROM packages WHERE id=?',(package_id,)).fetchone()
        if not p or any(v in {'paused','cancelled'} for v in p):c.rollback();return 0
        total=c.execute('SELECT COUNT(*) FROM files WHERE package_id=?',(package_id,)).fetchone()[0]
        cap=min(256,max(2,int(total*.01)))
        used,seconds,bytes_,pages=c.execute('SELECT COUNT(*),COALESCE(SUM(seconds),0),COALESCE(SUM(bytes),0),COALESCE(SUM(pages),0) FROM lp_catalog_refine WHERE package_id=?',(package_id,)).fetchone()
        scheduled=0
        for row,source in candidates:
            # Reserve worst-case work, including process timeout slack. Failures never refund loops.
            if used>=cap or seconds+budget['seconds']+1>1800 or bytes_+budget['bytes']>64*1024*1024 or pages+budget['ocr_pages']>128:break
            if c.execute('SELECT 1 FROM lp_catalog_refine WHERE package_id=? AND path=?',(package_id,row['path'])).fetchone():continue
            pending=c.execute("SELECT 1 FROM queue WHERE package_id=? AND path=? AND stage IN ('recognize','deep') AND status IN ('queued','running','retry')",(package_id,row['path'])).fetchone()
            if pending:continue
            c.execute('INSERT INTO lp_catalog_refine VALUES(?,?,?,?,?,?,?)',(package_id,row['path'],source,'queued',budget['seconds']+1,budget['bytes'],budget['ocr_pages']))
            c.execute("INSERT INTO queue(package_id,path,stage,status,updated_at) VALUES(?,?,'catalog_refine','queued',?)",(package_id,row['path'],time.time()))
            scheduled+=1;used+=1;seconds+=budget['seconds']+1;bytes_+=budget['bytes'];pages+=budget['ocr_pages']
        c.commit()
    return scheduled


def process_one(worker):
    store=worker.store
    claimed=store.claim('catalog_refine',1)
    if not claimed:return False
    package_id,path,token=claimed[0]
    from services.large_package_catalog_coordination import leaf_batch
    with leaf_batch(store, package_id, 'catalog-refine', lambda:worker._interrupted(package_id)) as allowed:
        if not allowed:
            store.requeue(package_id, path, 'catalog_refine', token=token)
            return False
        return _process_claimed(worker, package_id, path, token)


@package_lane
def _process_claimed(worker, package_id, path, token):
    store=worker.store
    worker._resource_package_id=package_id
    outcome='failed';finished=False
    try:
        row=store.get_file(package_id,path);package=store.get(package_id)
        with closing(store._connect()) as c:
            job=c.execute('SELECT source FROM lp_catalog_refine WHERE package_id=? AND path=?',(package_id,path)).fetchone()
        if not row or not package or not job or source_key(row)!=job[0]:
            outcome='stale';return True
        from services.large_package_preview_runner import PreviewCancelled
        summary,_=worker.preview_runner.run(package['root_path'],row,refine=True,cancel_check=lambda:worker._interrupted(package_id),**preview_context_kwargs())
        if summary.get('status')=='failed' or not summary.get('sample'):
            outcome='no_gain';return True
        fields=worker._preview_fields(summary,row,True)
        # Exact source snapshot and lease checked under the same transaction as the write.
        with store._lock,closing(store._connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            current=c.execute('SELECT * FROM files WHERE package_id=? AND path=?',(package_id,path)).fetchone()
            if not current or source_key(store._row(current))!=job[0] or worker._interrupted(package_id):
                c.rollback();outcome='stale';return True
            ok,reason=store._commit_stage_conn(c,package_id,path,'catalog_refine',token,file_fields=fields)
            if ok:
                c.execute("UPDATE lp_catalog_refine SET status='completed' WHERE package_id=? AND path=?",(package_id,path))
                c.commit();outcome='completed';finished=True
            else:c.rollback();outcome='stale'
    except ProtectionError:
        raise
    except Exception as exc:
        if resource_code(exc):
            # Keep the existing bounded reservation; only refund this lease
            # attempt. Even if the database is full, never finalize as failed.
            finished=True
            worker._defer_resource(package_id,path,'catalog_refine',token,exc)
            raise pressure(exc) from exc
        import logging
        logging.getLogger(__name__).info('bounded catalog refinement ended: %s/%s: %s',package_id,path,type(exc).__name__)
    finally:
        # Cancelled work remains accounted for and is not automatically retried indefinitely.
        # A successful content commit already consumed the lease. All other outcomes
        # finish once, atomically with their refinement status, only while we own it.
        if not finished:
            with store._lock,closing(store._connect()) as c:
                c.execute('BEGIN IMMEDIATE')
                ok,_=store._commit_stage_conn(c,package_id,path,'catalog_refine',token,
                                             ok=outcome in {'no_gain','stale'})
                if ok:
                    c.execute('UPDATE lp_catalog_refine SET status=? WHERE package_id=? AND path=?',(outcome,package_id,path))
                    c.commit()
                else:c.rollback()
    return True
