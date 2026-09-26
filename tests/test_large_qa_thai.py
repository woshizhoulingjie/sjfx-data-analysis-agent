"""Offline multilingual lexical, migration, permission and rollback contracts."""
import errno
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from services import large_qa_thai as thai
from services.conversation import ConversationScope, RetrievalRequest
from services.large_qa_index import LargeQAIndex, tokens
from services.large_qa_search import retrieve
from test_large_qa import FakeStore


class ThaiQATests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base/'sources'
        self.root.mkdir()
        self.index = LargeQAIndex('lp-test', self.base/'indexes', create=True)
        self.rows = []

    def add(self, path, summary, sensitive=0):
        target = self.root/path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(summary, encoding='utf8')
        info = target.stat()
        row = dict(path=path, name=target.name, category='test', group_id='test', topic_name='test',
                   size=info.st_size, modified_at_ns=info.st_mtime_ns, sensitive=sensitive,
                   local_summary=json.dumps(dict(summary=summary), ensure_ascii=False), deep_summary='{}', vector=None)
        self.rows.append(row)
        return row

    def sync(self):
        self.store = FakeStore(self.root, self.rows)
        self.addCleanup(self.store.db.close)
        self.index.sync_rows(self.rows, self.store.package, 'test-model')

    def query(self, text, scope=None):
        with patch('services.large_qa_search.vector_search', side_effect=ConnectionError('offline')):
            return retrieve(self.store, RetrievalRequest('lp-test', text, scope or ConversationScope(), top_k=20, intent='lookup'), base=self.base/'indexes')

    def original(self, text):
        with self.index.connect() as c:
            doc = dict(c.execute('SELECT * FROM documents LIMIT 1').fetchone())
        return self.index.put_original(doc['path'], doc['source_version'], [dict(text=text, location={'page':19})], True)

    def snapshot(self):
        with self.index.connect() as c:
            return {table: [tuple(row) for row in c.execute('SELECT * FROM '+table)]
                    for table in ('meta','documents','chunks','search','search_thai')}

    def test_no_vector_thai_no_spaces_retrieves_late_original_and_page(self):
        self.add('a.pdf', 'บทนำเกี่ยวกับโครงการ'); self.sync()
        self.original('บทนำเกี่ยวกับโครงการ '*600+'เงื่อนไขการชำระเงินต้องได้รับอนุมัติงบประมาณก่อน')
        result = self.query('เงื่อนไขการชำระเงิน')
        self.assertGreater(result['retrieval_stats']['thai_keyword_candidates'], 0)
        self.assertTrue(any('ชำระเงิน' in r['text'] and r['location'].get('page') == 19 for r in result['results']))
        self.assertIn('ชำระเงิน', result['results'][0]['text'])
        self.assertEqual(result['retrieval_stats']['vector_backend'], 'unavailable')

    def test_mixed_languages_numbers_tones_and_negation_are_retained(self):
        text = '预算 Budget ZX-91 งบประมาณไม่อนุมัติ 2026'
        self.add('a.txt', text); self.sync()
        for query in ('预算', 'Budget', 'ZX-91', 'งบประมาณ', 'ไม่อนุมัติ'):
            with self.subTest(query=query):
                self.assertEqual(self.query(query)['results'][0]['text'], 'summary: '+text)
        self.assertIn('th'+'ไม่'.encode().hex(), thai.thai_tokens('ไม่อนุมัติ'))
        self.assertNotEqual(thai.thai_tokens('ปา'), thai.thai_tokens('ป่า'))
        self.assertEqual(tokens(text), ['预算','budget','zx-91','2026'])

    def test_scope_is_applied_before_limit(self):
        for i in range(155): self.add('outside/%d.txt'%i, 'งบประมาณการชำระเงิน')
        self.add('inside/a.txt', 'งบประมาณการชำระเงิน'); self.sync()
        result = self.query('งบประมาณ', ConversationScope('directory','inside'))
        self.assertEqual({r['source_path'] for r in result['results']}, {'inside/a.txt'})

    def test_membership_sensitive_and_physical_changes_still_excluded(self):
        for name in ('deleted.txt','restricted.txt','changed.txt','missing.txt','ok.txt'):
            self.add(name,'งบประมาณการชำระเงิน')
        self.sync()
        self.store.db.execute("DELETE FROM files WHERE path='deleted.txt'")
        self.store.db.execute("UPDATE files SET sensitive=1 WHERE path='restricted.txt'")
        self.store.db.commit()
        (self.root/'changed.txt').write_text('changed',encoding='utf8')
        (self.root/'missing.txt').unlink()
        result=self.query('งบประมาณ')
        self.assertEqual({r['source_path'] for r in result['results']},{'ok.txt'})

    def test_non_thai_queries_never_use_new_tokenizer(self):
        self.add('a.txt','预算付款条件 Budget approval'); self.sync()
        with patch.object(thai,'thai_tokens',side_effect=AssertionError('legacy path changed')):
            for q in ('预算付款','Budget approval'):
                self.assertTrue(self.query(q)['results'])

    def test_incremental_update_delete_has_no_ghost_terms(self):
        row=self.add('a.txt','งบประมาณ'); self.sync()
        row['local_summary']=json.dumps({'summary':'โรงพยาบาล'},ensure_ascii=False)
        self.index.sync_rows(self.rows,self.store.package,'test-model')
        self.assertFalse(self.query('งบประมาณ')['results'])
        self.assertTrue(self.query('โรงพยาบาล')['results'])
        self.index.sync_rows([],self.store.package,'test-model')
        with self.index.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM search_thai').fetchone()[0],0)
            self.assertTrue(thai.ready(c))

    def test_legacy_upgrade_preserves_chunks_documents_and_old_fts(self):
        self.add('a.txt','งบประมาณ Budget 预算'); self.sync()
        self.original('การชำระเงิน Payment 付款')
        before=self.snapshot()
        with self.index.connect(write=True) as c:
            c.execute('DROP TABLE search_thai')
            c.execute('DELETE FROM meta WHERE key=?',(thai.META_KEY,))
        self.assertTrue(any('泰文关键词索引' in w for w in self.query('งบประมาณ')['warnings']))
        LargeQAIndex('lp-test',self.base/'indexes',create=True)
        after=self.snapshot()
        for table in ('documents','chunks','search','search_thai'):
            self.assertEqual(before[table],after[table])
        self.assertTrue(self.query('งบประมาณ')['results'])

    def test_old_writer_generation_invalidates_sidecar_then_rebuilds(self):
        self.add('a.txt','งบประมาณ'); self.sync()
        with self.index.connect(write=True) as c:
            c.execute("UPDATE chunks SET text='โรงพยาบาล'")
            self.index.set_meta(c,'generation',self.index.meta(c)['generation']+1)
        result=self.query('งบประมาณ')
        self.assertEqual(result['retrieval_stats']['thai_keyword_candidates'],0)
        self.assertTrue(any('需要更新' in w for w in result['warnings']))
        LargeQAIndex('lp-test',self.base/'indexes',create=True)
        self.assertFalse(self.query('งบประมาณ')['results'])
        self.assertTrue(self.query('โรงพยาบาล')['results'])

    def test_noop_never_retokenizes_current_index(self):
        self.add('a.txt','งบประมาณ'); self.sync()
        with patch.object(thai,'add_chunk',side_effect=AssertionError('rebuild')):
            LargeQAIndex('lp-test',self.base/'indexes',create=True)
            self.index.sync_rows(self.rows,self.store.package,'test-model')
        self.assertTrue(self.query('งบประมาณ')['results'])

    def test_migration_failure_is_atomic_and_recoverable(self):
        self.add('a.txt','งบประมาณ'); self.sync()
        with self.index.connect(write=True) as c:
            self.index.set_meta(c,'generation',self.index.meta(c)['generation']+1)
        before=self.snapshot()
        with patch.object(thai,'add_chunk',side_effect=thai.ThaiIndexError('injected')):
            with self.assertRaises(thai.ThaiIndexError):
                LargeQAIndex('lp-test',self.base/'indexes',create=True)
        self.assertEqual(before,self.snapshot())
        LargeQAIndex('lp-test',self.base/'indexes',create=True)
        self.assertTrue(self.query('งบประมาณ')['results'])

    def test_write_failure_rolls_back_all_old_evidence_and_versions(self):
        self.add('a.txt','งบประมาณ'); self.sync(); self.original('ข้อมูลเดิม Old original')
        before=self.snapshot()
        for error in (thai.ThaiIndexError('missing'), OSError(errno.ENOSPC,'full'), MemoryError('exhausted')):
            with self.subTest(error=type(error).__name__), patch.object(thai,'add_chunk',side_effect=error):
                with self.assertRaises(type(error)):
                    self.original('ข้อมูลใหม่ New original')
            self.assertEqual(before,self.snapshot())

    def test_migration_requires_transaction(self):
        with sqlite3.connect(':memory:') as c:
            with self.assertRaises(thai.ThaiIndexError): thai.ensure(c)

    def test_missing_tokenizer_is_visible_and_does_not_break_legacy_keywords(self):
        self.add('a.txt','งบประมาณ Budget'); self.sync()
        with patch.object(thai,'_provider',side_effect=thai.ThaiIndexError('missing')):
            result=self.query('งบประมาณ Budget')
        self.assertTrue(result['results'])
        self.assertTrue(any('泰文关键词检索暂不可用' in w for w in result['warnings']))

    def test_dependency_version_and_dictionary_are_checked(self):
        thai._provider.cache_clear()
        try:
            with patch.object(thai.metadata,'version',return_value='0.0.0'):
                with self.assertRaises(thai.ThaiIndexError): thai.thai_tokens('งบประมาณ')
            with patch.object(thai,'DICTIONARY_SHA256','invalid'):
                with self.assertRaises(thai.ThaiIndexError): thai.thai_tokens('งบประมาณ')
        finally:
            thai._provider.cache_clear()
        self.assertTrue(thai.thai_tokens('งบประมาณ'))

    def test_long_text_keeps_last_thai_terms(self):
        words=thai.thai_tokens('ประเทศไทย'*3000+'งบประมาณ')
        self.assertTrue(words)
        self.assertIn('th'+'งบประมาณ'.encode().hex(),words[-5:])

    def test_surface_phrase_recovers_context_dependent_word_boundary(self):
        self.add('a.txt','งบประมาณการชำระเงิน'); self.sync()
        self.assertTrue(self.query('งบประมาณ')['results'])
        self.assertTrue(self.query('ชำระเงิน')['results'])

    def test_surface_phrase_does_not_drop_tone_or_cross_spaces(self):
        self.add('a.txt','ป่า งบ ประมาณ'); self.sync()
        self.assertFalse(self.query('ปา')['results'])
        self.assertFalse(self.query('งบประมาณ')['results'])

    def test_bounded_migration_uses_keyset_batches(self):
        with self.index.connect(write=True) as c:
            for i in range(300):
                c.execute("INSERT INTO chunks(path,ordinal,kind,text,location,version) VALUES(?,0,'summary',?,'{}','1')",(str(i),'งบประมาณ'))
            self.index.set_meta(c,'generation',999)
        sizes=[]
        class Cursor:
            def __init__(self,inner):self.inner=inner
            def fetchall(self):
                rows=self.inner.fetchall();sizes.append(len(rows));return rows
        class Connection:
            def __init__(self,inner):self.inner=inner
            def __getattr__(self,name):return getattr(self.inner,name)
            def execute(self,sql,*args):
                cur=self.inner.execute(sql,*args)
                if sql.startswith('SELECT id,path,text FROM chunks'):
                    assert 'id>?' in sql and 'LIMIT 128' in sql
                    return Cursor(cur)
                return cur
        with self.index.connect(write=True) as c:
            c.execute('BEGIN IMMEDIATE');thai.ensure(Connection(c))
        self.assertEqual(sizes,[128,128,44,0])
        with self.index.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM search_thai').fetchone()[0],300)

    def test_single_scan_and_fallback_equal_exhaustive_authorized_top150(self):
        for i in range(700): self.add('目录/%04d.txt'%i,'งบประมาณ '+('โรงพยาบาล '*(i%3)))
        self.sync()
        captured=[]
        class Connection:
            def __init__(self,inner,fallback=False):self.inner,self.fallback=inner,fallback
            def execute(self,sql,args):
                if 'json_each' in sql:
                    captured.append((sql,args))
                    if self.fallback:raise sqlite3.OperationalError('no such table: json_each')
                return self.inner.execute(sql,args)
        with self.index.connect() as c:
            all_paths={r[0] for r in c.execute('SELECT path FROM documents')}
            allowed={p for p in all_paths if int(Path(p).stem)%7!=0}
            terms=thai.thai_tokens('งบประมาณ')
            fast=thai.candidates(Connection(c),terms,allowed)
            self.assertEqual(len(captured),1)
            expression=captured[0][1][0]
            reference=[(r['id'],r['rank']) for r in c.execute('''
                SELECT c.id,c.path,bm25(search_thai,2.0,1.0,0.35) rank
                FROM search_thai JOIN chunks c ON c.id=search_thai.rowid
                WHERE search_thai MATCH ? ORDER BY rank,c.id''',(expression,)) if r['path'] in allowed][:150]
            self.assertEqual(fast,reference)
            self.assertEqual(thai.candidates(Connection(c,True),terms,allowed),reference)
            self.assertEqual(c.execute('PRAGMA query_only').fetchone()[0],1)

    def test_fast_query_does_not_hide_database_failures_as_fallback(self):
        class Broken:
            def execute(self,*args):raise sqlite3.OperationalError('database is locked')
        with self.assertRaisesRegex(sqlite3.OperationalError,'locked'):
            thai.candidates(Broken(),thai.thai_tokens('งบประมาณ'),{'a.txt'})
