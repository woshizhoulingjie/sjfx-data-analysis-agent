"""Bounded evidence-demand retrieval. Recall hints never prove sufficiency."""
from __future__ import annotations

from .evidence_packing import evidence_role

VERSION = 'mail-gap-planner-v1'
SUFFIXES = {'support': '明确回应 接受 accept agreed', 'counter': '撤回 拒绝 withdraw reject',
            'condition': '条件 审批 subject to approval', 'update': '版本 修改 revise version'}


def plan_gap_queries(plan, evidence, used_queries=(), *, limit=2, verification_gaps=()):
    groups = {}
    for item in evidence:
        key = str(item.get('issue_id') or item.get('object_ref') or 'unresolved')
        groups.setdefault(key, set()).add(evidence_role(item))
    if not groups: groups['unresolved'] = set()
    demands = plan.get('required_evidence') or ['support', 'condition', 'counter', 'update']
    missing = []
    for requirement in demands:
        role = requirement.get('role') if isinstance(requirement, dict) else requirement
        if role in SUFFIXES and any(role not in roles for roles in groups.values()) and role not in missing:
            missing.append(role)
    anchors = plan.get('queries') or [plan.get('goal') or '']
    anchor = str(anchors[0]).strip()[:160]
    semantic_queries = []
    for gap in verification_gaps:
        if not isinstance(gap, dict) or gap.get('semantic_status') != 'insufficient':continue
        needs = ' '.join(gap.get('missing_requirements') or [])
        keywords = [word for word in ('版本', '接受', '回应', '条件', '撤回', '付款', '完成', '审批', '确认') if word in needs]
        semantic_queries.append(str(gap.get('object_ref') or anchor)[:160] + ' ' +
                                (' '.join(keywords) or '明确回应 原文 条件 version response'))
    candidates = semantic_queries + list(plan.get('gap_queries') or []) + [anchor + ' ' + SUFFIXES[r] for r in missing]
    used, queries = set(used_queries), []
    for query in candidates if limit > 0 else []:
        normalized = query.strip()[:300] if isinstance(query,str) else ''
        if normalized and normalized not in used:
            queries.append(normalized); used.add(normalized)
        if len(queries) >= limit:
            break
    return {'queries': queries, 'missing_roles': missing,
            'missing_by_issue': {key: [r for r in missing if r not in roles] for key, roles in groups.items()},
            'requirements_verified': False, 'basis': 'retrieval_hints_only', 'version': VERSION}


def stop_reason(*, round_number, max_rounds, new_count=None, candidates=0, max_candidates=500,
                cancelled=False, deadline=False, tool_limit=False, token_limit=False,
                model_limit=False, pending_queries=True):
    if cancelled: return 'cancelled'
    if deadline: return 'time_budget'
    if tool_limit: return 'tool_budget'
    if token_limit: return 'token_budget'
    if model_limit: return 'model_budget'
    if candidates >= max_candidates: return 'candidate_budget'
    if new_count == 0: return 'no_new_evidence'
    if not pending_queries: return 'no_untried_gap_query'
    if round_number >= max_rounds: return 'round_budget'
    return None
