"""Whole-source batch coverage and visible semantic output reservation."""
import json
import time
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from services.mail.research.runtime import MailResearchRuntime
from services.mail.research.evidence_packing import model_source,pack_evidence_batches
from services.mail.research.claim_verifier import judge_output_tokens
from services.mail.research.token_budget import init_usage,estimate_tokens


def source(key,text):
    return {'evidence_id':key,'message_key':'m'+key,'mime_path':'1','source_kind':'body','quote_text':text,
        'char_start':0,'char_end':len(text),'sent_at_utc':'2026-09-'+('15' if key=='early' else '22'),
        'attribution':'authored','source_revision':'r','participants':[{'role':'from','normalized_address':'alice@test'}],
        'body_fragments':[{'fragment_kind':'authored','char_start':0,'char_end':len(text)}]}


class ResearchBatchCapacityTests(TestCase):
    def test_complete_source_partition_preserves_all_ids_versions_and_metadata(self):
        sources=[source('early','价格须经内部审批后生效。'),source('late','财务已批准价格，合同生效。')]
        sources[0]['participants'][0].update(position=99,raw_address_sample='long redundant field')
        projected=[model_source(r) for r in sources]
        self.assertNotIn('position',projected[0]['participants'][0])
        self.assertEqual(projected[0]['source_revision'],'r')
        batches,manifest=pack_evidence_batches(projected,max(estimate_tokens(r) for r in projected)+10)
        self.assertEqual(len(batches),2)
        self.assertEqual({r['evidence_id'] for b in batches for r in b},{'early','late'})
        self.assertEqual([r['quote_text'] for b in batches for r in b],[r['quote_text'] for r in projected])
        self.assertTrue(manifest['synthesis_complete'])
        _,missing=pack_evidence_batches(projected,1)
        self.assertFalse(missing['synthesis_complete']);self.assertEqual(len(missing['omitted']),2)

    def run_batches(self,fail_late=False,withdrawal=False):
        sources=[source('early','建议价格须经内部审批后生效。'),source('late','我司撤回价格建议。' if withdrawal else '财务已批准价格，合同生效。')]
        persisted=[];edges=[]
        repo=SimpleNamespace(event=lambda *args:None,record_research_step=lambda *args,**kwargs:None,
            record_claims=lambda t,rows:persisted.append(deepcopy(rows)),
            replace_turn_relations=lambda p,o,t,rows:edges.append(deepcopy(rows)) or rows)
        runtime=MailResearchRuntime(repo);runtime.context_limit=6000
        state={'phase2':True,'analysis_truncated':False,'analysis_completed':False,'findings':[],
               'retrieval_gaps':[],'context_reviews':{'mearly':{'complete':True,'evidence_ids':['early','late']},
                   'mlate':{'complete':True,'evidence_ids':['early','late']}},'allowed_keys':{'mearly','mlate'}}
        if withdrawal:state['context_reviews']['mearly']['change_evidence_ids']=['late']
        seen=[]
        def model(t,o,state,purpose,system,prompt,**kwargs):
            payload=json.loads(prompt)
            if purpose=='evidence_analysis':
                ids=[r['evidence_id'] for r in payload['evidence']];seen.extend(ids)
                self.assertEqual(len(ids),1);self.assertIn('batch_scope_note',payload)
                if fail_late and ids==['late']:return None
                q=payload['evidence'][0]['quote_text']
                return {'answer':'仅历史表达','claims':[{'claim_text':q,'source_quote':q,'evidence_ids':ids,
                    'actor':'alice@test','subject_ref':'价格','object_ref':'价格','predicate':'confirmed' if ids==['late'] else 'proposed'}],'findings':[]}
            verdicts=[]
            for c in payload['claims']:
                verdicts.append({'claim_id':c['claim_id'],'atoms':[{'text':c['claim_text'],'status':'supported',
                    'support_evidence_ids':c['evidence_ids'],'source_quotes':[{'evidence_id':c['evidence_ids'][0],'quote':c['source_quote']}]}],
                    'qualification_reviews':[{**q,'applicability':'applicable','rationale':'该条件直接限定价格生效。'} for q in payload['qualification_checks'][c['claim_id']]]})
            return {'verdicts':verdicts}
        # A controlled small complete-source budget guarantees two batches.
        with patch.object(runtime,'_model_call',side_effect=model),patch('services.mail.research.evidence_packing.pack_evidence_batches',wraps=pack_evidence_batches) as pack:
            projected=[model_source(r) for r in sources]
            overhead={'objective':'价格','prior_memory':{k:[] for k in ('facts','conditions','contradictions','open_questions')}}
            capacity=max(estimate_tokens(r) for r in projected)+estimate_tokens(overhead)+1010
            rows,_=runtime._analyze_evidence('t','owner',state,{'objective':'价格','budget':{'max_context_tokens':capacity}},'p',sources)
        return state,rows,persisted,edges,seen

    def test_cross_batch_union_persisted_once_and_coverage_not_poisoned(self):
        state,rows,persisted,edges,seen=self.run_batches()
        self.assertEqual(set(seen),{'early','late'})
        self.assertEqual(len(persisted),1);self.assertEqual(len(edges),1)
        self.assertEqual({e for r in rows for e in r['evidence_ids']},{'early','late'})
        self.assertTrue(all(r['semantic_status']=='supported' for r in rows))
        self.assertTrue(state['context_reviews']['mearly']['complete'])
        self.assertTrue(state['evidence_packing']['synthesis_complete'])
        self.assertEqual(len(state['semantic_verification']),2)

    def test_unprocessed_later_source_keeps_partial_and_all_omitted_ids(self):
        state,rows,persisted,edges,seen=self.run_batches(True)
        self.assertFalse(state['analysis_completed']);self.assertTrue(state['analysis_truncated'])
        self.assertEqual(state['evidence_packing']['analysis_omitted'],['late'])
        self.assertFalse(state['context_reviews']['mearly']['complete'])
        self.assertTrue(all(r['support_status']!='supported' for r in rows))

    def test_judge_output_reservation_matches_transport_and_disables_hidden_retry(self):
        quote='若审批通过则价格生效。'
        payload={'claims':[{'claim_id':'c'+str(i),'claim_text':quote,'source_quote':quote,'evidence_ids':['e']} for i in range(8)],
                 'qualification_checks':{'c'+str(i):[{'evidence_id':'e','quote':quote,'char_start':0}]*8 for i in range(8)}}
        requested=judge_output_tokens(payload);self.assertGreater(requested,2600)
        calls=[];records=[]
        model=SimpleNamespace(chat_json=lambda *args,**kwargs:calls.append(kwargs) or {'json':{'verdicts':[]},'usage':{'total_tokens':10}})
        runtime=MailResearchRuntime(SimpleNamespace(event=lambda *args:None,record_research_step=lambda *args,**kwargs:records.append((args,kwargs))))
        runtime.model=model;runtime.context_limit=100000
        state={'phase2':True,'model_calls':0,'max_model_calls':2,'max_seconds':120,'started':time.monotonic(),
               'token_usage':init_usage(),'analysis_truncated':False}
        with patch.object(runtime,'_continue_turn',return_value=True),patch.object(runtime,'_persist_runtime'):
            runtime._model_call('t','owner',state,'semantic_claim_verification','audit',json.dumps(payload),required_fields=('verdicts',))
        self.assertEqual(calls[0]['max_tokens'],requested)
        self.assertEqual(calls[0]['structured_retry_limit'],0)
        reservation=next(args[2]['reservation'] for args,kwargs in records if len(args)>2 and args[1]=='token_accounting' and 'reservation' in args[2])
        self.assertEqual(reservation['output_reserved'],requested)
        self.assertEqual(state['model_calls'],1)
        with self.assertRaises(ValueError):judge_output_tokens({'claims':payload['claims']*100,'qualification_checks':payload['qualification_checks']})

    def test_full_global_context_preserves_later_counter_across_batches(self):
        state,rows,_,_,_=self.run_batches(withdrawal=True)
        early=next(r for r in rows if r['evidence_ids']==['early'])
        self.assertEqual(early['support_status'],'context_qualified')
        self.assertIn('late',early['counter_evidence_ids'])
        self.assertEqual(early['source_dates'],['2026-09-15'])

    def test_extraction_budget_grows_with_sources_and_is_bounded(self):
        from services.mail.research.evidence_packing import analysis_output_tokens
        payload={'evidence':[source(str(i),'原'*500) for i in range(8)]}
        self.assertGreater(analysis_output_tokens(payload),2400)
        self.assertLessEqual(analysis_output_tokens(payload),32768)

    def test_extraction_dispatch_uses_dynamic_reservation_and_budget_stop(self):
        from services.mail.research.evidence_packing import analysis_output_tokens
        payload={'evidence':[source(str(i),'原'*500) for i in range(8)]}
        requested=analysis_output_tokens(payload);calls=[];records=[]
        runtime=MailResearchRuntime(SimpleNamespace(event=lambda *args:None,record_research_step=lambda *args,**kwargs:records.append((args,kwargs))))
        runtime.model=SimpleNamespace(chat_json=lambda *args,**kwargs:calls.append(kwargs) or {'json':{'answer':'历史表达','claims':[]},'usage':{'total_tokens':10}})
        runtime.context_limit=100000
        state={'phase2':True,'model_calls':0,'max_model_calls':2,'max_seconds':120,'started':time.monotonic(),
               'token_usage':init_usage(),'analysis_truncated':False}
        with patch.object(runtime,'_continue_turn',return_value=True),patch.object(runtime,'_persist_runtime'):
            runtime._model_call('t','owner',state,'evidence_analysis','audit',json.dumps(payload),required_fields=('answer','claims'))
            self.assertEqual(calls[0]['max_tokens'],requested)
            self.assertEqual(calls[0]['structured_retry_limit'],0)
            reservation=next(args[2]['reservation'] for args,kwargs in records if len(args)>2 and args[1]=='token_accounting' and 'reservation' in args[2])
            self.assertEqual(reservation['output_reserved'],requested)
            state['token_usage']=init_usage({'max_tokens':1});calls.clear()
            result=runtime._model_call('t','owner',state,'evidence_analysis','audit',json.dumps(payload),required_fields=('answer','claims'))
            self.assertIsNone(result);self.assertFalse(calls)

    def test_answer_dates_are_provenance_and_exact_text_dedup_keeps_conditions(self):
        runtime=MailResearchRuntime(SimpleNamespace())
        early='价格须经内部审批后生效。';late='已批准价格，合同自9月22日生效。'
        state={'deadline_reached':False,'tool_limit_reached':False,'analysis_completed':True,
               'findings':[{'text_zh':late,'source_dates':['2026-09-24T01:00:00+00:00']},
                           {'text_zh':early,'source_dates':['2026-09-15T01:00:00+00:00']}]}
        claims=[{'claim_text':early,'source_dates':['2026-09-15T01:00:00+00:00'],'support_status':'supported','state':'asserted'},
                {'claim_text':'当前原合同未生效。','source_dates':['2026-09-15'],'support_status':'insufficient','state':'inferred'}]
        answer=runtime._answer({'mode':'research'},[{}],{}, {'matched':2},time.monotonic(),state,claims)['answer']
        self.assertLess(answer.index('2026-09-15'),answer.index('2026-09-24'))
        self.assertEqual(answer.count(early),1)
        self.assertIn('邮件日期 [2026-09-24]：'+late,answer)
        self.assertNotIn('当前原合同未生效',answer)
        self.assertEqual(state['findings'][0]['text_zh'],late)
        for finding in state['findings']:finding.pop('source_dates')
        claims[0].pop('source_dates')
        old=runtime._answer({'mode':'research'},[{}],{}, {'matched':2},time.monotonic(),state,claims)['answer']
        self.assertEqual(old,'1. '+late+'\n2. '+early)
