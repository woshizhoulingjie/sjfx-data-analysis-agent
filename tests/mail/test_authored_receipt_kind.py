"""The sender's receipt acknowledgement is an action, not incoming-only information."""
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from services.mail import analysis

QUOTE='调整后的报价明细已收到，我司内部已作初步评审。'
ACTOR='chen.li@example.test'
RECIPIENT='wang.rui@example.test'


def sample(kind='received',actor=ACTOR):
    chunk={'chunk_id':'c','message_key':'m','segment_id':'s','mime_path':'1',
           'source_kind':'body','text':QUOTE,'char_start':6,'char_end':6+len(QUOTE),
           'attribution_version':2,'authored':True,
           'body_fragments':[{'fragment_kind':'authored','char_start':6,'char_end':6+len(QUOTE)}],
           'participants':[{'role':'from','address':ACTOR},{'role':'to','address':RECIPIENT}]}
    row={'actor':actor,'kind':kind,'act_type':'acknowledge_receipt','intent':'acknowledge_receipt',
         'issue':'报价资料收到确认','statement':QUOTE,'uncertainty':'收到不表示接受报价',
         'conditions':[],'contradictions':[],'unresolved_questions':[],
         'citations':[{'chunk_id':'c','quote':QUOTE,'char_start':6}]}
    return {'findings':[row]},chunk


class AuthoredReceiptKindTests(TestCase):
    def test_sender_received_rejected_with_exact_category_repair_hint(self):
        value,chunk=sample()
        with self.assertRaises(ValueError) as caught:
            analysis.validate_dossier(value,[chunk],[ACTOR,RECIPIENT],{'m'})
        error=str(caught.exception)
        self.assertIn('第1条发现',error)
        self.assertIn('kind=action',error)
        self.assertIn('acknowledge_receipt',error)
        self.assertIn('禁止换actor',error)
        self.assertEqual(value['findings'][0]['kind'],'received')

    def test_sender_action_receipt_passes_without_becoming_accept(self):
        value,chunk=sample('action')
        found=analysis.validate_dossier(value,[chunk],[ACTOR,RECIPIENT],{'m'})
        self.assertEqual(found[0]['actor'],ACTOR)
        self.assertEqual(found[0]['kind'],'action')
        self.assertEqual(found[0]['intent'],'acknowledge_receipt')

    def test_recipient_cannot_inherit_authored_acknowledgement_as_action(self):
        value,chunk=sample('action',RECIPIENT)
        with self.assertRaises(ValueError):analysis.validate_dossier(value,[chunk],[ACTOR,RECIPIENT],{'m'})

    def test_received_category_stays_available_only_for_actual_recipient_information(self):
        value,chunk=sample('received',RECIPIENT)
        value['findings'][0].update(act_type='inform',intent='inform',statement='收到对方表示报价明细已收到的信息。')
        found=analysis.validate_dossier(value,[chunk],[ACTOR,RECIPIENT],{'m'})
        self.assertEqual(found[0]['actor'],RECIPIENT)
        self.assertEqual(found[0]['kind'],'received')
        value['findings'][0]['actor']='outside@example.test'
        with self.assertRaises(ValueError):analysis.validate_dossier(value,[chunk],[ACTOR,RECIPIENT,'outside@example.test'],{'m'})

    def test_model_must_repair_kind_explicitly_before_cursor_advances(self):
        value,chunk=sample();calls=[];commits=[]
        checkpoint={'phase':'reading','cursor':[0,0],'model_calls':0,'batches':0,'characters_read':0,'findings_count':0}
        spec={'mode':'message_dossier','engine_version':analysis.VERSION,'package_id':'p',
              'context_keys':['m'],'message_keys':['m'],'parties':[ACTOR,RECIPIENT],
              'objective':'分析收到与接受','manifest':{'metadata_fingerprint':'f'}}
        repo=SimpleNamespace(get_turn=lambda *args:{'status':'running','task_spec':spec,'final':checkpoint},event=lambda *args:None)
        def call(repo,tid,owner,spec,cp,model,system,payload,*args):
            calls.append(deepcopy(payload));result=deepcopy(value)
            if len(calls)==2:
                self.assertIn('kind=action',payload['validation_error'])
                self.assertIn('acknowledge_receipt',payload['validation_error'])
                self.assertEqual(commits,[])
                self.assertEqual(payload['previous_output']['findings'][0]['actor'],ACTOR)
                result['findings'][0]['kind']='action'
            return result,{**checkpoint,'model_calls':len(calls)}
        with patch.object(analysis,'manifest',return_value={'metadata_fingerprint':'f'}), \
             patch.object(analysis,'next_chunks',return_value=([chunk],[99,0])), \
             patch.object(analysis,'_call',side_effect=call), \
             patch.object(analysis,'_commit',side_effect=lambda *args,**kwargs:commits.append((args,kwargs))):
            analysis.run_slice(repo,'t','owner',object())
        self.assertEqual(len(calls),2)
        self.assertEqual(commits[0][0][4]['cursor'],[99,0])
        self.assertEqual(commits[0][1]['value']['findings'][0]['intent'],'acknowledge_receipt')

    def test_read_prompt_separates_category_role_and_communication_act(self):
        self.assertIn('kind=action and act_type/intent=acknowledge_receipt',analysis.READ_SYSTEM)
        self.assertIn('do not switch actor',analysis.READ_SYSTEM)
