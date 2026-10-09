"""Deep analysis reuses the immutable complete facts, never re-extracts them."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from config import Config
from web_compat import SJFXFastAPI
from services.mail.api import register_mail_routes
from services.mail.bootstrap import build_mail_services
from services.mail.person_activity import (_saved_findings,activity_page,DEEP_WORKFLOW)
from services.mail.person_activity_api import resume_analysis
from test_person_context_upgrade import person_data,attachment_text
from test_person_activity_v3 import make_turn,finish,Model


class Facts(Model):
    def chat_json(self,system,prompt,**kwargs):
        p=json.loads(prompt);self.payloads.append(p)
        if 'draft_findings' in p:
            return {'json':{'scanned_scan_ids':p['scan_manifest'],'findings':p['draft_findings']}}
        findings=[]
        sources={c['chunk_id']:c for c in p['chunks']}
        for q in p['citation_spans']:
            c=sources[q['chunk_id']]
            if c['source_kind']=='attachment':
                kind='source_information';statement='附件列明承建方签章栏Alice，并列计划到货9月25日和实际发货10月3日。'
                conditions=['签章栏仅为材料字段，不能证明实际个人签署'];contradictions=[]
            elif c['authored']:
                kind='action_statement';statement='本人表示希望在审批后推进WMS验收约定。'
                conditions=['以审批通过为前提'];contradictions=[]
            else:
                kind='received_information';statement='对方催要WMS测试报告和通电证明，并称两项均尚未收到。'
                conditions=[];contradictions=['仍未收到测试报告和通电证明']
            findings.append({'kind':kind,'statement':statement,'conditions':conditions,
                'contradictions':contradictions,'completion':'not_established',
                'citations':[{'evidence_id':q['evidence_id']}]})
        return {'json':{'scanned_scan_ids':p['scan_manifest'],'findings':findings}}


class NoConclusion:
    def __init__(self):self.payloads=[]
    def chat_json(self,system,prompt,**kwargs):
        p=json.loads(prompt);self.payloads.append(p)
        assert 'nodes' in p and 'chunks' not in p and 'draft_findings' not in p
        return {'json':{'considered_source_ids':[n['finding_id'] for n in p['nodes']],'sections':[]}}


def complete_parent(repo,pid,add):
    own=add('I want to proceed with the WMS acceptance agreement, only after approval.',attachment=True)
    attachment_text(repo,pid,own,'Contractor signature field: Alice. Planned arrival September 25; actual shipment October 3.')
    add('Please provide the WMS test report and power-on proof; neither has been received.',
        sender='bob@example.com',recipient='alice@example.com',date='Wed, 21 Oct 2026 09:25:00 +0800')
    repo.finish_import(pid,'owner')
    parent=make_turn(repo,pid);finish(repo,parent,Facts(),budget=4000)
    return parent


def test_deep_copies_every_canonical_fact_and_exact_counterevidence_without_scanning(person_data):
    repo,pid,add=person_data;parent=complete_parent(repo,pid,add)
    frozen=repo.get_turn(parent,'owner')['final'];canonical=_saved_findings(repo,parent)
    tid=make_turn(repo,pid,'deep_analysis',parent,target='unrelated target');model=NoConclusion()
    with patch('services.mail.person_activity.next_chunks',side_effect=AssertionError('deep must not rescan')):
        page=finish(repo,tid,model,budget=1)
    assert page['report']['complete'] and _saved_findings(repo,tid)==canonical
    assert repo.get_turn(parent,'owner')['final']==frozen and len(model.payloads)==1
    progress=page['progress']
    assert progress['characters_read']==0 and progress['batches']==0 and progress['cursor']==[0,0]
    assert not progress.get('scan_complete') and progress['source_scan_complete']
    assert progress['coverage_source']=='completed_activity_report' and progress['coverage_inherited_from']==parent
    assert progress['source_coverage']['characters_read']==progress['corpus']['characters']
    assert progress['source_coverage']['scan_complete']
    assert page['report']['coverage']['coverage_inherited_from']==parent
    md=page['report']['markdown']
    assert '资料覆盖继承已完成事实报告' in md and '本次深度分析复用事实底稿' in md
    assert '未形成该对象的态度或意图分析结论' in md
    for original in canonical:
        assert original['statement'] in md
        assert all(c in md for c in original['conditions']+original['contradictions'])
        assert all(q['quote'] in md for q in original['citations'])
    assert '关联材料中的信息' in md and '别人提及本人的情况' not in md
    nodes=model.payloads[0]['nodes']
    assert {n['fact_kind'] for n in nodes}=={'action_statement','source_information','received_information'}
    material=next(n for n in nodes if n['fact_kind']=='source_information')
    assert material['kind']=='context'
    originals=[q for n in nodes for q in n['original_evidence']]
    assert any(q['quotation_warning'] and q['source_kind']=='attachment' for q in originals)
    assert any('neither has been received' in q['quote'] and not q['authored'] for q in originals)
    with repo._connection() as c:
        steps=[dict(r) for r in c.execute('SELECT tool_name,output_ref FROM mail_research_steps WHERE turn_id=?',(tid,))]
    seed=next(s for s in steps if s['tool_name']=='person_activity_seed')
    assert set(json.loads(seed['output_ref']))=={'findings'}
    assert not any(s['tool_name']=='person_activity_batch' for s in steps)


def test_deep_token_budget_keeps_fact_seed_and_resume_never_duplicates_it(person_data):
    repo,pid,add=person_data;parent=complete_parent(repo,pid,add)
    tid=make_turn(repo,pid,'deep_analysis',parent);turn=repo.get_turn(tid,'owner')
    with repo._connection() as c:
        c.execute('UPDATE mail_research_turns SET task_spec_json=? WHERE turn_id=?',
                  (json.dumps({**turn['task_spec'],'max_tokens':1}),tid))
    model=NoConclusion();page=finish(repo,tid,model)
    before=repo.get_turn(tid,'owner');checkpoint=before['final']
    assert checkpoint['budget_exhausted'] and not page['report']['complete'] and not model.payloads
    assert checkpoint['characters_read']==0 and checkpoint['batches']==0 and checkpoint['model_calls']==0
    assert checkpoint['source_scan_complete'] and _saved_findings(repo,tid)==_saved_findings(repo,parent)
    services=SimpleNamespace(repository=repo,storage=SimpleNamespace(get_job=lambda *a:None),model=model)
    assert resume_analysis(services,before,'owner',{'max_tokens':100000},lambda *a:('resumed-job',True))[1]==202
    result=finish(repo,tid,model)
    assert result['report']['complete'] and len(model.payloads)==1
    assert _saved_findings(repo,tid)==_saved_findings(repo,parent)
    with repo._connection() as c:
        assert c.execute("SELECT COUNT(*) FROM mail_research_steps WHERE turn_id=? AND tool_name='person_activity_seed'",(tid,)).fetchone()[0]==1


def test_deep_groups_shared_exact_originals_once_instead_of_recounting_each_node(person_data):
    from services.mail.person_activity import _summary_payload,_summary_nodes
    repo,pid,add=person_data
    add('\n'.join('I request approval for WMS acceptance item %s, keeping the documented test report condition.'%i
                  for i in range(10)))
    class SharedOriginals:
        def chat_json(self,system,prompt,**kwargs):
            p=json.loads(prompt)
            findings=p.get('draft_findings')
            if findings is None:
                findings=[{'kind':'request_by_person','statement':'本人提出第%s项WMS验收审批请求。'%i,
                    'conditions':['保留测试报告条件'],'completion':'not_established',
                    'citations':[{'evidence_id':s['evidence_id']} for s in p['citation_spans']]} for i in range(10)]
            return {'json':{'scanned_scan_ids':p['scan_manifest'],'findings':findings}}
    parent=make_turn(repo,pid);finish(repo,parent,SharedOriginals(),budget=4000)
    tid=make_turn(repo,pid,'deep_analysis',parent);model=NoConclusion();page=finish(repo,tid,model)
    nodes=_summary_nodes(repo,tid,0)
    assert sum(len(json.dumps(_summary_payload([n]),ensure_ascii=False)) for n in nodes)>14000
    assert len(json.dumps(_summary_payload(nodes),ensure_ascii=False))<=14000
    assert page['report']['complete'] and len(model.payloads)==1
    originals=[q for n in model.payloads[0]['nodes'] for q in n['original_evidence']]
    assert len(originals)==len({(q['message_key'],q['mime_path'],q['char_start'],q['char_end']) for q in originals})


def test_summary_nested_source_mentions_add_exact_counterevidence_and_keep_facts_unchanged(person_data):
    from services.mail.person_activity import _summary_nodes
    repo,pid,add=person_data;parent=complete_parent(repo,pid,add)
    frozen=repo.get_turn(parent,'owner')['final'];facts=_saved_findings(repo,parent)
    tid=make_turn(repo,pid,'deep_analysis',parent)
    class NestedReferences:
        def chat_json(self,system,prompt,**kwargs):
            p=json.loads(prompt);nodes=p['nodes']
            own=next(n['finding_id'] for n in nodes if n['fact_kind']=='action_statement')
            material=next(n['finding_id'] for n in nodes if n['fact_kind']=='source_information')
            contrary=next(n['finding_id'] for n in nodes if n['fact_kind']=='received_information')
            return {'json':{'considered_source_ids':[n['finding_id'] for n in nodes], 'sections':[{
                'kind':'intent_hypothesis','title':'可能依据'+own+'继续推进',
                'statement':'一种可能的解释是根据'+own+'保留审批前提。',
                'primary_source_id':own,'source_ids':[own,own,material],
                'conditions':['以'+material+'材料所列要求为条件'],
                'contradictions':[contrary+'指出测试报告和通电证明尚未收到'],
                'uncertainty':'不能仅凭'+own+'确定心理动机',
                'alternative_explanations':['也可能是'+material+'所列流程要求'],
                'unresolved_questions':['如何回应'+contrary+'的催办尚需核实']} ]}}
    page=finish(repo,tid,NestedReferences(),budget=4000)
    sections=_summary_nodes(repo,tid,1)
    assert page['report']['complete'] and len(sections)==1
    section=sections[0]
    assert len(section['source_ids'])==3 and set(section['source_ids'])=={f['finding_id'] for f in facts}
    assert {item['finding_id'] for item in section['source_timeline']}=={f['finding_id'] for f in facts}
    assert len(section['citations'])==len({(q['segment_id'],q['char_start'],q['char_end'],q['quote']) for q in section['citations']})
    assert any('neither has been received' in q['quote'] for q in section['citations'])
    assert '2026年10月21日' in section['contradictions'][0] and '收到的信息' in section['contradictions'][0]
    for field in ('title','statement','conditions','contradictions','uncertainty','alternative_explanations','unresolved_questions'):
        assert 'n1' not in str(section[field]) and 'n2' not in str(section[field]) and 'n3' not in str(section[field])
    assert _saved_findings(repo,tid)==facts and repo.get_turn(parent,'owner')['final']==frozen


def test_summary_short_alias_is_resolved_before_another_group_reuses_same_number():
    from services.mail.person_activity import _bind_summary_references
    first={'n1':{'kind':'expressed_attitude','communication_time':'2026-10-18T03:05:00+00:00'},
           'n2':{'kind':'context','fact_kind':'received_information','communication_time':'2026-10-21T01:25:00+00:00'}}
    row={'source_ids':['n1','n1'],'statement':'可能继续推进','contradictions':['n2指出仍未验收。']}
    bound=_bind_summary_references(row,first)
    assert bound['source_ids']==['n1','n2']
    assert '2026年10月21日' in bound['contradictions'][0] and 'n2' not in bound['contradictions'][0]
    next_group={'n1':{'kind':'intent_hypothesis','source_timeline':[{'communication_time':'2026-10-21T01:25:00+00:00'}]},
                'n2':{'kind':'context','communication_time':'2030-01-01T00:00:00+00:00'}}
    next_row={**bound,'source_ids':['n1']}
    second=_bind_summary_references(next_row,next_group)
    assert second['source_ids']==['n1'] and second['contradictions']==bound['contradictions']
    assert '2030' not in second['contradictions'][0]
    assert row['source_ids']==['n1','n1'] and row['contradictions']==['n2指出仍未验收。']


def test_summary_unknown_short_source_cannot_survive_into_another_group():
    from services.mail.person_activity import _bind_summary_references
    sources={'n1':{'kind':'context','communication_time':None}}
    with pytest.raises(ValueError,match='本组未提供'):
        _bind_summary_references({'source_ids':['n1'],'contradictions':['n9指出不同情况。']},sources)
    ordinary=_bind_summary_references({'source_ids':['n1'],
        'statement':'Contact n2@example.com; inspect n3.pdf or model n4-A.'},sources)
    assert ordinary['source_ids']==['n1'] and ordinary['statement']=='Contact n2@example.com; inspect n3.pdf or model n4-A.'


@pytest.mark.parametrize('mutation',['report','coverage','original'])
def test_deep_basis_refresh_rejected_at_preview_and_resume(person_data,mutation):
    repo,pid,add=person_data;parent=complete_parent(repo,pid,add)
    tid=make_turn(repo,pid,'deep_analysis',parent);finish(repo,tid,NoConclusion())
    if mutation=='original':
        with repo._connection() as c:
            c.execute("UPDATE mail_text_segments SET text=REPLACE(text,'want','hate') WHERE package_id=? AND source_kind='body'",(pid,))
    else:
        source=repo.get_turn(parent,'owner')['final']
        changed={**source,'report':{**source['report'],'markdown':source['report']['markdown']+'\nRefreshed report.'}}
        if mutation=='coverage':changed['scan_complete']=False
        repo.update_turn(parent,'owner',final=changed)
    with pytest.raises((ValueError,RuntimeError)):activity_page(repo,tid,'owner')
    repo.update_turn(tid,'owner',status='failed')
    services=SimpleNamespace(repository=repo,storage=SimpleNamespace(get_job=lambda *a:None),model=NoConclusion())
    with pytest.raises((ValueError,RuntimeError)):
        resume_analysis(services,repo.get_turn(tid,'owner'),'owner',{'model_call_budget':200},lambda *a:('must-not-queue',True))


@pytest.mark.parametrize('old_revision',[None,'person-target-from-completed-facts-v1'])
def test_deep_cache_revision_and_download_validate_inherited_basis(person_data,monkeypatch,old_revision):
    repo,pid,add=person_data;parent=complete_parent(repo,pid,add)
    monkeypatch.setattr(Config,'MAIL_FEATURE_ENABLED',True)
    services=build_mail_services(repo.storage,model=NoConclusion(),job_factory=repo.storage.create_or_get_typed_job,
                                owner_resolver=lambda:'owner')
    app=SJFXFastAPI();register_mail_routes(app,services)
    path='/api/mail/packages/'+pid+'/person/analyses'
    payload={'address':'alice@example.com','analysis_mode':'deep_analysis','source_report_turn_id':parent,
             'target':'agreement','question':'他对此的明确态度与可能意图是什么？','max_tokens':100000}
    with TestClient(app) as client:
        response=client.post(path,json=payload);assert response.status_code==202,response.text
        tid=response.json()['turn_id'];finish(repo,tid,NoConclusion())
        repo.storage.update_job(response.json()['job_id'],status='completed')
        turn=repo.get_turn(tid,'owner')
        assert turn['task_spec']['workflow_revision']==DEEP_WORKFLOW
        cached=client.post(path,json=payload)
        assert cached.status_code==202 and cached.json()['cached'] and cached.json()['turn_id']==tid
        download='/api/mail/person-analyses/'+tid+'/report/download'
        assert client.get(download).status_code==200
        old=({**turn['task_spec'],'workflow_revision':old_revision} if old_revision else
             {k:v for k,v in turn['task_spec'].items() if k not in {'workflow_revision','source_report_evidence_hash'}})
        with repo._connection() as c:
            c.execute('UPDATE mail_research_turns SET task_spec_json=? WHERE turn_id=?',(json.dumps(old),tid))
        assert client.get(download).status_code==400
        fresh=client.post(path,json=payload)
        assert fresh.status_code==202 and fresh.json()['turn_id']!=tid and not fresh.json().get('cached')
        # Even a correct canonical workflow cannot download after its parent changes.
        fresh_id=fresh.json()['turn_id'];finish(repo,fresh_id,NoConclusion())
        source=repo.get_turn(parent,'owner')['final']
        repo.update_turn(parent,'owner',final={**source,'report':{**source['report'],'markdown':source['report']['markdown']+'\nUpdated.'}})
        assert client.get('/api/mail/person-analyses/'+fresh_id+'/report/download').status_code==400


@pytest.mark.parametrize('include_attachment',[True,False])
def test_deep_signature_field_upgrade_is_constrained_before_every_recursive_save(person_data,include_attachment):
    from services.mail.person_activity import _summary_nodes,ENGINE
    repo,pid,add=person_data
    # Verbatim 10/9 evidence and the failed real synthesis clause. The body
    # reports a company seal and mailing, with no personal-signing statement.
    own=add('本确认已加盖我司公章，扫描件见附件《蓝湾项目交付确认单-盖章扫描件.pdf》，'
        '纸质原件今日以顺丰寄出（单号 SF-1055-8877-3201），请贵司查收。',attachment=include_attachment)
    if include_attachment:
        attachment_text(repo,pid,own,'四、交付完成以双方签署《验收报告》之日为准。\n'
            '委托方（签章）：张伟\n承建方（签章）：李强\n日期：2026年10月9日\n日期：2026年10月9日')
    add('我方对业主方的承诺不变。',sender='bob@example.com',recipient='alice@example.com')
    add('我方对业主方的承诺尚需核实。',sender='carol@example.com',recipient='alice@example.com')
    class SignatureFacts(Model):
        def chat_json(self,system,prompt,**kwargs):
            p=json.loads(prompt)
            if 'draft_findings' in p:findings=p['draft_findings']
            else:
                sources={c['chunk_id']:c for c in p['chunks']};findings=[]
                for q in p['citation_spans']:
                    c=sources[q['chunk_id']]
                    kind=('source_information' if c['source_kind']=='attachment' else
                        'action_statement' if c['authored'] else 'received_information')
                    statement=('附件仅列明签章字段，不证明个人真实签署。' if kind=='source_information' else
                        '本人称本确认已加盖公司公章，扫描件随附，纸质原件以顺丰寄出。' if kind=='action_statement' else q['quote'])
                    findings.append({'kind':kind,'statement':statement,'citations':[{'evidence_id':q['evidence_id']}],
                        'completion':'self_reported' if kind=='action_statement' else 'not_established'})
            return {'json':{'scanned_scan_ids':p['scan_manifest'],'findings':findings}}
    parent=make_turn(repo,pid);finish(repo,parent,SignatureFacts(),budget=4000)
    canonical=_saved_findings(repo,parent);parent_final=repo.get_turn(parent,'owner')['final']
    assert repo.get_turn(parent,'owner')['task_spec']['execution_engine']==ENGINE=='person-full-package-activity-v3.4'
    tid=make_turn(repo,pid,'deep_analysis',parent)
    failed='在客户拒绝延期并设定更严格的10月18日交付节点后，李强迅速调整态度，接受该节点，签署盖章确认单，并承诺若再次逾期愿承担违约金责任。'
    fields=('title','statement','uncertainty','conditions','contradictions','alternative_explanations','unresolved_questions')
    class RepeatedUpgrade:
        def __init__(self):self.payloads=[]
        def chat_json(self,system,prompt,**kwargs):
            p=json.loads(prompt);nodes=p['nodes'];self.payloads.append(p)
            if len(self.payloads)>1:
                prior=next(n for n in nodes if n['kind']=='intent_hypothesis')
                for field in fields:
                    assert '签署盖章确认单' not in str(prior[field])
            ids=[n['finding_id'] for n in nodes]
            primary=next(n['finding_id'] for n in nodes if n['kind']!='context')
            row={'kind':'intent_hypothesis','source_ids':ids,'primary_source_id':primary,
                'title':'李强签署盖章确认单','statement':failed,
                'uncertainty':'李强签署盖章确认单，但动机尚需核实',
                'conditions':['李强签署盖章确认单后履约'],
                'contradictions':['李强签署盖章确认单与未验收情况并存'],
                'alternative_explanations':['李强签署盖章确认单也可能只是配合流程'],
                'unresolved_questions':['李强签署盖章确认单后为何未验收？']}
            sections=[row]
            if len(self.payloads)==1:
                sections.append({'kind':'context','title':'其他来源的约定',
                    'statement':'不同发言方的我方对业主承诺应分别看待；只有签署《验收报告》才视为交付完成。',
                    'source_ids':ids})
            return {'json':{'considered_source_ids':ids,'sections':sections}}
    model=RepeatedUpgrade();page=finish(repo,tid,model,budget=4000)
    assert page['report']['complete'] and len(model.payloads)==2
    for level in (1,2):
        section=next(s for s in _summary_nodes(repo,tid,level) if s['kind']=='intent_hypothesis')
        for field in fields:
            assert '签署盖章确认单' not in str(section[field])
            assert ('字段文本不能证明实际个人签署或盖章' if include_attachment else
                '现有来源不能证明本人亲自签署或盖章') in str(section[field])
        assert '10月18日' in section['statement'] and '接受该节点' in section['statement']
        assert '违约金责任' in section['statement']
        assert any('本确认已加盖我司公章' in q['quote'] for q in section['citations'])
        if include_attachment:assert any('承建方（签章）：李强' in q['quote'] for q in section['citations'])
        else:
            assert all(q['source_kind']=='body' for q in section['citations'])
            assert 'SF-1055-8877-3201' in section['statement']
        assert len(section['source_timeline'])==len(canonical)
    context=next(s for s in _summary_nodes(repo,tid,1) if s['kind']=='context')
    assert '只有签署《验收报告》才视为交付完成' in context['statement']
    assert _saved_findings(repo,tid)==canonical and repo.get_turn(parent,'owner')['final']==parent_final


@pytest.mark.parametrize('field',['title','statement','uncertainty','conditions','contradictions','alternative_explanations','unresolved_questions'])
def test_real_pure_body_summary_signing_upgrade_keeps_exact_company_claim(field):
    from services.mail.person_activity import _normalize_fact_claims
    fixture=json.loads((Path(__file__).parent/'fixtures'/'person_activity_summary2_body_signing.json').read_text(encoding='utf-8'))
    section=fixture['section'];assert len(section['citations'])==6
    assert all(q['source_kind']=='body' for q in section['citations'])
    original=json.loads(json.dumps(section))
    claim='李强签署盖章确认单'
    if field not in {'title','statement','uncertainty'}:section[field]=[claim]
    elif field!='statement':section[field]=claim
    assert '签署盖章确认单' in section['statement']
    result=_normalize_fact_claims([section],check_ownership=False)[0]
    text=str(result[field])
    assert claim not in text and '签署盖章确认单' not in result['statement']
    assert '现有来源不能证明本人亲自签署或盖章' in text
    assert '本确认已加盖我司公章' in text and '纸质原件今日以顺丰寄出' in text
    assert 'SF-1055-8877-3201' in text and 'li.qiang@shengyuan-zn.cn' in text
    assert '加派4名工程师' in result['statement'] and '接受该节点' in result['statement']
    assert '10月18日' in result['statement'] and '违约金责任' in result['statement']
    for name in ('citations','source_ids','source_timeline','finding_id','kind'):
        assert result[name]==original[name]


def test_pure_body_signing_without_own_support_stays_unestablished():
    from services.mail.person_activity import _normalize_fact_claims
    section=json.loads((Path(__file__).parent/'fixtures'/'person_activity_summary2_body_signing.json').read_text(encoding='utf-8'))['section']
    section['citations']=[q for q in section['citations'] if not q['authored']]
    section['statement']='李强已加盖公司公章，李强签署确认单。'
    result=_normalize_fact_claims([section],check_ownership=False)[0]
    assert '李强已加盖' not in result['statement'] and '李强签署' not in result['statement']
    assert '该动作及个人归属尚待核实' in result['statement']
    assert result['citations']==section['citations']
