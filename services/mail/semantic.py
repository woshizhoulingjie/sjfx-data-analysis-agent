"""Package-scoped E5 encoding, adapted from large_package_semantic without modifying it."""
from __future__ import annotations
import hashlib
import time
import numpy as np
from config import Config

_projections = {}
_tokenizer = None
_tokenizer_checked = False


def embedding_client():
    if not Config.MAIL_ENABLE_VECTOR_INDEX or not Config.ENABLE_VLLM_EMBEDDINGS:
        return None
    from services.vllm import VLLMEmbeddingClient
    return VLLMEmbeddingClient(Config.MAIL_EMBED_BASE_URL, Config.MAIL_EMBED_MODEL,
        timeout=12, max_batch_size=8, max_chars=1200, api_key=Config.VLLM_API_KEY)


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
    if not _tokenizer_checked:
        _tokenizer_checked = True
        try:
            import os
            from pathlib import Path
            from tokenizers import Tokenizer
            path = Path(os.getenv('SJFX_EMBED_MODEL_PATH',str(Path(Config.VLLM_MODEL_PATH).parent/'multilingual-e5-small')))/'tokenizer.json'
            _tokenizer = Tokenizer.from_file(str(path))
        except Exception:
            pass
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
    return _window(text,'passage: ',' / '.join(headings or []))


def query_text(question):
    return _window(question,'query: ')


def model_key(client):
    # Encoding changes invalidate cached vectors, while model clients still use the actual model name.
    return str(client.model)+':mail-e5-window-v1:'+str(Config.MAIL_EMBED_MAX_CHARS)


def index_status(repository, package_id, owner_id, key=None):
    package=repository.get_package(package_id,owner_id)
    if not package:raise ValueError('邮件资料包不存在')
    key=key or Config.MAIL_EMBED_MODEL+':mail-e5-window-v1:'+str(Config.MAIL_EMBED_MAX_CHARS)
    with repository._connection() as c:
        total=c.execute('SELECT COUNT(*) FROM mail_text_segments WHERE package_id=?',(package_id,)).fetchone()[0]
        ready=c.execute('SELECT COUNT(*) FROM mail_text_segments s JOIN mail_embeddings e ON e.segment_id=s.segment_id AND e.package_id=s.package_id AND e.text_sha256=s.text_sha256 WHERE s.package_id=? AND e.model=?',(package_id,key)).fetchone()[0]
    return {'total':total,'ready':ready,'pending':max(0,total-ready),'status':'ready' if ready==total else 'partial' if ready else 'not_started'}


def ensure_header_segments(repository,package_id,owner_id):
    """Index subject evidence separately; generic bodies must not hide informative subjects."""
    package=repository.get_package(package_id,owner_id)
    if not package:raise ValueError('邮件资料包不存在')
    with repository._connection() as c:
        rows=c.execute("SELECT DISTINCT m.message_key,m.subject_raw FROM mail_messages m JOIN mail_occurrences o ON o.message_key=m.message_key WHERE o.package_id=? AND COALESCE(m.subject_raw,'')<>'' AND NOT EXISTS (SELECT 1 FROM mail_text_segments s WHERE s.package_id=o.package_id AND s.message_key=m.message_key AND s.source_kind='header')",(package_id,)).fetchall()
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
    ensure_header_segments(repository,package_id,owner_id)
    key=model_key(client)
    with repository._connection() as c:
        rows=[dict(r) for r in c.execute('SELECT s.* FROM mail_text_segments s LEFT JOIN mail_embeddings e ON e.package_id=s.package_id AND e.segment_id=s.segment_id AND e.model=? AND e.text_sha256=s.text_sha256 WHERE s.package_id=? AND e.segment_id IS NULL ORDER BY s.segment_id LIMIT ?',(key,package_id,Config.MAIL_EMBED_BATCH))]
        cached={}
        for r in rows:
            old=c.execute('SELECT vector FROM mail_embeddings WHERE package_id=? AND model=? AND text_sha256=? LIMIT 1',(package_id,key,r['text_sha256'])).fetchone()
            if old:cached[r['text_sha256']]=normalize_vector(np.frombuffer(old[0],dtype=np.float32))
    pending={r['text_sha256']:passage_text(r['text']) for r in rows if r['text_sha256'] not in cached}
    if pending:
        values=client.embed(list(pending.values()))
        if len(values)!=len(pending):raise ValueError('嵌入响应数量不一致')
        cached.update(zip(pending,[normalize_vector(v) for v in values]))
    if cancelled():raise RuntimeError('邮件任务已取消')
    with repository._connection() as c:
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


def vector_search(repository,package_id,owner_id,question,*,limit=200,attachment_only=False,client=None):
    if not repository.get_package(package_id,owner_id):raise ValueError('邮件资料包不存在')
    client=client or embedding_client()
    if not client:return [],{'status':'disabled'}
    key=model_key(client);status=index_status(repository,package_id,owner_id,key)
    if not status['ready']:return [],status
    query=normalize_vector(client.embed([query_text(question)])[0])
    # Scan moderate mail packages exactly; LSH is approximate only above 20k valid segments.
    with repository._connection() as c:
        ids=None
        if status['ready']>20000:
            counts={}
            for band,bucket in enumerate(lsh_buckets(query)):
                neighbors=[bucket]+[bucket^(1<<i) for i in range(10)]
                for r in c.execute('SELECT segment_id FROM mail_embedding_buckets WHERE package_id=? AND model=? AND band=? AND bucket IN ('+','.join('?' for _ in neighbors)+') LIMIT 4096',[package_id,key,band]+neighbors):counts[r[0]]=counts.get(r[0],0)+1
            ids=sorted(counts,key=counts.get,reverse=True)[:4096]
            status['approximate']=True
            if not ids:return [],status
        sql='SELECT s.*,m.subject_raw,m.sent_at_utc,e.vector FROM mail_embeddings e JOIN mail_text_segments s ON s.package_id=e.package_id AND s.segment_id=e.segment_id AND s.text_sha256=e.text_sha256 JOIN mail_messages m ON m.message_key=s.message_key WHERE e.package_id=? AND e.model=? AND e.dim=?'
        args=[package_id,key,len(query)]
        if attachment_only:sql+=" AND s.source_kind='attachment'"
        if ids:sql+=' AND s.segment_id IN ('+','.join('?' for _ in ids)+')';args+=ids
        scored=[]
        for row in c.execute(sql,args):
            r=dict(row);v=np.frombuffer(r.pop('vector'),dtype=np.float32)
            score=float(v@query)
            if np.isfinite(score):r['vector_score']=score;scored.append(r)
    scored.sort(key=lambda r:(-r['vector_score'],r['segment_id']))
    return scored[:limit],status
