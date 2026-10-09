"""Completed factual reports can be repaired without replaying their scan."""
from copy import deepcopy
from contextlib import nullcontext
import json
from unittest.mock import patch

import pytest

from services.mail.person_activity import (apply_activity_amendment,activity_record_sha256,
    assemble_report,_saved_findings,_normalize_fact_claims,validate_batch,run_activity_slice,source_report_basis,validate_deep_basis)
from services.mail.person_corpus import next_chunks,selection_for_spec
from test_person_context_upgrade import person_data,attachment_text
from test_person_activity_v3 import make_turn,Model

REAL_PLAN="蓝湾项目  智能仓储分拣系统  交付方案 v2.0\n委托方：蓝湾实业集团  蓝湾项目组\n承建方：晟远智能装备有限公司\n版本：v2.0    定稿日期：2026 年 9 月 20 日\n第一章  项目范围\n1.1 分拣线 4 条（含输送段、扫码识别段、分拣执行段、收集段）。\n1.2 WMS 接口联调：与蓝湾实业现有 WMS 系统对接，覆盖入库、移库、出库三类单据。\n1.3 现场培训 2 场，每场 4 学时。\n第二章  责任人\n承建方交付负责人：李强（晟远智能装备有限公司）\n委托方项目经理：张伟（蓝湾实业集团）\n第三章  计划交付日期\n双方约定计划交付日期为：2026 年 10 月 15 日。\n（注：后续因 PLC 供货延迟提出延期申请，该日期经双方书面往来调整，以双方最终确认的《交付确认单》为准。）\n第四章  验收标准（本章即验收标准章节）\n4.1 分拣线连续运行 72 小时无故障停机。\n4.2 分拣准确率不低于 99.5%，误分率不高于 0.3%。\n4.3 WMS 接口联调测试全部用例通过，单据同步延迟不超过 2 秒。\n4.4 现场培训完成，双方签署《培训记录表》。\n4.5 交付物清单齐全：设备本体、备件包、操作手册、接口文档、测试报告。\n第五章  验收流程\n第一步  到货签收：委托方现场清点数量、核对装箱单，签署《到货签收单》。\n第二步  开箱检查：检查外观、铭牌、随机附件，记录开箱结果。\n第三步  安装调试：承建方完成机械与电气安装、单机调试。\n第四步  联调测试：完成 WMS 接口联调，提交测试报告。\n第五步  连续运行：按第 4.1 条进行 72 小时连续运行考核。\n第六步  签署《验收报告》：双方签字盖章，至此交付完成。\n第六章  交付完成的判定\n6.1 设备发运、物流签收均不构成交付完成。\n6.2 只有双方签署《验收报告》之日，方视为交付完成日。\n6.3 承建方单方面声明的“已交付”“已发货”不产生交付完成的法律效果。\n第七章  违约金\n承建方逾期交付的，每逾期一日按合同总价 0.5% 计付违约金。\n承建方：晟远智能装备有限公司（盖章）   委托方：蓝湾实业集团（盖章）"
REAL_ACCEPTANCE_MAIL="张经理：\n\n来函收悉。我司内部已紧急评估，现答复如下。\n\n一、我司接受贵方要求\n1. 蓝湾项目最终交付日期确定为：2026 年 10 月 18 日；\n2. WMS 接口联调于 2026 年 10 月 17 日前完成并提交测试报告；\n3. 原计划日期 10 月 15 日、我司申请日期 10 月 20 日，均不再采用。\n\n二、书面确认件\n本确认已加盖我司公章，扫描件见附件《蓝湾项目交付确认单-盖章扫描件.pdf》，纸质原件今日以顺丰寄出（单号 SF-1055-8877-3201），请贵司查收。\n\n三、我司承诺\n1. 按 2026 年 10 月 18 日完成交付；\n2. 交付前提交分拣线通电调试完成证明与 WMS 联调测试报告；\n3. 若再次逾期，我司接受按合同第七条约定的违约金计算方式承担责任。\n\n双方最终确认的交付日期为：2026 年 10 月 18 日。\n\n李强\n晟远智能装备有限公司 项目交付部"
REAL_REJECTION_MAIL="李工：\n\n就贵司 10 月 18 日所称的“完成交付”，我司正式答复如下。\n\n一、事实情况\n1. 截至 2026 年 10 月 21 日 09:00，我司蓝湾项目现场尚未收到贵司发出的任何设备；\n2. 物流单号 SF-8823-1097-4421 查询结果显示仅“已揽收”，无后续流转记录，承运方客服亦未能给出货物实际位置；\n3. 我司未签署《到货签收单》，未开箱，未进行安装调试。\n\n二、我方对“交付”的认定\n贵司所称“完成交付”不成立。按照《蓝湾项目交付方案-v2.docx》第六章约定：\n1. 设备发运、物流签收均不构成交付完成；\n2. 只有双方签署《验收报告》之日方视为交付完成日；\n3. 贵司单方面声明的“已交付”“已发货”不产生交付完成的效果。\n\n三、我方要求\n1. 请贵司于 2026 年 10 月 22 日 12:00 前书面提供货物实际位置、预计到达时间及承运方联系人；\n2. 到货后按交付方案第四章、第五章组织验收，验收合格并签署《验收报告》后方视为交付完成；\n3. 因 2026 年 10 月 18 日未能完成交付，我方保留按合同第七条追究逾期违约责任的权利。\n\n截至目前，蓝湾项目状态为：未交付、未验收。\n\n张伟\n蓝湾实业集团 蓝湾项目组 项目经理"


def exact_ref(source,start=None,end=None):
    start=source['char_start'] if start is None else start
    end=source['char_end'] if end is None else end
    return {'segment_id':source['segment_id'],'char_start':start,'char_end':end,
        'quote':source['text'][start-source['char_start']:end-source['char_start']]}


@pytest.fixture
def legacy(person_data):
    repo,pid,add=person_data
    first=add(REAL_ACCEPTANCE_MAIL,sender='li.qiang@shengyuan-zn.cn',recipient='zhang.wei@lanwan-group.com',cc='alice@example.com',attachment=True)
    second=add(REAL_REJECTION_MAIL,sender='zhang.wei@lanwan-group.com',recipient='li.qiang@shengyuan-zn.cn',cc='alice@example.com')
    attachment_text(repo,pid,first,REAL_PLAN)
    tid=make_turn(repo,pid);turn=repo.get_turn(tid,'owner');spec=turn['task_spec'];selected=selection_for_spec(repo,spec,'owner')
    chunks,cursor=next_chunks(repo,pid,'owner',selected,[0,0],50000,full_package=True)
    body1=next(c for c in chunks if c['message_key']==first and c['source_kind']=='body')
    body2=next(c for c in chunks if c['message_key']==second and c['source_kind']=='body')
    plan=next(c for c in chunks if c['source_kind']=='attachment')
    bad=['陈丽（alice@example.com）作为抄送人收到李强发送的承诺。',
        '收到蓝湾集团张巍发送给李强并抄送陈李的邮件，否认盛源公司所称完成交付。',
        '邮件正式答复人物方10月18日所称的完成交付不成立，要求人物方提供货物实际位置。']
    rows=[{'kind':'received_information','statement':text,'citations':[{
        'chunk_id':source['chunk_id'],'char_start':source['char_start'],'quote':source['text']}]} for text,source in zip(bad,[body1,body2,body2])]
    findings=validate_batch({'scanned_chunk_ids':[c['chunk_id'] for c in chunks],'findings':rows},chunks,selected,'activity_report')
    for i,f in enumerate(findings):f['finding_id']='legacy:'+str(i)
    with repo._connection() as c:
        for i,values in enumerate([findings,[],[]]):
            c.execute('INSERT INTO mail_research_steps(step_id,turn_id,sequence,tool_name,input_json,output_ref,status,started_at,finished_at) VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)',
                ('legacy_step_'+str(i),tid,i+1,'person_activity_batch','{}',json.dumps({'findings':values},ensure_ascii=False),'completed'))
    checkpoint={'analysis_version':3,'execution_engine':spec['execution_engine'],'purpose':'activity_report','phase':'completed',
        'cursor':cursor,'characters_read':spec['corpus']['characters'],'batches':3,'model_calls':6,
        'token_usage':{'input_tokens':123,'output_tokens':456,'total_tokens':579},'complete':True,'scan_complete':True,'findings_count':3}
    checkpoint['report']=assemble_report(repo,tid,'owner',spec,selected,checkpoint)
    with repo._connection() as c:c.execute('UPDATE mail_research_turns SET status=?,final_json=? WHERE turn_id=?',('completed',json.dumps(checkpoint,ensure_ascii=False),tid))
    replacements={f['finding_id']:{'kind':'received_information','citations':[exact_ref(source)]} for f,source in zip(findings,[body1,body2,body2])}
    kwargs={'amendment_id':'quality_v1','expected_checkpoint_sha256':activity_record_sha256(checkpoint),
        'expected_finding_sha256':{f['finding_id']:activity_record_sha256(f) for f in findings},
        'source_sha256':{c['segment_id']:c['text_sha256'] for c in [body1,body2,plan]},
        'replacements':replacements,'additions':[{'kind':'source_information','citations':[exact_ref(plan,0,500)]},
            {'kind':'source_information','citations':[exact_ref(plan,500,905)]}],
        'reason':'补回真实905字蓝湾验收方案，纠正中文姓名、公司名称和仅抄送人物的义务归属。'}
    return repo,tid,checkpoint,findings,kwargs,plan


def step_snapshot(repo,tid):
    with repo._connection() as c:return [tuple(row) for row in c.execute('SELECT * FROM mail_research_steps WHERE turn_id=? ORDER BY sequence',(tid,))]


def test_real_source_amendment_preserves_history_and_changes_canonical_report(legacy):
    repo,tid,before,old,kwargs,plan=legacy;snapshot=step_snapshot(repo,tid)
    before_basis,before_digest=source_report_basis(repo,repo.get_turn(tid,'owner'))
    deep_tid=make_turn(repo,repo.get_turn(tid,'owner')['task_spec']['package_id'],mode='deep_analysis',source=tid)
    deep_spec=repo.get_turn(deep_tid,'owner')['task_spec']
    assert deep_spec['source_report_evidence_hash']==before_digest
    result=apply_activity_amendment(repo,tid,'owner',**kwargs)
    after=repo.get_turn(tid,'owner')['final'];canonical=_saved_findings(repo,tid)
    assert not result['already_applied'] and len(canonical)==5
    assert step_snapshot(repo,tid)[:3]==snapshot
    for key in ('cursor','characters_read','batches','model_calls','token_usage','complete','scan_complete'):
        assert after[key]==before[key]
    assert after['findings_count']==5 and after['report']==result['report']
    with repo._connection() as c:
        output=json.loads(c.execute("SELECT output_ref FROM mail_research_steps WHERE turn_id=? AND tool_name='person_activity_correction'",(tid,)).fetchone()[0])
    assert output['prior_checkpoint']==before and output['prior_report_sha256']==activity_record_sha256(before['report'])
    assert len(output['supersedes'])==3
    body=result['report']['markdown'].split('## 附录：原文支撑')[0]
    assert all(f['statement'] in body for f in canonical)
    for wrong in ('陈丽','张巍','陈李','盛源公司','人物方'):assert wrong not in body
    for required in ('72','99.5%','0.3%','2 秒','4 学时','到货签收','开箱检查','安装调试','联调测试','连续运行','0.5%'):
        assert required in result['report']['markdown']
    assert 'li.qiang@shengyuan-zn.cn' in body and 'zhang.wei@lanwan-group.com' in body
    assert '选定邮箱仅收到抄送' in body
    material=[f for f in canonical if f['kind']=='source_information']
    assert ''.join(f['citations'][0]['quote'] for f in material)==REAL_PLAN
    assert all(f['citations'][0]['selected_roles']==['cc'] for f in material)
    turn=repo.get_turn(tid,'owner');basis,digest=source_report_basis(repo,turn)
    assert before_basis==old and basis==canonical and digest!=before_digest
    with pytest.raises(ValueError,match='事实报告已变化'):validate_deep_basis(repo,deep_spec,'owner')
    again=apply_activity_amendment(repo,tid,'owner',**kwargs)
    assert again['already_applied'] and len(step_snapshot(repo,tid))==4


@pytest.mark.parametrize('damage',['checkpoint','finding','source_hash','quote','scope','actor','id_collision'])
def test_stale_or_invalid_amendment_never_mutates_report(legacy,damage):
    repo,tid,before,old,kwargs,plan=legacy;kwargs=deepcopy(kwargs)
    if damage=='checkpoint':kwargs['expected_checkpoint_sha256']='0'*64
    elif damage=='finding':kwargs['expected_finding_sha256']['legacy:0']='0'*64
    elif damage=='source_hash':kwargs['source_sha256'][plan['segment_id']]='0'*64
    elif damage=='quote':kwargs['additions'][0]['citations'][0]['quote']='错'+kwargs['additions'][0]['citations'][0]['quote'][1:]
    elif damage=='scope':kwargs['replacements']['legacy:0']['citations']=kwargs['additions'][0]['citations']
    elif damage=='actor':kwargs['additions'][0]['kind']='action_statement'
    else:
        apply_activity_amendment(repo,tid,'owner',**kwargs);before=repo.get_turn(tid,'owner')['final'];kwargs['reason']='不同请求不得复用编号'
    snapshot=step_snapshot(repo,tid)
    with pytest.raises((ValueError,RuntimeError)):apply_activity_amendment(repo,tid,'owner',**kwargs)
    assert repo.get_turn(tid,'owner')['final']==before and step_snapshot(repo,tid)==snapshot


def test_amendment_cannot_access_another_owner(legacy):
    repo,tid,before,old,kwargs,plan=legacy
    with pytest.raises(LookupError):apply_activity_amendment(repo,tid,'other',**kwargs)
    assert repo.get_turn(tid,'owner')['final']==before


def test_source_change_while_preparing_amendment_is_rechecked_in_transaction(legacy):
    repo,tid,before,old,kwargs,plan=legacy;snapshot=step_snapshot(repo,tid)
    from services.mail import person_activity as module
    original=module.next_chunks;calls=[0]
    def race(*args,**options):
        result=original(*args,**options);calls[0]+=1
        if calls[0]==4:
            with repo._connection() as c:c.execute('UPDATE mail_text_segments SET text=? WHERE segment_id=?',('changed',plan['segment_id']))
        return result
    with patch.object(module,'next_chunks',race),pytest.raises((ValueError,RuntimeError)):
        apply_activity_amendment(repo,tid,'owner',**kwargs)
    assert repo.get_turn(tid,'owner')['final']==before and step_snapshot(repo,tid)==snapshot


def test_generalized_identity_guard_uses_original_speakers_not_guessed_names(legacy):
    repo,tid,before,findings,kwargs,plan=legacy
    corrected=_normalize_fact_claims(findings)
    assert all(f.get('identity_guard') for f in corrected)
    assert all(f['source_bound_record'] for f in corrected)
    assert '陈丽' not in corrected[0]['statement'] and '张巍' not in corrected[1]['statement']
    assert '人物方' not in corrected[2]['statement']
    assert '选定邮箱仅收到抄送' in corrected[2]['statement']
    assert corrected[2]['citations']==findings[2]['citations']


def test_empty_reader_and_checker_cannot_lose_real_905_character_attachment(person_data):
    repo,pid,add=person_data
    key=add('交付方案见附件。',sender='li.qiang@shengyuan-zn.cn',recipient='zhang.wei@lanwan-group.com',cc='alice@example.com',attachment=True)
    attachment_text(repo,pid,key,REAL_PLAN);tid=make_turn(repo,pid)
    class Empty(Model):
        def chat_json(self,system,prompt,**kwargs):
            p=json.loads(prompt);self.payloads.append(p)
            return {'json':{'scanned_scan_ids':p['scan_manifest'],'findings':[]}}
    model=Empty()
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        for _ in range(12):
            result=run_activity_slice(repo,tid,'owner',model,char_budget=500)
            if not result.get('_requeue_slice'):break
        else:raise AssertionError('empty model fixture did not complete')
    facts=_saved_findings(repo,tid);material=[f for f in facts if any(q['source_kind']=='attachment' for q in f['citations'])]
    quotes=[q for f in material for q in f['citations'] if q['source_kind']=='attachment']
    quotes.sort(key=lambda q:q['char_start'])
    assert ''.join(q['quote'] for q in quotes)==REAL_PLAN
    assert all(f['kind']=='source_information' and f['completion']=='not_established' for f in material)
    assert '99.5%' in repo.get_turn(tid,'owner')['final']['report']['markdown']


def test_partial_summary_with_complete_citation_retains_every_original_detail(legacy):
    repo,tid,before,findings,kwargs,plan=legacy
    from services.mail.person_activity import _preserve_new_spans,bind_span_kinds
    from services.mail.person_research import citation_span_payload
    selected=selection_for_spec(repo,repo.get_turn(tid,'owner')['task_spec'],'owner')
    spans=bind_span_kinds(citation_span_payload([plan]),[plan],selected,'activity_report')
    row={'kind':'source_information','statement':'附件列明计划交付日期。','citations':[{'evidence_id':s['evidence_id']} for s in spans]}
    partial=validate_batch({'scanned_chunk_ids':[plan['chunk_id']],'findings':[row]},[plan],selected,'activity_report')
    complete,ids=_preserve_new_spans(partial,[plan],selected,spans)
    assert ids==[] and len(complete)==1
    assert ''.join(d['quote'] for d in complete[0]['source_details'])==REAL_PLAN
    assert '99.5%' not in complete[0]['statement'] and '99.5%' in complete[0]['source_details'][0]['quote']
