"""Client must not publish coverage from partial or forged transaction receipts."""
from types import SimpleNamespace
import pytest
from sjfx_protection.local_agent import LocalAgent
from sjfx_protection.store import ProtectionError


def materials():
    metadata=dict(device_id='device',owner_id='alice',registration_id='reg',manifest_revision=1,
                  gap_epoch=0,local_root='/selected')
    files=[dict(binding_id='local:'+str(i),file_id=str(i),local_path='/selected/'+str(i)+'.txt',
                sha256='a'*64,version_id='a'*64,size=1,identity='1:'+str(i)) for i in range(1001)]
    return metadata,files


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('SJFX_LOCAL_AGENT_ENDPOINT','http://127.0.0.1:18767')
    return LocalAgent(SimpleNamespace(store=None,policy=lambda:{'inspection_profile':{'inventory_timeout_seconds':120}}))


def transport(metadata,files,client,fault=''):
    digest=client._manifest_digest(files);calls=[]
    def request(path,payload,**kwargs):
        action=path.rsplit('/',1)[-1];calls.append(action)
        if action=='begin':return dict(transaction_id='tx',next_offset=0,max_batch_files=500)
        if action=='batch':
            return dict(next_offset=payload['offset']+len(payload['files']),coverage_complete=fault=='premature')
        if action=='commit':return dict(coverage_complete=fault!='partial',count=len(files),manifest_digest=digest,gap_epoch=0)
        if action=='result':
            offset=payload['offset'];page=files[offset:offset+payload['limit']]
            bindings=[{**row,'device_id':metadata['device_id'],'owner_id':metadata['owner_id'],
                       'registration_id':metadata['registration_id'],'registration_revision':1,
                       'manifest_generation':1,'gap_epoch':0} for row in page]
            if fault=='path' and offset==500:bindings[-1]['local_path']='/outside'
            if fault=='duplicate' and offset==500:bindings[0]=dict(bindings[1])
            if fault=='truncated' and offset==1000:bindings=[]
            return dict(bindings=bindings,count=len(files),offset=offset,next_offset=offset+len(bindings),
                        manifest_digest='wrong' if fault=='digest' else digest)
        if action=='abort':return {'ok':True}
        raise AssertionError(action)
    return request,calls


def test_1001_members_verified_in_three_bounded_pages(client):
    metadata,files=materials();client.request,calls=transport(metadata,files,client)
    assert client._transaction_register(metadata,files)=='tx'
    assert calls==['begin','batch','batch','batch','commit','result','result','result']


@pytest.mark.parametrize('fault',['premature','partial','path','duplicate','truncated','digest'])
def test_incomplete_or_forged_receipt_never_returns_registration(client,fault):
    metadata,files=materials();client.request,calls=transport(metadata,files,client,fault)
    with pytest.raises(ProtectionError):client._transaction_register(metadata,files)
    if fault in {'premature','partial'}:assert calls[-1]=='abort'
    assert 'finish' not in calls


def test_control_character_cannot_ambiguate_manifest_digest(client):
    metadata,files=materials();files[0]['local_path']='/selected/new\nfile'
    client.request=lambda *a,**kw:pytest.fail('Unsupported digest input must stop before begin')
    with pytest.raises(ProtectionError,match='控制字符'):client._transaction_register(metadata,files)


def test_explicit_capacity_rejects_before_begin(client,monkeypatch):
    metadata,files=materials();monkeypatch.setenv('SJFX_LOCAL_AGENT_REGISTRATION_BYTES','1000')
    client.request=lambda *a,**kw:pytest.fail('Capacity must stop before begin')
    with pytest.raises(ProtectionError) as error:client._transaction_register(metadata,files)
    assert error.value.code=='local_agent_capacity_exceeded'
