"""Use the actual platform helper and the shared client over authenticated HTTP."""
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
from sjfx_protection.local_agent import LocalAgent
from sjfx_protection.scanner import stable_hash


def test_1001_real_files_shared_client_to_platform_helper(tmp_path,monkeypatch):
    selected=tmp_path/'中文资料';selected.mkdir()
    files=[]
    for index in range(1001):
        path=selected/('资料'+str(index)+'.txt');path.write_text('ordinary meeting note '+str(index),encoding='utf-8')
        observed=stable_hash(path)
        files.append(dict(binding_id='local:'+str(index),file_id=str(index),local_path=str(path),
                          version_id=observed['sha256'],**{k:observed[k] for k in ('sha256','size','identity')}))
    token=tmp_path/'token';token.write_text('a'*64,encoding='ascii')
    if os.name!='nt':token.chmod(0o600)
    helper=None;server=None;observer=None;stop=tmp_path/'helper.stop'
    if os.name=='nt':
        binary=os.getenv('SJFX_TEST_WINDOWS_HELPER')
        if not binary:pytest.skip('Actual compiled Windows helper must be explicitly selected')
        assert Path(binary).is_file()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        log=(tmp_path/'helper.log').open('wb')
        helper=subprocess.Popen([binary,'--local','--data',str(tmp_path/'helper'),'--local-token-file',str(token),
                                 '--listen','127.0.0.1:'+str(port),'--stop-file',str(stop)],
                                stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        from http.server import ThreadingHTTPServer
        from sjfx_protection.linux_observer import Observer
        observer=Observer(tmp_path/'helper',token,audit=False)
        server=ThreadingHTTPServer(('127.0.0.1',0),observer.handler());port=server.server_port
        threading.Thread(target=server.serve_forever,daemon=True).start()
    monkeypatch.setenv('SJFX_LOCAL_AGENT_ENDPOINT','http://127.0.0.1:'+str(port))
    monkeypatch.setenv('SJFX_LOCAL_AGENT_TOKEN_FILE',str(token))
    client=LocalAgent(SimpleNamespace(store=None,policy=lambda:{'inspection_profile':{'inventory_timeout_seconds':120}}))
    try:
        deadline=time.monotonic()+20
        while True:
            try:
                assert client.request('/agent-local/v1/health',{}).get('ok') is True
                break
            except Exception:
                if helper and helper.poll() is not None:pytest.fail((tmp_path/'helper.log').read_text(errors='replace'))
                if time.monotonic()>deadline:raise
                time.sleep(.1)
        metadata=dict(device_id='acceptance-device',owner_id='acceptance-owner',registration_id='acceptance-package',
                      manifest_revision=1,gap_epoch=0,local_root=str(selected))
        transaction=client._transaction_register(metadata,files)
        health=client.request('/agent-local/v1/health',{})
        assert health['binding_count']==1001
        assert health['registration_limits']['transaction_registration_supported'] is True
        assert client.request('/agent-local/v1/register/finish',{'transaction_id':transaction})['ok'] is True
        receipts=client.request('/agent-local/v1/sync',dict(device_id='acceptance-device',protection_enabled=True,
                                ack_event_ids=[],ack_result_ids=[],actions=[]))
        assert receipts['health']['directory_monitor_healthy'] is True
        assert receipts['health']['access_audit_healthy'] is False
    finally:
        if server:
            server.shutdown();server.server_close();observer.db.close()
        if helper:
            stop.write_text('stop',encoding='ascii')
            try:helper.wait(timeout=8)
            except subprocess.TimeoutExpired:
                helper.terminate();helper.wait(timeout=8)
            log.close()
