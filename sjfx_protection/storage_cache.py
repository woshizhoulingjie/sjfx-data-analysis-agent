"""Actual stored row provenance for the ordinary analysis database.

This adapter resolves ownership and package roots from persisted foreign keys.
HTTP IDs and SQL parameters never supply row ownership. The reusable SQL guard
also protects JSON projections and joins that do not use Storage getters.
"""
from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path

from . import business
from .sql_cache import checked_connection


PAYLOAD_TABLES = (
    'scans', 'summaries', 'unified_documents', 'package_analyses',
    'inventory_entries', 'tree_edits', 'scan_overviews', 'analysis_overviews',
    'analysis_progress', 'tree_nodes', 'file_previews', 'package_content_maps',
    'relationship_catalog', 'package_overviews', 'homogeneous_analyses',
    'homogeneous_records', 'homogeneous_relations', 'homogeneous_cases',
    'homogeneous_anomalies', 'document_translations', 'translation_memory',
    'conversations', 'conversation_messages', 'conversation_analysis_steps',
    'conversation_turn_events', 'conversation_turn_evidence',
    'conversation_claims', 'conversation_research_memory', 'evidence_index',
)
SPECS = {table: {'content': ('payload',),
                 'identity': ('scan_id', 'node_path', 'source_path',
                              'archive_source_path', 'owner_id', 'session_id',
                              'turn_id', 'root_path')}
         for table in PAYLOAD_TABLES}
SPECS.update({
    'embedding_cache': {'content': ('vector',), 'identity': ('model', 'cache_key')},
    'retrieval_sessions': {'content': ('evidence_ids',), 'identity': ('scan_id', 'scope')},
    'conversation_turns': {'content': ('question', 'plan', 'result', 'verification'),
                           'identity': ('owner_id', 'scan_id', 'session_id')},
    'analysis_jobs': {'content': ('result',), 'identity': ('owner_id', 'scan_id')},
    'file_relation_features': {'content': ('feature_value',),
                               'identity': ('scan_id', 'node_path', 'feature_kind')},
    'file_workflow_states': {'content': ('score_components', 'reasons'),
                             'identity': ('scan_id', 'node_path')},
    'evidence_fts': {'content': ('section', 'text'),
                     'identity': ('scan_id', 'index_key', 'source_path', 'archive_source_path'),
                     'fts': ('scan_id UNINDEXED', 'index_key UNINDEXED',
                             'source_path UNINDEXED', 'archive_source_path UNINDEXED',
                             'section', 'text')},
})
SPECS['homogeneous_records']['content'] += ('subject', 'sender', 'recipient', 'document_number')
SPECS['homogeneous_cases']['content'] += ('title',)
SPECS['conversation_claims']['content'] += ('text', 'evidence_ids')
SPECS['conversation_turn_events']['content'] += ('message',)
SPECS['conversations']['content'] += ('title',)

def _cancel_event_metadata(db,row):
    """A fixed cancellation notice contains no source-derived content."""
    if (row.get('event_type')!='cancelled' or row.get('stage')!='cancelled' or
        row.get('progress')!=100 or row.get('message')!='用户已取消本轮分析' or
        row.get('payload')!='{}'):
        return False
    turn=db.execute('SELECT owner_id FROM conversation_turns WHERE id=?',
                    (row.get('turn_id'),)).fetchone()
    return bool(turn and turn[0]==(business.context() or {}).get('owner'))

SPECS['conversation_turn_events']['metadata']=_cancel_event_metadata


def _owned_control_payload(db, row, column):
    """Control producers use persisted scan ownership, never a supplied owner."""
    scan = db.execute('SELECT owner_id FROM scans WHERE id=?', (row.get('scan_id'),)).fetchone()
    owner = (business.context() or {}).get('owner')
    if not scan or not owner or scan[0] != owner or (row.get('owner_id') and row['owner_id'] != owner):
        return None
    try:
        payload = json.loads(row.get(column) or '{}')
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _job_control_metadata(db, row):
    payload = _owned_control_payload(db, row, 'result')
    if payload is None or set(payload) - {'scan_id', 'scan_available', '_await_selection'}:
        return False
    return (('scan_id' not in payload or payload['scan_id'] == row.get('scan_id')) and
            all(type(payload[key]) is bool for key in ('scan_available', '_await_selection') if key in payload))


def _progress_control_metadata(db, row):
    payload = _owned_control_payload(db, row, 'payload')
    if payload is None or set(payload) - {'schema_version', 'progress', 'status', 'stage', 'message'}:
        return False
    states = {'running', 'queued', 'failed', 'cancelled', 'completed', 'paused', 'awaiting_selection',
              'checkpoint_requeued', 'analyzing', 'analysis_preparing', 'scanning', 'inventory_ready'}
    if payload.get('schema_version', 'analysis-progress/1.0') != 'analysis-progress/1.0':
        return False
    if any(not isinstance(payload[key], str) or payload[key] not in states for key in ('status', 'stage') if key in payload):
        return False
    if 'progress' in payload and (type(payload['progress']) not in (int, float) or not 0 <= payload['progress'] <= 100):
        return False
    message = payload.get('message', '')
    if not isinstance(message, str):
        return False
    if message in {'', '分析失败，请查看任务错误状态。', '分析已取消；已完成的文件检查点仍保留。',
                   '已安全暂停；已完成结果永久保留，未完成文件仍在待处理队列。',
                   '本轮达到运行时限，已保存检查点并等待自动续批。',
                   '数据分析、结构化摘要和报告已完成', '正式概览已生成，可查看和下载。'}:
        return True
    # The inventory-only notice contains fixed product text and numeric sizes,
    # never a filename, exception, model output or document excerpt.
    return bool(isinstance(message, str) and re.fullmatch(
        r'目录导入完成，尚未读取正文，等待选择文件。'
        r'(?:该目录已达到大数据包阈值（\d+ 个文件 / [\d.]+ [A-Za-z]+；阈值 \d+ 个文件或 [\d.]+ [A-Za-z]+）；'
        r'本次仍按普通文件导入处理，如需分阶段目录与按范围深度摘要，建议改用「大数据包导入」。)?', message))


SPECS['analysis_jobs']['metadata'] = _job_control_metadata
SPECS['analysis_progress']['metadata'] = _progress_control_metadata

# Queue rows share a table with scan jobs, but these persisted task types use
# canonical mail package IDs. Keep the set independent of optional worker
# imports and feature flags; disabled tasks must retain their stored provenance.
MAIL_JOB_TYPES = frozenset({
    'mail_import', 'mail_extract', 'mail_ocr', 'mail_embed', 'mail_factextract',
    'mail_research', 'mail_export', 'mail_foundation',
})


class StoredScopes:
    def __init__(self):
        self._scans = {}
        self._registrations = None
        self._resolved = {}

    def registrations(self):
        if self._registrations is not None:
            return self._registrations
        ctx = business.context() or {}
        contexts = ctx.get('package_contexts') or [ctx]
        result = []
        for item in contexts:
            if not item.get('owner') or not item.get('registration_id'):
                continue
            p = business.protection()
            reg = p.package._registration(item['owner'], item['registration_id'])
            result.append((item['owner'], reg['id'], Path(reg['root']).resolve()))
        self._registrations = result
        return result

    def scan(self, db, scan_id):
        if not scan_id:
            return None
        if scan_id not in self._scans:
            value = db.execute('SELECT owner_id,root_path FROM scans WHERE id=?', (scan_id,)).fetchone()
            self._scans[scan_id] = dict(value) if value else None
        return self._scans[scan_id]

    def rows(self, db, table):
        # The FTS mirror is rebuilt only for actual scans in the current source
        # roots. Each returned row still passes the exact row/ownership guard.
        clauses=[];values=[]
        for owner, rid, root in self.registrations():
            text=str(root)
            prefix=text.rstrip('/\\') + ('\\' if '\\' in text else '/')
            clauses.append('(s.owner_id=? AND (s.root_path=? OR substr(s.root_path,1,?)=?))')
            values.extend((owner,text,len(prefix),prefix))
        if not clauses:return []
        return db.execute('SELECT t.rowid AS __rowid__,t.* FROM "'+table+'" t '
                          'JOIN scans s ON s.id=t.scan_id WHERE '+ ' OR '.join(clauses),values)

    def __call__(self, db, table, row):
        # Control requests carry an authenticated owner, without content-source
        # receipts. Only exact typed notices may use that persisted scan scope.
        if (business.metadata_projection.get() is True and
                table in {'analysis_jobs', 'analysis_progress'} and
                SPECS[table]['metadata'](db, row)):
            return [{'owner': business.context()['owner'], 'scope': str(row['scan_id'])}]
        current = self.registrations()
        if not current:
            return []
        # These content-addressed caches have no package FK. Their proof still
        # freezes their actual producer's source versions under the real owner.
        if table in {'translation_memory', 'embedding_cache'}:
            return [{'owner': owner, 'scope': rid} for owner, rid, root in current]
        scan_id = row.get('id') if table == 'scans' else row.get('scan_id')
        if not scan_id and row.get('session_id'):
            found = db.execute('SELECT scan_id FROM conversations WHERE id=?',
                               (row['session_id'],)).fetchone()
            scan_id = found[0] if found else None
        if not scan_id and row.get('turn_id'):
            found = db.execute('SELECT scan_id FROM conversation_turns WHERE id=?',
                               (row['turn_id'],)).fetchone()
            scan_id = found[0] if found else None
        info = self.scan(db, scan_id)
        if info is None:
            return []
        actual = Path(info['root_path']).resolve()
        if row.get('owner_id') and row['owner_id'] != info['owner_id']:
            return []
        allowed = [(owner, rid) for owner, rid, root in current
                   if info['owner_id'] == owner and
                   (actual == root or actual.is_relative_to(root))]
        # A passing parent cannot authorize an independently registered child.
        # Resolve persisted roots and physical file locators with the same
        # deepest-registration rule used at HTTP admission.
        targets = [actual]
        locator = row.get('archive_source_path') or row.get('source_path') or row.get('node_path')
        if locator:
            physical = str(locator).split('::', 1)[0]
            path = Path(physical)
            path = path.resolve() if path.is_absolute() else (actual / path).resolve()
            if path.exists():
                targets.append(path)
        for target in targets:
            key = (info['owner_id'], str(target))
            if key not in self._resolved:
                self._resolved[key] = business.protection().package.resolve(info['owner_id'], root=str(target))['id']
            allowed = [(owner, rid) for owner, rid in allowed if rid == self._resolved[key]]
        return [{'owner': owner, 'scope': str(scan_id)} for owner, rid in allowed]


@contextlib.contextmanager
def connection(db, namespace):
    if not business.enabled():
        yield db
        return
    from .mail_cache import SPECS as mail_specs, MailScopes
    ordinary = StoredScopes()
    mail = MailScopes()
    def scopes(raw, table, row):
        if table == 'analysis_jobs' and row.get('task_type') in MAIL_JOB_TYPES:
            package_id = row.get('scan_id')
            if not (isinstance(package_id, str) and package_id.startswith('mp_') and row.get('owner_id')):
                return []
            # Use the actual queued FK, never caller options. MailScopes checks
            # persisted package ownership, source root and deepest registration.
            return mail(raw, table, {**row, 'package_id': package_id})
        return mail(raw, table, row) if table in mail_specs else ordinary(raw, table, row)
    scopes.rows=lambda raw,table: mail.rows(raw,table) if table in mail_specs else ordinary.rows(raw,table)
    with checked_connection(db, namespace, {**SPECS, **mail_specs}, scopes) as guarded:
        yield guarded
