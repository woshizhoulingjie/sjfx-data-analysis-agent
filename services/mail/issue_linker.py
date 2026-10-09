"""Conservative issue alignment. Recall scores never authorize a merge."""
from __future__ import annotations
import re
from .communication_acts import stable_id, canonical

VERSION = 'issue-linker-v2'
ANCHOR_KINDS = {'business_id', 'contract_id', 'order_id', 'quotation_id',
                'invoice_id', 'project_id', 'ticket_id'}


def checked_anchor(act):
    raw = act.get('issue_anchor')
    if not isinstance(raw, dict) or raw.get('kind') not in ANCHOR_KINDS:
        return None
    value = raw.get('value')
    obj = str(raw.get('object') or act.get('object_identity') or '').strip()
    if not isinstance(value, str) or not 2 <= len(value.strip()) <= 160 or not obj:
        return None
    value = value.strip()
    # Ordinary topic words and a model-generated title are not business IDs.
    if not re.search(r'\d', value) or not any(value in c['quote'] for c in act['citations']):
        return None
    if act.get('object_identity') and act['object_identity'] != obj:
        return None
    return {'kind': raw['kind'], 'value': value, 'object': obj}


def provisional_issue(act):
    anchor = checked_anchor(act)
    if anchor:
        issue_id = stable_id('missue', [act['package_id'], anchor, act['participants']])
    else:
        issue_id = stable_id('missue', [act['package_id'], 'provisional', act['act_id']])
    return {'issue_id': issue_id, 'package_id': act['package_id'],
            'title': act['issue_title'], 'anchor_key': canonical(anchor) if anchor else '',
            'anchors': [anchor] if anchor else [], 'participants': act['participants'],
            'objects': [act['object_identity']] if act['object_identity'] else [],
            'link_status': 'anchored' if anchor else 'provisional'}


def conflicts(source, targets, *, same_instance=False):
    result = []
    target_objects = {a.get('object_identity') for a in targets if a.get('object_identity')}
    source_object = source.get('object_identity')
    if source_object and target_objects and target_objects != {source_object}:
        result.append('object_identity_conflict')
    if same_instance:
        target_versions = {a.get('object_version') for a in targets if a.get('object_version')}
        if source.get('object_version') and target_versions and target_versions != {source['object_version']}:
            result.append('object_version_conflict')
    source_anchor = checked_anchor(source)
    anchors = [checked_anchor(a) for a in targets]
    if source_anchor and any(a and a != source_anchor for a in anchors):
        result.append('business_anchor_conflict')
    target_parties = {p for a in targets for p in a.get('participants', [])}
    if not target_parties.intersection(source.get('participants', [])):
        result.append('participant_relation_missing')
    return result


def validate_link_decision(decision, source, targets):
    """Both original sides and explicit audit are necessary, confidence is ignored."""
    if decision.get('decision') not in {'same_issue', 'same_instance'}:
        return False, ['decision_is_not_merge']
    if decision.get('review_status') != 'supported' or decision.get('method') not in {'semantic_support_checked', 'human'}:
        return False, ['semantic_alignment_not_reviewed']
    cited = set(decision.get('evidence_ids') or [])
    source_ids = set(source.get('evidence_ids', []))
    targets = [a for a in targets if a['act_id'] != source['act_id']]
    target_ids = {e for a in targets for e in a.get('evidence_ids', [])}
    if not cited.intersection(source_ids) or not cited.intersection(target_ids):
        return False, ['both_sides_exact_evidence_required']
    if not isinstance(decision.get('reason'), str) or not 1 <= len(decision['reason'].strip()) <= 2000:
        return False, ['alignment_reason_missing']
    failures = conflicts(source, targets, same_instance=decision['decision'] == 'same_instance')
    # Semantic alignment requires an actual object on both sides. A vague short
    # reply can be kept as a candidate until its referenced object is audited.
    if not source.get('object_identity') or not any(a.get('object_identity') for a in targets):
        failures.append('object_identity_unresolved')
    return not failures, failures
