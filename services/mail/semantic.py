"""Package-scoped E5 encoding, adapted from large_package_semantic without modifying it."""
from __future__ import annotations
import hashlib
import json
import time
import numpy as np
from config import Config
from services.embedding_encoding import prefix as embedding_prefix, tokenizer as selected_embedding_tokenizer, encoding as embedding_encoding

_projections = {}
_tokenizer = None
_tokenizer_checked = False


def embedding_client():
    from services.embedding_backend import create_embedding_client
    return create_embedding_client(Config, lane='mail', max_batch_size=Config.MAIL_EMBED_BATCH)


def normalize_vector(value):
    vector = np.asarray(value, dtype=np.float32)
    if vector.ndim != 1 or not len(vector) or not np.isfinite(vector).all() or np.linalg.norm(vector) < 1e-8:
        raise ValueError('无效语义向量')
    return vector / np.linalg.norm(vector)


def lsh_buckets(vector):
    dimension = len(vector)
    if dimension not in _projections:
        _projections[dimension] = np.random.default_rng(719).standard_normal((6,10,dimension)).astype(np.float32)
    signs = np.einsum('bkd,d->bk', _projections[dimension], vector) > 0
    return [int(sum(1 << i for i, bit in enumerate(row) if bit)) for row in signs]


def _window(text, prefix, headings=''):
    global _tokenizer, _tokenizer_checked
    _tokenizer = selected_embedding_tokenizer(Config)
    _tokenizer_checked = True
    import html
    text = html.unescape(str(text or ''))
    if _tokenizer:
        ids = _tokenizer.encode(text,add_special_tokens=False).ids
        title = _tokenizer.encode(headings,add_special_tokens=False).ids[:64]
        budget = 440-len(title)
        if len(ids)>budget:
            width=budget//3
            ids=ids[:width]+ids[len(ids)//2-width//2:len(ids)//2+width//2]+ids[-width:]
        body = _tokenizer.decode(title+ids,skip_special_tokens=True)
    else:
        windows = [text[:320],text[max(0,len(text)//2-160):len(text)//2+160],text[-320:]] if len(text)>960 else [text]
        body = headings[:140]+'\n'+'\n'.join(dict.fromkeys(windows))
    return prefix+body[:Config.MAIL_EMBED_MAX_CHARS-len(prefix)]


def passage_text(text, headings=None):
    return _window(text,embedding_prefix('passage', Config),' / '.join(headings or []))


def query_text(question):
    return _window(question,embedding_prefix('query', Config))


def model_key(client):
    # Encoding changes invalidate cached vectors, while model clients still use the actual model name.
    from services.embedding_backend import client_identity
    policy = 'mail-e5-window-v1' if embedding_encoding(Config) == 'e5' else 'mail-plain-window-v1'
    return client_identity(client)+':'+policy+':'+str(Config.MAIL_EMBED_MAX_CHARS)


def _require_space(c, package_id, key, dimension=0):
    from services.embedding_backend import require_index_identity
    keys=[r[0] for r in c.execute('SELECT DISTINCT model FROM mail_embeddings WHERE package_id=?',(package_id,))]
    require_index_identity(c,'mail',package_id,key,dimension,keys)
    dimensions={r[0] for r in c.execute('SELECT DISTINCT dim FROM mail_embeddings WHERE package_id=?',(package_id,))}
    if len(dimensions)>1 or (dimension and dimensions and dimensions!={dimension}):
        from services.model_contract import LocalModelError
        raise LocalModelError('邮件向量维度不一致，旧索引已保留，请重建',code='embedding_rebuild_required')


def index_status(repository, package_id, owner_id, key=None):
    package=repository.get_package(package_id,owner_id)
    if not package:raise ValueError('邮件资料包不存在')
    if key is None:
        try:
            client=embedding_client()
            if not client:return {'status':'disabled','total':0,'ready':0,'pending':0}
            key=model_key(client)
        except Exception as exc:
            return {'status':'unavailable','total':0,'ready':0,'pending':0,'error':str(exc)}
    with repository._connection() as c:
        total=c.execute('SELECT COUNT(*) FROM mail_text_segments WHERE package_id=?',(package_id,)).fetchone()[0]
        prior={r[0] for r in c.execute('SELECT DISTINCT model FROM mail_embeddings WHERE package_id=?',(package_id,))}
        if prior and prior!={key}:
            return {'total':total,'ready':0,'pending':total,'status':'embedding_rebuild_required','old_index_preserved':True,'rebuild_command':'python scripts/rebuild_embeddings.py --scope mail --package-id '+package_id}
        ready=c.execute('SELECT COUNT(*) FROM mail_text_segments s JOIN mail_embeddings e ON e.segment_id=s.segment_id AND e.package_id=s.package_id AND e.text_sha256=s.text_sha256 WHERE s.package_id=? AND e.model=?',(package_id,key)).fetchone()[0]
    return {'total':total,'ready':ready,'pending':max(0,total-ready),'status':'ready' if ready==total else 'partial' if ready else 'not_started'}


def ensure_header_segments(repository,package_id,owner_id):
    """Index subject evidence separately; generic bodies must not hide informative subjects."""
    package=repository.get_package(package_id,owner_id)
    if not package:raise ValueError('邮件资料包不存在')
    with repository._connection() as c:
        rows=c.execute("SELECT DISTINCT m.message_key,m.subject_raw FROM mail_messages m JOIN mail_visible_occurrences o ON o.message_key=m.message_key WHERE o.package_id=? AND COALESCE(m.subject_raw,'')<>'' AND NOT EXISTS (SELECT 1 FROM mail_text_segments s WHERE s.package_id=o.package_id AND s.message_key=m.message_key AND s.source_kind='header')",(package_id,)).fetchall()
        for row in rows:
            text=row['subject_raw'];sid='mheader_'+hashlib.sha256((package_id+'\0'+row['message_key']).encode()).hexdigest()[:32]
            digest=hashlib.sha256(text.encode()).hexdigest();language=repository._language(text)
            c.execute("INSERT OR IGNORE INTO mail_text_segments(segment_id,package_id,message_key,mime_path,source_kind,language,ordinal,char_start,char_end,text,text_sha256,source_revision,index_epoch) VALUES(?,?,?,'headers.subject','header',?,0,0,?,?,?,?,?)",(sid,package_id,row['message_key'],language,len(text),text,digest,package['source_revision'],package['index_epoch']))
            c.execute("INSERT INTO mail_search_fts(segment_id,package_id,message_key,mime_path,field,language,text) VALUES(?,?,?,'headers.subject','header',?,?)",(sid,package_id,row['message_key'],language,text))
    return len(rows)


def embed_segments_batch(repository,package_id,owner_id,client=None,cancelled=lambda:False):
    package=repository.get_package(package_id,owner_id)
    if not package:raise ValueError('邮件资料包不存在')
    if cancelled():raise RuntimeError('邮件任务已取消')
    if Config.MAIL_EMBED_PAUSED:return {'paused':True,**index_status(repository,package_id,owner_id)}
    client=client or embedding_client()
    if not client:raise RuntimeError('邮件语义索引未启用或嵌入服务不可用')
    key=model_key(client)
    with repository._connection() as c:
        _require_space(c,package_id,key)
    ensure_header_segments(repository,package_id,owner_id)
    with repository._connection() as c:
        rows=[dict(r) for r in c.execute('SELECT s.* FROM mail_text_segments s LEFT JOIN mail_embeddings e ON e.package_id=s.package_id AND e.segment_id=s.segment_id AND e.model=? AND e.text_sha256=s.text_sha256 WHERE s.package_id=? AND e.segment_id IS NULL ORDER BY s.segment_id LIMIT ?',(key,package_id,Config.MAIL_EMBED_BATCH))]
        cached={}
        for r in rows:
            old=c.execute('SELECT vector FROM mail_embeddings WHERE package_id=? AND model=? AND text_sha256=? LIMIT 1',(package_id,key,r['text_sha256'])).fetchone()
            if old:cached[r['text_sha256']]=normalize_vector(np.frombuffer(old[0],dtype=np.float32))
    pending={r['text_sha256']:passage_text(r['text']) for r in rows if r['text_sha256'] not in cached}
    if pending:
        values=client.embed(list(pending.values()))
        from services.embedding_backend import validate_vectors
        validate_vectors(values,len(pending),len(next(iter(cached.values()))) if cached else getattr(client,'dimension',0))
        if len(values)!=len(pending):raise ValueError('嵌入响应数量不一致')
        cached.update(zip(pending,[normalize_vector(v) for v in values]))
    if cancelled():raise RuntimeError('邮件任务已取消')
    with repository._connection() as c:
        dimensions={len(v) for v in cached.values()}
        if len(dimensions)>1:raise ValueError('邮件向量维度不一致')
        _require_space(c,package_id,key,next(iter(dimensions)) if dimensions else 0)
        for r in rows:
            current=c.execute('SELECT text_sha256 FROM mail_text_segments WHERE package_id=? AND segment_id=?',(package_id,r['segment_id'])).fetchone()
            if not current or current[0]!=r['text_sha256']:continue
            v=cached[r['text_sha256']]
            c.execute('INSERT OR REPLACE INTO mail_embeddings(package_id,segment_id,model,dim,vector,text_sha256,index_epoch) VALUES(?,?,?,?,?,?,?)',(package_id,r['segment_id'],key,len(v),v.tobytes(),r['text_sha256'],package['index_epoch']))
            c.execute('DELETE FROM mail_embedding_buckets WHERE package_id=? AND segment_id=? AND model=?',(package_id,r['segment_id'],key))
            c.executemany('INSERT INTO mail_embedding_buckets VALUES(?,?,?,?,?)',[(package_id,key,b,bucket,r['segment_id']) for b,bucket in enumerate(lsh_buckets(v))])
    status=index_status(repository,package_id,owner_id,key)
    with repository._connection() as c:
        c.execute("INSERT OR REPLACE INTO mail_index_epochs(package_id,index_epoch,stage,status,item_total,item_ready,item_failed,item_pending,config_fingerprint,completed_at) VALUES(?,?,'embedding',?,?,?,0,?,?,CASE WHEN ?=0 THEN CURRENT_TIMESTAMP ELSE NULL END)",(package_id,package['index_epoch'],status['status'],status['total'],status['ready'],status['pending'],key,status['pending']))
    return {**status,'processed':len(rows),'encoded':len(pending)}


def vector_search(repository,package_id,owner_id,question,*,limit=200,attachment_only=False,client=None,allowed_keys=None,include_attachments=True):
    if not repository.get_package(package_id,owner_id):raise ValueError('邮件资料包不存在')
    client=client or embedding_client()
    if not client:return [],{'status':'disabled'}
    key=model_key(client);status=index_status(repository,package_id,owner_id,key)
    if not status['ready']:return [],status
    if allowed_keys is not None:
        allowed_keys=sorted(set(allowed_keys))
        if not allowed_keys:return [],{**status,'scope_applied_before_limit':True,'scoped_ready':0}
    query=normalize_vector(client.embed([query_text(question)])[0])
    # Scan moderate mail packages exactly; LSH is approximate only above 20k valid segments.
    with repository._connection() as c:
        _require_space(c,package_id,key,len(query))
        ids=None
        scope_sql="";scope_args=[]
        if allowed_keys is not None:
            from .research.scope import scope_clause
            scope_sql=scope_clause(c,allowed_keys,'s.message_key')
        if not include_attachments:scope_sql+=" AND s.source_kind!='attachment'"
        if attachment_only:scope_sql+=" AND s.source_kind='attachment'"
        scoped_ready=c.execute('SELECT COUNT(*) FROM mail_embeddings e JOIN mail_text_segments s ON s.package_id=e.package_id AND s.segment_id=e.segment_id AND s.text_sha256=e.text_sha256 WHERE e.package_id=? AND e.model=?'+scope_sql,[package_id,key,*scope_args]).fetchone()[0]
        status.update(scope_applied_before_limit=True,scoped_ready=scoped_ready)
        if scoped_ready>20000:
            counts={}
            for band,bucket in enumerate(lsh_buckets(query)):
                neighbors=[bucket]+[bucket^(1<<i) for i in range(10)]
                for r in c.execute('SELECT b.segment_id FROM mail_embedding_buckets b JOIN mail_text_segments s ON s.package_id=b.package_id AND s.segment_id=b.segment_id WHERE b.package_id=? AND b.model=? AND b.band=? AND b.bucket IN ('+','.join('?' for _ in neighbors)+')'+scope_sql+' LIMIT 4096',[package_id,key,band,*neighbors,*scope_args]):counts[r[0]]=counts.get(r[0],0)+1
            ids=sorted(counts,key=counts.get,reverse=True)[:4096]
            status['approximate']=True
            if not ids:return [],status
        sql='SELECT s.*,m.subject_raw,m.sent_at_utc,e.vector FROM mail_embeddings e JOIN mail_text_segments s ON s.package_id=e.package_id AND s.segment_id=e.segment_id AND s.text_sha256=e.text_sha256 JOIN mail_messages m ON m.message_key=s.message_key WHERE e.package_id=? AND e.model=? AND e.dim=?'
        args=[package_id,key,len(query),*scope_args]
        sql+=scope_sql
        if attachment_only:sql+=" AND s.source_kind='attachment'"
        if ids:sql+=' AND s.segment_id IN ('+','.join('?' for _ in ids)+')';args+=ids
        scored=[]
        for row in c.execute(sql,args):
            r=dict(row);v=np.frombuffer(r.pop('vector'),dtype=np.float32)
            score=float(v@query)
            if np.isfinite(score):r['vector_score']=score;scored.append(r)
    scored.sort(key=lambda r:(-r['vector_score'],r['segment_id']))
    return scored[:limit],status
