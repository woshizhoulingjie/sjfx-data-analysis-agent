"""Literal local context plus explicit full-source qualification applicability."""
from unittest import TestCase
from services.mail.research.claim_verifier import verify_claims
from services.mail.research.verifier import verify_context


class QualificationScopeTests(TestCase):
    def check_claim(self, text, quote, body, applicability='not_applicable', ledger=True, actor='alice@test'):
        source={'evidence_id':'e','message_key':'m','quote_text':body,'char_start':0,
                'attribution':'authored','source_kind':'body',
                'body_fragments':[{'fragment_kind':'authored','char_start':0,'char_end':len(body)}],
                'participants':[{'role':'from','normalized_address':'alice@test'}]}
        claim={'claim_text':text,'source_quote':quote,'evidence_ids':['e'],'actor':actor,'object_ref':'交付'}
        def judge(system,payload):
            c=payload['claims'][0]
            verdict={'claim_id':c['claim_id'],'atoms':[{'text':text,'status':'supported',
                     'support_evidence_ids':['e'],'source_quotes':[{'evidence_id':'e','quote':quote}]}]}
            if ledger:
                verdict['qualification_reviews']=[{**q,'applicability':applicability,
                    'rationale':'该条件适用于另一事项；交付日期为独立陈述。' if applicability=='not_applicable' else '该条件限制本事项效力。'}
                    for q in payload['qualification_checks'][c['claim_id']]]
            return {'verdicts':[verdict]}
        result=verify_claims([claim],[source],judge)[0]
        return result,source

    def test_date_with_separate_compensation_condition(self):
        quote='交付日期确定为2026年10月18日。'
        result,source=self.check_claim(quote,quote,quote+'\n若再次逾期，承担违约责任。')
        self.assertEqual(result['semantic_status'],'supported')
        self.assertEqual(verify_context(result,{'e':source},{'m':{'complete':True}})['support_status'],'supported')

    def test_explicit_unresolved_qualification_is_complete_negative_evidence(self):
        from services.mail.person_summary import _complete_semantic_results
        quote='交付日期确定为2026年10月18日。'
        result,_=self.check_claim(quote,quote,quote+'\n若再次逾期，承担违约责任。','unresolved')
        audit=result['semantic_verification']
        self.assertEqual(result['semantic_status'],'insufficient')
        self.assertIn('qualification_scope_unresolved',audit['rule_gaps'])
        self.assertNotIn('qualification_scope_unproven',audit['rule_gaps'])
        self.assertTrue(audit['qualification_scope']['complete'])
        self.assertEqual(audit['qualification_scope']['reviewed'][0]['applicability'],'unresolved')
        self.assertTrue(_complete_semantic_results([result]))

    def test_missing_qualification_entry_cannot_be_cached_as_complete(self):
        from services.mail.person_summary import _complete_semantic_results
        quote='交付日期确定为2026年10月18日。'
        result,_=self.check_claim(quote,quote,quote+'\n若再次逾期，承担违约责任。',ledger=False)
        self.assertEqual(result['semantic_status'],'insufficient')
        self.assertIn('qualification_scope_unproven',result['semantic_verification']['rule_gaps'])
        self.assertFalse(_complete_semantic_results([result]))

    def test_compensation_cannot_drop_if_from_same_paragraph(self):
        result,_=self.check_claim('承担违约责任。','承担违约责任。','若再次逾期，承担违约责任。')
        self.assertEqual(result['semantic_status'],'insufficient')
        self.assertIn('number_time_negation_or_condition_mismatch',result['semantic_verification']['rule_gaps'])

    def test_cross_paragraph_approval_blocks_unconditional_acceptance(self):
        quote='交付验收已确认。'
        result,_=self.check_claim(quote,quote,quote+'\n该确认需经内部审批后生效。','applicable')
        self.assertEqual(result['semantic_status'],'insufficient')
        self.assertIn('applicable_qualification_omitted',result['semantic_verification']['rule_gaps'])

    def test_short_quote_without_full_scope_audit_fails_closed(self):
        quote='交付验收已确认。'
        result,source=self.check_claim(quote,quote,quote+'\n该确认需经内部审批后生效。',ledger=False)
        self.assertEqual(result['semantic_status'],'insufficient')
        self.assertEqual(verify_context(result,{'e':source},{'m':{'complete':True}})['support_status'],'context_incomplete')

    def test_unrelated_negation_does_not_contaminate_historical_fact(self):
        quote='曾确认交付日期为2026年10月18日。'
        result,_=self.check_claim(quote,quote,quote+'\n另一设备报价尚未批准。')
        self.assertEqual(result['semantic_status'],'supported')

    def test_wrong_actor_and_spliced_quote_stay_rejected(self):
        quote='交付日期确定为2026年10月18日。'
        result,_=self.check_claim(quote,quote,quote,actor='other@test')
        self.assertEqual(result['semantic_status'],'insufficient')
        result,_=self.check_claim(quote,'交付日期为2026年10月18日。',quote)
        self.assertEqual(result['semantic_status'],'insufficient')

    def test_context_nonliteral_anchor_stays_rejected(self):
        quote='交付日期确定为2026年10月18日。'
        result,source=self.check_claim(quote,quote,quote+'\n若再次逾期，承担违约责任。')
        result['object_ref']='蓝湾项目最终交期'
        checked=verify_context(result,{'e':source},{'m':{'complete':True}})
        self.assertIn('事项锚点未在引用原文中出现',checked['reason'])

    def test_judge_cross_source_citation_stays_rejected(self):
        quote='交付日期确定为2026年10月18日。'
        claim={'claim_text':quote,'source_quote':quote,'evidence_ids':['e']}
        sources=[{'evidence_id':key,'quote_text':quote} for key in ['e','other']]
        def judge(system,payload):
            return {'verdicts':[{'claim_id':payload['claims'][0]['claim_id'], 'atoms':[{
                'text':quote,'status':'supported','support_evidence_ids':['other'],
                'source_quotes':[{'evidence_id':'other','quote':quote}]}]}]}
        result=verify_claims([claim],sources,judge)[0]
        self.assertEqual(result['semantic_status'],'insufficient')
        self.assertIn('judge_used_unprovided_source',result['semantic_verification']['missing_requirements'])

    def test_relation_scope_binding_and_strict_fallback(self):
        from unittest.mock import patch
        from config import Config
        from services.mail.research.verifier import verify_business_relation
        quote='确认交付日期为2026年10月18日。'
        result, source=self.check_claim(quote,quote,quote+'\n若再次逾期，承担违约责任。')
        result.update(predicate='confirmed',subject_ref='交付',state='asserted')
        with patch.object(Config,'MAIL_ENABLE_AGENT_MINING',True):
            self.assertEqual(verify_business_relation(result,{'e':source})['support_status'],'supported')
            for changed in ['actor','source_quote','claim_text']:
                from copy import deepcopy
                mutated=deepcopy(result);mutated[changed]='篡改'
                self.assertNotEqual(verify_business_relation(mutated,{'e':source})['support_status'],'supported')
            mutated_source={**source,'quote_text':source['quote_text']+'\n还须经审批。'}
            self.assertNotEqual(verify_business_relation(result,{'e':mutated_source})['support_status'],'supported')
            result.pop('semantic_verification')
            self.assertNotEqual(verify_business_relation(result,{'e':source})['support_status'],'supported')

    def test_runtime_finding_preserves_scope_audit_and_missing_audit_fails(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import patch
        from services.mail.research.runtime import MailResearchRuntime
        quote='确认交付日期为2026年10月18日。'
        body=quote+'\n若再次逾期，承担违约责任。'
        source={'evidence_id':'e','message_key':'m','quote_text':body,'char_start':0,'source_kind':'body',
                'body_fragments':[{'fragment_kind':'authored','char_start':0,'char_end':len(body)}],
                'participants':[{'role':'from','normalized_address':'alice@test'}]}
        for include in [True,False]:
            runtime=MailResearchRuntime(SimpleNamespace(event=lambda *args:None,
                record_research_step=lambda *args,**kwargs:None,record_claims=lambda *args:None,
                replace_turn_relations=lambda *args:[]))
            state={'phase2':True,'analysis_truncated':False,'retrieval_gaps':[],'findings':[],
                   'context_reviews':{'m':{'complete':True,'gaps':[]}},'context_gaps':[],'allowed_keys':{'m'}}
            def model(t,o,state,purpose,system,prompt,**kwargs):
                payload=json.loads(prompt)
                if purpose=='evidence_analysis':
                    return {'answer':'交付日期','claims':[],'findings':[{'text_zh':quote,'evidence_id':'e',
                        'source_quote':quote,'object_ref':'交付','actor':'alice@test'}]}
                c=payload['claims'][0]
                verdict={'claim_id':c['claim_id'],'atoms':[{'text':quote,'status':'supported',
                    'support_evidence_ids':['e'],'source_quotes':[{'evidence_id':'e','quote':quote}]}]}
                if include:verdict['qualification_reviews']=[{**q,'applicability':'not_applicable',
                    'rationale':'条件涉及逾期责任，独立于已确认日期。'} for q in payload['qualification_checks'][c['claim_id']]]
                return {'verdicts':[verdict]}
            with patch.object(runtime,'_model_call',side_effect=model):
                runtime._analyze_evidence('t','owner',state,{'objective':'交期','budget':{}},'p',[source])
            self.assertEqual(state['findings'][0]['status'],'context_verified' if include else 'needs_review')
