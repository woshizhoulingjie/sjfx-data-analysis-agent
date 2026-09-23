import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from config import Config
from services.large_package_isolated import LargePackageStore
from services import large_package_semantic as semantic
from services import large_package_catalog_naming as naming


class Model:
    model='fixture-model'
    supports_json_response_format=True
    def __init__(self):self.calls=0
    def chat(self,system,user,**kwargs):
        self.calls+=1
        payload=json.loads(user)
        return {'content':json.dumps({'name':'远程证明中的设备证据验证','description':'讨论设备如何提供可信证据以及验证方如何完成校验。','evidence_ids':[payload['evidence'][0]['id']],'needs_review':False},ensure_ascii=False),'usage':{'prompt_tokens':100,'completion_tokens':50},'finish_reason':'stop'}


class _DistinctNameModel(Model):
    """给每个组返回**不同**名字的夹具模型。

    为什么需要它（DEF-16）：本文件默认的 `Model` 对任何输入都返回同一个名字
    `远程证明中的设备证据验证`。而 `normalize_leaves` 会把"同一主题内完全同名"
    的叶组合并成一个 —— **那正是产品要求的行为**（同名叶子必须合并）。
    于是默认夹具下 3 个组会被合并成 1 个叶子，而
    `test_optional_hierarchy_uses_budget_and_preserves_members` 需要至少 3 个叶子
    才能考"上层目录把叶子归成分支"，于是 `hierarchy.process` 提前 return、
    不写任何分支，用例在 `branches[0]` 上 IndexError。
    换句话说：不是合并逻辑错，是夹具的名字退化到只剩一个主题。
    """

    def __init__(self):
        super().__init__()
        self.named = 0

    def chat(self, system, user, **kwargs):
        self.named += 1
        result = super().chat(system, user, **kwargs)
        import json as _json
        data = _json.loads(result['content'])
        data['name'] = '设备证据验证主题%d' % self.named
        data['description'] = '第 %d 组设备证据验证的内容范围与边界。' % self.named
        result['content'] = _json.dumps(data, ensure_ascii=False)
        return result


class NamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.patches=[patch.object(Config,'SCAN_ALLOWED_ROOTS',()),patch.object(Config,'LP_CATALOG_MODEL_NAMING',True),patch.object(Config,'LP_CATALOG_SECONDS',60),patch.object(Config,'LP_CATALOG_REQUESTS',2),patch.object(Config,'LP_CATALOG_TOTAL_TOKENS',20000)]
        for p in self.patches:p.start()
        self.store=LargePackageStore(self.root/'large.db');self.pid=self.store.create(self.root)['id'];self.model=Model()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def add(self,path,text,vector=None):
        self.store.upsert_file(self.pid,{'path':path,'name':path,'size':len(text)})
        self.store.update_file(self.pid,path,quick_status='completed',local_summary={'sample':text,'portrait':{'schema':'large-content-portrait/1','headings':[],'fields':[]}})
        from services.large_package_catalog import material
        digest=material(self.store.get_file(self.pid,path))[1]
        semantic._group(self.store,self.pid,path,digest,semantic._vector(vector or [1,0]),'fixture-embed')
        semantic.name_batch(self.store,self.pid)
        self.store.update_package(self.pid,status='waiting_for_selection',phase='catalog_ready',progress=70)
        return self.store.get_file(self.pid,path)['local_summary']['portrait']['catalog_group']
    def run_name(self):
        return naming.process_one(self.store,self.pid,model_client=self.model)
    def test_model_label_visible_without_mutating_file_or_deep_summary(self):
        gid=self.add('a.pdf','Remote attestation verifies device evidence and freshness. '*4)
        before=self.store.get_file(self.pid,'a.pdf')
        self.assertTrue(self.run_name())
        after=self.store.get_file(self.pid,'a.pdf')
        self.assertEqual(after['topic'],'远程证明中的设备证据验证')
        for k in ('local_summary','deep_summary','sha256','deep_status'):self.assertEqual(before[k],after[k])
        node=next(x for x in self.store.catalog(self.pid)['smart']['topics'] if x['node_id']==gid)
        self.assertEqual(node['naming_status'],'model');self.assertEqual(node['evidence'][0]['source_path'],'a.pdf')
        self.assertEqual(self.model.calls,1)
    def test_restart_and_repeated_initial_round_do_not_reset_budget(self):
        self.add('a.txt','Remote attestation verifies device evidence and freshness. '*4)
        self.run_name()
        first=naming.ensure_round(self.store,self.pid)
        restarted=LargePackageStore(self.root/'large.db')
        second=naming.ensure_round(restarted,self.pid)
        self.assertEqual(first['id'],second['id']);self.assertEqual(second['requests'],1);self.assertEqual(second['tokens'],150)
    def test_concurrent_reservations_never_exceed_limit(self):
        rnd=naming.ensure_round(self.store,self.pid)
        with ThreadPoolExecutor(max_workers=8) as pool:
            values=list(pool.map(lambda _:naming.reserve(self.store,self.pid,rnd['id'],'g',100),range(8)))
        self.assertEqual(sum(bool(v) for v in values),2)
    def test_deadline_keeps_directory_available(self):
        self.add('a.txt','Remote attestation verifies device evidence and freshness. '*4)
        naming.queue_groups(self.store,self.pid);rnd=naming.ensure_round(self.store,self.pid)
        with closing(self.store._connect()) as c:c.execute('UPDATE lp_catalog_rounds SET deadline=?',(time.time()-1,))
        self.assertFalse(self.run_name());self.assertEqual(self.model.calls,0)
        self.assertTrue(self.store.catalog(self.pid)['smart']['topics']);self.assertEqual(naming.state(self.store,self.pid)['status'],'exhausted')
    def test_model_outage_does_not_burn_retries_or_budget(self):
        """模型不可用**不该消耗重试额度、也不该消耗轮次预算**。

        背景（2026-09-19 生产故障）：`reserve()` 在调用模型**之前**就 `requests+1`，
        失败不退。模型一次宕机（实测 **2 小时 20 分**）就把任务的 5 次重试与轮次预算
        一起烧光 —— 叶组永久拿不到名字，主题层只能在残缺集合上生成，其余全部落进
        「待归并主题」。

        钉住的性质：
          * 连接类失败（`LocalModelError`）→ attempts **不变**、预算**退还**、
            长退避后重试（模型回来就接着做完）；
          * 内容类失败（`ValueError`）→ 照旧计入 attempts
            （模型给的名字不合法，重试有限次是对的）。
        """
        from services.ollama import LocalModelError
        self.add('a.txt', 'Remote attestation verifies device evidence and freshness. ' * 4)
        naming.queue_groups(self.store, self.pid)
        naming.ensure_round(self.store, self.pid)

        def _job():
            with closing(self.store._connect()) as c:
                return c.execute("SELECT status, attempts FROM lp_catalog_jobs "
                                 "WHERE package_id=?", (self.pid,)).fetchone()

        def _requests():
            with closing(self.store._connect()) as c:
                return c.execute("SELECT requests FROM lp_catalog_rounds "
                                 "WHERE package_id=?", (self.pid,)).fetchone()[0]

        # ① 连接类失败：不烧重试次数、退还预算
        before, budget_before = _job(), _requests()
        with patch.object(self.model, 'chat',
                          side_effect=LocalModelError('本机 vLLM 调用失败：Connection refused')):
            self.run_name()
        after = _job()
        self.assertEqual(after['attempts'], before['attempts'],
                         '模型不可用不该消耗重试次数 —— 否则一次宕机就烧光额度')
        self.assertEqual(after['status'], 'deferred')
        self.assertEqual(_requests(), budget_before,
                         '没调成的那次必须退还轮次预算')

        # ② 内容类失败：照旧计入 attempts
        #    先把①留下的退避窗口清掉，否则任务还没到期、根本不会被拾取。
        with closing(self.store._connect()) as c:
            c.execute("UPDATE lp_catalog_jobs SET status='queued', next_at=0 "
                      "WHERE package_id=?", (self.pid,))
        before = _job()
        with patch.object(self.model, 'chat',
                          side_effect=ValueError('目录名称不能使用状态或字段名')):
            self.run_name()
        after = _job()
        self.assertEqual(after['attempts'], before['attempts'] + 1,
                         '内容不合法要计入重试次数')

    def test_deferred_job_is_retried_until_the_attempt_cap(self):
        """命名任务失败后被打成 deferred，**必须有出路**。

        背景（2026-09-19 生产故障，17 个包里坏了 5 个）：任务失败两次会被写成
        `status='deferred'`，而拾取只认 `status='queued'`，`_catalog_background`
        选包时也只认 queued/retry —— 于是只要模型短暂不可用（一次 vLLM 宕机），
        任务就**永久搁置**：叶组拿不到名字，主题层只能在残缺的叶组集合上生成，
        其余全部落进「待归并主题」。实测单包 133 个任务的 next_at 停在 100 分钟
        前、轮次预算还剩 79%，却没有任何代码去取它们。

        钉住的性质：
          * deferred 且**未到重试上限、且已到期** → 必须被拾起；
          * 达到上限后 → 不再拾取（避免模型长期不可用时无限空转）。
        """
        self.add('a.txt', 'Remote attestation verifies device evidence and freshness. ' * 4)
        naming.queue_groups(self.store, self.pid)
        naming.ensure_round(self.store, self.pid)
        cap = max(1, int(getattr(Config, 'LP_NAME_MAX_ATTEMPTS', 5) or 5))

        def _set_job(status, attempts, next_at):
            with closing(self.store._connect()) as c:
                c.execute("UPDATE lp_catalog_jobs SET status=?, attempts=?, next_at=?, "
                          "error='本机 vLLM 调用失败：Connection refused' WHERE package_id=?",
                          (status, attempts, next_at, self.pid))

        def _job_status():
            with closing(self.store._connect()) as c:
                return c.execute("SELECT status, attempts FROM lp_catalog_jobs "
                                 "WHERE package_id=?", (self.pid,)).fetchone()

        # ① 已到期、还有重试额度的 deferred —— 必须被拾起
        _set_job('deferred', 1, time.time() - 60)
        self.model.calls = 0
        self.run_name()
        self.assertGreaterEqual(
            self.model.calls, 1,
            'deferred 任务到期后必须被拾起 —— 否则模型一次抖动就会让目录永久残废')

        # ② 额度用尽的 deferred —— 不得再拾取（避免模型长期不可用时无限空转）
        _set_job('deferred', cap, time.time() - 60)
        self.model.calls = 0
        self.run_name()
        self.assertEqual(self.model.calls, 0, '达到重试上限后不得再拾取')
        row = _job_status()
        self.assertEqual(row[0], 'deferred', '额度用尽的任务应保持 deferred')

    def test_same_label_different_groups_never_merge(self):
        a=self.add('a.txt','Remote attestation verifies remote evidence. '*4,[1,0])
        b=self.add('b.txt','Memory encryption isolates virtual machine pages. '*4,[0,1])
        self.run_name();self.run_name()
        self.assertNotEqual(a,b)
        for gid,path in ((a,'a.txt'),(b,'b.txt')):
            self.assertEqual([x['path'] for x in self.store.file_page(self.pid,node_id=gid)['files']],[path])
        self.assertEqual(len([x for x in self.store.catalog(self.pid)['smart']['topics'] if x['naming_status']=='model']),2)
    def test_pause_cancels_late_result_and_preserves_budget(self):
        self.add('a.txt','Remote attestation verifies device evidence and freshness. '*4)
        original=self.model.chat
        def pause(*args,**kwargs):
            result=original(*args,**kwargs);self.store.update_package(self.pid,status='paused');return result
        self.model.chat=pause
        self.run_name()
        self.assertEqual(naming.state(self.store,self.pid)['named_nodes'],0)
        self.assertEqual(naming.state(self.store,self.pid)['requests'],1)
    def test_revision_change_rejects_stale_name(self):
        gid=self.add('a.txt','Remote attestation verifies device evidence and freshness. '*4)
        bundle=naming.bundle(self.store,self.pid,gid)
        result=naming.validate(self.model.chat('',bundle['user'])['content'],bundle)
        self.add('b.txt','Remote attestation issues new evidence and challenges. '*4)
        self.assertFalse(naming.apply(self.store,self.pid,gid,bundle,result,'fixture-model'))
    def test_invalid_evidence_and_invented_identifier_rejected(self):
        gid=self.add('a.txt','Remote attestation verifies device evidence and freshness. '*4)
        bundle=naming.bundle(self.store,self.pid,gid)
        # 不存在的证据编号、泛化名：仍然拒绝（这里不变）。
        for name,ids in [('设备可信证据的校验流程',['E999']),('相关资料',['E1'])]:
            with self.assertRaises(ValueError):naming.validate(json.dumps({'name':name,'description':'设备如何产生可信证据并进行验证。','evidence_ids':ids}),bundle)
        # 编造的缩写（XYZ）：2026-09-20 起不再让整条作废，而是**从名字里删掉**并标复核
        # ——整条作废 + 重试同一提示 = 永远没有名字（生产实测 7 组 / 276 个文件踩过）。
        got=naming.validate(json.dumps({'name':'未知XYZ产品的设备证据验证','description':'设备如何产生可信证据并进行验证。','evidence_ids':['E1']}),bundle)
        self.assertNotIn('XYZ',got['name'])
        self.assertTrue(got['needs_review'])
    def test_cached_names_and_single_settlement(self):
        gid=self.add('a.txt','Remote attestation verifies device evidence and freshness. '*4)
        self.run_name()
        with closing(self.store._connect()) as c:
            call=c.execute('SELECT id FROM lp_catalog_calls').fetchone()[0]
            c.execute('DELETE FROM lp_catalog_labels')
            c.execute("UPDATE lp_catalog_jobs SET status='queued'")
        naming.finish_call(self.store,call,{'total_tokens':1},1,'completed')
        self.run_name()
        self.assertEqual(self.model.calls,1)
        self.assertEqual(naming.state(self.store,self.pid)['tokens'],150)
    def test_representatives_include_tail_not_just_first_128(self):
        # Insert scalar memberships to keep this a fast, meaningful selection test.
        gid=self.add('a.txt','Remote attestation evidence verification. '*4)
        with closing(self.store._connect()) as c:
            for i in range(150):
                path='z%03d.txt'%i
                self.store.upsert_file(self.pid,{'path':path,'name':path})
                text='Remote attestation sample %s proves freshness.'%i
                local=json.dumps({'sample':text,'portrait':{'schema':'large-content-portrait/1','headings':[],'fields':[]}})
                c.execute('UPDATE files SET local_summary=? WHERE package_id=? AND path=?',(local,self.pid,path))
                v=semantic._vector([.8,.6] if i==149 else [1,0])
                c.execute('INSERT INTO large_catalog_members VALUES(?,?,?,?,?)',(self.pid,path,gid,str(i),v.tobytes()))
        rows,_=naming.representatives(self.store,self.pid,gid)
        self.assertIn('z149.txt',[r['path'] for r,_ in rows])

    def test_manual_round_never_creates_another_initial_budget(self):
        self.add('a.txt','Remote attestation verifies device evidence and freshness. '*4)
        rnd=naming.ensure_round(self.store,self.pid,'manual-round-1')
        self.run_name();self.run_name()
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM lp_catalog_rounds').fetchone()[0],1)
        self.assertEqual(naming.state(self.store,self.pid)['requests'],1)
    def test_manual_name_survives_new_content_and_can_restore_version(self):
        gid=self.add('a.txt','Remote attestation verifies device evidence and freshness. '*4)
        self.run_name()
        naming.rename_node(self.store,self.pid,gid,'设备身份与证据验证')
        self.add('b.txt','Remote attestation demonstrates freshness using device challenges. '*4)
        naming.queue_groups(self.store,self.pid)
        self.assertEqual(self.store.get_file(self.pid,'a.txt')['topic'],'设备身份与证据验证')
        self.assertFalse(naming.state(self.store,self.pid)['jobs'].get('queued',0))
        restored=naming.rename_node(self.store,self.pid,gid,'',restore_version=1)
        self.assertEqual(restored['name'],'远程证明中的设备证据验证')
    def test_all_preview_topics_are_paginated_without_hiding_files(self):
        self.store.upsert_files(self.pid,[{'path':'f%03d.txt'%i,'name':'f%03d.txt'%i} for i in range(210)])
        with closing(self.store._connect()) as c:
            c.executemany('UPDATE files SET topic=? WHERE package_id=? AND path=?',[('topic %03d'%i,self.pid,'f%03d.txt'%i) for i in range(210)])
        result=self.store.catalog(self.pid)['smart']
        self.assertEqual(len(result['topics']),200);self.assertTrue(result['topics_truncated'])
        from services.large_package_catalog_view import enhance_catalog
        page=enhance_catalog(self.store,self.pid,{'smart':{}},100,200)['smart']
        self.assertEqual(len(page['topics']),10)
        node=page['topics'][0]
        self.assertEqual(self.store.file_page(self.pid,node_id=node['node_id'])['total'],1)
    def test_worker_model_stage_publishes_at_deadline_with_inflight_request(self):
        from large_package_worker import LargePackageWorker
        from concurrent.futures import Future
        self.add('a.txt','Remote attestation verifies device evidence and freshness. '*4)
        naming.queue_groups(self.store,self.pid);naming.ensure_round(self.store,self.pid)
        self.store.update_package(self.pid,status='running',phase='catalog_naming',progress=66)
        with closing(self.store._connect()) as c:c.execute('UPDATE lp_catalog_rounds SET deadline=?',(time.time()-1,))
        worker=LargePackageWorker(self.store);worker._naming_future=Future()
        worker.run_once()
        self.assertEqual(self.store.get(self.pid)['progress'],70)
        self.assertEqual(self.store.get(self.pid)['status'],'waiting_for_selection')

    def test_coalesced_request_key_cannot_reset_budget_on_replay(self):
        first=naming.ensure_round(self.store,self.pid,'request-key-a')
        other=naming.ensure_round(self.store,self.pid,'request-key-b')
        self.assertEqual(first['id'],other['id'])
        with closing(self.store._connect()) as c:c.execute("UPDATE lp_catalog_rounds SET status='completed'")
        replay=naming.ensure_round(self.store,self.pid,'request-key-b')
        self.assertEqual(replay['id'],first['id'])
    def test_unchanged_completed_names_do_not_requeue_on_improve(self):
        self.add('a.txt','Remote attestation verifies device evidence and freshness. '*4)
        self.run_name();naming.state(self.store,self.pid)
        naming.queue_groups(self.store,self.pid)
        naming.ensure_round(self.store,self.pid,'another-improvement')
        self.run_name()
        self.assertEqual(self.model.calls,1)

    def test_optional_hierarchy_preserves_unrelated_members_without_forced_parent(self):
        # 用"每组名字都不同"的夹具，保证 3 个叶子不会被同名归一并掉（见类注释）
        self.model=_DistinctNameModel()
        groups=[]
        for i,vector in enumerate(([1,0,0],[0,1,0],[0,0,1])):
            groups.append(self.add('doc%s.txt'%i,'Remote attestation verifies evidence for case %s. '%i*4,vector))
        with patch.object(Config,'LP_CATALOG_REQUESTS',10):
            for _ in range(3):self.run_name()
            from services.large_package_catalog_hierarchy import process
            current=naming.state(self.store,self.pid)
            model=Model()
            def hierarchy(*args,**kwargs):
                return {'content':json.dumps({'branches':[{'name':'设备远程证明与证据验证','children':['D1','D2']}]},ensure_ascii=False),'usage':{'total_tokens':100}}
            model.chat=hierarchy
            self.assertTrue(process(self.store,self.pid,current,lambda:False,model))
        self.store._catalog_cache.clear()
        catalog=self.store.catalog(self.pid)
        branch=catalog['smart']['branches'][0]
        self.assertEqual(branch['status'],'pending')
        self.assertEqual(self.store.file_page(self.pid,node_id=branch['node_id'])['total'],3)
        self.assertEqual(len(catalog['smart']['topics']),3)
        self.assertEqual(naming.state(self.store,self.pid)['requests'],3)

    def test_explicit_different_projects_do_not_merge_by_template(self):
        from services.large_package_catalog import material
        groups=[]
        for path,project in [('a.txt','P-A'),('b.txt','P-B')]:
            text='项目编号：'+project+'。设备交付与验收计划，合同执行进度记录。'
            self.store.upsert_file(self.pid,{'path':path,'name':path})
            self.store.update_file(self.pid,path,quick_status='completed',local_summary={'sample':text,'portrait':{'schema':'large-content-portrait/1','projects':[project]}})
            digest=material(self.store.get_file(self.pid,path))[1]
            semantic._group(self.store,self.pid,path,digest,semantic._vector([1,0]),'fixture-embed')
            groups.append(self.store.get_file(self.pid,path)['catalog_node_id'])
        self.assertNotEqual(groups[0],groups[1])

    def test_bibliographic_header_does_not_hide_article_title(self):
        text='02 | 信息安全研究 Journal of Information Security Research 引用格式:冯登国.机密计算发展现状与趋势[J].信息安全研究,2024 机密计算发展现状与趋势 冯登国 正文说明可信执行环境。'
        self.assertTrue(naming.content_snippet(text).startswith('机密计算发展现状与趋势'))
        material={'evidence':[{'id':'E1','text':text,'headings':[]}]}
        with self.assertRaises(ValueError):naming.validate(json.dumps({'name':'《信息安全研究》2024年第1期','description':'期刊文章的出版信息与作者介绍。','evidence_ids':['E1']},ensure_ascii=False),material)

if __name__=='__main__':unittest.main()
