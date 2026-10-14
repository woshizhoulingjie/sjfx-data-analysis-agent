"""Production config expressions and complete retrieval output reservations."""
import ast
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from config import Config
from services.mail.research.output_budget import retrieval_output_tokens
from services.mail.research.runtime import MailResearchRuntime
from services.mail.research.token_budget import init_usage,estimate_tokens


class OrdinaryResearchDefaultsTests(TestCase):
    def expressions(self):
        module=ast.parse((Path(__file__).resolve().parents[1]/'config.py').read_text(encoding='utf-8'))
        config=next(n for n in module.body if isinstance(n,ast.ClassDef) and n.name=='Config')
        wanted={'MAIL_RESEARCH_MAX_MODEL_CALLS','MAIL_RESEARCH_MAX_TOOL_CALLS','MAIL_RESEARCH_MAX_TOKENS','MAIL_RESEARCH_TIMEOUT_SECONDS'}
        return {n.targets[0].id:compile(ast.Expression(n.value),'config.py','eval') for n in config.body
                if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name) and n.targets[0].id in wanted}

    def test_actual_default_expressions_cover_first_full_batch_pass(self):
        with patch.dict(os.environ,{},clear=True):
            values={k:eval(v,{'os':os,**vars(Config)}) for k,v in self.expressions().items()}
        self.assertEqual(values,{'MAIL_RESEARCH_MAX_MODEL_CALLS':512,'MAIL_RESEARCH_MAX_TOOL_CALLS':2000,
            'MAIL_RESEARCH_MAX_TOKENS':5000000,'MAIL_RESEARCH_TIMEOUT_SECONDS':86400})
        self.assertGreaterEqual(values['MAIL_RESEARCH_MAX_MODEL_CALLS'],14*2)

    def test_env_clamps_match_resume_service_caps(self):
        expressions=self.expressions()
        with patch.dict(os.environ,{k:'9999999999' for k in expressions},clear=True):
            values={k:eval(v,{'os':os,**vars(Config)}) for k,v in expressions.items()}
        self.assertEqual(values,{'MAIL_RESEARCH_MAX_MODEL_CALLS':32768,'MAIL_RESEARCH_MAX_TOOL_CALLS':100000,
            'MAIL_RESEARCH_MAX_TOKENS':100000000,'MAIL_RESEARCH_TIMEOUT_SECONDS':604800})


class RetrievalOutputBudgetTests(TestCase):
    def payload(self):
        return {'messages':[{'message_key':'m'+str(i),'evidence':[{'evidence_id':'e'+str(i),'text':'报价附审批条件。'*100}]} for i in range(6)]}

    def test_six_decisions_long_chinese_quotes_fit_full_schema_budget(self):
        payload=self.payload()
        response={'decisions':[{'message_key':m['message_key'],'status':'matched','rationale':'需核实审批状态。'*50,
            'evidence_id':m['evidence'][0]['evidence_id'],'source_quote':m['evidence'][0]['text']} for m in payload['messages']]}
        requested=retrieval_output_tokens('relevance',payload)
        self.assertGreater(requested,1200);self.assertGreater(requested,estimate_tokens(response))
        self.assertLessEqual(requested,32768)
        old={'decisions':[{'message_key':'m0','status':'matched','source_quote':'中'*2000,'rationale':'旧'*600}]}
        payload['previous_output']=old
        self.assertGreater(retrieval_output_tokens('relevance',payload),requested)
        self.assertEqual(payload['previous_output'],old)

    def test_complete_rerank_ids_and_scores_are_reserved(self):
        payload={'candidates':[{'segment_id':'seg'+str(i)} for i in range(40)]}
        requested=retrieval_output_tokens('rerank',payload)
        self.assertGreater(requested,700);self.assertLessEqual(requested,32768)
        response={'segment_ids':[r['segment_id'] for r in payload['candidates']],
                  'scores':[{'segment_id':r['segment_id'],'score':0.95,'rationale':'相关条件需核实。'*10} for r in payload['candidates']]}
        self.assertGreater(requested,estimate_tokens(response))

    def test_transport_reservation_and_explicit_skip_for_both_purposes(self):
        calls=[];records=[];events=[]
        runtime=MailResearchRuntime(SimpleNamespace(event=lambda *args:events.append(args),
            record_research_step=lambda *args,**kwargs:records.append((args,kwargs))))
        runtime.context_limit=1000000
        runtime.model=SimpleNamespace(chat_json=lambda *args,**kwargs:calls.append((args,kwargs)) or {
            'json':{'decisions':[],'segment_ids':[]},'usage':{'total_tokens':20}})
        state={'phase2':True,'model_calls':0,'max_model_calls':5,'max_seconds':120,'started':time.monotonic(),
            'token_usage':init_usage(),'analysis_truncated':False}
        payload=self.payload();payload['previous_output']={'decisions':[{'message_key':'m0','status':'matched','source_quote':'原引用'}]}
        with patch.object(runtime,'_continue_turn',return_value=True),patch.object(runtime,'_persist_runtime'):
            for purpose,p,fields in [('relevance',payload,('decisions',)),
                ('rerank',{'candidates':[{'segment_id':i} for i in range(40)]},('segment_ids',))]:
                expected=retrieval_output_tokens(purpose,p)
                runtime._model_call('t','owner',state,purpose,'complete rows',json.dumps(p),required_fields=fields)
                self.assertEqual(calls[-1][1]['max_tokens'],expected)
                self.assertEqual(calls[-1][1]['structured_retry_limit'],0)
                sent=json.loads(calls[-1][0][1]);self.assertEqual(sent,p)
                reservation=[a[2]['reservation'] for a,k in records if a[1]=='token_accounting' and 'reservation' in a[2]][-1]
                self.assertEqual(reservation['output_reserved'],expected)
            before=len(calls)
            too_big={'messages':[{'message_key':'m','evidence':[{'evidence_id':'e','text':'中'*40000}]}]}
            result=runtime._model_call('t','owner',state,'relevance','complete rows',json.dumps(too_big),required_fields=('decisions',))
            self.assertIsNone(result);self.assertEqual(len(calls),before);self.assertTrue(state['analysis_truncated'])
            self.assertTrue(any(a[1]=='model_skipped' and a[2]['reason']=='output_capacity' for a in events))
