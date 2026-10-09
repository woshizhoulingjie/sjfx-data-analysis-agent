"""Physical key recovery never classifies, borrows sources or certifies completeness."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.mail.research.relevance import classify, restore_missing_message_keys


def mapping():
    messages=[{'message_key':'m1'},{'message_key':'m2'}]
    sources={'m1':{'e1':{'quote_text':'项目甲的价格为 90,000.00 元。'}},
             'm2':{'e2':{'quote_text':'项目乙约定6月12日交付。'}}}
    return messages,sources


@pytest.mark.parametrize('status',['matched','not_matched','unknown'])
def test_missing_key_restores_only_known_physical_identity_for_every_status(status):
    messages,sources=mapping()
    rows=[{'status':status,'evidence_id':'e1','source_quote':'价格为 90,000.00 元','rationale':'原判定保留'}]
    before=deepcopy(rows);restored,audit=restore_missing_message_keys(rows,messages,sources)
    assert rows==before and restored==[{**before[0],'message_key':'m1'}]
    assert restored[0]['status']==status and audit[0]['original_decision']==before[0]


@pytest.mark.parametrize('failure',['foreign','previous_partition','ambiguous_id','nonstring_id',
    'blank_quote','changed_spaces','noncontiguous','nonstring_quote','existing_conflict','existing_null'])
def test_ambiguous_foreign_unbound_and_existing_keys_are_never_rewritten(failure):
    messages,sources=mapping();row={'status':'matched','evidence_id':'e1','source_quote':'价格为 90,000.00 元'}
    if failure=='foreign':row['evidence_id']='not-supplied'
    elif failure=='previous_partition':messages=[messages[1]]
    elif failure=='ambiguous_id':sources['m2']['e1']=deepcopy(sources['m1']['e1'])
    elif failure=='nonstring_id':row['evidence_id']=['e1']
    elif failure=='blank_quote':row['source_quote']='  '
    elif failure=='changed_spaces':row['source_quote']='价格为90,000.00元'
    elif failure=='noncontiguous':row['source_quote']='项目甲的价格元'
    elif failure=='nonstring_quote':row['source_quote']={}
    elif failure=='existing_conflict':row['message_key']='m2'
    else:row['message_key']=None
    before=deepcopy(row);restored,audit=restore_missing_message_keys([row],messages,sources)
    assert row==before and restored==[before] and not audit


class Repository:
    def __init__(self,messages,partial=False):self.messages=messages;self.partial=partial;self.events=[]
    @contextmanager
    def _connection(self):
        def execute(sql,args):
            if sql.startswith('SELECT *'):
                return [{'message_key':args[1],'source_kind':'body' if i==0 else 'attachment','mime_path':str(i+1),
                    'text':source['text'],'evidence_id':source['evidence_id']}
                    for i,source in enumerate(self.messages[args[1]]['evidence'])]
            return SimpleNamespace(fetchone=lambda:(1 if self.partial and 'mail_part_processing' in sql else 0,))
        yield SimpleNamespace(execute=execute)
    def create_evidence(self,pid,owner,row):return {'evidence_id':row['evidence_id'],'quote_text':row['text']}
    def event(self,*args):self.events.append(args)
    def record_research_step(self,*args,**kwargs):pass


def classify_rows(messages,response,*,partial=False,budget=8):
    repository=Repository({m['message_key']:m for m in messages},partial);calls=[]
    state={'phase2':True,'max_model_calls':budget,'model_calls':0}
    def model(*args,**kwargs):
        state['model_calls']+=1;calls.append(json.loads(args[5]));return deepcopy(response[len(calls)-1] if isinstance(response,list) else response)
    runtime=SimpleNamespace(repository=repository,context_limit=24000,_model_call=model)
    candidates=[{'message_key':m['message_key'],'evidence_id':'retrieved-'+m['message_key']} for m in messages]
    result=classify(runtime,'t','o',state,{'package_id':'p','objective':'项目甲交付是哪一天','scope':{}},candidates)
    return state,result,calls,repository.events


def messages():
    return [{'message_key':'m1','evidence':[{'evidence_id':'e1','text':'项目甲约定6月12日交付。'}]},
            {'message_key':'m2','evidence':[{'evidence_id':'e2','text':'项目乙报价200元。'}]}]


def test_recovered_not_matched_does_not_override_incomplete_text_unknown_gate():
    response={'decisions':[{'status':'not_matched','evidence_id':'e1','source_quote':'项目甲'}]}
    state,result,calls,events=classify_rows(messages()[:1],response,partial=True)
    assert len(calls)==1 and calls[0]['messages'][0]['complete_text'] is False
    assert state['relevance_decisions']['m1']['status']=='unknown' and result
    assert state['relevance_identity_recoveries'][0]['original_output']==response
    assert any(event[1]=='relevance_message_key_recovered' for event in events)


@pytest.mark.parametrize('status',['matched','not_matched','unknown'])
def test_existing_key_for_other_supplied_evidence_stays_conflicting_and_unknown(status):
    response={'decisions':[{'message_key':'m1','status':status,'evidence_id':'e2','source_quote':'项目乙报价200元。'}]}
    state,result,calls,_=classify_rows(messages(),response)
    assert all(row['status']=='unknown' for row in state['relevance_decisions'].values()) and len(result)==2
    assert not state.get('relevance_identity_recoveries') and len(calls)==2
    assert calls[1]['previous_output']==response and response['decisions'][0]['message_key']=='m1'


def test_duplicate_recovered_rows_remain_unknown_instead_of_arbitrary_selection():
    row={'status':'matched','evidence_id':'e1','source_quote':'项目甲'}
    state,result,calls,_=classify_rows(messages()[:1],{'decisions':[row,deepcopy(row)]},budget=3)
    assert len(calls)==1 and state['relevance_decisions']['m1']['status']=='unknown' and result


def test_missing_key_in_existing_quote_repair_can_be_recovered_without_touching_valid_decisions():
    initial={'decisions':[{'message_key':'m1','status':'matched','evidence_id':'e1','source_quote':'项目甲'},
        {'status':'matched','evidence_id':'e2','source_quote':'项目乙 报价200元。'}]}
    repaired={'decisions':[{'status':'not_matched','evidence_id':'e1','source_quote':'项目甲'},
        {'status':'matched','evidence_id':'e2','source_quote':'项目乙报价200元。'}]}
    before=deepcopy([initial,repaired]);state,result,calls,_=classify_rows(messages(),[initial,repaired])
    assert [initial,repaired]==before and len(calls)==2 and len(result)==2
    assert all(row['status']=='matched' for row in state['relevance_decisions'].values())
    assert all(audit['phase']=='repair' for audit in state['relevance_identity_recoveries'])
    assert all(audit['original_output']==repaired for audit in state['relevance_identity_recoveries'])


def test_actual_v5_partition_restores_all_three_not_matched_without_model_repair_or_unknown():
    fixture=json.loads((Path(__file__).parent/'fixtures'/'mail_relevance_v5_missing_key.json').read_text(encoding='utf-8'))
    supplied=fixture['payload']['messages'];raw=fixture['response'];before=deepcopy(raw)
    assert len(supplied)==3 and max(len(m['evidence']) for m in supplied)==19
    assert all(m['complete_text'] for m in supplied) and all('message_key' not in r for r in raw['decisions'])
    state,result,calls,events=classify_rows(supplied,raw)
    assert raw==before and len(calls)==1 and not result and state['analysis_anchor_keys']==[]
    assert all(row['status']=='not_matched' for row in state['relevance_decisions'].values())
    assert len(state['relevance_identity_recoveries'])==3
    assert all(audit['original_output']==before for audit in state['relevance_identity_recoveries'])
    assert not any(event[1]=='relevance_validation_failed' for event in events)
