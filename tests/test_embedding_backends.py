"""Real loopback HTTP contract tests; no model runtime or external service."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
import tempfile
from pathlib import Path
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.embedding_backend import (OpenAIEmbeddingClient, OllamaEmbeddingClient,
    create_embedding_client, artifact_fingerprint, validate_vectors, require_index_identity)
from services.model_contract import LocalModelError


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.state={'mode':'ok','posts':[],'gets':[],'revision':'weights-1','failures':0}
        state=self.state
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def send(self,data,status=200):
                raw=json.dumps(data).encode();self.send_response(status)
                self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_GET(self):
                state['gets'].append(self.path)
                if state['mode']=='redirect':
                    self.send_response(302);self.send_header('Location','http://127.0.0.1:9/exfiltrate');self.end_headers();return
                if self.path=='/api/tags': self.send({'models':[{'name':'embed:latest','digest':state['revision']}]})
                else: self.send({'data':[{'id':'embed','revision':state['revision']}]})
            def do_POST(self):
                request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                state['posts'].append((self.path,request))
                if state['failures']:
                    state['failures']-=1;self.send({'error':'secret payload'},503);return
                if state['mode']=='auth':self.send({'error':'token secret'},401);return
                rows=[{'index':i,'embedding':[float(i+1),2.]} for i in range(len(request['input']))]
                if state['mode']=='duplicate': rows[-1]['index']=0
                if state['mode']=='dimension':rows[-1]['embedding']=[1.,2.,3.]
                if state['mode']=='nan':rows[-1]['embedding']=[float('nan'),1.]
                if state['mode']=='bool':rows[-1]['embedding']=[True,1.]
                if state['mode']=='count':rows=rows[:-1]
                if self.path=='/api/embed': self.send({'model':request['model'],'embeddings':[r['embedding'] for r in rows]})
                else:self.send({'model':'another' if state['mode']=='model' else request['model'],'data':rows[::-1]})
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)
    def client(self,**kwargs):return OpenAIEmbeddingClient(self.base+'/v1','embed',retries=0,**kwargs)
    def test_openai_orders_indexes_and_bounds_batches(self):
        client=self.client(max_batch_size=2)
        self.assertEqual(client.embed(['a','b','c']),[[1.,2.],[2.,2.],[1.,2.]])
        self.assertEqual([len(r[1]['input']) for r in self.state['posts']],[2,1]);self.assertEqual(client.dimension,2)
    def test_native_ollama_digest_identity_and_cpu_options(self):
        client=OllamaEmbeddingClient(self.base+'/v1','embed',retries=0)
        self.assertEqual(client.embed(['a']),[[1.,2.]])
        self.assertEqual(self.state['posts'][0][0],'/api/embed');self.assertEqual(self.state['posts'][0][1]['options']['num_gpu'],0)
        old=client.identity;self.state['revision']='weights-2'
        with self.assertRaises(LocalModelError):client.embed(['a'])
        self.assertNotEqual(old,OllamaEmbeddingClient(self.base,'embed',retries=0).identity)
    def test_invalid_protocol_vectors_fail_without_retry(self):
        for mode in ['duplicate','dimension','nan','bool','count','model']:
            with self.subTest(mode=mode):
                self.state['mode']=mode;self.state['posts']=[]
                with self.assertRaises(LocalModelError):self.client().embed(['a','b'])
                self.assertEqual(len(self.state['posts']),1)
    def test_service_retry_is_bounded_and_auth_is_redacted(self):
        self.state['failures']=2
        with patch('services.embedding_backend.LocalModelClient._backoff'):
            self.assertEqual(self.client().embed(['a'],retries=2),[[1.,2.]])
        self.assertEqual(len(self.state['posts']),3)
        self.state['mode']='auth'
        with self.assertRaises(LocalModelError) as raised:self.client().embed(['a'],retries=3)
        self.assertEqual(raised.exception.code,'authentication_error');self.assertNotIn('secret',str(raised.exception))
        self.assertEqual(len(self.state['posts']),4)
    def test_local_url_and_redirect_refused(self):
        for url in ['http://example.com/v1','http://user:secret@127.0.0.1/v1','http://127.0.0.1/v1?x=1']:
            with self.assertRaises(LocalModelError):OpenAIEmbeddingClient(url,'embed')
        self.state['mode']='redirect'
        with self.assertRaises(LocalModelError) as raised:self.client().embed(['a'])
        self.assertEqual(raised.exception.code,'redirect_refused');self.assertEqual(self.state['posts'],[])
    def test_alias_revision_and_policy_identity_are_separate(self):
        first=self.client(encoding_policy='large');old=first.identity
        self.assertNotEqual(old,self.client(encoding_policy='mail').identity)
        clone=first.with_options(timeout=1,max_chars=200)
        self.assertEqual(old,clone.identity)
        self.state['revision']='weights-2'
        with self.assertRaises(LocalModelError):first.embed(['a'])
        self.assertNotEqual(old,self.client(encoding_policy='large').identity)
    def test_factory_selection_is_independent_and_has_no_fallback(self):
        config=SimpleNamespace(EMBED_BACKEND='ollama',EMBED_BASE_URL=self.base,EMBED_MODEL='embed',ENABLE_VLLM=False,LP_EMBED_FALLBACK_URL='http://127.0.0.1:9/v1')
        self.assertIsInstance(create_embedding_client(config),OllamaEmbeddingClient)
        config.EMBED_BACKEND='disabled';self.assertIsNone(create_embedding_client(config))
    def test_weight_and_config_content_fingerprint_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'model.safetensors').write_bytes(b'weights-A');(root/'config.json').write_text('{"hidden":2}')
            first=artifact_fingerprint(root);(root/'model.safetensors').write_bytes(b'weights-B')
            second=artifact_fingerprint(root);self.assertNotEqual(first,second)
            (root/'config.json').write_text('{"hidden":3}');self.assertNotEqual(second,artifact_fingerprint(root))


class IntegrityTests(unittest.TestCase):
    def test_ordinary_switch_keeps_cache_and_uses_no_embedding_request(self):
        from services import ordinary_recall
        c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
        c.execute('CREATE TABLE embedding_cache(cache_key TEXT,model TEXT,vector TEXT,dimensions INTEGER)')
        c.execute('INSERT INTO embedding_cache VALUES(?,?,?,?)',('ordinary-file::p::a.txt::old','old-revision','[1,0]',2))
        c.commit()
        client=SimpleNamespace(model='embed',identity='new-revision',embed=lambda values:self.fail('mixed model request'),max_batch_size=2)
        storage=SimpleNamespace(_connect=lambda:c,get_document=lambda *_:{'text':'Original text'},get_embeddings=lambda *_:{})
        try:
            with patch.object(ordinary_recall,'_client',return_value=client):
                self.assertEqual(ordinary_recall.file_vectors(storage,'p',['a.txt']),{})
            self.assertEqual(c.execute('SELECT model,vector FROM embedding_cache').fetchone()[:],('old-revision','[1,0]'))
        finally:c.close()
    def test_mail_switch_reports_rebuild_and_preserves_original_vectors(self):
        from services.mail import semantic
        c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
        c.executescript('CREATE TABLE mail_text_segments(package_id TEXT,segment_id TEXT,text_sha256 TEXT);CREATE TABLE mail_embeddings(package_id TEXT,segment_id TEXT,model TEXT,dim INTEGER,vector BLOB,text_sha256 TEXT);')
        c.execute("INSERT INTO mail_text_segments VALUES('p','s','hash')")
        c.execute("INSERT INTO mail_embeddings VALUES('p','s','old',2,X'0000803F00000000','hash')")
        c.commit()
        repo=SimpleNamespace(get_package=lambda *_:{'index_epoch':1},_connection=lambda:c)
        client=SimpleNamespace(model='embed',identity='new',embed=lambda _:self.fail('mixed model request'))
        try:
            key=semantic.model_key(client)
            status=semantic.index_status(repo,'p','owner',key)
            self.assertEqual(status['status'],'embedding_rebuild_required');self.assertTrue(status['old_index_preserved'])
            with self.assertRaises(LocalModelError):semantic.embed_segments_batch(repo,'p','owner',client=client)
            self.assertEqual(c.execute('SELECT model,dim FROM mail_embeddings').fetchone()[:],('old',2))
        finally:c.close()
    def test_identity_and_dimension_binding_preserves_prior_rows(self):
        c=sqlite3.connect(':memory:')
        try:
            require_index_identity(c,'large','p','model:v1',2)
            with self.assertRaises(LocalModelError):require_index_identity(c,'large','p','model:v2',2)
            with self.assertRaises(LocalModelError):require_index_identity(c,'large','p','model:v1',3)
            self.assertEqual(c.execute('SELECT identity,dimension FROM sjfx_embedding_identity').fetchone(),('model:v1',2))
        finally:c.close()
    def test_large_legacy_namespace_blocks_same_model_new_revision(self):
        from services import large_package_semantic as semantic
        c=sqlite3.connect(':memory:')
        try:
            semantic.init(c)
            c.execute("INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,updated_at) VALUES('p','g','embed',X'0000803F','a',0)")
            with self.assertRaises(LocalModelError):semantic._require_space(c,'p',SimpleNamespace(model='embed',identity='embed:v2'))
            self.assertEqual(c.execute('SELECT model,anchor FROM large_catalog_groups').fetchone(),('embed',b'\x00\x00\x80?'))
        finally:c.close()
    def test_evidence_callable_replacement_never_reuses_other_provider_cache(self):
        from services import evidence
        first=lambda texts:[[1.,0.] for _ in texts]
        second=lambda texts:[[1.,0.] if i==0 else [0.,1.] for i,_ in enumerate(texts)]
        evidence._EMBEDDING_CACHE.clear()
        self.assertEqual(evidence._embedding_scores('query',['text'],first)[0],{0:1.})
        self.assertEqual(evidence._embedding_scores('query',['text'],second)[0],{0:0.})
    def test_validation_rejects_numeric_strings_zero_nonfinite_and_mixed_spaces(self):
        for values in [[['1',2]],[[0,0]],[[float('inf'),1]],[[1,2],[1,2,3]]]:
            with self.assertRaises(LocalModelError):validate_vectors(values)


if __name__=='__main__':unittest.main()
