"""Enabled large-package lanes against real durable authorization and snapshots."""
import concurrent.futures
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import closing
from config import Config
from sjfx_protection import business
from sjfx_protection.store import Store,ProtectionError
from sjfx_protection.registration import PackageProtection
from services.large_package_isolated import LargePackageStore
from services.large_package_protection import large_package_execution,guarded_model_call,record_report_artifact,require_report_artifact,run_saved_lane
from services.large_package_preview_runner import PreviewRunner,_serve
from services.large_package_semantic import embed_many
from large_package_worker import LargePackageWorker,_source_snapshot

class LargeProtectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.root=self.base/'data';self.root.mkdir()
        self.file=self.root/'a.txt';self.file.write_text('approved research evidence',encoding='utf-8')
        self.state=Store(self.base/'file_protection');self.account=True
        self.package=PackageProtection(self.state,[str(self.root)],authorize_root=lambda owner,root:owner=='alice',authorize_owner=lambda owner,user:self.account)
        self.env=patch.dict(os.environ,{'SJFX_STATE_DIR':str(self.base),'SCAN_ALLOWED_ROOTS':str(self.root),'SJFX_PROTECTION_TEST_STATIC_ONLY':'1'});self.env.start()
        self.roots=patch.object(Config,'SCAN_ALLOWED_ROOTS',());self.roots.start()
        self.lp=LargePackageStore(self.base/'large.db')
        class Configured:
            store=self.state;package=self.package
            @property
            def enabled(inner):return self.package.policy()['enabled']
        self.configured=Configured();business.configure(self.configured)
        import sqlite3
        with closing(sqlite3.connect(self.base/'agent.db')) as db:
            db.execute('CREATE TABLE users(user_id TEXT,owner_id TEXT,status TEXT,must_change_pw INTEGER,role TEXT)')
            db.execute("INSERT INTO users VALUES('u1','alice','active',0,'user')");db.commit()
        with self.state.db(True) as db:db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('package-root-grants:alice',json.dumps([str(self.root)])))
        self.package.update_policy('admin',True,{'enabled':True,'allowed_registration_modes':['local'],'inspection_profile':{'required_engines':['static']}},'isolated static-only test')
        reg=self.package.register('alice','u1',str(self.root),'test','local');self.package.process_job(reg['job_id']);self.rid=reg['registration_id']
        with business.activate({}):created=self.lp.create(self.root,'alice')
        self.pid=created['id'];self.package.process_job(created['protection_intake']['job_id'])
        self.lp.admit_protection(self.pid)
        st=self.file.stat();self.lp.upsert_file(self.pid,{'path':'a.txt','name':'a.txt','size':st.st_size,'modified_at_ns':st.st_mtime_ns})
    def tearDown(self):
        business.configure(None);self.roots.stop();self.env.stop();self.tmp.cleanup()
    def assertCode(self,code,call,*args,**kwargs):
        with self.assertRaises(ProtectionError) as error:call(*args,**kwargs)
        self.assertEqual(error.exception.code,code)
    def test_thread_lane_restores_persisted_context_and_current_account(self):
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result=pool.submit(run_saved_lane,self.lp,self.pid,lambda:business.context()['registration_id']).result()
        self.assertEqual(result,self.rid);self.assertIsNone(business.execution.get())
        self.account=False
        self.assertCode('account_disabled',run_saved_lane,self.lp,self.pid,lambda:None)
        self.assertEqual(self.lp.get(self.pid)['status'],'paused')
    def test_summary_provenance_roundtrip_and_policy_change_rejects(self):
        self.lp.update_file(self.pid,'a.txt',local_summary={'sample':'approved evidence','portrait':{}})
        row=self.lp.get_file(self.pid,'a.txt');self.assertEqual(row['protection_context']['registration_id'],self.rid)
        self.package.update_policy('admin',True,{'operation_policy':{'anomaly_count':25}},'new policy revision')
        self.assertCode('context_stale',self.lp.get_file,self.pid,'a.txt')
    def test_historical_sql_content_denied_and_explicit_rebuild_succeeds(self):
        with closing(__import__('sqlite3').connect(self.lp.db_path)) as db:
            db.execute("UPDATE files SET local_summary=? WHERE package_id=?",(json.dumps({'sample':'legacy unknown'}),self.pid));db.commit()
        self.assertCode('cache_output_mismatch',self.lp.get_file,self.pid,'a.txt')
        with business.activate({}):self.lp.refresh_protection_context(self.pid,'alice',reset_cache=True)
        self.assertEqual(self.lp.get(self.pid)['phase'],'inventory');self.assertEqual(self.lp.counts(self.pid)['total_files'],0)
        self.lp.upsert_file(self.pid,{'path':'a.txt','name':'a.txt','size':self.file.stat().st_size})
        self.lp.update_file(self.pid,'a.txt',local_summary={'sample':'fresh source','portrait':{}})
        self.assertEqual(self.lp.get_file(self.pid,'a.txt')['local_summary']['sample'],'fresh source')
    def test_context_refresh_owner_mismatch_cannot_rebind(self):
        self.assertCode('package_not_found',self.lp.refresh_protection_context,self.pid,'bob',reset_cache=True)
    def test_model_account_revocation_during_call_discards_output(self):
        class Client:
            def chat(inner,*args,**kwargs):self.account=False;return {'content':'must discard'}
        with self.assertRaises(ProtectionError),large_package_execution(self.lp,self.pid):
            self.assertCode('account_disabled',guarded_model_call,Client(),'chat','system','evidence')
    def test_embedding_pool_carries_context_and_rechecks_each_chunk(self):
        calls=[]
        class Client:
            max_batch_size=1;model='fixture'
            def embed(inner,texts):calls.append(business.context()['registration_id']);return [[1.0] for _ in texts]
        with large_package_execution(self.lp,self.pid),patch('services.large_package_semantic.embed_concurrency',return_value=2):
            result=embed_many(Client(),['a','b','c'],factory=Client)
        self.assertEqual(result,[[1.0]]*3);self.assertEqual(calls,[self.rid]*3)
    def test_snapshot_reader_rejects_source_swap_before_reading(self):
        with self.assertRaises(ProtectionError),large_package_execution(self.lp,self.pid):
            self.file.write_text('swapped unknown content',encoding='utf-8')
            with self.assertRaises(ProtectionError):
                _source_snapshot(self.root,{'path':'a.txt'})
    def test_low_level_text_read_rejects_changed_source_before_reading(self):
        from services.scanner import extract_text
        with self.assertRaises(ProtectionError),large_package_execution(self.lp, self.pid):
            self.file.write_text('new unapproved content', encoding='utf-8')
            with self.assertRaises(ProtectionError):
                extract_text(self.file)
    def test_actual_text_parser_reads_direct_local_path_and_rechecks_version(self):
        from services.unified_parser import UnifiedDocumentParser
        parser = UnifiedDocumentParser(artifacts_path=self.base/'parser-artifacts')
        with self.assertRaises(ProtectionError),large_package_execution(self.lp, self.pid):
            document = parser.parse(self.file, 'a.txt')
            self.assertIn('approved research evidence', document['text'])
            self.assertEqual(business.context()['registration_id'], self.rid)
            self.file.write_text('unapproved replacement', encoding='utf-8')
            with self.assertRaises(ProtectionError):
                parser.parse(self.file, 'a.txt')
    def test_report_artifact_is_immutable_and_not_scanned_on_consume(self):
        target=self.base/'report.json';target.write_text('{"summary":"approved"}',encoding='utf-8')
        with large_package_execution(self.lp,self.pid):
            snapshot=record_report_artifact(target)
            self.assertNotEqual(snapshot,target)
            with patch.object(self.package,'_inspect_artifact',side_effect=AssertionError('HTTP scanning forbidden')):
                self.assertEqual(require_report_artifact(target),snapshot)
            target.write_text('{"summary":"tampered"}',encoding='utf-8')
            self.assertCode('artifact_changed',require_report_artifact,target)
    def test_preview_runner_requires_explicit_context_before_start(self):
        self.assertCode('protection_context_required',PreviewRunner().run,self.root,{'path':'a.txt'})
    def test_preview_child_rechecks_and_returns_protection_error(self):
        messages=[]
        class Connection:
            count=0
            def recv(inner):
                if inner.count:raise EOFError()
                inner.count+=1
                return {'root':str(self.root),'item':{'path':'a.txt'},'protection_context':self.lp.get(self.pid)['protection_context']}
            def send(inner,result):messages.append(result)
            def close(inner):pass
        self.account=False;_serve(Connection())
        self.assertEqual(messages[0]['protection_code'],'account_disabled')
    def test_deep_model_denial_is_not_successful_fallback(self):
        worker=LargePackageWorker(store=self.lp)
        with self.assertRaises(ProtectionError),large_package_execution(self.lp,self.pid),patch.object(Config,'LLM_BACKEND','vllm'),patch.object(Config,'ENABLE_VLLM',True),patch('services.vllm.VLLMClient') as Client:
            self.account=False
            self.assertCode('account_disabled',worker._model_summary,'a.txt','approved evidence')
            Client.return_value.chat.assert_not_called()

    def test_sql_projection_tamper_rejected_even_with_source_context(self):
        self.lp.update_file(self.pid,'a.txt',local_summary={'sample':'approved original'})
        import sqlite3
        with closing(sqlite3.connect(self.lp.db_path)) as db:
            db.execute('UPDATE files SET local_summary=? WHERE package_id=?',(json.dumps({'sample':'forged body'}),self.pid));db.commit()
        with self.assertRaises(ProtectionError) as caught:
            with large_package_execution(self.lp,self.pid),closing(self.lp._connect()) as db:
                db.execute("SELECT json_extract(local_summary,'$.sample') FROM files WHERE package_id=?",(self.pid,)).fetchall()
        self.assertEqual(caught.exception.code,'cache_output_mismatch')
    def test_model_cache_content_is_produced_and_hash_checked(self):
        with large_package_execution(self.lp,self.pid),closing(self.lp._connect()) as db:
            db.execute('INSERT INTO large_catalog_names VALUES(?,?,?)',('fixture','digest','approved result'))
            self.assertEqual(db.execute('SELECT result FROM large_catalog_names WHERE digest=?',('digest',)).fetchone()[0],'approved result')
        import sqlite3
        with closing(sqlite3.connect(self.lp.db_path)) as db:
            db.execute("UPDATE large_catalog_names SET result='forged result'");db.commit()
        with large_package_execution(self.lp,self.pid),closing(self.lp._connect()) as db:
            self.assertEqual(db.execute('SELECT result FROM large_catalog_names').fetchall(),[])
            db.execute('INSERT OR REPLACE INTO large_catalog_names VALUES(?,?,?)',('fixture','digest','recomputed approved result'))
            self.assertEqual(db.execute('SELECT result FROM large_catalog_names').fetchone()[0],'recomputed approved result')

    def test_real_spawned_preview_process_reads_verified_snapshot(self):
        # 本用例验保护正确性（子进程必须读经核验的受限快照），不是 4 秒延迟预算；
        # Windows 的 spawn 冷启动会超过默认 4 秒，故放宽到上限 30 秒。
        runner=PreviewRunner()
        try:
          with patch.dict(os.environ,{'SJFX_LP_PREVIEW_SECONDS':'30'}):
            with large_package_execution(self.lp,self.pid):
                ctx=dict(business.context());row=self.lp.get_file(self.pid,'a.txt')
                result,digest=runner.run(self.root,row,protection_context=ctx)
                self.assertIn('approved research evidence',result['sample'],result)
                self.assertFalse(result.get('error'),result)
        finally:runner.close()
    def test_deep_lane_atomic_write_uses_protected_source_provenance(self):
        from services.large_package_preview import preview_file
        with large_package_execution(self.lp,self.pid):
            result,_=preview_file(self.root,self.lp.get_file(self.pid,'a.txt'))
        self.assertIn('approved research evidence',result['sample'],result)
        lease=self.lp.claim('quick',1,self.pid)[0][2]
        ok,_=self.lp.commit_stage_result(self.pid,'a.txt','quick',lease,file_fields={'local_summary':result,'quick_status':'completed'})
        self.assertTrue(ok)
        self.assertEqual(self.lp.get_file(self.pid,'a.txt')['protection_context']['registration_id'],self.rid)

    def test_embedding_fallback_does_not_send_after_account_revocation(self):
        from services.large_package_semantic import FallbackEmbeddingClient
        class Primary:
            def embed(inner,texts):self.account=False;raise RuntimeError('primary unavailable')
        class Secondary:
            def embed(inner,texts):raise AssertionError('revoked evidence must never reach fallback')
        with self.assertRaises(ProtectionError),large_package_execution(self.lp,self.pid):
            self.assertCode('account_disabled',FallbackEmbeddingClient(Primary(),Secondary()).embed,['approved text'])

    def test_recall_rerank_revocation_discards_network_output(self):
        from services.large_package_recall import rerank_candidates
        class Client:
            def chat(inner,*args,**kwargs):
                self.account=False
                return {'content':'1,2,3'}
        rows=[{'source_path':str(i),'text':'approved evidence'} for i in range(3)]
        with self.assertRaises(ProtectionError),large_package_execution(self.lp,self.pid),patch('services.large_package_recall.reranking_enabled',return_value=True),patch('services.large_package_recall._rerank_endpoint',return_value=('http://isolated.invalid','fixture')),patch('services.model_backend.create_model_client',return_value=Client()):
            self.assertCode('account_disabled',rerank_candidates,'research query',rows)
    def test_topic_embedding_lane_rechecks_before_model_send(self):
        from services.large_package_catalog_topics import semantic_centroids
        self.account=False
        self.assertCode('account_disabled',semantic_centroids,self.lp,self.pid,[],{})
    def test_large_selection_import_requires_current_source_context(self):
        from services.large_package_catalog_import import create_scan_from_large_selection
        with self.assertRaises(ProtectionError),large_package_execution(self.lp,self.pid):
            self.account=False
            self.assertCode('account_disabled',create_scan_from_large_selection,None,{'id':self.pid},{},'selection','auto','alice','key')

    def test_independent_qa_readonly_source_rejects_tampered_summary(self):
        from scripts.large_qa_worker import ReadOnlyStore
        from services.large_qa_protection import qa_store_execution
        self.lp.update_file(self.pid,'a.txt',local_summary={'sample':'approved original'})
        with closing(__import__('sqlite3').connect(self.lp.db_path)) as db:
            db.execute('UPDATE files SET local_summary=? WHERE package_id=?',(json.dumps({'sample':'forged body'}),self.pid));db.commit()
        source = ReadOnlyStore(self.lp.db_path)
        with qa_store_execution(source,self.pid),source._connect() as db:
            self.assertCode('cache_output_mismatch',lambda:db.execute('SELECT local_summary FROM files WHERE package_id=?',(self.pid,)).fetchall())
        from services.large_qa_corpus import ReadOnlySourceStore
        source = ReadOnlySourceStore(self.lp.db_path)
        with qa_store_execution(source,self.pid),source._connect() as db:
            self.assertCode('cache_output_mismatch',lambda:db.execute('SELECT local_summary FROM files WHERE package_id=?',(self.pid,)).fetchall())

    def test_independent_qa_one_pass_sync_preserves_checked_summary(self):
        from scripts.large_qa_worker import ReadOnlyStore
        from services.large_qa_index import LargeQAIndex
        from services.large_qa_protection import qa_store_execution
        self.lp.update_file(self.pid,'a.txt',local_summary={'sample':'approved research evidence'})
        source = ReadOnlyStore(self.lp.db_path)
        with qa_store_execution(source,self.pid):
            index = LargeQAIndex(self.pid,self.base/'qa',create=True)
            self.assertEqual(index.sync_store(source),1)
            with index.connect() as db:
                row = db.execute('SELECT summary FROM documents WHERE path=?',('a.txt',)).fetchone()
                self.assertIn('approved research evidence',row['summary'])
        self.assertIsNone(business.context())

    def test_real_spawned_qa_extraction_reads_checked_local_source(self):
        from services.large_qa_extract import extract_document
        info=self.file.stat()
        doc={'path':'a.txt','size':info.st_size,'mtime':info.st_mtime_ns}
        with large_package_execution(self.lp,self.pid):
            records,complete,note=extract_document(self.root,doc,timeout=40)
            self.assertTrue(complete,note)
            self.assertIn('approved research evidence',' '.join(record['text'] for record in records))

    def test_qa_revoked_account_blocks_before_process_start(self):
        from services.large_qa_extract import extract_document
        info=self.file.stat()
        with self.assertRaises(ProtectionError),large_package_execution(self.lp,self.pid),patch('services.large_qa_extract.mp.get_context') as spawn:
            self.account=False
            self.assertCode('account_disabled',extract_document,self.root,{'path':'a.txt','size':info.st_size,'mtime':info.st_mtime_ns})
            spawn.assert_not_called()

    def test_qa_child_changed_content_denies_before_parser_call(self):
        from services.large_qa_extract import _child
        from unittest.mock import Mock
        connection=Mock()
        with large_package_execution(self.lp,self.pid):
            context=dict(business.context())
        self.file.write_text('unapproved replacement',encoding='utf-8')
        with patch('services.large_qa_extract._extract') as extract:
            _child(connection,str(self.file),'a.txt',100,protection_context=context)
        extract.assert_not_called()
        self.assertEqual(connection.send.call_args.args[0][0],'protection_denied')

    def test_independent_qa_worker_once_sync_and_revoked_constructor_gate(self):
        from scripts import large_qa_worker as worker
        from services.large_qa_index import index_path
        self.lp.update_file(self.pid,'a.txt',local_summary={'sample':'approved research evidence'})
        args=SimpleNamespace(source_db=str(self.lp.db_path),base=str(self.base/'qa-worker'),package=self.pid,once=True,summaries_only=True)
        with patch('services.managed_entry.mark_role_ready'),patch.object(worker,'STOP',False),patch('builtins.print'):
            worker.run(args)
        self.assertTrue(index_path(self.pid,args.base).exists())
        args.base=str(self.base/'qa-revoked')
        self.account=False
        with patch('services.managed_entry.mark_role_ready'),patch.object(worker,'STOP',False),patch('builtins.print'),patch.object(worker,'LargeQAIndex') as constructor:
            with self.assertRaises(SystemExit) as caught:
                worker.run(args)
        self.assertEqual(caught.exception.code,1)
        constructor.assert_not_called()
        self.assertFalse(index_path(self.pid,args.base).exists())

    def test_independent_qa_callable_model_revocation_discards_output(self):
        from services.large_qa_protection import qa_model_text
        def model(system,prompt):
            self.account=False
            return 'must discard'
        with self.assertRaises(ProtectionError),large_package_execution(self.lp,self.pid):
            self.assertCode('account_disabled',qa_model_text,model,'system','approved evidence')

    def test_translation_routes_jobs_and_approval_keep_translate_operation(self):
        import ast
        import sqlite3
        from sjfx_protection.local_source import route_operation
        routes=set()
        for node in ast.walk(ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text(encoding='utf-8'))):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='route' and node.args and isinstance(node.args[0],ast.Constant):
                routes.add(node.args[0].value)
        for route in ('/api/translation/<scan_id>','/api/translations/<scan_id>','/api/translate-package/<scan_id>'):
            self.assertIn(route,routes)
            for method in ('GET','POST'):
                with self.subTest(route=route,method=method):
                    self.assertEqual(route_operation(route.replace('<scan_id>','scan-fixture'),method,{}),'translate')
        self.package.update_policy('admin',True,{'operation_policy':{'translate':'approval'}},'require exact translation approval')
        with self.state.db() as db:
            self.assertIsNotNone(db.execute('SELECT id FROM registrations WHERE id=?',(self.rid,)).fetchone())
        approval=self.package._approve_targets('alice',self.rid,'translate','admin','isolated translation approval',is_admin=True)
        read_context=self.package.require_operation('alice',registration_id=self.rid,operation='read')['context']
        translate_context=self.package.require_operation('alice',registration_id=self.rid,operation='translate',approval_id=approval['approval_id'])['context']
        db_path=self.base/'agent.db'
        with closing(sqlite3.connect(db_path)) as db:
            db.execute('CREATE TABLE scans(id TEXT PRIMARY KEY,owner_id TEXT,root_path TEXT)')
            db.execute('INSERT INTO scans VALUES(?,?,?)',('scan-fixture','alice',str(self.root)))
            db.execute('CREATE TABLE analysis_jobs(id TEXT PRIMARY KEY,scan_id TEXT,owner_id TEXT,task_type TEXT,options TEXT)')
            db.commit()
        with patch.object(Config,'DB_PATH',db_path),patch.object(Config,'DATA_DIR',self.base):
            for task in ('translate_document','translate_package'):
                with business.activate(read_context):
                    self.assertCode('operation_approval_required',business.capture_resource_job,'alice','scan-fixture',{'path':'a.txt'},task)
                with business.activate(translate_context):
                    options=business.capture_resource_job('alice','scan-fixture',{'path':'a.txt'},task)
                self.assertEqual(options['protection_context']['operation'],'translate')
                self.assertEqual(options['protection_context']['approval_id'],approval['approval_id'])
                with closing(sqlite3.connect(db_path)) as db:
                    db.execute('INSERT INTO analysis_jobs VALUES(?,?,?,?,?)',(task,'scan-fixture','alice',task,json.dumps(options)))
                    db.commit()
                    db.row_factory=sqlite3.Row
                    job=dict(db.execute('SELECT * FROM analysis_jobs WHERE id=?',(task,)).fetchone())
                job['options']=json.loads(job['options'])
                with business.job_execution(job):
                    self.assertEqual(business.context()['operation'],'translate')
                    self.assertEqual(business.context()['approval_id'],approval['approval_id'])
                self.assertIsNone(business.context())

    def test_real_spawned_ordinary_parser_preserves_parse_denial_type(self):
        from services.unified_parser import UnifiedDocumentParser
        from services.parse_isolation import IsolatedParserRunner
        parser=UnifiedDocumentParser(artifacts_path=self.base/'ordinary-parser-artifacts')
        runner=IsolatedParserRunner(parser)
        try:
            with large_package_execution(self.lp,self.pid):
                document=runner.parse(self.file,'a.txt',mode='fast',timeout=40)
                self.assertIn('approved research evidence',document['text'])
            self.package.update_policy('admin',True,{'operation_policy':{'parse':'deny'}},'isolated parser rule')
            fresh=self.package.require_operation('alice',registration_id=self.rid,operation='analyze')['context']
            with business.activate(fresh):
                self.assertCode('operation_denied',runner.parse,self.file,'a.txt',mode='fast',timeout=40)
        finally:
            runner.close()

if __name__=='__main__':unittest.main()
