"""Decoder-limit recovery without native model dependencies."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.quickmt_translation import _PairModel
from services.translation import TranslationProviderError


def pair_model(threshold, output_limit=16):
    pair = _PairModel.__new__(_PairModel)
    pair.source = SimpleNamespace(encode=lambda text, out_type: list(text))
    pair.target = SimpleNamespace(decode=lambda tokens: ''.join(tokens))
    calls = []

    def inference(encoded, **options):
        sources = [''.join(tokens[:-1]) for tokens in encoded]
        calls.append(sources)
        assert options['max_input_length'] == 0
        assert options['max_decoding_length'] == output_limit
        return [SimpleNamespace(hypotheses=[
            ['CLIPPED'] * output_limit if len(text) > threshold else [text]
        ]) for text in sources]

    pair.engine = SimpleNamespace(translate_batch=Mock(side_effect=inference))
    return pair, calls


def test_successful_batch_member_is_not_retranslated_or_lost():
    pair, calls = pair_model(260)
    source = ' '.join('word%03d' % number for number in range(60))
    result = pair.translate(['short valid source', source], 5, 8, 16)
    assert result[0] == 'short valid source'
    assert result[1].split() == source.split()
    assert 'CLIPPED' not in result[1]
    assert len(calls) == 3
    assert all('short valid source' not in batch for batch in calls[1:])
    assert calls[1] == [source]
    assert ''.join(calls[2]) == source


def test_recursive_recovery_preserves_all_source_in_order():
    pair, calls = pair_model(128)
    source = '源' * 2048
    result = pair.translate([source], 5, 8, 16)
    assert result[0].replace(' ', '') == source
    assert 'CLIPPED' not in result[0]
    assert len(calls) > 2
    leaves = [text for batch in calls for text in batch if len(text) <= 128]
    assert ''.join(leaves) == source


def test_irrecoverable_small_source_remains_an_explicit_failure():
    pair, calls = pair_model(0)
    with pytest.raises(TranslationProviderError) as caught:
        pair.translate(['x' * 128], 5, 8, 16)
    assert caught.value.code == 'output_truncated'
    assert len(calls) == 2


def test_short_pdf_fragment_retries_with_repetition_penalty_before_failing():
    pair, _calls = pair_model(0)
    def inference(encoded, **options):
        tokens = ['模型给出了，与'] if options['repetition_penalty'] > 1 else ['与'] * 16
        return [SimpleNamespace(hypotheses=[tokens]) for _source in encoded]
    pair.engine.translate_batch.side_effect = inference
    assert pair.translate(['models were given, with'], 5, 8, 16) == ['模型给出了，与']
    calls = pair.engine.translate_batch.call_args_list
    assert [call.kwargs['repetition_penalty'] for call in calls] == [1, 1.1]
    assert all(call.kwargs['beam_size'] == 5 for call in calls)


def test_nontruncated_responses_preserve_exact_decoder_text():
    pair, calls = pair_model(1000)
    sources = [' one complete rendering ', 'second rendering']
    assert pair.translate(sources, 5, 8, 16) == sources
    assert calls == [sources]
