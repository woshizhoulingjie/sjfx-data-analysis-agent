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
        for name,ids in [('未知XYZ产品的设备证据验证',['E1']),('设备可信证据的校验流程',['E999']),('相关资料',['E1'])]:
            with self.assertRaises(ValueError):naming.validate(json.dumps({'name':name,'description':'设备如何产生可信证据并进行验证。','evidence_ids':ids}),bundle)
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

    def test_optional_hierarchy_uses_budget_and_preserves_members(self):
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
        self.assertEqual(self.store.file_page(self.pid,node_id=branch['node_id'])['total'],2)
        self.assertEqual(len(catalog['smart']['topics']),3)
        self.assertEqual(naming.state(self.store,self.pid)['requests'],4)

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
