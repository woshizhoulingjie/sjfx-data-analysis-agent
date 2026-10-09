"""Persistable token accounting; missing provider usage is explicitly estimated.

Reservations count an estimated input plus the configured output ceiling. They
are made before a call, so a failed or interrupted call cannot disappear from
the budget. The input estimate is a heuristic, not a provider tokenizer or a
guarantee; an actual overrun is recorded and prevents subsequent model calls.
"""
from __future__ import annotations

import copy
import json
import math

VERSION = 'mail-token-budget-v1'


def estimate_tokens(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    # UTF-8 accounts for Chinese and Thai more conservatively than len/4.
    return max(1, math.ceil(len(text.encode('utf-8')) / 3))


def init_usage(budget=None, previous=None):
    budget = budget or {}
    limit = budget.get('max_tokens', budget.get('max_token_budget'))
    result = {'version': VERSION, 'max_tokens': None if limit is None else max(0, int(limit)),
              'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0,
              'actual_tokens': 0, 'estimated_tokens': 0, 'reserved_tokens': 0,
              'calls': 0, 'failed_calls': 0, 'actual_calls': 0, 'estimated_calls': 0,
              'exhausted': False, 'overrun': False, 'estimate_method': 'utf8_bytes_div3_v1'}
    if isinstance(previous, dict):
        result.update(copy.deepcopy(previous))
        result['max_tokens'] = None if limit is None else max(0, int(limit))
        result['exhausted'] = limit is not None and result['total_tokens'] + result['reserved_tokens'] >= max(0,int(limit))
        result['overrun'] = limit is not None and result['total_tokens'] + result['reserved_tokens'] > max(0,int(limit))
    return result


def reserve_call(usage, system, prompt, max_output):
    count = estimate_tokens(str(system or '') + '\n' + str(prompt or ''))
    total = count + max(0, int(max_output))
    limit = usage.get('max_tokens')
    if limit is not None and usage['total_tokens'] + usage['reserved_tokens'] + total > limit:
        usage['exhausted'] = True
        return None
    reservation = {'input_estimate': count, 'output_reserved': max(0, int(max_output)),
                   'total': total, 'pending': True}
    usage['reserved_tokens'] += total
    return reservation


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def record_usage(usage, response, system, prompt, reservation=None):
    if reservation is not None and not reservation.get('pending'):
        return usage_snapshot(usage)
    if reservation is not None:
        usage['reserved_tokens'] = max(0, usage['reserved_tokens'] - reservation['total'])
        reservation['pending'] = False
    provider = response.get('usage', {}) if isinstance(response, dict) else {}
    provider = provider if isinstance(provider, dict) else {}
    incoming = _count(provider.get('input_tokens', provider.get('prompt_tokens')))
    outgoing = _count(provider.get('output_tokens', provider.get('completion_tokens')))
    actual_total = _count(provider.get('total_tokens'))
    if actual_total is not None:
        if incoming is not None and outgoing is None and actual_total >= incoming:
            outgoing = actual_total - incoming
        elif outgoing is not None and incoming is None and actual_total >= outgoing:
            incoming = actual_total - outgoing
    estimate_in = (reservation or {}).get('input_estimate', estimate_tokens(str(system or '') + '\n' + str(prompt or '')))
    if response is None:
        estimate_out = (reservation or {}).get('output_reserved', 0)
        usage['failed_calls'] += 1
    else:
        estimate_out = estimate_tokens(response.get('json', response) if isinstance(response, dict) else response)
    if actual_total is not None and incoming is None and outgoing is None:
        # The provider knows the total, but not its split. Do not invent one.
        input_count, output_count = 0, actual_total
        actual, estimated = actual_total, 0
        usage['split_unavailable_calls'] = usage.get('split_unavailable_calls', 0) + 1
    else:
        input_count = incoming if incoming is not None else estimate_in
        output_count = outgoing if outgoing is not None else estimate_out
        actual = (incoming or 0) + (outgoing or 0)
        estimated = (estimate_in if incoming is None else 0) + (estimate_out if outgoing is None else 0)
        if actual_total is not None and incoming is not None and outgoing is not None and actual_total > actual:
            usage['unattributed_actual_tokens'] = usage.get('unattributed_actual_tokens',0) + actual_total - actual
            actual = actual_total
    usage['input_tokens'] += input_count
    usage['output_tokens'] += output_count
    usage['total_tokens'] += actual + estimated
    usage['actual_tokens'] += actual
    usage['estimated_tokens'] += estimated
    usage['calls'] += 1
    usage['actual_calls'] += int(bool(actual) or incoming is not None or outgoing is not None or actual_total is not None)
    usage['estimated_calls'] += int(bool(estimated))
    limit = usage.get('max_tokens')
    if limit is not None and usage['total_tokens'] + usage['reserved_tokens'] >= limit:
        usage['exhausted'] = True
        usage['overrun'] = usage['total_tokens'] + usage['reserved_tokens'] > limit
    return usage_snapshot(usage)


def usage_snapshot(usage):
    result = copy.deepcopy(usage)
    result['accounting'] = ('mixed' if usage.get('actual_calls') and usage.get('estimated_calls') else
                            'actual' if usage.get('actual_calls') else 'estimated')
    result['input_estimate_is_provider_tokenizer'] = False
    return result
