"""One bulk repair preserves literal originals, roles and strict semantic gates."""
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from services.mail.research.runtime import MailResearchRuntime
from services.mail.research.claim_verifier import verify_claims
from services.mail.research.verifier import verify_business_relation

QUOTE='买方要求 2026 年 10 月 30 日到货。'
CLAIM='买方要求2026年10月30日到货。'
SOURCE={'evidence_id':'e','message_key':'m','source_kind':'body','quote_text':QUOTE,'char_start':0,
        'attribution':'authored','body_fragments':[{'fragment_kind':'authored','char_start':0,'char_end':len(QUOTE)}],
        'participants':[{'role':'from','normalized_address':'alice@test'}]}


def row(quote=QUOTE):
    return {'claim_text':CLAIM,'source_quote':quote,'actor':'alice@test','subject_ref':'买方',
            'object_ref':'到货','predicate':'proposed','state':'asserted','evidence_ids':['e']}


def verdict(payload,quote):
    return {'verdicts':[{'claim_id':c['claim_id'],'atoms':[{'text':c['claim_text'],'status':'supported',
        'support_evidence_ids':['e'],'source_quotes':[{'evidence_id':'e','quote':quote}]}]} for c in payload['claims']]}


class ResearchQuoteRepairTests(TestCase):
    def test_unavailable_extraction_repair_preserves_strict_failure(self):
        calls=[];recorded=[]
        repo=SimpleNamespace(event=lambda *args:None,record_research_step=lambda *args,**kwargs:None,
                             record_claims=lambda t,r:recorded.extend(r),replace_turn_relations=lambda *args:[])
        runtime=MailResearchRuntime(repo)
        state={'phase2':True,'analysis_truncated':False,'retrieval_gaps':[],'findings':[],
               'context_reviews':{'m':{'complete':True,'gaps':[]}},'context_gaps':[],'allowed_keys':{'m'}}
        def model(t,o,state,purpose,system,prompt,**kwargs):
            payload=json.loads(prompt);calls.append(purpose)
            if purpose=='evidence_analysis_repair':
                self.assertEqual(payload['invalid_rows'][0]['atom_index'],0)
                self.assertIn('source_quote_not_exact',payload['validation_errors'][0]['errors'])
                return None
            if purpose=='evidence_analysis':
                return {'answer':'待验证','claims':[row(CLAIM)],'findings':[]}
            return verdict(payload,QUOTE)
        with patch.object(runtime,'_model_call',side_effect=model):
            runtime._analyze_evidence('t','owner',state,{'objective':'交期','budget':{}},'p',[deepcopy(SOURCE)])
        self.assertEqual(calls,['evidence_analysis','evidence_analysis_repair','semantic_claim_verification'])
        self.assertEqual(recorded[0]['semantic_status'],'insufficient')
        self.assertIn('source_quote_not_exact',recorded[0]['semantic_verification']['missing_requirements'])

    def test_extraction_then_judge_each_have_one_bulk_literal_repair_and_audit(self):
        events=[];recorded=[];calls=[]
        repo=SimpleNamespace(event=lambda *args:events.append(args),record_research_step=lambda *args,**kwargs:None,
                             record_claims=lambda t,r:recorded.extend(r),replace_turn_relations=lambda *args:[])
        runtime=MailResearchRuntime(repo)
        state={'phase2':True,'analysis_truncated':False,'retrieval_gaps':[],'findings':[],
               'context_reviews':{'m':{'complete':True,'gaps':[]}},'context_gaps':[],'allowed_keys':{'m'}}
        def model(t,o,state,purpose,system,prompt,**kwargs):
            payload=json.loads(prompt);calls.append((purpose,payload))
            if purpose=='evidence_analysis':
                self.assertEqual(payload['evidence'][0]['participants'],SOURCE['participants'])
                self.assertEqual(payload['evidence'][0]['body_fragments'],SOURCE['body_fragments'])
                return {'answer':'说明交付要求','claims':[row(CLAIM)],'findings':[]}
            if purpose=='evidence_analysis_repair':
                self.assertIn('source_quote_not_exact',payload['validation_errors'][0]['errors'])
                self.assertEqual(payload['invalid_rows'],[{'atom_index':0,'row':row(CLAIM)}])
                return {'repairs':[{'atom_index':0,'row':row()}]}
            if not payload.get('validation_errors'):return verdict(payload,CLAIM)
            self.assertIn('judge_quote_not_exact',payload['validation_errors'][0]['errors'])
            self.assertEqual(payload['evidence'][0]['quote_text'],QUOTE)
            return verdict(payload,QUOTE)
        with patch.object(runtime,'_model_call',side_effect=model):
            runtime._analyze_evidence('t','owner',state,{'objective':'查交期','budget':{}},'p',[deepcopy(SOURCE)])
        self.assertEqual([c[0] for c in calls],['evidence_analysis','evidence_analysis_repair','semantic_claim_verification','semantic_claim_verification'])
        self.assertEqual(recorded[0]['semantic_status'],'supported')
        self.assertEqual([e[2]['repair'] for e in events if e[1]=='evidence_analysis_output'],[False,True])
        self.assertEqual([e[2]['repair'] for e in events if e[1]=='semantic_claim_judge_output'],[False,True])

    def test_failed_or_exhausted_judge_repair_does_not_accept_bad_quote(self):
        for response in [None,'same bad quote']:
            calls=[]
            def judge(system,payload):
                calls.append(payload)
                if len(calls)==2 and response is None:return None
                return verdict(payload,CLAIM)
            checked=verify_claims([row()],[SOURCE],judge,allowed_keys={'m'},_repair=True)[0]
            self.assertEqual(len(calls),2)
            self.assertEqual(checked['semantic_status'],'insufficient')
            self.assertIn('judge_quote_not_exact',checked['semantic_verification']['missing_requirements'])

    def test_semantic_disagreement_without_quote_defect_is_not_retried(self):
        calls=[]
        def judge(system,payload):
            calls.append(payload)
            value=verdict(payload,QUOTE);value['verdicts'][0]['atoms'][0]['status']='insufficient'
            return value
        checked=verify_claims([row()],[SOURCE],judge,_repair=True)[0]
        self.assertEqual(len(calls),1)
        self.assertEqual(checked['semantic_status'],'insufficient')

    def test_request_requirement_is_proposal_never_confirmation(self):
        self.assertEqual(verify_business_relation(row(),{'e':SOURCE})['support_status'],'supported')
        confirmation={**row(),'predicate':'confirmed'}
        self.assertEqual(verify_business_relation(confirmation,{'e':SOURCE})['support_status'],'predicate_not_expressed')
        unsupported={**SOURCE,'quote_text':'买方记录了到货安排。'}
        self.assertEqual(verify_business_relation(row(),{'e':unsupported})['support_status'],'predicate_not_in_evidence')

    def test_english_request_is_proposal_and_acceptance_is_not_remapped(self):
        source={**SOURCE,'quote_text':'Buyer requests delivery.'}
        proposal={**row(),'claim_text':'Buyer requests delivery.','subject_ref':'Buyer','object_ref':'delivery'}
        self.assertEqual(verify_business_relation(proposal,{'e':source})['support_status'],'supported')
        accepted={**proposal,'predicate':'confirmed','claim_text':'Buyer accepted delivery.'}
        self.assertEqual(verify_business_relation(accepted,{'e':source})['support_status'],'predicate_not_in_evidence')
