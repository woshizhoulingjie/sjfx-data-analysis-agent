"""Measure context once after a provider rejection; never trim source messages."""
import io
import json
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from services.ollama import LocalModelError
from services.vllm import VLLMClient


def response(value):
    return io.BytesIO(json.dumps(value).encode('utf-8'))


def context_error():
    detail = "This model's maximum context length is 32768 tokens. However, you requested 3600 output tokens and your prompt contains at least 29169 input tokens, for a total of at least 32769 tokens."
    return urllib.error.HTTPError('http://127.0.0.1/v1/chat/completions', 400, 'Bad Request', {},
                                  response({'error': {'message': detail}}))


def success():
    return response({'choices':[{'message':{'content':'{"findings":[]}'},'finish_reason':'stop'}],
                     'usage':{'prompt_tokens':29169,'completion_tokens':5,'total_tokens':29174}})


def invoke(responses, retries=0, **constructor_options):
    calls = []
    def urlopen(request, timeout=None, *, allowed_endpoints=()):
        calls.append((request.full_url, json.loads(request.data.decode('utf-8'))))
        value = responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
    client = VLLMClient('http://127.0.0.1/v1', 'test', api_key='test-key', **constructor_options)
    with patch('services.vllm.open_local_request', side_effect=urlopen), \
         patch('services.llm_trace.get_recorder', return_value=None), \
         patch.object(VLLMClient, '_backoff'):
        # Execute real transport while avoiding the unrelated admission database.
        result = VLLMClient.chat.__wrapped__(client, 'source policy', '完整原文，不裁剪',
            max_tokens=3600, retries=retries, response_format={'type':'json_object'})
    return result, calls


@pytest.mark.parametrize('measured,effective', [({'count':29169},3599), ({'count':31000},1768),
                                              ({'tokens':list(range(30000))},2768)])
def test_precise_dynamic_budget_preserves_all_input_and_schema(measured, effective):
    result, calls = invoke([context_error(), response(measured), success()])
    assert [url for url, payload in calls] == ['http://127.0.0.1/v1/chat/completions','http://127.0.0.1/tokenize','http://127.0.0.1/v1/chat/completions']
    original, tokenize, retried = [payload for url, payload in calls]
    assert original['messages'] == tokenize['messages'] == retried['messages']
    assert tokenize['add_generation_prompt'] is True
    assert original['chat_template_kwargs'] == tokenize['chat_template_kwargs'] == retried['chat_template_kwargs']
    assert original['response_format'] == retried['response_format']
    assert original['max_tokens'] == 3600 and retried['max_tokens'] == effective
    assert result['context_budget']['effective_max_tokens'] == effective
    assert result['context_budget']['input_truncated'] is False


def test_normal_request_never_tokenizes_or_changes_completion_budget():
    result, calls = invoke([success()])
    assert len(calls) == 1 and calls[0][1]['max_tokens'] == 3600
    assert 'context_budget' not in result


def test_configured_precise_retry_respects_selected_window_and_safety_reserve():
    result, calls = invoke([context_error(), response({'count': 29169}), success()],
                          budget_config=SimpleNamespace(LLM_CONTEXT_TOKENS=30000))
    assert calls[-1][1]['max_tokens'] == 30000-29169-256
    assert result['context_budget']['context_limit'] == 30000
    assert result['context_budget']['provider_context_limit'] == 32768
    assert result['context_budget']['reserve_tokens'] == 256
    assert result['context_budget']['input_truncated'] is False


def test_configured_precise_retry_never_consumes_reserved_input_capacity():
    with pytest.raises(LocalModelError) as caught:
        invoke([context_error(), response({'count': 29900})], context_tokens=30000)
    assert caught.value.code == 'context_limit'


@pytest.mark.parametrize('token_response', [response({'count':32768}), response({'count':40000})])
def test_input_over_limit_is_explicit_and_never_truncated(token_response):
    with pytest.raises(LocalModelError, match='需要调用方分片'):
        invoke([context_error(), token_response])


@pytest.mark.parametrize('token_response', [urllib.error.URLError('tokenizer offline'), response({'count':True}),
                                          response({'tokens':['invalid']})])
def test_failed_or_invalid_tokenization_keeps_original_context_error(token_response):
    with pytest.raises(LocalModelError, match='输入超出模型上下文限制'):
        invoke([context_error(), token_response])


def test_second_context_rejection_is_not_hidden_or_retried_again():
    with pytest.raises(LocalModelError, match='vLLM HTTP 400'):
        invoke([context_error(), response({'count':29169}), context_error()], retries=2)


def test_unrelated_bad_request_never_uses_context_adaptation():
    error = urllib.error.HTTPError('http://127.0.0.1/v1/chat/completions',400,'Bad Request',{},response({'error':'invalid schema'}))
    with pytest.raises(LocalModelError, match='请求被本机服务拒绝'):
        invoke([error], retries=2)


def test_transient_http_retry_budget_is_preserved():
    error = urllib.error.HTTPError('http://127.0.0.1/v1/chat/completions',503,'Unavailable',{},response({'error':'busy'}))
    result, calls = invoke([error, success()], retries=1)
    assert len(calls) == 2 and all(payload['max_tokens'] == 3600 for url,payload in calls)
