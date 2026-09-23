"""Large-only persisted hybrid retrieval, scope-first ranking and honest coverage."""
from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from pathlib import Path

from services.large_qa_index import LargeQAIndex, loads, tokens, plain_text

LOGGER=logging.getLogger(__name__)
_vectors=OrderedDict()
_lock=threading.RLock()


def scope_filter(c, scope):
    rows=c.execute('SELECT path,topic,topic_name FROM documents WHERE sensitive=0').fetchall()
    paths=[]
    for row in rows:
        if not scope.contains_source(row['path']):
            continue
        if scope.kind=='file_type' and not scope.source_paths:
            wanted=str(scope.value).lower().lstrip('.')
            if Path(row['path']).suffix.lower().lstrip('.')!=wanted:
                continue
        if scope.kind=='topic' and scope.value and not scope.source_paths:
            if str(scope.value).casefold() not in (row['topic']+' '+row['topic_name']).casefold():
                continue
        # Unresolved entity/time scopes cannot safely become all-package searches.
        if scope.kind in {'entity','time'} and not scope.source_paths:
            continue
        paths.append(row['path'])
    return set(paths)


def current_paths(store, package_id, candidates):
    """Recheck source membership after index publication, including deletions."""
    output=set()
    if not candidates:
        return output
    with store._connect() as src:
        values=list(candidates)
        for start in range(0,len(values),500):
            batch=values[start:start+500]
            query='SELECT path FROM files WHERE package_id=? AND sensitive=0 AND path IN ({})'.format(','.join('?' for _ in batch))
            output.update(r[0] for r in src.execute(query,[package_id]+batch))
    return output


def vector_search(c,index,query,allowed,meta,query_vector=None,limit=100):
    import numpy as np
    model=meta.get('embedding_model') or ''
    if query_vector is None:
        from services.large_package_semantic import embedding_client
        from services.vllm import VLLMEmbeddingClient
        base=embedding_client()
        if not base or not model or model!=base.model:
            return [],'unavailable_model'
        endpoints=[getattr(base,'primary',base)]
        if getattr(base,'secondary',None) is not None:
            endpoints.append(base.secondary)
        error=None
        encoded=None
        for endpoint in endpoints:
            try:
                client=VLLMEmbeddingClient(endpoint.base_url,model,timeout=4,max_batch_size=1,max_chars=1024,
                                           api_key=getattr(endpoint,'api_key',''),semaphore_timeout=1,retries=0)
                encoded=client.embed(['query: '+query[:1024]])
                break
            except Exception as exc:
                error=exc
        if encoded is None:
            raise error or RuntimeError('No embedding endpoint')
        query_vector=np.asarray(encoded[0],dtype=np.float32)
    query_vector=np.asarray(query_vector,dtype=np.float32)
    if not np.isfinite(query_vector).all() or np.linalg.norm(query_vector)<1e-9:
        return [],'invalid_query_vector'
    # Raw vectors avoid corpus-centre drift; the cache is versioned and bounded.
    key=(str(index.path),meta.get('index_epoch'),meta.get('summary_generation'),model,len(query_vector))
    with _lock:
        cached=_vectors.get(key)
    if cached is None:
        paths=[]; vectors=[]
        for row in c.execute('SELECT path,vector FROM documents WHERE vector IS NOT NULL AND sensitive=0 ORDER BY path'):
            v=np.frombuffer(row['vector'],dtype=np.float32)
            if len(v)!=len(query_vector) or not np.isfinite(v).all() or np.linalg.norm(v)<1e-9:
                continue
            paths.append(row['path']); vectors.append(v)
        if not paths:
            return [],'no_vectors'
        matrix=np.vstack(vectors)
        matrix/=np.linalg.norm(matrix,axis=1,keepdims=True)
        cached=(paths,matrix)
        with _lock:
            _vectors[key]=cached
            while len(_vectors)>2 or sum(x[1].nbytes for x in _vectors.values())>256*1024*1024:
                _vectors.popitem(last=False)
    paths,matrix=cached
    # Exact BLAS search over authorized rows is a measured baseline; no 20k cap.
    indexes=np.array([i for i,p in enumerate(paths) if p in allowed],dtype=np.int64)
    if not len(indexes):
        return [],'exact_scoped'
    scores=matrix[indexes]@(query_vector/np.linalg.norm(query_vector))
    best=np.argsort(-scores,kind='stable')[:limit]
    return [(paths[int(indexes[i])],float(scores[i])) for i in best],'exact_scoped'


def rrf(lists,k=60):
    scores={}
    for values in lists:
        for rank,key in enumerate(dict.fromkeys(values),1):
            scores[key]=scores.get(key,0)+1/(k+rank)
    return scores


def select_material(rows,query,limit=40,global_mode=False):
    remaining=[dict(r) for r in rows]; chosen=[]; topics=set(); bodies=set(); terms_q=set(tokens(query))
    for row in remaining:
        row['_terms']=set(tokens(row['text']))
        row['_redundancy']=0
        row['_rel']=len(row['_terms']&terms_q)/max(1,len(terms_q))
        row['_exception']=bool(re.search(r'例外|争议|但是|除非|however|unless|except',row['text'],re.I))
    while remaining and len(chosen)<limit:
        def score(row):
            return row.get('fusion',0)*20+row['_rel']+.25*(row['kind']=='original')+.2*row['_exception']+(.8 if global_mode and row['topic'] not in topics else 0)-.65*row['_redundancy']
        item=max(remaining,key=score); remaining.remove(item)
        if item['text'] in bodies:
            continue
        bodies.add(item['text']); topics.add(item['topic']); chosen.append(item)
        for other in remaining:
            left,right=other['_terms'],item['_terms']
            other['_redundancy']=max(other['_redundancy'],len(left&right)/max(1,len(left|right)))
    return chosen


def evidence(row,package_id,root):
    location=loads(row['location'],{})
    body=plain_text(row['text'])
    return {'evidence_id':'lpqa:{}:{}'.format(row['id'],row['version'][:16]),
            'source_path':row['path'],'text':body,'original_text':body,
            'location':location,'source_hash':row['version'],'label':'paragraph' if row['kind']=='original' else 'file_summary',
            'evidence_role':'direct_source' if row['kind']=='original' else '文件摘要',
            'evidence_level':'passage' if row['kind']=='original' else 'file',
            'source_package':{'id':package_id,'type':'large','name':Path(root).name},
            'match_type':'hybrid','retrieval_score':row.get('fusion',0),'topic_id':row['topic'],
            'match_reason':'大包持久化检索与主题覆盖选材'}


def original_document(package_id, root, path, full=False, base=None):
    """Serve the indexed original through the existing document-reader contract."""
    index=LargeQAIndex(package_id,base)
    if not index.exists:
        return None
    with index.connect() as c:
        doc=c.execute('SELECT * FROM documents WHERE path=? AND sensitive=0',(path,)).fetchone()
        if not doc or doc['parse_status'] not in {'complete','partial'}:
            return None
        physical=(Path(root)/path).resolve()
        try:
            st=physical.stat()
            if Path(root).resolve() not in physical.parents or (st.st_size,st.st_mtime_ns)!=(doc['size'],doc['mtime']):
                return None
        except OSError:
            return None
        rows=[dict(r) for r in c.execute("SELECT c.*,d.topic FROM chunks c JOIN documents d USING(path) WHERE path=? AND kind='original' ORDER BY ordinal",(path,))]
        if not rows:
            return None
        # When a parser supplies both positioned evidence and full cached text,
        # display the latter once. PDF page records retain their page boundaries.
        full_rows=[r for r in rows if loads(r['location'],{}).get('representation')=='extracted_text']
        parts=[];last_end=0
        for row in full_rows or rows:
            loc=loads(row['location'],{})
            start=int(loc.get('segment_char_start') or 0)
            body=row['text']
            if start and last_end>start:
                body=body[last_end-start:]
            elif parts:
                parts.append('\n\n')
            parts.append(body)
            last_end=int(loc.get('segment_char_end') or len(body))
        body=plain_text(''.join(parts))
        clipped=len(body)>200000
        warnings=[doc['parse_note']] if doc['parse_note'] else []
        if clipped:
            warnings.append('阅读面板显示前 20 万字符；检索仍使用已建立的全部正文索引。')
        title=doc['name'] or Path(path).name
        return {'schema_version':'large-document/2.0','source':{'path':path,'name':title,'physical_path':path},
                'source_reference':{'logical_path':path,'physical_path':path,'location':{}},
                'analysis_level':'parsed','parser':{'name':'large-qa-original','mode':'cached_original','degraded':not bool(doc['parse_complete'])},
                'structure':{'title':title,'headings':[]},'focus':{},'data_profile':None,'data_profiles':[],
                'coverage':{'complete':bool(doc['parse_complete']) and not clipped,'parse_complete':bool(doc['parse_complete']),
                            'semantic_complete':False,'preview_only':False,'display_truncated':clipped},
                'warnings':warnings,'text_preview':body[:5000],'text_content':body[:200000] if full else None,
                'text_char_count':len(body),'evidence':[evidence(r,package_id,root) for r in rows[:100]],'evidence_count':len(rows)}


def retrieve(store,request,base=None,query_vector=None):
    index=LargeQAIndex(request.scan_id,base)
    if not index.exists:
        return None
    package=store.get(request.scan_id)
    if not package:
        raise ValueError('大数据包不存在')
    query=request.query
    overview=bool(re.search(r'整体介绍|总体介绍|概括|概览|综述|主要(?:内容|主题|方向)|资料.{0,16}(?:研究什么|讲什么)',query))
    global_mode=(request.intent in {'summary','analysis','multi_task'} or overview) and not re.search(r'为什么|多少|付款|预算|第\d+|编号',query)
    warnings=[]
    with index.connect() as c:
        meta=index.meta(c)
        if not meta.get('synced_at'):
            return None
        allowed=current_paths(store,request.scan_id,scope_filter(c,request.scope))
        global_mode=global_mode and len(allowed)>1
        # Store allowed membership in a TEMP table without mutating the persistent DB.
        # query_only disallows temp writes too; use bound IN clauses in manageable batches.
        lexical=[]; lexical_seen=set()
        terms=list(dict.fromkeys(tokens(query)))[:48]
        if terms and allowed:
            expression=' OR '.join('"'+t.replace('"','""')+'"' for t in terms)
            # Authorization is included before LIMIT, so a tiny scope cannot be
            # drowned out by higher scoring results from another directory.
            paths=list(allowed)
            for start in range(0,len(paths),500):
                batch=paths[start:start+500]
                sql='''SELECT c.id,bm25(search,2.0,1.0) rank FROM search JOIN chunks c ON c.id=search.rowid
                       WHERE search MATCH ? AND c.path IN ({}) ORDER BY rank LIMIT 100'''.format(','.join('?' for _ in batch))
                lexical.extend((r['id'],r['rank']) for r in c.execute(sql,[expression]+batch))
            lexical.sort(key=lambda x:(x[1],x[0]))
            lexical=[x for x in lexical if not (x[0] in lexical_seen or lexical_seen.add(x[0]))][:150]
        try:
            vectors,backend=vector_search(c,index,query,allowed,meta,query_vector)
        except Exception as exc:
            vectors=[];backend='unavailable'; warnings.append('语义检索暂不可用，本轮使用关键词和主题材料。')
            LOGGER.info('large QA vector unavailable: %s',str(exc)[:120])
        rows={}
        def get_chunk(chunk_id):
            row=c.execute('SELECT c.*,d.topic,d.topic_name,d.size,d.mtime FROM chunks c JOIN documents d USING(path) WHERE c.id=?',(chunk_id,)).fetchone()
            if row:
                rows[chunk_id]=dict(row)
        for chunk_id,_ in lexical:
            get_chunk(chunk_id)
        vector_ids=[]
        for path,_ in vectors:
            candidates=c.execute('SELECT c.*,d.topic,d.topic_name,d.size,d.mtime FROM chunks c JOIN documents d USING(path) WHERE path=? ORDER BY kind,ordinal',(path,)).fetchall()
            # Full cached file text is indexed; choose query-relevant windows,
            # not merely the first paragraph of a semantic file hit.
            q=set(terms)
            candidates=sorted(candidates,key=lambda r:len(q&set(tokens(r['text']))),reverse=True)[:3]
            for row in candidates:
                vector_ids.append(row['id']);rows[row['id']]=dict(row)
        topics=index.topic_material(c,allowed) if global_mode else []
        topic_ids=[]
        if global_mode:
            for topic in topics:
                paths=[]
                for report in topic['reports']:
                    for point in loads(report['text'],{}).get('points',[])[:4]:
                        paths.extend(point['sources'])
                if not paths:
                    paths=[r[0] for r in c.execute('SELECT path FROM documents WHERE topic=? AND summary!="" ORDER BY path',(topic['topic'],)) if r[0] in allowed][:2]
                for path in dict.fromkeys(paths):
                    if path not in allowed:
                        continue
                    row=c.execute('SELECT c.*,d.topic,d.topic_name,d.size,d.mtime FROM chunks c JOIN documents d USING(path) WHERE path=? ORDER BY kind,ordinal LIMIT 1',(path,)).fetchone()
                    if row:
                        rows[row['id']]=dict(row);topic_ids.append(row['id'])
        scores=rrf([[x[0] for x in lexical],vector_ids,topic_ids])
        for key,row in rows.items():
            row['fusion']=scores.get(key,0)
        # Physical changes invalidate both original excerpts and old summaries.
        fresh=[]
        for row in rows.values():
            path=(Path(package['root_path'])/row['path']).resolve()
            try:
                stat=path.stat()
                if Path(package['root_path']).resolve() not in path.parents:
                    continue
                if (stat.st_size,stat.st_mtime_ns)!=(row['size'],row['mtime']):
                    continue
            except OSError:
                continue
            fresh.append(row)
        chosen=select_material(fresh,query,50 if global_mode else max(10,min(40,request.top_k)),global_mode)
        counts={'total':len(allowed),'summaries':0,'originals':0,'partial':0}
        for row in c.execute('SELECT path,summary!="" summary,parse_complete,parse_status FROM documents'):
            if row['path'] in allowed:
                counts['summaries']+=bool(row['summary']);counts['originals']+=bool(row['parse_complete']);counts['partial']+=row['parse_status']=='partial'
        reports=[]
        for topic in topics:
            points=[p for n in topic['reports'] for p in loads(n['text'],{}).get('points',[])]
            reports.append({'topic_id':topic['topic'],'name':topic['topic_name'],'files':topic['files'],
                            'input_files':len(topic['covered_paths']),'points':points[:24]})
        coverage={'scope':request.scope.kind,'total_files':counts['total'],'scope_files':counts['total'],
                  'candidate_files':len({r['path'] for r in fresh}),'inspected_files':len({r['path'] for r in chosen}),
                  'query_coverage':None,'coverage_basis':'versioned_index_not_recall_estimate',
                  'summary_backed_files':counts['summaries'],'original_indexed_files':counts['originals'],
                  'partial_original_files':counts['partial'],'package_reading_complete':False,
                  'topic_total':len(topics),'selected_topics':len({r['topic'] for r in chosen}),
                  'index_generation':meta.get('generation'),'index_synced_at':meta.get('synced_at')}
        if counts['originals']<counts['total']:
            warnings.append('当前范围 {} 份文件中，{} 份已完成正文索引，其余依据已有摘要或部分正文。'.format(counts['total'],counts['originals']))
        if global_mode and coverage['selected_topics']<len(topics):
            warnings.append('本轮具体证据涉及 {} / {} 个主题，主题概览不等于逐份全文阅读。'.format(coverage['selected_topics'],len(topics)))
        result={'results':[{**evidence(r,request.scan_id,package['root_path']),'large_global':global_mode} for r in chosen],
                'index_mode':'large-qa-v1','retrieval_mode':'large_qa','warnings':warnings,'coverage':coverage,
                'candidate_paths':list(dict.fromkeys(r['path'] for r in chosen)),
                'package_context':{'package_type':'large','package_id':request.scan_id,'coverage':coverage,
                                   'large_qa':{'global_mode':global_mode,'topics':reports,'generation':meta.get('generation'),'summary_generation':meta.get('summary_generation')},
                                   'file_summaries':[],'reading_manifest':{'files_read':counts['total'],'summary_backed_files':counts['summaries'],
                                   'files_without_summary':counts['total']-counts['summaries'],
                                   'summary_coverage':counts['summaries']/max(1,counts['total']),
                                   'complete':counts['summaries']==counts['total'],'basis':'summary_index_only'}},
                'retrieval_stats':{'keyword_candidates':len(lexical),'semantic_files':len(vectors),'vector_backend':backend,
                                   'selected_evidence':len(chosen),'scope_files':len(allowed)}}
    try:
        index.enqueue([r['path'] for r in chosen])
    except Exception:
        LOGGER.warning('Could not prioritize large QA extraction',exc_info=True)
    return result
