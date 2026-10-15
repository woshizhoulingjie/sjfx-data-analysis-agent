from copy import deepcopy
from unittest import TestCase
from services.mail.analysis import _review_qualification_checks, _review_qualification_scope, validate_dossier


class DossierReviewScopeTests(TestCase):
    def sample(self, statement='邮件收到，已开始初评。', body=None, effect='none', app='not_applicable'):
        body=body or statement+'\n接受报价须经内部审批后生效。'
        chunk={'chunk_id':'c','message_key':'m','segment_id':1,'mime_path':'1','text':body,
               'char_start':10,'char_end':10+len(body),'source_kind':'body','authored':True,
               'attribution_version':2,'participants':[{'role':'from','address':'alice@test'}],
               'body_fragments':[{'fragment_kind':'authored','char_start':10,'char_end':10+len(body)}]}
        finding={'finding_id':'f','actor':'alice@test','kind':'action','intent':'inform','issue':'报价',
                 'statement':statement,'conditions':[],'contradictions':[],'unresolved_questions':[],
                 'uncertainty':'仅为邮件表达','citations':[{'chunk_id':'c','quote':statement,'char_start':10,'char_end':10+len(statement)}]}
        checks=_review_qualification_checks([chunk])
        review={'qualification_reviews':[{'check_id':q['check_id'],'applicability':app,'effect':effect,
            'rationale':'审批仅限制报价接受效力，收到和初评是独立历史表达。' if app=='not_applicable' else '该审批或撤回直接限制本事项。',
            'citations':[{'chunk_id':q['chunk_id'],'quote':q['quote'],'char_start':q['char_start']}]}
            for q in checks]}
        return finding,review,chunk

    def run_review(self,f,r,c,conditions=None,contradictions=None):
        return _review_qualification_scope(f,r,[c],'supported',conditions or [],contradictions or [],[])

    def test_historical_receipt_assessment_and_expected_notification_exclude_other_action(self):
        for statement in ['邮件收到，已开始初评。','预计下周告知评估结果。']:
            f,r,c=self.sample(statement)
            state,questions,audit,citations=self.run_review(f,r,c)
            self.assertEqual(state,'supported');self.assertTrue(audit['complete']);self.assertFalse(questions)
            self.assertTrue(citations)

    def test_cross_paragraph_approval_cannot_be_unconditional(self):
        f,r,c=self.sample('我司接受该报价。',app='applicable',effect='condition')
        state,questions,audit,_=self.run_review(f,r,c)
        self.assertEqual(state,'unresolved');self.assertFalse(audit['complete'])
        state,questions,audit,_=self.run_review(f,r,c,conditions=['接受报价须经内部审批后生效。'])
        self.assertEqual(state,'conditional');self.assertTrue(audit['complete'])

    def test_later_withdrawal_cannot_be_supported(self):
        statement='我司计划继续交付设备。'
        f,r,c=self.sample(statement,statement+'\n我司撤回该交付计划，取消交付。','contradiction','applicable')
        state,_,audit,_=self.run_review(f,r,c,contradictions=['我司撤回该交付计划，取消交付。'])
        self.assertEqual(state,'contradicted');self.assertTrue(audit['complete'])

    def test_missing_unresolved_and_conflicting_scope_fail_closed(self):
        f,r,c=self.sample()
        for mutation in ['missing','unresolved','effect']:
            changed=deepcopy(r)
            if mutation=='missing':changed['qualification_reviews']=[]
            elif mutation=='unresolved':changed['qualification_reviews'][0]['applicability']='unresolved'
            else:changed['qualification_reviews'][0]['effect']='condition'
            state,_,audit,_=self.run_review(f,changed,c)
            self.assertEqual(state,'unresolved');self.assertFalse(audit['complete'])

    def test_short_citation_cannot_hide_same_paragraph_condition(self):
        statement='我司接受该报价。'
        f,r,c=self.sample(statement,statement+'但须经内部审批后生效。')
        state,_,audit,_=self.run_review(f,r,c)
        self.assertEqual(state,'unresolved');self.assertFalse(audit['complete'])

    def test_scope_citations_remain_exact_complete_and_same_source(self):
        f,r,c=self.sample()
        for mutation in ['spliced','wrong_source','short']:
            changed=deepcopy(r);quote=changed['qualification_reviews'][0]['citations'][0]
            if mutation=='spliced':quote['quote']='接受报价……审批后生效。'
            elif mutation=='wrong_source':quote['chunk_id']='other'
            else:quote['quote']='内部审批后生效。';quote['char_start']=10+c['text'].index(quote['quote'])
            if mutation=='short':
                state,_,audit,_=self.run_review(f,changed,c)
                self.assertEqual(state,'unresolved');self.assertFalse(audit['complete'])
            else:
                with self.assertRaises(ValueError):self.run_review(f,changed,c)

    def test_original_wrong_actor_still_rejected_and_audit_binds_source(self):
        f,r,c=self.sample()
        original=deepcopy(f);original['actor']='other@test'
        with self.assertRaises(ValueError):validate_dossier({'findings':[original]},[c],['alice@test','other@test'],{'m'})
        audit=self.run_review(f,r,c)[2]
        changed={**c,'text':c['text']+'\n其他内容。'}
        changed_audit=self.run_review(f,r,changed)[2]
        self.assertNotEqual(audit['binding'],changed_audit['binding'])

    def test_full_review_slice_supplies_checks_and_persists_server_audit(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from services.mail.analysis import run_slice, VERSION
        import services.mail.analysis as module
        f,r,c=self.sample()
        f['citations'][0]['message_key']='m'
        cp={'phase':'review','review_index':0,'review_cursor':[0,0],'reviews':{}}
        spec={'package_id':'p','context_keys':['m'],'message_keys':['m'],'engine_version':VERSION,
              'manifest':{'metadata_fingerprint':'fp'},'mode':'dossier','parties':['alice@test']}
        repo=SimpleNamespace(get_turn=lambda *args:{'status':'running','task_spec':spec,'final':cp})
        r.update(finding_id='f',status='supported',rationale='仅独立历史表达',
                 conditions=[],contradictions=[],unresolved_questions=[],
                 citations=[{'chunk_id':'c','quote':f['statement'],'char_start':10}])
        calls=[];commits=[]
        def call(repo,tid,owner,spec,checkpoint,model,system,payload,required,cancelled):
            calls.append(payload)
            return {'reviews':[r]},checkpoint
        with patch.object(module,'manifest',return_value={'metadata_fingerprint':'fp'}), \
             patch.object(module,'_steps',return_value=[{'findings':[f]}]), \
             patch.object(module,'related_messages',return_value=['m']), \
             patch.object(module,'next_chunks',return_value=([c],[1,0])), \
             patch.object(module,'_call',side_effect=call), \
             patch.object(module,'_commit',side_effect=lambda *args,**kwargs:commits.append((args,kwargs))):
            run_slice(repo,'t','owner',None)
        self.assertEqual(calls[0]['qualification_checks'],_review_qualification_checks([c]))
        saved=commits[0][0][4]['reviews']['f']
        self.assertEqual(saved['status'],'supported')
        self.assertTrue(saved['qualification_scope_audits'][0]['complete'])

    def review_batch(self):
        body='青河项目预算人民币120000元。\n只有完成三个站点验收后才付款。\n该项目目前尚未付款。\n请陈禾确认采购安排。'
        _,_,chunk=self.sample(body=body)
        statements=['告知青河项目预算人民币120000元。','告知只有完成三个站点验收后才付款。',
                    '告知该项目目前尚未付款。','要求陈禾确认采购安排。']
        quotes=['青河项目预算人民币120000元。','只有完成三个站点验收后才付款。','该项目目前尚未付款。','请陈禾确认采购安排。']
        rows=[]
        for index,(statement,quote) in enumerate(zip(statements,quotes)):
            rows.append({'actor':'alice@test','kind':'action','intent':'request' if index==3 else 'inform',
                         'issue':'项目采购与付款','statement':statement,'conditions':['完成三个站点验收'] if index==1 else [],
                         'contradictions':[],'unresolved_questions':[],'uncertainty':'仅为邮件表达',
                         'citations':[{'chunk_id':'c','quote':quote,'char_start':10+body.index(quote)}]})
        findings=validate_dossier({'findings':rows},[chunk],['alice@test'],{'m'})
        checks=_review_qualification_checks([chunk]);reviews=[]
        for index,finding in enumerate(findings):
            finding['finding_id']='f%s'%index
            reviews.append({'finding_id':finding['finding_id'],'status':'conditional' if index==1 else 'supported',
                            'rationale':'仅按可回查原文核对本项表达。','conditions':['只有完成三个站点验收后才付款。'] if index==1 else [],
                            'contradictions':[],'unresolved_questions':[],
                            'citations':[{k:q[k] for k in ('chunk_id','quote','char_start')} for q in finding['citations']],
                            'qualification_reviews':[{'check_id':q['check_id'],
                                'applicability':'applicable' if index==1 and '只有' in q['quote'] else 'not_applicable',
                                'effect':'condition' if index==1 and '只有' in q['quote'] else 'none',
                                'rationale':'该条件限制付款安排。' if index==1 and '只有' in q['quote'] else '本项是原文独立表达，未将另一事项条件认定为已满足。'} for q in checks]})
        return findings,reviews,chunk

    def run_review_batch(self,findings,reviews,chunk,previous=None,finish=False):
        from types import SimpleNamespace
        from unittest.mock import patch
        import services.mail.analysis as module
        cp={'phase':'review','review_index':0,'review_cursor':[0,0],'reviews':deepcopy(previous or {}),
            'model_calls':1,'summary_index':0,'issues':[],'characters_read':len(chunk['text']),'findings_count':4}
        spec={'package_id':'p','context_keys':['m'],'message_keys':['m'],'engine_version':module.VERSION,
              'manifest':{'metadata_fingerprint':'fp','fingerprint':'fp','missing_body':0},
              'mode':'message_dossier','parties':['alice@test'],'objective':'说明采购诉求、付款条件和当前付款状态',
              'phase2_enabled':False}
        state={'status':'running','task_spec':spec,'final':cp};events=[];commits=[];steps={}
        repo=SimpleNamespace(get_turn=lambda *args:state,event=lambda *args:events.append(args),
                             coverage=lambda *args:{'attachments_without_text':0})
        def read_steps(repo,tid,tool,offset,limit):
            if tool=='dossier_findings':return [{'findings':findings}] if offset==0 else []
            return steps.get(tool,[])[offset:offset+limit]
        def next_chunks(*args,**kwargs):return ([chunk],[1,0]) if args[4]==[0,0] else ([],[1,0])
        def call(repo,tid,owner,spec,checkpoint,model,system,payload,required,cancelled):
            value={'reviews':reviews} if required==('reviews',) else {'issues':[{
                'title':'采购诉求与付款情况','statement':'青河项目预算为人民币120000元，只有完成三个站点验收后才付款，目前尚未付款，并请求确认采购安排。',
                'uncertainty':'辅助核查存在未完成项，结论仅归纳邮件原文。','finding_ids':[f['finding_id'] for f in findings]}]}
            return value,{**checkpoint,'model_calls':checkpoint['model_calls']+1}
        def commit(repo,tid,owner,before,after,**kwargs):
            commits.append((after,kwargs));state['final']=after
            if kwargs.get('tool'):steps.setdefault(kwargs['tool'],[]).append(kwargs['value'])
            if kwargs.get('complete'):state['status']='completed';state['sufficiency']=kwargs.get('sufficiency')
        with patch.object(module,'manifest',return_value=spec['manifest']), \
             patch.object(module,'_steps',side_effect=read_steps), \
             patch.object(module,'related_messages',return_value=['m']), \
             patch.object(module,'next_chunks',side_effect=next_chunks), \
             patch.object(module,'_call',side_effect=call), \
             patch.object(module,'_commit',side_effect=commit):
            for _ in range(8 if finish else 1):
                module.run_slice(repo,'t','owner',None)
                if state['status']=='completed':break
        return state,events,commits,steps

    def test_bad_main_or_qualification_citation_rejects_only_auxiliary_row(self):
        for field in ['citations','qualification_reviews']:
            findings,reviews,chunk=self.review_batch();before=deepcopy(findings);bad=reviews[2]
            bad.update(status='contradicted',conditions=['付款条件已全部满足'],contradictions=['120001元已经支付'])
            invalid={'chunk_id':'c','quote':'原文不存在的审核引用','char_start':10}
            if field=='citations':bad['citations']=[invalid]
            else:bad['qualification_reviews'][0]['citations']=[invalid]
            state,events,_,steps=self.run_review_batch(findings,reviews,chunk)
            saved=state['final'];self.assertEqual(saved['review_cursor'],[1,0]);self.assertEqual(findings,before)
            self.assertEqual(saved['reviews']['f2']['status'],'unresolved')
            for name in ['citations','conditions','contradictions']:self.assertEqual(saved['reviews']['f2'][name],[])
            self.assertFalse(saved['reviews']['f2']['qualification_scope_audits'][0]['complete'])
            self.assertEqual(saved['reviews']['f0']['status'],'supported')
            self.assertEqual(saved['reviews']['f1']['status'],'conditional')
            self.assertEqual(saved['reviews']['f3']['status'],'supported')
            self.assertEqual(saved['rejected_review_rows'],1)
            self.assertEqual(events[0][1],'dossier_review_row_rejected');self.assertEqual(events[0][2]['field'],field)
            output=steps['dossier_review'][0]
            self.assertEqual(len(output['reviews']),3)
            self.assertTrue(all(q['quote']!='原文不存在的审核引用' for q in output['checked_citations']))
            self.assertEqual(before[1]['conditions'],['完成三个站点验收'])
            self.assertIn('120000',before[0]['statement']);self.assertIn('尚未付款',before[2]['statement'])

    def test_rejected_row_preserves_previous_checked_conditions_counterevidence_and_rank(self):
        findings,reviews,chunk=self.review_batch();reviews[2]['citations']=[{'chunk_id':'wrong','quote':'伪造审核依据','char_start':0}]
        previous={'f2':{'status':'contradicted','conditions':['此前已核实的条件'],
                        'contradictions':['此前已核实的反证'],'unresolved_questions':[],
                        'citations':[deepcopy(findings[2]['citations'][0])]}}
        state,_,_,_=self.run_review_batch(findings,reviews,chunk,previous)
        saved=state['final']['reviews']['f2']
        self.assertEqual(saved['status'],'contradicted');self.assertEqual(saved['conditions'],previous['f2']['conditions'])
        self.assertEqual(saved['contradictions'],previous['f2']['contradictions'])
        self.assertEqual(saved['citations'],previous['f2']['citations'])
        self.assertTrue(saved['unresolved_questions']);self.assertFalse(saved['qualification_scope_audits'][-1]['complete'])

    def test_rejected_review_can_complete_partial_chinese_summary_with_original_facts(self):
        findings,reviews,chunk=self.review_batch();reviews[2]['citations']=[{'chunk_id':'c','quote':'目前尚未付款','char_start':10+chunk['text'].index('目前尚未付款')}]
        state,_,_,steps=self.run_review_batch(findings,reviews,chunk,finish=True)
        self.assertEqual(state['status'],'completed');self.assertEqual(state['sufficiency'],'partial')
        final=state['final'];self.assertTrue(final['complete']);self.assertEqual(final['rejected_review_rows'],1)
        for text in ['120000','只有完成三个站点验收后才付款','尚未付款','确认采购安排']:
            self.assertIn(text,final['answer'])
        self.assertTrue(any('辅助核查未通过原文校验' in note for note in final['limitations']))
        self.assertEqual(len(steps['dossier_verified'][0]['findings']),4)

    def test_review_cancellation_and_storage_errors_are_not_auxiliary_data_rejections(self):
        from unittest.mock import patch
        import services.mail.analysis as module
        from services.mail.research.runtime import JobCancelled
        findings,reviews,chunk=self.review_batch()
        for error in [JobCancelled('已取消'),OSError('存储不可用')]:
            with patch.object(module,'_citations',side_effect=error):
                with self.assertRaises(type(error)):self.run_review_batch(findings,reviews,chunk)

    def test_summary_omitting_rejected_finding_cannot_make_turn_sufficient(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import services.mail.analysis as module
        findings,_,_=self.review_batch()
        spec={'package_id':'p','context_keys':['m'],'message_keys':['m'],'parties':['alice@test'],
              'manifest':{'fingerprint':'fp','missing_body':0},'phase2_enabled':False}
        repo=SimpleNamespace(coverage=lambda *args:{'attachments_without_text':0})
        for rejected,expected in [(0,'sufficient'),(1,'partial')]:
            cp={'issues':[{'title':'预算','statement':findings[0]['statement'],
                           'finding_ids':['f0'],'review_status':'supported'}],
                'rejected_review_rows':rejected,'semantic_unverified_findings':0}
            with patch.object(module,'manifest',return_value=spec['manifest']),patch.object(module,'_commit') as commit:
                result=module._finish_dossier(repo,'t','owner',spec,cp)
            self.assertEqual(result['sufficiency'],expected)
            self.assertEqual(commit.call_args[1]['sufficiency'],expected)
            self.assertIn('120000',commit.call_args[0][4]['answer'])

    def test_attachment_only_overlimit_feedback_preserves_strict_limits(self):
        f,r,c=self.sample()
        attachment={**c,'source_kind':'attachment','body_fragments':[]}
        f['citations']=[deepcopy(f['citations'][0]) for _ in range(11)]
        with self.assertRaisesRegex(ValueError,'citations必须为1-6条.*11条'):
            validate_dossier({'findings':[f]},[attachment],['alice@test'],{'m'})
        f['citations']=f['citations'][:1]
        with self.assertRaisesRegex(ValueError,'仅有附件引用.*主|仅有附件引用.*作者正文'):
            validate_dossier({'findings':[f]},[attachment],['alice@test'],{'m'})
        f['statement']='长'*1201
        with self.assertRaisesRegex(ValueError,'statement必须为1-1200字符'):
            validate_dossier({'findings':[f]},[c],['alice@test'],{'m'})

    def test_compact_check_id_backfills_exact_paragraph_and_bad_optional_citation_rejected(self):
        f,r,c=self.sample()
        r['qualification_reviews'][0].pop('citations')
        state,_,audit,citations=self.run_review(f,r,c)
        self.assertEqual(state,'supported');self.assertTrue(audit['complete'])
        check=_review_qualification_checks([c])[0]
        self.assertEqual(citations[0]['quote'],check['quote'])
        self.assertEqual(citations[0]['char_start'],check['char_start'])
        for mutation in ['unknown','duplicate','empty_optional']:
            changed=deepcopy(r)
            if mutation=='unknown':changed['qualification_reviews'].append({**changed['qualification_reviews'][0],'check_id':'unknown'})
            elif mutation=='duplicate':changed['qualification_reviews']*=2
            else:changed['qualification_reviews'][0]['citations']=[]
            state,_,audit,_=self.run_review(f,changed,c)
            self.assertEqual(state,'unresolved');self.assertFalse(audit['complete'])
        r['qualification_reviews'][0]['citations']=[{'chunk_id':'c','quote':'篡改引用','char_start':10}]
        with self.assertRaises(ValueError):self.run_review(f,r,c)

    def test_dynamic_review_capacity_reserved_and_dispatched_identically(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import services.mail.analysis as module
        f,r,c=self.sample()
        checks=_review_qualification_checks([c])*15
        payload={'findings':[f]*8,'qualification_checks':checks}
        expected=module._review_output_tokens(payload)
        self.assertGreater(expected,3600);self.assertLessEqual(expected,32768)
        commits=[];model=SimpleNamespace(chat_json=lambda *args,**kwargs: {'json':{'reviews':[]},'usage':{'total_tokens':50}})
        observed=[]
        def chat(*args,**kwargs):observed.append(kwargs);return {'json':{'reviews':[]},'usage':{'total_tokens':50}}
        model.chat_json=chat
        repo=SimpleNamespace(get_turn=lambda *args:{'status':'running'})
        cp={'model_calls':0}
        with patch.object(module,'_commit',side_effect=lambda *args,**kwargs:commits.append(args)):
            module._call(repo,'t','owner',{'model_call_budget':2},cp,model,module.REVIEW_SYSTEM,payload,('reviews',),lambda:False)
        reserved=commits[0][4]['pending_token_reservation']
        self.assertEqual(reserved['output_reserved'],expected)
        self.assertEqual(observed[0]['max_tokens'],expected)
        with patch.object(module,'_commit') as commit:
            observed.clear()
            value,checkpoint=module._call(repo,'t','owner',{'model_call_budget':2,'max_tokens':1},cp,model,module.REVIEW_SYSTEM,payload,('reviews',),lambda:False)
            self.assertIsNone(value);self.assertFalse(observed);self.assertTrue(checkpoint['token_budget_exhausted'])
        excessive={'findings':[f]*12,'qualification_checks':checks*20}
        with patch.object(module,'_commit') as commit:
            with self.assertRaisesRegex(ValueError,'32768'):
                module._call(repo,'t','owner',{'model_call_budget':2},cp,model,module.REVIEW_SYSTEM,excessive,('reviews',),lambda:False)
            commit.assert_not_called()
