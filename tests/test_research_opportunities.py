import copy
import json
from pathlib import Path

import pytest
from services import reporting as rep
from services.research_opportunities import review_model_proposal
from services.research_elements import research_passages
from services.preliminary_summary_input import build_preliminary_input
from services.document_analysis import _preview_input
from ordinary_research_fixtures import cases, document, analysis, VISION, CALIBRATION, COUNTER

@pytest.mark.parametrize('name,material,expected',cases(),ids=[c[0] for c in cases()])
def test_expected_opportunities_recalled(name, material, expected):
    candidates=rep._direction_candidates({},material)
    assert expected <= {c['opportunity_kind'] for c in candidates}
    if not expected:
        assert not candidates
    assert material['research_opportunity_audit']['coverage']=='all_available_topics_scanned'
    for c in candidates:
        assert c['quality_gate']['status']=='passed'
        assert c['evidence_ids'] and c['research_questions'] and all('→' in m for m in c['methods'])
        assert set(c['evidence_roles'])=={'support','prior_work','conditions','counterevidence','resources'}
        assert set(c['retrieval_audit']['role_queries'])==set(c['evidence_roles'])

def test_counterevidence_changes_support_and_is_sent_to_model():
    a=document('vision/a.md',VISION,'噪声图像识别')
    before=rep._direction_candidates({},analysis([a]))[0]
    after_candidates=rep._direction_candidates({},analysis([a,document('vision/c.md',COUNTER,'噪声复核')]))
    after=next(c for c in after_candidates if c['opportunity_kind']=='boundary')
    assert after['score'] < before['score']
    assert after['counterevidence'] and after['evidence_roles']['counterevidence']
    report={'direction_candidates':after_candidates}
    catalog=rep._aligned_evidence_catalog({},report,[])
    assert set(after['evidence_roles']['counterevidence']) <= {e['evidence_id'] for e in catalog}

def test_copies_and_mtime_do_not_change_primary_score_or_question():
    rows={name:rep._direction_candidates({},material) for name,material,_ in cases()}
    first=rows['论文明确局限'][0]
    for name in ('重复副本','修改时间扰动'):
        assert rows[name][0]['score']==first['score']
        assert rows[name][0]['research_questions']==first['research_questions']
        assert rows[name][0]['independent_source_count']==first['independent_source_count']

def test_selected_scope_rejects_foreign_sources_and_candidate_citations(monkeypatch):
    material=next(m for name,m,_ in cases() if name=='选定范围隔离')
    candidate=rep._direction_candidates({},material)[0]
    assert all(e['source_path']=='vision/a.md' for e in candidate['evidence_chain'])
    outside=document('archive/out.md','档案评价的结果表明基线存在变化，需要核验来源。','档案评价')['evidence'][0]
    monkeypatch.setattr(rep,'claim_semantic_scores',lambda c,e:([0.0]*len(e),'lexical-fallback'))
    merged=rep.merge_model_report_aligned({'direction_candidates':[candidate],'recommended_research_direction':candidate},
        {'recommended_research_direction':{'candidate_rank':1,'rationale':'噪声图像识别值得复核','evidence_ids':[outside['evidence_id']]}},candidate['evidence_chain']+[outside])
    assert merged['model_adoption']['recommendation']=='rejected_proposal_review'
    assert merged['recommended_research_direction']==candidate

def test_global_first_and_unsupported_numbers_are_rejected():
    candidate=rep._direction_candidates({},analysis([document('a.md',VISION,'融合')]))[0]
    _,audit,ok=review_model_proposal(candidate,{'rationale':'这是全球首次填补图像识别空白','evidence_ids':candidate['evidence_ids']},candidate['evidence_chain'])
    assert not ok and audit['rejected']
    _,audit,ok=review_model_proposal(candidate,{'rationale':'在3000个样本上准确率达到99%，研究已经证明效果','evidence_ids':candidate['evidence_ids']},candidate['evidence_chain'])
    assert not ok and audit['rejected'][0]['reason']=='unverified_numbers'

def test_middle_limitations_units_and_negations_reach_both_active_inputs():
    critical='实验条件为温度37℃，样本120例，剂量0.5 mg；结果没有显著改善，尚未验证其他条件。'
    doc={'text':'背景资料。'*2500+'\n局限\n'+critical+'\n'+'附录记录。'*2500,
         'structure':{'title':'实验记录'},'evidence':[{'evidence_id':'E-middle','text':critical,'section':'局限'}]}
    original=copy.deepcopy(doc)
    for select in (build_preliminary_input,lambda d:_preview_input(d,8000)):
        text=select(doc)
        assert critical in text and 'E-middle' in text
    assert doc==original

def test_short_source_keeps_ids_without_repeating_body():
    d=document('a.md',VISION,'融合与边界')
    selected=build_preliminary_input(d)
    assert selected.count(VISION)==1
    assert d['evidence'][0]['evidence_id'] in selected

def test_model_does_not_erase_counterevidence_or_raise_local_confidence(monkeypatch):
    material=analysis([document('a.md',VISION,'图像'),document('counter.md',COUNTER,'复核')])
    candidate=rep._direction_candidates({},material)[0]
    monkeypatch.setattr(rep,'claim_semantic_scores',lambda c,e:([0.0]*len(e),'lexical-fallback'))
    merged=rep.merge_model_report_aligned({'direction_candidates':[candidate],'recommended_research_direction':candidate},
        {'recommended_research_direction':{'candidate_rank':1,'rationale':'先核验低质量图像识别的噪声输入条件与可靠性','confidence':'高','evidence_ids':candidate['evidence_ids']}},candidate['evidence_chain'])
    chosen=merged['recommended_research_direction']
    assert chosen['confidence']=='低'
    assert chosen['counterevidence']==candidate['counterevidence']
    assert chosen['limitations']==candidate['limitations']
