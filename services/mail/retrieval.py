"""Rank fusion keeps lexical exact signals and exposes incomplete vector coverage."""
from __future__ import annotations
import re
from config import Config


def rrf_fuse(rankings,k=60):
    found={}
    for name,items in rankings:
        seen=set()
        for rank,item in enumerate(items,1):
            key=item.get('segment_id') or (item['message_key'],item['mime_path'],item.get('char_start'))
            if key in seen:continue
            seen.add(key)
            if key not in found:found[key]={**item,'retrieval':{},'fusion_score':0.0}
            row=found[key];row['fusion_score']+=1/(k+rank)
            row['retrieval'][name]={'rank':rank,'score':item.get(name+'_score')}
    return sorted(found.values(),key=lambda r:-r['fusion_score'])


def hybrid_search(repository,package_id,owner_id,question,*,limit=50,attachment_only=False):
    lexical=repository.search_segments_lexical(package_id,owner_id,question,attachment_only=attachment_only,limit=max(limit,200))
    if not Config.MAIL_ENABLE_VECTOR_INDEX:return lexical if limit>=200 else {**lexical,'items':lexical['items'][:limit]}
    anchors=re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?\s*(?:THB|USD|CNY|RMB|บาท|泰铢)|[A-Za-z0-9_-]+\.(?:pdf|docx?|xlsx?|eml)",str(question),re.I)
    if anchors:
        precise=[]
        for anchor in anchors[:3]:precise.extend(repository.search_segments_lexical(package_id,owner_id,anchor,attachment_only=attachment_only,limit=50)["items"])
        seen={r["segment_id"] for r in precise}
        lexical["items"]=precise+[r for r in lexical["items"] if r["segment_id"] not in seen]
    from .semantic import vector_search
    try:
        vector,meta=vector_search(repository,package_id,owner_id,question,limit=200,attachment_only=attachment_only)
    except Exception as exc:
        vector,meta=[],{'status':'unavailable','reason':str(exc)[:200]}
    fused=rrf_fuse([('lexical',lexical['items']),('vector',vector)],Config.MAIL_RRF_K)
    # Numbers, references and filenames retain exact lexical matches ahead of semantic candidates.
    if re.search(r'\d|\.[A-Za-z]{2,5}\b',str(question)):
        protected=lexical['items'][:5];keys={r['segment_id'] for r in protected}
        indexed={r['segment_id']:r for r in fused}
        fused=[indexed.get(r['segment_id'],r) for r in protected]+[r for r in fused if r['segment_id'] not in keys]
    for r in fused[:limit]:
        if not r.get('evidence_id'):
            evidence=repository.create_evidence(package_id,owner_id,r)
            r.update(evidence_id=evidence['evidence_id'],quote_text=evidence['quote_text'])
    return {'items':fused[:limit],'coverage':lexical['coverage'],'retrieval':{'fusion':'rrf','lexical':len(lexical['items']),'vector':len(vector),'vector_index':meta}}
