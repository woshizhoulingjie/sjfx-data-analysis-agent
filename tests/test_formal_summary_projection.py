from copy import deepcopy
import pytest
from services.formal_summary import project_node_claims


def sample():
    return {'evidence_contract': 'question-answer-evidence/3.0',
            'evidence_status': 'supported', 'member_paths': ['a.txt'],
            'coverage': {'verification_status': 'verified', 'deep_analyzed_files': 1},
            'claims': [{'statement': '买方尚未确认延期。', 'type': 'direct_fact',
                        'support_status': 'supported', 'evidence_ids': ['e1']}],
            'evidence': [{'evidence_id': 'e1', 'source_path': 'a.txt',
                          'supporting_quote': '买方尚未确认延期。', 'page': 2,
                          'support_status': 'supported'}]}


def test_exact_binding_locations_and_input_unchanged():
    data=sample(); before=deepcopy(data); result=project_node_claims(data)
    assert data==before
    assert result['conclusions'][0]['supports']==data['evidence']
    assert result['conclusions'][0]['text']==data['claims'][0]['statement']
    assert result['verification_status']=='verified'
    assert result['evidence_count']==1
    result['conclusions'][0]['supports'][0]['page']=99
    assert data==before


@pytest.mark.parametrize('field,value',[('support_status','candidate'),('support_status','insufficient'),
    ('support_status','unverified'),('support_status','unsupported'),('support_status',''),
    ('evidence_role','node_coverage'),('preview_only',True),('evidence_level','preview'),
    ('coverage',{'preview_only':True}),('source_path','outside.txt'),
    ('source_path',''),('supporting_quote',''),('file_conclusion_status','candidate')])
def test_ineligible_records_never_support_a_claim(field,value):
    data=sample();data['evidence'][0][field]=value
    result=project_node_claims(data)
    assert not result['conclusions']
    assert result['evidence_count']==0
    assert result['verification_status']=='unverified'
    assert len(result['file_review_items'])==1


@pytest.mark.parametrize('status',['candidate','insufficient','unverified','unsupported'])
def test_aggregate_guard_cannot_be_revived(status):
    data=sample();data['evidence_status']=status
    assert not project_node_claims(data)['conclusions']


@pytest.mark.parametrize('place',['claim','evidence','aggregate','inherited'])
def test_partial_support_stays_partial(place):
    data=sample()
    if place=='claim': data['claims'][0]['support_status']='partially_supported'
    elif place=='evidence': data['evidence'][0]['support_status']='partially_supported'
    elif place=='inherited': data['evidence'][0]['file_conclusion_status']='partially_supported'
    else: data['evidence_status']='partially_supported'
    result=project_node_claims(data)
    assert result['evidence_status']=='partially_supported'
    assert result['coverage']['verification_status']=='partial'
    assert len(result['conclusions'])==1


def test_missing_reference_never_uses_nearby_evidence():
    data=sample();data['claims'][0]['evidence_ids']=['missing']
    assert not project_node_claims(data)['conclusions']


def test_partial_reference_loss_downgrades_card():
    data=sample();data['claims'][0]['evidence_ids'].append('missing')
    assert project_node_claims(data)['conclusions'][0]['support_status']=='partially_supported'


def test_duplicate_ids_are_deduplicated_but_conflicts_rejected():
    data=sample();data['claims'][0]['evidence_ids']*=2;data['evidence']*=2
    assert len(project_node_claims(data)['conclusions'][0]['supports'])==1
    data['evidence'].append({**data['evidence'][0],'source_path':'b.txt'})
    assert not project_node_claims(data)['conclusions']


def test_explicit_empty_claims_override_stale_nested_and_legacy_claims():
    data=sample();data['question_answer_evidence']=deepcopy(data)
    data['claims']=[];data['conclusions']=[{'text':'old'}]
    assert not project_node_claims(data)['conclusions']
    assert project_node_claims(data)['evidence_status']=='insufficient'


def test_legacy_payload_is_not_rewritten():
    assert project_node_claims({'conclusions':[{'text':'legacy'}]})=={}


def test_empty_scope_forbids_out_of_scope_evidence():
    data=sample();data['member_paths']=[]
    assert not project_node_claims(data)['conclusions']


@pytest.mark.parametrize('field,value',[('analysis_stage','preliminary'),
    ('analysis_level','preview'),('coverage',{'preview_only':True})])
def test_preview_payload_cannot_be_promoted(field,value):
    data=sample();data[field]=value
    assert not project_node_claims(data)['conclusions']


def test_malformed_optional_fields_do_not_crash_or_invent_evidence():
    data=sample();data['coverage']=[];data['claims'][0]['evidence_ids']='e1'
    assert not project_node_claims(data)['conclusions']
    data['claims']=None
    assert not project_node_claims(data)['conclusions']


def test_review_claim_prevents_fully_supported_topic():
    data=sample();data['claims'].append({'statement':'未核实推断','support_status':'insufficient'})
    result=project_node_claims(data)
    assert result['evidence_status']=='partially_supported'
    assert len(result['conclusions'])==len(result['file_review_items'])==1


def test_nested_only_contract_and_stable_ids():
    data=sample();nested={'question_answer_evidence':data,'member_paths':['a.txt']}
    assert project_node_claims(data)['conclusions']==project_node_claims(nested)['conclusions']


@pytest.mark.parametrize('branch',['tree','selected','preview','saved'])
def test_all_formal_directory_sources_use_projection(branch):
    result=directory_response(branch)
    topics=result['directory']['topics']
    assert len(topics)==1
    assert topics[0]['conclusions'][0]['text']=='买方尚未确认延期。'
    assert topics[0]['evidence_status']=='partially_supported'
    assert topics[0]['coverage']['verification_status']=='partial'
    assert result['directory']['coverage']['formal_evidence']==1


def test_unpublished_deep_results_remain_hidden():
    result=directory_response('saved',published=False)
    assert result['directory']['topics']==[]
    assert not result['directory']['formal']


def directory_response(branch,published=True):
    # Execute the actual route body without importing model/server startup.
    import ast
    import hashlib
    from pathlib import Path
    from unittest.mock import Mock
    from services.import_state_machine import summary_type
    source=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text())
    defs=[node for node in source.body if isinstance(node,ast.FunctionDef)
          and node.name in {'formal_directory','evidence_is_formal'}]
    for node in defs: node.decorator_list=[]
    module=ast.Module(body=defs,type_ignores=[])
    data=sample();data.update(node_id='n',kind='topic',deep_analysis=True,analysis_level='deep',formal=True)
    data['claims'][0]['support_status']='partially_supported'
    data['evidence_status']='partially_supported'
    task={'status':'completed','checkpoint':{'selected_paths':['a.txt']}}
    if branch=='selected': task['checkpoint']['selection_plan']={'node_id':'n','selected_file_count':1}
    storage=Mock()
    storage.get_analysis.return_value={}
    storage.get_import_task.return_value=task
    storage.get_tree_page.return_value=[data] if branch=='tree' else []
    storage.list_summaries.return_value=[{'path':'a.txt','type':'deep_file_summary',
                                        'payload':{'deep_analysis':True}}]
    if branch=='saved': storage.list_summaries.return_value.append(
        {'path':'node:n','type':'deep_node_summary','payload':data})
    storage.get_summary.return_value=data
    storage.file_workflow_counts.return_value={'total':1}
    storage.package_processing_counts.return_value={}
    storage.list_tree_edits.return_value=[]
    storage.get_scan.return_value={'file_count':1}
    env={'storage':storage,'require_scan':lambda _:None,'jsonify':lambda x:x,
         '_published_deep_scope':lambda _:({'a.txt'},published),
         '_build_candidate_preview_directory':lambda _:{'topics':[data] if branch in {'selected','preview'} else []},
         '_effective_owner':lambda :'test','staged_summary_type':summary_type,
         'project_node_claims':project_node_claims,'hashlib':hashlib}
    exec(compile(module,'app.py','exec'),env)
    return env['formal_directory']('test')
