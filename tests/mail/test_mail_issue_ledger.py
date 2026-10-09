"""Synthetic ledger contracts; no application, model, network or production data."""
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

# An explicit local-only runner can test these stdlib modules even on the host's
# Python 3.7 without importing the application's optional web/model dependencies.
if os.environ.get('SJFX_LEDGER_STDLIB_TEST') == '1' and 'services.mail' not in sys.modules:
    package = types.ModuleType('services.mail')
    package.__path__ = [str(Path(__file__).resolve().parents[2] / 'services' / 'mail')]
    sys.modules['services.mail'] = package

from services.mail.communication_acts import canonical
from services.mail.issue_repository import (IssueRepository, issue_message_scope, search_semantic_ledger,
                                           ledger_dependency, publish_workflow, align_workflow_findings)
from services.mail import schema


class SyntheticRepository:
    def __init__(self, path):
        self.path = str(path)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class IssueLedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.repo = SyntheticRepository(Path(self.directory.name) / 'synthetic.db')
        self.ledger = IssueRepository(self.repo)
        with self.repo._connection() as c:
            schema.migrate(c)
            for pid, owner in [('p', 'owner'), ('foreign', 'other'), ('sameowner', 'owner')]:
                c.execute('INSERT INTO mail_packages(package_id,owner_id,display_name,source_kind,source_path,source_revision,status) '
                          'VALUES(?,?,?,?,?,?,?)', (pid, owner, pid, 'synthetic', '.', 'r1', 'ready'))
        self.ordinal = 0

    def tearDown(self):
        self.directory.cleanup()

    def add(self, text, actor='a@example.com', to='b@example.com', date=None, pid='p', source_kind='body', quote_kind='authored'):
        self.ordinal += 1
        key, segment = 'm'+str(self.ordinal), 's'+str(self.ordinal)
        date = date or '2026-09-%02dT08:00:00+00:00' % self.ordinal
        raw_hash = hashlib.sha256(key.encode()).hexdigest()
        with self.repo._connection() as c:
            c.execute('INSERT INTO mail_messages(message_key,raw_sha256,sent_at_utc,subject_raw,parse_status,parser_version,source_revision) '
                      'VALUES(?,?,?,?,?,?,?)', (key, raw_hash, date, 'Identical title', 'parsed', 'synthetic', 'r1'))
            c.execute('INSERT INTO mail_occurrences(occurrence_id,package_id,source_id,message_key,source_member_name,source_member_ordinal,raw_sha256) '
                      'VALUES(?,?,?,?,?,?,?)', ('o'+key, pid, 'source', key, key+'.eml', self.ordinal, raw_hash))
            for position, (address, role) in enumerate([(actor, 'from'), (to, 'to')]):
                aid = 'address_'+address
                c.execute('INSERT OR IGNORE INTO mail_addresses(address_id,normalized_address) VALUES(?,?)', (aid, address))
                c.execute('INSERT INTO mail_message_participants(message_key,address_id,role,position) VALUES(?,?,?,?)', (key, aid, role, position))
            c.execute('INSERT INTO mail_text_segments(segment_id,package_id,message_key,mime_path,source_kind,ordinal,char_start,char_end,text,text_sha256,source_revision) '
                      'VALUES(?,?,?,?,?,?,?,?,?,?,?)', (segment, pid, key, '1', source_kind, 0, 0, len(text), text,
                                                       hashlib.sha256(text.encode()).hexdigest(), 'r1'))
            if source_kind == 'body':
                c.execute('INSERT INTO mail_body_fragments(message_key,mime_path,ordinal,fragment_kind,char_start,char_end,text,method,confidence) '
                          'VALUES(?,?,?,?,?,?,?,?,?)', (key, '1', 0, quote_kind, 0, len(text), text, 'synthetic', 'high'))
        return {'message_key': key, 'segment_id': segment, 'mime_path': '1', 'quote': text, 'char_start': 0,
                'char_end': len(text), 'source_kind': source_kind, 'sent_at': date, 'authored': True,
                'quotation_warning': quote_kind != 'authored'}

    def finding(self, citation, act_type='propose', actor='a@example.com', version='v1', anchor=True, **extra):
        value = {'actor': actor, 'kind': 'action', 'act_type': act_type, 'intent': act_type,
                 'issue': '合同', 'statement': citation['quote'], 'object': 'contract', 'object_version': version,
                 'semantic_status': 'supported', 'conditions': [], 'citations': [citation]}
        if anchor:
            value['issue_anchor'] = {'kind': 'contract_id', 'value': 'C-42', 'object': 'contract'}
        value.update(extra)
        return value

    def ingest(self, finding, **kwargs):
        result = self.ledger.ingest_checked_findings('p', 'owner', [finding], **kwargs)
        self.assertEqual(result['rejected'], [])
        self.assertEqual(len(result['acts']), 1)
        return result['acts'][0]

    def test_append_migration_preserves_historical_checksums_and_is_idempotent(self):
        with self.repo._connection() as c:
            sources = [(2, schema.DDL+schema.FTS_DDL+schema.FTS_FALLBACK_DDL), (3, schema.DDL_V3),
                       (4, schema.DDL_V4+schema.DDL_V4_COLUMNS), (5, schema.DDL_V5)]
            for version, source in sources:
                self.assertEqual(c.execute('SELECT checksum FROM mail_schema_migrations WHERE version=?', (version,)).fetchone()[0],
                                 hashlib.sha256(source.encode()).hexdigest())
            self.assertFalse(schema.migrate(c))
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_v6_failure_rolls_back_and_resumes(self):
        c = sqlite3.connect(':memory:')
        c.row_factory = sqlite3.Row
        original = schema._apply_v6
        def fail(database):
            original(database)
            raise RuntimeError('synthetic interruption')
        with patch.object(schema, '_apply_v6', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'interruption'):
                schema.migrate(c)
        self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='mail_issues'").fetchone())
        self.assertIsNone(c.execute('SELECT 1 FROM mail_schema_migrations WHERE version=6').fetchone())
        self.assertTrue(schema.migrate(c))
        self.assertFalse(schema.migrate(c))
        c.close()

    def test_stable_ids_evidence_and_generation_ignore_turn_ids(self):
        citation = self.add('Contract C-42 proposal version v1')
        finding = self.finding(citation, finding_id='turn-one:1')
        first = self.ledger.ingest_checked_findings('p', 'owner', [finding])
        finding['finding_id'] = 'different-turn:12'
        second = self.ledger.ingest_checked_findings('p', 'owner', [finding])
        self.assertEqual(first['acts'][0]['act_id'], second['acts'][0]['act_id'])
        self.assertEqual(first['generation_id'], second['generation_id'])
        self.assertTrue(second['idempotent_replay'])
        with self.repo._connection() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM mail_evidence').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM mail_communication_acts').fetchone()[0], 1)

    def test_equal_titles_threads_and_confidence_do_not_merge(self):
        a = self.ingest(self.finding(self.add('First unrelated contract proposal.'), anchor=False))
        b = self.ingest(self.finding(self.add('Second unrelated contract proposal.'), anchor=False))
        self.assertNotEqual(a['issue_id'], b['issue_id'])
        out = self.ledger.apply_checked_links('p', 'owner', [{'source_act_id': b['act_id'], 'target_issue_id': a['issue_id'],
                 'decision': 'same_issue', 'confidence': 0.999, 'review_status': 'unresolved', 'method': 'vector_similarity'}])
        self.assertEqual(out['applied'], [])
        self.assertTrue(out['rejected'])

    def test_exact_anchor_keeps_different_actors_versions_and_conditions(self):
        first = self.ingest(self.finding(self.add('Contract C-42 v1 is rejected.', actor='b@example.com', to='a@example.com'),
                                        act_type='reject', actor='b@example.com'))
        second = self.ingest(self.finding(self.add('Contract C-42 v2 accepted after approval.', actor='b@example.com', to='a@example.com'),
                              act_type='accept', actor='b@example.com', version='v2', conditions=['after approval']))
        self.assertEqual(first['issue_id'], second['issue_id'])
        states = self.ledger.issue_timeline('p', 'owner', first['issue_id'])['states']
        self.assertEqual({s['object_version']: s['status'] for s in states}, {'v1': 'rejected', 'v2': 'conditional_accept'})
        self.assertEqual(states[1]['conditions'][0]['satisfaction'], 'unverified')

    def test_actor_disagreement_is_not_consensus(self):
        a = self.ingest(self.finding(self.add('Contract C-42 v1 accepted.'), act_type='accept'))
        b = self.ingest(self.finding(self.add('Contract C-42 v1 rejected.', actor='b@example.com', to='a@example.com'),
                                    act_type='reject', actor='b@example.com'))
        self.assertEqual(a['issue_id'], b['issue_id'])
        states = self.ledger.issue_timeline('p', 'owner', a['issue_id'])['states']
        self.assertEqual({s['actor']: s['status'] for s in states}, {'a@example.com': 'accepted', 'b@example.com': 'rejected'})

    def test_received_and_cc_never_become_acceptance(self):
        citation = self.add('Contract C-42 accepted by the sender.')
        with self.repo._connection() as c:
            c.execute("UPDATE mail_message_participants SET role='cc' WHERE message_key=? AND role='to'", (citation['message_key'],))
        act = self.ingest(self.finding(citation, actor='b@example.com', act_type='accept', kind='received'))
        states = self.ledger.issue_timeline('p', 'owner', act['issue_id'])['states']
        self.assertEqual(states[0]['status'], 'received_information')
        invalid = self.ledger.ingest_checked_findings('p', 'owner', [self.finding(citation, actor='b@example.com', act_type='accept')])
        self.assertFalse(invalid['acts'])

    def test_quoted_and_attachment_sources_do_not_prove_sender_view(self):
        for citation in [self.add('Contract C-42 I accept.', quote_kind='quoted'),
                         self.add('Contract C-42 I accept.', source_kind='attachment')]:
            out = self.ledger.ingest_checked_findings('p', 'owner', [self.finding(citation, act_type='accept')])
            self.assertFalse(out['acts'])
            self.assertTrue(out['rejected'])

    def test_claimed_completion_is_not_real_world_completion(self):
        act = self.ingest(self.finding(self.add('Contract C-42 payment completed.'), act_type='report_completed'))
        state = self.ledger.issue_timeline('p', 'owner', act['issue_id'])['states'][0]
        self.assertEqual(state['status'], 'claimed_completed')
        self.assertEqual(state['real_world_completion'], 'unverified')

    def test_withdrawal_preserves_history_and_other_versions(self):
        first = self.ingest(self.finding(self.add('Contract C-42 v1 commitment.'), act_type='commit'))
        self.ingest(self.finding(self.add('Contract C-42 v2 commitment.'), act_type='commit', version='v2'))
        final = self.ingest(self.finding(self.add('Contract C-42 withdraw v1.'), act_type='withdraw'))
        timeline = self.ledger.issue_timeline('p', 'owner', first['issue_id'])
        states = {s['object_version']: s for s in timeline['states']}
        self.assertEqual(states['v1']['status'], 'withdrawn')
        self.assertEqual(states['v2']['status'], 'committed')
        self.assertEqual(len(states['v1']['history_act_ids']), 2)
        scoped = self.ledger.issue_timeline('p', 'owner', final['issue_id'], scope={'message_keys': [first['message_key']]})
        self.assertEqual(scoped['states'][0]['status'], 'committed')

    def test_late_assertion_about_old_effective_time_does_not_replace_newer_state(self):
        a = self.ingest(self.finding(self.add('Contract C-42 accepted.', date='2026-09-10T08:00:00+00:00'), act_type='accept',
                    effective_at='2026-09-10T08:00:00+00:00'))
        self.ingest(self.finding(self.add('Contract C-42 was rejected earlier.', date='2026-09-20T08:00:00+00:00'), act_type='reject',
                    effective_at='2026-09-01T08:00:00+00:00'))
        timeline = self.ledger.issue_timeline('p', 'owner', a['issue_id'])
        self.assertEqual(timeline['states'][0]['status'], 'accepted')
        historical = self.ledger.issue_timeline('p', 'owner', a['issue_id'], as_of='2026-09-15T00:00:00+00:00')
        self.assertEqual(len(historical['items']), 1)

    def test_old_context_review_is_not_semantic_support(self):
        finding = self.finding(self.add('Contract C-42 accepted.'), act_type='accept', review_status='supported')
        finding.pop('semantic_status')
        act = self.ingest(finding)
        state = self.ledger.issue_timeline('p', 'owner', act['issue_id'])['states'][0]
        self.assertEqual(state['expressed_status'], 'accepted')
        self.assertEqual(state['status'], 'pending_verification')

    def test_scope_owner_package_and_empty_scope_are_enforced(self):
        act = self.ingest(self.finding(self.add('Contract C-42 proposal.')))
        for fn in [lambda: self.ledger.list_issues('p', 'intruder'),
                   lambda: self.ledger.issue_timeline('p', 'intruder', act['issue_id']),
                   lambda: search_semantic_ledger(self.repo, 'p', 'intruder', 'contract')]:
            with self.assertRaises(LookupError):
                fn()
        self.assertFalse(self.ledger.list_issues('p', 'owner', scope={'message_keys': []})['items'])
        self.assertFalse(search_semantic_ledger(self.repo, 'p', 'owner', 'contract', allowed_keys=[])['items'])
        self.assertFalse(self.ledger.list_issues('sameowner', 'owner')['items'])
        with self.assertRaises(LookupError):
            self.ledger.issue_timeline('foreign', 'other', act['issue_id'])

    def test_source_text_mutation_invalidates_acts_even_if_cached_hash_unchanged(self):
        citation = self.add('Contract C-42 proposal.')
        act = self.ingest(self.finding(citation))
        with self.repo._connection() as c:
            c.execute('UPDATE mail_text_segments SET text=? WHERE segment_id=?', ('Changed contents here!', citation['segment_id']))
        self.assertFalse(self.ledger.issue_timeline('p', 'owner', act['issue_id'])['items'])
        self.assertFalse(search_semantic_ledger(self.repo, 'p', 'owner', 'contract')['items'])

    def test_slice_cache_scope_model_and_attribution_binding(self):
        citation = self.add('Contract C-42 accepted. This tail was not read.')
        prefix = citation['quote'][:22]
        source_hash = hashlib.sha256(prefix.encode()).hexdigest()
        sliced = {**citation, 'quote': prefix, 'char_end': len(prefix)}
        description = {'mime_path': '1', 'segment_id': citation['segment_id'], 'char_start': 0, 'char_end': len(prefix),
                       'parties': ['a@example.com']}
        kwargs = {'scope': {'message_keys': [citation['message_key']]}, 'slice_scope': description, 'model_version': 'model-a'}
        self.ledger.put_cached_extraction('p', 'owner', citation['message_key'], source_hash, [self.finding(sliced)], **kwargs)
        hit = self.ledger.get_cached_extraction('p', 'owner', citation['message_key'], source_hash, **kwargs)
        self.assertEqual(hit['completion'], 'slice_only')
        self.assertIsNone(self.ledger.get_cached_extraction('p', 'owner', citation['message_key'], source_hash,
                           **{**kwargs, 'model_version': 'model-b'}))
        self.assertIsNone(self.ledger.get_cached_extraction('p', 'owner', citation['message_key'], source_hash,
                           **{**kwargs, 'scope': {}}))
        with self.repo._connection() as c:
            c.execute("UPDATE mail_body_fragments SET fragment_kind='quoted' WHERE message_key=?", (citation['message_key'],))
        self.assertIsNone(self.ledger.get_cached_extraction('p', 'owner', citation['message_key'], source_hash, **kwargs))

    def test_cache_rejects_out_of_slice_citations_and_source_changes(self):
        citation = self.add('Contract C-42 accepted. Additional source text.')
        description = {'mime_path': '1', 'segment_id': citation['segment_id'], 'char_start': 0, 'char_end': 12}
        source_hash = hashlib.sha256(citation['quote'][:12].encode()).hexdigest()
        with self.assertRaises(ValueError):
            self.ledger.put_cached_extraction('p', 'owner', citation['message_key'], source_hash,
                                             [self.finding(citation)], slice_scope=description)
        with self.assertRaises(ValueError):
            self.ledger.put_cached_extraction('p', 'owner', citation['message_key'], 'falsehash', [], slice_scope=description)

    def test_reviewed_semantic_link_requires_both_sides_and_conflict_checks(self):
        a = self.ingest(self.finding(self.add('First contract proposal.'), anchor=False))
        b = self.ingest(self.finding(self.add('I accept the first contract.'), actor='a@example.com', act_type='accept', anchor=False))
        decision = {'source_act_id': b['act_id'], 'target_issue_id': a['issue_id'], 'decision': 'same_issue',
                    'review_status': 'supported', 'method': 'semantic_support_checked', 'reason': 'Original reply names the first contract.',
                    'evidence_ids': a['evidence_ids'] + b['evidence_ids']}
        rejected = self.ledger.apply_checked_links('p', 'owner', [{**decision, 'evidence_ids': b['evidence_ids']}])
        self.assertFalse(rejected['applied'])
        applied = self.ledger.apply_checked_links('p', 'owner', [decision])
        self.assertEqual(len(applied['applied']), 1)
        timeline = self.ledger.issue_timeline('p', 'owner', a['issue_id'])
        self.assertEqual(len(timeline['items']), 2)
        replay = self.ledger.apply_checked_links('p', 'owner', [decision])
        self.assertTrue(replay['idempotent_replay'])
        scoped = self.ledger.apply_checked_links('p', 'owner', [decision], scope={'message_keys': [b['message_key']]})
        self.assertFalse(scoped['applied'])

    def test_same_instance_version_conflict_and_manual_split_history(self):
        a = self.ingest(self.finding(self.add('Contract C-42 first version.'), version='v1'))
        b = self.ingest(self.finding(self.add('Contract C-42 second version.'), version='v2'))
        decision = {'source_act_id': b['act_id'], 'target_issue_id': a['issue_id'], 'decision': 'same_instance',
                    'review_status': 'supported', 'method': 'semantic_support_checked', 'reason': 'synthetic',
                    'evidence_ids': a['evidence_ids']+b['evidence_ids']}
        self.assertFalse(self.ledger.apply_checked_links('p', 'owner', [decision])['applied'])
        split = self.ledger.apply_checked_links('p', 'owner', [{**decision, 'decision': 'different_issue', 'method': 'human', 'reason': 'Confirmed separate instances'}])
        new_issue = split['applied'][0]['issue_id']
        self.assertNotEqual(a['issue_id'], new_issue)
        self.assertEqual(len(self.ledger.issue_timeline('p', 'owner', a['issue_id'])['items']), 1)
        with self.repo._connection() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM mail_issue_memberships WHERE act_id=?', (b['act_id'],)).fetchone()[0], 2)

    def test_generation_failure_is_atomic_and_retryable(self):
        finding = self.finding(self.add('Contract C-42 proposal.'))
        with patch.object(self.ledger, '_evidence', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
                self.ledger.ingest_checked_findings('p', 'owner', [finding])
        with self.repo._connection() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM mail_semantic_generations').fetchone()[0], 0)
        self.assertEqual(len(self.ledger.ingest_checked_findings('p', 'owner', [finding])['acts']), 1)

    def test_slice_generation_replacement_and_disposable_state_snapshot(self):
        citation = self.add('Contract C-42 proposal.')
        a = self.ingest(self.finding(citation), generation_key='slice-1')
        b = self.ingest(self.finding(citation, act_type='inform'), generation_key='slice-1')
        self.assertNotEqual(a['act_id'], b['act_id'])
        timeline = self.ledger.issue_timeline('p', 'owner', b['issue_id'])
        self.assertEqual(len(timeline['items']), 1)
        snap = self.ledger.recompute_issue_states('p', 'owner', b['issue_id'])
        self.assertEqual(snap['states'][0]['status'], 'informed')
        scope = issue_message_scope(self.repo, 'p', 'owner', b['issue_id'])
        self.assertEqual(scope['message_keys'], [citation['message_key']])

    def test_conditional_withdrawal_retains_effective_commitment(self):
        first=self.ingest(self.finding(self.add('Contract C-42 v1 commitment.'),act_type='commit'))
        self.ingest(self.finding(self.add('Contract C-42 withdraw v1 if approval arrives.'),act_type='withdraw',conditions=['if approval arrives']))
        state=self.ledger.issue_timeline('p','owner',first['issue_id'])['states'][0]
        self.assertEqual(state['status'],'committed')
        self.assertIn('later_mutation_target_or_conditions_unverified',state['gaps'])
        self.assertEqual(len(state['history_act_ids']),2)

    def test_negative_opinion_is_not_negated_rejection_or_withdrawal(self):
        for kind,text in [('reject','Contract C-42 v1. We reject it.'),
                          ('withdraw','We withdraw Contract C-42 v1. No payment was approved.'),
                          ('reject','我们拒绝合同 C-42 v1。'),('withdraw','我们撤回合同 C-42 v1，不安排付款。'),
                          ('reject','เราปฏิเสธ Contract C-42 v1.'),('withdraw','เรายกเลิก Contract C-42 v1.')]:
            act=self.ingest(self.finding(self.add(text),act_type=kind,polarity='negative'))
            self.assertEqual(act['model_polarity'],'negative')
            self.assertEqual(act['polarity'],'positive')
            self.assertEqual(self.ledger.issue_timeline('p','owner',act['issue_id'])['states'][0]['expressed_status'],
                             'rejected' if kind=='reject' else 'withdrawn')

    def test_explicitly_negated_rejection_and_withdrawal_remain_unresolved(self):
        for kind,text in [('reject','We do not reject Contract C-42 v1.'),
                          ('withdraw','We will not withdraw Contract C-42 v1.'),
                          ('reject',"We don't reject Contract C-42 v1."),('withdraw',"We won't withdraw Contract C-42 v1."),
                          ('reject','我们不会拒绝合同 C-42 v1。'),('withdraw','我们不撤回合同 C-42 v1。'),
                          ('reject','เราไม่ปฏิเสธ Contract C-42 v1.'),('withdraw','เราไม่ยกเลิก Contract C-42 v1.')]:
            act=self.ingest(self.finding(self.add(text),act_type=kind,polarity='positive'))
            self.assertEqual(act['polarity'],'negative')
            self.assertEqual(self.ledger.issue_timeline('p','owner',act['issue_id'])['states'][0]['expressed_status'],'unresolved')

    def test_state_recompute_repairs_legacy_negative_opinion_payload(self):
        act=self.ingest(self.finding(self.add('We withdraw Contract C-42 v1. No payment was approved.'),act_type='withdraw'))
        with self.repo._connection() as c:
            row=c.execute('SELECT payload_json FROM mail_communication_acts WHERE act_id=?',(act['act_id'],)).fetchone()
            legacy=json.loads(row[0]);legacy['polarity']='negative';legacy.pop('model_polarity',None);legacy.pop('polarity_resolution',None)
            c.execute('UPDATE mail_communication_acts SET payload_json=? WHERE act_id=?',(canonical(legacy),act['act_id']))
        self.assertEqual(self.ledger.recompute_issue_states('p','owner',act['issue_id'])['states'][0]['status'],'withdrawn')

    def test_observed_reason_does_not_postpone_asserted_withdrawal(self):
        finding=self.finding(self.add('We withdraw Contract C-42 v2. Legal approval was not obtained. No payment was approved.'),
             act_type='withdraw',version='v2',polarity='negative',conditions=['Legal approval was not obtained'],review_status='contradicted')
        act=self.ingest(finding)
        self.assertEqual(act['conditions'],[])
        self.assertEqual(act['model_conditions'][0]['expression'],'Legal approval was not obtained')
        self.assertEqual(act['qualifications'][0]['expression'],'Legal approval was not obtained')
        state=self.ledger.issue_timeline('p','owner',act['issue_id'])['states'][0]
        self.assertEqual(state['status'],'withdrawn')
        self.assertEqual(state['context_review_status'],'contradicted')

    def test_context_review_does_not_mix_version_business_states(self):
        a=self.ingest(self.finding(self.add('Contract C-42 v1 rejected.'),act_type='reject',review_status='contradicted'))
        self.ingest(self.finding(self.add('Contract C-42 v2 accepted after approval.'),act_type='accept',version='v2',
                    conditions=['approval'],review_status='unresolved'))
        states=self.ledger.issue_timeline('p','owner',a['issue_id'])['states']
        self.assertEqual({s['object_version']:s['status'] for s in states},{'v1':'rejected','v2':'conditional_accept'})
        self.assertTrue(all('context_review_unresolved_or_counterevidence' in s['gaps'] for s in states))

    def test_withdrawal_cannot_target_another_actor_or_unspecified_version(self):
        first=self.ingest(self.finding(self.add('Contract C-42 v1 commitment.'),act_type='commit'))
        self.ingest(self.finding(self.add('Contract C-42 withdraw v1.'),act_type='withdraw',target_act_id='another-actor-act'))
        self.assertEqual(self.ledger.issue_timeline('p','owner',first['issue_id'])['states'][0]['status'],'committed')
        unspecified=self.ingest(self.finding(self.add('Contract C-42 withdraw unknown version.'),act_type='withdraw',version=''))
        states=self.ledger.issue_timeline('p','owner',unspecified['issue_id'])['states']
        self.assertEqual(next(s for s in states if s['object_version']=='unspecified')['status'],'pending_verification')

    def test_current_authorship_and_source_versions_invalidate_published_acts(self):
        citation=self.add('Contract C-42 accepted.')
        act=self.ingest(self.finding(citation,act_type='accept'))
        before=ledger_dependency(self.repo,'p','owner')
        with self.repo._connection() as c:c.execute("UPDATE mail_body_fragments SET fragment_kind='quoted' WHERE message_key=?",(citation['message_key'],))
        self.assertFalse(self.ledger.issue_timeline('p','owner',act['issue_id'])['items'])
        self.assertNotEqual(before,ledger_dependency(self.repo,'p','owner'))
        self.assertFalse(search_semantic_ledger(self.repo,'p','owner','Contract')['items'])

    def test_cache_is_bound_to_package_revision_and_exact_mime_source(self):
        citation=self.add('Contract C-42 accepted.')
        description={'mime_path':'1','segment_id':citation['segment_id'],'char_start':0,'char_end':citation['char_end']}
        source_hash=hashlib.sha256(citation['quote'].encode()).hexdigest()
        self.ledger.put_cached_extraction('p','owner',citation['message_key'],source_hash,[self.finding(citation)],slice_scope=description)
        with self.repo._connection() as c:c.execute("UPDATE mail_packages SET source_revision='r2' WHERE package_id='p'")
        self.assertIsNone(self.ledger.get_cached_extraction('p','owner',citation['message_key'],source_hash,slice_scope=description))

    def test_rejected_replacement_does_not_hide_valid_generation(self):
        citation=self.add('Contract C-42 proposal.')
        act=self.ingest(self.finding(citation),generation_key='stable-slot')
        invalid=self.finding({**citation,'quote':'invented quote'})
        result=self.ledger.ingest_checked_findings('p','owner',[invalid],generation_key='stable-slot')
        self.assertEqual(result['completion'],'not_published')
        self.assertEqual(len(self.ledger.issue_timeline('p','owner',act['issue_id'])['items']),1)

    def test_republishing_superseded_payload_appends_once_and_retries_idempotently(self):
        citation=self.add('Contract C-42 proposal.')
        finding=self.finding(citation)
        first=self.ledger.ingest_checked_findings('p','owner',[finding],generation_key='stable-slot')
        revised=self.ledger.ingest_checked_findings('p','owner',[self.finding(citation,act_type='inform')],generation_key='stable-slot')
        republished=self.ledger.ingest_checked_findings('p','owner',[finding],generation_key='stable-slot')
        replay=self.ledger.ingest_checked_findings('p','owner',[finding],generation_key='stable-slot')
        self.assertNotEqual(first['generation_id'],republished['generation_id'])
        self.assertNotEqual(revised['generation_id'],republished['generation_id'])
        self.assertEqual(republished['generation_id'],replay['generation_id'])
        self.assertTrue(replay['idempotent_replay'])
        with self.repo._connection() as c:
            generations=[dict(r) for r in c.execute('SELECT generation_id,sequence,status FROM mail_semantic_generations ORDER BY sequence')]
            self.assertEqual(len(generations),3)
            self.assertEqual([g['status'] for g in generations],['superseded','superseded','ready'])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM mail_evidence').fetchone()[0],1)
        self.assertEqual(self.ledger.issue_timeline('p','owner',republished['issues'][0])['states'][0]['status'],'proposed')

    def test_new_source_version_has_fresh_evidence_even_when_quote_text_is_identical(self):
        citation=self.add('Contract C-42 proposal.')
        first=self.ingest(self.finding(citation),generation_key='stable-slot')
        before=ledger_dependency(self.repo,'p','owner')
        with self.repo._connection() as c:
            c.execute("UPDATE mail_packages SET source_revision='r2',index_epoch=2 WHERE package_id='p'")
            c.execute("UPDATE mail_text_segments SET source_revision='r2',index_epoch=2 WHERE message_key=?",(citation['message_key'],))
        second=self.ingest(self.finding(citation),generation_key='stable-slot')
        self.assertNotEqual(first['evidence_ids'],second['evidence_ids'])
        self.assertNotEqual(before,ledger_dependency(self.repo,'p','owner'))
        with self.repo._connection() as c:
            row=c.execute('SELECT source_revision,index_epoch FROM mail_evidence WHERE evidence_id=?',(second['evidence_ids'][0],)).fetchone()
            self.assertEqual(tuple(row),('r2',2))

    def test_as_of_and_attachment_scope_are_enforced_on_ingest_and_recall(self):
        citation=self.add('Contract C-42 accepted.',date='2026-09-10T08:00:00+00:00')
        out=self.ledger.ingest_checked_findings('p','owner',[self.finding(citation)],scope={'as_of':'2026-09-01T00:00:00+00:00'})
        self.assertFalse(out['acts'])
        self.ingest(self.finding(citation))
        self.assertFalse(search_semantic_ledger(self.repo,'p','owner','Contract',scope={'as_of':'2026-09-01T00:00:00+00:00'})['items'])

    def test_workflow_publication_is_checkpoint_atomic_with_explicit_fallback(self):
        citation=self.add('Contract C-42 proposal.')
        spec={'package_id':'p','mode':'pair_dossier','parties':['a@example.com','b@example.com'],'message_keys':[citation['message_key']]}
        with self.repo._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            with patch.object(IssueRepository,'_evidence',side_effect=sqlite3.OperationalError('synthetic unavailable')):
                result=publish_workflow(self.repo,c,spec,'owner',[self.finding(citation)])
            self.assertEqual(result['status'],'unavailable')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM mail_semantic_generations').fetchone()[0],0)
        with self.repo._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            rows=[self.finding(citation)]
            result=publish_workflow(self.repo,c,spec,'owner',rows)
            self.assertEqual(result['status'],'ready')
            self.assertTrue(rows[0]['act_id'] and rows[0]['issue_id'])

    def test_unanchored_workflow_alignment_has_two_sided_evidence_and_scope(self):
        a=self.ingest(self.finding(self.add('First contract version v1 proposal.'),anchor=False))
        b=self.ingest(self.finding(self.add('I accept first contract version v1.'),act_type='accept',anchor=False))
        finding={'act_id':b['act_id'],'semantic_status':'supported'}
        spec={'package_id':'p','mode':'pair_dossier','parties':['a@example.com','b@example.com'],'message_keys':[a['message_key'],b['message_key']]}
        seen=[]
        def judge(system,payload):
            seen.append(payload)
            return {'decisions':[{'source_act_id':b['act_id'],'target_issue_id':a['issue_id'],'decision':'same_issue',
                'review_status':'supported','method':'semantic_support_checked','reason':'Both quotes identify the same contract v1.',
                'evidence_ids':a['evidence_ids']+b['evidence_ids']}]}
        result=align_workflow_findings(self.repo,spec,'owner',[finding],judge)
        self.assertEqual(len(result['applied']),1)
        self.assertEqual(finding['issue_id'],a['issue_id'])
        self.assertTrue(seen[0]['alignments'][0]['source']['citations'])


if __name__ == '__main__':
    unittest.main()
