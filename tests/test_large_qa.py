"""Large-package retrieval and publication contracts; no external model needed."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from services.conversation import ConversationScope, RetrievalRequest, IntentRouter, _citation
from services.analysis_planner import AnalysisPlanner
from services import answer_quality as aq
from services.conversation import ContextWindowPolicy, _compact_citation_numbers
from services.claim_verifier import _readable_guard_blocks, _attribute_claim, _concepts
from services.large_qa_index import LargeQAIndex, loads, split_text, tokens, plain_text
from services.large_qa_search import retrieve, vector_search, select_material, original_document
from services.large_qa_topics import refresh_maps, refine_one
from services.large_qa_synthesis import prepare_global
from services.analysis_tools import merge_retrieval_results


class FakeStore:
    def __init__(self, root, rows):
        self.package={'id':'lp-test','root_path':str(root),'owner_id':'tester'}
        self.db=sqlite3.connect(':memory:')
        self.db.row_factory=sqlite3.Row
        self.db.execute('CREATE TABLE files(package_id TEXT,path TEXT,sensitive INTEGER)')
        self.db.executemany('INSERT INTO files VALUES(?,?,?)',[('lp-test',r['path'],r.get('sensitive',0)) for r in rows])
        self.db.commit()

    def _connect(self):
        return self.db

    def get(self, package_id):
        return self.package if package_id=='lp-test' else None


class LargeQATests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.base=Path(self.tmp.name)
        self.root=self.base/'sources';self.root.mkdir()
        self.index=LargeQAIndex('lp-test',self.base/'indexes',create=True)
        self.rows=[]

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, path, summary, topic='energy', body=None, sensitive=0):
        target=self.root/path;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(body or summary,encoding='utf-8')
        st=target.stat()
        row={'path':path,'name':target.name,'category':topic,'group_id':topic,'topic_name':topic,
             'size':st.st_size,'modified_at_ns':st.st_mtime_ns,'sensitive':sensitive,
             'local_summary':json.dumps({'summary':summary},ensure_ascii=False),'deep_summary':'{}',
             'vector':np.array([1.,0.,0.],dtype=np.float32).tobytes()}
        self.rows.append(row)
        return row

    def sync(self):
        self.store=FakeStore(self.root,self.rows)
        self.index.sync_rows(self.rows,self.store.package,'test-model')

    def query(self,text,scope=None,intent='lookup'):
        return retrieve(self.store,RetrievalRequest('lp-test',text,scope or ConversationScope(),top_k=40,intent=intent),
                        base=self.base/'indexes',query_vector=np.array([1.,0.,0.]))

    def original(self,path,text,complete=True):
        with self.index.connect() as c:
            doc=dict(c.execute('SELECT * FROM documents WHERE path=?',(path,)).fetchone())
        self.assertTrue(self.index.put_original(path,doc['source_version'],[{'text':text,'location':{'page':9}}],complete))

    def test_chinese_bigrams_and_late_paragraph(self):
        self.add('a.pdf','报告的概览没有写到付款条件。')
        self.sync()
        body='材料性能介绍。'*700+'钇铝石榴石最终付款比例为百分之九十，验收后才可支付。'
        self.original('a.pdf',body)
        result=self.query('钇铝石榴石最终付款条件')
        self.assertTrue(any('百分之九十' in r['text'] and r['location'].get('page')==9 for r in result['results']))
        self.assertIn('石榴',tokens('钇铝石榴石'))
        citation=_citation(next(r for r in result['results'] if '百分之九十' in r['text']),1)
        self.assertEqual(citation['evidence_level'],'passage')
        self.assertEqual(citation['location']['page'],9)

    def test_scope_applies_before_ranking(self):
        for i in range(120):self.add('outside/%03d.txt'%i,'钇铝石榴石强度与付款条件。')
        self.add('inside/target.txt','钇铝石榴石最终付款必须验收。')
        self.sync()
        result=self.query('钇铝石榴石付款条件',ConversationScope('directory','inside'))
        self.assertEqual({r['source_path'] for r in result['results']},{'inside/target.txt'})
        self.assertEqual(result['coverage']['scope_files'],1)

    def test_deleted_and_restricted_sources_are_excluded_immediately(self):
        self.add('a.txt','重点研究碳矿化储存以及相关工程限制。')
        self.add('b.txt','重点研究碳矿化储存以及相关工程限制。')
        self.sync()
        self.store.db.execute("DELETE FROM files WHERE path='a.txt'")
        self.store.db.execute("UPDATE files SET sensitive=1 WHERE path='b.txt'")
        self.store.db.commit()
        self.assertFalse(self.query('碳矿化储存')['results'])

    def test_changed_physical_file_is_not_quoted(self):
        self.add('a.txt','碳矿化研究有充分来源。');self.sync()
        (self.root/'a.txt').write_text('changed, superseded',encoding='utf-8')
        self.assertFalse(self.query('碳矿化')['results'])

    def test_incremental_fts_removal_and_name_change(self):
        row=self.add('a.txt','过时概念乙烯催化。');self.sync();self.original('a.txt','原文中的铜催化剂支持回查。')
        row['name']='display-name-change.txt'
        row['local_summary']=json.dumps({'summary':'更新概念碳矿化封存。'},ensure_ascii=False)
        self.index.sync_rows(self.rows,self.store.package,'test-model')
        self.original('a.txt','替换原文后，镍催化剂被采用。')
        with self.index.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM search WHERE search MATCH '铜催'").fetchone()[0],0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM search WHERE search MATCH '乙烯'").fetchone()[0],0)
            self.assertGreater(c.execute("SELECT COUNT(*) FROM search WHERE search MATCH '镍催'").fetchone()[0],0)

    def test_original_publication_rejects_obsolete_version(self):
        self.add('a.txt','正文原始版本。');self.sync()
        doc=self.index.next_parse()
        self.rows[0]['modified_at_ns']+=1
        self.index.sync_rows(self.rows,self.store.package,'test-model')
        self.assertFalse(self.index.put_original('a.txt',doc['source_version'],[{'text':'旧内容'}],True))

    def test_summary_update_preserves_originals(self):
        self.add('a.txt','最初的材料研究摘要。');self.sync();self.original('a.txt','完整原文可回查。')
        self.rows[0]['local_summary']=json.dumps({'summary':'改进摘要覆盖更多细节。'},ensure_ascii=False)
        self.index.sync_rows(self.rows,self.store.package,'test-model')
        with self.index.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM chunks WHERE kind='original'").fetchone()[0],1)
            self.assertEqual(c.execute('SELECT parse_complete FROM documents').fetchone()[0],1)

    def test_no_change_does_not_republish(self):
        self.add('a.txt','完整材料摘要足以形成一个基础主题。');self.sync()
        with self.index.connect() as c:before=self.index.meta(c)
        self.assertEqual(self.index.sync_rows(self.rows,self.store.package,'test-model'),0)
        with self.index.connect() as c:self.assertEqual(before['generation'],self.index.meta(c)['generation'])

    def test_all_summaries_enter_hierarchy_with_honest_provenance(self):
        for i in range(95):self.add('%03d.txt'%i,'材料编号{}讨论储能系统的老化特征，但是存在个体差异与使用限制。'.format(i))
        self.sync();refresh_maps(self.index)
        with self.index.connect() as c:
            topics=self.index.topic_material(c)
            self.assertEqual(len(topics[0]['covered_paths']),95)
            self.assertGreater(topics[0]['reports'][0]['level'],0)
            for report in topics[0]['reports']:
                for p in loads(report['text'])['points']:
                    self.assertTrue(set(p['sources']).issubset({r['path'] for r in self.rows}))
            scoped=self.index.topic_material(c,{'000.txt'})
            self.assertFalse(scoped[0]['reports'])
        result=self.query('概括整个资料包主要内容',intent='summary')
        self.assertEqual(result['coverage']['summary_backed_files'],95)
        self.assertEqual(result['coverage']['original_indexed_files'],0)
        self.assertIsNone(result['coverage']['query_coverage'])
        self.assertFalse(result['coverage']['package_reading_complete'])

    def test_filetype_and_unresolved_entity_scopes(self):
        self.add('a.txt','地质封存方法研究说明。');self.add('b.pdf','地质封存方法研究说明。');self.sync()
        result=self.query('地质封存',ConversationScope('file_type','pdf'))
        self.assertEqual({r['source_path'] for r in result['results']},{'b.pdf'})
        self.assertFalse(self.query('地质封存',ConversationScope('entity','甲公司'))['results'])

    def test_explicit_scope_does_not_drop_files_after_five_hundred(self):
        scope=ConversationScope('files',source_paths=tuple('%05d.txt'%i for i in range(1001)))
        self.assertEqual(len(scope.source_paths),1001)
        self.assertTrue(scope.contains_source('01000.txt'))

    def test_vector_tail_after_twenty_thousand(self):
        vector=np.array([1.,0.,0.],dtype=np.float32).tobytes()
        with self.index.connect(write=True) as c:
            c.executemany('INSERT INTO documents(path,vector,sensitive) VALUES(?,?,0)',[('%05d.txt'%i,vector) for i in range(25005)])
        with self.index.connect() as c:
            hits,_=vector_search(c,self.index,'target',{'25004.txt'},{'embedding_model':'test','summary_generation':91},[1,0,0])
        self.assertEqual(hits[0][0],'25004.txt')

    def test_merge_keeps_coverage_and_unknown_recall(self):
        self.add('a.txt','材料的主要内容是光伏老化。');self.sync()
        result=merge_retrieval_results([self.query('光伏老化')])
        self.assertEqual(result['retrieval_mode'],'large_qa')
        self.assertIsNone(result['coverage']['query_coverage'])
        self.assertEqual(result['coverage']['original_indexed_files'],0)

    def test_mmr_preserves_minority_topic(self):
        rows=[{'text':'光伏材料效率和户外老化。'+str(i),'topic':'pv','kind':'summary','fusion':.05} for i in range(30)]
        rows.append({'text':'但是电池热失控仍有使用限制。','topic':'battery','kind':'summary','fusion':.01})
        selected=select_material(rows,'概括资料',5,True)
        self.assertIn('battery',{r['topic'] for r in selected})

    def test_pending_index_falls_back(self):
        self.store=FakeStore(self.root,[])
        self.assertIsNone(self.query('尚未完成索引'))

    def test_mapping_failure_is_explicit_and_keeps_evidence(self):
        self.add('a.txt','光伏组件研究强调温度变化与老化的关系。');self.add('b.txt','光伏发电使用条件与户外老化研究。');self.sync();refresh_maps(self.index)
        retrieval=self.query('概括资料主题',intent='summary')
        with patch('services.conversation._model_text',side_effect=TimeoutError('test')):
            result=prepare_global(retrieval,'概括资料主题',object(),self.base/'indexes')
        self.assertFalse(result['package_context']['large_qa']['map_complete'])
        self.assertTrue(result['results'])
        self.assertTrue(any('未完成模型归并' in w for w in result['warnings']))

    def test_original_progress_does_not_invalidate_topic_snapshot(self):
        self.add('a.txt','光伏组件研究强调温度变化与老化的关系。');self.add('b.txt','光伏发电使用条件与户外老化研究。');self.sync();refresh_maps(self.index)
        retrieval=self.query('概括资料主题',intent='summary')
        self.original('a.txt','光伏组件研究强调温度变化与老化的关系。')
        with patch('services.conversation._model_text',return_value=json.dumps({'points':[{'text':'光伏组件研究强调温度变化与老化的关系。','sources':['S1']}]})):
            result=prepare_global(retrieval,'概括资料主题',object(),self.base/'indexes')
        self.assertTrue(result['package_context']['large_qa']['map_complete'])
        self.assertTrue(any(r['evidence_level']=='passage' for r in result['results']))

    def test_natural_chinese_explanation_is_not_translation(self):
        question='请整体介绍这些资料在研究什么，按主要方向说明代表性发现。请用自然连贯的中文讲清楚。'
        self.assertEqual(IntentRouter().route(question).name,'summary')
        self.assertIn('summary',AnalysisPlanner().plan(question,{'kind':'package'})['modes'])
        self.assertEqual(IntentRouter().route('只看原文').name,'translation')
        self.assertEqual(IntentRouter().route('翻译成中文').name,'translation')
        self.assertNotEqual(IntentRouter().route('请根据原文解释原因').name,'translation')

    def test_do_not_list_files_keeps_document_question_mode(self):
        question='请详细概括这份资料。请娓娓道来，不要只列出文件目录。'
        self.assertEqual(IntentRouter().route(question).name,'summary')
        self.assertNotIn('file_search',AnalysisPlanner().plan(question,{'kind':'package'})['modes'])

    def test_exact_windows_cover_tail_without_gaps(self):
        text='开头。'+'a'*7777+'尾部特别重要。'
        chunks=list(split_text(text,1600,180))
        self.assertEqual(chunks[-1][2],len(text))
        self.assertTrue(all(text[start:end]==body for body,start,end in chunks))
        self.assertTrue(all(chunks[i+1][1]<=chunks[i][2] for i in range(len(chunks)-1)))

    def test_markup_is_not_sent_as_document_prose(self):
        body='<!DOCTYPE html><html><head><script>bad()</script></head><body><h1>材料性能</h1><p>强度 &gt; 20，条件见表。</p></body></html>'
        result=plain_text(body)
        self.assertIn('强度 > 20',result)
        self.assertNotIn('<html',result)
        self.assertNotIn('bad()',result)

    def test_reader_returns_original_without_window_overlap(self):
        self.add('a.txt','这是摘要，不应替代原文。');self.sync()
        text='这是正文。'+'中间段落有细节。'*900+'最后一段是验收条件。'
        self.original('a.txt',text)
        document=original_document('lp-test',self.root,'a.txt',True,self.base/'indexes')
        self.assertEqual(document['text_content'],text)
        self.assertTrue(document['coverage']['parse_complete'])
        self.assertFalse(document['coverage']['semantic_complete'])

    def test_unchanged_topic_map_is_reused_during_answer_repair(self):
        self.add('a.txt','光伏组件研究强调温度变化与老化的关系。');self.add('b.txt','光伏发电使用条件与户外老化研究。');self.sync();refresh_maps(self.index)
        retrieval=self.query('概括资料主题',intent='summary')
        response=json.dumps({'points':[{'text':'光伏组件研究强调温度变化与老化的关系。','sources':['S1']}]})
        with patch('services.conversation._model_text',return_value=response) as model_call:
            first=prepare_global(retrieval,'概括资料主题',object(),self.base/'indexes')
            second=prepare_global(retrieval,'概括资料主题',object(),self.base/'indexes')
        self.assertEqual(model_call.call_count,1)
        self.assertFalse(first['package_context']['large_qa']['map_cache_hit'])
        self.assertTrue(second['package_context']['large_qa']['map_cache_hit'])

    def test_final_prompt_selection_preserves_minor_topic(self):
        entries=[]
        for i in range(8):
            entries.append({'source_path':'main/%s.txt'%i,'evidence_id':str(i),'citation_index':i+1,'citation_label':'[%s]'%(i+1),
                            'original_text':'材料研究说明光伏组件老化的材料研究限制。'*8,'source_kind':'summary',
                            'large_global':True,'topic_id':'main','label':'file_summary'})
        entries.append({'source_path':'minor/a.txt','evidence_id':'minor','citation_index':9,'citation_label':'[9]',
                        'original_text':'地理资料记载了气候变化以及当地的降水特征。'*8,'source_kind':'summary',
                        'large_global':True,'topic_id':'minor','label':'file_summary'})
        selected=aq.select_evidence(entries,ContextWindowPolicy(max_prompt_evidence=3),'材料研究限制')
        self.assertIn('minor',{r['topic_id'] for r in selected})

    def test_guard_keeps_list_numbering_contiguous(self):
        claims=[{'raw_text':'1. 不被支持。','status':'unsupported','paragraph_index':0},
                {'raw_text':'2. 有依据的内容 [1]。','status':'supported','paragraph_index':1},
                {'raw_text':'3. 另一个发现 [2]。','status':'supported','paragraph_index':2}]
        blocks,_,_=_readable_guard_blocks(claims)
        self.assertTrue(blocks[0].startswith('1. '))
        self.assertTrue(blocks[1].startswith('2. '))

    def test_chinese_topic_map_can_organize_english_evidence(self):
        self.add('a.txt','Photovoltaic modules degrade after exposure to outdoor temperature changes.')
        self.add('b.txt','Temperature cycling is one of the conditions examined in solar energy research.')
        self.sync();refresh_maps(self.index)
        retrieval=self.query('概括资料主题',intent='summary')
        with patch('services.conversation._model_text',return_value=json.dumps({'points':[{'text':'光伏组件在户外环境中的老化。','sources':['S1']}]})):
            result=prepare_global(retrieval,'概括资料主题',object(),self.base/'indexes')
        self.assertTrue(result['package_context']['large_qa']['map_complete'])

    def test_guard_removes_dangling_introduction_after_removed_details(self):
        claims=[{'raw_text':'**具体案例**','status':'section_label','paragraph_index':0},
                {'raw_text':'资料详细列举了以下几个案例： [1]','status':'supported','paragraph_index':1},
                {'raw_text':'没有依据的数字。','status':'unsupported','paragraph_index':2}]
        blocks,_,_=_readable_guard_blocks(claims)
        self.assertEqual(blocks,[])

    def test_background_refinement_yields_without_burning_retry(self):
        self.add('a.txt','光伏组件的户外老化需要结合温度变化研究。');self.sync();refresh_maps(self.index)
        self.assertFalse(refine_one(self.index,object(),cancel_check=lambda:True))
        with self.index.connect() as c:
            self.assertEqual(c.execute('SELECT SUM(attempts) FROM topic_nodes').fetchone()[0],0)

    def test_model_and_reader_share_compact_citation_numbers(self):
        citations=[{'citation_index':i,'citation_label':'[%s]'%i,'evidence_id':'E%s'%i} for i in range(1,8)]
        displayed,selected=_compact_citation_numbers(citations,[citations[6],citations[2]])
        self.assertEqual([x['citation_index'] for x in selected],[1,2])
        self.assertEqual([x['evidence_id'] for x in displayed[:2]],['E7','E3'])
        self.assertEqual(displayed[:2],selected)
        self.assertEqual(citations[6]['citation_label'],'[7]')

    def test_auto_attribution_removes_only_redundant_sources(self):
        text='光伏组件的户外老化需要结合温度变化研究。'
        citations=[{'citation_index':i,'source_path':'%s.txt'%i,'original_text':text,'source_kind':'summary'} for i in range(1,7)]
        result=_attribute_claim(text,_concepts(text),citations,ConversationScope())
        self.assertEqual(result['indexes'],[1,2])
        self.assertEqual(result['coverage'],1.0)

    def test_auto_attribution_keeps_unique_numeric_evidence(self):
        text='光伏组件温度变化研究持续100天。'
        citations=[{'citation_index':i,'source_path':'%s.txt'%i,'original_text':'光伏组件温度变化研究持续。','source_kind':'summary'} for i in (1,2)]
        citations.append({'citation_index':3,'source_path':'3.txt','original_text':'组件温度研究100天。','source_kind':'summary'})
        result=_attribute_claim(text,_concepts(text),citations,ConversationScope())
        self.assertIn(3,result['indexes'])
        self.assertEqual(len(result['indexes']),2)


if __name__=='__main__':
    unittest.main()
