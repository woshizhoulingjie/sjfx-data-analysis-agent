import json
from contextlib import closing
from unittest.mock import Mock, patch
import numpy as np
import pytest
import test_catalog_next_stage as hierarchy_fixture
import test_large_catalog_import as bridge_fixture
from services import large_package_catalog_tree as tree
from services import large_package_catalog_naming as naming

def test_three_levels_below_old_twelve_branch_cutoff():
    case = hierarchy_fixture.NextStageTests(); case.setUp()
    try:
        for i in range(40):
            v = np.ones(8, dtype=np.float32); v[i // 10] += 1.5
            case.add('g%03d' % i, v)
        rnd = naming.ensure_round(case.store, case.pid)
        with patch('services.large_package_catalog_topics.name_topics', side_effect=lambda _s,_p,b,*a:{k:'设备运行监测技术' for k in b}):
            assert tree.build(case.store, case.pid, rnd, model_client=Mock())
        with closing(case.store._connect()) as c:
            branches = tree.visible(c, case.pid)
        by_id = {b['node_id']:b for b in branches}
        roots = [b for b in branches if not b.get('parent_id')]
        assert any(b.get('depth') == 1 for b in branches)
        def walk(node, ancestors=()):
            assert node['node_id'] not in ancestors
            found=[]
            for child in node['child_nodes']:
                if child in by_id:
                    assert by_id[child]['parent_id'] == node['node_id']
                    found.extend(walk(by_id[child], ancestors+(node['node_id'],)))
                else: found.append(child)
            assert sorted(found)==sorted(node['children'])
            assert len(found)==len(set(found))
            return found
        found=[leaf for root in roots for leaf in walk(root)]
        assert len(found)==len(set(found))==40
        assert max(b.get('depth',0) for b in branches)==1
    finally: case.doCleanups()

@pytest.mark.parametrize('legacy', [False, True])
def test_bridge_executes_ordinary_preliminary_pipeline(monkeypatch, legacy):
    import app as web
    case = bridge_fixture.LargeCatalogImportTests(); case.setUp()
    try:
        store=case.ordinary
        monkeypatch.setattr(web,'storage',store)
        monkeypatch.setattr(web.Config,'ENABLE_TRANSLATION',False)
        monkeypatch.setattr(web.Config,'ENABLE_IMPORT_TRANSLATION',False)
        monkeypatch.setattr(web,'_publish_analysis_progress',lambda *a,**k:None)
        monkeypatch.setattr(web,'_package_large_options',lambda:{})
        overviews=[]; seen=[]
        monkeypatch.setattr(web,'_write_local_overview',lambda *a,**k:overviews.append(k['summary_stage']))
        def analyze(scan_id, scan, storage, parser, progress, **kwargs):
            assert kwargs['workflow_source']=='selected_file_parse'
            seen.extend(kwargs['target_paths'])
            for path in kwargs['target_paths']:
                storage.save_document(scan_id,path,{'text':'设备状态记录 / operating status / รายงานสถานะ','source':{'path':path},'evidence':[]})
            analysis={'statistics':{},'analysis_tree':{'kind':'analysis_root','node_id':'root','children':[]},'workflow':{},'coverage':{}}
            storage.save_analysis(scan_id,analysis)
            return analysis
        monkeypatch.setattr(web,'analyze_package',analyze)
        result=case._create(case._export(node_ids=[bridge_fixture.GROUP_A,bridge_fixture.GROUP_B]))
        job=store.claim_next_job('catalog-flow-test')
        assert job['id']==result['job_id']
        if legacy:job['options']['workflow_source']='large_catalog_selection'
        output=web._run_claimed_analysis_job(job)
        jobs=[j for j in store.list_jobs(owner_id='o1') if j['task_type']=='generate_summary']
        assert jobs and len(jobs)==len(seen)
        assert {j['options']['path'] for j in jobs}==set(seen)
        assert all(j['options']['workflow_source']=='preliminary_model_summary' for j in jobs)
        assert all(j['options']['analysis_level']=='preview' for j in jobs)
        assert all(not j['options'].get('deep_batch_id') for j in jobs)
        assert overviews==['parsed']
        task=store.get_import_task(result['scan_id'])
        assert task['status']=='preliminary_summarizing'
        assert set(task['checkpoint']['preliminary_summary_job_ids'])=={j['id'] for j in jobs}
        assert output['preliminary_summary_job_ids']
    finally:case.tearDown()

def test_attached_leaf_is_visible_selectable_and_counted():
    case=hierarchy_fixture.NextStageTests();case.setUp()
    try:
        for i,v in enumerate(([1,0],[1,0],[.7,.714])):
            case.add('g%d'%i,v)
        rnd=naming.ensure_round(case.store,case.pid)
        with patch('services.large_package_semantic._kmeans_fit',return_value=np.eye(2)),patch('services.large_package_catalog_topics.name_topics',side_effect=lambda _s,_p,b,*a:{k:'设备运行监测技术' for k in b}):
            assert tree.build(case.store,case.pid,rnd,model_client=Mock())
        with closing(case.store._connect()) as c:branches=tree.visible(c,case.pid)
        root=next(b for b in branches if b['node_id'].startswith('topic:'))
        assert set(root['children'])==set(root['child_nodes'])=={'g0','g1','g2'}
        assert root['files']==3
        assert case.store.file_page(case.pid,node_id=root['node_id'])['total']==3
    finally:case.doCleanups()
