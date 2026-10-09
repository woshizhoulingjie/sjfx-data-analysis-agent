"""Completion capacity, bounded complete repairs, and global source coverage."""
import json
import time
from copy import deepcopy
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from services.mail.research.claim_verifier import judge_output_tokens, pack_judge_payloads
from services.mail.research.evidence_packing import analysis_output_tokens, model_source
from services.mail.research.output_budget import estimate_output_tokens, retrieval_output_tokens
from services.mail.research.relevance import classify
from services.mail.research.runtime import MailResearchRuntime
from services.mail.research.token_budget import estimate_tokens, init_usage


class StructuredCapacityTests(TestCase):
    def test_chinese_and_opaque_english_output_are_more_conservative_than_input(self):
        for text in ['审批通过后生效。' * 500, 'opaque-id-93afe7c-01234: approved only after review. ' * 100]:
            self.assertGreaterEqual(estimate_output_tokens(text), estimate_tokens(text) * 1.49)

    def test_repairs_reserve_complete_prior_extraction_and_judge_without_mutation(self):
        extraction={'evidence':[{'evidence_id':'e','quote_text':'需审批后生效。'}]}
        judge={'claims':[{'claim_id':'c','claim_text':'需审批后生效。','source_quote':'需审批后生效。','evidence_ids':['e']}],
               'qualification_checks':{'c':[]}}
        for payload, field, callback in [(extraction,'previous_output',analysis_output_tokens),
                                        (judge,'previous_verdicts',judge_output_tokens)]:
            initial=callback(payload)
            payload[field]={'complete_prior_result':'条件' * 5000}
            prior=deepcopy(payload)
            self.assertGreater(callback(payload),initial)
            self.assertEqual(payload,prior)
            payload[field]={'complete_prior_result':'条件' * 30000}
            with self.assertRaises(ValueError):callback(payload)

    def test_single_source_reserves_multiple_claims_and_findings(self):
        source={'evidence_id':'e','quote_text':'原文条件。' * 300}
        one=analysis_output_tokens({'evidence':[source]})
        eight=analysis_output_tokens({'evidence':[source]*8})
        self.assertEqual(one,eight)
        self.assertGreater(one,20000)
        self.assertLessEqual(one,32768)

    def test_judge_partition_keeps_complete_sources_conditions_and_prior_verdicts(self):
        claims=[{'claim_id':'c'+str(i),'claim_text':'价格须审批后生效。','source_quote':'价格须审批后生效。',
                 'evidence_ids':['e'+str(i)],'conditions':['财务审批后生效']} for i in range(6)]
        evidence=[{'evidence_id':'e'+str(i),'quote_text':'正文'*300+'价格须审批后生效。','sent_at_utc':'2026-09-15'} for i in range(6)]
        payload={'claims':claims,'evidence':evidence,
                 'qualification_checks':{c['claim_id']:[{'evidence_id':c['evidence_ids'][0],
                    'quote':'价格须审批后生效。','char_start':600}] for c in claims},
                 'previous_verdicts':{'verdicts':[{'claim_id':c['claim_id'],'atoms':[{'text':c['claim_text']}]} for c in claims]},
                 'validation_errors':[{'claim_id':c['claim_id'],'errors':['judge_quote_not_exact']} for c in claims]}
        original=deepcopy(payload)
        batches=pack_judge_payloads(payload,max_chars=2200)
        self.assertGreater(len(batches),1)
        self.assertEqual([c for b in batches for c in b['claims']],claims)
        self.assertEqual([s for b in batches for s in b['evidence']],evidence)
        self.assertEqual([v for b in batches for v in b['previous_verdicts']['verdicts']],payload['previous_verdicts']['verdicts'])
        self.assertEqual(payload,original)
        for batch in batches:
            self.assertLessEqual(len(json.dumps(batch,ensure_ascii=False)),2200)
            self.assertLessEqual(judge_output_tokens(batch),32768)
            for c in batch['claims']:
                self.assertEqual(batch['qualification_checks'][c['claim_id']],payload['qualification_checks'][c['claim_id']])

    def test_model_source_keeps_canonical_aliases_and_conditions(self):
        source={'evidence_id':'e','quote_text':'审批条件。','conditions':['财务审批'],
                'source_aliases':['e-old'],'occurrence_id':'original-part','object_version':'v2'}
        projected=model_source(source)
        for key in source:self.assertEqual(projected[key],source[key])

    def test_relevance_complete_repairs_fit_context_and_preserve_every_message(self):
        events=[];calls=[]
        text='价格 90,000.00 元/台，需审批后确认。'*45
        class Connection:
            def execute(self,sql,args):
                if sql.startswith('SELECT *'):
                    return [{'source_kind':'body','mime_path':'1','message_key':args[1],'text':text}]
                return SimpleNamespace(fetchone=lambda:(0,))
        class Repository:
            @contextmanager
            def _connection(self):yield Connection()
            def create_evidence(self,pid,owner,row):return {'evidence_id':'e-'+row['message_key'],'quote_text':row['text']}
            def event(self,*args):events.append(args)
            def record_research_step(self,*args,**kwargs):pass
        state={'phase2':True,'max_model_calls':30,'model_calls':0}
        def model(tid,owner,state,purpose,system,prompt,**kwargs):
            self.assertLessEqual(len(prompt),24000)
            payload=json.loads(prompt);calls.append(payload);state['model_calls']+=1
            repaired='validation_error' in payload
            return {'decisions':[{'message_key':m['message_key'],'status':'matched','rationale':'条件需核实。'*100,
                'evidence_id':m['evidence'][0]['evidence_id'],'source_quote':text if repaired else 'incorrect exact quote'} for m in payload['messages']]}
        runtime=SimpleNamespace(repository=Repository(),context_limit=24000,_model_call=model)
        candidates=[{'message_key':'m'+str(i)} for i in range(12)]
        result=classify(runtime,'t','owner',state,{'package_id':'p','objective':'价格条件','scope':{}},candidates)
        self.assertEqual(result,candidates)
        self.assertEqual(len(state['relevance_decisions']),12)
        self.assertTrue(all(d['status']=='matched' for d in state['relevance_decisions'].values()))
        self.assertTrue(any('validation_error' in call for call in calls))
        for call in calls:
            for message in call['messages']:self.assertEqual(message['evidence'][0]['text'],text)
            self.assertLessEqual(retrieval_output_tokens('relevance',call),32768)

    def test_rerank_bounded_batches_never_drop_unranked_or_foreign_ids(self):
        repository=SimpleNamespace(event=lambda *args:None)
        runtime=MailResearchRuntime(repository,context_limit=3200)
        state={'max_model_calls':5,'model_calls':0}
        candidates=[{'segment_id':'s'+str(i),'message_key':'m'+str(i),'text':'证据'*350} for i in range(10)]
        calls=[]
        def model(*args,**kwargs):
            prompt=args[5];self.assertLessEqual(len(prompt),3200)
            payload=json.loads(prompt);calls.append(payload);state['model_calls']+=1
            return {'segment_ids':['foreign']+[c['segment_id'] for c in reversed(payload['candidates'])]}
        with patch('config.Config.MAIL_RERANK_ENABLED',True),patch.object(runtime,'_model_call',side_effect=model):
            result=runtime._rerank('t','o',state,{'objective':'审查'},candidates)
        self.assertGreater(len(calls),1)
        self.assertEqual({r['segment_id'] for r in result},{r['segment_id'] for r in candidates})
        self.assertEqual(len(result),len(candidates))
        self.assertEqual(state['model_calls'],3)

    def test_parse_repaired_length_response_is_rejected_and_retry_is_explicit(self):
        calls=[];events=[]
        runtime=MailResearchRuntime(SimpleNamespace(event=lambda *args:events.append(args),
            record_research_step=lambda *args,**kwargs:None),model=SimpleNamespace(chat_json=lambda *args,**kwargs:
            calls.append(kwargs) or {'json':{'segment_ids':['s']},'finish_reason':'length','usage':{'total_tokens':10}}))
        state={'phase2':True,'model_calls':0,'max_model_calls':3,'max_seconds':30,'started':time.monotonic(),
               'token_usage':init_usage(),'analysis_truncated':False}
        with patch.object(runtime,'_continue_turn',return_value=True),patch.object(runtime,'_persist_runtime'):
            result=runtime._model_call('t','owner',state,'rerank','rank',json.dumps({'candidates':[{'segment_id':'s'}]}),required_fields=('segment_ids',))
        self.assertIsNone(result)
        self.assertEqual(state['model_calls'],1)
        self.assertEqual(state['token_usage']['calls'],1)
        self.assertEqual(calls[0]['structured_retry_limit'],0)
        self.assertTrue(calls[0]['long_output'])
        self.assertTrue(calls[0]['allow_short_timeout'])
        self.assertLessEqual(calls[0]['timeout'],30)
        self.assertTrue(any(e[1]=='model_output_truncated' for e in events))

    def test_rerank_batches_keep_sources_with_missing_or_duplicate_segment_id_once(self):
        runtime=MailResearchRuntime(SimpleNamespace(event=lambda *args:None),context_limit=3200)
        candidates=[{'segment_id':None if i%2 else 'shared-id','message_key':'m'+str(i),'text':'证据'*350} for i in range(8)]
        state={'max_model_calls':30,'model_calls':0}
        def model(*args,**kwargs):
            state['model_calls']+=1
            return {'segment_ids':['shared-id']}
        with patch('config.Config.MAIL_RERANK_ENABLED',True),patch.object(runtime,'_model_call',side_effect=model):
            result=runtime._rerank('t','owner',state,{'objective':'审查'},candidates)
        self.assertEqual(len(result),len(candidates))
        self.assertEqual({r['message_key'] for r in result},{r['message_key'] for r in candidates})
