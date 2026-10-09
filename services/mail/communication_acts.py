"""Source-stable communication expressions; expressions are not world facts."""
from __future__ import annotations

import hashlib
import json
import re

VERSION = 'communication-acts-v3'
FIELDS = ('act_type', 'intent', 'issue', 'issue_title', 'issue_anchor', 'object_identity',
          'object', 'object_version', 'event_ref_id', 'target_act_id', 'polarity', 'certainty',
          'temporal_axis', 'effective_at', 'effective_interval', 'time_resolution_status',
          'explicit_or_inferred', 'unresolved_fields', 'attributed_source', 'qualifications')
EXTRACTION_INSTRUCTIONS = '''
Also supply actor (an exact selected mailbox), act_type (request/propose/accept/reject/
commit/deliver/report_completed/amend/withdraw/inform/acknowledge_receipt/question/
confirm/other), issue, object_identity, object_version, polarity, certainty, temporal_axis,
effective_at and target_act_id when explicitly known. Use null/empty for unresolved
objects, versions or dates; NEVER invent them from a topic or title. issue_anchor is
optional {kind: contract_id/order_id/quotation_id/invoice_id/project_id/ticket_id,
value: verbatim business identifier, object: object_identity}. Conditions retain their
exact object/version scope. confirm without an explicit object is not acceptance.
object_identity is the STABLE BASE business object, without its separately supplied
version: e.g. object_identity="Quote Q-27", object_version="v1" or "v2", and
issue_anchor={kind:"quotation_id",value:"Q-27",object:"Quote Q-27"}. Versions of
the same explicitly identified quote belong to one issue but retain separate
actor/version states and action instances. Do not embed v1/v2 in object_identity
or issue_anchor.object. Different business identifiers remain different objects.
report_completed is only the actor's assertion, not independently verified completion.
An amendment or withdrawal applies only to its exact actor/object/version target.
conditions contain ONLY a premise determining WHETHER this act takes effect,
such as "we will withdraw IF approval is refused". "We withdraw. Legal approval
was not obtained" is an asserted withdrawal with an observed reason: keep that
reason in qualifications/uncertainty, not as a condition postponing withdrawal.
Earlier conditions on acceptance do not automatically become conditions on a
later withdrawal. Preserve each qualification and its original source.
polarity describes whether the COMMUNICATION ACT ITSELF is asserted or negated,
not a positive/negative opinion of the proposal: "We reject v1" and "We withdraw
v2. No payment was approved" have polarity=positive; "We do not reject v1" and
"We will not withdraw v2" have polarity=negative. Preserve the negation's scope.
Do not fill semantic_status: a later auxiliary audit determines semantic support.
'''

ACTION_MARKERS = {
    'reject': re.compile(r'(?i)\breject(?:s|ed|ing)?\b|拒绝|拒絕|ไม่ยอมรับ|ปฏิเสธ'),
    'withdraw': re.compile(r'(?i)\bwithdraw(?:s|n|ing)?\b|\bwithdrew\b|撤回|撤销|撤銷|取消|ถอน(?:ข้อเสนอ)?|ยกเลิก'),
}


def source_action_polarity(act_type, quote, model_polarity):
    """Resolve act negation locally, never interpret rejection as negation itself."""
    marker=ACTION_MARKERS.get(act_type)
    if marker is None:return model_polarity,'model'
    values=[]
    for match in marker.finditer(quote):
        prefix=re.split(r'[.!?;。！？；\n]|\bbut\b|但是|但|แต่',quote[:match.start()],flags=re.I)[-1][-80:]
        english=bool(re.search(r'(?i)\b(?:not|never|cannot|(?:do|does|did|wo|would|ca|could|must|should|is|are|have|has)n[\'’]t)\s+(?:(?:be|been|being|will|would|have|has|had|to|ever|again|formally|actually|intend|plan|want|decide|decided|agree|agreed|going)\s+){0,4}$',prefix))
        chinese=bool(re.search(r'(?:不|不会|不會|未|没有|沒有|从未|從未|无需|無需)(?:再|予以|进行|進行|正式|打算|决定|決定|作出|立即|该|該|会|會){0,3}$',prefix))
        thai=bool(re.search(r'(?:ไม่|ยังไม่|ไม่ได้)\s*(?:จะ|ได้)?\s*$',prefix))
        values.append('negative' if english or chinese or thai else 'positive')
    if not values:return model_polarity,'model_action_unlocated'
    if len(set(values))>1:return 'unknown','mixed_source_action_negation'
    return values[0],'source_action_negation_checked'


CONTINGENCY = re.compile(r'(?i)\b(?:if|unless|provided|pending|once|upon)\b|subject to|conditional on|on condition|after\s+(?:legal\s+)?approval|如果|若|前提|取决于|取決於|批准后|批准後|待.{0,15}批准|หาก|ถ้า|เมื่อได้รับอนุมัติ')
OBSERVED_REASON = re.compile(r'(?i)not\s+(?:obtained|received|granted|approved)|approval\s+(?:was|is|has|had).{0,30}(?:not|failed)|未(?:取得|获得|獲得|获|獲|批准)|没有(?:取得|获得|獲得).{0,15}批准|ไม่ได้รับ.{0,20}(?:อนุมัติ|ความเห็นชอบ)')


def recheck_expression(act):
    """Recompute source-scoped qualifications for old immutable snapshots too."""
    act=dict(act)
    quote=act.get('exact_quote') or ''
    model_polarity=act.get('model_polarity',act.get('polarity','positive'))
    polarity,resolution=source_action_polarity(act['act_type'],quote,model_polarity)
    act.update(polarity=polarity,model_polarity=model_polarity,polarity_resolution=resolution)
    conditions=act.get('conditions') or []
    if act['act_type'] in {'withdraw','reject'} and conditions and polarity=='positive' and resolution=='source_action_negation_checked' and not CONTINGENCY.search(quote) and OBSERVED_REASON.search(quote):
        act['model_conditions']=act.get('model_conditions',list(conditions))
        act['qualifications']=list(act.get('qualifications') or [])+list(conditions)
        act['conditions']=[]
        act['condition_scope_resolution']='observed_reason_not_precondition'
    return act
ACT_TYPES = {'request', 'propose', 'accept', 'reject', 'commit', 'deliver',
             'report_completed', 'amend', 'withdraw', 'inform',
             'acknowledge_receipt', 'question', 'confirm', 'other'}
INTENT_ADAPTER = {'refuse': 'reject', 'negotiate': 'amend', 'coordinate': 'inform'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def stable_id(prefix, value):
    return prefix + '_' + hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def normalize_conditions(values, object_identity, object_version):
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError('conditions must be a bounded list')
    result = []
    for value in values:
        if isinstance(value, str):
            item = {'expression': value, 'object': object_identity,
                    'object_version': object_version, 'satisfaction': 'unverified',
                    'satisfaction_evidence_ids': []}
        elif isinstance(value, dict) and isinstance(value.get('expression'), str):
            item = dict(value)
            item.setdefault('object', object_identity)
            item.setdefault('object_version', object_version)
            # A model's satisfaction label is still only an attributed claim.
            item['satisfaction_claim'] = item.pop('satisfaction', 'unknown')
            item['satisfaction'] = 'unverified'
            item.setdefault('satisfaction_evidence_ids', [])
        else:
            raise ValueError('invalid condition expression')
        if not item['expression'].strip() or len(item['expression']) > 2000:
            raise ValueError('invalid condition expression')
        result.append(item)
    return result


def normalize_finding(package_id, finding, citations, *, actor, addressees,
                      extraction_version=VERSION):
    """Adapt already checked citations; source validation belongs to the gateway."""
    if not isinstance(finding, dict) or not citations:
        raise ValueError('finding must contain checked source citations')
    statement = finding.get('statement')
    if not isinstance(statement, str) or not statement.strip():
        raise ValueError('finding statement missing')
    own = [c for c in citations if c.get('attribution') == 'authored']
    kind = finding.get('kind', 'action')
    primary = sorted(own or citations, key=lambda c: (c.get('sent_at') or '',
                                                     c['message_key'], c['char_start']))[-1]
    raw_type = finding.get('act_type') or finding.get('intent') or 'inform'
    act_type = INTENT_ADAPTER.get(raw_type, raw_type)
    if act_type not in ACT_TYPES:
        act_type = 'other'
    if kind == 'received':
        act_type = 'acknowledge_receipt'
    elif not own:
        raise ValueError('actor expression requires demonstrably authored body')
    object_identity = str(finding.get('object_identity') or finding.get('object') or '').strip()
    object_version = str(finding.get('object_version') or '').strip()
    conditions = normalize_conditions(finding.get('conditions', []), object_identity, object_version)
    model_polarity=finding.get('polarity','positive')
    polarity,polarity_resolution=source_action_polarity(act_type,primary['quote'],model_polarity)
    model_conditions=list(conditions)
    qualifications=list(finding.get('qualifications') or [])
    condition_scope_resolution='model_preconditions'
    if act_type in {'withdraw','reject'} and conditions and polarity=='positive' and polarity_resolution=='source_action_negation_checked' and not CONTINGENCY.search(primary['quote']) and OBSERVED_REASON.search(primary['quote']):
        qualifications.extend(conditions)
        conditions=[]
        condition_scope_resolution='observed_reason_not_precondition'
    review = finding.get('semantic_status')
    verification = finding.get('semantic_verification') or {}
    if review is None and isinstance(verification, dict):
        review = verification.get('status')
    if review not in {'supported', 'contradicted', 'insufficient'}:
        review = 'unresolved'
    locator = [primary['message_key'], primary['mime_path'], primary['char_start'],
               primary['char_end'], primary['source_hash'], primary['quote']]
    # The question, title, translated statement, batch and turn IDs are excluded.
    act_id = stable_id('mact', [package_id, locator, actor, kind, act_type,
                              object_identity, object_version])
    if isinstance(verification,dict):
        verification=dict(verification)
        if 'claim_id' in verification:verification['claim_id']=act_id
    return {'act_id': act_id, 'package_id': package_id,
            'message_key': primary['message_key'], 'source_fragment_id': primary.get('segment_id'),
            'source_hash': primary['source_hash'], 'actor': actor, 'speaker': actor,
            'addressees': sorted(set(addressees)), 'participants': sorted(set([actor] + list(addressees))),
            'kind': kind, 'act_type': act_type, 'statement': statement.strip(),
            'issue_title': str(finding.get('issue') or finding.get('issue_title') or '待对齐事项')[:200],
            'issue_anchor': finding.get('issue_anchor'), 'object': object_identity,
            'object_identity': object_identity, 'object_version': object_version,
            'event_ref_id': finding.get('event_ref_id'), 'target_act_id': finding.get('target_act_id'),
            'polarity': polarity, 'model_polarity':model_polarity, 'polarity_resolution':polarity_resolution,
            'certainty': finding.get('certainty', 'unknown'),
            'temporal_axis': finding.get('temporal_axis', 'unknown'),
            'conditions': conditions, 'deadline': finding.get('deadline'),
            'model_conditions':model_conditions, 'qualifications':qualifications,
            'condition_scope_resolution':condition_scope_resolution,
            'asserted_at': primary.get('sent_at'), 'effective_at': finding.get('effective_at'),
            'effective_interval': finding.get('effective_interval'),
            'time_resolution_status': finding.get('time_resolution_status',
                                      'asserted_only' if primary.get('sent_at') else 'unknown'),
            'exact_quote': primary['quote'], 'char_start': primary['char_start'],
            'char_end': primary['char_end'], 'citations': citations,
            'evidence_ids': sorted({c['evidence_id'] for c in citations if c.get('evidence_id')}),
            'source_kind': primary.get('source_kind'), 'body_fragments': primary.get('body_fragments', []),
            'sent_at_utc': primary.get('sent_at'), 'attribution': primary['attribution'],
            'attributed_source': finding.get('attributed_source', actor),
            'explicit_or_inferred': finding.get('explicit_or_inferred', 'inferred' if kind in {'hypothesis', 'change'} else 'explicit'),
            'review_status': review, 'semantic_status': review,
            'context_review_status': finding.get('review_status'),
            'semantic_verification': verification, 'extraction_version': extraction_version,
            'unresolved_fields': list(finding.get('unresolved_fields') or []),
            'unresolved_questions': list(finding.get('unresolved_questions') or []),
            'contradictions': list(finding.get('contradictions') or []),
            'uncertainty': finding.get('uncertainty', '')}
