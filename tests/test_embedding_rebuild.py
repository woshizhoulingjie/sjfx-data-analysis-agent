"""Candidate failure, publication races and vector-only restore regressions."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.embedding_rebuild import rebuild_embeddings,publish_rebuild
from services.model_contract import LocalModelError


class Client:
    model='embed'; identity='embed:revision-2'; max_batch_size=2; dimension=2
    def embed(self,texts): return [[2.,1.] for _ in texts]


class RebuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Path(self.tmp.name)/'index.db'
        with closing(sqlite3.connect(self.db)) as c:
            c.executescript('''CREATE TABLE mail_packages(package_id TEXT PRIMARY KEY,index_epoch INTEGER,source_revision TEXT);
                CREATE TABLE mail_text_segments(segment_id TEXT,package_id TEXT,text TEXT,text_sha256 TEXT);
                CREATE TABLE mail_embeddings(package_id TEXT,segment_id TEXT,model TEXT,dim INTEGER,vector BLOB,text_sha256 TEXT,index_epoch INTEGER,created_at TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(package_id,segment_id,model));
                CREATE TABLE mail_embedding_buckets(package_id TEXT,model TEXT,band INTEGER,bucket INTEGER,segment_id TEXT,PRIMARY KEY(package_id,model,band,bucket,segment_id));
                INSERT INTO mail_packages VALUES('p',1,'original');
                INSERT INTO mail_text_segments VALUES('s','p','Original evidence','hash');
                INSERT INTO mail_embeddings(package_id,segment_id,model,dim,vector,text_sha256,index_epoch) VALUES('p','s','embed:revision-1',2,X'0000803F00000000','hash',1);''')
            c.commit()
    def tearDown(self):self.tmp.cleanup()
    def rows(self):
        with closing(sqlite3.connect(self.db)) as c:return c.execute('SELECT model,vector FROM mail_embeddings').fetchall()
    def build(self,**kwargs):return rebuild_embeddings(self.db,'mail','p',client=Client(),**kwargs)
    def test_candidate_then_atomic_publish_and_vector_only_restore(self):
        before=self.rows();result=self.build();self.assertEqual(result['status'],'candidate_ready');self.assertEqual(self.rows(),before)
        result=publish_rebuild(result['manifest']);self.assertEqual(result['status'],'published');self.assertNotEqual(self.rows(),before)
        result=publish_rebuild(result['manifest'],restore=True);self.assertEqual(result['status'],'restored');self.assertEqual(self.rows(),before)
        with closing(sqlite3.connect(self.db)) as c:self.assertEqual(c.execute('SELECT text FROM mail_text_segments').fetchone()[0],'Original evidence')
        self.assertTrue(Path(result['backup']).is_file());self.assertTrue(Path(result['candidate']).is_file())
    def test_failed_model_response_preserves_active_vectors_and_sources(self):
        class Invalid(Client):
            def embed(self,texts):return [[float('nan'),1.] for _ in texts]
        before=self.rows()
        with self.assertRaises(LocalModelError):rebuild_embeddings(self.db,'mail','p',client=Invalid(),publish=True)
        self.assertEqual(self.rows(),before)
        manifests=list((self.db.parent/'embedding_rebuilds').glob('*.json'))
        self.assertEqual(json.loads(manifests[0].read_text(encoding='utf8'))['status'],'failed')
    def test_source_changes_reject_publish_and_preserve_new_source(self):
        result=self.build();before=self.rows()
        with closing(sqlite3.connect(self.db)) as c:c.execute("UPDATE mail_text_segments SET text='Changed evidence'");c.commit()
        with self.assertRaises(LocalModelError):publish_rebuild(result['manifest'])
        self.assertEqual(self.rows(),before)
        with closing(sqlite3.connect(self.db)) as c:self.assertEqual(c.execute('SELECT text FROM mail_text_segments').fetchone()[0],'Changed evidence')
    def test_index_changes_reject_publish_and_candidate_tampering_is_rejected(self):
        result=self.build()
        with closing(sqlite3.connect(self.db)) as c:c.execute("UPDATE mail_embeddings SET model='newer'");c.commit()
        with self.assertRaises(LocalModelError):publish_rebuild(result['manifest'])
        self.assertEqual(self.rows()[0][0],'newer')
        result=self.build()
        with closing(sqlite3.connect(result['candidate'])) as c:c.execute("UPDATE mail_embeddings SET dim=9");c.commit()
        with self.assertRaises(LocalModelError):publish_rebuild(result['manifest'])
    def test_restore_refuses_to_overwrite_sources_changed_since_rebuild(self):
        result=self.build(publish=True)
        with closing(sqlite3.connect(self.db)) as c:c.execute("UPDATE mail_text_segments SET text='new source'");c.commit()
        before=self.rows()
        with self.assertRaises(LocalModelError):publish_rebuild(result['manifest'],restore=True)
        self.assertEqual(self.rows(),before)
    def test_ordinary_uses_existing_cache_and_restore_keeps_parser_documents(self):
        with closing(sqlite3.connect(self.db)) as c:
            c.executescript('''CREATE TABLE unified_documents(scan_id TEXT,node_path TEXT,payload TEXT);
                CREATE TABLE embedding_cache(cache_key TEXT,model TEXT,vector TEXT,dimensions INTEGER,updated_at TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(cache_key,model));''')
            c.execute('INSERT INTO unified_documents VALUES(?,?,?)',('scan','a.txt',json.dumps({'text':'Parser evidence'})))
            c.execute('INSERT INTO embedding_cache VALUES(?,?,?,?,CURRENT_TIMESTAMP)',('ordinary-file::scan::a.txt::old','embed:revision-1','[1,0]',2));c.commit()
        result=rebuild_embeddings(self.db,'ordinary','scan',client=Client(),publish=True)
        with closing(sqlite3.connect(self.db)) as c:self.assertEqual(c.execute('SELECT DISTINCT model FROM embedding_cache').fetchall(),[('embed:revision-2',)])
        publish_rebuild(result['manifest'],restore=True)
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT DISTINCT model FROM embedding_cache').fetchall(),[('embed:revision-1',)])
            self.assertEqual(json.loads(c.execute('SELECT payload FROM unified_documents').fetchone()[0])['text'],'Parser evidence')
    def test_committed_publication_recovers_if_manifest_update_fails(self):
        result=self.build();before=self.rows()
        with patch.object(Path,'write_text',side_effect=OSError('disk failure')):
            published=publish_rebuild(result['manifest'])
        self.assertEqual(published['status'],'published');self.assertIn('manifest_warning',published)
        restored=publish_rebuild(result['manifest'],restore=True)
        self.assertEqual(restored['status'],'restored');self.assertEqual(self.rows(),before)
    def test_large_centroids_and_dimensions_publish_and_restore_together(self):
        from services.large_package_isolated import LargePackageStore
        from config import Config
        large_db=Path(self.tmp.name)/'large.db'
        with patch.object(Config,'SCAN_ALLOWED_ROOTS',()):
            store=LargePackageStore(large_db);package=store.create(Path(self.tmp.name))['id']
        store.upsert_file(package,{'path':'a.txt','name':'a.txt'})
        store.update_file(package,'a.txt',quick_status='completed',local_summary={'sample':'Remote attestation verifies devices and claims. Remote attestation uses original trusted evidence.','portrait':{'schema':'large-content-portrait/1','fields':[],'headings':[]}})
        with closing(store._connect()) as c:
            c.execute("INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,updated_at) VALUES(?,?,?,?,?,0)",(package,'g','embed:revision-1',b'\0\0\x80?\0\0\0\0','a.txt'))
            c.execute('INSERT INTO large_catalog_members VALUES(?,?,?,?,?)',(package,'a.txt','g','old',b'\0\0\x80?\0\0\0\0'))
            original=c.execute('SELECT local_summary FROM files WHERE package_id=?',(package,)).fetchone()[0]
        rebuilt=rebuild_embeddings(large_db,'large',package,client=Client(),publish=True)
        self.assertEqual(rebuilt['count'],1)
        with closing(store._connect()) as c:
            self.assertEqual(c.execute('SELECT model,member_count FROM large_catalog_groups').fetchone()[:],('embed:revision-2',1))
            self.assertEqual(c.execute('SELECT local_summary FROM files WHERE package_id=?',(package,)).fetchone()[0],original)
        publish_rebuild(rebuilt['manifest'],restore=True)
        with closing(store._connect()) as c:self.assertEqual(c.execute('SELECT model,member_count FROM large_catalog_groups').fetchone()[:],('embed:revision-1',1))


if __name__=='__main__':unittest.main()
