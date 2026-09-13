import csv
import hashlib
import time
from pathlib import Path
from services.structured_profile import profile_path, _date
from services.tabular_document import project_table
from services.storage import Storage


def project(path, max_rows=None):
    profile=profile_path(path,max_rows=max_rows)
    base={'source':{'path':path.name,'name':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()},'parser':{},'structure':{},'coverage':{},'warnings':[]}
    return project_table(path,base,profile)


def test_complete_table_aggregates_zero_missing_negative_and_quoted_newline(tmp_path):
    p=tmp_path/'observations.csv'
    with p.open('w',newline='') as f:
        w=csv.writer(f);w.writerow(['EVENT_ID','GROUP','COUNT','NOTES']);w.writerows([['001','A',10,'two\nlines'],['002','A',0,'zero'],['003','B','','missing'],['004','B',-2,'negative']])
    d=project(p)
    assert d['coverage']['complete'] is True
    assert d['coverage']['model_full_narrative_review'] is False
    assert d['parser']['record_count']==4
    assert d['data_profile']['columns']['COUNT']['sum']==8
    assert d['data_profile']['columns']['COUNT']['missing_count']==1
    assert 'EVENT_ID' not in d['data_profile']['grouped_statistics_scope']['measures']
    groups={r['value']:r for r in d['data_profile']['grouped_statistics']['GROUP']}
    assert groups['A']['rows']==2 and groups['A']['sums']['COUNT']==10
    assert groups['B']['rows']==2 and groups['B']['numeric_counts']['COUNT']==1
    last=next(e for e in d['evidence'] if e.get('record_index')==4)
    assert last['row_range']==[5,5] and '004' in last['text']
    assert '漏洞' not in d['text']


def test_profile_limit_never_becomes_whole_table_coverage(tmp_path):
    p=tmp_path/'limited.csv';p.write_text('GROUP,COUNT\nA,1\nB,2\nC,30\n')
    d=project(p,max_rows=2)
    assert d['coverage']['complete'] is False
    assert d['data_profile']['columns']['COUNT']['sum']==3
    assert d['data_profile']['grouped_statistics_scope']['complete'] is False
    assert not any(e.get('record_index',0)>2 for e in d['evidence'])


def test_dates_and_narrative_values_keep_existing_meaning():
    assert _date('2024-02-29')=='2024-02-29T00:00:00'
    assert _date('2023-02-29') is None
    assert _date('long narrative describing 2024-02-29 elsewhere') is None
    assert _date('2024/02/29 12:30:04')=='2024-02-29T12:30:04'


def test_due_retry_scheduler_queues_once_and_respects_future(tmp_path):
    s=Storage(tmp_path/'state.db')
    sid=s.save_scan({'root':str(tmp_path),'tree':{'kind':'directory','name':'data','path':'.','children':[]},'file_count':2})
    with s.lock, s._connect() as conn:
        conn.executemany(
            "INSERT INTO file_analysis_states(scan_id,node_path,fingerprint,status,error,retryable,attempt_count,next_retry_at) VALUES (?,?,?,?,?,?,?,?)",
            [(sid,'due.txt','v','failed','transient',1,1,time.time()-1),
             (sid,'future.txt','v','failed','transient',1,1,time.time()+3600)],
        )
    assert s.wake_due_file_retries()==1
    jobs=s.list_jobs(statuses=['queued'])
    assert len(jobs)==1 and jobs[0]['options']['target_paths']==['due.txt']
    assert s.wake_due_file_retries()==0


def test_model_hallucinated_number_and_cause_cannot_enter_factual_table_summary(tmp_path):
    from services.tabular_document import grounded_table_summary
    p=tmp_path/'hazards.csv';p.write_text('EVENT_TYPE,DEATHS_DIRECT,INJURIES_DIRECT\nWildfire,102,32\nStrong Wind,1,0\n')
    d=project(p)
    result=grounded_table_summary({'title':'事件统计','core_summary':'999人死亡，主要由强风导致。','conclusions':['999人死亡']},d)
    assert '999' not in result['core_summary']
    assert '强风导致' not in result['core_summary']
    assert '103' in result['core_summary'] and '直接受伤人数' in result['core_summary']
    assert any('Wildfire' in x and '102' in x for x in result['conclusions'])
    assert '999' in result['unverified_model_analysis']['core_summary']
    assert result['summary_provenance']['factual_text']=='deterministic_statistics'


def test_related_tables_are_not_combined_as_event_total(tmp_path):
    from services.tabular_document import grounded_group_summary
    one=tmp_path/'events_2022.csv';one.write_text('EVENT_ID,EVENT_TYPE,DEATHS_DIRECT\n1,Heat,2\n2,Heat,3\n')
    two=tmp_path/'events_2023.csv';two.write_text('EVENT_ID,EVENT_TYPE,DEATHS_DIRECT\n3,Heat,4\n')
    related=tmp_path/'fatalities.csv';related.write_text('EVENT_ID,FATALITY_ID\n1,10\n1,11\n')
    out=grounded_group_summary({'core_summary':'5 events and 18 deaths'}, {p.name:project(p) for p in [one,two,related]})
    assert len(out['table_group_scopes'])==2
    assert sorted(s['row_count'] for s in out['table_group_scopes'])==[2,3]
    assert any('合计 9' in x for x in out['key_facts'])
    assert '18 deaths' not in out['core_summary']


def test_node_input_budget_preserves_each_file_and_valid_json():
    import json
    from services.folder_analysis import _bounded_node_inputs
    files=[{'path':f'file-{i}.csv','summary':'完整句子，数值为12345。'*500,'key_facts':['DEATHS_DIRECT=875']*4} for i in range(13)]
    evidence=[{'source_path':f'file-{i%13}.csv','evidence_id':f'E-{i}','text':'统计证据。'*1000} for i in range(100)]
    summaries,clusters,chosen=_bounded_node_inputs(files,[],evidence)
    assert len(summaries)==13
    assert {f['path'] for f in summaries}=={f['path'] for f in files}
    assert len(json.dumps([summaries,clusters,chosen],ensure_ascii=False).encode())<=22000
    assert all(e['evidence_id'] in {x['evidence_id'] for x in evidence} for e in chosen)


def test_unrelated_column_with_majority_missing_does_not_block_numeric_total(tmp_path):
    from services.structured_qa import answer_question
    a=tmp_path/'year1.csv';a.write_text('MAGNITUDE,DEATHS_DIRECT\n,1\n,2\n20,3\n')
    b=tmp_path/'year2.csv';b.write_text('MAGNITUDE,DEATHS_DIRECT\n10,4\n20,5\n30,6\n')
    docs=[{'path':p.name,'payload':{'data_profile':profile_path(p)}} for p in [a,b]]
    assert docs[0]['payload']['data_profile']['columns']['MAGNITUDE']['inferred_type']=='missing'
    result=answer_question('DEATHS_DIRECT 合计是多少？',docs)
    assert result['value']==21 and result['aggregation_scope']['participating_source_count']==2


def test_empty_non_target_column_is_schema_unknown_not_conflict(tmp_path):
    from services.structured_qa import answer_question
    a=tmp_path/'a.csv';a.write_text('CATEGORY,DEATHS_DIRECT\n2,3\n1,4\n')
    b=tmp_path/'b.csv';b.write_text('CATEGORY,DEATHS_DIRECT\n,5\n,6\n')
    docs=[{'path':p.name,'payload':{'data_profile':profile_path(p)}} for p in [a,b]]
    assert answer_question('DEATHS_DIRECT 合计',docs)['value']==18


def test_report_closes_only_current_scope_with_healthy_quality():
    import copy
    from services.import_state_machine import deep_report_outcome
    task={'checkpoint':{'selected_paths':['a.csv'],'selection_version':2}}
    report={'report_scope':{'selected_paths':['a.csv'],'selection_version':2},'coverage':{'pipeline_coverage':{'content_parse':{'complete':True},'deep_analysis':{'complete':True}}},'summary_quality':{'stage':'deep','file_summaries':1,'degraded_file_summaries':0,'degraded_node_summaries':0},'model_analysis':{'status':'completed'}}
    assert deep_report_outcome(task,report)=='completed'
    degraded=copy.deepcopy(report);degraded['summary_quality']['degraded_node_summaries']=1
    assert deep_report_outcome(task,degraded)=='partial'
    stale=copy.deepcopy(report);stale['report_scope']['selection_version']=1
    assert deep_report_outcome(task,stale) is None
    changed=copy.deepcopy(report);changed['report_scope']['selected_paths']=['other.csv']
    assert deep_report_outcome(task,changed) is None


def test_paged_inventory_supplies_formats_and_table_years():
    from services.package_overview import build_package_overview_from_storage
    class Store:
        def get_scan(self,sid):return {'root':'/source','inventory_mode':'durable_paged_v1','file_count':2,'total_size':30,'directory_count':0,'tree':{'kind':'directory','path':'.','children':[]}}
        def iter_inventory_entries(self,*args,**kwargs):
            return iter([{'path':'a.csv','payload':{'path':'a.csv','extension':'.csv','size':10,'kind':'file'}},{'path':'guide.pdf','payload':{'path':'guide.pdf','extension':'.pdf','size':20,'kind':'file'}}])
        def iter_documents(self,*args,**kwargs):
            return iter([{'path':'a.csv','payload':{'source':{'path':'a.csv'},'data_profile':{'columns':{'YEAR':{'min':2022,'max':2025}}}}}])
        def get_analysis(self,*args):return None
    result=build_package_overview_from_storage(Store(),'s')
    assert result['package']['file_count']==2
    assert {i['format']:i['file_count'] for i in result['formats']['items']}=={'.csv':1,'.pdf':1}
    assert {i['period'] for i in result['timeline']['document_dates']['items']}=={'2022','2025'}
