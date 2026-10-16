"""Instance mode changes only protection, with no services or app lifespan."""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from config import Config
from services.storage import Storage
from sjfx_protection import business, desktop_runtime, policy
from sjfx_protection.integration import Protection, install
from sjfx_protection.registration import PackageProtection


@pytest.mark.parametrize('value', ['', 'true', 'false', 'yes', '2', '-1', ' 0', '1 '])
def test_invalid_instance_mode_is_rejected(value, monkeypatch, tmp_path):
    monkeypatch.setenv('SJFX_DESKTOP_FILE_PROTECTION', value)
    with pytest.raises(RuntimeError, match='exactly 0 or 1'):
        policy.protection_enabled(tmp_path)
    with pytest.raises(RuntimeError, match='exactly 0 or 1'):
        business.enabled()


def test_source_default_preserves_enabled_policy_and_corruption_fail_closed(monkeypatch, tmp_path):
    monkeypatch.delenv('SJFX_DESKTOP_FILE_PROTECTION', raising=False)
    assert policy.file_protection_requested() is True
    path = tmp_path / 'package-policy.json'
    path.write_text('{"enabled":true,"revision":7}', encoding='utf-8')
    assert policy.protection_enabled(tmp_path) is True
    path.write_text('corrupt existing policy', encoding='utf-8')
    assert policy.protection_enabled(tmp_path) is True
    assert policy.effective_policy(tmp_path)['invalid'] is True


@pytest.mark.parametrize('raw', [b'{"enabled":true,"revision":7}', b'corrupt existing policy'])
def test_disabled_mode_preserves_policy_history_and_bypasses_without_database(raw, monkeypatch, tmp_path):
    monkeypatch.setenv('SJFX_DESKTOP_FILE_PROTECTION', '0')
    path = tmp_path / 'package-policy.json'
    history = tmp_path / 'package-policy-history.jsonl'
    path.write_bytes(raw)
    history.write_bytes(b'previous durable history\n')
    assert policy.protection_enabled(tmp_path) is False
    effective = policy.effective_policy(tmp_path)
    assert effective['enabled'] is False and effective['stored_enabled'] is True
    package = object.__new__(PackageProtection)
    package.store = SimpleNamespace(directory=tmp_path)
    assert package.enabled is False
    assert package.require_operation('alice', root='unused-source', operation='analyze') == {
        'state':'allow', 'bypass':True, 'context':None, 'enabled':False}
    assert path.read_bytes() == raw and history.read_bytes() == b'previous durable history\n'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['package-policy-history.jsonl', 'package-policy.json']
    monkeypatch.setenv('SJFX_DESKTOP_FILE_PROTECTION', '1')
    assert policy.protection_enabled(tmp_path) is True
    assert path.read_bytes() == raw


def test_disabled_mode_overrides_configured_and_background_business(monkeypatch):
    monkeypatch.setenv('SJFX_DESKTOP_FILE_PROTECTION', '0')
    monkeypatch.setattr(business, '_configured', SimpleNamespace(enabled=True))
    assert business.enabled() is False and business.protection() is None
    monkeypatch.setattr(business, '_configured', None)
    assert business.enabled() is False and business.protection() is None
    monkeypatch.setenv('SJFX_DESKTOP_FILE_PROTECTION', '1')
    monkeypatch.setattr(business, '_configured', SimpleNamespace(enabled=True))
    assert business.enabled() is True


def test_disabled_runtime_does_not_initialize_state_query_helper_or_run_executor(monkeypatch, tmp_path):
    monkeypatch.setenv('SJFX_DESKTOP_FILE_PROTECTION', '0')
    def forbidden(*args, **kwargs):
        raise AssertionError('Disabled mode touched protection runtime')
    monkeypatch.setattr(desktop_runtime, 'current_owner', forbidden)
    monkeypatch.setattr(desktop_runtime, 'protect_private_path', forbidden)
    monkeypatch.setattr(desktop_runtime, 'helper_health', forbidden)
    config = SimpleNamespace(DATA_DIR=tmp_path / 'untouched-state')
    assert desktop_runtime.prepare_environment(config) is None
    health = desktop_runtime.health_report(config)
    assert health['status'] == 'disabled' and health['enabled'] is False
    assert health['configured'] is False and health['disabled_by_configuration'] is True
    assert health['helper']['running'] is False and health['executor']['healthy'] is False
    with pytest.raises(RuntimeError, match='executor is disabled'):
        desktop_runtime.executor(SimpleNamespace(state=str(config.DATA_DIR)))
    assert not config.DATA_DIR.exists()


@pytest.mark.parametrize('role', ['user','admin'])
def test_disabled_web_status_requires_login_and_reports_effective_disabled(monkeypatch, tmp_path, role):
    monkeypatch.setenv('SJFX_DESKTOP_FILE_PROTECTION', '0')
    monkeypatch.setattr(business, '_configured', None)
    (tmp_path / 'package-policy.json').write_text('{"enabled":true,"revision":7}', encoding='utf-8')
    before = {p.name:p.read_bytes() for p in tmp_path.iterdir()}
    app = FastAPI()
    user = {'user_id':'u1', 'owner_id':'alice', 'role':role, 'must_change_pw':False}
    protection = install(app, state_dir=tmp_path, allowed_roots=[], mode='enforce',
                         session_resolver=lambda sid: user if sid == 'valid-session' else None)
    async def requests():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            missing = await client.get('/protection/api/status')
            assert missing.status_code == 401
            authenticated = await client.get('/protection/api/status', headers={'Cookie':'sjfx_session=valid-session'})
            assert authenticated.status_code == 200
            value = authenticated.json()
            assert value['enabled'] is False and value['status'] == 'disabled'
            assert value['mode'] == 'off' and value['disabled_by_configuration'] is True
            assert value['runtime_disabled'] is True
            if role == 'admin':
                assert value['global_mode'] == 'off' and value['configured_mode'] == 'enforce'
    asyncio.run(requests())  # ASGITransport does not start application lifespan.
    assert protection._store is None
    assert {p.name:p.read_bytes() for p in tmp_path.iterdir()} == before


def test_disabled_policy_form_reads_durable_switch_and_preserves_it_on_other_edits(monkeypatch, tmp_path):
    monkeypatch.setenv('SJFX_DESKTOP_FILE_PROTECTION', '0')
    monkeypatch.setattr(business, '_configured', None)
    path=tmp_path/'package-policy.json'
    path.write_text('{"enabled":true,"revision":7}', encoding='utf-8')
    app=FastAPI()
    user={'user_id':'u1','owner_id':'alice','role':'admin','must_change_pw':False}
    protection=install(app,state_dir=tmp_path,allowed_roots=[],mode='enforce',
        session_resolver=lambda sid:user if sid=='admin-session' else None)
    # The page's existing registrations view initializes its protection schema.
    # All state belongs to this disposable fixture; no application lifespan runs.
    protection.package._ensure()
    async def requests():
        headers={'Cookie':'sjfx_session=admin-session','Origin':'http://test','X-SJFX-Protection':'1'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            original=path.read_bytes()
            response=await client.get('/protection/api/package-policy',headers=headers)
            assert response.status_code==200
            value=response.json()
            assert value['policy']['enabled'] is True
            assert value['effective_enabled'] is False and value['runtime_disabled'] is True
            assert path.read_bytes()==original
            saved=await client.post('/protection/api/package-policy',headers=headers,
                json={'enabled':value['policy']['enabled'],'operation_policy':{'delete':'warn'},
                      'reason':'synthetic unrelated persisted policy edit'})
            assert saved.status_code==200
            assert saved.json()['policy']['enabled'] is True and saved.json()['effective_enabled'] is False
            assert json.loads(path.read_text(encoding='utf-8'))['enabled'] is True
            status=(await client.get('/protection/api/status',headers=headers)).json()
            assert status['runtime_disabled'] is True and status['enabled'] is False
    asyncio.run(requests())


def test_disabled_web_does_not_start_watcher_with_old_agent_token(monkeypatch, tmp_path):
    monkeypatch.setenv('SJFX_DESKTOP_FILE_PROTECTION', '0')
    monkeypatch.setenv('SJFX_LOCAL_AGENT_TOKEN_FILE', str(tmp_path / 'old-token'))
    protection = Protection(tmp_path, [], lambda _: None, mode='enforce')
    protection.ensure_executor()
    assert protection._watch_proc is None and protection._store is None
    assert list(tmp_path.iterdir()) == []


def test_disabled_mode_keeps_real_storage_owner_isolation(monkeypatch, tmp_path):
    monkeypatch.setenv('SJFX_DESKTOP_FILE_PROTECTION', '0')
    monkeypatch.setattr(business, '_configured', SimpleNamespace(enabled=True))
    monkeypatch.setattr(Config, 'DATA_DIR', tmp_path / 'state')
    monkeypatch.setattr(Config, 'OUTPUT_DIR', tmp_path / 'outputs')
    monkeypatch.setattr(Config, 'SCAN_ALLOWED_ROOTS', ())
    storage = Storage(tmp_path / 'business.db')
    storage.save_scan({'root':str(tmp_path / 'source'), 'files':[], 'tree':{}}, scan_id='scan-A', owner_id='alice')
    assert storage.get_scan('scan-A', 'alice') is not None
    assert storage.get_scan('scan-A', 'bob') is None
    job = storage.create_job('scan-A', owner_id='alice')
    storage.update_job(job, result={'summary':'core content without protection mode'})
    assert storage.get_job(job, 'alice')['result']['summary'] == 'core content without protection mode'
    assert storage.get_job(job, 'bob') is None
    assert not (Config.DATA_DIR / 'file_protection').exists()


def test_disabled_supervisor_starts_and_reaps_only_selected_core_roles(monkeypatch, tmp_path):
    import config
    import sjfxctl
    from services import managed_embedding, process_liveness
    monkeypatch.setenv('SJFX_DESKTOP_FILE_PROTECTION', '0')
    cfg = SimpleNamespace(DATA_DIR=tmp_path, LOG_DIR=tmp_path, HOST='127.0.0.1', PORT=18000,
                          EXTRA_BIND_HOSTS=(), LARGE_QA_ENABLED=True)
    monkeypatch.setattr(config, 'ensure_runtime_directories', lambda: None)
    monkeypatch.setattr(sjfxctl, '_dependency_check', lambda _: [])
    monkeypatch.setattr(sjfxctl, '_ports_available', lambda _: None)
    monkeypatch.setattr(sjfxctl, '_web_ready', lambda _: True)
    monkeypatch.setattr(sjfxctl, '_identity', lambda _: 1.0)
    monkeypatch.setattr(sjfxctl.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(managed_embedding, 'selected', lambda _: True)
    monkeypatch.setattr(managed_embedding, 'resource_check', lambda _: {'resource_complete':True, 'missing_files':[]})
    def forbidden(*args, **kwargs):
        raise AssertionError('Disabled supervisor touched helper preparation or health')
    monkeypatch.setattr(desktop_runtime, 'prepare_environment', forbidden)
    monkeypatch.setattr(desktop_runtime, 'helper_health', forbidden)
    spawned, reaped, states = [], [], []
    class Log:
        def close(self):pass
    def spawn(role, config, runtime, token, command=None):
        spawned.append(role)
        gate = runtime / (role + '.gate')
        gate.with_suffix('.ready').write_text(token, encoding='ascii')
        process = SimpleNamespace(pid=100 + len(spawned), poll=lambda: None, role=role)
        return process, None, Log(), gate
    monkeypatch.setattr(sjfxctl, '_spawn_role', spawn)
    monkeypatch.setattr(process_liveness, 'terminate_process_tree', lambda p, **_: reaped.append(p.role))
    def publish(path, state):
        states.append(json.loads(json.dumps(state)))
        if state['phase'] == 'ready':
            (path.parent / 'stop.json').write_text(json.dumps({'run_id':state['run_id']}), encoding='utf-8')
    monkeypatch.setattr(sjfxctl, '_atomic_json', publish)
    assert sjfxctl.supervise(SimpleNamespace(run_id='bounded-mode-test', timeout=10), cfg) == 0
    assert spawned == ['embedding','web','worker','large-worker','qa-worker']
    assert reaped == list(reversed(spawned))
    ready = next(state for state in states if state['phase'] == 'ready')
    assert set(ready['roles']) == set(spawned)
    assert ready['desktop_protection']['status'] == 'disabled'
    assert not (tmp_path / 'desktop_protection').exists() and not (tmp_path / 'file_protection').exists()


def test_disabled_doctor_skips_audit_preflight(monkeypatch, tmp_path):
    import sjfxctl
    from services import deployment_readiness
    from sjfx_protection import desktop_audit
    monkeypatch.setenv('SJFX_DESKTOP_FILE_PROTECTION', '0')
    def forbidden(*args, **kwargs):
        raise AssertionError('Disabled doctor ran access-audit preflight')
    monkeypatch.setattr(desktop_audit, 'preflight', forbidden)
    monkeypatch.setattr(sjfxctl, '_model_health', lambda _: {})
    monkeypatch.setattr(deployment_readiness, 'deployment_report', lambda *_args, **_kwargs: {'ok':True})
    reports=[]
    monkeypatch.setattr(sjfxctl, '_emit', reports.append)
    cfg = SimpleNamespace(DATA_DIR=tmp_path, LLM_BACKEND='disabled', MANAGED_EMBEDDING=False,
        DOCLING_ARTIFACTS_DIR=tmp_path/'docling', RAPIDOCR_MODEL_DIR=tmp_path/'ocr', EMBED_MODEL_PATH=tmp_path/'embedding')
    assert sjfxctl.doctor(cfg) == 0
    assert reports[0]['desktop_protection']['status'] == 'disabled'
    assert reports[0]['program_access_audit_preflight']['status'] == 'disabled'
    assert not (tmp_path / 'desktop_protection').exists()


def test_linux_template_keeps_durable_policy_control_without_forcing_runtime_off(tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('linux_mode_test_builder', root / 'packaging/build_offline_bundle.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    builder._write_templates(tmp_path, 'linux-x64', root / 'packaging')
    text = (tmp_path / 'config/sjfx.env.template').read_text(encoding='utf-8')
    assert 'SJFX_DESKTOP_FILE_PROTECTION=' not in text
    assert 'AUTH_REQUIRED=1\n' in text
    installer = (root / 'packaging/install_linux.sh').read_text(encoding='utf-8')
    assert '{"enabled": False}' in installer
