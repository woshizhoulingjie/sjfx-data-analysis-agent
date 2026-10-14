"""Transient Office files must not poison real selected-file access batches."""
import json
import time
import pytest
from sjfx_protection.store import Store, ProtectionError
from sjfx_protection.registration import PACKAGE_SCHEMA


@pytest.fixture(params=['C:\\资料', '/home/local/资料'])
def environment(tmp_path, request):
    store=Store(tmp_path/'state');root=request.param
    device={'id':'device','owner':'owner'}
    with store.db(True) as db:
        db.executescript(PACKAGE_SCHEMA)
        db.execute('INSERT INTO registrations(id,owner,user_id,name,mode,root,policy_revision,state,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   ('reg','owner','user','selected','local',root,1,'ready',time.time(),time.time()))
        db.execute('INSERT INTO registration_devices(registration_id,device_id,local_root) VALUES(?,?,?)',('reg','device',root))
        db.execute('INSERT INTO bindings VALUES(?,?,?,?,?,?,?,?,?)',
                   ('binding','file','device','owner',root+'/ordinary.docx','identity','a'*64,'watching',time.time()))
    event=dict(event_id='directory-observation-1',action='monitor.directory_change_observed',
               registration_id='reg',device_id='device',owner_id='owner',gap_epoch=0,
               path=root+'/~$ordinary.docx',local_root=root,occurred=time.time(),sequence=1)
    return store,device,event


def test_temporary_file_observation_and_real_program_read_are_both_durable(environment):
    store,device,event=environment
    native=dict(event_id='security-read-1',binding_id='binding',action='file.read',
                process='C:\\Program Files\\Microsoft Office\\WINWORD.EXE',process_id='0x1234',
                native_event_id=4663,native_record_id=123,occurred=time.time(),sequence=2)
    assert store.ingest(device,[event,native])==[event['event_id'],native['event_id']]
    with store.db() as db:
        rows=list(db.execute('SELECT action,file_id,payload FROM events ORDER BY seq'))
    assert [(r['action'],r['file_id']) for r in rows]==[('monitor.directory_change_observed',''),('file.read','file')]
    assert json.loads(rows[1]['payload'])['payload']['native_record_id']==123


@pytest.mark.parametrize('change',[
    {'registration_id':'other'}, {'device_id':'other'}, {'owner_id':'other'},
    {'path':'/outside/ordinary.docx'}, {'local_root':'/outside'},
    {'binding_id':'binding'}, {'identity':'claimed-file'}, {'sha256':'a'*64},
    {'version_id':'claimed-version'}, {'gap_epoch':True}, {'gap_epoch':-1},
])
def test_unbound_observation_cannot_escape_scope_or_claim_content(environment,change):
    store,device,event=environment
    with pytest.raises(ProtectionError):store.ingest(device,[{**event,**change}])
    with store.db() as db:assert db.execute('SELECT count(*) FROM events').fetchone()[0]==0
