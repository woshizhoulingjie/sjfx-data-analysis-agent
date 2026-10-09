"""Recomputable actor/version states with separate evidence and business axes."""
from __future__ import annotations
from datetime import datetime, timezone
from .communication_acts import fingerprint, source_action_polarity, recheck_expression

VERSION = 'issue-state-v4'
EXPRESSIONS = {'request': 'requested', 'propose': 'proposed', 'accept': 'accepted',
               'reject': 'rejected', 'commit': 'committed', 'deliver': 'reported_delivery',
               'report_completed': 'claimed_completed', 'amend': 'amended',
               'withdraw': 'withdrawn', 'inform': 'informed',
               'acknowledge_receipt': 'received_information', 'question': 'questioned',
               'confirm': 'confirmed_unspecified', 'other': 'unresolved'}


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            return None
        return result.astimezone(timezone.utc)
    except ValueError:
        return None


def visible_at(act, as_of=None):
    if as_of is None:
        return True
    cutoff = timestamp(as_of)
    if cutoff is None:
        raise ValueError('as_of requires a timezone-aware ISO timestamp')
    asserted = timestamp(act.get('asserted_at'))
    effective = timestamp(act.get('effective_at'))
    # Never include later assertions in a historical information state.
    return asserted is not None and asserted <= cutoff and (effective is None or effective <= cutoff)


def expression(act):
    value = EXPRESSIONS.get(act['act_type'], 'unresolved')
    if act.get('kind') == 'received' or act.get('attribution') != 'authored':
        return 'received_information'
    polarity,_=source_action_polarity(act['act_type'],act.get('exact_quote') or '',act.get('model_polarity',act.get('polarity')))
    if polarity in {'negative', 'negated', False, 'unknown'} or act.get('temporal_axis') in {'hypothetical', 'counterfactual'}:
        return 'unresolved'
    if value in {'accepted', 'committed'}:
        if not act.get('object_identity'):
            return 'unresolved'
        if act.get('conditions'):
            return 'conditional_accept' if value == 'accepted' else 'conditional_commit'
    return value


def derive_issue_states(acts, *, as_of=None):
    groups = {}
    for act in acts:
        act=recheck_expression(act)
        if not visible_at(act, as_of):
            continue
        obj = act.get('object_identity') or ('unresolved:' + act['act_id'])
        key = (act['actor'], obj, act.get('object_version') or 'unspecified')
        groups.setdefault(key, []).append(act)
    result = []
    for (actor, obj, version), history in sorted(groups.items()):
        known, unknown = [], []
        for act in history:
            effective = timestamp(act.get('effective_at'))
            asserted = timestamp(act.get('asserted_at'))
            if effective or asserted:
                known.append((effective or asserted, act['act_id'], act))
            else:
                unknown.append(act)
        known.sort(key=lambda value: (value[0], value[1]))
        latest = known[-1][2] if known else None
        gaps = []
        if unknown:
            gaps.append('unknown_time_expressions_retained')
        if latest is None:
            latest = sorted(unknown, key=lambda a: a['act_id'])[-1]
            gaps.append('current_state_time_unresolved')
        # A contingent/negated/hypothetical withdrawal is an expression requiring
        # review. It cannot erase a previous effective commitment.
        mutations = {'withdraw', 'amend'}
        eligible = []
        pending = []
        for _, _, act in known:
            target = act.get('target_act_id')
            earlier = {a['act_id'] for a in eligible}
            blocked = act['act_type'] in mutations and (
                bool(act.get('conditions')) or expression(act) == 'unresolved' or
                bool(target and target not in earlier) or not act.get('object_version'))
            if blocked:
                pending.append(act)
            else:
                eligible.append(act)
        if eligible and pending and latest in pending:
            latest = eligible[-1]
            gaps.append('later_mutation_target_or_conditions_unverified')
        elif latest['act_type'] in mutations and (latest.get('conditions') or not latest.get('object_version') or
                                                latest.get('target_act_id') and not eligible):
            gaps.append('mutation_target_or_conditions_unverified')
        candidate = expression(latest)
        if candidate == 'unresolved':
            gaps.append('expression_object_or_factuality_unresolved')
        if latest.get('conditions'):
            gaps.append('condition_satisfaction_not_independently_verified')
        if not latest.get('object_version'):
            gaps.append('object_version_unspecified')
        version_conflict = not latest.get('object_version') and any(
            a.get('object_identity') == latest.get('object_identity') and a.get('object_version') for a in acts)
        if version_conflict:
            gaps.append('expression_target_version_unresolved')
        supported = latest.get('semantic_status') == 'supported'
        if not supported:
            gaps.append('semantic_support_not_verified')
        if latest.get('context_review_status') in {'contradicted','unresolved'}:
            gaps.append('context_review_unresolved_or_counterevidence')
        status = candidate if supported and not unknown and not version_conflict and 'mutation_target_or_conditions_unverified' not in gaps else 'pending_verification'
        result.append({'actor': actor, 'object': obj, 'object_version': version,
                       'status': status, 'expressed_status': candidate,
                       'review_status': latest.get('review_status', 'unresolved'),
                       'context_review_status':latest.get('context_review_status'),
                       'qualifications':latest.get('qualifications',[]),
                       'conditions': latest.get('conditions', []), 'as_of': as_of,
                       'asserted_at': latest.get('asserted_at'), 'effective_at': latest.get('effective_at'),
                       'time_resolution_status': latest.get('time_resolution_status', 'unknown'),
                       'source_act_ids': [latest['act_id']],
                       'history_act_ids': [x[2]['act_id'] for x in known] + [a['act_id'] for a in unknown],
                       'evidence_ids': latest.get('evidence_ids', []), 'gaps': gaps,
                       'real_world_completion': 'unverified', 'engine_version': VERSION})
    return result


def state_fingerprint(acts, scope, as_of):
    return fingerprint([VERSION, scope, as_of, sorted(acts, key=lambda a: a['act_id'])])
