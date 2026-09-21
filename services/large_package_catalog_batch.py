"""Batch optional catalog names without changing evidence or leaf membership.

Reuse each group's complete bounded evidence bundle, revision guard, cache,
validation, durable budget ledger and foreground-priority generation gate.

失败处理与单组路径**共用一套**（`naming.classify_failure` / `naming.release_job`）：
此前批量路径统一走 5 秒短退避 + attempts+1 且不退款，而单组路径会识别"依赖不可用"、
长退避、不递增 attempts、并退回轮次额度——一次模型宕机在批量路径上会把成百上千
个任务的退避额度烧掉（生产实测 2h20m 的 vLLM 故障）。同时这里开始如实记录
**逐组结果**与 partial 状态：一次调用里有的组发布了、有的组失败，调用台账不再
一律记成 completed（专家报告 §10/§13.3）。
"""
from contextlib import closing
import json
import time
from services import large_package_catalog_naming as naming

BATCH_SYSTEM = naming.SYSTEM.split("只输出JSON：", 1)[0] + (
    '\n本次输入含多个独立内容组 groups。逐组完成相同的命名任务；'
    '每组证据编号只在本组有效，不混用任何其他组的事实。'
    '只输出JSON：{"groups":[{"id":"G1","name":"名称","description":"范围",'
    '"evidence_ids":["E1"],"outliers":[],"needs_review":false}]}。'
    '"outliers" 只列出该组中**明显不属于本组共同主题**的证据编号，'
    '**确实没有就填空数组 []——这是绝大多数组的正确回答**。'
    '同一领域的不同侧面、同一政策的不同条款、同一研究的不同方法，都属于同一个共同主题；'
    '名称里出现顿号也只说明内容有层次，不代表成员无关。'
    '只有某一两项确实是另一件毫不相干的事时才列出，且不得超过本组半数。'
    '证据编号必须来自对应组。不得自行改成员。'
    '每个输入组恰好返回一项，id 原样返回。')


def process_batch(store, package_id, cancelled=lambda: False, model_client=None):
    from config import Config
    size = getattr(Config, 'LP_CATALOG_BATCH_SIZE', 4)
    if size <= 1:
        return naming.process_one(store, package_id, cancelled, model_client)
    cfg = naming.settings()
    if not cfg['enabled'] or cancelled():
        return False
    # 与 process_one 同理：必须先把轮次的有效状态落库，ensure_round 才看得到
    # "这个轮次已经超时/用尽"，否则它会按续期逻辑把截止时间刷新掉，截止时间失效。
    naming.settle_round(store, package_id)
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
    published = []                 # 真正发布的组
    failed = []                    # [{'group':gid,'category':...}]
    category = None                # 整次调用的失败类别（若发生）
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
                    if not stop() and naming.apply(store,package_id,job['group_id'],material,result,model_client.model):
                        published.append(job['group_id'])
                    continue
                item = {'id':'G'+str(len(pending)+1), **json.loads(material['user'])}
                proposed = json.dumps({'groups':payload+[item]}, ensure_ascii=False,separators=(',',':'))
                if pending and naming.token_count(BATCH_SYSTEM+'\n'+proposed)+64 > getattr(Config,'LP_CATALOG_BATCH_INPUT_TOKENS',6500):
                    naming.release_job(store,package_id,job['group_id'],job['revision'],attempts=job['attempts'])
                    continue
                pending.append((job,material,item['id']))
                payload.append(item)
            except Exception as exc:
                one = naming.classify_failure(exc)
                failed.append({'group': job['group_id'], 'category': one})
                naming.release_job(store, package_id, job['group_id'], job['revision'],
                                   attempts=job['attempts'], error=str(exc), category=one)
        if not pending or stop():
            return True
        user = json.dumps({'groups':payload},ensure_ascii=False,separators=(',',':'))
        output = cfg['output'] * len(pending) + 32
        reserved = naming.token_count(BATCH_SYSTEM+'\n'+user)+64+output
        # A batch that cannot fit the remaining token budget must not exhaust
        # the round if an ordinary single-group request can still fit.
        if current['tokens'] + reserved > current['max_tokens']:
            for job, _, _ in pending:
                naming.release_job(store,package_id,job['group_id'],job['revision'],attempts=job['attempts'])
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
        duplicates=set()
        for item in value['groups']:
            if not isinstance(item,dict) or not isinstance(item.get('id'),str) or item['id'] not in allowed:
                continue
            if item['id'] in results:
                duplicates.add(item['id'])
            results[item['id']]=item
        for job,material,key in pending:
            try:
                if key in duplicates:
                    raise ValueError('批量目录组编号重复')
                result=naming.validate(json.dumps(results.get(key),ensure_ascii=False),material)
                if not stop() and naming.apply(store,package_id,job['group_id'],material,result,model_client.model):
                    published.append(job['group_id'])
                else:
                    failed.append({'group':job['group_id'],'category':naming.FAILURE_STALE})
                    naming.release_job(store,package_id,job['group_id'],job['revision'],
                                       attempts=job['attempts'],error='结果作废（命名暂停或修订已变）',
                                       category=naming.FAILURE_STALE)
            except Exception as exc:
                one=naming.classify_failure(exc)
                failed.append({'group':job['group_id'],'category':one})
                naming.release_job(store,package_id,job['group_id'],job['revision'],
                                   attempts=job['attempts'],error=str(exc),category=one)
        # 整次调用的状态如实反映组级结果：全部发布=completed，部分发布=partial，
        # 一个都没发出去=failed，被暂停打断=stale（专家报告 §10 最后一条）。
        if stop():
            outcome = 'stale'
        elif not failed:
            outcome = 'completed'
        elif published:
            outcome = 'partial'
        else:
            outcome = 'failed'
        return True
    except Exception as exc:
        category = naming.classify_failure(exc)
        for job in jobs:
            naming.release_job(store, package_id, job['group_id'], job['revision'],
                               attempts=job['attempts'], error=str(exc), category=category)
        return True
    finally:
        if call_id:
            naming.finish_call(store,call_id,response.get('usage'),time.monotonic()-started,outcome,
                               detail={'mode':'batch','published':published,'failed':failed,
                                       'reserved_groups':len(pending)})
            if category == naming.FAILURE_UNAVAILABLE:
                # 依赖不可用＝根本没调成：退回轮次请求额度（与单组路径一致）。
                naming.refund_call(store, call_id)
        for job in jobs:
            # 只把"本次领取但没轮到处理"的任务放回去；已完成/已释放的不会被这次
            # 调用覆盖（release_job 内部带 status='running' 保护）。
            naming.release_job(store,package_id,job['group_id'],job['revision'],attempts=job['attempts'])
        store._catalog_cache.pop(package_id,None)
