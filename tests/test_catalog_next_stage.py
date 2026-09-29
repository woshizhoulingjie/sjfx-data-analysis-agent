import json
import tempfile
import time
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch,Mock
import numpy as np
from config import Config
from services.large_package_isolated import LargePackageStore
from services import large_package_catalog_quality as quality
from services import large_package_feature_delta as features
from services import large_package_catalog_naming as naming
from services import large_package_semantic as semantic


class NextStageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        for p in (patch.object(Config,'SCAN_ALLOWED_ROOTS',()),patch.object(Config,'LP_CATALOG_KMODE',True)):
            p.start();self.addCleanup(p.stop)
        self.store=LargePackageStore(self.root/'test.db');self.pid=self.store.create(self.root)['id']
        features.clear_cache()

    def add(self,gid,vector,n=1,name='设备运行监测技术'):
        vector=quality.unit(vector).astype(np.float32).tobytes()
        paths=[]
        with closing(self.store._connect()) as c:
            c.execute('INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,updated_at) VALUES(?,?,?,?,?,?)',
                      (self.pid,gid,'test',vector,gid+'-0.txt',time.time()))
        for i in range(n):
            path=gid+'-%d.txt'%i;paths.append(path)
            self.store.upsert_file(self.pid,dict(path=path,name=path,size=100,modified_at_ns=1))
            self.store.update_file(self.pid,path,quick_status='completed',local_summary={'sample':name+'记录设备状态、运行参数和监测结果。'*4,'portrait':{'headings':[name]}})
            with closing(self.store._connect()) as c:
                c.execute('INSERT INTO large_catalog_members VALUES(?,?,?,?,?)',(self.pid,path,gid,'hash-'+path,vector))
        with closing(self.store._connect()) as c:
            rev=c.execute('SELECT content_revision FROM large_catalog_groups WHERE package_id=? AND id=?',(self.pid,gid)).fetchone()[0]
            c.execute('INSERT INTO lp_catalog_labels VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(self.pid,gid,rev,1,name,name+'的内容范围与结果。','[]','test','d',0,0,time.time()))
        return paths

    def test_all_members_follow_content_not_largest_seed(self):
        rows=[dict(path=str(i),vector=([1,0] if i<3 else [0,1])) for i in range(10)]
        buckets=quality.partition_members(rows,[dict(name='a',paths=['0','1']),dict(name='b',paths=['3'])])
        self.assertEqual([len(b['paths']) for b in buckets],[3,7])
        rows.append(dict(path='ambiguous',vector=[1,1]))
        self.assertIsNone(quality.partition_members(rows,[dict(name='a',paths=['0']),dict(name='b',paths=['3'])]))

    def test_same_name_different_content_cannot_merge(self):
        self.add('a',[1,0],2);self.add('b',[0,1],2)
        features.ensure_package(self.store,self.pid)
        from services.large_package_catalog_merge import merge_identical_names
        self.assertEqual(merge_identical_names(self.store,self.pid),0)
        with closing(self.store._connect()) as c:self.assertFalse(quality.compatible_groups(c,self.pid,['a','b']))

    def test_incremental_features_process_only_changed_file_and_delete_df(self):
        self.add('a',[1,0],4);features.ensure_package(self.store,self.pid)
        original=features.make_card
        with patch.object(features,'make_card',wraps=original) as make:
            features.ensure_package(self.store,self.pid);self.assertEqual(make.call_count,0)
            self.store.update_file(self.pid,'a-1.txt',local_summary={'sample':'库存盘点账册库存记录数量核对及仓储业务。'*5})
            features.ensure_package(self.store,self.pid);self.assertEqual(make.call_count,1)
            with closing(self.store._connect()) as c:c.execute('DELETE FROM files WHERE package_id=? AND path=?',(self.pid,'a-2.txt'))
            features.ensure_package(self.store,self.pid);self.assertEqual(make.call_count,1)
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute('SELECT files FROM lp_feature_meta WHERE package_id=?',(self.pid,)).fetchone()[0],3)
            self.assertFalse(c.execute('SELECT 1 FROM lp_feature_df WHERE n<0').fetchone())

    def test_round_background_snapshot_stays_stable(self):
        self.add('a',[1,0],2);naming.ensure_round(self.store,self.pid)
        before=features.background(self.store,self.pid)
        self.store.update_file(self.pid,'a-0.txt',local_summary={'sample':'库存盘点数量及仓储异常处理说明。'*5})
        features.ensure_package(self.store,self.pid);features.clear_cache()
        self.assertEqual(features.background(self.store,self.pid),before)

    def test_vector_epoch_ignores_names_and_tracks_vector_changes(self):
        self.add('a',[1,0]);before=semantic._km_signature(self.store,self.pid)
        naming.rename_node(self.store,self.pid,'a','人工确认设备监测')
        self.assertEqual(before,semantic._km_signature(self.store,self.pid))
        with closing(self.store._connect()) as c:c.execute('UPDATE large_catalog_members SET vector=? WHERE package_id=?',(np.array([0,1],dtype=np.float32).tobytes(),self.pid))
        self.assertNotEqual(before,semantic._km_signature(self.store,self.pid))

    def test_small_delta_does_not_run_global_kmeans_or_change_locked_group(self):
        self.add('a',[1,0],3);self.add('b',[0,1],3)
        with closing(self.store._connect()) as c:
            c.execute('DELETE FROM lp_catalog_changes WHERE package_id=?',(self.pid,));quality.save_signature(c,self.pid,6)
        naming.rename_node(self.store,self.pid,'b','人工确认设备监测')
        with closing(self.store._connect()) as c:c.execute('UPDATE large_catalog_members SET input_hash=? WHERE package_id=? AND path=?',('changed',self.pid,'a-1.txt'))
        with patch.object(semantic,'_kmeans_fit',side_effect=AssertionError('global rebuild')):
            self.assertEqual(semantic.kmeans_recluster(self.store,self.pid),1)
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM large_catalog_members WHERE group_id=?',('b',)).fetchone()[0],3)
            self.assertEqual(c.execute('SELECT locked FROM lp_catalog_labels WHERE group_id=?',('b',)).fetchone()[0],1)

    def test_batch_commit_rejects_stale_item_keeps_valid_sibling(self):
        self.add('a',[1,0],2)
        claimed=self.store.claim('quick',2,self.pid)
        items=[dict(package_id=self.pid,path=p,stage='quick',token=t,file_fields={'error':'accepted'}) for _,p,t in claimed]
        items[0]['token']='stale'
        outcomes=self.store.commit_stage_batch(items)
        self.assertFalse(outcomes[0][0]);self.assertTrue(outcomes[1][0])
        self.assertNotEqual(self.store.get_file(self.pid,items[0]['path'])['error'],'accepted')
        self.assertEqual(self.store.get_file(self.pid,items[1]['path'])['error'],'accepted')

    def test_new_and_legacy_packages_keep_distinct_embedding_policy(self):
        self.assertEqual(quality.representation(self.store,self.pid),2)
        self.add('a',[1,0]);self.assertEqual(quality.representation(self.store,self.pid),2)
        with closing(self.store._connect()) as c:c.execute('DELETE FROM lp_catalog_policy')
        self.assertEqual(quality.representation(self.store,self.pid),1)
        value=dict(text='设备状态监测和运行异常分析。'*80,headings=['设备监测'],fields=[],deep_summary='')
        self.assertTrue(semantic.embedding_passage(value,2).startswith('query: '))
        self.assertTrue(semantic.embedding_passage(value,1).startswith('passage: '))

    def test_schema_limits_evidence_ids_and_batch_group_ids(self):
        from services.large_package_catalog_schema import response
        value=response(pending=[({},dict(evidence=[{'id':'E1'}]),'G1')])
        schema=value['json_schema']['schema']['properties']['groups']['items']['anyOf'][0]
        self.assertEqual(schema['properties']['id']['enum'],['G1'])
        self.assertEqual(schema['properties']['evidence_ids']['items']['enum'],['E1'])
        self.assertFalse(schema['additionalProperties'])

    def test_source_digest_tracks_unsampled_evidence_block(self):
        from services.large_package_catalog import material
        row={'local_summary':{'portrait':{'evidence':[dict(text='设备监测数据和运行记录。'*10,kind='text',location=str(i)) for i in range(8)]}}}
        before=material(row)[1]
        row['local_summary']['portrait']['evidence'][2]['text']='新增温度异常分析和设备检修结论。'*10
        self.assertNotEqual(material(row)[1],before)

    def test_mixed_repair_is_local_and_does_not_loop_without_new_content(self):
        paths=self.add('mixed',[1,0],8)
        self.add('stable',[0,1],4)
        with closing(self.store._connect()) as c:
            c.executemany('UPDATE large_catalog_members SET vector=? WHERE package_id=? AND path=?',
                          [(np.array([0,1],dtype=np.float32).tobytes(),self.pid,p) for p in paths[4:]])
            c.execute("UPDATE large_catalog_groups SET result=? WHERE id='mixed'",(json.dumps({'purity':'mixed'}),))
            before=[tuple(r) for r in c.execute("SELECT path,group_id FROM large_catalog_members WHERE group_id='stable'")]
        with patch.object(semantic,'calibrate',return_value=(None,.9)),patch.object(semantic,'_buckets',return_value=[]):
            self.assertTrue(quality.repair_mixed(self.store,self.pid))
            quality.repair_mixed(self.store,self.pid)
            self.assertFalse(quality.repair_mixed(self.store,self.pid))
        with closing(self.store._connect()) as c:
            self.assertEqual(before,[tuple(r) for r in c.execute("SELECT path,group_id FROM large_catalog_members WHERE group_id='stable'")])
            self.assertLessEqual(c.execute('SELECT SUM(attempts) FROM lp_catalog_repair').fetchone()[0],2)

    def test_delete_updates_geometry_and_counts_without_global_clustering(self):
        self.add('a',[1,0],4);self.add('b',[0,1],3)
        with closing(self.store._connect()) as c:
            c.execute('DELETE FROM lp_catalog_changes');quality.save_signature(c,self.pid,7)
            c.execute('DELETE FROM files WHERE package_id=? AND path=?',(self.pid,'a-0.txt'))
        with patch.object(semantic,'_kmeans_fit',side_effect=AssertionError('global rebuild')):
            semantic.kmeans_recluster(self.store,self.pid)
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute("SELECT member_count FROM large_catalog_groups WHERE id='a'").fetchone()[0],3)
            self.assertFalse(c.execute('SELECT 1 FROM lp_catalog_changes').fetchone())

    def test_budgeted_refinement_is_idempotent_and_bounded(self):
        from services.large_package_catalog_refine import request
        paths=self.add('a',[1,0],6)
        material=dict(route='insufficient',evidence=[dict(source_path=p,truncated=True) for p in paths])
        result=dict(needs_review=True,purity='unsure')
        self.assertEqual(request(self.store,self.pid,'a',material,result),2)
        self.assertEqual(request(self.store,self.pid,'a',material,result),0)
        with closing(self.store._connect()) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM lp_catalog_refine').fetchone()[0],2)

    def test_recursive_tree_selection_export_and_stale_visibility(self):
        from services import large_package_catalog_tree as tree
        from services.large_package_catalog_hierarchy import visible
        from services.large_package_catalog_view import catalog_tree
        # 130 coherent leaves require more than one parent layer at the default fanout.
        # 13 个子类分属 3 个大类（维 13/14/15），上级层来自真实的层次结构；
        # 互不相关的子类不得被硬收进同一上级（见 test_catalog_flow_regression）。
        for i in range(130):
            v=np.ones(16,dtype=np.float32);v[i//10]+=1.0;v[13+i//50]+=2.0
            self.add('g%03d'%i,v)
        rnd=naming.ensure_round(self.store,self.pid)
        with patch('services.large_package_catalog_topics.name_topics',side_effect=lambda _s,_p,b,*args:{k:'设备运行监测技术' for k in b}):
            self.assertTrue(tree.build(self.store,self.pid,rnd,model_client=Mock()))
        with closing(self.store._connect()) as c:branches=visible(c,self.pid)
        self.assertTrue(any(b.get('depth',0)>0 for b in branches))
        roots=[b for b in branches if not b.get('parent_id')]
        self.assertEqual(sum(len(b['children']) for b in roots),130)
        self.assertEqual(len(set(p for b in roots for p in b['children'])),130)
        parent=next(b for b in roots if b['node_id'].startswith('topic:'))
        self.assertEqual(self.store.file_page(self.pid,node_id=parent['node_id'])['total'],len(parent['children']))
        exported=self.store.export_selection(self.pid,node_ids=[parent['node_id']])
        self.assertTrue(exported)
        data=catalog_tree(self.store,self.pid)
        self.assertEqual(data['leaf_total'],130);self.assertTrue(any(t['children'] for t in data['themes']))
        naming.rename_node(self.store,self.pid,parent['children'][0],'人工确认设备状态')
        with closing(self.store._connect()) as c:
            found=next(b for b in visible(c,self.pid) if b['node_id']==parent['node_id'])
        self.assertEqual(found['status'],'stale')

    def test_refinement_completes_once_with_atomic_status(self):
        from services.large_package_catalog_refine import request,process_one
        paths=self.add('a',[1,0])
        request(self.store,self.pid,'a',dict(route='insufficient',evidence=[dict(source_path=paths[0],truncated=True)]),dict(needs_review=True,purity='unsure'))
        worker=Mock(store=self.store)
        worker._interrupted.return_value=False
        worker.preview_runner.run.return_value=({'sample':'补充正文'},None)
        worker._preview_fields.return_value={'category':'refined'}
        with patch.object(self.store,'finish_queue',side_effect=AssertionError('lease already consumed')):
            self.assertTrue(process_one(worker))
        self.assertEqual(self.store.get_file(self.pid,paths[0])['category'],'refined')
        with closing(self.store._connect()) as c:
            self.assertEqual(tuple(c.execute("SELECT status,lease_token FROM queue WHERE stage='catalog_refine'").fetchone()),('completed',None))
            self.assertEqual(c.execute('SELECT status FROM lp_catalog_refine').fetchone()[0],'completed')

    def test_refinement_lost_lease_cannot_overwrite_status_or_content(self):
        from services.large_package_catalog_refine import request,process_one
        paths=self.add('a',[1,0])
        request(self.store,self.pid,'a',dict(route='insufficient',evidence=[dict(source_path=paths[0],truncated=True)]),dict(needs_review=True,purity='unsure'))
        worker=Mock(store=self.store);worker._interrupted.return_value=False
        def reclaimed(*args,**kwargs):
            with closing(self.store._connect()) as c:
                c.execute("UPDATE queue SET lease_token='new-owner' WHERE stage='catalog_refine'")
            return {'sample':'过期正文'},None
        worker.preview_runner.run.side_effect=reclaimed
        worker._preview_fields.return_value={'category':'expired'}
        self.assertTrue(process_one(worker))
        self.assertNotEqual(self.store.get_file(self.pid,paths[0])['category'],'expired')
        with closing(self.store._connect()) as c:
            self.assertEqual(tuple(c.execute("SELECT status,lease_token FROM queue WHERE stage='catalog_refine'").fetchone()),('running','new-owner'))
            self.assertEqual(c.execute('SELECT status FROM lp_catalog_refine').fetchone()[0],'queued')

    def test_missing_parent_name_can_retry_next_round_and_semantic_wording_survives(self):
        from services import large_package_catalog_tree as tree
        for i in range(3):self.add('g%d'%i,[1,.01*i])
        rnd=naming.ensure_round(self.store,self.pid)
        with patch.object(tree,'TOPIC_REGENERATION_ATTEMPTS',3), patch('services.large_package_catalog_topics.name_topics',return_value={}) as names:
            for attempt in range(1,4):
                self.assertTrue(tree.build(self.store,self.pid,rnd,model_client=Mock()))
                with closing(self.store._connect()) as c:
                    saved=json.loads(c.execute('SELECT payload FROM lp_catalog_tree WHERE package_id=?',(self.pid,)).fetchone()[0])
                self.assertEqual(saved['topic_regeneration_attempts'],attempt)
                self.assertEqual(saved['attempted_round'],rnd['id'])
            self.assertFalse(tree.build(self.store,self.pid,rnd,model_client=Mock()))
            self.assertEqual(names.call_count,3)
        with patch('services.large_package_catalog_topics.name_topics',side_effect=lambda _s,_p,b,*a:{k:'工业设备健康管理' for k in b}):
            self.assertTrue(tree.build(self.store,self.pid,dict(rnd,id='next-round'),model_client=Mock()))
        with closing(self.store._connect()) as c:
            payload=json.loads(c.execute('SELECT payload FROM lp_catalog_tree WHERE package_id=?',(self.pid,)).fetchone()[0])
        self.assertEqual(payload['nodes'][0]['name'],'工业设备健康管理')
        self.assertFalse(payload['pending_names'])

    def test_worker_retries_pending_parent_in_same_round_without_resetting_budget(self):
        from large_package_worker import LargePackageWorker
        from services import large_package_catalog_tree as tree
        for i in range(3):self.add('g%d'%i,[1,.01*i])
        worker=LargePackageWorker(store=self.store)
        for runner in worker._preview_runners:self.addCleanup(runner.close)
        rnd=naming.ensure_round(self.store,self.pid,'topics',min_seconds=480)
        calls=[]
        def names(_store,_pid,buckets,*args,**kwargs):
            calls.append(1)
            return {} if len(calls)<3 else {k:'工业设备健康管理' for k in buckets}
        with patch.object(Config,'LP_TOPIC_MERGE',True), patch.object(tree,'TOPIC_REGENERATION_ATTEMPTS',3), patch.object(naming,'ensure_round',return_value=rnd) as ensure, patch('services.large_package_catalog_topics.name_topics',side_effect=names), patch('services.large_package_catalog_merge.consolidate',return_value=0):
            for attempt in range(1,4):
                self.assertTrue(worker._merge_topics_once(self.pid))
                with closing(self.store._connect()) as c:
                    self.assertEqual(tree.stale(c,self.pid),attempt<3)
                    payload=json.loads(c.execute('SELECT payload FROM lp_catalog_tree WHERE package_id=?',(self.pid,)).fetchone()[0])
                self.assertEqual(payload['attempted_round'],rnd['id'])
                self.assertEqual(payload['topic_regeneration_attempts'],attempt)
            self.assertFalse(worker._merge_topics_once(self.pid))
            self.assertEqual(ensure.call_count,3)
            self.assertEqual([call.args[2] for call in ensure.call_args_list],['topics']*3)
        self.assertEqual(len(calls),3)
        self.assertFalse(payload['pending_names'])
        self.assertEqual(payload['nodes'][0]['name'],'工业设备健康管理')

    def test_worker_stops_same_round_parent_retries_at_persisted_limit(self):
        from large_package_worker import LargePackageWorker
        from services import large_package_catalog_tree as tree
        for i in range(3):self.add('g%d'%i,[1,.01*i])
        worker=LargePackageWorker(store=self.store)
        for runner in worker._preview_runners:self.addCleanup(runner.close)
        with patch.object(Config,'LP_TOPIC_MERGE',True), patch.object(tree,'TOPIC_REGENERATION_ATTEMPTS',2), patch('services.large_package_catalog_topics.name_topics',return_value={}) as names, patch('services.large_package_catalog_merge.consolidate',return_value=0):
            self.assertTrue(worker._merge_topics_once(self.pid))
            self.assertTrue(worker._merge_topics_once(self.pid))
            for _ in range(4):self.assertFalse(worker._merge_topics_once(self.pid))
            with closing(self.store._connect()) as c:
                self.assertFalse(tree.stale(c,self.pid))
                payload=json.loads(c.execute('SELECT payload FROM lp_catalog_tree WHERE package_id=?',(self.pid,)).fetchone()[0])
            self.assertEqual(names.call_count,2)
            self.assertEqual(payload['topic_regeneration_attempts'],2)
            self.assertTrue(payload['pending_names'])

    def test_worker_rebuilds_parent_after_manual_rename_even_when_counts_unchanged(self):
        from large_package_worker import LargePackageWorker
        for i in range(3):self.add('g%d'%i,[1,.01*i])
        worker=LargePackageWorker(store=self.store);worker._topics_rebuild_debounce=0
        with patch.object(Config,'LP_TOPIC_MERGE',True),patch.object(naming,'merge_topics',return_value=True) as build:
            self.assertTrue(worker._merge_topics_once(self.pid))
            naming.rename_node(self.store,self.pid,'g0','人工确认运行参数监测')
            self.assertTrue(worker._merge_topics_once(self.pid))
            self.assertFalse(worker._merge_topics_once(self.pid))
            self.assertEqual(build.call_count,2)


if __name__=='__main__':unittest.main()

