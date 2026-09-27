"""Project normalized node claims into the existing conclusion/evidence UI.

This adapter does not verify new claims or infer missing support. Legacy
conclusion payloads without a QAE contract remain unchanged.
"""
from copy import deepcopy
import hashlib


def project_node_claims(payload):
    qae = payload.get('question_answer_evidence')
    qae = qae if isinstance(qae, dict) else {}
    if not qae and not str(payload.get('evidence_contract') or '').startswith('question-answer-evidence/'):
        return {}
    # An explicitly emptied top-level list is authoritative (e.g. numeric
    # grounding rejected the earlier nested claims). Never resurrect it.
    claims = payload.get('claims', qae.get('claims', []))
    claims = claims if isinstance(claims, list) else []
    evidence = payload.get('evidence', qae.get('evidence', []))
    evidence = evidence if isinstance(evidence, list) else []
    scope = payload.get('member_paths')
    scope = set(str(p) for p in scope) if isinstance(scope, list) else None
    overall = str(payload.get('evidence_status', qae.get('evidence_status', '')) or '').lower()
    coverage = payload.get('coverage')
    coverage = coverage if isinstance(coverage, dict) else {}
    blocked = (overall in {'candidate', 'preview_candidate', 'insufficient', 'unsupported', 'rejected', 'unverified', 'pending'}
               or bool(payload.get('preview_only') or coverage.get('preview_only'))
               or payload.get('analysis_level') == 'preview'
               or payload.get('analysis_stage') in {'preview', 'preliminary'})
    by_id, ambiguous = {}, set()
    for item in evidence:
        if not isinstance(item, dict):
            continue
        eid = str(item.get('evidence_id') or item.get('id') or '')
        if not eid:
            continue
        if eid in by_id and by_id[eid] != item:
            ambiguous.add(eid)
        by_id[eid] = item

    def eligible(item):
        source = item.get('source_path') or item.get('path')
        quote = item.get('supporting_quote') or item.get('text') or item.get('content')
        item_coverage = item.get('coverage')
        item_coverage = item_coverage if isinstance(item_coverage, dict) else {}
        return (isinstance(source, str) and bool(source.strip())
                and isinstance(quote, str) and bool(quote.strip())
                and (scope is None or source in scope)
                and not item.get('preview_only')
                and not item_coverage.get('preview_only')
                and item.get('evidence_level') != 'preview'
                and item.get('evidence_role') != 'node_coverage'
                and item.get('support_status') in {'supported', 'partially_supported'}
                and item.get('file_conclusion_status', 'supported') in {'supported', 'partially_supported'})

    conclusions, review, used = [], [], set()
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        statement = str(claim.get('statement') or '').strip()
        if not statement:
            continue
        ids = claim.get('evidence_ids')
        ids = list(dict.fromkeys(str(i) for i in ids)) if isinstance(ids, list) else []
        supports = [deepcopy(by_id[eid]) for eid in ids
                    if eid in by_id and eid not in ambiguous and eligible(by_id[eid])]
        status = str(claim.get('support_status') or '')
        if blocked or status not in {'supported', 'partially_supported'} or not supports:
            review.append({'text': statement, 'support_status': status or 'insufficient',
                           'evidence_ids': ids, 'reason': '候选判断或缺少可追溯的结论支撑，未进入正式结论。'})
            continue
        # Dropped or partial supports cannot yield a fully verified card.
        if len(supports) != len(ids) or any(
                s.get('support_status') != 'supported'
                or s.get('file_conclusion_status', 'supported') != 'supported' for s in supports):
            status = 'partially_supported'
        key = hashlib.sha256((statement + '\0' + '\0'.join(ids)).encode()).hexdigest()[:20]
        conclusions.append({'conclusion_id': 'node-claim-' + key, 'text': statement,
                            'statement': statement, 'type': claim.get('type') or 'observation',
                            'supports': supports, 'evidence_ids': [s.get('evidence_id') or s.get('id') for s in supports],
                            'support_status': status, 'status': 'verified' if status == 'supported' else 'partial',
                            'support_label': '原文直接支持' if status == 'supported' else '部分支撑，需复核'})
        used.update(s.get('evidence_id') or s.get('id') for s in supports)
    if conclusions:
        status = 'supported' if not review and overall != 'partially_supported' and all(
            c['support_status'] == 'supported' for c in conclusions) else 'partially_supported'
    else:
        status = 'candidate' if overall in {'candidate', 'preview_candidate'} else 'insufficient'
    verification = 'verified' if status == 'supported' else ('partial' if conclusions else 'unverified')
    coverage = deepcopy(coverage)
    coverage['verification_status'] = verification
    return {'conclusions': conclusions, 'file_review_items': review,
            'evidence_status': status, 'verification_status': verification,
            'coverage': coverage, 'evidence_count': len(used),
            'conclusion_contract': 'node-claim-presentation/1.0'}
