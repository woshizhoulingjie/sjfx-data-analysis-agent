"""Versioned multilevel navigation; every leaf has one parent and keeps its files."""
import hashlib
import json
import math
import os
import time
from contextlib import closing
import logging

LOGGER = logging.getLogger('sjfx.catalog.tree')
import numpy as np
from services.large_package_catalog_quality import unit


# 批次3：同一轮内主题层最多重建几次（每次只花 1~2 个短请求）。
TOPIC_REGENERATION_ATTEMPTS = max(1, int(os.getenv('SJFX_LP_TOPIC_REGEN_ATTEMPTS', '5') or 5))


def init(c):
    c.execute('CREATE TABLE IF NOT EXISTS lp_catalog_tree(package_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,payload TEXT NOT NULL)')
    c.execute('CREATE TRIGGER IF NOT EXISTS lp_tree_delete AFTER DELETE ON packages BEGIN DELETE FROM lp_catalog_tree WHERE package_id=OLD.id; END')


def fingerprint(leaves):
    value=[(x['group_id'],x['content_revision'],x['version'],x['name']) for x in sorted(leaves,key=lambda x:x['group_id'])]
    return hashlib.sha256(('tree/3'+json.dumps(value,ensure_ascii=False)).encode()).hexdigest()


def build(store,package_id,round_state,cancelled=lambda:False,model_client=None):
    from services import large_package_catalog_naming as naming
    from services.large_package_catalog_topics import name_topics
    from services import large_package_catalog_topics as _topics
    from services.large_package_semantic import _kmeans_fit
    with closing(store._connect()) as c:
        leaves=[dict(r) for r in c.execute('''SELECT g.id group_id,g.content_revision,g.anchor,
          g.member_count files,l.name,l.description,l.version FROM large_catalog_groups g JOIN lp_catalog_labels l
          ON l.package_id=g.package_id AND l.group_id=g.id WHERE g.package_id=? AND g.member_count>0
          AND (l.revision=g.content_revision OR l.locked=1) AND g.id NOT LIKE 'km-other-%'
          AND g.id NOT LIKE 'km-pending-%' ORDER BY g.id''',(package_id,))]
        old=c.execute('SELECT fingerprint,payload FROM lp_catalog_tree WHERE package_id=?',(package_id,)).fetchone()
    signature=fingerprint(leaves)
    previous=json.loads(old['payload']) if old else {}
    # **沿用上一版已经验证过的主题名。**
    # node_id = 'topic:' + 该簇 children 的 sha256 前 24 位，同一簇重现时 id 不变，
    # 所以可以按 id 取回旧名字 —— 这是"某次重建没拿到命名预算"时不把正确名字
    # 抹成占位名的第一道保险。
    prev_nodes={str(n.get('group_id')):n for n in (previous.get('nodes') or []) if isinstance(n,dict)}
    carried_over=0
    # 批次3：原来"同一轮内只尝试一次"，于是**一次主题层起名失败就把整棵树上锁**。
    # 在 B1 把 request_key 改成稳定的 "topics" 之后，round_id 不再变化，
    # 这个早退就变成永久早退：主题层停在"内容分组（N个子目录）"兜底名上再也不会重建，
    # 而 _topic_catchup 每分钟还按"落后"记一次日志（实测 lp-68848e2c：15 个节点全部
    # pending、3 分钟内 4 次 catchup 空转）。
    # 语义修正：轮次预算不变，但**同一轮内允许有限次重试**（次数落库，默认 5 次），
    # 超过才停手 —— 既不会无限重置预算，也不会被一次失败永久锁死。
    attempted=int(previous.get('topic_regeneration_attempts') or 0)
    if old and old[0]==signature and previous.get('attempted_round')==round_state['id']:
        if not previous.get('pending_names') or attempted>=TOPIC_REGENERATION_ATTEMPTS:
            return False
    if not leaves or cancelled():return False
    vectors={}
    for leaf in leaves:
        v=np.frombuffer(leaf['anchor'],dtype=np.float32)
        if len(v) and np.isfinite(v).all() and np.linalg.norm(v)>1e-8:vectors[leaf['group_id']]=unit(v)
    model_client=model_client or naming.client()
    frontier=[dict(x,leaf_ids=[x['group_id']],child_nodes=[]) for x in leaves]
    nodes={};terminal=[]
    all_reports=[]
    # No quota-based file movement. A heterogeneous frontier may remain wider.
    # Two semantic parent layers: theme -> subtopic -> leaf directory.
    # Never manufacture a parent for a singleton or unrelated children.
    for level in range(2):
        if cancelled():return False
        eligible=[x for x in frontier if x['group_id'] in vectors]
        terminal.extend(x for x in frontier if x['group_id'] not in vectors)
        if len(eligible)<2:
            terminal.extend(eligible);break
        X=np.vstack([vectors[x['group_id']] for x in eligible])
        k=max(1,int(math.ceil(len(X)/10)))
        centers=_kmeans_fit(X,k,20,seed=23)
        labels=(X@centers.T).argmax(axis=1)
        buckets={}
        for item,lab in zip(eligible,labels):buckets.setdefault(int(lab),[]).append(item)
        accepted={}
        for lab,items in buckets.items():
            center=unit(np.mean([vectors[x['group_id']] for x in items],axis=0))
            support=[float(vectors[x['group_id']]@center) for x in items]
            if len(items)<2 or min(support)<.65:
                terminal.extend(items)
            else:accepted[lab]=items
        if not accepted:break
        # B：如实汇报本次主题命名的结果（拿到几个/命中缓存几个/预算被拒几次/是否被 stop 掐断）。
        # **不改 name_topics 的签名**：契约测试用 lambda 打桩，多一个关键字参数会 TypeError。
        names=name_topics(store,package_id,accepted,vectors,round_state,cancelled,model_client) if model_client else {}
        name_report=dict(_topics.LAST_REPORT.get(package_id) or {})
        # 只采信"本次调用"的回报：被测试打桩时 LAST_REPORT 可能残留上一次真实调用的
        # 结果（或为空），用 round 对齐避免误判成"拿不到预算"而错误拒写。
        if name_report.get('round')!=round_state.get('id'):
            name_report={}
        if name_report.get('refused') or name_report.get('stopped'):
            all_reports.append(name_report)
        next_level=[]
        for lab,items in sorted(accepted.items()):
            children=sorted(x['group_id'] for x in items)
            leaf_ids=sorted(p for x in items for p in x['leaf_ids'])
            gid='topic:'+hashlib.sha256('|'.join(children).encode()).hexdigest()[:24]
            name=names.get(lab)
            # A title supported by only one child must not pretend to describe the parent.
            supported=sum((naming.name_support(name or '',x['name']+' '+x.get('description','')) or 0)>0 for x in items)
            pending_name=not name
            review=pending_name or supported<math.ceil(len(items)*.6)
            # Lexical absence cannot disprove a semantic parent (synonyms and abstractions).
            # Preserve the validated title and show its review status; never replace it just for wording.
            if pending_name:
                # 先沿用上一版同 id 节点的名字；只有**从来没有过名字**时才退占位名。
                # 效果：显示只会"变旧"，不会"变空" —— 本缺陷最直接的止血点。
                prev=prev_nodes.get(gid)
                if prev and not prev.get('name_pending') and str(prev.get('name') or '').strip():
                    name=str(prev['name']);pending_name=False;review=False;carried_over+=1
                else:
                    name='内容分组（%d个子目录）'%len(items)
            scope='；'.join(x['name'] for x in items)[:900]
            node=dict(group_id=gid,node_id=gid,name=name,description=scope,files=sum(x['files'] for x in items),
                      leaf_ids=leaf_ids,children=leaf_ids,child_nodes=children,parent_id=None,depth=0,
                      content_revision=signature,version=1,name_pending=pending_name,status='needs_review' if review else 'current')
            nodes[gid]=node;vectors[gid]=unit(np.mean([vectors[x['group_id']] for x in items],axis=0))
            for item in items:
                if item['group_id'] in nodes:nodes[item['group_id']]['parent_id']=gid
            next_level.append(node)
        if level == 1 or len(next_level)<2:
            terminal.extend(next_level);break
        frontier=next_level
    else:terminal.extend(frontier)

    # ---- 三级架构补全：不许有"没有主题的叶组" --------------------------------
    #
    # 上面每一层都会把"不够紧"的簇（len(items)<2 或 min(support)<.65）和"没有向量"的
    # 叶组丢进 terminal，而它们**从不进入 nodes** —— 于是这些叶组在目录里就没有任何
    # 上级主题，只能挂在根下。生产实测顶层主题只覆盖 140/370、143/340、144/376，
    # 也就是 58~62% 的叶组是孤儿，用户要的三级架构根本不成立。
    #
    # 这里按**向量最近邻**把它们挂到顶层已接受节点下。只做归属：
    #   · 簇（有 child_nodes）→ 作为子节点挂上，并把它的子节点重新认父，深度保持一致；
    #   · 单个叶组 → 直接并入目标节点的 leaf_ids。
    # 被拒的簇**不配新名字**（命名质量门槛不放松），沿用 frontier 上已有的名字；
    # 全程零模型调用。
    def _vector_of(node):
        gid = node.get('group_id')
        if gid in vectors:
            return vectors[gid]
        pool = [vectors[p] for p in (node.get('leaf_ids') or []) if p in vectors]
        if not pool:
            return None
        return unit(np.mean(pool, axis=0))

    # Attach only to a bottom branch, not a root that would skip the middle
    # layer. Recompute every ancestor from direct children afterwards.
    tops = [n for n in nodes.values() if not any(k in nodes for k in n['child_nodes'])]
    attached_clusters = attached_leaves = orphaned = 0
    if tops and terminal:
        for item in terminal:
            gid = item.get('group_id')
            if not gid or gid in nodes:
                continue
            vec = _vector_of(item)
            best, best_sim = None, -2.0
            for top in tops:
                if top.get('group_id') == gid:
                    continue
                tv = vectors.get(top.get('group_id'))
                if tv is None or vec is None:
                    continue
                sim = float(vec @ tv)
                if sim > best_sim:
                    best, best_sim = top, sim
            # **相似度下限：不强迫归属。**
            # 契约测试 test_optional_hierarchy_preserves_unrelated_members_without_forced_parent
            # 明确要求"不适合归并的保留原样"。所以只有确实相近（余弦 >= 与聚类同源的
            # 0.65 门槛）才吸附；语义上不属于任何现有主题的，如实留作未归类，
            # 而不是硬塞进一个不相关的主题下（那会让主题名与内容不符、
            # 也让"证据支持"口径失真）。
            if best is None or best_sim < 0.65:
                orphaned += 1
                continue
            best['leaf_ids'] = sorted(set(best.get('leaf_ids') or []) | set(item.get('leaf_ids') or []))
            best['files'] = int(best.get('files') or 0) + int(item.get('files') or 0)
            if item.get('child_nodes'):
                # 它是一个"被拒的簇"：作为子节点挂上，并让它的子节点重新认父
                nodes[gid] = dict(item, parent_id=best['group_id'], depth=1,
                                  name_pending=bool(item.get('name_pending')))
                best['child_nodes'] = sorted(set(best.get('child_nodes') or []) | {gid})
                for child in item.get('child_nodes') or []:
                    if child in nodes:
                        nodes[child]['parent_id'] = gid
                attached_clusters += 1
            else:
                best['child_nodes'] = sorted(set(best['child_nodes']) | {gid})
                attached_leaves += 1
    if attached_clusters or attached_leaves or orphaned:
        LOGGER.info('theme coverage pass: 归入主题 %d 个簇 + %d 个叶组；无法归属 %d 个 '
                    '（共计 terminal %d）',
                    attached_clusters, attached_leaves, orphaned, len(terminal))

    leaf_by_id = {x['group_id']: x for x in leaves}
    def refresh(node):
        members = set()
        for child in node['child_nodes']:
            if child in nodes:
                members.update(refresh(nodes[child]))
            elif child in leaf_by_id:
                members.add(child)
        node['leaf_ids'] = node['children'] = sorted(members)
        node['files'] = sum(leaf_by_id[k]['files'] for k in members)
        return members
    for node in nodes.values():
        if node['parent_id'] is None:
            refresh(node)
    for node in nodes.values():
        parent=node['parent_id'];depth=0
        while parent:
            depth+=1;parent=nodes[parent]['parent_id']
        node['depth']=depth
    # Lowest branches remain a partition for legacy counters and invariant checks.
    bottom=[n for n in nodes.values() if not any(k in nodes for k in n['child_nodes'])]
    revisions={x['group_id']:x['content_revision'] for x in leaves}
    payload={'nodes':list(nodes.values()),'revisions':revisions,'versions':{x['group_id']:x['version'] for x in leaves},
             'names':{x['group_id']:x['name'] for x in leaves},'attempted_round':round_state['id'],
             # 批次3：同一轮内已尝试次数 —— 换轮次时归零（旧包没有这个键，默认 0）。
             'topic_regeneration_attempts':attempted + 1 if previous.get('attempted_round')==round_state['id'] else 1,
             'pending_names':any(n.get('name_pending') for n in nodes.values())}
    with store._lock,closing(store._connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        p=c.execute('SELECT status,control_state FROM packages WHERE id=?',(package_id,)).fetchone()
        if not p or any(v in {'paused','cancelled'} for v in p) or cancelled():c.rollback();return False
        # **不许用占位名覆盖已经正确的主题名。**
        # 触发条件正是实测到的那一种：本次重建**既没命中缓存、也没成功命名过任何一簇**，
        # 而预算被拒或被 stop 掐断过 —— 即"根本没机会拿到名字"，不是"模型给的名字不合格"。
        # 后者（部分失败）仍照常写：那才是如实的"这几个还没起好名"。
        if all_reports and not carried_over:
            got=sum(r['named']+r['cached'] for r in all_reports)
            if got==0:
                c.rollback()
                LOGGER.warning('theme layer refused to overwrite names: no name obtained '
                               '(refused=%s stopped=%s round=%s) package=%s nodes=%d',
                               sum(r['refused'] for r in all_reports),
                               any(r['stopped'] for r in all_reports),
                               all_reports[0].get('round'), package_id, len(nodes))
                return False
        if carried_over:
            LOGGER.info('theme layer kept %d previously validated name(s) package=%s '
                        '(本次未重新命名，避免覆盖)', carried_over, package_id)
        current=[dict(r) for r in c.execute('''SELECT g.id group_id,g.content_revision,l.version,l.name FROM large_catalog_groups g
          JOIN lp_catalog_labels l ON l.package_id=g.package_id AND l.group_id=g.id WHERE g.package_id=? AND g.member_count>0
          AND (l.revision=g.content_revision OR l.locked=1) AND g.id NOT LIKE 'km-other-%' AND g.id NOT LIKE 'km-pending-%' ''',(package_id,))]
        if fingerprint(current)!=signature:c.rollback();return False
        c.execute('DELETE FROM lp_catalog_branches WHERE package_id=?',(package_id,))
        c.executemany('INSERT INTO lp_catalog_branches VALUES(?,?,?,?,?,?,?)',[(package_id,n['node_id'],n['name'],
             json.dumps(n['leaf_ids']),json.dumps({p:revisions[p] for p in n['leaf_ids']}),round_state['id'],time.time()) for n in bottom])
        c.execute('INSERT OR REPLACE INTO lp_catalog_tree VALUES(?,?,?)',(package_id,signature,json.dumps(payload,ensure_ascii=False)))
        c.execute("UPDATE lp_catalog_rounds SET hierarchy_status='completed' WHERE package_id=? AND id=?",(package_id,round_state['id']))
        c.commit()
    store._catalog_cache.pop(package_id,None)
    return True


def visible(c,package_id):
    record=c.execute('SELECT payload FROM lp_catalog_tree WHERE package_id=?',(package_id,)).fetchone()
    if not record:return None
    payload=json.loads(record[0]);nodes=payload['nodes']
    groups={r['id']:dict(r) for r in c.execute('''SELECT g.id,g.content_revision,l.version,l.name,g.member_count files FROM large_catalog_groups g
      LEFT JOIN lp_catalog_labels l ON l.package_id=g.package_id AND l.group_id=g.id WHERE g.package_id=? AND g.member_count>0''',(package_id,))}
    result=[];covered=set();ids={n['node_id'] for n in nodes}
    for node in nodes:
        children=[p for p in node['children'] if p in groups]
        if not children:continue
        stale=len(children)!=len(node['children']) or any(groups[p]['content_revision']!=payload['revisions'].get(p)
              or groups[p]['version']!=payload['versions'].get(p) or groups[p]['name']!=payload.get('names',{}).get(p) for p in children)
        result.append(dict(node,children=children,files=sum(groups[p]['files'] for p in children),status='stale' if stale else node['status']))
        covered.update(children)
    alive={n['node_id'] for n in result}
    for n in result:
        n['child_nodes']=[i for i in n['child_nodes'] if i in alive or i in groups]
    missing=sorted(set(groups)-covered)
    if missing:result.append(dict(node_id='branch:unassigned',name='独立主题与待归并目录',children=missing,child_nodes=missing,
                parent_id=None,depth=0,files=sum(groups[p]['files'] for p in missing),status='pending'))
    return result


def retry_due(payload, latest_round=None):
    """A saved pending name may retry, but never beyond its per-round cap."""
    return bool(payload.get('pending_names') and (
        int(payload.get('topic_regeneration_attempts') or 0)<TOPIC_REGENERATION_ATTEMPTS
        or (latest_round and latest_round!=payload.get('attempted_round'))))


def stale(c,package_id):
    record=c.execute('SELECT fingerprint,payload FROM lp_catalog_tree WHERE package_id=?',(package_id,)).fetchone()
    if not record:return None
    leaves=[dict(r) for r in c.execute('''SELECT g.id group_id,g.content_revision,l.version,l.name FROM large_catalog_groups g
      JOIN lp_catalog_labels l ON l.package_id=g.package_id AND l.group_id=g.id WHERE g.package_id=? AND g.member_count>0
      AND (l.revision=g.content_revision OR l.locked=1) AND g.id NOT LIKE 'km-other-%' AND g.id NOT LIKE 'km-pending-%' ''',(package_id,))]
    if record['fingerprint']!=fingerprint(leaves):return True
    payload=json.loads(record['payload'])
    latest=c.execute('SELECT id FROM lp_catalog_rounds WHERE package_id=? ORDER BY started DESC LIMIT 1',(package_id,)).fetchone()
    return retry_due(payload,latest[0] if latest else None)
