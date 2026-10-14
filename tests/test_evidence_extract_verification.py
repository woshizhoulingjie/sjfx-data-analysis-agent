"""Extractive fallbacks have explicit evidence without claiming model input."""
from copy import deepcopy
import re
from unittest.mock import Mock

import pytest

from config import Config
from services import answer_quality as aq
from services.claim_verifier import ClaimVerifier
from services.conversation import CallableEvidenceRetriever, ConversationEngine, _citation


SOURCE = '甲项目因设备故障延期。延期影响是交付顺延十天。'
FIRST, SECOND = '甲项目因设备故障延期。', '延期影响是交付顺延十天。'


def extract_result(**citation_overrides):
    citation = _citation({'evidence_id': 'e1', 'source_path': 'allowed/source.txt',
                          'text': SOURCE, 'label': 'paragraph', **citation_overrides}, 1)
    return {
        'answer': '根据当前可回查证据：\n\n' + FIRST + '[1]' + SECOND + '[1]',
        'citations': [citation], 'model_citations': [],
        'input_manifest': {
            'answer_mode': 'evidence_extract', 'model_request_sent': False,
            'actual_citation_indexes': [], 'fallback_citation_indexes': [1],
            'answer_contract': aq.answer_contract('延期原因和影响是什么？', [citation]),
        },
    }


PLAN = {'scope': {'kind': 'package'}, 'modes': ['retrieval']}


def test_single_source_two_sentences_survive_verify_guard_verify(monkeypatch):
    monkeypatch.setattr(Config, 'CONVERSATION_QUALITY_ENABLED', True)
    result = extract_result()
    verifier = ClaimVerifier()
    checked = verifier.verify(result, PLAN)
    assert checked['status'] == 'verified', checked['failed_claims']
    assert checked['ledger']['supported_claim_count'] == 2
    guarded = verifier.guard_result(result, checked)
    final = verifier.verify(guarded, PLAN)
    # The existing guard places citation labels before sentence punctuation.
    # Both exact source sentences must survive that presentation change.
    plain = re.sub(r'\[\d{1,4}\]|\s+', '', guarded['answer'])
    assert FIRST in plain and SECOND in plain
    assert final['status'] == 'verified'
    assert guarded['model_citations'] == []
    assert guarded['input_manifest']['actual_citation_indexes'] == []


@pytest.mark.parametrize('change', [
    {'answer_mode': 'model'}, {'model_request_sent': True},
    {'model_request_sent': None}, {'actual_citation_indexes': [1]},
    {'fallback_citation_indexes': [99]}, {'fallback_citation_indexes': []},
])
def test_fallback_requires_explicit_unused_model_and_matching_indexes(monkeypatch, change):
    monkeypatch.setattr(Config, 'CONVERSATION_QUALITY_ENABLED', True)
    result = extract_result()
    result['input_manifest'].update(change)
    checked = ClaimVerifier().verify(result, PLAN)
    assert checked['status'] != 'verified'
    assert checked['ledger']['unsupported_claim_count'] == 2


def test_ordinary_model_answer_cannot_use_unsent_display_evidence(monkeypatch):
    monkeypatch.setattr(Config, 'CONVERSATION_QUALITY_ENABLED', True)
    result = extract_result()
    result['input_manifest'] = {}
    checked = ClaimVerifier().verify(result, PLAN)
    assert checked['status'] != 'verified'
    assert checked['ledger']['unsupported_claim_count'] == 2


def test_extract_manifest_cannot_replace_nonempty_model_evidence(monkeypatch):
    monkeypatch.setattr(Config, 'CONVERSATION_QUALITY_ENABLED', True)
    result = extract_result()
    result['model_citations'] = [_citation({
        'evidence_id': 'e2', 'source_path': 'other.txt',
        'text': '乙项目因暴雨导致施工中断。', 'label': 'paragraph'}, 2)]
    checked = ClaimVerifier().verify(result, PLAN)
    assert checked['status'] != 'verified'
    assert checked['ledger']['unsupported_claim_count'] == 2


def test_extract_can_only_use_the_declared_display_subset(monkeypatch):
    monkeypatch.setattr(Config, 'CONVERSATION_QUALITY_ENABLED', True)
    result = extract_result()
    result['citations'].append(_citation({
        'evidence_id': 'e2', 'source_path': 'other.txt',
        'text': '乙项目因暴雨导致施工中断。', 'label': 'paragraph'}, 2))
    result['answer'] = '乙项目因暴雨导致施工中断。[2]'
    checked = ClaimVerifier().verify(result, PLAN)
    assert checked['status'] != 'verified'
    assert checked['ledger']['unsupported_claim_count'] == 1


@pytest.mark.parametrize('kind', ['preview', 'out_of_scope', 'ineligible'])
def test_extract_fallback_preserves_existing_source_gates(monkeypatch, kind):
    monkeypatch.setattr(Config, 'CONVERSATION_QUALITY_ENABLED', True)
    extra = {'preview_only': True} if kind == 'preview' else {}
    result = extract_result(**extra)
    if kind == 'ineligible':
        result['citations'][0].update(original_text='甲项目', label='heading')
    plan = deepcopy(PLAN)
    if kind == 'out_of_scope':
        plan['scope'] = {'kind': 'files', 'source_paths': ['other.txt']}
    checked = ClaimVerifier().verify(result, plan)
    assert checked['status'] != 'verified'
    assert checked['ledger']['unsupported_claim_count'] == 2
    guarded = ClaimVerifier().guard_result(result, checked)
    assert FIRST not in guarded['answer'] and SECOND not in guarded['answer']


def test_small_context_engine_extract_is_verified_without_model_call(monkeypatch):
    for key in ('LLM_CONTEXT_TOKENS', 'CONVERSATION_CONTEXT_TOKENS'):
        monkeypatch.setattr(Config, key, 4096)
    monkeypatch.setattr(Config, 'LLM_TOKENIZER_PATH', '')
    monkeypatch.setattr(Config, 'CONVERSATION_QUALITY_ENABLED', True)
    model = Mock()
    retriever = CallableEvidenceRetriever(lambda request: {'results': [
        {'evidence_id': 'e1', 'source_path': 'allowed/source.txt',
         'text': SOURCE, 'label': 'paragraph'},
    ]})
    engine = ConversationEngine(retriever, answer_model=model)
    result = engine.ask(engine.new_session('scan'), '甲项目为什么延期、影响是什么？')
    assert model.chat.call_count == 0
    assert result['input_manifest']['answer_mode'] == 'evidence_extract'
    assert result['input_manifest']['model_request_sent'] is False
    assert result['input_manifest']['actual_citation_indexes'] == []
    assert result['model_citations'] == []
    assert result['input_manifest']['fallback_citation_indexes'] == [1]
    verifier = ClaimVerifier()
    checked = verifier.verify(result, PLAN)
    assert checked['status'] == 'verified', checked['failed_claims']
    guarded = verifier.guard_result(result, checked)
    plain = re.sub(r'\[\d{1,4}\]|\s+', '', guarded['answer'])
    assert FIRST in plain and SECOND in plain
    assert verifier.verify(guarded, PLAN)['status'] == 'verified'
