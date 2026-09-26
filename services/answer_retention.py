"""Choose a whole verified answer, never splice drafts or recycle labels.

An earlier answer is eligible only while the observed evidence, citation bodies,
scope and package revision remain unchanged. Expanded search changes are tracked
before merging, because a merge may hide a changed body behind the same ID.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from services import answer_quality as aq


def _fingerprint(item):
    if not isinstance(item, dict):
        return None
    body = item.get('original_text') or item.get('text')
    if not body or not (item.get('evidence_id') or item.get('source_path')):
        return None
    # Ranking and citation label changes do not change the underlying evidence.
    value = {key: item.get(key) for key in (
        'evidence_id', 'source_path', 'file_id', 'location', 'label',
        'translated_text', 'preview_only', 'coverage', 'evidence_role',
        'source_kind', 'content_hash', 'source_revision')}
    value['body'] = body
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    default=str).encode('utf-8')).hexdigest()


def evidence_state(retrieval):
    """Return an order-independent observed corpus, or None if unprovable."""
    if not isinstance(retrieval, dict):
        return None
    if retrieval.get('error') or retrieval.get('status') in {'failed', 'error', 'cancelled'}:
        return None
    items = retrieval.get('results')
    if not isinstance(items, list):
        return None
    values = [_fingerprint(item) for item in items]
    return None if None in values else frozenset(values)


def advance_evidence_state(previous, retrieval):
    current = evidence_state(retrieval)
    if previous is None or current is None:
        return None
    return previous | current


def capture_candidate(result, verification, plan, session, observed_evidence):
    return copy.deepcopy({'result': result, 'verification': verification,
        'plan': plan, 'session': session, 'observed_evidence': observed_evidence})


def _citation_state(result):
    values = [_fingerprint(item) for item in result.get('citations') or []]
    return frozenset(values) if values and None not in values else None


def _revision(result):
    manifest = result.get('input_manifest') or {}
    return ((result.get('package_reading') or {}).get('source_digest'),
            (manifest.get('package_reading') or {}).get('source_digest'))


def _score(verification):
    claims = (verification.get('ledger') or {}).get('claims') or []
    facts = {re.sub(r'\[\d+\]|\s+', '', str(c.get('text') or ''))
             for c in claims if c.get('status') == 'supported'}
    return (-len(set(verification.get('missing_answer_aspects') or [])), len(facts))


def select_candidate(candidates, verifier, question, tool_results, batch_summary, checkpoint):
    """Bounded local revalidation; does not generate or retrieve anything."""
    latest = candidates[-1]
    common_contract = aq.answer_contract(question, latest['result'].get('citations') or [])
    latest_citations = _citation_state(latest['result'])
    evaluated = []
    decisions = []
    for index, candidate in enumerate(candidates):
        checkpoint()
        reason = None
        if index != len(candidates) - 1:
            if not latest['observed_evidence'] or candidate['observed_evidence'] != latest['observed_evidence']:
                reason = 'observed_evidence_changed_or_unknown'
            elif not latest_citations or _citation_state(candidate['result']) != latest_citations:
                reason = 'citation_evidence_changed_or_unknown'
            elif candidate['plan'].get('scope') != latest['plan'].get('scope'):
                reason = 'scope_changed'
            elif _revision(candidate['result']) != _revision(latest['result']):
                reason = 'package_revision_changed'
        if reason:
            decisions.append({'attempt': index, 'eligible': False, 'reason': reason})
            continue
        candidate = copy.deepcopy(candidate)
        result = candidate['result']
        manifest = dict(result.get('input_manifest') or {})
        manifest['answer_contract'] = copy.deepcopy(common_contract)
        result['input_manifest'] = manifest
        # Recheck the actual candidate under the final tools/batch, not a cached
        # score, before removing unsupported claims. Keep each candidate's own
        # citations and labels paired with its answer throughout.
        verification = verifier.verify(result, candidate['plan'],
            tool_results=tool_results, batch_summary=batch_summary)
        if any(c.get('status') == 'unsupported'
               for c in (verification.get('ledger') or {}).get('claims') or []):
            result = verifier.guard_result(result, verification)
            verification = verifier.verify(result, candidate['plan'],
                tool_results=tool_results, batch_summary=batch_summary)
        candidate.update(result=result, verification=verification)
        metrics = verification.get('quality_metrics') or {}
        safe = (not verification.get('failed_claims')
                and not manifest.get('invalid_generated_citation_indexes')
                and not any(metrics.get(key) for key in
                    ('out_of_scope_citation_count', 'numeric_failure_count')))
        score = _score(verification)
        decisions.append({'attempt': index, 'eligible': safe, 'score': list(score),
                          'missing_answer_aspects': list(verification.get('missing_answer_aspects') or [])})
        # The newest candidate remains the baseline even if still partial.
        # A retained earlier candidate must have no unverified factual claims.
        if safe or index == len(candidates) - 1:
            evaluated.append((score, index, candidate))
    _, selected_index, selected = max(evaluated, key=lambda item: (item[0], item[1]))
    selected['verification']['candidate_selection'] = {
        'policy': 'verified-answer-retention/1.0', 'selected_attempt': selected_index,
        'latest_attempt': len(candidates) - 1, 'retained_previous': selected_index != len(candidates) - 1,
        'candidates': decisions, 'model_calls_added': 0, 'citation_sets_spliced': False}
    return selected
