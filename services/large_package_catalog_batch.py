"""Batch optional catalog names without changing evidence or leaf membership.

Reuse each group's complete bounded evidence bundle, revision guard, cache,
validation, durable budget ledger and foreground-priority generation gate.
"""
from contextlib import closing
import json
import time
from services import large_package_catalog_naming as naming

BATCH_SYSTEM = naming.SYSTEM.split("只输出JSON：", 1)[0] + (
    '\n本次输入含多个独立内容组 groups。逐组完成相同的命名任务；'
    '每组证据编号只在本组有效，不混用任何其他组的事实。'
    '只输出JSON：{"groups":[{"id":"G1","name":"名称","description":"范围",'
    '"evidence_ids":["E1"],"needs_review":false}]}。'
    '证据编号必须来自对应组。若材料明显混杂，needs_review=true；不得自行改成员。'
    '每个输入组恰好返回一项，id 原样返回。')


def _release(store, package_id, job, error='', retry=False):
    with closing(store._connect()) as c:
        c.execute("""UPDATE lp_catalog_jobs SET status=?,leased=NULL,next_at=?,
            attempts=attempts+?,error=? WHERE package_id=? AND group_id=? AND revision=? AND status='running'""",
            ('queued' if not retry or job['attempts'] < 1 else 'deferred',
             time.time()+(5 if retry else 0), int(retry), error[:250], package_id, job['group_id'], job['revision']))


def process_batch(store, package_id, cancelled=lambda: False, model_client=None):
    from config import Config
    size = getattr(Config, 'LP_CATALOG_BATCH_SIZE', 4)
    if size <= 1:
        return naming.process_one(store, package_id, cancelled, model_client)
    cfg = naming.settings()
    if not cfg['enabled'] or cancelled():
        return False
    naming.queue_groups(store, package_id)
    naming.ensure_round(store, package_id)
    current = naming.state(store, package_id)
    if not current['active']:
        return False
    jobs = []
    with store._lock, closing(store._connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE lp_catalog_jobs SET status='queued',leased=NULL WHERE package_id=? AND status='running' AND leased<?", (package_id, time.time()-120))
        # Failed batches are retried through the existing single-group route.
        retry = c.execute("SELECT 1 FROM lp_catalog_jobs WHERE package_id=? AND status='queued' AND attempts>0 AND next_at<=? LIMIT 1", (package_id, time.time())).fetchone()
        if not retry:
            order = 'ASC' if current.get('requests', 0) % 4 == 3 else 'DESC'
            jobs = [dict(row) for row in c.execute("""SELECT j.*,g.member_count n
                FROM lp_catalog_jobs j JOIN large_catalog_groups g ON g.package_id=j.package_id AND g.id=j.group_id
                WHERE j.package_id=? AND j.status='queued' AND j.next_at<=?
                ORDER BY n """ + order + ",j.group_id LIMIT ?", (package_id,time.time(),size))]
            for job in jobs:
                c.execute("UPDATE lp_catalog_jobs SET status='running',leased=? WHERE package_id=? AND group_id=? AND revision=?", (time.time(),package_id,job['group_id'],job['revision']))
        c.commit()
    if retry or not jobs:
        return naming.process_one(store,package_id,cancelled,model_client)

    def stop():
        package = store.get(package_id)
        return cancelled() or not package or package['status'] in {'paused','cancelled'} or time.time() >= current['deadline']

    pending, payload = [], []
    call_id = None
    response = {}
    started = time.monotonic()
    outcome = 'failed'
    try:
        model_client = model_client or naming.client()
        if not model_client:
            raise ValueError('本机命名模型未启用')
        for job in jobs:
            if stop():
                break
            try:
                material = naming.bundle(store, package_id, job['group_id'])
                with closing(store._connect()) as c:
                    cached = c.execute('SELECT result FROM lp_catalog_cache WHERE owner_id=? AND model=? AND digest=?', (material['owner_id'],model_client.model,material['digest'])).fetchone()
                if cached:
                    result = naming.validate(cached[0],material)
                    if not stop():
                        naming.apply(store,package_id,job['group_id'],material,result,model_client.model)
                    continue
                item = {'id':'G'+str(len(pending)+1), **json.loads(material['user'])}
                proposed = json.dumps({'groups':payload+[item]}, ensure_ascii=False,separators=(',',':'))
                if pending and naming.token_count(BATCH_SYSTEM+'\n'+proposed)+64 > getattr(Config,'LP_CATALOG_BATCH_INPUT_TOKENS',6500):
                    _release(store,package_id,job)
                    continue
                pending.append((job,material,item['id']))
                payload.append(item)
            except Exception as exc:
                _release(store,package_id,job,str(exc),retry=True)
        if not pending or stop():
            return True
        user = json.dumps({'groups':payload},ensure_ascii=False,separators=(',',':'))
        output = cfg['output'] * len(pending) + 32
        reserved = naming.token_count(BATCH_SYSTEM+'\n'+user)+64+output
        # A batch that cannot fit the remaining token budget must not exhaust
        # the round if an ordinary single-group request can still fit.
        if current['tokens'] + reserved > current['max_tokens']:
            for job, _, _ in pending:
                _release(store,package_id,job)
            return naming.process_one(store,package_id,cancelled,model_client)
        call_id = naming.reserve(store,package_id,current['id'],'batch:'+','.join(j['group_id'] for j,_,_ in pending),reserved)
        if not call_id:
            return True
        from services.model_dispatch import background_generation
        kwargs = {'temperature':.1,'max_tokens':output,'retries':0,
                  'timeout':min(cfg['timeout'],max(1,int(current['deadline']-time.time())))}
        if getattr(model_client,'supports_json_response_format',False):
            kwargs['response_format']={'type':'json_object'}
        with background_generation(stop):
            response=model_client.chat(BATCH_SYSTEM,user,**kwargs)
        if response.get('finish_reason')=='length':
            raise ValueError('批量目录命名输出被截断，改为逐组重试')
        from services.model_output import extract_json_value
        value=extract_json_value(response.get('content',''))
        if not isinstance(value,dict) or not isinstance(value.get('groups'),list):
            raise ValueError('批量目录命名格式无效，改为逐组重试')
        allowed={key for _,_,key in pending}
        results={}
        for item in value['groups']:
            if not isinstance(item,dict) or not isinstance(item.get('id'),str) or item['id'] not in allowed or item['id'] in results:
                raise ValueError('批量目录组编号缺失、重复或无效')
            results[item['id']]=item
        for job,material,key in pending:
            try:
                result=naming.validate(json.dumps(results.get(key),ensure_ascii=False),material)
                if not stop():
                    naming.apply(store,package_id,job['group_id'],material,result,model_client.model)
            except Exception as exc:
                _release(store,package_id,job,str(exc),retry=True)
        outcome='completed' if not stop() else 'stale'
        return True
    except Exception as exc:
        for job in jobs:
            _release(store,package_id,job,str(exc),retry=True)
        return True
    finally:
        if call_id:
            naming.finish_call(store,call_id,response.get('usage'),time.monotonic()-started,outcome)
        for job in jobs:
            _release(store,package_id,job)
        store._catalog_cache.pop(package_id,None)
