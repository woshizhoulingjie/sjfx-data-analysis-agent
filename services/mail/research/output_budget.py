"""Conservative, bounded completion reservations for structured research output."""
import json
import math

MAX_OUTPUT_TOKENS = 32768


def estimate_output_tokens(value):
    """Use UTF-8 /2 plus reservation headroom for CJK and opaque IDs.

    This is a reservation heuristic, not a provider tokenizer. In particular,
    input accounting's UTF-8 /3 estimate must not size generated Chinese JSON.
    """
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return max(1, math.ceil(len(text.encode('utf-8')) / 2))


def complete_output_reservation(skeleton, *, minimum, previous=None):
    # A repair replaces the COMPLETE prior result. Never clip prior rows,
    # conditions, literal quotes or histories merely to fit the reservation.
    size = max(estimate_output_tokens(skeleton), estimate_output_tokens(previous) if previous is not None else 0)
    requested = max(minimum, (size * 5 + 3) // 4 + 512)
    if requested > MAX_OUTPUT_TOKENS:
        raise ValueError('完整结构化输出预计超过32768 tokens，未截断判定行、条件或既有修复结果')
    return requested


def retrieval_output_tokens(purpose, payload):
    previous = payload.get('previous_output')
    if purpose == 'relevance':
        old_rows = previous.get('decisions', []) if isinstance(previous, dict) else []
        rows = []
        for message in payload.get('messages') or []:
            evidence = message.get('evidence') or []
            source = max(evidence, key=lambda e: estimate_output_tokens(str(e.get('text') or e.get('quote_text') or '')), default={})
            quote = str(source.get('text') or source.get('quote_text') or '')
            rationale = '据' * 500
            for old in old_rows if isinstance(old_rows, list) else []:
                if isinstance(old, dict) and old.get('message_key') == message.get('message_key'):
                    if estimate_output_tokens(str(old.get('source_quote') or '')) > estimate_output_tokens(quote):
                        quote = str(old['source_quote'])
                    if estimate_output_tokens(str(old.get('rationale') or '')) > estimate_output_tokens(rationale):
                        rationale = str(old['rationale'])
            rows.append({'message_key': message.get('message_key'), 'status': 'not_matched',
                         'rationale': rationale, 'evidence_id': source.get('evidence_id'), 'source_quote': quote})
        skeleton = {'decisions': rows}
        minimum = 1200
    elif purpose == 'rerank':
        candidates = payload.get('candidates') or []
        skeleton = {'segment_ids': [row.get('segment_id') for row in candidates],
                    'scores': [{'segment_id': row.get('segment_id'), 'score': 0.123456,
                                'rationale': '据' * 120} for row in candidates]}
        minimum = 700
    else:
        raise ValueError('未支持的检索输出预算用途')
    return complete_output_reservation(skeleton, minimum=minimum, previous=previous)
