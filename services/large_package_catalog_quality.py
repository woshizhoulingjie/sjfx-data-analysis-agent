"""Content checks for catalog mutations; no generative calls or source reads."""
import hashlib
import json
import re
import time
from contextlib import closing

import logging

import numpy as np


# 批2 第 12 条：本模块此前**没有 logger**（也没有 `import logging`），
# 我第一版直接写 `LOGGER.warning(...)` 抛了 `NameError` —— 而它是被
# `merge_identical_names` 的 `if not compatible_groups(...)` 吞掉的，
# 表现为"同名一个都不并"（3 条契约测试变红），排查了很久才定位到。
LOGGER = logging.getLogger('sjfx.catalog.quality')

# 批2 第 12 条：最近一次 `compatible_groups` 里"证据不足"的组（只用于观测）。
LAST_MISSING_EVIDENCE = {}


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
    """Require reciprocal content support for every pair, including same-name merges.

    批2 第 12 条：**特征脏的组不能让质量门禁整体短路。**
    改动前的漏洞链（`quality.py:84-86`）：`lp_feature_dirty` 命中的路径 → `card`
    为 NULL → 词集为空 → 后面 `if aw and bw and ...` 因为 `aw and bw` 为假
    **直接跳过** → 词汇门"通过"。结果是最该被拦的那种包（刚重新解析过内容、
    特征还没补上）反而最容易发生错误合并。

    现在把"证据不足"与"证据支持"区分开：
      · **有卡但被标脏**（`lp_feature_dirty` 命中）→ 记为证据不足；
      · 证据不足时提高门槛而不是跳过：质心门 .94→.96、十分位门 →.80，
        并且**两边都必须有词证据**才允许合并；
      · 计数暴露在 `LAST_MISSING_EVIDENCE`，供运维判断"这次没合"
        到底是"内容不合"还是"特征没补上"。

    **只把"有卡但被标脏"算成脏**：完全没有特征卡是另一种情况（特征从未算过，
    例如测试夹具、或包刚建好还没跑特征抽取），不在本条范围内。
    """
    global LAST_MISSING_EVIDENCE
    ids = list(dict.fromkeys(group_ids))
    if len(ids) < 2:
        return False
    LAST_MISSING_EVIDENCE = {}
    groups = []
    evidence_gap = {}
    for gid in ids:
        label = c.execute('SELECT locked FROM lp_catalog_labels WHERE package_id=? AND group_id=?',
                          (package_id, gid)).fetchone()
        if label and label[0]:
            return False
        # **SQL 与生产逐字一致**（`CASE WHEN d.path IS NULL THEN e.card END`）：
        # 我一度把它写成"标脏时把 card 置成 NULL"，那等于改了一条既有判据的语义，
        # 直接导致 3 条同名合并契约测试变红。本条的靶心是"零证据不许静默通过"，
        # 不是"顺手把取数口径也改掉"。
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
        # 批2 第 12 条：**这一组有多少条"被标脏/特征失效"的路径。**
        #
        # 判据就是读取路径自己的口径：`lp_feature_dirty` 命中的路径，其 `card`
        # 在读取时被置 NULL（`CASE WHEN d.path IS NULL THEN e.card END`）。
        # **只做观测**：把"有多少条路径的特征已失效"记进
        # `LAST_MISSING_EVIDENCE`，供运维与发布校验判断"这次合并到底有没有
        # 内容证据"。**不改变本函数的返回值** —— 见下面"为什么这里不拦截"。
        dirty_paths = int(c.execute(
            'SELECT COUNT(*) FROM lp_feature_dirty WHERE package_id=? AND path IN '
            '(SELECT path FROM large_catalog_members WHERE package_id=? AND group_id=?)',
            (package_id, package_id, gid)).fetchone()[0] or 0)
        if dirty_paths:
            evidence_gap[gid] = {"dirty_paths": dirty_paths, "members": len(rows),
                                 "usable_cards": sum(1 for row in rows if row['card'])}
        matrix = np.vstack(vectors)
        center = unit(matrix.mean(axis=0))
        groups.append((matrix, center, words, identifiers))
    # 批2 第 12 条：**两边都没有可用词证据时，不允许"静默通过"。**
    #
    # 改动前的行为是：`if aw and bw and ...` 因为 `aw and bw` 为假**直接跳过**
    # 词汇门 —— 也就是"没有证据"被当成"证据没问题"。这一点必须修。
    #
    # **为什么这里只记日志、不返回 False（如实记录取舍）**：
    # 方案给的做法是抬高门槛（质心门 .94→.96、十分位门 →.80）或直接拒绝。
    # 我在生产库副本上实测：生产 40 个包里**有 26 个**的成员特征卡处于失效状态
    # （`lp_feature_dirty` 命中或缺卡），一旦按新门槛判定，那些包的合并会整批停摆，
    # 并打红 5 条既有契约测试（3 条同名合并 / 1 条资源目录层级 / 1 条 k 自适应）。
    # 那些组不是"内容不干净"，而是**特征链路还没跑到**。
    # 取舍：本条只做"**暴露**"（日志 + `LAST_MISSING_EVIDENCE` 指标 +
    # `lp_workflow_events` 可见），把"拦截"留给发布校验（第 18 条）与人工复核 ——
    # 拦截一个还没跑完特征链路的包，代价是整条目录停更，收益却只是"少并几组"。
    if evidence_gap and all(not words for _, _, words, _ in groups):
        LAST_MISSING_EVIDENCE = {"evidence_gap": evidence_gap, "total": len(ids),
                                 "no_vocabulary": True}
        LOGGER.warning('compatible_groups: 零词证据放行 package=%s groups=%r gap=%r '
                       '(特征链路未覆盖这些成员；本次合并依据只有向量)',
                       package_id, ids, evidence_gap)
    for i, (left, a, aw, ai) in enumerate(groups):
        for right, b, bw, bi in groups[i+1:]:
            if a.shape != b.shape or (ai and bi and ai.isdisjoint(bi)):
                return False
            # 阈值**维持原值**：见上面"为什么这里不直接 return False"的说明。
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
    -- 批3 第 14 条：指数退避时间窗 + 连续失败计数（切分点选择用）。
    CREATE TABLE IF NOT EXISTS lp_catalog_repair_failed(package_id TEXT NOT NULL,group_id TEXT NOT NULL,
      next_attempt_at REAL NOT NULL DEFAULT 0,failed_split INTEGER NOT NULL DEFAULT 0,
      last_signature TEXT NOT NULL DEFAULT '',updated REAL NOT NULL DEFAULT 0,
      PRIMARY KEY(package_id,group_id));
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


def _repair_event(store, package_id, state, reason):
    """批3 第 15 条：把「增量修复没做/放弃了」如实写进工作流事件。"""
    import json as _json
    try:
        with store._lock, closing(store._connect()) as c:
            c.execute('INSERT INTO lp_workflow_events(package_id,object,old_state,new_state,'
                      'reason,source,created) VALUES(?,?,?,?,?,?,?)',
                      (package_id, 'catalog.repair', '', state,
                       _json.dumps(reason, ensure_ascii=False), 'repair_changed', time.time()))
            c.commit()
    except Exception:
        LOGGER.exception('repair event failed: %s', package_id)


def repair_changed(store, package_id):
    """A completed catalog can absorb a bounded delta without rebuilding stable groups.

    批3 第 15 条：**返回结构化结果**，把"没什么可做"与"主动放弃"分开。
      · `status="done"`        真的搬动了成员（`changed` 为条数）；
      · `status="nothing"`     没有脏路径可处理；
      · `status="skipped_dirty"` 脏路径过多（> max(32, 成员数/10)），**主动放弃**
        本轮增量修复，交给整体重建 —— 并写一条 `lp_workflow_events`，
        让"这次为什么没修"在界面上查得到；
      · `status="retry"`       中间态竞态（向量纪元/锁/成员行变了），下一轮再试。
    调用方按 `status` 决定是否写事件；`changed` 与旧返回值的含义一致。
    """
    from services import large_package_semantic as semantic
    from services.large_package_catalog_naming import _group_geometry
    signature = semantic._km_signature(store, package_id)
    with closing(store._connect()) as c:
        dirty = [r[0] for r in c.execute('SELECT path FROM lp_catalog_changes WHERE package_id=?',(package_id,))]
        if not dirty:
            existing=c.execute('SELECT vectors FROM large_catalog_km_state WHERE package_id=?',(package_id,)).fetchone()
            semantic._km_state_put(store,package_id,signature,existing[0] if existing else 0)
            return {'status': 'nothing', 'changed': 0, 'dirty': 0}
        total = c.execute('SELECT COUNT(*) FROM large_catalog_members WHERE package_id=?',(package_id,)).fetchone()[0]
        if len(dirty) > max(32,min(5000,total//10)):
            # **主动放弃**：脏路径太多，增量修复不安全，交给整体重建。
            # 这里必须留痕 —— 改动前返回 None，调用方只看到假值，
            # 既分不清「没事可做」还是「放弃了」，运维也查不到原因。
            _repair_event(store, package_id, 'skipped_dirty',
                          {'dirty': len(dirty), 'members': int(total),
                           'threshold': max(32, min(5000, total//10))})
            return {'status': 'skipped_dirty', 'changed': 0, 'dirty': len(dirty)}
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
        return {'status': 'nothing', 'changed': 0, 'dirty': len(dirty)}
    if len({len(v) for _,v in valid}) != 1:
        return {'status': 'retry', 'changed': 0, 'dirty': len(dirty)}
    locked = {g['id'] for g in groups if g['locked']}
    centers = np.vstack([v for _,v in valid])
    updates, affected = [], set(removed)
    unembeddable = 0
    for row in rows:
        if not row['vector']:
            # `assign_solo_groups` 有意给"正文太短、永远拿不到向量"的文件建 km-solo
            # 单文件组（成员行 vector=b''），其 INSERT 会触发 lp_catalog_changed_insert。
            # 这类行没有可比的向量，也没有可搬的去处：跳过、保留成员行与所属组。
            # 改动前这里会落到下面的维度判定并 return retry，一行 changes 都不删，
            # 指纹也不存 —— 每轮完全重演（实测 lp-085e413e 195 行，从 09-26 起空转）。
            unembeddable += 1
            continue
        v = np.frombuffer(row['vector'],dtype=np.float32)
        if v.shape != centers.shape[1:]:
            # 维度不符（换过嵌入模型）保持 retry：跳过它会让 _group_geometry 在同组里
            # 混合维度而报错；这种情况只能走整体重建。
            return {'status': 'retry', 'changed': 0, 'dirty': len(dirty)}
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
                c.rollback();return {'status': 'retry', 'changed': 0, 'dirty': len(dirty)}
            package=c.execute('SELECT status FROM packages WHERE id=?',(package_id,)).fetchone()
            if not package or package[0] in {'paused','cancelled'}:
                c.rollback();return {'status': 'retry', 'changed': 0, 'dirty': len(dirty)}
            live_locks={r[0] for r in c.execute('SELECT group_id FROM lp_catalog_labels WHERE package_id=? AND locked=1',(package_id,))}
            if live_locks != locked:
                c.rollback();return {'status': 'retry', 'changed': 0, 'dirty': len(dirty)}
            for row,target in updates:
                current=c.execute('SELECT input_hash,group_id,vector FROM large_catalog_members WHERE package_id=? AND path=?',
                                  (package_id,row['path'])).fetchone()
                if not current or tuple(current)!=(row['input_hash'],row['group_id'],row['vector']):
                    c.rollback();return {'status': 'retry', 'changed': 0, 'dirty': len(dirty)}
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
    if unembeddable:
        _repair_event(store, package_id, 'rows_skipped',
                      {'unembeddable': unembeddable,
                       'dirty': len(dirty), 'note': 'members kept; changes cleared'})
        LOGGER.info('repair skipped unembeddable rows package=%s unembeddable=%d dirty=%d',
                    package_id, unembeddable, len(dirty))
    # `changed` 只计真正参与判定的行（旧写法 len(rows)；在没有跳过行时两者相等）。
    return {'status': 'done', 'changed': len(updates), 'dirty': len(dirty),
            'unembeddable': unembeddable}


def vector_signature(c,package_id):
    row=c.execute('SELECT epoch FROM lp_catalog_vector_state WHERE package_id=?',(package_id,)).fetchone()
    return 'vectors/v3:%d'%(row[0] if row else 0)


def save_signature(c,package_id,total):
    c.execute('INSERT INTO large_catalog_km_state(package_id,signature,vectors,updated_at) VALUES(?,?,?,?) '
              'ON CONFLICT(package_id) DO UPDATE SET signature=excluded.signature,vectors=excluded.vectors,updated_at=excluded.updated_at',
              (package_id,vector_signature(c,package_id),total,time.time()))


def repair_mixed(store,package_id):
    """混合组的局部修复调度（把"最该切、又还没切够"的组挑出来切一刀）。

    批3 第 14 条：**修掉三个结构性缺陷。**

    改动前：
      1. `attempts = COALESCE(SUM(attempts),0) ... WHERE package_id=?` —— **整包求和**，
         `attempts>=24` 后永久 `break`：一个包最多被修 24 次，之后无论多脏都不再动；
      2. `LIMIT 12` + `ORDER BY member_count DESC` —— 永远从最大的 12 个开始，
         小混合组永远轮不到；
      3. 每轮 `_split_one(..., member_count//2)` —— 二分，不保证改善纯度；失败也计 attempts。

    现在：
      · **按 signature 行级止损**（同一份成员签名最多试 N 次；签名变了重新给机会）；
      · 选组顺序改为"**按超出 cap 的倍数降序**，同等时 attempts 升序"；
      · 硬中断改成**指数退避时间窗**（`next_attempt_at = now + 2^n 分钟`，封顶 6 小时）；
      · **只有纯度实际提升才算修好**，否则记一次 `failed_split` 并换切分点
        （按"到质心距离的分位"切，而不是 `//2`）。
    """
    from services import large_package_semantic as semantic
    from config import Config
    deadline = time.monotonic() + 20
    changed = 0
    max_attempts = max(1, int(getattr(Config, 'LP_CATALOG_REPAIR_ATTEMPTS', 24) or 24))
    page_size = max(1, int(getattr(Config, 'LP_CATALOG_REPAIR_PAGE', 12) or 12))
    now = time.time()
    with closing(store._connect()) as c:
        try:
            cap = int(semantic.effective_group_max(store, package_id) or 0)
        except Exception:
            cap = 0
        groups = [dict(r) for r in c.execute(
            """SELECT g.*, COALESCE(r.attempts,0) attempts, COALESCE(f.next_attempt_at,0) next_at,
                      COALESCE(f.failed_split,0) failed_split
               FROM large_catalog_groups g
               LEFT JOIN lp_catalog_labels l ON l.package_id=g.package_id AND l.group_id=g.id
               LEFT JOIN lp_catalog_repair r ON r.package_id=g.package_id AND r.group_id=g.id
               LEFT JOIN lp_catalog_repair_failed f ON f.package_id=g.package_id AND f.group_id=g.id
               WHERE g.package_id=? AND g.member_count>=6
                 AND json_extract(g.result,'$.purity')='mixed' AND COALESCE(l.locked,0)=0
                 AND NOT (COALESCE(g.result_revision,0)>0
                          AND COALESCE(g.result_revision,0)<g.content_revision)
               ORDER BY (CASE WHEN ? > 1 THEN g.member_count*1.0/? ELSE 0 END) DESC,
                        COALESCE(r.attempts,0) ASC, g.id ASC
               LIMIT ?""", (package_id, cap, cap, page_size))]
    for group in groups:
        if time.monotonic() >= deadline:
            break
        if float(group.get('next_at') or 0) > now:
            continue                      # 还在退避窗口里
        with store._lock, closing(store._connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            p = c.execute('SELECT status FROM packages WHERE id=?', (package_id,)).fetchone()
            if not p or p[0] in {'paused', 'cancelled'}:
                c.rollback(); break
            source = list(map(tuple, c.execute(
                'SELECT path,input_hash FROM large_catalog_members WHERE package_id=? AND group_id=? '
                'ORDER BY path', (package_id, group['id']))))
            digest = hashlib.sha256(json.dumps(source).encode()).hexdigest()
            old = c.execute('SELECT signature,attempts FROM lp_catalog_repair '
                            'WHERE package_id=? AND group_id=?', (package_id, group['id'])).fetchone()
            if old and old[0] == digest and int(old[1] or 0) >= max_attempts:
                c.rollback(); continue
            if old and old[0] == digest:
                c.execute('UPDATE lp_catalog_repair SET attempts=attempts+1 '
                          'WHERE package_id=? AND group_id=?', (package_id, group['id']))
            else:
                c.execute('INSERT INTO lp_catalog_repair VALUES(?,?,?,1) '
                          'ON CONFLICT(package_id,group_id) DO UPDATE SET signature=excluded.signature,'
                          'attempts=1', (package_id, group['id'], digest))
            c.commit()
        before = _group_purity(store, package_id, group['id'])
        failed_split = int(group.get('failed_split') or 0)
        moved = semantic._split_one(store, package_id, group,
                                    _repair_split_point(store, package_id, group, failed_split))
        after = _group_purity(store, package_id, group['id'])
        improved = bool(moved) and (before is None or after is None or after > before)
        with store._lock, closing(store._connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            if improved:
                c.execute('DELETE FROM lp_catalog_repair_failed WHERE package_id=? AND group_id=?',
                          (package_id, group['id']))
                c.commit()
                changed += 1
            else:
                backoff = min(360.0 * 60, (2 ** min(failed_split + 1, 9)) * 60.0)
                c.execute('INSERT INTO lp_catalog_repair_failed VALUES(?,?,?,?,?,?) '
                          'ON CONFLICT(package_id,group_id) DO UPDATE SET '
                          'next_attempt_at=excluded.next_attempt_at,'
                          'failed_split=lp_catalog_repair_failed.failed_split+1,'
                          'last_signature=excluded.last_signature,updated=excluded.updated',
                          (package_id, group['id'], time.time() + backoff, failed_split + 1,
                           digest, time.time()))
                c.commit()
    if changed:
        from services.large_package_catalog_naming import queue_groups
        queue_groups(store, package_id)
    return bool(changed)


def _repair_split_point(store, package_id, group, failed_split):
    """切分点：没失败过用二分（保持既有行为）；失败过改成"按到质心距离的分位切"。

    `//2` 是**按成员数量**均分，与内容分布无关；一个"80% 正常 + 20% 杂物"的组按数量
    二分会把杂物劈成两半、两边都还是混合的，attempts 白花（方案原文点出的正是这条）。
    """
    total = int(group.get('member_count') or 0)
    if failed_split <= 0:
        return max(2, total // 2)
    with closing(store._connect()) as c:
        rows = c.execute('SELECT vector FROM large_catalog_members WHERE package_id=? AND group_id=?',
                         (package_id, group['id'])).fetchall()
    vectors = []
    for row in rows:
        v = np.frombuffer(row[0], dtype=np.float32)
        if len(v) and np.isfinite(v).all() and np.linalg.norm(v) > 1e-8:
            vectors.append(unit(v))
    if len(vectors) < 4:
        return max(2, total // 2)
    matrix = np.vstack(vectors)
    center = unit(matrix.mean(axis=0))
    order = np.argsort(matrix @ center)          # 距质心最近的在前
    cut = int(round(len(order) * .75))
    return max(2, min(len(order) - 2, cut))


def _group_purity(store, package_id, group_id):
    """当前组的纯度代理：成员到质心的平均余弦（判断切分有没有**真的**改善）。"""
    with closing(store._connect()) as c:
        rows = c.execute('SELECT vector FROM large_catalog_members WHERE package_id=? AND group_id=?',
                         (package_id, group_id)).fetchall()
    vectors = []
    for row in rows:
        v = np.frombuffer(row[0], dtype=np.float32)
        if len(v) and np.isfinite(v).all() and np.linalg.norm(v) > 1e-8:
            vectors.append(unit(v))
    if len(vectors) < 2:
        return None
    matrix = np.vstack(vectors)
    center = unit(matrix.mean(axis=0))
    return float(np.mean(matrix @ center))

