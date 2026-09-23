"""Content checks for catalog mutations; no generative calls or source reads."""
import hashlib
import json
import re
import time
from contextlib import closing

import numpy as np


def unit(value):
    value = np.asarray(value, dtype=np.float32)
    norm = np.linalg.norm(value)
    return value / norm if norm > 1e-8 else value


def partition_members(rows, seeds, terms=None):
    """Score every member. Ambiguity vetoes the split instead of guessing."""
    vectors = {}
    for row in rows:
        raw = row['vector']
        vector = np.frombuffer(raw, dtype=np.float32) if isinstance(raw, bytes) else np.asarray(raw)
        if vector.ndim != 1 or not len(vector) or not np.isfinite(vector).all() or np.linalg.norm(vector) < 1e-8:
            return None
        vectors[row['path']] = unit(vector)
    if len({len(v) for v in vectors.values()}) != 1 or len(seeds) < 2:
        return None
    selected = [p for seed in seeds for p in seed['paths']]
    if len(selected) != len(set(selected)) or not set(selected).issubset(vectors):
        return None
    if any(not seed['paths'] for seed in seeds):return None
    centers = np.vstack([unit(np.mean([vectors[p] for p in seed['paths']], axis=0)) for seed in seeds])
    separation = centers @ centers.T
    np.fill_diagonal(separation, -1)
    if float(separation.max()) > .995:
        return None
    buckets = [dict(name=s['name'], paths=[]) for s in seeds]
    fixed = {p: i for i, seed in enumerate(seeds) for p in seed['paths']}
    vocabulary = [set().union(*(set((terms or {}).get(p, [])) for p in s['paths'])) for s in seeds]
    paths = sorted(vectors)
    for start in range(0, len(paths), 512):
        batch = paths[start:start+512]
        scores = np.vstack([vectors[p] for p in batch]) @ centers.T
        for path, values in zip(batch, scores):
            order = np.argsort(-values, kind='stable')
            first, second = map(int, order[:2])
            margin = max(.012, min(.055, (1-float(separation[first, second]))*.15))
            if path in fixed:
                choice = fixed[path]
                if values[first] - values[choice] > margin:
                    return None
            else:
                observed = set((terms or {}).get(path, []))
                lexical = [len(observed & v) / max(1, len(observed)) for v in vocabulary]
                if values[first] < .55 or values[first]-values[second] < margin:
                    # Presence can support a narrow boundary decision; absence cannot veto it.
                    if values[first] < .65 or lexical[first]-lexical[second] < .18:
                        return None
                choice = first
            buckets[choice]['paths'].append(path)
    return buckets


def split_assignment(store, package_id, group_id, seeds):
    from services import large_package_evidence_store as features
    cards = dict(features.group_cards(store, package_id, group_id))
    with closing(store._connect()) as c:
        rows = c.execute('SELECT path,vector FROM large_catalog_members WHERE package_id=? AND group_id=?',
                         (package_id, group_id)).fetchall()
    return partition_members(rows, seeds, {p: (card or {}).get('terms', []) for p, card in cards.items()})


def compatible_groups(c, package_id, group_ids):
    """Require reciprocal content support for every pair, including same-name merges."""
    ids = list(dict.fromkeys(group_ids))
    if len(ids) < 2:
        return False
    groups = []
    for gid in ids:
        label = c.execute('SELECT locked FROM lp_catalog_labels WHERE package_id=? AND group_id=?',
                          (package_id, gid)).fetchone()
        if label and label[0]:
            return False
        rows = c.execute('SELECT m.vector,CASE WHEN d.path IS NULL THEN e.card END card FROM large_catalog_members m LEFT JOIN lp_naming_features e '
                         'ON e.package_id=m.package_id AND e.path=m.path LEFT JOIN lp_feature_dirty d '
                         'ON d.package_id=m.package_id AND d.path=m.path WHERE m.package_id=? AND m.group_id=? '
                         'ORDER BY m.path', (package_id, gid)).fetchall()
        vectors, words, identifiers = [], set(), set()
        for row in rows:
            v = np.frombuffer(row['vector'], dtype=np.float32)
            if len(v) and np.isfinite(v).all() and np.linalg.norm(v) > 1e-8:
                vectors.append(unit(v))
            card = json.loads(row['card']) if row['card'] else {}
            words.update(card.get('terms') or [])
            for heading in card.get('headings') or []:
                identifiers.update(re.findall(r'(?:项目编号|合同编号|设备型号)\s*[:：]\s*([A-Za-z0-9_-]{3,40})', heading))
        if not vectors or len({len(v) for v in vectors}) != 1:
            return False
        matrix = np.vstack(vectors)
        center = unit(matrix.mean(axis=0))
        groups.append((matrix, center, words, identifiers))
    for i, (left, a, aw, ai) in enumerate(groups):
        for right, b, bw, bi in groups[i+1:]:
            if a.shape != b.shape or (ai and bi and ai.isdisjoint(bi)):
                return False
            if float(a @ b) < .94:
                return False
            for matrix, own, other in ((left,a,b),(right,b,a)):
                if float(np.quantile(matrix @ other, .1)) < max(.70, float(np.quantile(matrix @ own,.1))-.06):
                    return False
            if aw and bw and len(aw & bw)/max(1,min(len(aw),len(bw))) < .10:
                return False
    return True


def route(profile, coherence=None):
    n = max(1, int(profile.get('members') or 0))
    readable = int(profile.get('readable_files') or 0)
    if readable/n < .5:
        return 'insufficient'
    if (coherence is not None and coherence < .70) or (profile.get('minority') and not profile.get('common')):
        return 'boundary'
    return 'clear'


def init(c):
    # 触发器**无条件重建**。此前是 `CREATE TRIGGER IF NOT EXISTS`，于是已经建过
    # 触发器的库永远留着旧定义：修好写法再部署，线上仍然跑旧的坏触发器。
    # 实测代价：三个触发器的旧定义让 k-means 聚类写入整条路径永久崩掉
    # （见下面 lp_catalog_changed_* 的说明）。
    for _name in ('lp_catalog_changed_insert', 'lp_catalog_changed_vector',
                  'lp_catalog_changed_delete'):
        c.execute('DROP TRIGGER IF EXISTS %s' % _name)
    c.executescript('''
    CREATE TABLE IF NOT EXISTS lp_catalog_policy(package_id TEXT PRIMARY KEY,representation INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS lp_catalog_changes(package_id TEXT NOT NULL,path TEXT NOT NULL, PRIMARY KEY(package_id,path));
    CREATE TABLE IF NOT EXISTS lp_catalog_vector_state(package_id TEXT PRIMARY KEY,epoch INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS lp_catalog_removed_groups(package_id TEXT NOT NULL,group_id TEXT NOT NULL,PRIMARY KEY(package_id,group_id));
    CREATE TABLE IF NOT EXISTS lp_catalog_repair(package_id TEXT NOT NULL,group_id TEXT NOT NULL,signature TEXT NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(package_id,group_id));
    CREATE TABLE IF NOT EXISTS lp_catalog_refine(package_id TEXT NOT NULL,path TEXT NOT NULL,source TEXT NOT NULL,
      status TEXT NOT NULL,seconds REAL NOT NULL DEFAULT 0,bytes INTEGER NOT NULL DEFAULT 0,pages INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(package_id,path));
    CREATE TRIGGER lp_catalog_changed_insert AFTER INSERT ON large_catalog_members BEGIN
      INSERT INTO lp_catalog_changes(package_id,path) SELECT NEW.package_id,NEW.path
        WHERE NOT EXISTS(SELECT 1 FROM lp_catalog_changes
                         WHERE package_id=NEW.package_id AND path=NEW.path);
      INSERT INTO lp_catalog_vector_state VALUES(NEW.package_id,1) ON CONFLICT(package_id) DO UPDATE SET epoch=epoch+1; END;
    CREATE TRIGGER lp_catalog_changed_vector AFTER UPDATE OF vector,input_hash ON large_catalog_members
    WHEN OLD.vector IS NOT NEW.vector OR OLD.input_hash IS NOT NEW.input_hash BEGIN
      INSERT INTO lp_catalog_changes(package_id,path) SELECT NEW.package_id,NEW.path
        WHERE NOT EXISTS(SELECT 1 FROM lp_catalog_changes
                         WHERE package_id=NEW.package_id AND path=NEW.path);
      INSERT INTO lp_catalog_vector_state VALUES(NEW.package_id,1) ON CONFLICT(package_id) DO UPDATE SET epoch=epoch+1; END;
    CREATE TRIGGER lp_catalog_changed_delete AFTER DELETE ON large_catalog_members BEGIN
      INSERT INTO lp_catalog_changes(package_id,path) SELECT OLD.package_id,OLD.path
        WHERE NOT EXISTS(SELECT 1 FROM lp_catalog_changes
                         WHERE package_id=OLD.package_id AND path=OLD.path);
      INSERT OR IGNORE INTO lp_catalog_removed_groups VALUES(OLD.package_id,OLD.group_id);
      INSERT INTO lp_catalog_vector_state VALUES(OLD.package_id,1) ON CONFLICT(package_id) DO UPDATE SET epoch=epoch+1; END;
    CREATE TRIGGER IF NOT EXISTS lp_catalog_file_removed AFTER DELETE ON files BEGIN
      DELETE FROM large_catalog_members WHERE package_id=OLD.package_id AND path=OLD.path; END;
    CREATE TRIGGER IF NOT EXISTS lp_catalog_quality_delete AFTER DELETE ON packages BEGIN
      DELETE FROM lp_catalog_policy WHERE package_id=OLD.id;
      DELETE FROM lp_catalog_changes WHERE package_id=OLD.id;
      DELETE FROM lp_catalog_vector_state WHERE package_id=OLD.id;
      DELETE FROM lp_catalog_removed_groups WHERE package_id=OLD.id;
      DELETE FROM lp_catalog_repair WHERE package_id=OLD.id;
      DELETE FROM lp_catalog_refine WHERE package_id=OLD.id; END;
    ''')


def representation(store, package_id):
    with store._lock, closing(store._connect()) as c:
        c.execute('INSERT OR IGNORE INTO lp_catalog_policy SELECT ?,CASE WHEN EXISTS('
                  'SELECT 1 FROM large_catalog_members WHERE package_id=?) THEN 1 ELSE 2 END',
                  (package_id,package_id))
        return int(c.execute('SELECT representation FROM lp_catalog_policy WHERE package_id=?',(package_id,)).fetchone()[0])


def repair_changed(store, package_id):
    """A completed catalog can absorb a bounded delta without rebuilding stable groups."""
    from services import large_package_semantic as semantic
    from services.large_package_catalog_naming import _group_geometry
    signature = semantic._km_signature(store, package_id)
    with closing(store._connect()) as c:
        dirty = [r[0] for r in c.execute('SELECT path FROM lp_catalog_changes WHERE package_id=?',(package_id,))]
        if not dirty:
            existing=c.execute('SELECT vectors FROM large_catalog_km_state WHERE package_id=?',(package_id,)).fetchone()
            semantic._km_state_put(store,package_id,signature,existing[0] if existing else 0)
            return 0
        total = c.execute('SELECT COUNT(*) FROM large_catalog_members WHERE package_id=?',(package_id,)).fetchone()[0]
        if len(dirty) > max(32,min(5000,total//10)):
            return None
        groups = [dict(r) for r in c.execute('SELECT g.id,g.anchor,COALESCE(l.locked,0) locked FROM large_catalog_groups g '
                   'LEFT JOIN lp_catalog_labels l ON l.package_id=g.package_id AND l.group_id=g.id '
                   "WHERE g.package_id=? AND g.id NOT LIKE 'km-pending-%' AND g.id NOT LIKE 'km-other-%'", (package_id,))]
        rows = []
        removed={r[0] for r in c.execute('SELECT group_id FROM lp_catalog_removed_groups WHERE package_id=?',(package_id,))}
        for start in range(0,len(dirty),256):
            batch = dirty[start:start+256]
            rows.extend(dict(r) for r in c.execute('SELECT * FROM large_catalog_members WHERE package_id=? AND path IN ('+
                        ','.join('?' for _ in batch)+')',[package_id]+batch))
    valid = [(g,unit(np.frombuffer(g['anchor'],dtype=np.float32))) for g in groups if g['anchor']]
    valid = [(g,v) for g,v in valid if len(v) and np.isfinite(v).all() and np.linalg.norm(v)>1e-8]
    if not valid:
        return None
    if len({len(v) for _,v in valid}) != 1:return None
    locked = {g['id'] for g in groups if g['locked']}
    centers = np.vstack([v for _,v in valid])
    updates, affected = [], set(removed)
    for row in rows:
        v = np.frombuffer(row['vector'],dtype=np.float32)
        if v.shape != centers.shape[1:]:
            return None
        old = row['group_id']; target = old
        scores = centers @ unit(v); order = np.argsort(-scores)
        top = int(order[0]); margin = float(scores[top]-scores[order[1]]) if len(order)>1 else 1.0
        if old not in locked and not valid[top][0]['locked'] and scores[top] >= .78 and margin >= .025:
            target = valid[top][0]['id']
        elif old.startswith('km-pending-'):
            # A new ambiguous group remains independently nameable; it is never assigned by size.
            target = 'km-local-'+hashlib.sha256((package_id+'\0'+row['path']).encode()).hexdigest()[:18]
        updates.append((row,target))
        affected.update((old,target))
    with store._lock, closing(store._connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            if vector_signature(c,package_id) != signature:
                c.rollback();return 0
            package=c.execute('SELECT status FROM packages WHERE id=?',(package_id,)).fetchone()
            if not package or package[0] in {'paused','cancelled'}:
                c.rollback();return 0
            live_locks={r[0] for r in c.execute('SELECT group_id FROM lp_catalog_labels WHERE package_id=? AND locked=1',(package_id,))}
            if live_locks != locked:
                c.rollback();return 0
            for row,target in updates:
                current=c.execute('SELECT input_hash,group_id,vector FROM large_catalog_members WHERE package_id=? AND path=?',
                                  (package_id,row['path'])).fetchone()
                if not current or tuple(current)!=(row['input_hash'],row['group_id'],row['vector']):
                    c.rollback();return 0
                if target.startswith('km-local-'):
                    model=c.execute('SELECT model FROM large_catalog_groups WHERE package_id=? AND id=?',(package_id,row['group_id'])).fetchone()
                    c.execute('INSERT OR IGNORE INTO large_catalog_groups(package_id,id,model,anchor,representative,status,updated_at) '
                              "VALUES(?,?,?,?,?,'queued',?)",(package_id,target,model[0] if model else '',row['vector'],row['path'],time.time()))
                c.execute('UPDATE large_catalog_members SET group_id=? WHERE package_id=? AND path=? AND group_id<>?',
                          (target,package_id,row['path'],target))
                c.execute('DELETE FROM lp_catalog_changes WHERE package_id=? AND path=?',(package_id,row['path']))
            for gid in affected:
                center,rep=_group_geometry(c,package_id,gid)
                if center is not None:
                    c.execute('UPDATE large_catalog_groups SET anchor=?,representative=? WHERE package_id=? AND id=?',
                              (center,rep,package_id,gid))
            c.execute('DELETE FROM large_catalog_groups WHERE package_id=? AND member_count=0',(package_id,))
            c.execute('DELETE FROM lp_catalog_changes WHERE package_id=?',(package_id,))
            c.execute('DELETE FROM lp_catalog_removed_groups WHERE package_id=?',(package_id,))
            save_signature(c,package_id,total)
            c.commit()
        except Exception:
            c.rollback();raise
    store._catalog_cache.pop(package_id,None)
    return len(rows)


def vector_signature(c,package_id):
    row=c.execute('SELECT epoch FROM lp_catalog_vector_state WHERE package_id=?',(package_id,)).fetchone()
    return 'vectors/v3:%d'%(row[0] if row else 0)


def save_signature(c,package_id,total):
    c.execute('INSERT INTO large_catalog_km_state(package_id,signature,vectors,updated_at) VALUES(?,?,?,?) '
              'ON CONFLICT(package_id) DO UPDATE SET signature=excluded.signature,vectors=excluded.vectors,updated_at=excluded.updated_at',
              (package_id,vector_signature(c,package_id),total,time.time()))


def repair_mixed(store,package_id):
    """At most 24 local attempts per package, one per input; never rebuild clean groups."""
    from services import large_package_semantic as semantic
    deadline=time.monotonic()+20;changed=0
    with closing(store._connect()) as c:
        groups=[dict(r) for r in c.execute('''SELECT g.* FROM large_catalog_groups g LEFT JOIN lp_catalog_labels l
          ON l.package_id=g.package_id AND l.group_id=g.id WHERE g.package_id=? AND g.member_count>=6
          AND json_extract(g.result,'$.purity')='mixed' AND COALESCE(l.locked,0)=0 ORDER BY g.member_count DESC,g.id LIMIT 12''',(package_id,))]
    for group in groups:
        if time.monotonic()>=deadline:break
        with store._lock,closing(store._connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            p=c.execute('SELECT status FROM packages WHERE id=?',(package_id,)).fetchone()
            attempts=c.execute('SELECT COALESCE(SUM(attempts),0) FROM lp_catalog_repair WHERE package_id=?',(package_id,)).fetchone()[0]
            if not p or p[0] in {'paused','cancelled'} or attempts>=24:c.rollback();break
            source=list(map(tuple,c.execute('SELECT path,input_hash FROM large_catalog_members WHERE package_id=? AND group_id=? ORDER BY path',(package_id,group['id']))))
            digest=hashlib.sha256(json.dumps(source).encode()).hexdigest()
            old=c.execute('SELECT signature FROM lp_catalog_repair WHERE package_id=? AND group_id=?',(package_id,group['id'])).fetchone()
            if old and old[0]==digest:c.rollback();continue
            c.execute('INSERT INTO lp_catalog_repair VALUES(?,?,?,1) ON CONFLICT(package_id,group_id) DO UPDATE SET signature=excluded.signature,attempts=attempts+1',(package_id,group['id'],digest))
            c.commit()
        changed+=semantic._split_one(store,package_id,group,max(2,int(group['member_count'])//2))
    if changed:
        from services.large_package_catalog_naming import queue_groups
        queue_groups(store,package_id)
    return bool(changed)
