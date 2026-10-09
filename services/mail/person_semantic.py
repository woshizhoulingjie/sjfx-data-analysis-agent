"""Immutable model references, expanded before the existing semantic gates."""
import copy
import re

REFERENCE_NOTE = '''
OUTPUT PROTOCOL: each atom has text (an exact contiguous claim_text substring),
status, source_refs [{ref_id, role: support/counter}], missing_requirements.
Choose source_refs ONLY from source_reference_spans. Each span selects immutable
original text in evidence.quote_text; offsets are relative to that text. You may
select several spans or the complete-source span. Read COMPLETE sources and MIME
fragments before judging. Never emit source_quotes, offsets, support_evidence_ids
or counter_evidence_ids: the backend expands chosen references exactly and runs
the original attribution/date/condition gates. A reference does not imply support.
Each qualification_review has check_id, applicability, rationale, choosing EVERY
check_id assigned to this claim in qualification_checks exactly once. Do not copy
its text or coordinates. Keep genuine contradictions and insufficient verdicts.
The guided output uses qualification_reviews as an object keyed by this claim's
check_ids; each value has applicability and rationale. Empty checks use {}.
Each claim's allowed_source_refs lists ONLY its original sources. References
from a different claim, even in this page, are forbidden.
'''


def reference_payload(payload):
    """The registry covers all original text; no model-generated quote is trusted."""
    result=copy.deepcopy(payload); registry={}; spans=[]; checks={}
    for source in result.get('evidence', []):
        text=source.get('quote_text') or ''
        if not text:continue
        ranges=[(0,len(text))]
        # Complete contiguous paragraphs/sentences, plus a full-source option.
        # Splitting creates navigation spans, never alters or discards characters.
        ranges.extend((m.start(),m.end()) for m in re.finditer(r'[^\n。！？!?]+[。！？!?]?|\n+',text) if m.group().strip())
        for start,end in dict.fromkeys(ranges):
            ref='s'+str(len(registry)+1)
            registry[ref]={'evidence_id':source['evidence_id'],'quote':text[start:end],
                           'char_start':int(source.get('char_start') or 0)+start}
            spans.append({'ref_id':ref,'evidence_id':source['evidence_id'],
                          'start':start,'end':end,'complete_source':start==0 and end==len(text)})
    for claim_id,rows in result.get('qualification_checks',{}).items():
        mapped=[]
        for row in rows:
            key='q'+str(len(checks)+1);checks[key]=(claim_id,copy.deepcopy(row))
            mapped.append({'check_id':key,**row})
        result['qualification_checks'][claim_id]=mapped
    result['source_reference_spans']=spans
    for claim in result.get('claims',[]):
        allowed=set(claim.get('evidence_ids',[])+claim.get('counter_evidence_ids',[]))
        claim['allowed_source_refs']=[key for key,row in registry.items() if row['evidence_id'] in allowed]
    # Repairs retain decisions but do not ask the model to recopy expanded quotes.
    if isinstance(result.get('previous_verdicts'),dict):
        for verdict in result['previous_verdicts'].get('verdicts',[]):
            for atom in verdict.get('atoms',[]):
                atom.pop('source_quotes',None)
    return result,registry,checks


def expand_reference_verdicts(value,registry,checks):
    """Unknown/conflicting IDs fail closed; original validators still decide truth."""
    result=copy.deepcopy(value)
    if not isinstance(result,dict):return result
    for verdict in result.get('verdicts',[]):
        if not isinstance(verdict,dict):continue
        for atom in verdict.get('atoms',[]):
            if not isinstance(atom,dict) or 'source_refs' not in atom:continue
            support=[];counter=[];quotes=[]
            for item in atom['source_refs']:
                if not isinstance(item,dict) or item.get('ref_id') not in registry or item.get('role') not in {'support','counter'}:
                    raise ValueError('语义核查使用未知或无效原文编号')
                quote=registry[item['ref_id']]
                if any(k in item and item[k]!=v for k,v in quote.items()):
                    raise ValueError('语义核查原文编号与附带引用冲突')
                ids=support if item['role']=='support' else counter
                if quote['evidence_id'] not in ids:ids.append(quote['evidence_id'])
                if quote not in quotes:quotes.append(copy.deepcopy(quote))
            expanded={'support_evidence_ids':support,'counter_evidence_ids':counter,'source_quotes':quotes}
            if any(k in atom and atom[k]!=v for k,v in expanded.items()):
                raise ValueError('语义核查固定原文与生成引用冲突')
            atom.update(expanded)
        reviews=verdict.get('qualification_reviews',[])
        if isinstance(reviews,dict):
            if any(not isinstance(v,dict) for v in reviews.values()):raise ValueError('语义核查条件判断结构无效')
            reviews=[{'check_id':key,**v} for key,v in reviews.items()]
            verdict['qualification_reviews']=reviews
        for review in reviews:
            if not isinstance(review,dict) or 'check_id' not in review:continue
            key=review['check_id']
            if key not in checks or checks[key][0]!=verdict.get('claim_id'):
                raise ValueError('语义核查条件编号不属于当前判断')
            required=checks[key][1]
            if any(k in review and review[k]!=v for k,v in required.items()):
                raise ValueError('语义核查条件编号与原文冲突')
            review.update(copy.deepcopy(required))
    return result


def reference_schema(payload,registry,checks):
    strings={'type':'array','items':{'type':'string'}}
    variants=[]
    for claim in payload['claims']:
        cid=claim['claim_id'];allowed=set(claim.get('evidence_ids',[])+claim.get('counter_evidence_ids',[]))
        ids=[key for key,row in registry.items() if row['evidence_id'] in allowed]
        ref={'type':'object','properties':{'ref_id':{'type':'string','enum':ids},
              'role':{'type':'string','enum':['support','counter']}},'required':['ref_id','role'],'additionalProperties':False}
        atom={'type':'object','properties':{'text':{'type':'string'},'status':{'type':'string','enum':['supported','contradicted','insufficient']},
              'source_refs':{'type':'array','items':ref},'missing_requirements':strings},
              'required':['text','status','source_refs','missing_requirements'],'additionalProperties':False}
        review={'type':'object','properties':{'applicability':{'type':'string','enum':['applicable','not_applicable','unresolved']},'rationale':{'type':'string'}},
              'required':['applicability','rationale'],'additionalProperties':False}
        keys=[key for key,(owner,_) in checks.items() if owner==cid]
        qualification={'type':'object','properties':{key:review for key in keys},'required':keys,'additionalProperties':False}
        variants.append({'type':'object','properties':{'claim_id':{'type':'string','enum':[cid]},
              'atoms':{'type':'array','items':atom,'minItems':1,'maxItems':24},'qualification_reviews':qualification},
              'required':['claim_id','atoms','qualification_reviews'],'additionalProperties':False})
    return {'type':'object','properties':{'verdicts':{'type':'array','items':{'oneOf':variants}}},'required':['verdicts'],'additionalProperties':False}
