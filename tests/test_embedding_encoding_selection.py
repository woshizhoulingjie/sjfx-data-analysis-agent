"""Embedding input policies and tokenizers follow the selected vector space."""
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from config import Config
from services import embedding_encoding, large_package_semantic
from services.embedding_backend import require_index_identity
from services.mail import semantic as mail_semantic
from services.model_contract import LocalModelError


def test_plain_mail_and_catalog_inputs_keep_source_without_e5_instruction(monkeypatch):
    monkeypatch.setattr(Config, 'EMBED_ENCODING', 'plain', raising=False)
    monkeypatch.setattr(Config, 'EMBED_TOKENIZER_PATH', '', raising=False)
    source = 'Original purchase amount 120; not yet paid.'
    assert '120' in mail_semantic.passage_text(source)
    assert not mail_semantic.passage_text(source).startswith('passage: ')
    assert not mail_semantic.query_text('purchase amount').startswith('query: ')
    value = {'text': source, 'deep_summary': '', 'fields': [], 'headings': [],
             'evidence_blocks': [{'text': source, 'kind': 'text'}]}
    for policy in (1, 2):
        passage = large_package_semantic.embedding_passage(value, policy)
        assert '120' in passage and 'not yet paid' in passage
        assert not passage.startswith(('query: ', 'passage: '))


def test_legacy_e5_inputs_keep_existing_encoding_and_namespace(monkeypatch):
    monkeypatch.setattr(Config, 'EMBED_ENCODING', 'e5', raising=False)
    monkeypatch.setattr(Config, 'EMBED_TOKENIZER_PATH', '', raising=False)
    assert mail_semantic.passage_text('purchase 120').startswith('passage: ')
    assert mail_semantic.query_text('purchase').startswith('query: ')
    client = SimpleNamespace(identity='existing-e5-space', model='e5')
    assert ':mail-e5-window-v1:' in mail_semantic.model_key(client)


def test_plain_policy_cannot_reuse_existing_e5_mail_index(monkeypatch):
    client = SimpleNamespace(identity='same-endpoint-model', model='same-model')
    monkeypatch.setattr(Config, 'EMBED_ENCODING', 'e5', raising=False)
    before = mail_semantic.model_key(client)
    with sqlite3.connect(':memory:') as connection:
        require_index_identity(connection, 'mail', 'p', before, 384)
        monkeypatch.setattr(Config, 'EMBED_ENCODING', 'plain')
        after = mail_semantic.model_key(client)
        with pytest.raises(LocalModelError) as error:
            require_index_identity(connection, 'mail', 'p', after, 384)
        assert error.value.code == 'embedding_rebuild_required'
        assert connection.execute('SELECT identity FROM sjfx_embedding_identity').fetchone()[0] == before


def _write_tokenizer(path, vocabulary):
    from tokenizers import Tokenizer, models, pre_tokenizers
    tokenizer = Tokenizer(models.WordLevel(vocabulary, unk_token='[UNK]'))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.save(str(path))


def test_vector_tokenizer_never_falls_back_to_generation_weights(tmp_path):
    path = tmp_path / 'generation'
    path.mkdir()
    _write_tokenizer(path / 'tokenizer.json', {'[UNK]': 0, 'purchase': 1})
    config = SimpleNamespace(EMBED_ENCODING='plain', EMBED_TOKENIZER_PATH='',
                             VLLM_MODEL_PATH=str(path), CONVERSATION_TOKENIZER_PATH=str(path))
    assert embedding_encoding.tokenizer(config) is None


def test_explicit_vector_tokenizer_accepts_file_or_directory_and_refreshes(tmp_path):
    path = tmp_path / 'tokenizer.json'
    _write_tokenizer(path, {'[UNK]': 0, 'purchase': 1})
    config = SimpleNamespace(EMBED_ENCODING='plain', EMBED_TOKENIZER_PATH=str(path),
                             EMBED_MODEL='one', EMBED_BASE_URL='http://localhost:1')
    first = embedding_encoding.tokenizer(config)
    assert first.encode('purchase', add_special_tokens=False).ids == [1]
    _write_tokenizer(path, {'[UNK]': 0, 'purchase': 2, 'other': 1})
    refreshed = embedding_encoding.tokenizer(config)
    assert refreshed.encode('purchase', add_special_tokens=False).ids == [2]
    config.EMBED_TOKENIZER_PATH = str(tmp_path)
    assert embedding_encoding.tokenizer(config).encode('purchase').ids == [2]


def test_rebuild_encoding_uses_client_space_even_if_process_default_differs(monkeypatch):
    monkeypatch.setattr(Config, 'EMBED_ENCODING', 'e5', raising=False)
    client = SimpleNamespace(encoding_policy='ordinary-plain-v1')
    assert embedding_encoding.prefix('passage', client=client) == ''
    assert embedding_encoding.prefix('query', client=client) == ''
