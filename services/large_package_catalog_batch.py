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

BATCH_SYSTEM = (naming.NAMING_RULES + naming.NAMING_OUTPUT_RULES +
    '本次groups包含多个独立组。各组事实和证据编号不得混用，每组恰好返回一项，id原样返回。'
    '只输出JSON：{"groups":[{"id":"G1",' + naming.NAMING_RESULT_SCHEMA[1:] + ']}')


def process_batch(store, package_id, cancelled=lambda: False, model_client=None, prefetch=False):
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
    # 没有待办时不续期：否则刚结算为 completed 的轮次会被立刻复活，
    # 界面永远显示"正在完善"（见 naming.naming_work_pending）。
    if naming.naming_work_pending(store, package_id):
        naming.ensure_round(store, package_id)
    current = naming.state(store, package_id)
    if not current['active']:
        return False
    order = 'ASC' if current.get('requests', 0) % 4 == 3 else 'DESC'

    # ---- 阶段 1：只读挑候选（不建租约、不改任务状态）------------------------
    #
    # C（文档 §3C.1）："只领取本次实际发出的组"。
    #
    # 原实现先把 min(24,size*3) 个候选**全部标成 running**，再组包、再筛出至多 size
    # 个发一次 chat()，剩下的靠 release_job 放回。单路时这只是白做写操作；两路时它会
    # 直接让两条车道互相饿死 —— A 路把 24 个组全占住，B 路的候选 SELECT 一条也看不到，
    # 于是"两路在途"永远不会发生（文档 §1 第 2 条正是这一点）。
    with store._lock, closing(store._connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE lp_catalog_jobs SET status='queued',leased=NULL WHERE package_id=? AND status='running' AND leased<?", (package_id, time.time()-120))
        # Failed batches are retried through the existing single-group route.
        retry = c.execute("SELECT 1 FROM lp_catalog_jobs WHERE package_id=? AND status='queued' AND attempts>0 AND next_at<=? LIMIT 1", (package_id, time.time())).fetchone()
        candidates = []
        if not retry:
            candidates = [dict(row) for row in c.execute("""SELECT j.*,g.member_count n
                FROM lp_catalog_jobs j JOIN large_catalog_groups g ON g.package_id=j.package_id AND g.id=j.group_id
                WHERE j.package_id=? AND j.status='queued' AND j.next_at<=?
                ORDER BY n """ + order + ",j.group_id LIMIT ?", (package_id,time.time(),min(24,size*3)))]
        c.commit()
    if retry or not candidates:
        return naming.process_one(store,package_id,cancelled,model_client)

    def stop():
        package = store.get(package_id)
        return cancelled() or not package or package['status'] in {'paused','cancelled'} or time.time() >= current['deadline']

    pending, payload = [], []
    cache_hits = []            # (job, material, cached_row)：命中缓存，不需要模型槽
    miss_reasons = {}          # group_id -> 未命中缓存的原因（只用于记账）
    pre_claim_failed = []      # (job, exc)：领取之前就失败，任务**仍是 queued**
    oversized = False
    call_id = None
    response = {}
    started = time.monotonic()
    outcome = 'failed'
    published = []             # 真正发布的组
    failed = []                # [{'group':gid,'category':...}]
    category = None            # 整次调用的失败类别（若发生）
    claimed = []               # 本次**真正领取到**的任务；finally 里统一放回
    try:
        model_client = model_client or naming.client()
        if not model_client:
            raise ValueError('本机命名模型未启用')

        # ---- 阶段 2：组包（纯 CPU，不动任务状态）----------------------------
        prepare_limit = max(size, int(getattr(Config, 'LP_CATALOG_PREPARE_LIMIT', size)))
        def prepared_candidates():
            # Usually prepare one batch, not three. If cache hits, route mismatch
            # or oversized evidence leave room, continue the bounded lookahead
            # instead of starving eligible groups later in the candidate list.
            for start in range(0, len(candidates), prepare_limit):
                if stop() or len(pending) >= size:
                    return
                prepared = []
                for job in candidates[start:start + prepare_limit]:
                    if stop():
                        return
                    try:
                        prepared.append((job, naming.bundle(store, package_id, job['group_id'])))
                    except Exception as exc:
                        pre_claim_failed.append((job, exc))
                # Retain the alternating fairness order within each window.
                prepared.sort(key=lambda pair:(pair[1].get('route','clear'),pair[1].get('tokens',0)),reverse=order=='DESC')
                yield from prepared

        # ---- 阶段 3：选出恰好要发的组（缓存查找也只读）----------------------
        batch_route=None
        for job,material in prepared_candidates():
            if stop():
                break
            try:
                difficulty=material.get('route','clear')
                if len(pending)>=size or (batch_route is not None and difficulty!=batch_route):
                    continue
                with closing(store._connect()) as c:
                    cached = c.execute('SELECT result FROM lp_catalog_cache WHERE owner_id=? AND model=? AND digest=?', (material['owner_id'],model_client.model,material['digest'])).fetchone()
                if cached:
                    cache_hits.append((job,material,cached))
                    continue
                # 提速方案 v2 §P4-1 测量：记下未命中缓存的原因（只读，失败返回 unknown）。
                # 用 getattr 取：Web 进程可能还缓存着没有这个函数的旧 naming 模块。
                _reason_of = getattr(naming, 'cache_miss_reason', None)
                if _reason_of is not None:
                    miss_reasons[job['group_id']] = _reason_of(
                        store, package_id, job['group_id'], material['digest'], model_client.model)
                item = {'id':'G'+str(len(pending)+1), **json.loads(material['user'])}
                proposed = json.dumps({'groups':payload+[item]}, ensure_ascii=False,separators=(',',':'))
                if naming.token_count(BATCH_SYSTEM+'\n'+proposed)+64 > getattr(Config,'LP_CATALOG_BATCH_INPUT_TOKENS',6500):
                    oversized = True
                    continue
                batch_route=difficulty
                pending.append((job,material,item['id']))
                payload.append(item)
            except Exception as exc:
                pre_claim_failed.append((job, exc))
        # 领取之前失败的任务：用 expect_status='queued' 复用同一套失败收口，
        # 退避与 attempts 语义与"领取后失败"完全一致；若这一瞬间已被别的车道领走
        # （status 已变 running），则不动它——那不是我们的任务。
        for job, exc in pre_claim_failed:
            one = naming.classify_failure(exc)
            failed.append({'group': job['group_id'], 'category': one})
            naming.release_job(store, package_id, job['group_id'], job['revision'],
                               attempts=job['attempts'], error=str(exc), category=one,
                               expect_status='queued')
        if not pending and oversized and not stop():
            return naming.process_one(store,package_id,cancelled,model_client)
        if pending:
            user = json.dumps({'groups':payload},ensure_ascii=False,separators=(',',':'))
            from services.large_package_catalog_schema import output_budget
            output = sum(output_budget(cfg,m) for _,m,_ in pending)+32
            reserved = naming.token_count(BATCH_SYSTEM+'\n'+user)+64+output
            # A batch that cannot fit the remaining token budget must not exhaust
            # the round if an ordinary single-group request can still fit.
            if current['tokens'] + reserved > current['max_tokens']:
                # **还没领取，所以无需放回** —— 这正是"只领要发的组"省下的写操作。
                return naming.process_one(store,package_id,cancelled,model_client)

        # ---- 阶段 4：原子领取"要发的组 + 缓存命中的组"------------------------
        #
        # 条件更新带 status='queued' 且检查 rowcount：两条车道即使同时选中同一组，
        # 也只有一个能领到，另一个自动放弃 —— 不会重复命名，也不会抢别人的租约。
        wanted = [job for job,_,_ in cache_hits] + [job for job,_,_ in pending]
        if wanted:
            owned = set()
            with store._lock, closing(store._connect()) as c:
                c.execute('BEGIN IMMEDIATE')
                leased = time.time()
                for job in wanted:
                    cur = c.execute("UPDATE lp_catalog_jobs SET status='running',leased=?,"
                                    "control_generation=COALESCE((SELECT control_generation FROM packages "
                                    "WHERE id=lp_catalog_jobs.package_id),0) "
                                    "WHERE package_id=? AND group_id=? AND revision=? AND status='queued'",
                                    (leased, package_id, job['group_id'], job['revision']))
                    if cur.rowcount == 1:
                        owned.add(job['group_id'])
                    else:
                        failed.append({'group': job['group_id'],
                                       'category': naming.FAILURE_RELEASED})
                c.commit()
            cache_hits = [t for t in cache_hits if t[0]['group_id'] in owned]
            pending = [t for t in pending if t[0]['group_id'] in owned]
            claimed = [t[0] for t in cache_hits] + [t[0] for t in pending]

        # ---- 阶段 5a：缓存命中的组先落库（不占模型槽）------------------------
        for job,material,cached in cache_hits:
            try:
                result = naming.validate(cached[0],material)
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
        if not pending:
            outcome = 'completed' if not failed else ('partial' if published else 'failed')
            return True
        if stop():
            outcome = 'stale'
            return True
        # 领取时可能被并发剔掉若干组：编号与 payload 必须按**最终集合**重排（id 要连续）。
        payload = []
        rebuilt = []
        for index,(job,material,_key) in enumerate(pending, start=1):
            item = {'id':'G%d' % index, **json.loads(material['user'])}
            payload.append(item)
            rebuilt.append((job,material,item['id']))
        pending = rebuilt
        user = json.dumps({'groups':payload},ensure_ascii=False,separators=(',',':'))
        from services.large_package_catalog_schema import output_budget
        output = sum(output_budget(cfg,m) for _,m,_ in pending)+32
        reserved = naming.token_count(BATCH_SYSTEM+'\n'+user)+64+output
        # A2：group_id 只记 'batch:N'，全量组 id 放 group_scope（见 naming.reserve）。
        _scope=','.join(j['group_id'] for j,_,_ in pending)
        call_id = naming.reserve(store,package_id,current['id'],'batch:%d'%len(pending),reserved,
                                 group_scope=_scope)
        if not call_id:
            return True
        from services.model_dispatch import background_generation
        kwargs = {'temperature':.1,'max_tokens':output,'retries':0,
                  'timeout':min(cfg['timeout'],max(1,int(current['deadline']-time.time())))}
        if getattr(model_client,'supports_json_response_format',False):
            from services.large_package_catalog_schema import response as schema_response
            kwargs['response_format']=schema_response(pending=pending)
        from services.catalog_prefetch import start as prefetch_evidence, enabled as prefetch_enabled
        if (prefetch or prefetch_enabled()) and getattr(Config, 'LP_EVIDENCE_PREFETCH', True):
            claimed_ids = {job['group_id'] for job in claimed}
            prefetch_evidence(store, package_id,
                              [job['group_id'] for job in candidates if job['group_id'] not in claimed_ids],
                              stop, getattr(Config, 'LP_EVIDENCE_PREFETCH_GROUPS', 4))
        with background_generation(stop), naming.lease_renewal(
                store, package_id, [job['group_id'] for job in claimed], stop):
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
        # ---- 阶段 5b：逐组提交（模型调用已在事务之外）------------------------
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
        for job in claimed:
            naming.release_job(store, package_id, job['group_id'], job['revision'],
                               attempts=job['attempts'], error=str(exc), category=category)
        return True
    finally:
        if call_id:
            naming.finish_call(store,call_id,response.get('usage'),time.monotonic()-started,outcome,
                               detail={'mode':'batch','published':published,'failed':failed,
                                       'reserved_groups':len(pending),
                                       'finish_reason':response.get('finish_reason'),
                                       # 批次 1 测量补点：同一口径，见 naming.process_one。
                                       'output_chars':len(response.get('content') or ''),
                                       'cache_miss':{gid: miss_reasons[gid] for gid in
                                                     (j['group_id'] for j,_m,_k in pending)
                                                     if gid in miss_reasons}})
            if category == naming.FAILURE_UNAVAILABLE:
                # 依赖不可用＝根本没调成：退回轮次请求额度（与单组路径一致）。
                naming.refund_call(store, call_id)
        for job in claimed:
            # 只把"本次领取但没轮到处理"的任务放回去；已完成/已释放的不会被这次
            # 调用覆盖（release_job 内部带 status='running' 保护）。
            naming.release_job(store,package_id,job['group_id'],job['revision'],attempts=job['attempts'])
        store._catalog_cache.pop(package_id,None)
