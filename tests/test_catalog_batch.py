import json
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import Future
import numpy as np

from config import Config
from services.large_package_isolated import LargePackageStore
from services import large_package_semantic as semantic
from services import large_package_catalog_naming as naming
from services.large_package_catalog_batch import process_batch
from large_package_worker import LargePackageWorker


class BatchModel:
    model='fixture-batch-model'
    supports_json_response_format=True
    def __init__(self):self.calls=0;self.bad=False;self.pause=None
    def chat(self,system,user,**kwargs):
        self.calls+=1
        groups=json.loads(user)['groups'];results=[]
        for group in groups:
            results.append({'id':group['id'],'name':'设备证据与可信验证流程','description':'描述设备证据的收集、验证和可信状态确认过程。','evidence_ids':['E999' if self.bad else group['evidence'][0]['id']],'needs_review':False})
        if self.pause:self.pause()
        return {'content':json.dumps({'groups':results},ensure_ascii=False),'usage':{'total_tokens':321},'finish_reason':'stop'}


class CatalogBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.patches=[patch.object(Config,'SCAN_ALLOWED_ROOTS',()),patch.object(Config,'LP_CATALOG_MODEL_NAMING',True),patch.object(Config,'LP_CATALOG_SECONDS',60),patch.object(Config,'LP_CATALOG_BATCH_SIZE',4),patch.object(Config,'LP_CATALOG_BATCH_INPUT_TOKENS',10000),patch.object(Config,'LP_CATALOG_TOTAL_TOKENS',100000)]
        for p in self.patches:p.start()
        self.store=LargePackageStore(self.root/'state.db');self.pid=self.store.create(self.root)['id'];self.model=BatchModel()
        self.gids=[]
        for i in range(4):
            path='file%d.txt'%i
            self.store.upsert_file(self.pid,{'path':path,'name':path,'size':100})
            self.store.update_file(self.pid,path,quick_status='completed',local_summary={'sample':('Device evidence verification and trusted state confirmation. %d '%i)*3,'portrait':{'fields':[],'headings':[]}})
            from services.large_package_catalog import material
            digest=material(self.store.get_file(self.pid,path))[1]
            semantic._group(self.store,self.pid,path,digest,np.eye(4,dtype=np.float32)[i],'fixture')
            self.gids.append(self.store.get_file(self.pid,path)['local_summary']['portrait']['catalog_group'])
        semantic.name_batch(self.store,self.pid)
        self.store.update_package(self.pid,status='waiting_for_selection',phase='catalog_ready',progress=70)
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def test_four_names_one_call_without_changing_files_or_membership(self):
        before=[self.store.get_file(self.pid,'file%d.txt'%i) for i in range(4)]
        self.assertTrue(process_batch(self.store,self.pid,model_client=self.model))
        self.assertEqual(self.model.calls,1)
        state=naming.state(self.store,self.pid)
        self.assertEqual(state['named_nodes'],4);self.assertEqual(state['requests'],1);self.assertEqual(state['tokens'],321)
        for i in range(4):
            after=self.store.get_file(self.pid,'file%d.txt'%i)
            for key in ('local_summary','deep_summary','sha256','deep_status','quick_status'):
                self.assertEqual(after[key],before[i][key])
        with closing(self.store._connect()) as c:
            self.assertEqual([r[0] for r in c.execute('SELECT member_count FROM large_catalog_groups')],[1]*4)
    def test_replayed_batch_uses_persisted_labels_without_new_requests(self):
        process_batch(self.store,self.pid,model_client=self.model)
        with closing(self.store._connect()) as c:
            c.execute("UPDATE lp_catalog_rounds SET hierarchy_status='completed'")
        self.assertFalse(process_batch(self.store,self.pid,model_client=self.model))
        self.assertEqual(self.model.calls,1)
    def test_manual_lock_is_not_overwritten(self):
        naming.rename_node(self.store,self.pid,self.gids[0],'人工确认的设备目录')
        process_batch(self.store,self.pid,model_client=self.model)
        with closing(self.store._connect()) as c:
            row=c.execute('SELECT name,locked FROM lp_catalog_labels WHERE group_id=?',(self.gids[0],)).fetchone()
        self.assertEqual(tuple(row),('人工确认的设备目录',1))
    def test_input_limit_splits_batch_without_shortening_evidence(self):
        inputs=[]
        original=self.model.chat
        def capture(system,user,**kwargs):
            inputs.append(json.loads(user));return original(system,user,**kwargs)
        self.model.chat=capture
        with patch.object(Config,'LP_CATALOG_BATCH_INPUT_TOKENS',1500):
            process_batch(self.store,self.pid,model_client=self.model)
        self.assertGreaterEqual(len(inputs),1)
        self.assertLess(len(inputs[0]['groups']),4)
        for group in inputs[0]['groups']:
            self.assertTrue(group['evidence'][0]['text'])
    def test_invalid_evidence_cannot_publish_any_name_and_retries_are_durable(self):
        self.model.bad=True;process_batch(self.store,self.pid,model_client=self.model)
        self.assertEqual(naming.state(self.store,self.pid)['named_nodes'],0)
        with closing(self.store._connect()) as c:
            jobs=c.execute('SELECT status,attempts FROM lp_catalog_jobs').fetchall()
        self.assertTrue(all(tuple(j)==('queued',1) for j in jobs))
    def test_pause_rejects_inflight_result_and_preserves_budget(self):
        self.model.pause=lambda:self.store.update_package(self.pid,status='paused')
        process_batch(self.store,self.pid,model_client=self.model)
        self.assertEqual(naming.state(self.store,self.pid)['named_nodes'],0)
        self.assertEqual(naming.state(self.store,self.pid)['requests'],1)
    def test_revision_change_only_discards_changed_group(self):
        self.model.pause=lambda:self._bump(self.gids[0])
        process_batch(self.store,self.pid,model_client=self.model)
        self.assertEqual(naming.state(self.store,self.pid)['named_nodes'],3)
    def _bump(self,gid):
        with closing(self.store._connect()) as c:
            c.execute('UPDATE large_catalog_groups SET content_revision=content_revision+1 WHERE id=?',(gid,))
    def test_old_naming_stage_immediately_allows_selection_with_model_inflight(self):
        self.store.update_package(self.pid,status='running',phase='catalog_naming',progress=66)
        worker=LargePackageWorker(self.store);worker._naming_future=Future()
        worker.run_once()
        self.assertEqual(self.store.get(self.pid)['status'],'waiting_for_selection')
        self.assertEqual(self.store.get(self.pid)['progress'],70)
    def test_member_count_migration_and_content_update_do_not_double_count(self):
        row=self.store.get_file(self.pid,'file0.txt')
        from services.large_package_catalog import material
        digest=material(row)[1]
        semantic._group(self.store,self.pid,'file0.txt',digest,np.eye(4,dtype=np.float32)[0],'fixture')
        self.store.update_file(self.pid,'file0.txt',local_summary={'sample':'Changed evidence verification content. '*5,'portrait':{'fields':[],'headings':[]}})
        digest=material(self.store.get_file(self.pid,'file0.txt'))[1]
        semantic._group(self.store,self.pid,'file0.txt',digest,np.eye(4,dtype=np.float32)[1],'fixture')
        with closing(self.store._connect()) as c:
            for row in c.execute('SELECT id,member_count FROM large_catalog_groups'):
                count=c.execute('SELECT COUNT(*) FROM large_catalog_members WHERE group_id=?',(row['id'],)).fetchone()[0]
                self.assertEqual(row['member_count'],count)
        LargePackageStore(self.root/'state.db')


if __name__=='__main__':unittest.main()
