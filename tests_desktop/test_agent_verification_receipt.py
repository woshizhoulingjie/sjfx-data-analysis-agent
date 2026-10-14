"""Isolated cross-platform regressions for complete-inspection attachment receipts.

The static-only policy and simulated helper health are TEST fixtures, not claims
of production antivirus or operating-system audit coverage.
"""
import json
import time

import pytest

from sjfx_protection.local_agent import LocalAgent
from sjfx_protection.registration import PackageProtection
from sjfx_protection.store import ProtectionError, Store, encode


@pytest.fixture
def checked(tmp_path,monkeypatch):
    monkeypatch.setenv('SJFX_PROTECTION_TEST_STATIC_ONLY','1')
    monkeypatch.setenv('SJFX_PROTECTION_CLAM','')
    monkeypatch.setenv('SJFX_PROTECTION_CLAMD_SOCKET','')
    monkeypatch.setenv('SJFX_LOCAL_AGENT_ENDPOINT','http://127.0.0.1:18767')
    root=tmp_path/'中文资料';root.mkdir()
    (root/'evidence.txt').write_text('normal research evidence',encoding='utf-8')
    p=PackageProtection(Store(tmp_path/'state'),[str(root)],
        authorize_owner=lambda owner,user:{'user_id':'user1','role':'user'} if owner=='alice' else False,
        authorize_root=lambda owner,path:owner=='alice')
    p.update_policy('admin',True,{'enabled':True,'inspection_profile':{'required_engines':['static']}},
                    'Explicit isolated TEST profile; no real AV assertion')
    intake=p.begin_local('alice','user1',str(root))
    result=p.process_job(intake['job_id'])
    assert result['state']=='completed',result
    return p,intake['registration_id']


def state(p,rid):
    with p.store.db() as db:
        rd=dict(db.execute('SELECT * FROM registration_devices WHERE registration_id=? AND device_id=?',
                           (rid,p.local_device_id())).fetchone())
        marker=db.execute('SELECT value FROM settings WHERE key=?',('local-agent-registration:'+rid,)).fetchone()
        binding=db.execute('SELECT * FROM bindings WHERE id IN (SELECT id FROM file_instances WHERE registration_id=?)',
                           (rid,)).fetchone()
    return rd,json.loads(marker[0]) if marker else None,dict(binding) if binding else None


def client_for(p,monkeypatch,on_request=None):
    client=LocalAgent(p);calls=[]
    def request(path,payload,**kwargs):
        assert path=='/agent-local/v1/register'
        calls.append(payload)
        if on_request:on_request()
        return {'bindings':[{**row,'device_id':payload['device_id'],'owner_id':payload['owner_id'],
                'registration_id':payload['registration_id'],'registration_revision':payload['manifest_revision'],
                'manifest_generation':payload['manifest_revision'],'gap_epoch':payload['gap_epoch']}
                for row in payload['files']]}
    monkeypatch.setattr(client,'request',request)
    return client,calls


def helper_health(p):
    # Explicit fixture to exercise linkage, not an OS health assertion.
    report={'connected':True,'at':time.time(),'health':{'directory_monitor_healthy':True,
            'access_audit_requested':True,'access_audit_healthy':True},'registration_errors':{}}
    with p.store.db(True) as db:
        db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('local-agent-health',encode(report)))


def recheck(p,rid):
    job=p.recheck('alice',rid)
    result=p.process_job(job['id'])
    assert result['state']=='completed',result
    return p._registration('alice',rid)


def restored(p,rid):
    """Run core command/result/event paths with a matching fixture receipt."""
    rd,marker,binding=state(p,rid)
    with p.store.db() as db:
        device=dict(db.execute('SELECT * FROM devices WHERE id=?',(p.local_device_id(),)).fetchone())
    aid=p.store.command('alice',binding['id'],'protect_delete','Isolated test lock')
    p.store.action_result(device,aid,{'status':'completed'})
    restore=p.store.command('alice',binding['id'],'restore','Isolated test restore',aid)
    p.store.action_result(device,restore,{'status':'completed','actual':{
        'action_id':restore,'local_path':binding['local_path'],'local_identity':binding['local_identity'],
        'sha256':binding['sha256']}})
    accepted=p.store.ingest(device,[{'event_id':'fixture-restore-event','binding_id':binding['id'],
        'action':'file.restored','path':binding['local_path'],'identity':binding['local_identity'],
        'sha256':binding['sha256']}])
    assert accepted==['fixture-restore-event']
    with p.store.db() as db:
        assert db.execute('SELECT state FROM actions WHERE id=?',(restore,)).fetchone()[0]=='completed'
    return rd,marker,binding


def rejected(code,fn,*args):
    with pytest.raises(ProtectionError) as error:fn(*args)
    assert error.value.code==code


def test_same_complete_receipt_skips_redundant_registration(checked,monkeypatch):
    p,rid=checked;client,calls=client_for(p,monkeypatch)
    reg=p._registration('alice',rid)
    client.register(reg)
    rd,marker,binding=state(p,rid)
    assert marker==[reg['manifest_revision'],rd['gap_epoch'],rd['last_complete']]
    assert binding['state']=='watching'
    client.register(reg)
    assert len(calls)==1
    helper_health(p)
    assert p.local_coverage('alice',rid)['healthy'] is True


def test_restore_requires_reinspection_and_unchanged_reinspection_registers_again(checked,monkeypatch):
    p,rid=checked;client,calls=client_for(p,monkeypatch)
    reg=p._registration('alice',rid)
    client.register(reg);helper_health(p)
    before,old_marker,binding=restored(p,rid)
    rd,marker,current=state(p,rid)
    assert rd['state']=='unverified' and current['state']=='unverified'
    assert marker==old_marker and rd['gap_epoch']==before['gap_epoch']
    rejected('monitoring_gap',client.register,reg)
    rejected('binding_unverified',p.store.command,'alice',binding['id'],'protect_delete','Requires reinspection')
    assert len(calls)==1 and p.local_coverage('alice',rid)['healthy'] is False

    refreshed=recheck(p,rid)
    rd,marker,current=state(p,rid)
    assert refreshed['manifest_revision']==reg['manifest_revision']
    assert rd['gap_epoch']==before['gap_epoch'] and rd['last_complete']>before['last_complete']
    assert rd['state']=='verified' and current['state']=='unverified'
    assert p.local_coverage('alice',rid)['healthy'] is False
    client.register(refreshed)
    rd,marker,current=state(p,rid)
    assert len(calls)==2 and marker==[refreshed['manifest_revision'],rd['gap_epoch'],rd['last_complete']]
    assert marker!=old_marker and current['state']=='watching'
    assert p.local_coverage('alice',rid)['healthy'] is True
    action=p.store.command('alice',binding['id'],'protect_delete','After complete reinspection and attachment')
    assert any(a['id']==action for a in p.store.pending_actions(p.local_device_id()))


@pytest.mark.parametrize('mutation',[
    "last_complete=last_complete+1",
    "state='unverified'",
    "gap_epoch=gap_epoch+1",
    "verified_epoch=verified_epoch-1",
    "verified_revision=verified_revision+1",
    "manifest_generation=manifest_generation+1",
])
def test_receipt_change_during_request_does_not_publish(checked,monkeypatch,mutation):
    p,rid=checked;initial,calls=client_for(p,monkeypatch)
    initial.register(p._registration('alice',rid))
    before,old_marker,binding=restored(p,rid)
    reg=recheck(p,rid)
    def race():
        with p.store.db(True) as db:
            db.execute('UPDATE registration_devices SET '+mutation+' WHERE registration_id=?',(rid,))
    client,race_calls=client_for(p,monkeypatch,race)
    rejected('context_stale',client.register,reg)
    rd,marker,current=state(p,rid)
    assert len(race_calls)==1 and marker==old_marker and current['state']=='unverified'
    helper_health(p)
    assert p.local_coverage('alice',rid)['healthy'] is False


@pytest.mark.parametrize('mutation',[
    "state='unverified'",
    "last_complete=0",
    "verified_revision=verified_revision+1",
    "manifest_generation=manifest_generation+1",
])
def test_invalid_receipt_never_sends_registration(checked,monkeypatch,mutation):
    p,rid=checked;client,calls=client_for(p,monkeypatch)
    reg=p._registration('alice',rid)
    client.register(reg)
    with p.store.db(True) as db:
        db.execute('UPDATE registration_devices SET '+mutation+' WHERE registration_id=?',(rid,))
        db.execute("UPDATE bindings SET state='unverified'")
    rejected('monitoring_gap',client.register,reg)
    rd,marker,binding=state(p,rid)
    assert len(calls)==1 and binding['state']=='unverified'
    helper_health(p)
    assert p.local_coverage('alice',rid)['healthy'] is False


def test_legacy_marker_requires_valid_receipt_before_migration(checked,monkeypatch):
    p,rid=checked;client,calls=client_for(p,monkeypatch)
    reg=p._registration('alice',rid);client.register(reg)
    rd,marker,binding=state(p,rid)
    with p.store.db(True) as db:
        db.execute('UPDATE settings SET value=? WHERE key=?',
                   (encode(marker[:2]),'local-agent-registration:'+rid))
        db.execute("UPDATE registration_devices SET state='unverified' WHERE registration_id=?",(rid,))
        db.execute("UPDATE bindings SET state='unverified'")
    helper_health(p)
    assert p.local_coverage('alice',rid)['healthy'] is False
    rejected('monitoring_gap',client.register,reg)
    assert len(calls)==1
    reg=recheck(p,rid)
    assert p.local_coverage('alice',rid)['healthy'] is False
    client.register(reg)
    rd,marker,binding=state(p,rid)
    assert len(calls)==2 and len(marker)==3 and binding['state']=='watching'
    assert p.local_coverage('alice',rid)['healthy'] is True
