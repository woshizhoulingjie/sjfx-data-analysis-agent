"""Bounded extractive repair of omitted, explicitly scoped quantity answers.

No new model call, retrieval, arithmetic, or relaxed verification. Only an
unambiguous observed record from the existing citation set can contribute;
every proposed addition must pass the same final verifier as model prose.
"""
from __future__ import annotations

import copy
import re

from services import answer_quality as aq
from services.conversation import ConversationScope
from services.evidence import evidence_quality
from services.grounded_clauses import (
    _CONDITIONAL, _DATE, _JOIN, _ORGANIZATION, _QUANTITY, _UNCERTAIN,
    _context_matches, _plain, _record_quantities, _record_windows,
)

_ASPECT_ROLE = {'实收数量': '实收', '缺少数量': '尚缺'}
_STALE_GAP_NOTE = '资料暂时无法支持的内容没有纳入这份回答。'
_QUERY_TRANSFORM = re.compile(
    r'如果|假设|假如|预计|计划|未来|总计|合计|累计|平均|增长|新增|分别|比较|对比|各自|'
    r'之前|之后|以前|以后|昨天|今天|明天|本月|上月|最新|现在|目前|截至')
_QUANTITY_HINT = re.compile(
    r'(?:实收到货|实收|尚缺|实际收到|已收到|已到货|还缺|缺少)[^。；;]{0,12}'
    r'[0-9]+(?:[.][0-9]+)?[ ]*(?:台|件|份|套|辆|箱|个|吨|千克|公斤|毫米|厘米|米)')
_REVOCATION = re.compile(r'更正|纠正|作废|撤销|误报|误记|不属实|否认|传闻')
_BOUNDARY = r'[。！？!?；;|，]|(?<![0-9]),|,(?![0-9])|' + chr(10)
_KEYS = ('projects', 'identifiers', 'dates', 'organizations')
_MENTION = re.compile(
    r'(?P<role>实收到货|实收|尚缺|实际收到|已收到|已到货|还缺|缺少)'
    r'(?:数量)?[ 的为是共：:]*'
    r'(?P<value>[+-]?[0-9]+(?:[.][0-9]+)?)[ ]*'
    r'(?P<unit>台|件|份|套|辆|箱|个|吨|千克|公斤|毫米|厘米|米)')


def _identity(text):
    return {'projects': aq._projects(text),
            'identifiers': set(re.findall(aq.RECORD_ID, text)),
            'dates': set(_DATE.findall(text)),
            'organizations': set(_ORGANIZATION.findall(text))}


def complete_quantities(result, verification, plan, verifier, *,
                        tool_results=None, batch_summary=None, checkpoint=lambda: None):
    """Return unchanged inputs unless a complete, reverified improvement exists.

Unqualified questions, calculations, temporal selection, competing records,
unread evidence and unsupported grammar remain explicit gaps. Do not guess.
"""
    if (not aq.enabled() or verification.get('failed_claims')
            or verification.get('contradictions') or verification.get('counter_evidence')):
        return result, verification
    missing = set(verification.get('missing_answer_aspects') or [])
    requested = [a for a in _ASPECT_ROLE if a in missing]
    if not requested:
        return result, verification
    query = str(result.get('resolved_query') or result.get('question') or plan.get('objective') or '')
    if len(query) > 2000 or _QUERY_TRANSFORM.search(query):
        return result, verification
    context = _identity(query)
    if not (context['projects'] or context['identifiers'] or context['organizations']):
        return result, verification
    if any(len(context[key]) > 1 for key in ('projects', 'dates', 'organizations')):
        return result, verification
    units = set(re.findall(r'(?:多少|几)(台|件|份|套|辆|箱|个|吨|千克|公斤)', query))
    citations = list(result.get('model_citations', result.get('citations') or []) or [])
    if not citations or len(citations) > 32:
        return result, verification
    # Keep the existing verifier independent of this repair.
    from services.claim_verifier import _scope_contains
    scope = ConversationScope.from_dict(plan.get('scope') or {'kind': 'package'})
    observations, mentions, chars = [], [], 0
    for position, citation in enumerate(citations, 1):
        checkpoint()
        text = str(citation.get('original_text') or '')
        if not text:
            continue
        chars += len(text)
        if chars > 64000 or len(text) > 16000:
            return result, verification
        item = dict(citation, text=text)
        if not _scope_contains(scope, citation):
            continue
        if _QUANTITY_HINT.search(text) and _REVOCATION.search(text):
            return result, verification
        if (aq.source_kind(item) == 'preview' or citation.get('source_kind') == 'preview'
                or not evidence_quality(item).get('eligible')):
            # Unqualified evidence cannot support or resolve a conflicting
            # quantity. Never silently discard it and prefer another number.
            if _QUANTITY_HINT.search(text):
                return result, verification
            continue
        for window in _record_windows(text):
            if not _QUANTITY_HINT.search(window):
                continue
            identity = _identity(window)
            if not _context_matches(context, window):
                # Anonymous counts could conflict. Named other records are
                # unrelated, not support for the object in this question.
                if not any(identity[key] for key in ('projects', 'identifiers', 'organizations')):
                    return result, verification
                continue
            if _CONDITIONAL.search(window) or _UNCERTAIN.search(window):
                return result, verification
            quantities = _record_quantities(window)
            if not quantities:
                # Narrative mentions (e.g. a request concerning missing items)
                # are veto-only, never a replacement observed record. Keep
                # every recognized value for conflict checks after selection.
                found = list(_MENTION.finditer(window))
                if not found:
                    return result, verification
                for match in found:
                    prefix = window[max(0, match.start() - 8):match.start()]
                    if re.search(r'未|没有|无|不|非', prefix):
                        return result, verification
                    role = '尚缺' if match['role'] in {'尚缺', '还缺', '缺少'} else '实收'
                    mentions.append((role, match['value'] + match['unit'], identity['identifiers']))
                continue
            if any(len(values) != 1 for values in quantities.values()):
                return result, verification
            if any(_ASPECT_ROLE[a] not in quantities for a in requested):
                return result, verification
            excerpts = {}
            for clause in re.split(_BOUNDARY, window):
                for fragment in _JOIN.split(clause):
                    plain = _plain(fragment)
                    match = _QUANTITY.fullmatch(plain)
                    if not match:
                        continue
                    role = '尚缺' if match['role'] == '尚缺' else '实收'
                    value = match['value'] + match['unit']
                    if quantities.get(role) != {value} or (units and match['unit'] not in units):
                        continue
                    excerpt = plain[match.start('role'):]
                    # Presentation normalization is only a matching aid.
                    if excerpt not in text:
                        continue
                    excerpts.setdefault(role, excerpt)
            if any(_ASPECT_ROLE[a] not in excerpts for a in requested):
                return result, verification
            signature = tuple((key, tuple(sorted(identity[key]))) for key in _KEYS)
            observations.append((signature, quantities, excerpts, citation, position))
    if not observations or len({o[0] for o in observations}) != 1:
        return result, verification
    for role in _ASPECT_ROLE.values():
        values = set().union(*(o[1].get(role, set()) for o in observations))
        if len(values) > 1:
            return result, verification
    _, quantities, excerpts, citation, position = observations[0]
    record_ids = set(dict(observations[0][0])['identifiers'])
    if any(quantities.get(role) != {value} or not identifiers.issubset(record_ids)
           for role, value, identifiers in mentions):
        return result, verification
    index = int(citation.get('citation_index') or position)
    additions = [f'{excerpts[_ASPECT_ROLE[a]]}[{index}]。' for a in requested]
    candidate = copy.deepcopy(result)
    existing = str(result.get('answer') or '')
    if missing == set(requested) and existing.endswith(_STALE_GAP_NOTE):
        existing = existing[:-len(_STALE_GAP_NOTE)].rstrip()
    candidate['answer'] = chr(10).join(additions) + (chr(10) * 2 + existing if existing else '')
    checkpoint()
    checked = verifier.verify(candidate, plan, tool_results=tool_results, batch_summary=batch_summary)
    metrics = checked.get('quality_metrics') or {}
    supported = {str(c.get('text') or '').strip() for c in (checked.get('ledger') or {}).get('claims') or []
                 if c.get('status') == 'supported'}
    if (checked.get('failed_claims') or any(a in (checked.get('missing_answer_aspects') or []) for a in requested)
            or any(line not in supported for line in additions)
            or any(metrics.get(k) for k in ('numeric_failure_count', 'out_of_scope_citation_count'))):
        return result, verification
    audit = {'policy': 'scoped-quantity-completion/1.0', 'aspects': requested,
             'source_path': citation.get('source_path'), 'citation_index': index,
             'source_kind': aq.source_kind(citation), 'excerpts': [excerpts[_ASPECT_ROLE[a]] for a in requested],
             'model_calls_added': 0, 'citation_sets_spliced': False, 'reverified': True}
    candidate.setdefault('input_manifest', {})['quantity_completion'] = audit
    checked['quantity_completion'] = audit
    for key in ('candidate_selection', 'revision_attempts'):
        if key in verification:
            checked[key] = copy.deepcopy(verification[key])
    return candidate, checked
