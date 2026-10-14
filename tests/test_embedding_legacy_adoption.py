"""Explicit legacy adoption preserves bytes while retaining revision/dimension gates."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from services.embedding_backend import (
    OpenAIEmbeddingClient, client_identity, create_embedding_client,
)
from services.model_contract import LocalModelError


class LegacyAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.path = self.root / 'embedding-legacy-adoption.json'
        self.config = SimpleNamespace(DATA_DIR=self.root, BASE_DIR=self.root,
            EMBED_BACKEND='openai_compatible', EMBED_BASE_URL='http://127.0.0.1:8002/v1',
            EMBED_MODEL='embed', EMBED_MODEL_PATH='', EMBED_DIMENSION=0,
            MAIL_ENABLE_VECTOR_INDEX=True, MAIL_EMBED_MAX_CHARS=1180)
        self.revision = 'verified-weights-1'
        self.output_dimension = 2
        self.posts = []
        owner = self

        def request(client, path, payload=None, **kwargs):
            if path == '/models':
                return {'data': [{'id': client.model, 'revision': owner.revision}]}
            if path == '/api/tags':
                return {'models': [{'name': client.model + ':latest', 'digest': owner.revision}]}
            owner.posts.append((path, payload))
            vectors = [[1.0] * owner.output_dimension for _ in payload['input']]
            if path == '/api/embed':
                return {'model': client.model, 'embeddings': vectors}
            return {'model': client.model, 'data': [
                {'index': index, 'embedding': vector} for index, vector in enumerate(vectors)]}

        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'SJFX_EMBED_LEGACY_ADOPTION_FILE': '',
            'SJFX_LP_EMBED_BASE_URL': '', 'SJFX_LP_EMBED_MAX_BATCH_SIZE': ''}).start()
        patch.object(OpenAIEmbeddingClient, '_request', request).start()

    def client(self, lane='ordinary'):
        return create_embedding_client(self.config, lane=lane)

    def record(self, lane='ordinary', client=None):
        client = client or self.client(lane)
        return {'identity': client.identity, 'legacy_namespace': client.model, 'dimension': 2}

    def document(self, lanes=None):
        return {'schema': 'sjfx.embedding-legacy-adoption', 'version': 1,
            'lanes': lanes or {'ordinary': self.record()}}

    def write(self, document):
        self.path.write_text(json.dumps(document), encoding='utf-8')

    def test_missing_file_never_adopts_or_creates_configuration(self):
        client = self.client()
        self.assertEqual(client_identity(client), client.identity)
        self.assertIn(':sjfx-embedding-v2:', client_identity(client))
        self.assertFalse(self.path.exists())

    def test_exact_identity_adopts_without_replacing_transport_identity(self):
        record = self.record()
        self.write(self.document({'ordinary': record}))
        before = self.path.read_bytes()
        client = self.client()
        self.assertEqual(client_identity(client), 'embed')
        self.assertEqual(client.identity, record['identity'])
        self.assertEqual(client.dimension, 2)
        self.assertEqual(client.embed(['verified passage']), [[1., 1.]])
        self.assertEqual(client.identity, record['identity'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_lanes_cannot_reuse_another_encoding_policy_identity(self):
        self.write(self.document({'ordinary': self.record(), 'large': self.record()}))
        self.assertEqual(client_identity(self.client()), 'embed')
        large = self.client('large')
        self.assertEqual(client_identity(large), large.identity)
        mail = self.client('mail')
        self.assertEqual(client_identity(mail), mail.identity)

    def test_configured_dimension_conflict_disables_adoption(self):
        self.config.EMBED_DIMENSION = 3
        self.write(self.document({'ordinary': self.record()}))
        client = self.client()
        self.assertEqual(client_identity(client), client.identity)
        self.assertEqual(client.dimension, 3)

    def test_new_weights_and_endpoint_do_not_inherit_attestation(self):
        self.write(self.document())
        self.revision = 'new-weights'
        client = self.client()
        self.assertEqual(client_identity(client), client.identity)
        self.revision = 'verified-weights-1'
        self.config.EMBED_BASE_URL = 'http://127.0.0.1:8003/v1'
        client = self.client()
        self.assertEqual(client_identity(client), client.identity)

    def test_bad_and_ambiguous_documents_keep_v2_namespace(self):
        good = self.document()
        malformed = []
        for key, value in [('schema', 'other'), ('version', True), ('version', 2),
                           ('extra', 'not allowed')]:
            document = copy.deepcopy(good)
            document[key] = value
            malformed.append(document)
        for key, value in [('dimension', True), ('dimension', 0), ('dimension', -1),
                           ('dimension', 65537), ('dimension', 2.0), ('dimension', '2'),
                           ('identity', 'embed'), ('legacy_namespace', 'other'),
                           ('extra', 'not allowed')]:
            document = copy.deepcopy(good)
            document['lanes']['ordinary'][key] = value
            malformed.append(document)
        document = copy.deepcopy(good)
        document['lanes']['unregistered-lane'] = self.record()
        malformed.append(document)
        for document in malformed:
            with self.subTest(document=document):
                self.write(document)
                client = self.client()
                self.assertEqual(client_identity(client), client.identity)
        for raw in ['not JSON', '{"schema":"one","schema":"two"}', ' ' * 65537,
                    '{"schema":"sjfx.embedding-legacy-adoption","version":1,"lanes":{"ordinary":{}}}',
                    json.dumps(good).replace('"version": 1', '"version": 1, "version": 1'),
                    json.dumps(good).replace('"dimension": 2', '"dimension": 2, "dimension": 2')]:
            with self.subTest(raw=raw[:80]):
                self.path.write_text(raw, encoding='utf-8')
                client = self.client()
                self.assertEqual(client_identity(client), client.identity)

    def test_client_keeps_process_start_snapshot_and_fresh_client_rechecks_file(self):
        self.write(self.document())
        client = self.client()
        self.path.unlink()
        self.assertEqual(client_identity(client), 'embed')
        fresh = self.client()
        self.assertEqual(client_identity(fresh), fresh.identity)

    def test_explicit_relative_path_uses_application_root(self):
        target = self.root / 'approved' / 'registration.json'
        target.parent.mkdir()
        target.write_text(json.dumps(self.document()), encoding='utf-8')
        self.write({'bad': 'default file must not be selected'})
        with patch.dict(os.environ, {'SJFX_EMBED_LEGACY_ADOPTION_FILE': 'approved/registration.json'}):
            self.assertEqual(client_identity(self.client()), 'embed')
        with patch.dict(os.environ, {'SJFX_EMBED_LEGACY_ADOPTION_FILE': str(self.root / 'missing.json')}):
            client = self.client()
            self.assertEqual(client_identity(client), client.identity)

    def test_request_options_keep_binding_and_original_identity(self):
        self.write(self.document())
        original = self.client()
        true_identity = original.identity
        clone = original.with_options(max_chars=128, max_batch_size=1, timeout=4)
        self.assertEqual(client_identity(clone), 'embed')
        self.assertEqual(clone.identity, true_identity)
        self.assertEqual(clone.dimension, 2)
        self.assertEqual(clone.embed(['passage']), [[1., 1.]])

    def test_direct_embed_validates_attested_output_dimension(self):
        self.write(self.document())
        client = self.client()
        self.output_dimension = 3
        with self.assertRaises(LocalModelError) as raised:
            client.embed(['passage'])
        self.assertEqual(raised.exception.code, 'embedding_incompatible')
        self.assertEqual(client.dimension, 2)

    def test_running_adopted_client_still_rejects_revision_change_before_post(self):
        self.write(self.document())
        client = self.client()
        self.assertEqual(client_identity(client), 'embed')
        self.revision = 'replaced-weights'
        with self.assertRaises(LocalModelError) as raised:
            client.embed(['passage'])
        self.assertEqual(raised.exception.code, 'embedding_incompatible')
        self.assertEqual(self.posts, [])

    def test_ollama_adoption_keeps_native_digest_guard(self):
        self.config.EMBED_BACKEND = 'ollama'
        self.write(self.document())
        client = self.client()
        true_identity = client.identity
        self.assertEqual(client_identity(client), 'embed')
        self.assertEqual(client.embed(['passage']), [[1., 1.]])
        self.assertEqual(client.identity, true_identity)
        self.assertEqual(self.posts[0][0], '/api/embed')
        self.revision = 'replaced-weights'
        with self.assertRaises(LocalModelError):
            client.embed(['passage'])

    def test_existing_ordinary_vectors_are_read_without_reencoding(self):
        from services import ordinary_recall
        self.write(self.document())
        client = self.client()
        text = ordinary_recall._file_text('a.txt', '', {'text': 'Original evidence'})
        import hashlib
        key = 'ordinary-file::p::a.txt::' + hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:16]
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('CREATE TABLE embedding_cache(cache_key TEXT,model TEXT,vector TEXT,dimensions INTEGER)')
        conn.execute('INSERT INTO embedding_cache VALUES(?,?,?,?)', (key, 'embed', '[1,0]', 2))
        conn.commit()
        storage = SimpleNamespace(_connect=lambda: conn,
            get_document=lambda *_: {'text': 'Original evidence'},
            get_embeddings=lambda keys, model: {r[0]: json.loads(r[1]) for r in
                conn.execute('SELECT cache_key,vector FROM embedding_cache WHERE model=?', (model,))})
        self.assertEqual(ordinary_recall.file_vectors(storage, 'p', ['a.txt'], client=client), {'a.txt': [1., 0.]})
        self.assertEqual(self.posts, [])
        self.assertEqual(conn.execute('SELECT model,vector FROM embedding_cache').fetchone(), ('embed', '[1,0]'))

    def test_large_index_retains_vectors_and_rejects_mixed_namespaces(self):
        from services import large_package_semantic
        self.write(self.document({'large': self.record('large')}))
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        large_package_semantic.init(conn)
        vector = b'\x00\x00\x80?\x00\x00\x00\x00'
        conn.execute('INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,updated_at) VALUES(?,?,?,?,?,0)',
            ('p', 'g', 'embed', vector, 'a.txt'))
        conn.execute('INSERT INTO large_catalog_members VALUES(?,?,?,?,?)', ('p', 'a.txt', 'g', 'hash', vector))
        self.assertEqual(large_package_semantic._require_space(conn, 'p', self.client('large'), 2), 'embed')
        self.assertEqual(conn.execute('SELECT vector FROM large_catalog_members').fetchone()[0], vector)
        conn.execute('UPDATE large_catalog_groups SET model=?', ('other',))
        with self.assertRaises(LocalModelError):
            large_package_semantic._require_space(conn, 'p', self.client('large'), 2)

    def test_mail_appends_original_window_namespace_and_reports_ready(self):
        from services.mail import semantic
        self.write(self.document({'mail': self.record('mail')}))
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.executescript('CREATE TABLE mail_text_segments(package_id TEXT,segment_id TEXT,text_sha256 TEXT);'
            'CREATE TABLE mail_embeddings(package_id TEXT,segment_id TEXT,model TEXT,dim INTEGER,vector BLOB,text_sha256 TEXT);')
        namespace = 'embed:mail-e5-window-v1:1180'
        vector = b'\x00\x00\x80?\x00\x00\x00\x00'
        conn.execute('INSERT INTO mail_text_segments VALUES(?,?,?)', ('p', 's', 'hash'))
        conn.execute('INSERT INTO mail_embeddings VALUES(?,?,?,?,?,?)', ('p', 's', namespace, 2, vector, 'hash'))
        conn.commit()
        repo = SimpleNamespace(get_package=lambda *_: {'index_epoch': 1}, _connection=lambda: conn)
        with patch.object(semantic.Config, 'MAIL_EMBED_MAX_CHARS', 1180):
            key = semantic.model_key(self.client('mail'))
            self.assertEqual(key, namespace)
            self.assertEqual(semantic.index_status(repo, 'p', 'owner', key)['status'], 'ready')
            semantic._require_space(conn, 'p', key, 2)
        self.assertEqual(conn.execute('SELECT model,vector FROM mail_embeddings').fetchone(), (namespace, vector))
        self.assertEqual(self.posts, [])

    def fallback(self, *, attested=True, secondary_dimension=2, secondary_weights='verified-artifact'):
        from services.large_package_semantic import FallbackEmbeddingClient, _EmbeddingConfigView
        self.config.EMBED_WEIGHTS_ID = 'verified-artifact'
        self.config.VLLM_EMBED_RETRIES = 0
        backup_path = self.root / 'embedding-fallback-adoption.json'
        view = _EmbeddingConfigView(self.config, EMBED_BASE_URL='http://127.0.0.1:8004/v1',
            SJFX_LP_EMBED_BASE_URL='http://127.0.0.1:8004/v1',
            SJFX_EMBED_LEGACY_ADOPTION_FILE=str(backup_path), EMBED_WEIGHTS_ID=secondary_weights)
        if attested:
            self.write(self.document({'large': self.record('large')}))
            record = self.record('large', create_embedding_client(view, lane='large'))
            record['dimension'] = secondary_dimension
            backup_path.write_text(json.dumps(self.document({'large': record})), encoding='utf-8')
        return FallbackEmbeddingClient(self.client('large'), create_embedding_client(view, lane='large'))

    def test_large_endpoint_and_batch_override_leave_ordinary_and_mail_unchanged(self):
        from services import large_package_semantic
        with patch.dict(os.environ, {'SJFX_LP_EMBED_BASE_URL': 'http://127.0.0.1:8005/v1',
                                     'SJFX_LP_EMBED_MAX_BATCH_SIZE': '16'}):
            large = self.client('large')
            self.assertEqual(large.base_url, 'http://127.0.0.1:8005/v1')
            self.assertEqual(large.max_batch_size, 16)
            self.assertEqual(large_package_semantic.embed_batch_size(), 16)
            for lane in ('ordinary', 'mail'):
                client = self.client(lane)
                self.assertEqual(client.base_url, self.config.EMBED_BASE_URL)
                self.assertEqual(client.max_batch_size, 8)
            self.assertEqual(self.config.EMBED_BASE_URL, 'http://127.0.0.1:8002/v1')

    def test_semantic_factory_keeps_separate_attestations_with_explicit_main_file(self):
        from services import large_package_semantic
        wrapper = self.fallback()
        self.config.LP_EMBED_FALLBACK_URL = wrapper.secondary.base_url
        self.config.SJFX_LP_EMBED_BASE_URL = wrapper.primary.base_url
        with patch('config.Config', self.config), patch.dict(os.environ,
                {'SJFX_EMBED_LEGACY_ADOPTION_FILE': str(self.path)}):
            actual = large_package_semantic.embedding_client()
        self.assertEqual(actual.primary._legacy_adoption[0], wrapper.primary._legacy_adoption[0])
        self.assertEqual(actual.secondary._legacy_adoption[0], wrapper.secondary._legacy_adoption[0])
        self.assertEqual(actual.secondary.base_url, 'http://127.0.0.1:8004/v1')
        self.assertNotEqual(actual.primary.identity, actual.secondary.identity)

    def test_attested_transient_fallback_keeps_primary_true_identity(self):
        client = self.fallback()
        identity = client.identity
        self.assertEqual(client_identity(client), 'embed')
        with patch.object(client.primary, 'embed', side_effect=LocalModelError(
                'GPU temporarily unavailable', code='unavailable', retryable=True)):
            self.assertEqual(client.embed(['passage']), [[1., 1.]])
        self.assertEqual(client.fallbacks, 1)
        self.assertEqual(client.identity, identity)
        self.assertEqual(client_identity(client), 'embed')
        self.assertEqual(len(self.posts), 1)

    def test_unattested_backup_never_mixes_distinct_v2_spaces(self):
        client = self.fallback(attested=False)
        with patch.object(client.primary, 'embed', side_effect=LocalModelError(
                'GPU temporarily unavailable', code='unavailable', retryable=True)):
            with self.assertRaises(LocalModelError) as raised:
                client.embed(['passage'])
        self.assertEqual(raised.exception.code, 'embedding_incompatible')
        self.assertEqual(self.posts, [])
        self.assertEqual(client.fallbacks, 0)

    def test_primary_identity_and_protocol_failures_cannot_trigger_backup(self):
        client = self.fallback()
        for code in ('embedding_incompatible', 'authentication_error', 'invalid_embedding',
                     'invalid_response', 'configuration_error', 'model_unavailable'):
            with self.subTest(code=code), patch.object(client.primary, 'embed',
                    side_effect=LocalModelError('fail closed', code=code, retryable=True)):
                with self.assertRaises(LocalModelError) as raised:
                    client.embed(['passage'])
                self.assertEqual(raised.exception.code, code)
        self.assertEqual(self.posts, [])
        self.assertEqual(client.fallbacks, 0)

    def test_attested_backup_must_match_dimension_weights_and_encoding(self):
        for kwargs in ({'secondary_dimension': 3}, {'secondary_weights': 'other-artifact'}, {}):
            with self.subTest(kwargs=kwargs):
                client = self.fallback(**kwargs)
                self.assertEqual(client_identity(client), 'embed')
                if not kwargs:
                    client.secondary.encoding_policy = 'other-policy'
                with patch.object(client.primary, 'embed', side_effect=LocalModelError(
                        'GPU temporarily unavailable', code='unavailable', retryable=True)):
                    with self.assertRaises(LocalModelError) as raised:
                        client.embed(['passage'])
                self.assertEqual(raised.exception.code, 'embedding_incompatible')
        self.assertEqual(self.posts, [])

    def test_real_revision_change_is_not_hidden_by_fallback(self):
        client = self.fallback()
        self.assertEqual(client_identity(client), 'embed')
        self.revision = 'changed-after-attestation'
        with self.assertRaises(LocalModelError) as raised:
            client.embed(['passage'])
        self.assertEqual(raised.exception.code, 'embedding_incompatible')
        self.assertEqual(self.posts, [])
        self.assertEqual(client.fallbacks, 0)

    def test_backup_output_dimension_is_still_checked(self):
        client = self.fallback()
        self.assertEqual(client_identity(client), 'embed')
        self.output_dimension = 3
        with patch.object(client.primary, 'embed', side_effect=LocalModelError(
                'GPU temporarily unavailable', code='unavailable', retryable=True)):
            with self.assertRaises(LocalModelError) as raised:
                client.embed(['passage'])
        self.assertEqual(raised.exception.code, 'embedding_incompatible')
        self.assertEqual(len(self.posts), 1)

    def test_fallback_request_clone_retains_identity_binding_and_both_budgets(self):
        original = self.fallback()
        identity = original.identity
        clone = original.with_options(timeout=4, max_batch_size=1, max_chars=128, retries=0)
        self.assertEqual(clone.identity, identity)
        self.assertEqual(client_identity(clone), 'embed')
        for part in (clone.primary, clone.secondary):
            self.assertEqual((part.timeout, part.max_batch_size, part.max_chars, part.retries), (4, 1, 128, 0))
        with patch.object(clone.primary, 'embed', side_effect=LocalModelError(
                'GPU temporarily unavailable', code='busy', retryable=True)):
            self.assertEqual(clone.embed(['passage']), [[1., 1.]])

    def test_health_does_not_hide_primary_configuration_or_auth_failure(self):
        client = self.fallback()
        for code in ('authentication_error', 'model_unavailable', 'configuration_error'):
            with self.subTest(code=code), patch.object(client.primary, 'health_check',
                    return_value={'reachable': False, 'model_available': False, 'code': code}), \
                    patch.object(client.secondary, 'health_check', side_effect=AssertionError('backup must not mask failure')):
                self.assertFalse(client.health_check()['fallback_verified'])

    def test_unpinned_health_never_bootstraps_slow_identity_requests(self):
        client = self.fallback()
        with patch.object(client.primary, 'health_check',
                return_value={'reachable': False, 'model_available': False, 'code': 'unavailable'}), \
                patch.object(client.primary, '_model_info', side_effect=AssertionError('no unbounded identity lookup')):
            self.assertFalse(client.health_check(timeout=1)['fallback_verified'])


if __name__ == '__main__':
    unittest.main()
