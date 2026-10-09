"""Fair issue/actor/role context allocation; the source ledger is never truncated."""
from __future__ import annotations

from collections import defaultdict, deque
import json
import re
from .token_budget import estimate_tokens

VERSION = 'mail-evidence-packing-v1'
ROLES = ('counter', 'condition', 'update', 'support')


def evidence_role(source):
    role = source.get('role') or source.get('evidence_role')
    if role in ROLES:
        return role
    text = str(source.get('quote_text') or source.get('text') or '')
    if re.search(r'撤回|拒绝|取消|不同意|尚未|withdraw|reject|not approved|ไม่อนุมัติ', text, re.I):
        return 'counter'
    if re.search(r'如果|除非|审批|条件|若|subject to|provided that|\bif\b|pending approval|หาก', text, re.I):
        return 'condition'
    if re.search(r'修改|更新|修订|变更|revise|amend|update|แก้ไข', text, re.I):
        return 'update'
    return 'support'


def pack_evidence(evidence, max_tokens, *, overhead_tokens=0, max_chars=None):
    capacity = max(0, int(max_tokens) - int(overhead_tokens))
    groups, seen = defaultdict(deque), set()
    omitted, selected, allocations = [], [], []
    for source in evidence:
        key = source.get('evidence_id') or source.get('chunk_id')
        if not key or key in seen:
            continue
        seen.add(key)
        issue = str(source.get('issue_id') or source.get('object_ref') or source.get('message_key') or 'unresolved')
        actor = str(source.get('actor') or source.get('speaker') or 'unknown')
        groups[(issue, actor, evidence_role(source))].append(source)
    # Interleave issues first, then each actor and protected role. Low frequency
    # dissent competes for a seat before a long run of repeated support.
    keys = sorted(groups, key=lambda k: (ROLES.index(k[2]), k[0], k[1]))
    used_tokens, used_chars = 0, 0
    while any(groups.values()):
        for key in keys:
            if not groups[key]:
                continue
            source = groups[key].popleft()
            serialized = json.dumps(source, ensure_ascii=False, default=str)
            tokens, chars = estimate_tokens(serialized), len(serialized)
            if used_tokens + tokens > capacity or (max_chars is not None and used_chars + chars > max_chars):
                omitted.append({'evidence_id': source.get('evidence_id') or source.get('chunk_id'),
                                'issue_id': key[0], 'actor': key[1], 'role': key[2],
                                'reason': 'complete_source_exceeds_context_budget', 'estimated_tokens': tokens})
                continue
            selected.append(source)
            used_tokens += tokens
            used_chars += chars
            allocations.append({'evidence_id': source.get('evidence_id') or source.get('chunk_id'),
                                'issue_id': key[0], 'actor': key[1], 'role': key[2], 'estimated_tokens': tokens})
    return selected, {'version': VERSION, 'source_total': len(seen), 'included': len(selected),
                      'omitted': omitted, 'allocations': allocations, 'estimated_tokens': used_tokens,
                      'max_tokens': int(max_tokens), 'estimate_method': 'utf8_bytes_div3_v1',
                      'ledger_complete': True, 'synthesis_complete': not omitted}


def model_source(source, *, compact=False):
    """Project provenance without removing literal content or role distinctions."""
    fields=('evidence_id','message_key','mime_path','page_number','sheet_name','cell_range','source_kind',
            'sent_at_utc','char_start','char_end','attribution','attribution_version','attribution_uncertain',
            'quotation_boundary','quoted_ranges','quote_text',
            'issue_id','act_id','actor','speaker','role','evidence_role','conditions','object_version',
            'package_id','source_revision','index_epoch','source_sha256','payload_sha256','text_sha256',
            'occurrence_id','source_aliases','text_origin','extraction_method','source_text_notice','ocr_quality')
    result={k:source[k] for k in fields if k in source}
    if isinstance(result.get('ocr_quality'),dict):
        result['ocr_quality']={k:result['ocr_quality'][k] for k in
            ('engine','confidence_available','line_count','scored_lines','minimum_confidence',
             'mean_confidence','recognition_accuracy','confidence_basis') if k in result['ocr_quality']}
    result['quote_text']=str(source.get('quote_text') or source.get('text') or '')
    result['body_fragments']=[{k:f[k] for k in ('mime_path','fragment_kind','char_start','char_end','method','confidence') if k in f}
                              for f in source.get('body_fragments') or []]
    result['participants']=[{k:p[k] for k in ('role','address','normalized_address','display_name','display_name_sample') if k in p}
                            for p in source.get('participants') or []]
    if compact:
        # Exact source text, offsets, authorship and semantic distinctions stay
        # in the prompt. Hashes and retrieval aliases belong to the full server
        # ledger used for binding, deduplication and citation resolution.
        for key in ('package_id','source_sha256','payload_sha256','text_sha256','source_aliases'):
            result.pop(key,None)
        start=source.get('char_start')
        if isinstance(start,int):
            end=start+len(result['quote_text'])
            result['body_fragments']=[f for f in result['body_fragments']
                if not isinstance(f.get('char_start'),int) or not isinstance(f.get('char_end'),int)
                or (f['char_start']<end and f['char_end']>start)]
        result={k:v for k,v in result.items() if v is not None and v!=[] and v!=''}
        result['quote_text']=str(source.get('quote_text') or source.get('text') or '')
    return result


def pack_evidence_batches(evidence,max_tokens,*,overhead_tokens=0,max_chars=None):
    """Allocate every complete source once; oversize sources remain explicit gaps."""
    remaining=list(evidence);batches=[];allocations=[];oversized=[]
    while remaining:
        batch,manifest=pack_evidence(remaining,max_tokens,overhead_tokens=overhead_tokens,max_chars=max_chars)
        if not batch:
            oversized.extend(manifest['omitted']);break
        batches.append(batch);allocations.extend(manifest['allocations'])
        included={r.get('evidence_id') or r.get('chunk_id') for r in batch}
        remaining=[r for r in remaining if (r.get('evidence_id') or r.get('chunk_id')) not in included]
    return batches,{'version':VERSION+'-bounded-batches','source_total':len(evidence),
                    'included':sum(len(b) for b in batches),'batch_count':len(batches),
                    'batch_evidence_ids':[[r.get('evidence_id') or r.get('chunk_id') for r in b] for b in batches],
                    'omitted':oversized,'allocations':allocations,'ledger_complete':True,
                    'synthesis_complete':not oversized,'max_tokens':int(max_tokens)}


def analysis_output_tokens(payload):
    """Estimate the complete bounded extraction schema, including literal quotes."""
    from .output_budget import complete_output_reservation
    evidence=payload.get('evidence') or []
    rows=[];findings=[]
    # One complete source can support several distinct claims/findings.
    # Reserve the full response contract, rather than one row per source.
    for i in range(8 if evidence else 0):
        source=evidence[i%len(evidence)];quote=str(source.get('quote_text') or '')[:400]
        rows.append({'claim_text':'陈'*240,'subject_ref':'主'*60,'predicate':'confirmed','object_ref':'事'*80,
            'state':'asserted','evidence_ids':[source.get('evidence_id')], 'actor':'mailbox@example.com',
            'source_quote':quote,'conditions':['条'*120], 'time_basis':source.get('sent_at_utc'), 'uncertainty':'核'*120})
    for i in range(6 if evidence else 0):
        source=evidence[i%len(evidence)]
        findings.append({'text_zh':'发'*240,'evidence_id':source.get('evidence_id'),
            'source_quote':str(source.get('quote_text') or '')[:400],'object_ref':'事'*80,'actor':'mailbox@example.com'})
    return complete_output_reservation({'answer':'答'*600,'claims':rows,'findings':findings},
        minimum=2400,previous=payload.get('previous_output'))


def answer_output_tokens(payload):
    """Reserve four complete answer atoms; unresolved questions are not facts."""
    from .output_budget import complete_output_reservation
    evidence=payload.get('evidence') or []
    rows=[]
    for index in range(4 if evidence else 0):
        source=evidence[index%len(evidence)]
        if source.get('source_ref'):
            block=(payload.get('source_blocks') or {}).get(source['source_ref'],{})
            documents=payload.get('source_documents') or {}
            source={**documents.get('_defaults',{}),**documents.get(block.get('document_ref'),{}),**block,**source}
        rows.append({'claim_text':'答'*240,'actor':'mailbox@example.com',
            'evidence_ids':[source.get('evidence_id')],'source_quote':str(source.get('quote_text') or '')[:400],
            'object_ref':'事'*80,'conditions':['条'*160]})
    return complete_output_reservation({'claims':rows,'unanswered_subquestions':['问'*120]*4},minimum=2400)


def answer_repair_output_tokens(payload):
    """Reserve each complete bounded candidate proposal, including exact quotes."""
    from .output_budget import complete_output_reservation
    rows=[{'atom_index':row.get('atom_index'),'claim_text':'答'*240,'object_ref':'事'*80,
        'evidence_ids':[(source.get('evidence_id')) for source in payload.get('evidence') or []],
        'source_quote':'引'*400,'actor':'a'*320} for row in payload.get('format_repairs') or []]
    return complete_output_reservation({'repairs':rows},minimum=1600)


def bundle_sources(sources):
    """Share literal spans, never semantic source identities or audit bindings."""
    groups={};projected=[]
    boundaries=('message_key','mime_path','source_kind','char_start','char_end','page_number','sheet_name','cell_range',
        'source_revision','index_epoch','source_sha256','payload_sha256','text_sha256','attribution',
        'attribution_version','attribution_uncertain','quotation_boundary','quoted_ranges','body_fragments')
    separate={'evidence_id','issue_id','act_id','actor','speaker','role','evidence_role','conditions','object_version'}
    for source in sources:
        item=model_source(source,compact=True);projected.append(item)
        identity=json.dumps([item['quote_text'],{k:source.get(k) for k in boundaries}],ensure_ascii=False,sort_keys=True,default=str)
        groups.setdefault(identity,[]).append(item)
    blocks={};records=[]
    for index,items in enumerate(groups.values()):
        ref='s'+str(index+1)
        common=set(items[0])-separate
        for item in items[1:]:common={k for k in common if k in item and item[k]==items[0][k]}
        block={k:items[0][k] for k in sorted(common)}
        blocks[ref]=block
        records.extend({'source_ref':ref,**{k:v for k,v in item.items() if k not in common}} for item in items)
    # Header identity is shared only when every field is exactly equal. MIME,
    # offsets, author boundaries, semantic roles and conditions stay per span.
    documents={};document_ids={}
    fields=('message_key','participants','sent_at_utc','source_revision','index_epoch')
    for block in blocks.values():
        shared={k:block[k] for k in fields if k in block}
        identity=json.dumps(shared,ensure_ascii=False,sort_keys=True,default=str)
        if identity not in document_ids:
            ref='d'+str(len(documents)+1);document_ids[identity]=ref;documents[ref]=shared
        block['document_ref']=document_ids[identity]
        for key in shared:block.pop(key)
    # Global defaults apply only to fields present and exactly equal in EVERY
    # document. Missing metadata or a different revision never inherits a value.
    defaults={k:next(iter(documents.values()))[k] for k in ('source_revision','index_epoch')
        if documents and all(k in doc for doc in documents.values())
        and all(json.dumps(doc[k],ensure_ascii=False,sort_keys=True,default=str)==
                json.dumps(next(iter(documents.values()))[k],ensure_ascii=False,sort_keys=True,default=str)
                for doc in documents.values())}
    if defaults and len(documents)>1:
        for doc in documents.values():
            for key in defaults:doc.pop(key)
        documents['_defaults']=defaults
    # Verify exact reconstruction in original ID space, before any model call.
    expected={item['evidence_id']:item for item in projected}
    resolved={item['evidence_id']:{**documents.get('_defaults',{}),**documents[blocks[item['source_ref']]['document_ref']],
        **{k:v for k,v in blocks[item['source_ref']].items() if k!='document_ref'},
        **{k:v for k,v in item.items() if k!='source_ref'}} for item in records}
    if len(expected)!=len(projected) or resolved!=expected:
        raise ValueError('来源映射未保留每个证据ID的精确原文和语义元数据')
    return records,blocks,documents
