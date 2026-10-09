"""Supplementary display quotes carry exact provenance, never new verdicts."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from services.mail.research.answer_synthesis import supplementary_source_quotes,synthesize
from test_mail_answer_synthesis import setup
from test_mail_question_content_profile import candidate_setup,execute
from test_mail_question_literal_projection import v9_replay


def actual_v10():
    return json.loads((Path(__file__).parent/'fixtures'/'mail_question_v10_display_failure.json').read_text(encoding='utf-8'))


def test_actual_v10_original_final_omitted_dates_but_safe_rendering_restores_own_literal_paragraphs():
    data=actual_v10();final=data['final'];synthesis=deepcopy(final['answer_synthesis']);sources=deepcopy(final['evidence'])
    assert '10 月 20 日' not in final['answer']
    before=deepcopy(synthesis);runtime,state,_,_,_=setup()
    state.update(answer_synthesis=synthesis,findings=[],context_gaps=[],allowed_keys={s['message_key'] for s in sources})
    rendered=runtime._answer({'mode':'research'},sources,{}, {'matched':8},0,state,[])
    assert synthesis==before and sources==final['evidence']
    assert '原计划日期 10 月 15 日、我司申请日期 10 月 20 日，均不再采用。' in rendered['answer']
    assert '设备发运、物流签收均不构成交付完成' in rendered['answer']
    assert '仅为原文引用，不证明其适用或已履行' in rendered['evidence_appendix']['recorded_answer']
    assert '附件原文' in rendered['evidence_appendix']['recorded_answer'] and '实际完成日期' in rendered['answer'] and '未知' in rendered['answer']
    by_id={s['evidence_id']:s for s in sources};supplements=rendered['source_supplements']
    assert supplements['semantic_verdicts_unchanged']
    for q in supplements['quotes']:
        source=by_id[q['evidence_id']];start=q['char_start']-int(source.get('char_start') or 0)
        assert source['quote_text'][start:start+len(q['quote'])]==q['quote']
        assert q['char_end']==q['char_start']+len(q['quote'])
        assert q['semantic_status']=='literal_source_reference_only'
        assert q['proves_applicability'] is False and q['proves_fulfillment'] is False
    definition=next(q for q in supplements['quotes'] if '设备发运、物流签收均不构成交付完成' in q['quote'])
    assert definition['recorded_at'].startswith('2026-10-21') and definition['recorded_sender']=='zhang.wei@lanwan-group.com'
    assert any(o.get('literal')=='设备发运、物流签收均不构成交付完成。' for o in supplements['omitted'])


def exact_actual_replay():
    data=actual_v10();p=data['generation_prompt'];d=p['source_documents'];b=p['source_blocks']
    sources=[{**d.get('_defaults',{}),**d[b[r['source_ref']]['document_ref']],
        **{k:v for k,v in b[r['source_ref']].items() if k!='document_ref'},**{k:v for k,v in r.items() if k!='source_ref'}} for r in p['evidence']]
    runtime,state,_,calls,_=setup();steps=[];runtime.repository.record_research_step=lambda *a,**kw:steps.append((a,kw))
    keys=sorted({s['message_key'] for s in sources});state.update(allowed_keys=set(keys),analysis_completed=False,
        context_reviews={k:{'complete':False,'gaps':['uncertain_thread_links'],'message_keys':keys,'edges':[],
            'evidence_ids':[s['evidence_id'] for s in sources]} for k in keys})
    def model(t,o,current,purpose,system,prompt,**kw):
        current['model_calls']+=1;payload=json.loads(prompt);calls.append((purpose,payload,system,prompt,kw))
        name={'answer_synthesis':'generation','answer_synthesis_repair':'repair','semantic_claim_verification':'judge'}[purpose]
        return deepcopy(data[name])
    runtime._model_call=model
    # Isolate the frozen v11 provider path; v12 literal alternatives are covered
    # with their exact v11 failure input in the dedicated reproposal regression.
    with patch('services.mail.research.answer_synthesis._literal_content_proposal',return_value=None):
        success=synthesize(runtime,'t','o',state,{'objective':p['objective'],'mode':'research',
            'budget':{'max_context_tokens':12000}},sources,[],direct=True)
    state.update(findings=[],context_gaps=[])
    rendered=runtime._answer({'mode':'research'},sources,{}, {'matched':8},0,state,[])
    return success,state,calls,steps,rendered,data


def test_exact_actual_v10_original_generation_repair_judge_replay_preserves_all_original_verdicts():
    success,state,calls,steps,rendered,data=exact_actual_replay()
    assert success and len(calls)==3
    # This replay reconstructs wire sources/reviews, so integrity hashes are
    # correctly rebound to that local context; every semantic result stays exact.
    checked=deepcopy(state['answer_synthesis']['claims']);expected=deepcopy(data['final']['answer_synthesis']['claims'])
    for rows in (checked,expected):
        for row in rows:
            row['semantic_verification'].pop('audit_binding')
            row['semantic_verification']['qualification_scope'].pop('binding')
    assert checked==expected
    for purpose,payload,_,_,_ in calls:
        name={'answer_synthesis':'generation','answer_synthesis_repair':'repair','semantic_claim_verification':'judge'}[purpose]
        assert payload==data[name+'_prompt']
    audit=state['answer_synthesis']['candidate_audit']
    assert audit['answer_generation_raw']==data['generation'] and audit['answer_format_repair']['proposal']==data['repair']
    assert audit['answer_candidate_projections'][0]['atom_index']==1
    assert any(args[1]=='answer_candidate_audit' and args[2]['stage']=='bounded_repair_started' for args,kw in steps)
    assert '10 月 20 日' in rendered['answer'] and '物流签收均不构成' in rendered['answer']


def simple():
    source={'evidence_id':'e','message_key':'m','source_kind':'body','mime_path':'1','char_start':100,
        'quote_text':'项目甲确认期限为10月18日。\n若签署报告，项目甲才完成交付。',
        'body_fragments':[{'fragment_kind':'authored','char_start':100,'char_end':200}],
        'participants':[{'role':'from','address':'a@example.com'}],'sent_at_utc':'2026-10-09'}
    row={'claim_text':'项目甲确认期限为10月18日。','source_quote':'项目甲确认期限为10月18日。',
        'evidence_ids':['e'],'actor':'a@example.com','object_ref':'项目甲','conditions':['项目甲才完成交付。'],
        'semantic_status':'supported','answer_relevance':'direct'}
    return row,source


def test_condition_fragment_expands_to_full_original_paragraph_with_preceding_condition_and_absolute_offset():
    row,source=simple();audit=supplementary_source_quotes([row],[source],allowed_keys={'m'})
    q=audit['quotes'][0]
    assert q['quote']=='若签署报告，项目甲才完成交付。'
    assert q['char_start']==100+source['quote_text'].find('若签署报告')


@pytest.mark.parametrize('change',['forged','foreign','quoted','cc','unknown','duplicate','background','insufficient','scope','wrong_anchor','joined','ambiguous_primary','multiple_authors'])
def test_display_does_not_promote_foreign_unbound_ambiguous_or_unverified_conditions(change):
    row,source=simple();sources=[source];allowed={'m'}
    if change=='forged':row['conditions']=['项目甲已经验收完成。']
    elif change=='foreign':
        source['quote_text']=source['quote_text'].split('\n')[0]
        sources.append({**deepcopy(source),'evidence_id':'foreign','message_key':'other','quote_text':'若签署报告，项目甲才完成交付。'})
        row['evidence_ids'].append('foreign');allowed.add('other')
    elif change=='quoted':
        start=100+source['quote_text'].find('若签署报告');source['body_fragments']=[
            {'fragment_kind':'authored','char_start':100,'char_end':start},
            {'fragment_kind':'quoted','char_start':start,'char_end':200}]
    elif change=='cc':source['participants'][0]['role']='cc'
    elif change=='unknown':source['body_fragments']=[]
    elif change=='duplicate':source['quote_text']+='\n若签署报告，项目甲才完成交付。'
    elif change=='background':row['answer_relevance']='background'
    elif change=='insufficient':row['semantic_status']='insufficient'
    elif change=='scope':allowed=set()
    elif change=='wrong_anchor':row['object_ref']='项目乙'
    elif change=='joined':row['conditions']=['确认期限为10月18日。项目甲才完成交付。']
    elif change=='ambiguous_primary':
        other={**deepcopy(source),'evidence_id':'other','message_key':'other'};sources.append(other);row['evidence_ids'].append('other');allowed.add('other')
    elif change=='multiple_authors':source['participants'].append({'role':'from','address':'b@example.com'})
    original=deepcopy(row);audit=supplementary_source_quotes([row],sources,allowed_keys=allowed)
    assert not audit['quotes'] and row==original


def test_explicit_own_attachment_quote_retains_mail_envelope_without_claiming_sender_is_attachment_author():
    row,source=simple();row['conditions']=['项目甲以验收报告为准。']
    attachment={**deepcopy(source),'evidence_id':'a','source_kind':'attachment','mime_path':'2',
        'quote_text':'项目甲以验收报告为准。','char_start':0,'body_fragments':[]}
    row['evidence_ids'].append('a');q=supplementary_source_quotes([row],[source,attachment],allowed_keys={'m'})['quotes'][0]
    assert q['source_kind']=='attachment' and q['attribution']=='attachment' and q['recorded_sender']=='a@example.com'
    assert 'author' not in q and q['proves_applicability'] is False


def test_candidate_raw_projection_and_original_verdict_are_persisted_without_provider_prompt_changes():
    success,state,calls,data=v9_replay()
    assert success and [purpose for purpose,_,_ in calls]==['answer_synthesis','semantic_claim_verification']
    audit=state['answer_synthesis']['candidate_audit']
    assert audit['answer_generation_raw']==data['generation']
    assert audit['answer_candidate_projections'][0]['before']==data['generation']['claims'][1]
    assert audit['answer_candidate_projections'][0]['after']['source_quote']==data['generation']['claims'][1]['source_quote']


def test_rejected_strict_repair_persists_raw_schema_proposal_and_stop_audit():
    runtime,state,sources,calls,generated,options=candidate_setup();options['repair_failure']='conditions';steps=[]
    runtime.repository.record_research_step=lambda *args,**kw:steps.append((args,kw))
    # Preserve the v9 repair-only regression route, without optional projection.
    from unittest.mock import patch
    with patch('services.mail.research.answer_synthesis._source_local_date_proposal',return_value=None), \
            patch('services.mail.research.answer_synthesis._literal_content_proposal',return_value=None):
        assert not execute(runtime,state,sources)
    audit=state['answer_synthesis']['candidate_audit']
    assert audit['answer_generation_raw']==generated
    repair=audit['answer_format_repair']
    assert repair['status']=='rejected' and repair['proposal']['repairs'][0]['conditions']==[]
    assert repair['output_schema']['additionalProperties'] is False
    persisted=[json.loads(kw['output_ref']) for args,kw in steps if args[1]=='answer_candidate_audit']
    assert persisted[-1]==audit and any(a['answer_format_repair']['status']=='attempted' for a in persisted if 'answer_format_repair' in a)


def test_new_attempt_cannot_inherit_prior_projection_repair_or_precheck_snapshots():
    runtime,state,sources,_,_=setup();steps=[]
    runtime.repository.record_research_step=lambda *a,**kw:steps.append((a,kw))
    state.update(answer_generation_raw={'claims':['old']},answer_candidate_projections=[{'atom_index':99}],
        answer_format_repair={'proposal':'old'},answer_content_precheck={'failures':['old']},answer_candidate_attempt=1)
    assert synthesize(runtime,'t','o',state,{'objective':'项目甲交付日期','budget':{'max_context_tokens':12000}},sources,[])
    audit=state['answer_synthesis']['candidate_audit']
    assert audit['attempt']==2 and audit['answer_generation_raw']['claims']!=['old']
    assert not any(key in audit for key in ('answer_candidate_projections','answer_format_repair','answer_content_precheck'))
    # A third malformed response also preserves its own complete raw at stop.
    bad={'claims':['malformed',{'unexpected':True}],'unanswered_subquestions':['actual_completion_date'],'extra_original_field':'preserve verbatim'}
    runtime._model_call=lambda *a,**kw:deepcopy(bad)
    assert not synthesize(runtime,'t','o',state,{'objective':'项目甲交付日期','budget':{'max_context_tokens':12000}},sources,[])
    stopped=state['answer_synthesis']['candidate_audit']
    assert stopped['attempt']==3 and stopped['answer_generation_raw']==bad
    assert json.loads(steps[-1][1]['output_ref'])==stopped
