"""Conservative source-qualifier preservation, not a general semantic verifier.

Only recognized status-strength and exact-action authorization mismatches are
rewritten. No source text is changed; every change retains its original wording.
Budgets never silently truncate sources, because a late record may contradict
an earlier observation. No model call, I/O, or current-time inference.
"""
from __future__ import annotations
import copy
import hashlib
import json
import re
from services.grounded_clauses import status_strength_mismatch

SUMMARY_QUALIFIER_INSTRUCTION = (
    ' 保留原文的确定程度：未确认完成不等于已完成，也不等于尚未完成；'
    '未授权不等于明令禁止，计划不等于已实施。'
    '区分事实、他人主张、条件和建议。历史资料保留原始日期和当时状态，'
    '不得把过去计划改写成相对当前的明天、下周或未来几天，不推断现在进度。'
)
_CLAUSE = re.compile(r'[^。！？!?；;，,\n]+')
_HEAD = re.compile(r'^\s*(?:\*\*)?(?:主题|关键事实|风险|后续建议|结论|事实)(?:\*\*)?[：:]\s*')
_STATUS = re.compile(r'(?:尚未|还未|并未|没有|已经|未|已)(?:完成(?P<operation>通电联合调试|联合调试|通电调试|书面验收|最终验收|调试|验收|修复)|(?P<repair>修复|修好))$')
_BAN = re.compile(r'^(?P<prefix>合同|文件|条款|原文)?(?:禁止|严禁)(?P<action>.+)$')
_UNAUTHORIZED = re.compile(r'(?:尚未|并未|未曾|未|没有|未经)授权(?P<tail>.*)')
_QUOTED = re.compile(r'[“”「」"‘’]')
_NON_ASSERTION = re.compile(r'如果|假设|假如|例如|建议|声称|据称|是否|并非|不是|不代表|不等于|不能说明|无法证明')
_FIELDS = ('core_summary','summary','conclusions','key_facts','arguments','methodology')

def summary_qualifier_patch(summary, evidence_items):
    """Return only changed fields and an explicit bounded-check diagnostic.

    Callers merge this before constructing their unchanged claim/evidence UI
    contracts. Mixed files or oversized sources are not rewritten.
    """
    items = [x for x in evidence_items or [] if isinstance(x, dict)]
    audit = {'policy':'summary-qualifiers/1.0','full_semantic_validation':False,
             'status':'checked','changes':[]}
    patch = {'summary_fidelity':audit}
    paths = {str(x.get('source_path') or '') for x in items}
    bodies = [str(x.get('original_text') or x.get('text') or '') for x in items]
    if not items or len(paths) != 1 or not all(bodies):
        audit.update(status='not_checked',reason='missing_or_mixed_source')
        return patch
    if len(items)>1000 or sum(map(len,bodies))>200000:
        audit.update(status='not_checked',reason='source_budget')
        return patch
    # Headings, hypothetical examples and quotations are not silently converted
    # into factual assertions. Unknown grammar stays with existing verification.
    corpus=chr(10).join(bodies)
    source_clauses=[(item, m.group()) for item,body in zip(items,bodies) for m in _CLAUSE.finditer(body)]
    def digest(value):
        return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode('utf-8')).hexdigest()
    audit['source_digest']=digest([(x.get('evidence_id'),x.get('source_path'),body) for x,body in zip(items,bodies)])
    statement_changes={}

    def change_text(value, field):
        if not isinstance(value,str) or _QUOTED.search(value):return value
        changes=[]
        protected=[(m.start(),m.end()) for m in re.finditer(r'[^。！？!?；;\n]+',value) if _NON_ASSERTION.search(m.group())]
        def replace(match):
            if any(a<=match.start()<b for a,b in protected):return match.group()
            raw=match.group(); head=_HEAD.match(raw)
            prefix=raw[:head.end()] if head else raw[:len(raw)-len(raw.lstrip())]
            body=raw[len(prefix):]; stripped=body.strip(); trailing=body[len(body.rstrip()):]
            if not stripped or _NON_ASSERTION.search(stripped):return raw
            replacement=None; supports=[]; reason=None
            status=_STATUS.search(stripped)
            if status and not _QUOTED.search(corpus) and status_strength_mismatch(stripped,corpus):
                supports=[item for item,text in zip(items,bodies) if status_strength_mismatch(stripped,text)]
                # This does not infer whether completion actually happened.
                operation=status['operation'] or '修复'
                replacement=stripped[:status.start()]+operation+'状态未获原文确认'
                reason='status_strength'
            ban=_BAN.fullmatch(stripped)
            if replacement is None and ban and len(ban['action'])>=4:
                action=ban['action']
                explicit=False
                for item,clause in source_clauses:
                    if action not in clause:continue
                    if re.search(r'(?:禁止|严禁|不得)'+re.escape(action),clause):explicit=True
                    grant=_UNAUTHORIZED.search(clause)
                    if (grant and grant['tail'].endswith(action)
                            and not _NON_ASSERTION.search(clause) and not _QUOTED.search(clause)):
                        supports.append(item)
                if supports and not explicit:
                    replacement=(ban['prefix'] or '原文')+'未授权'+action
                    reason='authorization_strength'
            if replacement is None or not supports:return raw
            replacement=prefix+replacement+trailing
            changes.append({'field':field,'reason':reason,'original':raw,'replacement':replacement,
                'evidence_ids':list(dict.fromkeys(str(x.get('evidence_id')) for x in supports if x.get('evidence_id')))})
            return replacement
        result=_CLAUSE.sub(replace,value)
        audit['changes'].extend(changes)
        if result!=value:statement_changes[value]=result
        return result

    for field in _FIELDS:
        value=summary.get(field)
        if isinstance(value,str):updated=change_text(value,field)
        elif isinstance(value,list):
            updated=[]
            for index,part in enumerate(value):
                if isinstance(part,dict):
                    item=copy.deepcopy(part)
                    for key in ('claim','text','statement'):
                        if isinstance(part.get(key),str):item[key]=change_text(part[key],field+'.'+str(index)+'.'+key)
                    updated.append(item)
                else:updated.append(change_text(part,field+'.'+str(index)))
        else:continue
        if updated!=value:patch[field]=updated
    if audit['changes']:
        audit['status']='qualified'
        audit['statement_changes']=[{'original':old,'replacement':new} for old,new in statement_changes.items()]
        # Preserve declared source IDs only when the exact original statement was
        # corrected; never let lexical fallback manufacture a replacement label.
        if isinstance(summary.get('claim_evidence'),list):
            citations=copy.deepcopy(summary['claim_evidence'])
            for citation in citations:
                if isinstance(citation,dict) and citation.get('statement') in statement_changes:
                    citation['statement']=statement_changes[citation['statement']]
            if citations!=summary['claim_evidence']:patch['claim_evidence']=citations
    fields=_FIELDS+('claim_evidence',)
    audit['output_digest']=digest({k:patch.get(k,summary.get(k)) for k in fields})
    previous=summary.get('summary_fidelity') or {}
    if (not audit['changes'] and isinstance(previous,dict)
            and previous.get('policy')==audit['policy']
            and previous.get('source_digest')==audit['source_digest']
            and previous.get('output_digest')==audit['output_digest']):
        patch['summary_fidelity']=copy.deepcopy(previous)
    return patch
