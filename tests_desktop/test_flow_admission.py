"""Cross-platform component tests. Static-only is an explicit TEST policy.

These prove admission orchestration, never production antivirus completeness.
All bytes, snapshots, jobs and databases belong to pytest's temporary folder.
"""
import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from sjfx_protection import business
from sjfx_protection.integration import install
from sjfx_protection.registration import PackageProtection
from sjfx_protection.store import ProtectionError, Store


@pytest.fixture
def flow(tmp_path,monkeypatch):
    monkeypatch.setenv('SJFX_PROTECTION_TEST_STATIC_ONLY','1')
    monkeypatch.setenv('SJFX_PROTECTION_CLAM','')
    monkeypatch.setenv('SJFX_PROTECTION_CLAMD_SOCKET','')
    root=tmp_path/'中文资料';root.mkdir()
    (root/'evidence.txt').write_text('normal research evidence',encoding='utf-8')
    p=PackageProtection(Store(tmp_path/'state'),[str(root)],
        authorize_owner=lambda owner,user: {'user_id':'user1','role':'user'} if owner=='alice' else False,
        authorize_root=lambda owner,path:owner=='alice')
    p.update_policy('admin',True,{'enabled':True,'inspection_profile':{'required_engines':['static']}},'Explicit isolated TEST profile; no real AV assertion')
    business.configure(SimpleNamespace(enabled=True,package=p,store=p.store))
    yield p,root
    business.configure(None)


def denied(code,fn,*args,**kwargs):
    with pytest.raises(ProtectionError) as error:fn(*args,**kwargs)
    assert error.value.code==code


def checked(p,root):
    receipt=p.begin_local('alice','user1',str(root))
    result=p.process_job(receipt['job_id'])
    assert result['state']=='completed',result
    return receipt,p.complete_local('alice',receipt)


def test_one_selection_is_metadata_only_until_async_check(flow,monkeypatch):
    p,root=flow
    monkeypatch.setattr(p,'process_job',lambda *a,**k:pytest.fail('HTTP must never run inventory'))
    monkeypatch.setattr(p,'local_paths',lambda *a:pytest.fail('HTTP must never enumerate contents'))
    options=business.capture_import('alice',str(root),{'parse_mode':'auto'})
    assert 'protection_context' not in options
    assert p.jobs.get('alice',options['protection_intake']['job_id'])['state']=='queued'
    denied('inspection_pending',p.complete_local,'alice',options['protection_intake'])


def test_normal_check_freezes_snapshot_and_durable_context(flow):
    p,root=flow;options=business.capture_import('alice',str(root),{})
    persisted=[]
    approved=business.admit_import('alice',str(root),options,persist=persisted.append)
    assert persisted==[approved]
    frozen=approved['protection_context']
    with business.activate(frozen):
        path=business.protected_path(root/'evidence.txt',operation='parse')
    assert path!=root/'evidence.txt' and path.read_bytes()==(root/'evidence.txt').read_bytes()
    reopened=PackageProtection(Store(p.store.directory),[str(root)],p.authorize_root,p.authorize_owner)
    assert reopened.complete_local('alice',options['protection_intake'])['versions']==frozen['versions']


@pytest.mark.parametrize('kind',['script','mail','unsupported'])
def test_dangerous_or_incomplete_package_never_reaches_parser(flow,kind):
    p,root=flow
    if kind=='script':(root/'bad.ps1').write_text('Write-Host fixture')
    elif kind=='mail':
        (root/'bad.eml').write_bytes(b'From: attacker@example.test\r\nTo: user@example.test\r\nSubject: Login\r\nMIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<a href="javascript:alert(1)">login</a>')
    else:(root/'outlook.msg').write_bytes(b'unsupported fixture')
    effects=[];options=business.capture_import('alice',str(root),{})
    with pytest.raises(ProtectionError) as error:
        business.admit_import('alice',str(root),options)
        effects.append('parse/index/model')
    assert error.value.code in {'dangerous_content','inspection_incomplete','risk_review_required'}
    assert effects==[] and p.status('alice',options['protection_intake']['registration_id'])['can_use'] is False


@pytest.mark.parametrize('mutation',['edit','add','replace','rename','remove'])
def test_change_invalidates_old_receipt_and_persists_recheck(flow,mutation):
    p,root=flow;receipt,frozen=checked(p,root)
    file=root/'evidence.txt'
    if mutation=='edit':file.write_text('new evidence')
    elif mutation=='add':(root/'new.txt').write_text('new member')
    elif mutation=='replace':
        replacement=root/'replacement.tmp';replacement.write_bytes(file.read_bytes());os.replace(replacement,file)
    elif mutation=='rename':file.rename(root/'renamed.txt')
    else:file.unlink();(root/'replacement.txt').write_text('remaining data')
    denied('content_changed',p.require_operation,'alice',registration_id=frozen['registration_id'],context=frozen)
    with p.store.db() as db:
        jobs=db.execute("SELECT id FROM protection_jobs WHERE registration_id=? AND state IN ('queued','running')",(frozen['registration_id'],)).fetchall()
    assert len(jobs)==1
    assert p.process_job(jobs[0]['id'])['state']=='completed'
    denied('context_stale',p.complete_local,'alice',receipt)
    denied('context_stale',p.require_operation,'alice',registration_id=frozen['registration_id'],context=frozen)
    fresh=p.begin_local('alice','user1',str(root))
    p.process_job(fresh['job_id'])
    assert p.complete_local('alice',fresh)['manifest_revision']>frozen['manifest_revision']


def test_lost_lease_restores_inventory_without_automatic_business_approval(flow):
    p,root=flow;receipt=p.begin_local('alice','user1',str(root))
    p.jobs.claim('dead-worker',receipt['job_id'],lease_seconds=10)
    with p.store.db(True) as db:db.execute('UPDATE protection_jobs SET lease_until=? WHERE id=?',(time.time()-1,receipt['job_id']))
    p=PackageProtection(Store(p.store.directory),[str(root)],p.authorize_root,p.authorize_owner)
    denied('inspection_pending',p.complete_local,'alice',receipt)
    assert p.process_job(receipt['job_id'])['state']=='completed'
    assert p.complete_local('alice',receipt)['manifest_revision']==1


def test_cancelled_inventory_and_legacy_work_fail_closed(flow):
    p,root=flow;receipt=p.begin_local('alice','user1',str(root))
    p.cancel('alice',receipt['registration_id'])
    denied('import_cancelled',p.complete_local,'alice',receipt)
    denied('protection_context_required',business.admit_import,'alice',str(root),{})


def test_cancel_business_before_side_effect_or_context_save(flow):
    p,root=flow;options=business.capture_import('alice',str(root),{});saved=[]
    denied('import_cancelled',business.admit_import,'alice',str(root),options,persist=saved.append,cancel=lambda:True)
    assert saved==[]


def test_approval_is_bound_to_actor_operation_and_checked_set(flow):
    p,root=flow;receipt,frozen=checked(p,root)
    denied('operation_approval_required',p.require_operation,'alice',root=str(root),operation='bulk_delete')
    request=p.request_approval('alice',frozen['registration_id'],'bulk_delete','isolated test')
    approval=p.approve('alice',request['id'],'admin',True,'fixture approval',is_admin=True)['approval_id']
    assert p.require_operation('alice',root=str(root),operation='bulk_delete',approval_id=approval)['state']=='allow'
    denied('operation_approval_required',p.require_operation,'alice',root=str(root),operation='delete',approval_id=approval)
    (root/'evidence.txt').write_text('updated before side effect')
    denied('content_changed',p.require_operation,'alice',root=str(root),operation='bulk_delete',context=frozen,approval_id=approval)


def test_parse_rule_applies_to_snapshot_and_derived_before_effect(flow):
    p,root=flow;p.update_policy('admin',True,{'operation_policy':{'parse':'deny'}},'fixture parser denial')
    receipt,frozen=checked(p,root);effects=[]
    with business.activate(frozen),pytest.raises(ProtectionError):
        with business.file_execution(root/'evidence.txt'):effects.append('parsed')
    assert effects==[]


def test_metadata_projection_contains_status_but_no_business_content():
    from sjfx_protection.metadata import sanitize_control,is_metadata_route
    assert is_metadata_route('/api/jobs/new','GET',b'protection_metadata=1')
    result=sanitize_control({'ok':True,'job':{'id':'new','stage':'security_inspection','message':'pending','result':{'body':'SECRET'},'options':{'root':'secret'}}})
    assert result=={'ok':True,'job':{'id':'new','stage':'security_inspection','message':'pending'}}


def test_http_import_does_not_inspect_content(flow,monkeypatch):
    p,root=flow;app=FastAPI()
    protected=install(app,state_dir=p.store.directory,allowed_roots=[str(root)],
        session_resolver=lambda sid:{'owner_id':'alice','user_id':'user1','role':'user','must_change_pw':False},
        authorize_root=p.authorize_root,authorize_owner=p.authorize_owner,
        resource_resolver=lambda owner,path,method,query,data:[str(root)])
    monkeypatch.setattr(protected,'ensure_executor',lambda:None)
    monkeypatch.setattr(protected.package,'prepare_local',lambda *a:pytest.fail('synchronous middleware inspection'))
    @app.post('/api/scan')
    def submit():return {'ok':True,'accepted':True}
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            return await client.post('/api/scan',json={'path':str(root)},headers={'Cookie':'sjfx_session=fixture'})
    response=asyncio.run(request())
    assert response.status_code==200,response.text
    assert response.json()['accepted'] is True


@pytest.fixture
def runtime(flow,monkeypatch,tmp_path):
    from config import Config
    p,root=flow
    monkeypatch.setattr(Config,'DATA_DIR',tmp_path/'runtime')
    monkeypatch.setattr(Config,'DB_PATH',tmp_path/'runtime'/'sjfx.db')
    monkeypatch.setattr(Config,'OUTPUT_DIR',tmp_path/'outputs')
    monkeypatch.setattr(Config,'SCAN_ALLOWED_ROOTS',(root,))
    return p,root,Config


def test_real_ordinary_queue_persists_admission_before_worker_reads(runtime):
    from services.storage import Storage
    p,root,config=runtime;storage=Storage(config.DB_PATH)
    jid=storage.create_scan_job(str(root),100,'auto',10,owner_id='alice')
    assert storage.get_job(jid)['stage']=='security_inspection'
    job=storage.claim_next_job('fixture-worker')
    assert job['id']==jid and not job['options'].get('protection_context')
    effects=[]
    with business.job_execution(job):
        stored=storage.get_job(jid)['options']['protection_context']
        assert stored['registration_id']==job['options']['protection_intake']['registration_id']
        with business.file_execution(root/'evidence.txt') as snapshot:effects.append(snapshot.read_text())
    assert effects==['normal research evidence']
    assert p.store.verify_chain()['ok']


def test_real_ordinary_queue_refuses_dangerous_before_worker_body(runtime):
    from services.storage import Storage
    p,root,config=runtime;(root/'attack.ps1').write_text('Write-Host fixture')
    storage=Storage(config.DB_PATH);jid=storage.create_scan_job(str(root),100,'auto',10,owner_id='alice')
    effects=[]
    with pytest.raises(ProtectionError):
        with business.job_execution(storage.get_job(jid)):effects.append('parser/index/model')
    assert effects==[] and not storage.get_job(jid)['options'].get('protection_context')


def test_real_ordinary_restored_job_does_not_relabel_old_results(runtime):
    from services.storage import Storage
    p,root,config=runtime;storage=Storage(config.DB_PATH)
    jid=storage.create_scan_job(str(root),100,'auto',10,owner_id='alice')
    with business.job_execution(storage.get_job(jid)):pass
    old=storage.get_job(jid)['options']['protection_context']
    (root/'evidence.txt').write_text('changed after work')
    with pytest.raises(ProtectionError):
        with business.job_execution(storage.get_job(jid)):pytest.fail('old task reached business')
    assert storage.get_job(jid)['options']['protection_context']==old


def test_real_large_package_waits_for_complete_set(runtime):
    from services.large_package_isolated import LargePackageStore
    from services.large_package_protection import large_package_execution
    p,root,config=runtime;store=LargePackageStore(config.DATA_DIR/'large_package.db')
    package=store.create(str(root),'alice')
    assert package['protection_intake'] and not package['protection_context']
    denied('inspection_pending',store.admit_protection,package['id'])
    assert store.counts(package['id'])['total_files']==0
    assert p.process_job(package['protection_intake']['job_id'])['state']=='completed'
    with large_package_execution(store,package['id']):
        assert business.context()['registration_id']==package['protection_intake']['registration_id']
    assert store.get(package['id'])['protection_context']['manifest_revision']==1


def test_real_large_package_dangerous_set_stays_unusable(runtime):
    from services.large_package_isolated import LargePackageStore
    p,root,config=runtime;(root/'attack.ps1').write_text('Write-Host fixture')
    store=LargePackageStore(config.DATA_DIR/'large_package.db');package=store.create(str(root),'alice')
    p.process_job(package['protection_intake']['job_id'])
    denied('dangerous_content',store.admit_protection,package['id'])
    assert not store.get(package['id'])['protection_context']
    assert store.counts(package['id'])['total_files']==0


def test_no_model_modules_are_loaded_by_import_protection(flow):
    import sys
    p,root=flow;before=set(sys.modules)
    options=business.capture_import('alice',str(root),{})
    business.admit_import('alice',str(root),options)
    loaded=set(sys.modules)-before
    assert not loaded.intersection({'app','transformers','torch','sentence_transformers','llama_cpp'})


def test_pending_queue_can_cancel_and_never_reads_business(runtime):
    from services.storage import Storage
    p,root,config=runtime;storage=Storage(config.DB_PATH)
    jid=storage.create_scan_job(str(root),100,'auto',10,owner_id='alice')
    cancelled=storage.cancel_job(jid)
    assert cancelled['status']=='cancelled'
    assert storage.claim_next_job('fixture-worker') is None
    assert storage.get_job(jid)['options'].get('protection_context') is None


@pytest.mark.parametrize('dangerous',[False,True])
def test_real_mail_queue_admits_version_before_message_parser(runtime,dangerous):
    from services.storage import Storage
    from services.mail.repository import MailRepository
    p,root,config=runtime
    message=root/'letter.eml'
    message.write_bytes(b'From: author@example.test\r\nTo: user@example.test\r\nSubject: Research\r\nMIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nNormal project evidence.')
    if dangerous:
        message.write_bytes(b'From: attacker@example.test\r\nTo: user@example.test\r\nSubject: Login\r\nMIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<a href="javascript:alert(1)">login</a>')
    storage=Storage(config.DB_PATH);repository=MailRepository(storage);repository.migrate()
    captured=business.capture_import('alice',str(message),{})
    package,created=repository.create_package(owner_id='alice',display_name='fixture',source_kind='eml',source_path=str(message),source_revision='pending-inspection',config=captured)
    jid,_=storage.create_or_get_typed_job(package['package_id'],'mail_import',{'package_id':package['package_id']},'alice')
    job=storage.claim_next_job('fixture-worker');assert job['id']==jid
    effects=[]
    def execute():
        with business.job_execution(job):
            with business.file_execution(message) as snapshot:effects.append(snapshot.read_bytes())
    if dangerous:
        with pytest.raises(ProtectionError):execute()
        assert effects==[]
    else:
        execute()
        assert effects==[message.read_bytes()]
        assert repository.get_package(package['package_id'],'alice')['config']['protection_context']['manifest_revision']==1


@pytest.mark.parametrize('value',[float('nan'),float('inf'),-1,0,True])
def test_policy_capacity_rejects_nonfinite_or_invalid_numbers(flow,value):
    p,root=flow
    with pytest.raises(ProtectionError):p.update_policy('admin',True,{'inspection_profile':{'hash_max_bytes':value}},'fixture invalid capacity')


def test_new_selection_rechecks_ready_source_without_relabeling_same_versions(flow):
    p,root=flow;first,frozen=checked(p,root)
    fresh=p.begin_local('alice','user1',str(root))
    assert fresh['job_id']!=first['job_id']
    denied('inspection_pending',p.complete_local,'alice',fresh)
    p.process_job(fresh['job_id'])
    assert p.complete_local('alice',fresh)['manifest_revision']==frozen['manifest_revision']
    assert p.require_operation('alice',registration_id=frozen['registration_id'],context=frozen)['state']=='allow'


def test_revocation_still_persists_failed_result_without_masking_original_error(flow):
    p,root=flow;receipt,frozen=checked(p,root)
    with pytest.raises(ProtectionError) as error:
        with business.saved_execution('alice',[str(root)],frozen,'read'):
            p.authorize_owner=lambda owner,user:False
            raise ProtectionError('original failure',409,'original_failure')
    assert error.value.code=='original_failure'
    with p.store.db() as db:row=db.execute("SELECT payload FROM events WHERE action='operation.read' ORDER BY seq DESC LIMIT 1").fetchone()
    assert json.loads(row[0])['payload']['result']=='failed'


def test_http_does_not_send_result_if_source_changes_before_response(flow,monkeypatch):
    p,root=flow;checked(p,root);app=FastAPI()
    protected=install(app,state_dir=p.store.directory,allowed_roots=[str(root)],
        session_resolver=lambda sid:{'owner_id':'alice','user_id':'user1','role':'user','must_change_pw':False},
        authorize_root=p.authorize_root,authorize_owner=p.authorize_owner,
        resource_resolver=lambda owner,path,method,query,data:[str(root)])
    monkeypatch.setattr(protected,'ensure_executor',lambda:None)
    @app.get('/api/document/fixture')
    def document():
        (root/'evidence.txt').write_text('unapproved changed source')
        return {'ok':True,'text':'SECRET business result'}
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            return await client.get('/api/document/fixture',headers={'Cookie':'sjfx_session=fixture'})
    response=asyncio.run(request())
    assert response.status_code==423,response.text
    assert response.json()['code']=='content_changed'
    assert 'SECRET' not in response.text


def test_static_only_is_never_production_admission(flow,monkeypatch):
    p,root=flow;receipt,frozen=checked(p,root)
    monkeypatch.delenv('SJFX_PROTECTION_TEST_STATIC_ONLY')
    denied('inspection_incomplete',p.require_operation,'alice',registration_id=frozen['registration_id'])
    denied('test_profile_required',p.update_policy,'admin',True,{'inspection_profile':{'required_engines':['static']}},'cannot deploy synthetic profile')
