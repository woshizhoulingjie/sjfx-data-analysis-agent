"""Explicit, recoverable embedding rebuilds using the existing SQLite indexes.

Build into a candidate snapshot. Publish only vector rows, in one transaction,
after rechecking source and vector fingerprints. Full snapshots remain available
for audit; restore also copies vector rows only and never replaces source data.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from services.embedding_backend import (client_identity, create_embedding_client,
    require_index_identity, validate_vectors)
from services.model_contract import LocalModelError
from services.embedding_encoding import prefix as embedding_prefix


SCOPES = {
    'ordinary': ['embedding_cache'],
    'mail': ['mail_embeddings', 'mail_embedding_buckets'],
    'large': ['large_catalog_members', 'large_catalog_groups', 'large_catalog_buckets',
              'large_catalog_centers', 'large_catalog_km_state'],
}


def _connect(path):
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(c):
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _where(table, scope, package_id):
    if table == 'embedding_cache':
        prefix = 'ordinary-file::'+package_id+'::'
        return 'substr(cache_key,1,?)=?', [len(prefix), prefix]
    if table == 'sjfx_embedding_identity':
        return 'scope=? AND package_id=?', [scope, package_id]
    return 'package_id=?', [package_id]


def _hash_rows(h, table, rows):
    h.update(table.encode())
    for row in rows:
        h.update(b'R')
        for value in row:
            if isinstance(value, bytes):
                payload = b'B'+value
            else:
                payload = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
            h.update(len(payload).to_bytes(8, 'big')); h.update(payload)


def _fingerprints(c, scope, package_id):
    vector_hash, source_hash = hashlib.sha256(), hashlib.sha256()
    vectors = set(SCOPES[scope]) | {'sjfx_embedding_identity'}
    for table in sorted(_tables(c)):
        if table.startswith('sjfx_embedding_rebuild_'):
            continue
        if not table.replace('_', '').isalnum():
            continue
        columns = [r[1] for r in c.execute('PRAGMA table_info("'+table+'")')]
        if table in vectors:
            clause, args = _where(table, scope, package_id)
            _hash_rows(vector_hash, table, c.execute('SELECT * FROM "'+table+'" WHERE '+clause+' ORDER BY '+','.join('"'+v+'"' for v in columns), args))
        else:
            if scope == 'large' and (table.startswith(('lp_catalog_', 'large_catalog_')) or table in {'queue','lp_qa_source_revisions'}):
                continue
            domain_column = 'scan_id' if scope == 'ordinary' else 'package_id'
            if table == 'scans' and scope == 'ordinary': domain_column = 'id'
            if table == 'packages' and scope == 'large': domain_column = 'id'
            if domain_column not in columns or (scope == 'mail' and not table.startswith('mail_')):
                continue
            _hash_rows(source_hash, table, c.execute('SELECT * FROM "'+table+'" WHERE '+domain_column+'=? ORDER BY '+','.join('"'+v+'"' for v in columns), (package_id,)))
    return {'source': source_hash.hexdigest(), 'vectors': vector_hash.hexdigest()}


def _copy_snapshot(source, target):
    with closing(_connect(source)) as src, closing(_connect(target)) as dst:
        src.backup(dst)


def _normalize_batch(client, texts):
    import numpy as np
    raw = validate_vectors(client.embed(texts), len(texts), getattr(client, 'dimension', 0))
    return [np.asarray(v, dtype=np.float32)/np.linalg.norm(np.asarray(v, dtype=np.float32)) for v in raw]


def _build_mail(c, package_id, client):
    from services.mail.semantic import model_key, passage_text, lsh_buckets
    package = c.execute('SELECT * FROM mail_packages WHERE package_id=?', (package_id,)).fetchone()
    if not package:
        raise ValueError('邮件资料包不存在')
    key = model_key(client)
    c.execute('DELETE FROM mail_embeddings WHERE package_id=?', (package_id,))
    c.execute('DELETE FROM mail_embedding_buckets WHERE package_id=?', (package_id,))
    rows = c.execute('SELECT segment_id,text,text_sha256 FROM mail_text_segments WHERE package_id=? ORDER BY segment_id', (package_id,))
    count, dimension = 0, 0
    while True:
        batch = rows.fetchmany(max(1, min(64, client.max_batch_size)))
        if not batch: break
        values = _normalize_batch(client, [passage_text(r['text']) for r in batch])
        validate_vectors([v.tolist() for v in values],len(batch),dimension)
        for row, vector in zip(batch, values):
            dimension = len(vector)
            c.execute('INSERT INTO mail_embeddings(package_id,segment_id,model,dim,vector,text_sha256,index_epoch) VALUES(?,?,?,?,?,?,?)', (package_id,row['segment_id'],key,dimension,vector.tobytes(),row['text_sha256'],package['index_epoch']))
            c.executemany('INSERT INTO mail_embedding_buckets VALUES(?,?,?,?,?)', [(package_id,key,b,bucket,row['segment_id']) for b,bucket in enumerate(lsh_buckets(vector))])
        count += len(batch)
    return key, count, dimension


def _build_large(c, package_id, client):
    import numpy as np
    from services.large_package_isolated import LargePackageStore
    from services.large_package_semantic import embedding_passage, evidence_quality, _buckets
    from services.large_package_catalog import material
    from services.large_package_evidence import row_parts
    if not c.execute('SELECT 1 FROM packages WHERE id=?', (package_id,)).fetchone():
        raise ValueError('大资料包不存在')
    policy = c.execute('SELECT representation FROM lp_catalog_policy WHERE package_id=?', (package_id,)).fetchone() if 'lp_catalog_policy' in _tables(c) else None
    policy = int(policy[0]) if policy else 1
    identity = client_identity(client)
    count, dimension = 0, 0
    # Existing membership and user directory edits stay intact; changed vectors
    # invalidate k-means state so the regular worker can recompute when resumed.
    cursor = c.execute('SELECT f.* FROM files f JOIN large_catalog_members m ON m.package_id=f.package_id AND m.path=f.path WHERE f.package_id=? ORDER BY f.path', (package_id,))
    while True:
        batch = cursor.fetchmany(max(1, min(64, client.max_batch_size)))
        if not batch: break
        usable = []
        for row in batch:
            info = LargePackageStore._row(row)
            source = material(info)
            if source and evidence_quality(source[0])[0]:
                value, digest = source
                if policy >= 2: value = dict(value, evidence_blocks=row_parts(info))
                usable.append((row['path'], digest, embedding_passage(value, policy)))
            else:
                c.execute('DELETE FROM large_catalog_members WHERE package_id=? AND path=?', (package_id, row['path']))
        values = _normalize_batch(client, [item[2] for item in usable]) if usable else []
        validate_vectors([v.tolist() for v in values],len(usable),dimension)
        for (path, digest, _), vector in zip(usable, values):
            dimension = len(vector)
            c.execute('UPDATE large_catalog_members SET vector=?,input_hash=? WHERE package_id=? AND path=?', (vector.tobytes(),digest,package_id,path))
        count += len(values)
    c.execute('DELETE FROM large_catalog_centers WHERE package_id=?', (package_id,))
    c.execute('DELETE FROM large_catalog_buckets WHERE package_id=?', (package_id,))
    for group in c.execute('SELECT id FROM large_catalog_groups WHERE package_id=?', (package_id,)).fetchall():
        members = c.execute('SELECT path,vector FROM large_catalog_members WHERE package_id=? AND group_id=? ORDER BY path', (package_id,group[0])).fetchall()
        if members:
            anchor = np.stack([np.frombuffer(r[1],dtype=np.float32) for r in members]).mean(axis=0)
            norm = float(np.linalg.norm(anchor))
            if norm < 1e-8: anchor = np.frombuffer(members[0][1],dtype=np.float32)
            else: anchor = anchor/norm
            c.execute('UPDATE large_catalog_groups SET model=?,anchor=?,representative=?,member_count=? WHERE package_id=? AND id=?', (identity,anchor.tobytes(),members[0][0],len(members),package_id,group[0]))
            c.executemany('INSERT INTO large_catalog_buckets VALUES(?,?,?,?)', [(package_id,band,bucket,group[0]) for band,bucket in enumerate(_buckets(anchor))])
        else:
            c.execute('UPDATE large_catalog_groups SET model=?,anchor=? WHERE package_id=? AND id=?',(identity,b'',package_id,group[0]))
    c.execute("UPDATE large_catalog_km_state SET signature='',vectors=0 WHERE package_id=?", (package_id,))
    if count:
        # QA index already takes its model namespace from this existing table.
        c.execute('INSERT INTO large_catalog_centers VALUES(?,?,?,?,?,?)', (package_id,identity,None,0.94,count,time.time()))
    return identity, count, dimension


def _build_ordinary(c, candidate, db_path, package_id, client):
    from services.storage import Storage
    from services.ordinary_recall import _file_text
    # Candidate-side Storage reads existing parser sidecars through their
    # original directory; parser data and those files are never rewritten.
    storage = Storage.__new__(Storage)
    storage.sidecar_dir = Path(db_path).parent/'document_payloads'
    rows = c.execute('SELECT node_path,payload FROM unified_documents WHERE scan_id=? ORDER BY node_path', (package_id,)).fetchall()
    if not rows and 'scans' in _tables(c) and not c.execute('SELECT 1 FROM scans WHERE id=?',(package_id,)).fetchone():
        raise ValueError('普通资料包不存在')
    prefix = 'ordinary-file::'+package_id+'::'
    c.execute('DELETE FROM embedding_cache WHERE substr(cache_key,1,?)=?', (len(prefix),prefix))
    identity, count, dimension = client_identity(client), 0, 0
    for offset in range(0,len(rows),max(1,client.max_batch_size)):
        texts, keys = [], []
        for row in rows[offset:offset+client.max_batch_size]:
            path = row[0]; document = storage._load_document_payload(row[1])
            summaries = ['']
            if 'summaries' in _tables(c):
                for saved in c.execute('SELECT payload FROM summaries WHERE scan_id=? AND node_path=?',(package_id,path)):
                    payload=json.loads(saved[0])
                    summaries.append(' '.join(str(payload.get('summary') or payload.get('core_summary') or '').split())[:500])
            if not str((document or {}).get('text') or '').strip() and not any(summaries):
                continue
            passages = {_file_text(path.split('::')[-1],summary,document) for summary in summaries}
            for text in sorted(passages):
                if text:
                    keys.append(prefix+path+'::'+hashlib.sha256(text.encode('utf8','replace')).hexdigest()[:16]); texts.append(embedding_prefix('passage', client=client)+text)
        values = validate_vectors(client.embed(texts), len(texts),dimension or getattr(client,'dimension',0)) if texts else []
        for key, vector in zip(keys, values):
            dimension = len(vector)
            c.execute('INSERT INTO embedding_cache(cache_key,model,vector,dimensions) VALUES(?,?,?,?)', (key,identity,json.dumps(vector,allow_nan=False),dimension))
        count += len(values)
    return identity, count, dimension


def rebuild_embeddings(db_path, scope, package_id, *, client=None, publish=False, output_dir=None):
    if scope not in SCOPES:
        raise ValueError('Unknown embedding scope')
    db_path = Path(db_path).resolve()
    if not db_path.is_file(): raise ValueError('索引数据库不存在')
    client = client or create_embedding_client(lane=scope)
    if client is None: raise ValueError('向量服务未启用')
    # Read-only preflight happens before any source database mutation.
    client_identity(client)
    folder = Path(output_dir or db_path.parent/'embedding_rebuilds').resolve()
    folder.mkdir(parents=True,exist_ok=True)
    token = time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
    backup, candidate, manifest = [folder/(token+suffix) for suffix in ('.backup.db','.candidate.db','.json')]
    _copy_snapshot(db_path, backup)
    _copy_snapshot(backup, candidate)
    with closing(_connect(backup)) as c:
        before = _fingerprints(c,scope,package_id)
    result = dict(schema=1,db_path=str(db_path),scope=scope,package_id=package_id,
                  backup=str(backup),candidate=str(candidate),manifest=str(manifest),before=before,status='building')
    manifest.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    try:
        with closing(_connect(candidate)) as c:
            c.execute('BEGIN IMMEDIATE')
            if scope == 'mail': values = _build_mail(c,package_id,client)
            elif scope == 'large': values = _build_large(c,package_id,client)
            else: values = _build_ordinary(c,candidate,db_path,package_id,client)
            identity,count,dimension = values
            if count == 0:
                raise LocalModelError('没有可重建的正文向量；旧索引已保留。请先完成正文解析，或让目录工作器建立初始索引',code='rebuild_no_material')
            if 'sjfx_embedding_identity' in _tables(c):
                c.execute('DELETE FROM sjfx_embedding_identity WHERE scope=? AND package_id=?',(scope,package_id))
            require_index_identity(c,scope,package_id,identity,dimension)
            c.commit()
            if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('候选向量索引完整性检查失败')
            result.update(identity=identity,count=count,dimension=dimension,status='candidate_ready',candidate_fingerprint=_fingerprints(c,scope,package_id))
        manifest.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        if publish: return publish_rebuild(manifest)
        return result
    except Exception as exc:
        result.update(status='failed',error=str(exc))
        manifest.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        raise


def _replace_vectors(conn, attached, scope, package_id):
    # Existing table shapes are copied explicitly; source tables are excluded.
    for table in SCOPES[scope]+['sjfx_embedding_identity']:
        if table == 'sjfx_embedding_identity':
            conn.execute('CREATE TABLE IF NOT EXISTS sjfx_embedding_identity(scope TEXT NOT NULL,package_id TEXT NOT NULL,identity TEXT NOT NULL,dimension INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(scope,package_id))')
        columns = [r[1] for r in conn.execute('PRAGMA table_info("'+table+'")')]
        clause,args = _where(table,scope,package_id)
        conn.execute('DELETE FROM "'+table+'" WHERE '+clause,args)
        exists = conn.execute('SELECT 1 FROM '+attached+'.sqlite_master WHERE type=\'table\' AND name=?',(table,)).fetchone()
        if exists:
            names=','.join('"'+x+'"' for x in columns)
            conn.execute('INSERT INTO "'+table+'"('+names+') SELECT '+names+' FROM '+attached+'."'+table+'" WHERE '+clause,args)
    if scope == 'large':
        # Membership triggers run during publication; correct the persisted
        # counts in the same transaction regardless of table copy order.
        conn.execute('UPDATE large_catalog_groups SET member_count=(SELECT COUNT(*) FROM large_catalog_members m WHERE m.package_id=large_catalog_groups.package_id AND m.group_id=large_catalog_groups.id) WHERE package_id=?',(package_id,))
        if 'lp_catalog_vector_state' in _tables(conn):
            conn.execute('UPDATE lp_catalog_vector_state SET epoch=epoch+1 WHERE package_id=?',(package_id,))


def publish_rebuild(manifest_path, *, restore=False):
    manifest_path=Path(manifest_path)
    result=json.loads(manifest_path.read_text(encoding='utf8'))
    scope,package_id=result['scope'],result['package_id']
    if scope not in SCOPES or result.get('schema')!=1: raise ValueError('重建记录无效')
    # Recover a committed publication even if the process exited before the
    # external manifest update. This ledger commits with the vector rows.
    with closing(_connect(result['db_path'])) as c:
        if 'sjfx_embedding_rebuild_log' in _tables(c):
            committed=c.execute('SELECT state FROM sjfx_embedding_rebuild_log WHERE manifest=?',(str(manifest_path.resolve()),)).fetchone()
            if committed:
                saved=json.loads(committed[0])
                result.update(status=saved['status'],published_fingerprint=saved['published_fingerprint'])
    if not restore and result['status']!='candidate_ready': raise ValueError('候选索引尚未完成')
    if restore and result['status']!='published': raise ValueError('该索引未发布或已恢复')
    source=result['backup'] if restore else result['candidate']
    with closing(_connect(source)) as c:
        source_fingerprint=_fingerprints(c,scope,package_id)
    expected_source=result['before']['source']
    expected_vectors=result['published_fingerprint']['vectors'] if restore else result['before']['vectors']
    if source_fingerprint != (result['before'] if restore else result['candidate_fingerprint']):
        raise LocalModelError('候选索引或备份被修改，拒绝切换',code='rebuild_conflict')
    with closing(_connect(result['db_path'])) as c:
        c.execute('ATTACH DATABASE ? AS replacement',(source,))
        c.execute('BEGIN IMMEDIATE')
        try:
            current=_fingerprints(c,scope,package_id)
            if current['source']!=expected_source or current['vectors']!=expected_vectors:
                raise LocalModelError('资料或索引在重建后变化，未发布；旧索引和候选仍可恢复',code='rebuild_conflict')
            _replace_vectors(c,'replacement',scope,package_id)
            published=_fingerprints(c,scope,package_id)
            committed_status='restored' if restore else 'published'
            c.execute('CREATE TABLE IF NOT EXISTS sjfx_embedding_rebuild_log(manifest TEXT PRIMARY KEY,state TEXT NOT NULL)')
            c.execute('INSERT OR REPLACE INTO sjfx_embedding_rebuild_log VALUES(?,?)',(str(manifest_path.resolve()),json.dumps({'status':committed_status,'published_fingerprint':published},sort_keys=True)))
            c.commit()
        except Exception:
            c.rollback(); raise
    result.update(status='restored' if restore else 'published',published_fingerprint=published)
    try:
        manifest_path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    except OSError:
        result['manifest_warning']='索引事务已提交；重建状态可从数据库中的 sjfx_embedding_rebuild_log 恢复'
    return result
