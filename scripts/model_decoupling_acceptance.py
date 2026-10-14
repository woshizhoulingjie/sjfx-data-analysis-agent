"""Real isolated model configuration and business acceptance on Windows/Linux.

Uses already running services, synthetic documents and a private credential file.
No engine installation, model download or production configuration change occurs.
Evidence excludes credentials and private instance configuration. Existing model
quality gates are checked against original text rather than response metadata.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from platform_functional_acceptance import Acceptance, write_json


def active_probe():
    from config import Config
    from services.model_backend import create_model_client
    from services.embedding_backend import create_embedding_client
    from services.model_contract import LocalModelError
    from services.model_settings import get_model_settings
    client = create_model_client(Config)
    assert client is not None
    normal = client.chat('Answer using the supplied fact only.', '采购合计120元。采购合计多少元？', max_tokens=256, retries=0)
    assert '120' in normal['content'], normal
    structured = client.chat_json('Output only JSON.', 'Return exactly {"amount":120,"paid":false}.', max_tokens=512,
                                  retries=0, required_fields=['amount','paid'])
    assert structured['json']['amount'] == 120 and structured['json']['paid'] is False, structured
    rejected = None
    try:
        client.chat('Context acceptance.', '测' * (Config.LLM_CONTEXT_TOKENS * 3), max_tokens=64, retries=0)
    except LocalModelError as exc:
        rejected = exc.code
    assert rejected == 'context_limit', rejected
    lanes = {}
    for lane in ('ordinary','large','mail'):
        emb = create_embedding_client(Config,lane=lane)
        assert emb is not None
        vectors = emb.embed(['采购合计120元，尚未付款。'])
        assert len(vectors)==1 and len(vectors[0]) > 0
        lanes[lane] = {'backend':type(emb).__name__,'endpoint':emb.base_url,'model':emb.model,
                       'dimension':len(vectors[0]),'encoding_policy':emb.encoding_policy}
    assert len({row['endpoint'] for row in lanes.values()}) == 1, lanes
    assert len({row['model'] for row in lanes.values()}) == 1, lanes
    return {'active':get_model_settings(Config)['active'],'normal':normal,'structured':structured,
            'over_limit_rejected':rejected,'embedding_lanes':lanes}


def rebuild_probe(scan_id):
    from contextlib import closing
    import sqlite3
    from config import Config
    from services.embedding_backend import create_embedding_client, require_index_identity, client_identity
    from services.embedding_rebuild import rebuild_embeddings, publish_rebuild, _fingerprints
    from services.model_contract import LocalModelError
    db = Config.DB_PATH
    client = create_embedding_client(Config,lane='ordinary')
    identity = client_identity(client)
    with closing(sqlite3.connect(db)) as conn:
        before = _fingerprints(conn,'ordinary',scan_id)
        # Explicitly bind this synthetic old E5 index when ordinary recall has
        # not yet materialized a binding row. Existing vectors remain intact.
        keys = {row[0] for row in conn.execute('SELECT DISTINCT model FROM embedding_cache WHERE substr(cache_key,1,?)=?',
                    (len('ordinary-file::'+scan_id+'::'),'ordinary-file::'+scan_id+'::'))}
        assert keys and identity not in keys,keys
        refused = None
        try:
            require_index_identity(conn,'ordinary',scan_id,identity,client.dimension,existing_keys=keys)
        except LocalModelError as exc:
            refused = exc.code
        assert refused == 'embedding_rebuild_required',refused
        assert _fingerprints(conn,'ordinary',scan_id)==before,'old vectors altered by rejection'
    candidate = rebuild_embeddings(db,'ordinary',scan_id,client=client,publish=False)
    assert candidate['status']=='candidate_ready' and candidate['count'] > 0,candidate
    assert candidate['dimension'] != 384,'test requires a genuinely different vector model'
    with closing(sqlite3.connect(db)) as conn:
        assert _fingerprints(conn,'ordinary',scan_id)==before,'candidate changed live index'
    published = publish_rebuild(candidate['manifest'])
    assert published['published_fingerprint']['source']==before['source'],published
    restored = publish_rebuild(candidate['manifest'],restore=True)
    assert restored['published_fingerprint']==before,restored
    return {'old_index_rejection':refused,'old_vectors_preserved_during_build':True,
            'source_preserved_during_publish_and_restore':True,'candidate':candidate,'published':published,'restored':restored}


class ModelAcceptance(Acceptance):
    def __init__(self,args):
        super().__init__(args.evidence,args.timeout,scenario='model-decoupling')
        self.gateway = args.gateway.rstrip('/')
        self.native = args.native.rstrip('/')
        self.secret = json.loads(args.credential_file.read_text(encoding='utf-8'))['api_key']
        self.model = args.model
        self.scenario = args.scope
        self.direct_generic = args.generic_url
        self.model_settings_header = {'X-SJFX-Model-Settings':'1'}
        for key in list(self.env):
            if key.startswith(('LLM_','CONVERSATION_','TRANSLATION_')):
                self.env.pop(key)
        self.metrics_before = None

    def report(self):
        # Parent report has no configuration secrets. Scrub assertion text too.
        super().report()
        path = self.root / 'acceptance.json'
        value = path.read_text(encoding='utf-8').replace(self.secret,'[redacted]')
        path.write_text(value,encoding='utf-8')

    def setup(self):
        result = super().setup()
        # Explicit representative business budgets; no model is enabled yet.
        extra = {'LARGE_QA_DEMAND_ONLY':'0','LARGE_QA_IDLE_SLEEP':'1','LARGE_QA_EXTRACT_PAUSE':'0',
                 'LARGE_QA_SYNC_INTERVAL':'2','LARGE_QA_REFINE_TOPICS':'0',
                 'MAIL_RESEARCH_MAX_MODEL_CALLS':'16','MAIL_RESEARCH_MAX_TOOL_CALLS':'30',
                 'MAIL_RESEARCH_MAX_TOKENS':'60000','MAIL_RESEARCH_TIMEOUT_SECONDS':str(self.timeout),
                 'MAIL_ENABLE_VECTOR_INDEX':'1','LLM_CONTEXT_TOKENS':'32768'}
        lines = self.env_file.read_text(encoding='utf-8').splitlines()
        lines = [line for line in lines if line.split('=',1)[0] not in extra]
        self.env_file.write_text('\n'.join(lines)+'\n'+'\n'.join(k+'='+v for k,v in extra.items())+'\n',encoding='utf-8')
        return result

    def settings(self):
        return self.req(self.admin,'GET','/api/admin/model-settings')

    def draft(self,payload,role='all'):
        return self.req(self.admin,'POST','/api/admin/model-test',expected=(200,503),
                        headers=self.model_settings_header,
                        timeout=70,json={'settings':payload,'role':role,'timeout_seconds':60})

    def config(self,backend='vllm',context=32768,embed_backend='openai_compatible'):
        gen_url = self.gateway+'/gen/v1' if backend != 'ollama' else self.gateway+'/native'
        if backend == 'openai_compatible' and self.direct_generic:
            gen_url = self.direct_generic.rstrip('/')
        gen = {'backend':backend,'base_url':gen_url,'model':self.model,'enabled':True,
               'context_tokens':context,'timeout_seconds':180,'concurrency':1}
        if gen_url.startswith(self.gateway):
            gen['api_key'] = self.secret
        if backend == 'openai_compatible':
            gen['supports_json_response_format'] = True
        embedding = {'backend':embed_backend,'base_url':self.gateway+'/embed/v1',
                     'model':'multilingual-e5-small','api_key':self.secret,'encoding':'e5','dimension':384}
        if embed_backend == 'ollama':
            embedding.update(base_url=self.gateway+'/native',model='qwen-embed:latest',encoding='plain',dimension=0)
        return {'generation':gen,'embedding':embedding}

    def permissions(self):
        self.req(self.user,'PUT','/api/admin/model-settings',expected=(403,),headers=self.model_settings_header,json=self.config())
        self.req(self.user,'POST','/api/admin/model-test',expected=(403,),headers=self.model_settings_header,json={'settings':self.config()})
        self.req(self.admin,'PUT','/api/admin/model-settings',expected=(403,),json=self.config())
        for url in ('http://169.254.169.254/v1','http://user:password@example.com/v1','http://example.com/v1?secret=x'):
            self.req(self.admin,'PUT','/api/admin/model-settings',expected=(400,),headers=self.model_settings_header,
                     json={'generation':{'base_url':url}})
        return {'user_settings_and_test_denied':True,'write_header_required':True,'invalid_endpoint_rejected':True}

    def diagnostic_acceptance(self):
        before = self.settings()
        file = self.state/'model_settings.json'
        prior = file.read_bytes() if file.exists() else None
        positive = self.draft(self.config())
        write_json(self.root/'draft_real_vllm_and_e5.json',positive)
        assert positive['ok'],positive
        wrong = self.config(); wrong['generation']['api_key'] = 'sjfx-deliberately-invalid-test-key'
        denied = self.draft(wrong,'generation')
        assert not denied['ok'] and any(c.get('http_status')==401 for c in denied['roles']['generation']['checks'].values()),denied
        missing = self.config(); missing['generation']['model'] = 'sjfx-missing-model-acceptance'
        absent = self.draft(missing,'generation')
        assert not absent['ok'] and absent['roles']['generation']['configured_model_present'] is False,absent
        assert self.settings()==before,'draft changed runtime/saved settings'
        assert (file.read_bytes() if file.exists() else None)==prior,'draft persisted configuration'
        return {'positive':positive,'authentication_error':denied,'unknown_model':absent,'draft_not_persisted':True}

    def save_restart(self,payload,name):
        before = self.settings()
        saved = self.req(self.admin,'PUT','/api/admin/model-settings',headers=self.model_settings_header,json=payload)
        assert saved['ok'] and saved['restart_required'],saved
        assert saved['active']==before['active'],'save hot-switched active model'
        assert self.secret not in json.dumps(saved),'credential exposed in API'
        assert not saved['saved']['embedding']['lane_overrides'],saved
        write_json(self.root/(name+'_saved_settings.json'),saved)
        self.cli('stop','--timeout','30')
        started = self.start()
        active = self.settings()
        assert not active['restart_required'] and active['active']==active['saved'],active
        assert active['active']['generation']['backend']==payload['generation']['backend'],active
        self.req(self.user,'GET','/api/auth/me')
        write_json(self.root/(name+'_active_settings.json'),active)
        self.generation_url = active['active']['generation']['base_url']
        return {'settings':active,'roles':started,'sessions_retained':True}

    def direct_probe(self,name):
        # Probe in a fresh process without this instance's background jobs
        # competing for the same configured generation admission slots.
        self.cli('stop','--timeout','30')
        proc = subprocess.run([sys.executable,'-X','utf8',str(Path(__file__)),'--active-probe'],cwd=ROOT,env=self.env,
                              capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=self.timeout+60)
        text = (proc.stdout+'\n'+proc.stderr).replace(self.secret,'[redacted]')
        (self.root/(name+'_transport.log')).write_text(text,encoding='utf-8')
        self.start()
        assert proc.returncode==0,text[-4000:]
        value = json.loads(proc.stdout)
        write_json(self.root/(name+'_transport.json'),value)
        return value

    def real_rebuild(self):
        # Publication deliberately requires a stable source snapshot. Pause
        # this synthetic instance before building/publishing/restoring vectors.
        self.cli('stop','--timeout','30')
        proc = subprocess.run([sys.executable,'-X','utf8',str(Path(__file__)),'--rebuild-probe',self.ids['model_scan']],
                              cwd=ROOT,env=self.env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=self.timeout+60)
        text = (proc.stdout+'\n'+proc.stderr).replace(self.secret,'[redacted]')
        (self.root/'plain_vector_rebuild.log').write_text(text,encoding='utf-8')
        assert proc.returncode==0,text[-4000:]
        result = json.loads(proc.stdout)
        write_json(self.root/'plain_vector_rebuild.json',result)
        return result

    def native_availability(self,payload):
        report = self.draft(payload,'generation')
        write_json(self.root/'native_real_inference_availability.json',report)
        row = report['roles']['generation']
        if not report['ok']:
            assert not row.get('inference'),report
            assert any(not c.get('ok') for c in row.get('checks',{}).values()) or row.get('code')=='timeout',report
        return report

    def phase(self,name,call):
        original = self.instance
        self.instance = original/name
        self.instance.mkdir(exist_ok=True)
        try:
            return call()
        finally:
            self.instance = original

    def metrics(self):
        req = urllib.request.Request(self.gateway+'/_test_metrics',headers={'Authorization':'Bearer '+self.secret})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=20) as resp:
            return json.load(resp)

    def metrics_evidence(self):
        after = self.metrics()
        assert after != self.metrics_before,'real relay received no new model requests'
        result = {'before':self.metrics_before,'after':after,'real_services':'existing generation and embedding engines',
                  'stream_scope':'relay buffers upstream NDJSON; streamed parsing validated, token timing not certified'}
        write_json(self.root/'real_service_request_counts.json',result)
        return result

    def run(self):
        try:
            if not self.case('synthetic_fixtures',self.fixtures): return
            if not self.case('isolated_disabled_setup',self.setup): return
            if not self.case('four_roles_start',self.start): return
            if not self.case('first_admin_and_owner_permissions',self.auth): return
            self.case('model_configuration_authorization',self.permissions)
            self.metrics_before = self.metrics()
            if not self.case('real_draft_chat_json_vectors_errors_no_persistence',self.diagnostic_acceptance): return
            if not self.case('save_server_model_restart_all_roles',lambda:self.save_restart(self.config(),'server')): return
            self.case('server_real_chat_json_three_vector_lanes_context_limit',lambda:self.direct_probe('server'))
            self.case('server_selected_txt_import_search_qa_exact_quote',lambda:self.phase('server-sample',self.model_sample))
            if self.scenario == 'vectors':
                plain = self.config('vllm',embed_backend='ollama')
                if self.case('save_independent_native_plain_vectors_restart_roles',lambda:self.save_restart(plain,'plain-vectors')):
                    self.case('real_generation_with_native_plain_three_vector_lanes',lambda:self.direct_probe('plain-vectors'))
                    self.case('real_changed_vector_dimension_candidate_publish_restore',self.real_rebuild)
                self.case('actual_remote_service_call_evidence',self.metrics_evidence)
                return
            if self.scenario == 'full':
                self.case('server_ordinary_file_directory_report_docx',lambda:self.phase('server-ordinary',self.extended_ordinary))
                self.case('server_large_package_original_qa',lambda:self.phase('server-large',self.extended_large))
                if self.case('new_mail_import_previews_threads_people',self.mail_import):
                    self.case('server_mail_original_screen',self.extended_mail_screen)
                    self.case('server_mail_research_original_claim',self.extended_mail_research)
                    if self.ids.get('mail_research_result') or self.ids.get('mail_screen_result'):
                        self.case('mail_research_csv_html_xlsx_export',self.extended_mail_exports)
            native = self.config('ollama',context=4096)
            available = {}
            def check_native():
                available.update(self.native_availability(native))
                return available
            self.case('native_actual_inference_availability_and_error_reporting',check_native)
            if available.get('ok') and self.case('save_native_ollama_4096_restart_all_roles',lambda:self.save_restart(native,'native')):
                self.case('native_real_normal_streamed_json_and_vectors',lambda:self.direct_probe('native'))
                self.case('native_4096_new_selected_txt_qa',lambda:self.phase('native-sample',self.model_sample))
            generic = self.config('openai_compatible',context=32768)
            if self.case('save_generic_openai_service_restart_all_roles',lambda:self.save_restart(generic,'generic')):
                def generic_diagnostic():
                    result = self.draft(generic)
                    assert result['ok'],result
                    return result
                self.case('generic_real_short_diagnostic_chat_json_vectors',generic_diagnostic)
                self.case('generic_real_chat_json_and_vectors',lambda:self.direct_probe('generic'))
                self.case('generic_new_selected_txt_qa',lambda:self.phase('generic-sample',self.model_sample))
            self.case('actual_remote_service_call_evidence',self.metrics_evidence)
        finally:
            if self.started:
                self.case('clean_four_role_shutdown',lambda:self.cli('stop','--timeout','30'))
            for client in self.clients:
                client.close()
            self.report()
            print('Evidence: '+str(self.root),flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--active-probe',action='store_true')
    p.add_argument('--rebuild-probe')
    p.add_argument('--saved-diagnostic',action='store_true')
    p.add_argument('--evidence',type=Path)
    p.add_argument('--credential-file',type=Path)
    p.add_argument('--gateway')
    p.add_argument('--native',default='http://100.120.119.161:11434')
    p.add_argument('--generic-url')
    p.add_argument('--model',default='qwen3.6:27b')
    p.add_argument('--timeout',type=int,default=240)
    p.add_argument('--scope',choices=['core','full','vectors'],default='full')
    args = p.parse_args()
    if args.active_probe:
        print(json.dumps(active_probe(),ensure_ascii=False,default=str))
        return 0
    if args.rebuild_probe:
        print(json.dumps(rebuild_probe(args.rebuild_probe),ensure_ascii=False,default=str))
        return 0
    if args.saved_diagnostic:
        from config import Config
        from services.model_diagnostics import test_models
        result = test_models(Config,role='all',timeout_seconds=60)
        print(json.dumps(result,ensure_ascii=False,default=str))
        return 0 if result['ok'] else 1
    if not args.evidence or not args.gateway or not args.credential_file:
        p.error('--evidence, --gateway and --credential-file are required')
    run = ModelAcceptance(args)
    run.run()
    return 1 if any(r['status']=='failed' for r in run.results) else 0


if __name__=='__main__':
    raise SystemExit(main())
