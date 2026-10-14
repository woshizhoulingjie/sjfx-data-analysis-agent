"""Small-context contracts keep complete evidence and distinguish impossible overhead."""
from contextlib import nullcontext
from unittest.mock import patch

import pytest

from config import Config
from services.mail.person_activity import FACT_SYSTEM, _saved_findings
from services.mail.person_activity_parallel import (
    ReadingContextCapacityError, check_context, fit_output_budget)
from services.mail.person_research import run_person_slice
from test_person_context_upgrade import person_data
from test_person_activity_v3 import Model, make_turn


def test_tiny_real_reading_contract_cannot_fit_4k():
    with pytest.raises(ReadingContextCapacityError, match='缩小原文批次无法解决'):
        fit_output_budget(FACT_SYSTEM, '{}', 5000, 4096)


def test_small_usable_contract_caps_output_without_truncating_input():
    prompt = '{"scan_manifest":["S1"],"citation_spans":[{"quote":"Complete original."}]}'
    output = fit_output_budget('Read every supplied source.', prompt, 5000, 4096)
    assert 1024 <= output < 5000
    assert check_context('Read every supplied source.', prompt, output, 4096) + output == 4096


def test_32k_keeps_existing_output_floor():
    # Input fits an 8K reservation, and spare space remains available to output.
    prompt = 'source ' * 3000
    assert fit_output_budget('Read all sources.', prompt, 16384, 32768) >= 8000


def test_4k_failure_preserves_cursor_coverage_and_spent_calls(person_data, monkeypatch):
    repo, pid, add = person_data
    add('I request approval for the agreement.')
    tid = make_turn(repo, pid)
    model = Model()
    monkeypatch.setattr(Config, 'LLM_CONTEXT_TOKENS', 4096)
    with patch('services.model_dispatch.background_generation', lambda *a: nullcontext()):
        with pytest.raises(ReadingContextCapacityError):
            run_person_slice(repo, tid, 'owner', model, char_budget=500)
    turn = repo.get_turn(tid, 'owner')
    final = turn['final']
    assert turn['status'] == 'failed'
    assert turn['error']['code'] == 'person_context_capacity_insufficient'
    assert final['activity']['state'] == 'context_capacity_insufficient'
    assert final['cursor'] == [0, 0] and final['characters_read'] == 0
    assert final['model_calls'] == 0 and not model.payloads
    assert not final.get('adaptive_batch') and not _saved_findings(repo, tid)


def test_small_context_never_accepts_length_as_complete(person_data, monkeypatch):
    repo, pid, add = person_data
    add('I request approval for the agreement.')
    tid = make_turn(repo, pid)
    monkeypatch.setattr(Config, 'LLM_CONTEXT_TOKENS', 4096)
    # A short test contract fits; production's full fixed instructions do not.
    monkeypatch.setattr('services.mail.person_activity.FACT_SYSTEM', 'Read every source completely.')

    class Truncated(Model):
        def chat_json(self, system, prompt, **kwargs):
            self.payloads.append(prompt)
            assert kwargs['max_tokens'] < 5000
            return {'json': {'findings': []}, 'finish_reason': 'length'}

    model = Truncated()
    with patch('services.model_dispatch.background_generation', lambda *a: nullcontext()):
        with pytest.raises(ValueError, match='最小批次'):
            run_person_slice(repo, tid, 'owner', model, char_budget=500)
    final = repo.get_turn(tid, 'owner')['final']
    assert final['model_calls'] == len(model.payloads) == 1
    assert final['characters_read'] == 0 and final['cursor'] == [0, 0]
    assert not final['complete'] and not _saved_findings(repo, tid)


def test_capacity_failure_keeps_previously_saved_evidence(person_data, monkeypatch):
    repo, pid, add = person_data
    add('I request approval for the agreement. ' * 30)
    tid = make_turn(repo, pid)
    model = Model()
    with patch('services.model_dispatch.background_generation', lambda *a: nullcontext()):
        run_person_slice(repo, tid, 'owner', model, char_budget=500)
        before = repo.get_turn(tid, 'owner')['final']
        evidence = _saved_findings(repo, tid)
        assert evidence and before['characters_read'] > 0
        monkeypatch.setattr(Config, 'LLM_CONTEXT_TOKENS', 4096)
        with pytest.raises(ReadingContextCapacityError):
            run_person_slice(repo, tid, 'owner', model, char_budget=500)
    after = repo.get_turn(tid, 'owner')['final']
    for key in ('cursor', 'characters_read', 'model_calls', 'batches', 'token_usage'):
        assert after[key] == before[key]
    assert _saved_findings(repo, tid) == evidence
