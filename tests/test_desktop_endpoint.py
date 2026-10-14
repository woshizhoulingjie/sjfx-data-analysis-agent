from pathlib import Path
import json
import os
import threading
from http.server import ThreadingHTTPServer
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from types import SimpleNamespace
import pytest
from sjfx_protection.linux_observer import Observer,snapshot,manifest_digest
from sjfx_protection.desktop_audit import parse_auditd_records
from sjfx_protection import desktop_runtime


def fixture(tmp_path, file_root=False):
    root=tmp_path/'selected';root.mkdir();source=root/'ordinary.txt';source.write_text('stable bytes')
    token=tmp_path/'token';token.write_text('11'*32);token.chmod(0o600)
    helper=Observer(tmp_path/'formal-helper',token)
    info=snapshot(source)
    file={'binding_id':'local:file','file_id':'file','local_path':str(source),**info,'version_id':info['sha256']}
    registration={'device_id':'formal-device','owner_id':'owner','registration_id':'reg','manifest_revision':1,'gap_epoch':0,'local_root':str(source if file_root else root),'files':[file]}
    return helper,registration,source,token


def sync_request(**updates):
    return {'device_id':'formal-device','protection_enabled':True,'ack_event_ids':[],'ack_result_ids':[],'actions':[],**updates}


def test_single_file_complete_registration_and_owner_rebind_denied(tmp_path):
    helper,registration,source,token=fixture(tmp_path,True)
    assert helper.register(registration)['coverage_complete'] is True
    with pytest.raises(ValueError): helper.register({**registration,'owner_id':'other'})
    assert helper.bindings()[0]['owner_id']=='owner'
    helper.db.close()


def test_atomic_registration_version_mismatch_leaves_no_partial_bindings(tmp_path):
    helper,registration,source,token=fixture(tmp_path)
    second=source.parent/'second.txt';second.write_text('second')
    member={**registration['files'][0],'binding_id':'local:second','file_id':'second','local_path':str(second)}
    with pytest.raises(ValueError): helper.register({**registration,'files':registration['files']+[member]})
    assert helper.bindings()==[]
    helper.db.close()


def test_observed_change_queue_ack_and_restart_gap_are_durable(tmp_path):
    helper,registration,source,token=fixture(tmp_path)
    helper.register(registration);helper.sync(sync_request());source.write_text('new version')
    response=helper.sync(sync_request());events=response['events']
    assert any(e['action']=='file.modified' for e in events)
    helper.db.close();restarted=Observer(tmp_path/'formal-helper',token)
    assert restarted.status()['has_unresolved_gaps'] is True
    assert restarted.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]>=1
    with pytest.raises(ValueError): restarted.register(registration)
    current=snapshot(source)
    registration['gap_epoch']=1;registration['files'][0].update(current,version_id=current['sha256'])
    restarted.register(registration)
    assert restarted.status()['has_unresolved_gaps'] is False
    restarted.sync(sync_request(ack_event_ids=[e['event_id'] for e in events]))
    assert restarted.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==0
    restarted.db.close()


def test_linux_unimplemented_action_is_rejected_without_side_effect(tmp_path):
    helper,registration,source,token=fixture(tmp_path)
    helper.register(registration)
    response=helper.sync(sync_request(actions=[{'id':'frozen-action','kind':'quarantine'}]))
    assert response['results'][0]['result']=={'status':'rejected','reason':'unsupported_linux_observer_action','performed':False}
    assert source.read_text()=='stable bytes'
    with pytest.raises(ValueError): helper.sync(sync_request(actions=[{'id':'frozen-action','kind':'read_lock'}]))
    helper.db.close()


def test_loopback_auth_origin_and_health_summary(tmp_path):
    helper,registration,source,token=fixture(tmp_path)
    server=ThreadingHTTPServer(('127.0.0.1',0),helper.handler());threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        url=f'http://127.0.0.1:{server.server_port}/agent-local/v1/health'
        headers={'Content-Type':'application/json','Authorization':'Bearer '+token.read_text()}
        with urlopen(Request(url,data=b'{}',headers=headers)) as response: health=json.load(response)
        assert health['access_audit_healthy'] is False and 'bindings' not in health
        for rejected in ({**headers,'Origin':'http://localhost'},{**headers,'Authorization':'Bearer wrong'}):
            with pytest.raises(HTTPError) as error: urlopen(Request(url,data=b'{}',headers=rejected))
            assert error.value.code==403
    finally: server.shutdown();server.server_close();helper.db.close()


def test_native_auditd_evidence_preserves_pid_user_permissions_without_upload_claim(tmp_path):
    helper,registration,source,token=fixture(tmp_path);helper.register(registration)
    lines=[f'type=SYSCALL msg=audit(1700000000.123:42): arch=c000003e syscall=257 success=yes exit=3 pid=99 uid=1000 auid=1000 exe="/usr/bin/word-reader" key="sjfx_test" perm=r',
           f'type=PATH msg=audit(1700000000.123:42): item=0 name="{source}" inode=123 dev=08:01',
           'type=EOE msg=audit(1700000000.123:42):']
    assert parse_auditd_records(lines[:-1],helper.bindings(),'sjfx_test')==[]
    events=parse_auditd_records(lines,helper.bindings(),'sjfx_test')
    assert len(events)==1 and events[0]['process_id']=='99' and events[0]['user']=='1000'
    assert events[0]['access_mask']=='r' and events[0]['action']=='file.access_requested'
    assert events[0]['version_evidence']=='current_binding_at_collection_not_event_content_proof'
    assert parse_auditd_records(lines,helper.bindings(),'other_key')==[]
    helper.db.close()


def test_formal_desktop_installation_owner_and_token_isolation(tmp_path,monkeypatch):
    monkeypatch.setenv('SJFX_PROTECTION_TEST_STATIC_ONLY','1')
    for key in ('SJFX_LOCAL_AGENT_TOKEN_FILE','SJFX_LOCAL_AGENT_ENDPOINT','SJFX_DESKTOP_MANAGED_PROTECTION','SJFX_PROTECTION_MODE','SJFX_PROTECTION_ALLOW_ENFORCEMENT','HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE','SJFX_PROTECTION_SCAN_WORKDIR'):
        monkeypatch.setenv(key,os.getenv(key,''))
    monkeypatch.setattr(desktop_runtime,'current_owner',lambda:'test-owner')
    monkeypatch.setattr(desktop_runtime,'protect_private_path',lambda path:None)
    monkeypatch.setenv('SJFX_LOCAL_AGENT_ENDPOINT','http://127.0.0.1:19237')
    config=SimpleNamespace(DATA_DIR=tmp_path)
    paths=desktop_runtime.prepare_environment(config)
    assert 'SJFX_PROTECTION_TEST_STATIC_ONLY' not in os.environ
    assert paths['endpoint']=='http://127.0.0.1:19237'
    assert len(Path(paths['token_file']).read_text())==64
    assert json.loads((Path(paths['core_state'])/'package-policy.json').read_text())['enabled'] is True
    assert paths['helper_state']!=paths['core_state']
    monkeypatch.setattr(desktop_runtime,'current_owner',lambda:'other-owner')
    with pytest.raises(RuntimeError): desktop_runtime.prepare_environment(config)


def test_transaction_1001_members_partial_rejection_atomic_commit_and_frozen_receipts(tmp_path):
    helper,registration,source,token=fixture(tmp_path)
    files=[]
    for i in range(1001):
        path=source.parent/f'large-{i:04d}.txt';path.write_text('stable')
        info=snapshot(path);fid=f'file-{i:04d}'
        files.append({'binding_id':'local:'+fid,'file_id':fid,'local_path':str(path),**info,'version_id':info['sha256']})
    metadata={k:v for k,v in registration.items() if k!='files'}
    begin=helper.registration_transaction('/register/begin',{'registration':metadata,'expected_count':len(files),'expected_bytes':sum(f['size'] for f in files),'manifest_digest':manifest_digest(files)})
    identifier=begin['transaction_id']
    helper.registration_transaction('/register/batch',{'transaction_id':identifier,'offset':0,'files':files[:500]})
    with pytest.raises(ValueError): helper.registration_transaction('/register/commit',{'transaction_id':identifier})
    assert helper.bindings()==[]
    for offset in range(500,len(files),500):
        result=helper.registration_transaction('/register/batch',{'transaction_id':identifier,'offset':offset,'files':files[offset:offset+500]})
        assert result['coverage_complete'] is False
    complete=helper.registration_transaction('/register/commit',{'transaction_id':identifier})
    assert complete['coverage_complete'] is True and complete['count']==1001
    seen=[];offset=0
    while offset<1001:
        page=helper.registration_transaction('/register/result',{'transaction_id':identifier,'offset':offset,'limit':500})
        seen.extend(page['bindings']);offset=page['next_offset']
        assert page['manifest_digest']==manifest_digest(files)
    assert [b['file_id'] for b in seen]==[f['file_id'] for f in files]
    helper.registration_transaction('/register/finish',{'transaction_id':identifier})
    assert len(helper.bindings())==1001
    helper.db.close()


def test_transaction_changed_version_digest_and_budget_fail_without_activation(tmp_path,monkeypatch):
    helper,registration,source,token=fixture(tmp_path)
    files=registration.pop('files')
    begin=helper.registration_transaction('/register/begin',{'registration':registration,'expected_count':1,'expected_bytes':files[0]['size'],'manifest_digest':manifest_digest(files)})
    identifier=begin['transaction_id']
    helper.registration_transaction('/register/batch',{'transaction_id':identifier,'offset':0,'files':files})
    source.write_text('changed after batch')
    with pytest.raises(ValueError): helper.registration_transaction('/register/commit',{'transaction_id':identifier})
    assert helper.bindings()==[]
    helper.registration_transaction('/register/abort',{'transaction_id':identifier})
    monkeypatch.setenv('SJFX_LOCAL_AGENT_REGISTRATION_BYTES','1')
    with pytest.raises(OverflowError): helper.registration_transaction('/register/begin',{'registration':registration,'expected_count':1,'expected_bytes':2,'manifest_digest':manifest_digest(files)})
    helper.db.close()


def test_audit_log_rotation_marks_durable_gap_without_fabricating_coverage(tmp_path,monkeypatch):
    helper,registration,source,token=fixture(tmp_path);helper.register(registration)
    log=tmp_path/'native-audit.log';log.write_text('')
    monkeypatch.setenv('SJFX_LINUX_AUDIT_LOG',str(log));monkeypatch.setenv('SJFX_LINUX_AUDIT_KEY','selected-key')
    helper.collect_audit()
    assert helper.audit_health is False
    log.rename(tmp_path/'native-audit.log.1');log.write_text('')
    helper.collect_audit()
    assert helper.status()['has_unresolved_gaps'] is True
    assert helper.audit_health is False
    helper.db.close()
