"""Mail table content specifications and actual stored ownership/row scopes."""
from __future__ import annotations

from pathlib import Path
import json

from . import business
from .sql_cache import GuardedConnection, checked_connection


def _spec(content, *identity, **extra):
    return {'content': tuple(content.split()), 'identity': tuple(identity), **extra}


# Queue state, source locators, inventories, schema ledgers and package metadata
# are deliberately absent. Content/projection/result columns always have proofs.
SPECS = {
    'mail_messages': _spec('canonical_message_id_raw canonical_message_id_normalized subject_raw subject_normalized date_raw sent_at_utc headers_json', 'message_key', 'raw_sha256'),
    'mail_addresses': _spec('raw_address_sample display_name_sample', 'address_id', 'normalized_address'),
    'mail_message_participants': _spec('role position', 'message_key', 'address_id'),
    'mail_parts': _spec('filename_raw content_type content_disposition content_id payload_sha256', 'message_key', 'mime_path'),
    'mail_part_processing': _spec('text_sha256', 'message_key', 'mime_path', 'stage'),
    'mail_text_segments': _spec('text text_sha256', 'package_id', 'message_key', 'mime_path', 'source_kind', 'ordinal', 'page_number', 'sheet_name', 'cell_range', 'char_start', 'char_end', 'source_revision', 'index_epoch'),
    'mail_raw_store': _spec('raw_bytes', 'raw_sha256'),
    'mail_body_fragments': _spec('text', 'message_key', 'mime_path', 'ordinal', 'fragment_kind', 'char_start', 'char_end', 'method'),
    'mail_search_fts': _spec('text', 'segment_id', 'package_id', 'message_key', 'mime_path', 'field', 'language',
                             fts=('segment_id UNINDEXED', 'package_id UNINDEXED', 'message_key UNINDEXED',
                                  'mime_path UNINDEXED', 'field UNINDEXED', 'language UNINDEXED', 'text')),
    'mail_embeddings': _spec('vector text_sha256', 'package_id', 'segment_id', 'model', 'dim', 'index_epoch'),
    'mail_embedding_buckets': _spec('bucket', 'package_id', 'segment_id', 'model', 'band'),
    'mail_thread_edges': _spec('source_header target_message_id_raw evidence_json', 'package_id', 'source_message_key', 'target_message_key', 'relation_type'),
    'mail_communication_edges': _spec('occurrence_count', 'package_id', 'message_key', 'from_address_id', 'to_address_id', 'recipient_role'),
    'mail_communication_daily': _spec('message_count occurrence_count', 'package_id', 'day', 'from_address_id', 'to_address_id', 'recipient_role'),
    'mail_business_relations': _spec('subject_ref predicate object_ref evidence_ids_json review_note', 'package_id', 'created_by'),
    'mail_evidence': _spec('quote_text source_sha256 payload_sha256 text_sha256', 'package_id', 'message_key', 'occurrence_id', 'mime_path', 'segment_id', 'page_number', 'sheet_name', 'cell_range', 'char_start', 'char_end', 'source_revision', 'index_epoch'),
    'mail_result_sets': _spec('definition_json coverage_json', 'package_id', 'owner_id', 'source_revision', 'index_epoch', 'parent_result_set_id'),
    'mail_result_set_items': _spec('item_type item_id rank rationale_json', 'result_set_id'),
    'mail_research_sessions': _spec('title scope_json', 'package_id', 'owner_id', 'source_revision'),
    'mail_research_turns': _spec('question task_spec_json final_json', 'session_id', 'owner_id', 'source_revision', 'index_epoch'),
    'mail_research_steps': _spec('input_json output_ref', 'turn_id', 'sequence', 'tool_name'),
    'mail_research_events': _spec('payload_json', 'turn_id', 'sequence', 'event_type'),
    'mail_claims': _spec('claim_text evidence_ids_json counter_evidence_ids_json verification_json', 'turn_id', 'claim_type'),
    'mail_conversations': _spec('subject_normalized merge_basis', 'package_id', 'root_message_key'),
    'mail_conversation_members': _spec('depth is_root', 'conversation_id', 'message_key'),
    'mail_facts': _spec('fact_key subject_ref value_raw value_normalized confidence', 'package_id', 'message_key', 'evidence_id', 'segment_id', 'fact_type', 'unit', 'source_revision', 'index_epoch'),
    'mail_fact_conflicts': _spec('fact_ids_json reason', 'package_id', 'fact_type', 'fact_key', 'subject_ref'),
    'mail_entities': _spec('display_name basis', 'package_id', 'entity_type'),
    'mail_entity_addresses': _spec('basis', 'package_id', 'entity_id', 'address_id'),
    'mail_entity_audit': _spec('payload_json', 'package_id', 'entity_id', 'actor_id', 'action'),
    'mail_attachment_versions': _spec('filename_normalized revision_count', 'package_id', 'chain_key', 'payload_sha256', 'first_message_key', 'last_message_key'),
    'mail_attachment_version_links': _spec('payload_sha256', 'package_id', 'chain_key', 'message_key', 'mime_path', 'version_ordinal'),
    'mail_thread_nodes': _spec('message_id_normalized node_kind', 'package_id', 'generation_id', 'message_key'),
    'mail_thread_edge_history': _spec('source_header target_message_id_raw evidence_json', 'package_id', 'generation_id', 'source_message_key', 'target_message_key'),
    'mail_thread_anomalies': _spec('payload_json', 'package_id', 'generation_id', 'message_key', 'anomaly_type'),
    'mail_conversation_history': _spec('subject_normalized merge_basis', 'package_id', 'generation_id', 'root_message_key'),
    'mail_conversation_member_history': _spec('depth is_root', 'generation_id', 'conversation_id', 'message_key'),
    'mail_conversation_corrections': _spec('payload_json', 'package_id', 'actor_id', 'action'),
    'mail_communication_acts': _spec('actor act_type object_identity object_version payload_json', 'package_id', 'generation_id', 'message_key', 'source_hash'),
    'mail_issues': _spec('title payload_json', 'package_id', 'anchor_key'),
    'mail_issue_memberships': _spec('decision_json', 'package_id', 'generation_id', 'act_id', 'issue_id', 'event_ref_id'),
    'mail_assertion_links': _spec('payload_json', 'package_id', 'generation_id', 'source_act_id', 'target_act_id', 'relation_type'),
    'mail_issue_states': _spec('payload_json', 'package_id', 'issue_id', 'scope_fingerprint', 'dependency_fingerprint', 'engine_version'),
    'mail_act_extraction_cache': _spec('slice_json payload_json', 'package_id', 'owner_id', 'message_key', 'source_hash', 'scope_fingerprint', 'extraction_version', 'model_version', 'attribution_fingerprint'),
}


def metadata_event(db, row):
    if row.get('event_type') not in {'cancel_requested', 'paused', 'cancelled'}:
        return False
    try:
        payload = json.loads(row.get('payload_json') or '{}')
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict) or set(payload) - {'job_id', 'status', 'counts'}:
        return False
    if 'status' in payload and payload['status'] not in {'paused', 'cancelling', 'cancelled'}:
        return False
    if 'counts' in payload:
        counts = payload['counts']
        if (not isinstance(counts, dict) or set(counts) - {'total', 'ready', 'failed', 'pending', 'completed'} or
                any(type(value) is not int or not 0 <= value <= 10**12 for value in counts.values())):
            return False
    if 'job_id' in payload:
        job_id = payload['job_id']
        if not isinstance(job_id, str) or len(job_id) > 128:
            return False
        turn = db.execute('SELECT job_id FROM mail_research_turns WHERE turn_id=?', (row.get('turn_id'),)).fetchone()
        if not turn or job_id != turn[0]:
            return False
    return True


SPECS['mail_research_events']['metadata'] = metadata_event


class MailScopes:
    """Only real relational package ownership and approved source roots count."""
    def __init__(self):
        self._registrations = None
        self._packages = {}

    def registrations(self):
        if self._registrations is not None:
            return self._registrations
        ctx = business.context() or {}
        result = []
        for item in ctx.get('package_contexts') or [ctx]:
            if not item.get('owner') or not item.get('registration_id'):
                continue
            reg = business.protection().package._registration(item['owner'], item['registration_id'])
            result.append((item['owner'], item['registration_id'], Path(reg['root']).resolve()))
        self._registrations = result
        return result

    def package(self, db, package_id):
        if package_id not in self._packages:
            row = db.execute('SELECT owner_id,source_path FROM mail_packages WHERE package_id=?', (package_id,)).fetchone()
            self._packages[package_id] = dict(row) if row else None
        return self._packages[package_id]

    def package_ids(self, db, row):
        if row.get('package_id'):
            return [row['package_id']]
        if row.get('turn_id'):
            return [item[0] for item in db.execute('SELECT s.package_id FROM mail_research_turns t JOIN mail_research_sessions s ON s.session_id=t.session_id WHERE t.turn_id=?', (row['turn_id'],))]
        if row.get('session_id'):
            return [item[0] for item in db.execute('SELECT package_id FROM mail_research_sessions WHERE session_id=?', (row['session_id'],))]
        if row.get('result_set_id'):
            return [item[0] for item in db.execute('SELECT package_id FROM mail_result_sets WHERE result_set_id=?', (row['result_set_id'],))]
        if row.get('conversation_id'):
            return [item[0] for item in db.execute('SELECT package_id FROM mail_conversations WHERE conversation_id=?', (row['conversation_id'],))]
        if row.get('message_key'):
            return [item[0] for item in db.execute('SELECT DISTINCT package_id FROM mail_occurrences WHERE message_key=?', (row['message_key'],))]
        if row.get('raw_sha256'):
            return [item[0] for item in db.execute('SELECT DISTINCT package_id FROM mail_occurrences WHERE raw_sha256=?', (row['raw_sha256'],))]
        if row.get('address_id'):
            return [item[0] for item in db.execute('SELECT DISTINCT o.package_id FROM mail_message_participants p JOIN mail_occurrences o ON o.message_key=p.message_key WHERE p.address_id=?', (row['address_id'],))]
        return []

    def __call__(self, db, table, row):
        current = self.registrations()
        if not current:
            return []
        result = []
        for package_id in set(self.package_ids(db, row)):
            package = self.package(db, package_id)
            if not package or row.get('owner_id') and row['owner_id'] != package['owner_id']:
                continue
            actual = Path(package['source_path']).resolve()
            allowed = [(owner, rid) for owner, rid, root in current
                       if owner == package['owner_id'] and (actual == root or actual.is_relative_to(root))]
            if allowed:
                selected = business.protection().package.resolve(package['owner_id'], root=str(actual))['id']
                allowed = [(owner, rid) for owner, rid in allowed if rid == selected]
            if allowed:
                result.append({'owner': package['owner_id'], 'scope': package_id})
        return result

    def rows(self, db, table):
        # FTS read rebuild and non-single-row writer snapshots inspect only real
        # currently scoped packages, rather than every owner's full mail index.
        if table == 'mail_search_fts':
            current = self.registrations()
            packages = []
            for row in db.execute('SELECT package_id,owner_id,source_path FROM mail_packages'):
                actual = Path(row['source_path']).resolve()
                if any(owner == row['owner_id'] and (actual == root or actual.is_relative_to(root)) for owner, rid, root in current):
                    packages.append(row['package_id'])
            if not packages:
                return []
            return db.execute('SELECT rowid AS __rowid__,* FROM main.mail_search_fts WHERE package_id IN (' + ','.join('?' for _ in packages) + ')', packages).fetchall()
        return db.execute('SELECT rowid AS __rowid__,* FROM main."' + table.replace('"', '""') + '"').fetchall()


def connection(db, namespace):
    # Storage's unified guard already includes these mail tables. Its single
    # trigger journal and callbacks must not be shadowed by a second wrapper.
    if isinstance(db, GuardedConnection):
        return _existing(db)
    return checked_connection(db, namespace, SPECS, MailScopes())


def _existing(db):
    from contextlib import nullcontext
    return nullcontext(db)


def raw_production(db, raw, name):
    from contextlib import nullcontext
    if not business.enabled():
        return nullcontext(db)
    return db.raw_regeneration(raw, name, ('mail_messages', 'mail_parts', 'mail_addresses'))


def require_current(operation='analyze'):
    from . import cache
    sources = cache.capture_sources()
    if sources:
        p = business.protection()
        for source in sources:
            cache._require_source(p, source, source['context']['owner'], operation)
    return sources


class ProtectedMailModel:
    def __init__(self, model):
        self._model = model

    def __getattr__(self, name):
        value = getattr(self._model, name)
        if name not in {'chat_json', 'chat', 'generate'} or not callable(value):
            return value
        def checked(*args, **kwargs):
            require_current('analyze')
            result = value(*args, **kwargs)
            require_current('analyze')
            return result
        return checked


def protected_model(model):
    # Always install the lazy proxy so a runtime ON transition is covered. OFF
    # require_current returns before touching any provenance DB or source.
    return model if model is None or isinstance(model, ProtectedMailModel) else ProtectedMailModel(model)
