"""Old events cannot borrow printed footer fields as substantive new evidence."""
from copy import deepcopy
import json
from contextlib import nullcontext
from unittest.mock import patch

import pytest

from services.mail.person_activity import validate_batch,bind_span_kinds,_preserve_new_spans,_normalize_fact_claims
from services.mail.person_research import citation_span_payload
from test_person_activity_identity import chunk,SELECTED
from test_person_context_upgrade import person_data,attachment_text
from test_person_activity_v3 import make_turn,Model
from services.mail.person_activity import run_activity_slice,_saved_findings,_non_event_tail

REAL_TAIL='                           经办人：王锐\n日期：2026 年 9 月    日                   日期：2026 年 9 月    日'




# Exact source and both recorded responses from the 932+89 production window.
REAL_CONTEXT=(
    "海星项目  设备采购合同（条款清单）\n"
    "合同编号：SY-CG-2026-0918-HX\n"
    "甲方（采购方）：晟远智能装备有限公司\n"
    "乙方（供货方）：拓峰精密设备有限公司\n"
    "签署地点：上海市浦东新区\n"
    "清单出具日期：2026 年 9 月 18 日\n"
    "第一条  合同标的\n"
    "高精度影像测量仪，规格型号 TF-VMM860，数量 2 台，产地：中国深圳。\n"
    "第二条  合同总价\n"
    "人民币 180,000.00 元（大写：壹拾捌万元整）。该价格为海星项目专项价格，含 13% 增值税专用发票、含运输费、含现场安装调试费，不含现场土建改造费用。\n"
    "第三条  价格形成依据\n"
    "本合同价格经双方议价确认：乙方首次报价 100,000.00 元/台；经协商乙方让利至 95,000.00 元/台；再经协商，乙方最终下调至 90,000.00 元/台。甲方于 2026 年 9 月 15 日原则同意该价格，并以 2026 年 9 月 17 日财务审批通过为生效条件。\n"
    "第四条  交货期\n"
    "合同生效后 35 个自然日。按本合同于 2026 年 9 月 22 日生效计算，交货日期为 2026 年 10 月 27 日，满足甲方提出的 2026 年 10 月 30 日前到货要求。\n"
    "第五条  付款方式\n"
    "1. 预付款 30%，即人民币 54,000.00 元，合同生效后 5 个工作日内支付；2. 到货款 60%，即人民币 108,000.00 元，到货验收合格后 15 个工作日内支付；3. 质保金 10%，即人民币 18,000.00 元，验收合格满 12 个月且无质量问题后支付。\n"
    "第六条  验收标准\n"
    "按《TF-VMM860 技术协议》第三章执行，含精度校验、重复性测试、软件功能测试三项。\n"
    "第七条  违约责任\n"
    "乙方逾期交付的，每逾期一日按合同总价的 0.5% 计付违约金，累计不超过合同总价的 10%。\n"
    "第八条  变更与补充\n"
    "任何对数量、价格、交期的变更均须以书面（含电子邮件）形式确认，并作为本合同附件，与本合同具有同等效力。\n"
    "第九条  生效条件\n"
    "本合同经双方签字盖章，且甲方财务审批通过后生效。\n"
    "甲方：晟远智能装备有限公司（盖章）          乙方：拓峰精密设备有限公司（盖章）\n"
    "经办人：陈立   "
)
REAL_SELECTED={'addresses':['chen.li@shengyuan-zn.cn'],'entity_id':None}
REAL_RESPONSES=[
    {
        "scanned_scan_ids": [
            "c1"
        ],
        "findings": [
            {
                "statement": "收到海星项目高精度影像测量仪采购合同条款清单（合同编号：SY-CG-2026-0918-HX），其中列明甲方为晟远智能装备有限公司，乙方为拓峰精密设备有限公司，经办人处列有陈立及王锐。清单包含具体条款：标的为2台TF-VMM860，总价180,000.00元（单价90,000.00元，含13%税、运费及安装调试费，不含土建改造费）；价格形成过程记录为乙方从100,000.00元/台降至95,000.00元/台再降至90,000.00元/台，且注明甲方于2026年9月15日原则同意该价格，以2026年9月17日财务审批通过为生效条件；交货期为合同生效后35个自然日（按2026年9月22日生效计算为2026年10月27日）；付款方式为30%预付款（54,000.00元，生效后5个工作日内）、60%到货款（108,000.00元，验收合格后15个工作日内）、10%质保金（18,000.00元，验收合格满12个月且无质量问题后）；验收标准按《TF-VMM860 技术协议》第三章；违约责任为逾期每日0.5%违约金，累计不超过10%；生效条件为双方签字盖章且甲方财务审批通过。清单中甲方盖章处经办人列为陈立，乙方盖章处经办人列为王锐，日期栏为空白。",
                "conditions": [
                    "该文件为合同条款清单，列明生效条件为双方签字盖章且甲方财务审批通过",
                    "清单中日期栏未填写具体日期",
                    "清单注明甲方于2026年9月15日原则同意价格，以2026年9月17日财务审批通过为生效条件"
                ],
                "unresolved_questions": [],
                "completion": "not_established",
                "kind": "source_information",
                "citations": [
                    {
                        "evidence_id": "q1"
                    },
                    {
                        "evidence_id": "q2"
                    },
                    {
                        "evidence_id": "q3"
                    }
                ]
            }
        ]
    },
    {
        "scanned_scan_ids": [
            "c1"
        ],
        "findings": [
            {
                "statement": "收到海星项目高精度影像测量仪采购合同条款清单（合同编号：SY-CG-2026-0918-HX），文件列明甲方为晟远智能装备有限公司，乙方为拓峰精密设备有限公司。清单中甲方盖章处经办人列为陈立，乙方盖章处经办人列为王锐，日期栏均为空白。清单包含具体条款：标的为2台TF-VMM860高精度影像测量仪，产地中国深圳；总价180,000.00元（单价90,000.00元，含13%增值税专用发票、运输费及现场安装调试费，不含现场土建改造费）；价格形成过程记录为乙方从100,000.00元/台降至95,000.00元/台再降至90,000.00元/台，且注明甲方于2026年9月15日原则同意该价格，以2026年9月17日财务审批通过为生效条件；交货期为合同生效后35个自然日（按2026年9月22日生效计算为2026年10月27日，满足2026年10月30日前到货要求）；付款方式为30%预付款（54,000.00元，生效后5个工作日内支付）、60%到货款（108,000.00元，到货验收合格后15个工作日内支付）、10%质保金（18,000.00元，验收合格满12个月且无质量问题后支付）；验收标准按《TF-VMM860 技术协议》第三章执行（含精度校验、重复性测试、软件功能测试）；违约责任为乙方逾期交付每逾期一日按合同总价0.5%计付违约金，累计不超过合同总价10%；变更与补充须以书面形式确认；生效条件为双方签字盖章且甲方财务审批通过。",
                "conditions": [
                    "该文件为合同条款清单，列明生效条件为双方签字盖章且甲方财务审批通过",
                    "清单中日期栏未填写具体日期",
                    "清单注明甲方于2026年9月15日原则同意价格，以2026年9月17日财务审批通过为生效条件"
                ],
                "unresolved_questions": [],
                "completion": "not_established",
                "kind": "source_information",
                "citations": [
                    {
                        "evidence_id": "q1"
                    },
                    {
                        "evidence_id": "q2"
                    },
                    {
                        "evidence_id": "q3"
                    }
                ]
            }
        ]
    }
]


def real_windows(primary_text=REAL_TAIL):
    old=chunk(REAL_CONTEXT,context_overlap=True,source_kind='attachment',mime_path='2',
        segment_id="mseg_9f475189b28e09cc160a069411fd20970c60a6febb8c6c740f894b8bcd17963f",
        chunk_id="mseg_9f475189b28e09cc160a069411fd20970c60a6febb8c6c740f894b8bcd17963f:0:context",
        message_key="mm_c4b187b1c1724b8da8f9924be4083dc9",
        selected_roles=['to'],participants=[{"role":"cc","address":"zhao.min@shengyuan-zn.cn"},{"role":"cc","address":"liu.yang@tuofeng-precision.com"},{"role":"from","address":"wang.rui@tuofeng-precision.com"},{"role":"to","address":"chen.li@shengyuan-zn.cn"}],
        sent_at_utc='2026-09-18T06:20:00+00:00',date_raw='Fri, 18 Sep 2026 14:20:00 +0800',
        body_fragments=[],attribution_uncertain=True)
    new={**old,'context_overlap':False,'text':primary_text,'char_start':932,'char_end':932+len(primary_text),
        'chunk_id':old['segment_id']+':932'}
    chunks=[old,new]
    return chunks,bind_span_kinds(citation_span_payload(chunks),chunks,REAL_SELECTED,'activity_report')


@pytest.mark.parametrize('call_index',[0,1],ids=['recorded_reader','recorded_checker'])
def test_real_mixed_context_footer_response_is_fully_audited_without_new_event(call_index):
    chunks,spans=real_windows();value=deepcopy(REAL_RESPONSES[call_index]);excluded=[]
    assert len(REAL_CONTEXT)==932 and [s['char_end'] for s in spans]==[878,932,1021]
    findings=validate_batch(value,chunks,REAL_SELECTED,'activity_report',
        discard_scanned_overlap=True,excluded_overlap=excluded)
    assert findings==[] and len(excluded)==1
    audit=excluded[0]
    assert audit['reason']=='primary_citations_only_non_event_tail'
    assert audit['finding']['statement']==value['findings'][0]['statement']
    assert audit['finding']['conditions']==value['findings'][0]['conditions']
    assert [q['evidence_id'] for q in audit['finding']['citations']]==['q1','q2','q3']
    assert [q['quote'] for q in audit['finding']['citations']]==[s['quote'] for s in spans]
    assert _preserve_new_spans(findings,chunks,REAL_SELECTED,spans)==([],[])
    assert len(validate_batch(value,chunks,REAL_SELECTED,'activity_report'))==1


def test_primary_template_only_row_is_also_excluded():
    chunks,spans=real_windows();value=deepcopy(REAL_RESPONSES[1]);excluded=[]
    value['findings'][0]['citations']=[{'evidence_id':'q3'}]
    value['findings'][0]['statement']='材料尾栏仅列经办人和未填写日的日期栏。'
    assert validate_batch(value,chunks,REAL_SELECTED,'activity_report',
        discard_scanned_overlap=True,excluded_overlap=excluded)==[]
    assert excluded[0]['reason']=='primary_citations_only_non_event_tail'
    assert excluded[0]['finding']['citations'][0]['quote']==REAL_TAIL


def test_appended_identity_anchor_cannot_promote_a_primary_template_row():
    chunks,spans=real_windows()
    for source in chunks:source['selected_roles']=[]
    selected={**REAL_SELECTED,'entity_id':'person1','display_name':'陈立'}
    value=response([{'kind':'source_information','statement':'材料尾栏列经办人和空白日期字段。',
        'citations':[{'evidence_id':'q3'}],'identity_anchor':{'evidence_id':'q2'}}])
    excluded=[]
    assert validate_batch(value,chunks,selected,'activity_report',
        discard_scanned_overlap=True,excluded_overlap=excluded)==[]
    assert [q['evidence_id'] for q in excluded[0]['finding']['citations']]==['q3','q2']


@pytest.mark.parametrize('mutation',['unknown_quote','wrong_actor','psychology','missing_ack','bad_text','bad_completion'])
def test_invalid_mixed_footer_rows_are_rejected_before_exclusion(mutation):
    chunks,spans=real_windows();value=deepcopy(REAL_RESPONSES[1]);row=value['findings'][0];excluded=[]
    if mutation=='unknown_quote':row['citations'].append({'evidence_id':'q99'})
    elif mutation=='wrong_actor':row['kind']='action_statement'
    elif mutation=='psychology':row['kind']='intent_hypothesis'
    elif mutation=='missing_ack':value['scanned_scan_ids']=[]
    elif mutation=='bad_text':row['conditions']=['']
    else:row['completion']='self_reported'
    with pytest.raises(ValueError):
        validate_batch(value,chunks,REAL_SELECTED,'activity_report',
            discard_scanned_overlap=True,excluded_overlap=excluded)
    assert excluded==[]


@pytest.mark.parametrize('text',['日期：2026年10月9日','日期：2026-10-09',
    '新增条件：若审批不通过恢复原方案。','已寄出纸质原件，扫描件尚未提交。',
    '经办人：王锐已发货','经办人：王锐2026年10月9日',
    '经办人：王锐发出','经办人：王锐收货'])
def test_mixed_row_with_concrete_new_date_condition_or_action_is_kept(text):
    chunks,spans=real_windows(text);value=deepcopy(REAL_RESPONSES[1]);excluded=[]
    value['findings'][0]['statement']='材料旧条款及新增原文：'+text
    findings=validate_batch(value,chunks,REAL_SELECTED,'activity_report',
        discard_scanned_overlap=True,excluded_overlap=excluded)
    assert len(findings)==1 and excluded==[]
    assert findings[0]['citations'][-1]['quote']==text


def test_footer_exclusion_preserves_other_uncited_substantive_new_original():
    chunks,spans=real_windows();text='新增条件：审批未通过前不得下订单；否则恢复原方案。'
    chunks.append({**chunks[1],'chunk_id':'other_new','text':text,'char_start':1021,'char_end':1021+len(text)})
    spans=bind_span_kinds(citation_span_payload(chunks),chunks,REAL_SELECTED,'activity_report')
    value=deepcopy(REAL_RESPONSES[1]);value['scanned_scan_ids']=['c1','c2'];excluded=[]
    findings=validate_batch(value,chunks,REAL_SELECTED,'activity_report',
        discard_scanned_overlap=True,excluded_overlap=excluded)
    preserved,ids=_preserve_new_spans(findings,chunks,REAL_SELECTED,spans)
    assert excluded and ids==['q4'] and len(preserved)==1
    assert preserved[0]['citations'][0]['quote']==text
    assert text in preserved[0]['statement']


def windows():
    previous=('合同要求分项验收，需提交测试报告。'+'原已扫描资料。'*130)[:932]
    tail='新增条件：财务审批未通过前不得下订单；若审批不通过恢复原方案。\n承建方（签章）：张伟\n日期：2026年10月9日'
    old=chunk(previous,context_overlap=True,source_kind='attachment',mime_path='2',page_number=1,selected_roles=['cc'])
    new=chunk(tail,2,len(previous),source_kind='attachment',mime_path='2',page_number=1,selected_roles=['cc'])
    chunks=[old,new]
    spans=bind_span_kinds(citation_span_payload(chunks),chunks,SELECTED,'activity_report')
    return chunks,spans


def response(rows):return {'scanned_scan_ids':['c1'],'findings':rows}


def duplicate():
    return {'kind':'source_information','statement':'材料要求分项验收并提交测试报告。',
        'citations':[{'evidence_id':'q1'}],'completion':'not_established'}


def test_valid_old_only_event_is_audited_and_new_condition_signature_tail_is_preserved():
    chunks,spans=windows();excluded=[]
    findings=validate_batch(response([duplicate()]),chunks,SELECTED,'activity_report',
        discard_scanned_overlap=True,excluded_overlap=excluded)
    assert findings==[] and len(excluded)==1
    assert excluded[0]['reason']=='all_citations_already_scanned'
    assert excluded[0]['finding']['citations'][0]['quote']==spans[0]['quote']
    findings,ids=_preserve_new_spans(findings,chunks,SELECTED,spans)
    normalized=_normalize_fact_claims(findings)
    assert ids==[spans[-1]['evidence_id']] and len(normalized)==1
    assert normalized[0]['citations'][0]['quote']==chunks[1]['text']
    assert '审批未通过前不得下订单' in normalized[0]['statement'] and '恢复原方案' in normalized[0]['statement']
    assert '日期：2026年10月9日' in normalized[0]['statement']
    assert normalized[0]['completion']=='not_established' and '不能证明实际个人签署或盖章' in normalized[0]['statement']
    with pytest.raises(ValueError,match='重叠上下文'):validate_batch(response([duplicate()]),chunks,SELECTED,'activity_report')


def test_mixed_new_and_old_event_is_kept_without_duplicate_filtering():
    chunks,spans=windows();row=duplicate();row['citations'].append({'evidence_id':spans[-1]['evidence_id']})
    row['statement']='材料要求验收，新增条件规定财务审批未通过前不得下订单。'
    excluded=[]
    result=validate_batch(response([row]),chunks,SELECTED,'activity_report',discard_scanned_overlap=True,excluded_overlap=excluded)
    assert len(result)==1 and not excluded and len(result[0]['citations'])==2
    preserved,ids=_preserve_new_spans(result,chunks,SELECTED,spans)
    assert len(preserved)==1 and ids==[] and preserved[0]['statement']==result[0]['statement']
    assert preserved[0]['source_details'][0]['quote']==chunks[1]['text']


@pytest.mark.parametrize('mutation',['unknown_quote','wrong_actor','psychology','missing_ack','bad_text','bad_completion'])
def test_invalid_pure_overlap_rows_or_scan_ack_still_fail(mutation):
    chunks,spans=windows();row=duplicate();value=response([row])
    if mutation=='unknown_quote':row['citations']=[{'evidence_id':'q99'}]
    elif mutation=='wrong_actor':row['kind']='action_statement'
    elif mutation=='psychology':row['kind']='intent_hypothesis'
    elif mutation=='missing_ack':value['scanned_scan_ids']=[]
    elif mutation=='bad_text':row['conditions']=['']
    else:row['completion']='self_reported'
    with pytest.raises(ValueError):validate_batch(value,chunks,SELECTED,'activity_report',discard_scanned_overlap=True)


def test_new_span_only_partially_cited_still_gets_complete_exact_original():
    chunks,spans=windows();text=chunks[1]['text'];first=text.split('；')[0]
    value=response([{'kind':'source_information','statement':'仅概括新增条件的一部分。',
        'citations':[{'chunk_id':chunks[1]['chunk_id'],'quote':first,'char_start':chunks[1]['char_start']}]}])
    findings=validate_batch(value,chunks,SELECTED,'activity_report')
    complete,ids=_preserve_new_spans(findings,chunks,SELECTED,spans)
    assert ids==[spans[-1]['evidence_id']] and len(complete)==2
    assert complete[-1]['citations'][0]['quote']==text and '恢复原方案' in complete[-1]['statement']


def test_real_reader_checker_mixed_footer_is_scanned_without_new_event(person_data):
    assert len(REAL_TAIL)==89 and _non_event_tail(REAL_TAIL)
    repo,pid,add=person_data;key=add('确认书原文见附件。',attachment=True)
    prefix=REAL_CONTEXT
    attachment_text(repo,pid,key,prefix+REAL_TAIL)
    tid=make_turn(repo,pid)
    class RecordedResponses(Model):
        def __init__(self):super().__init__();self.tail_calls=0
        def chat_json(self,system,prompt,**kwargs):
            p=json.loads(prompt)
            if not any(c.get('context_overlap') for c in p['chunks']):return super().chat_json(system,prompt,**kwargs)
            self.payloads.append(p)
            assert [s['char_end'] for s in p['citation_spans']]==[878,932,1021]
            assert ''.join(s['quote'] for s in p['citation_spans'])==REAL_CONTEXT+REAL_TAIL
            value=deepcopy(REAL_RESPONSES[self.tail_calls]);self.tail_calls+=1
            return {'json':value}
    model=RecordedResponses()
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        for _ in range(4):
            run_activity_slice(repo,tid,'owner',model,char_budget=932)
            before=repo.get_turn(tid,'owner')['final']
            if before['cursor'][1]==932:break
        else:raise AssertionError('fixture did not reach the real tail offset')
        facts=_saved_findings(repo,tid)
        with repo._connection() as c:
            old_steps=[tuple(row) for row in c.execute('SELECT step_id,output_ref FROM mail_research_steps WHERE turn_id=? ORDER BY sequence',(tid,))]
        result=run_activity_slice(repo,tid,'owner',model,char_budget=500)
    after=repo.get_turn(tid,'owner')['final']
    assert result['_requeue_slice'] and after['characters_read']==before['characters_read']+89
    assert after['batches']==before['batches']+1 and after['cursor']!=before['cursor']
    assert _saved_findings(repo,tid)==facts and after['findings_count']==before['findings_count']
    with repo._connection() as c:
        assert [tuple(row) for row in c.execute('SELECT step_id,output_ref FROM mail_research_steps WHERE turn_id=? ORDER BY sequence LIMIT ?',(tid,len(old_steps)))]==old_steps
        output=json.loads(c.execute("SELECT output_ref FROM mail_research_steps WHERE turn_id=? AND tool_name='person_activity_batch' ORDER BY sequence DESC LIMIT 1",(tid,)).fetchone()[0])
    assert output['findings']==[] and output['scanned_scan_ids']==['c1']
    assert model.tail_calls==2 and len(output['excluded_overlap_findings'])==2 and not output['preserved_new_evidence_ids']
    assert all(f['reason']=='primary_citations_only_non_event_tail' for f in output['excluded_overlap_findings'])
    assert all([q['evidence_id'] for q in f['finding']['citations']]==['q1','q2','q3'] for f in output['excluded_overlap_findings'])
    assert output['non_event_tail_spans'][0]['quote']==REAL_TAIL


@pytest.mark.parametrize('text',['日期：2026年10月9日','日期：2026-10-09',
    '生效日期：2026年10月9日','新增条件：若审批不通过恢复原方案。',
    '经办人：王锐已发货','经办人：王锐2026年10月9日',
    '经办人：王锐发出','经办人：王锐收货','签章：王锐经办123'])
def test_concrete_new_dates_and_conditions_are_not_blank_template_tails(text):
    chunks,spans=windows();chunks[1].update(text=text,char_end=chunks[1]['char_start']+len(text))
    spans=bind_span_kinds(citation_span_payload(chunks),chunks,SELECTED,'activity_report')
    assert not _non_event_tail(text)
    preserved,ids=_preserve_new_spans([],chunks,SELECTED,spans)
    assert len(preserved)==1 and ids and text in preserved[0]['statement']
    assert preserved[0]['completion']=='not_established'
