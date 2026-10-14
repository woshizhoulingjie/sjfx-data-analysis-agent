"""Turn-local execution primitives. Provider threads never mutate research state."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
import hashlib
import json
import re
import time

VERSION = 'mail-speed-five-stage-v1'
EXTRACTION_SCHEMA = {'type':'object','required':['answer','claims','findings'],
    'properties':{'answer':{'type':'string'},
        'claims':{'type':'array','maxItems':8,'items':{'type':'object'}},
        'findings':{'type':'array','maxItems':6,'items':{'type':'object'}}}}
REPAIR_SCHEMA = {'type':'object','required':['repairs'],
    'properties':{'repairs':{'type':'array','maxItems':14,'items':{
        'type':'object','required':['atom_index','row'],
        'properties':{'atom_index':{'type':'integer','minimum':0,'maximum':13},'row':{'type':'object'}}}}}}


def repair_output_tokens(payload):
    from .output_budget import complete_output_reservation
    rows = [{'atom_index':item['atom_index'], 'row':{**item['row'],
        'claim_text':'结'*240,'source_quote':'引'*400,'subject_ref':'主'*60,
        'object_ref':'事'*80,'actor':'a'*320}} for item in payload.get('invalid_rows') or []]
    return complete_output_reservation({'repairs':rows},minimum=1600)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    default=str).encode('utf-8')).hexdigest()


def execution_contract(spec):
    """Conservative objective routing; ambiguity and global status use deep review."""
    objective = str(spec.get('objective') or '')
    global_status = bool(re.search(r'最终|最新|当前|最早|是否|完成|验收|原因|为什么|风险|比较|对比|变化|演变|责任|final|latest|why|compare', objective, re.I))
    listing = bool(re.search(r'哪些|哪几个|清单|列出|有哪些|list|which', objective, re.I))
    factual = bool(re.search(r'日期|时间|金额|价格|数量|型号|名称|项目|何时|when|date|price|quantity|name', objective, re.I))
    route = 'deep_review' if global_status or not (listing or factual) else ('source_inventory' if listing else 'factual_lookup')
    return {'version': VERSION, 'route': route, 'objective': objective,
            'scope': deepcopy(spec.get('scope') or {}),
            'coverage': 'exhaustive' if re.search(r'所有|全部|都|完整|every|all', objective, re.I) else 'retrieval',
            'required_roles': ['support', 'counter', 'condition', 'update'],
            'unknown_is_visible': True, 'strict_verification': True,
            'max_provider_lanes': 2, 'stop_on_no_information_gain': True}


class ProviderLanes:
    """At most two admitted requests, each with its own dispatch ContextVars."""
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='mail-provider')
        self.pending = {}

    def submit(self, key, request):
        if key in self.pending or len(self.pending) >= 2:
            return False
        context = copy_context()
        self.pending[key] = (request, self.pool.submit(context.run, self._invoke, request))
        return True

    @staticmethod
    def _invoke(request):
        started = time.monotonic()
        try:
            return {'result': request['model'].chat_json(request['system'], request['prompt'], **request['options']),
                    'seconds': time.monotonic() - started}
        except Exception as exc:
            return {'error': exc, 'seconds': time.monotonic() - started}

    def take(self, key):
        item = self.pending.pop(key, None)
        return (item[0], item[1].result()) if item else None

    def invoke_sync(self, request):
        # Synchronous verification shares the same executor admission as
        # prefetched extraction; two pending calls cannot create a third lane.
        context = copy_context()
        return self.pool.submit(context.run, self._invoke, request).result()

    def close(self):
        # All reserved calls are accounted by the coordinator, including failures.
        self.pool.shutdown(wait=True)


class EvidenceSnapshot:
    """Per-turn facade: exact identities, isolated copies, no shared repo mutation."""
    def __init__(self, repository, package_id, owner_id):
        self.repository = repository
        self.package_id, self.owner_id = package_id, owner_id
        self.messages = {}
        self.evidence = {}
        self.identities = {}
        self.stats = {'message_hits': 0, 'evidence_hits': 0, 'bulk_reads': 0}
        self.version = None

    def __getattr__(self, name):
        return getattr(self.repository, name)

    def refresh(self):
        package = self.repository.get_package(self.package_id, self.owner_id) or {}
        version = (package.get('source_revision'), package.get('index_epoch'))
        if version != self.version:
            self.evidence.clear()
            self.identities.clear()
        elif callable(getattr(self.repository,'get_evidence',None)):
            # Retain identities, but reload processing provenance after refresh.
            self.evidence.clear()
        # Authorship and processing metadata may change without a source revision.
        self.messages.clear()
        self.version = version
        return version

    def get_message(self, key, owner_id, package_id=None):
        cache_key = (key, owner_id, package_id)
        if cache_key not in self.messages:
            self.messages[cache_key] = self.repository.get_message(key, owner_id, package_id)
        else:
            self.stats['message_hits'] += 1
        return deepcopy(self.messages[cache_key])

    def create_evidence(self, package_id, owner_id, segment):
        return self.create_evidence_batch(package_id,owner_id,[segment])[0]

    def checkpoint(self):
        return {'package_id':self.package_id, 'owner_id':self.owner_id,
                'version':list(self.version or ()), 'identities':dict(self.identities)}

    def restore(self, checkpoint):
        if (isinstance(checkpoint,dict) and checkpoint.get('package_id')==self.package_id
                and checkpoint.get('owner_id')==self.owner_id
                and checkpoint.get('version')==list(self.version or ())):
            identities=checkpoint.get('identities')
            if not isinstance(identities,dict):return
            self.identities={k:v for k,v in identities.items()
                             if isinstance(k,str) and isinstance(v,str)}

    def _source_key(self, package_id, owner_id, segment):
        # Retrieval ranks, query tags, headers and semantic roles describe how
        # a physical fragment was found; they must not mint a second identity.
        # The extraction/judge caches bind those semantic inputs separately.
        physical={field:segment.get(field) for field in
                  ('message_key','mime_path','segment_id','page_number',
                   'sheet_name','cell_range','char_start','char_end')}
        physical['text']=str(segment.get('text') or '')
        return fingerprint([package_id,owner_id,self.version,physical])

    def _reload(self, key, package_id, owner_id, segment):
        eid=self.identities.get(key)
        reader=getattr(self.repository,'get_evidence',None)
        if not eid or not callable(reader):return None
        value=reader(eid,owner_id)
        if not value or value.get('package_id')!=package_id:return None
        if (value.get('source_revision'),value.get('index_epoch'))!=self.version:return None
        from config import Config
        text=str(segment.get('text') or '')
        quote=text[:2000] if Config.MAIL_ENABLE_AGENT_MINING else text[:1000]
        if value.get('quote_text')!=quote:return None
        if value.get('text_sha256')!=(segment.get('text_sha256') or hashlib.sha256(text.encode()).hexdigest()):return None
        if any(value.get(k)!=segment.get(k) for k in
               ('message_key','segment_id','mime_path','char_start','page_number','sheet_name','cell_range')):return None
        # get_evidence reloads current OCR/native provenance; no old verdicts
        # or mutable processing metadata are restored from the checkpoint.
        return {k:v for k,v in value.items() if k in
                ('evidence_id','quote_text','text_origin','extraction_method','source_text_notice','ocr_quality')}

    def create_evidence_batch(self, package_id, owner_id, segments):
        if (package_id,owner_id)!=(self.package_id,self.owner_id):
            raise ValueError('证据快照不得跨越当前用户和资料包')
        keys = [self._source_key(package_id,owner_id,row) for row in segments]
        missing = {}; positions = []
        for key, row in zip(keys, segments):
            if key not in self.evidence:
                restored=self._reload(key,package_id,owner_id,row)
                if restored is not None:self.evidence[key]=restored
            if key not in self.evidence:
                missing.setdefault(key, row)
            else:
                self.stats['evidence_hits'] += 1
            positions.append(key)
        if missing:
            writer = getattr(self.repository, 'create_evidence_batch', None)
            values = (writer(package_id, owner_id, list(missing.values())) if writer else
                      [self.repository.create_evidence(package_id, owner_id, row) for row in missing.values()])
            self.evidence.update(zip(missing, values))
            self.identities.update((key,value['evidence_id']) for key,value in zip(missing,values))
        return [deepcopy(self.evidence[key]) for key in positions]

    def relevance_sources(self, keys, include):
        """Three queries per chunk instead of three queries per mail; no truncation."""
        if not self.repository.get_package(self.package_id, self.owner_id):
            raise ValueError('邮件资料包不存在或不属于当前用户')
        result = {key: {'rows': [], 'missing': 0, 'partial': 0} for key in keys}
        with self.repository._connection() as connection:
            for offset in range(0, len(keys), 300):
                chunk = keys[offset:offset + 300]
                marks = ','.join('?' for _ in chunk)
                params = [self.package_id] + chunk
                for row in connection.execute('SELECT * FROM mail_text_segments WHERE package_id=? AND message_key IN (' + marks + ') ORDER BY message_key,source_kind,mime_path,ordinal', params):
                    if include or row['source_kind'] != 'attachment':
                        result[row['message_key']]['rows'].append(dict(row))
                if include:
                    for row in connection.execute("SELECT p.message_key,COUNT(*) FROM mail_parts p WHERE p.is_explicit_attachment=1 AND p.message_key IN (" + marks + ") AND NOT EXISTS (SELECT 1 FROM mail_text_segments s WHERE s.package_id=? AND s.message_key=p.message_key AND s.mime_path=p.mime_path AND s.source_kind='attachment' AND LENGTH(s.text)>0) GROUP BY p.message_key", chunk + [self.package_id]):
                        result[row[0]]['missing'] = row[1]
                    for row in connection.execute("SELECT message_key,COUNT(*) FROM mail_part_processing WHERE message_key IN (" + marks + ") AND stage='basic_text' AND (status IN ('partial','failed','unsupported') OR coverage_ratio<1) GROUP BY message_key", chunk):
                        result[row[0]]['partial'] = row[1]
                self.stats['bulk_reads'] += 1
        return result


def dedicated_mail_model(model, queue_timeout):
    """Give mail its own bounded queue allowance without changing shared clients.

    Production transports enforce queue_timeout both before the shared generation
    gate and before their local semaphore. Shallow copies keep the original
    semaphore, endpoint and model configuration; normal provider context and
    task deadlines still apply. Opaque or immutable adapters retain their current
    behavior, including older transports without a separate queue allowance.
    """
    from copy import copy
    import math
    if model is None:
        return model
    try:
        transport = getattr(model, 'transport', None)
        if transport is None or not hasattr(transport, 'queue_timeout'):
            return model
        if isinstance(queue_timeout, bool):
            return model
        allowance = float(queue_timeout)
        if not math.isfinite(allowance):
            return model
        allowance = max(1.0, min(300.0, allowance))
        dedicated_transport = copy(transport)
        dedicated_model = copy(model)
        if dedicated_transport is transport or dedicated_model is model:
            return model
        dedicated_transport.queue_timeout = allowance
        dedicated_model.transport = dedicated_transport
        if (getattr(dedicated_model, 'transport', None) is not dedicated_transport
                or dedicated_transport.queue_timeout != allowance):
            return model
        return dedicated_model
    except Exception:
        # Optional queue tuning must not break existing compatible model adapters.
        return model
