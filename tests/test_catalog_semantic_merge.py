import json
import unittest
from contextlib import closing
from unittest.mock import patch
import numpy as np

from services import large_package_catalog_merge as merge
from services import large_package_catalog_naming as naming
from services import large_package_semantic as semantic


class MergeModel:
    model = 'merge-fixture'
    supports_json_response_format = True

    def __init__(self, change=None):
        self.calls = 0
        self.change = change

    def chat(self, system, user, **kwargs):
        self.calls += 1
        value = json.loads(user)['sets'][0]
        output = {'sets':[{'id':value['id'],'merges':[{
            'ids':[leaf['id'] for leaf in value['leaves']],
            'name':'设备远程证明与证据验证',
            'description':'设备提交可信证据，验证方校验设备身份和运行状态。'}]}]}
        if self.change:
            self.change(output)
        return {'content':json.dumps(output,ensure_ascii=False), 'usage':{'total_tokens':100}}


class SemanticMergeTests(unittest.TestCase):
    def setUp(self):
        from tests.test_catalog_batch import CatalogBatchTests
        self.fixture = CatalogBatchTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store,self.pid = self.fixture.store,self.fixture.pid
        self.ids = self.fixture.gids
        names = ['设备远程证明与证据校验','设备远程证明与可信证据验证','内存数据加密机制','通信传输协议分析']
        with closing(self.store._connect()) as c:
            c.execute('UPDATE large_catalog_groups SET anchor=? WHERE id=?',(np.eye(4,dtype=np.float32)[0].tobytes(),self.ids[1]))
            c.execute('UPDATE large_catalog_members SET vector=? WHERE group_id=?',(np.eye(4,dtype=np.float32)[0].tobytes(),self.ids[1]))
            for i,gid in enumerate(self.ids):
                rev = c.execute('SELECT content_revision FROM large_catalog_groups WHERE id=?',(gid,)).fetchone()[0]
                evidence = json.dumps([{'id':'E1','source_path':'file%d.txt'%i,'text':names[i]+'具体内容证据','headings':[]}])
                c.execute('INSERT OR REPLACE INTO lp_catalog_labels VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                          (self.pid,gid,rev,1,names[i],names[i]+'的具体方法及适用条件。',evidence,'fixture','digest',0,0,0))
        self.round = naming.ensure_round(self.store,self.pid)
        self.model = MergeModel()
        scope_patch=patch('services.large_package_catalog_topics.semantic_centroids',return_value={})
        scope_patch.start()
        self.addCleanup(scope_patch.stop)

    def run_merge(self):
        return merge.consolidate(self.store,self.pid,self.round,model_client=self.model)

    def membership(self):
        with closing(self.store._connect()) as c:
            return [tuple(r) for r in c.execute('SELECT path,group_id,input_hash FROM large_catalog_members ORDER BY path')]

    def test_equivalent_scopes_merge_preserving_files_hashes_and_history(self):
        before = self.membership()
        files = [self.store.get_file(self.pid,'file%d.txt'%i) for i in range(4)]
        self.assertEqual(self.run_merge(),1)
        after = self.membership()
        self.assertEqual([(r[0],r[2]) for r in before],[(r[0],r[2]) for r in after])
        self.assertEqual(len({r[1] for r in after}),3)
        for i in range(4):
            current = self.store.get_file(self.pid,'file%d.txt'%i)
            for key in ('local_summary','deep_summary','sha256','quick_status','deep_status'):
                self.assertEqual(files[i][key],current[key])
        with closing(self.store._connect()) as c:
            for row in c.execute('SELECT id,member_count,content_revision,result FROM large_catalog_groups'):
                self.assertEqual(row['member_count'],c.execute('SELECT COUNT(*) FROM large_catalog_members WHERE group_id=?',(row['id'],)).fetchone()[0])
                if row['member_count']==2:
                    self.assertEqual(json.loads(row['result'])['semantic_merge_revision'],row['content_revision'])
            audit = c.execute("SELECT reason FROM lp_workflow_events WHERE source='semantic_leaf_merge'").fetchone()
            self.assertEqual(len(json.loads(audit[0])['sources']),2)
        self.assertEqual(self.run_merge(),0)
        self.assertEqual(self.model.calls,1)

    def test_negative_verdict_is_cached_without_merging(self):
        self.model.change = lambda output: output['sets'][0].update(merges=[])
        before = self.membership()
        self.assertEqual(self.run_merge(),0)
        self.assertEqual(self.run_merge(),0)
        self.assertEqual(self.model.calls,1)
        self.assertEqual(before,self.membership())

    def test_unknown_reference_never_mutates_members(self):
        self.model.change = lambda output: output['sets'][0]['merges'][0].update(ids=['L0','L99'])
        before = self.membership()
        self.assertEqual(self.run_merge(),0)
        self.assertEqual(before,self.membership())

    def test_manual_label_is_never_merged(self):
        with closing(self.store._connect()) as c:
            c.execute('UPDATE lp_catalog_labels SET locked=1 WHERE group_id=?',(self.ids[0],))
        self.assertEqual(self.run_merge(),0)
        self.assertEqual(self.model.calls,0)

    def test_pause_during_inference_rejects_merge(self):
        self.model.change = lambda _: self.store.update_package(self.pid,status='paused')
        before = self.membership()
        self.assertEqual(self.run_merge(),0)
        self.assertEqual(before,self.membership())

    def test_changed_revision_rejects_entire_merge(self):
        def change(_):
            with closing(self.store._connect()) as c:
                c.execute('UPDATE large_catalog_groups SET content_revision=content_revision+1 WHERE id=?',(self.ids[0],))
        self.model.change = change
        before = self.membership()
        self.assertEqual(self.run_merge(),0)
        self.assertEqual(before,self.membership())

    def test_queued_names_defer_consolidation(self):
        with closing(self.store._connect()) as c:
            c.execute('INSERT INTO lp_catalog_jobs(package_id,group_id,revision) VALUES(?,?,?)',(self.pid,self.ids[0],1))
        self.assertEqual(self.run_merge(),0)
        self.assertEqual(self.model.calls,0)

    def test_verified_merge_is_not_split_just_for_size(self):
        self.run_merge()
        with closing(self.store._connect()) as c:
            group = dict(c.execute('SELECT * FROM large_catalog_groups WHERE member_count=2').fetchone())
        with patch.object(semantic.np,'frombuffer',side_effect=AssertionError('must not split verified scope')):
            self.assertEqual(semantic._split_one(self.store,self.pid,group,1),0)

    def test_explicit_identifiers_prevent_automatic_candidate_merge(self):
        with closing(self.store._connect()) as c:
            c.execute("UPDATE lp_catalog_labels SET name='ABC设备远程证明与证据校验' WHERE group_id=?",(self.ids[0],))
            c.execute("UPDATE lp_catalog_labels SET name='XYZ设备远程证明与证据校验' WHERE group_id=?",(self.ids[1],))
        self.assertEqual(self.run_merge(),0)
        self.assertEqual(self.model.calls,0)

    def test_stale_outlier_decision_cannot_move_members(self):
        gid=self.ids[0]
        with closing(self.store._connect()) as c:
            c.execute('UPDATE large_catalog_members SET group_id=? WHERE group_id=?',(gid,self.ids[1]))
            revision=c.execute('SELECT content_revision FROM large_catalog_groups WHERE id=?',(gid,)).fetchone()[0]
        material={'revision':revision-1,'evidence':[{'id':'E1','source_path':'file0.txt'}]}
        before=self.membership()
        with patch.object(naming,'_leaf_split_enabled',return_value=10):
            self.assertFalse(naming.split_off_outliers(self.store,self.pid,gid,material,{'outliers':['E1']}))
        self.assertEqual(before,self.membership())


class ClusterAlgorithmTests(unittest.TestCase):
    def test_candidate_scan_can_cover_more_than_twenty_four_sets(self):
        rows=[]
        for i in range(30):
            v=np.eye(30,dtype=np.float32)[i]
            for j in range(2):
                rows.append({'group_id':'g%d-%d'%(i,j),'name':'设备证据验证研究','files':3,
                             'anchor':v.tobytes()})
        self.assertEqual(len(merge.candidates(rows,limit=len(rows))),30)

    def test_equivalent_scope_vectors_propose_different_wording_for_review(self):
        rows=[{'group_id':'a','name':'高校人才培养机制','files':4,'anchor':b'',
               '_scope_vector':np.array([1.,0.],dtype=np.float32)},
              {'group_id':'b','name':'大学教学体系建设','files':3,'anchor':b'',
               '_scope_vector':np.array([1.,0.],dtype=np.float32)}]
        self.assertEqual(len(merge.candidates(rows)),1)

    def test_small_but_unrelated_topics_are_not_forced_into_one_leaf(self):
        X=np.vstack([np.tile([1.,0.],(6,1)),np.tile([0.,1.],(6,1))]).astype(np.float32)
        labels=semantic._balanced_labels(X,2,60,20)
        self.assertEqual(len(set(labels)),2)
        self.assertEqual(len(set(labels[:6])),1)
        self.assertNotEqual(labels[0],labels[-1])

    def test_catalog_cleaning_removes_template_but_keeps_real_title(self):
        text='# 01_法律法规与司法案例\n## Callaway, Minnesota\nPopulation and geography.'
        cleaned,headings=semantic.clean_catalog_content(text,['01_法律法规与司法案例','Callaway, Minnesota'])
        self.assertNotIn('01_法律法规与司法案例',cleaned)
        self.assertIn('Callaway, Minnesota',cleaned)
        self.assertEqual(headings,['Callaway, Minnesota'])

    def test_catalog_cleaning_preserves_substantive_numbered_heading(self):
        text='2024年法律修订\n第1章 合同条款\n正文内容'
        self.assertEqual(semantic.clean_catalog_content(text,['第1章 合同条款']),
                         (text,['第1章 合同条款']))

    def test_template_metadata_is_not_embedding_authority(self):
        text='01_法律法规与司法案例 编制单位：数据中心 日期：2026-09-17 Actual geography content'
        cleaned,headings=semantic.clean_catalog_content(text,['01_法律法规与司法案例','编制单位：数据中心 日期：2026-09-17'])
        self.assertEqual(cleaned,'Actual geography content')
        self.assertEqual(headings,[])

    def test_accelerator_failure_falls_back_without_losing_vectors(self):
        X=np.array([[1.,0.],[.99,.01],[0.,1.],[.01,.99]],dtype=np.float32)
        with patch('sklearn.cluster.KMeans',side_effect=RuntimeError('unavailable')):
            centers=semantic._kmeans_fit(X,2,10)
        self.assertEqual(centers.shape,(2,2))
        self.assertTrue(np.isfinite(centers).all())

    def test_identical_vectors_keep_one_semantic_group_with_file_pagination(self):
        X = np.tile(np.array([[1.,0.]],dtype=np.float32),(101,1))
        with patch.object(semantic,'_kmeans_fit',side_effect=AssertionError('identical vectors need no fitting')):
            labels = semantic._balanced_labels(X,8,30,40)
        self.assertEqual(len(labels),101)
        self.assertEqual(set(labels),{0})

    def test_refinement_improves_assignment_without_exceeding_capacity(self):
        X = np.array([[1,0],[.99,.01],[0,1],[.01,.99],[1,0],[0,1]],dtype=np.float32)
        X /= np.linalg.norm(X,axis=1,keepdims=True)
        labels = [0,0,1,1,1,0]
        result = semantic._refine_catalog_labels(X,labels,3)
        self.assertEqual(len(result),len(X))
        self.assertLessEqual(max(result.count(i) for i in set(result)),3)
        self.assertEqual(result,semantic._refine_catalog_labels(X,labels,3))


if __name__=='__main__':unittest.main()
