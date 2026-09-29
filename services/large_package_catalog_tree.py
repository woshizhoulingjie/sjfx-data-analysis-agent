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


# 批3 第 3 条 B：同一主题在两版之间"还算同一个"的成员重合度下限。
TOPIC_INHERIT_JACCARD = 0.5


def _best_prev_by_jaccard(prev_nodes, children, threshold=TOPIC_INHERIT_JACCARD):
    """在上一版主题节点里找成员 Jaccard 最高且 ≥threshold 的那个（确定性取最大）。

    为什么必须有它：id 锚点化（A）只能减少抖动，不能让"成员换了几个"的簇仍被认成
    同一个主题。改动前 `prev_nodes.get(gid)` 未命中就直接退占位名 —— 用户看到的是
    "一批内容分组（N个子目录）"。按成员重合度继承才是真正的止血点。
    并列时按 `(jaccard, node_id)` 取最大，保证同样输入给出同样结果。
    """
    if not children:
        return None
    target = set(str(c) for c in children)
    best, best_score = None, 0.0
    for node in prev_nodes:
        if not isinstance(node, dict):
            continue
        members = set(str(x) for x in (node.get('child_nodes') or node.get('children') or []))
        if not members:
            continue
        score = len(target & members) / float(len(target | members) or 1)
        if score < threshold:
            continue
        key = (score, str(node.get('node_id') or ''))
        if best is None or key > best_score:
            best, best_score = node, key
    return best


def _group_topics(eligible, vectors, leaf_mean, threshold):
    """把**主题节点**收成上级主题（第 2 层及以上专用）。

    为什么不能沿用第 1 层的 k-means + 0.65 门槛：
      · mean-pooled 句向量强烈各向异性，原始空间里**毫不相干**的主题两两余弦也在
        0.87~0.98（实测 lp-b09821d4：医学/政策/AI/气候/生物医学五个主题）。
        0.65 门槛在这个空间里恒为真，于是"把整层收成一个上级"的兜底路径**必然触发**，
        用户看到的就是一个"生物医学机制与人工智能应用研究"大筐装下全部 78 个叶组。
      · 第 2 层的 k 仍按"叶组数/每主题叶组数"算，但这一层的输入是**主题**不是叶组，
        5 个主题 → round(5/12)=0 → k=1，聚类本身不提供任何分组信息。
    做法：先减去全包叶组的均值（与 semantic._project 同一口径），在去均值空间里做
    平均连接的层次合并；只有相似度 >= threshold 的主题才共享上级，并且**绝不产生
    一个覆盖本层全部主题的上级**（那样的上级不提供任何导航信息）。
    返回 {标签: [item,...]}，只含 >=2 个成员的组；其余主题保持平级。
    """
    def centered(item):
        pool=[vectors[g] for g in (item.get('leaf_ids') or [item['group_id']]) if g in vectors]
        if not pool:
            return None
        v=np.mean(pool,axis=0)-leaf_mean
        n=float(np.linalg.norm(v))
        return v/n if n>1e-8 else None
    items=sorted(eligible,key=lambda x:x['group_id'])
    vecs={}
    for it in items:
        v=centered(it)
        if v is not None:
            vecs[it['group_id']]=v
    ids=[it['group_id'] for it in items if it['group_id'] in vecs]
    if len(ids)<3:
        # 两个主题收一个上级 = 上级覆盖本层全部，不提供信息
        return {}
    clusters=[[g] for g in ids]
    def link(a,b):
        return float(np.mean([vecs[x]@vecs[y] for x in a for y in b]))
    while len(clusters)>2:
        best=None
        for i in range(len(clusters)):
            for j in range(i+1,len(clusters)):
                if len(clusters[i])+len(clusters[j])>=len(ids):
                    continue
                score=link(clusters[i],clusters[j])
                key=(score,-i,-j)
                if best is None or key>best[0]:
                    best=(key,i,j)
        if best is None or best[0][0]<threshold:
            break
        _,i,j=best
        clusters[i]=sorted(clusters[i]+clusters[j]);del clusters[j]
    by_id={it['group_id']:it for it in items}
    groups=[sorted(c) for c in clusters if len(c)>=2]
    groups.sort()
    return {lab:[by_id[g] for g in grp] for lab,grp in enumerate(groups)}


def build(store,package_id,round_state,cancelled=lambda:False,model_client=None):
    from config import Config
    from services import large_package_catalog_naming as naming
    from services.large_package_catalog_topics import name_topics
    from services import large_package_catalog_topics as _topics
    from services.large_package_semantic import _kmeans_fit
    with closing(store._connect()) as c:
        leaves=[dict(r) for r in c.execute('''SELECT g.id group_id,g.content_revision,g.anchor,
          g.member_count files,l.name,l.description,l.version FROM large_catalog_groups g JOIN lp_catalog_labels l
          ON l.package_id=g.package_id AND l.group_id=g.id WHERE g.package_id=? AND g.member_count>0
          AND (l.revision=g.content_revision OR l.locked=1) AND '''+naming.system_group_sql('g.id')+
          ' ORDER BY g.id',(package_id,))]
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
    # 去语料均值的参照点：只用叶组向量（此刻 vectors 里还没有主题节点）。
    leaf_mean=np.mean(list(vectors.values()),axis=0) if vectors else None
    parent_min_sim=float(getattr(Config,'LP_TOPIC_PARENT_MIN_SIM',0.5))
    centered_mode=bool(getattr(Config,'LP_TOPIC_CENTERED',True))
    frontier=[dict(x,leaf_ids=[x['group_id']],child_nodes=[]) for x in leaves]
    nodes={};terminal=[]
    all_reports=[]
    # 批2 第 6 条：覆盖率账本。`rejected` 记被剔除的离群成员，
    # `coverage_rounds` 记每轮的 (k, eligible, accepted)，用于
    # "接受率 <0.75 就把原因写进事件表"。
    rejected=[]
    coverage_rounds=[]
    # No quota-based file movement. A heterogeneous frontier may remain wider.
    # Two semantic parent layers: theme -> subtopic -> leaf directory.
    # Never manufacture a parent for a singleton or unrelated children.
    for level in range(2):
        if cancelled():return False
        eligible=[x for x in frontier if x['group_id'] in vectors]
        terminal.extend(x for x in frontier if x['group_id'] not in vectors)
        if len(eligible)<2:
            terminal.extend(eligible);break
        if level>=1 and centered_mode:
            # 上级主题：去均值空间里的层次合并（见 _group_topics 的说明）。
            # 不走 k-means、不走"整层收成一个上级"的兜底 —— 这两条在原始向量空间里
            # 恒成立，正是"医学、政策、AI 被装进同一个大主题"的根因。
            k=0
            accepted=_group_topics(eligible,vectors,leaf_mean,parent_min_sim) if leaf_mean is not None else {}
            names=name_topics(store,package_id,accepted,vectors,round_state,cancelled,model_client) if (model_client and accepted) else {}
            name_report=dict(_topics.LAST_REPORT.get(package_id) or {})
            coverage_rounds.append({'level': level, 'k': 0, 'eligible': len(eligible),
                                    'accepted': sum(len(v) for v in accepted.values()),
                                    'clusters': len(accepted), 'mode': 'centered_agglomerative',
                                    'threshold': parent_min_sim})
            if not accepted:
                terminal.extend(eligible);break
            _grouped={x['group_id'] for v in accepted.values() for x in v}
            terminal.extend(x for x in eligible if x['group_id'] not in _grouped)
        else:
            X=np.vstack([vectors[x['group_id']] for x in eligible])
            # 批2 第 1 条：**顶层 k 由"叶组数 ÷ 每个主题想放的叶组数"决定，并 clamp 到
            # [LP_TOPIC_MIN, LP_TOPIC_MAX]。**
            #
            # 改动前是 `k = ceil(len(X)/10)` —— 与规模无关的硬编码 10，也没有任何上限。
            # 819 个叶组 → k=82 个"主题"；16 个叶组 → k=2。
            #
            # **k 下限只在"输入本来就够大"时才生效**（这一步很重要，方案第 1 条的
            # 第一步明确要求"不动层数、放开层数留到第 2 条/批4"）：
            # 本层的输入是上一层的 frontier —— 第 1 层是叶组，第 2 层是**主题节点**。
            # 如果无条件套 `max(6, ...)`，那么只有 3~11 个叶组的小包也会被推着去聚
            # 第二层（原来 k=1、第 2 层根本不会发生），凭空多出一个上级节点，
            # 并改变"第 2 层是否会调用 name_topics" —— 实测直接打红 3 条既有契约
            # （`test_catalog_next_stage` 的三条重试测试，在批1 基线上全绿）。
            # 判据：输入点数 ≥ 2×下限才用下限，否则退化成单簇（与旧行为一致）。
            per=max(3,int(getattr(Config,'LP_TOPIC_LEAVES_PER_TOPIC',12) or 12))
            floor=int(getattr(Config,'LP_TOPIC_MIN',6) or 6)
            ceil_=int(getattr(Config,'LP_TOPIC_MAX',24) or 24)
            if len(X) >= 2*floor:
                k=max(floor,min(ceil_,int(round(len(X)/float(per)))))
            else:
                k=max(1,min(ceil_,int(round(len(X)/float(per)))))
            # `k` 也不能超过"这一层参与聚类的成员数"：sklearn 会为多余的中心产生空簇。
            k=max(1,min(k,len(X)))
            # **在去语料均值空间里分簇**（与 semantic._project 同一口径）。
            # 原始 mean-pooled 向量里无关叶组两两余弦中位数 0.907，k-means 的划分几乎只
            # 由噪声决定：实测 lp-b09821d4 换一个 seed，"气候变化"就从医学簇跳进生物簇，
            # 生物医学预印本被并进 AI 主题。去均值后同一数据在不同 seed 下都稳定分出
            # 政策 / 气候 / 生物医学 / 医学与卫生经济 / AI 几大块。
            # 只改"怎么分"，不改"收不收"：下面的 support 与 .65 门槛仍按原始向量计算，
            # 既有契约不变。SJFX_LP_TOPIC_CENTERED=0 可整体回退到旧行为。
            Xk=X
            if centered_mode and leaf_mean is not None and k>=2:
                Xk=X-leaf_mean
                _n=np.linalg.norm(Xk,axis=1,keepdims=True)
                Xk=np.where(_n>1e-8,Xk/np.maximum(_n,1e-8),X).astype(np.float32)
            centers=_kmeans_fit(Xk,k,20,seed=23)
            labels=(Xk@centers.T).argmax(axis=1)
            buckets={}
            for item,lab in zip(eligible,labels):buckets.setdefault(int(lab),[]).append(item)
            accepted={}
            for lab,items in buckets.items():
                # 批2 第 6 条：**一个弱成员不能否决整个簇，剔除离群后重判。**
                #
                # 改动前只要簇里有一个成员的"到质心余弦"<0.65，**整簇被拆散**塞进
                # terminal（不成为主题）；而离群成员的归属随 k 变化 → 同一份输入两次
                # 构建可能给不同的簇判死刑 → 目录漂移。
                #
                # 现在按 support 升序逐个剔除最低的，直到 `min(support)>=.65` 或剩余 <2；
                # 被剔除的进 terminal（不丢成员，只是不参与这个主题）。**并列必须确定性**：
                # 排序键用 `(support, group_id)`，不只看浮点值。
                if len(items)<2:
                    # 单成员簇：**也记账**，否则"接受 + 剔除 = 参与聚类的成员数"对不上。
                    if items:
                        rejected.append((items[0]['group_id'], None))
                    terminal.extend(items)
                    continue
                kept=list(items)
                # **批3 修正：k=1（整层当一个簇）时，绝不在这一层剔除离群。**
                #
                # 实测背景：3 个互不相关的叶组在 k=1 下会被聚成"1 个簇"，
                # 质心落在三个正交向量的均值上 → 三个成员的 support 都 ≈0.577 <0.65，
                # 于是离群剔除会把其中两个留下、一个踢进 terminal；第 2 层再把这两个
                # 收成一个占位名的上级 —— 结果 `visible()` 里第一分支变成占位名节点，
                # 契约 `test_optional_hierarchy_preserves_unrelated_members_without_forced_parent`
                # 断言"三个叶组应保持三个并列主题、点进去能看到全部 3 个文件"（实测 2 != 3）。
                #
                # 语义上：k=1 表示"这一层没有被分成多组"，聚类本身没有提供任何分组信息，
                # 此时按"离群"剔除成员等于**凭质心算术硬拆一组**，正是方案第 6 条要避免的
                # "结果依赖 k" 那类漂移。所以 k<2 时整层原样留作 terminal（与改动前一致）。
                if k < 2:
                    terminal.extend(items)
                    continue
                while len(kept)>=2:
                    center=unit(np.mean([vectors[x['group_id']] for x in kept],axis=0))
                    scored=sorted(((float(vectors[x['group_id']]@center),x['group_id'],x) for x in kept),
                                  key=lambda row:(row[0],row[1]))
                    if scored[0][0]>=.65:
                        break
                    dropped=scored[0][2]
                    rejected.append((dropped['group_id'],round(scored[0][0],4)))
                    kept=[row[2] for row in scored[1:]]
                if len(kept)>=2:
                    accepted[lab]=kept
                else:
                    terminal.extend(kept)
            # 批3 修正：**兜底那条路径也要把它的簇交给模型命名 —— 但只在它确实会成节点时。**
            #
            # 实测背景（两个契约互相拉扯，最后收敛到这里）：
            #   · `test_catalog_next_stage` 的两条重试测试要求"重试到最后一次时名字真的
            #     落到节点上"（"工业设备健康管理"）。原来 `accepted` 为空就在
            #     `if not accepted` 处 break，兜底节点永远停在占位名 → 那两条必红；
            #   · 但也不能无条件多调一次：`test_optional_hierarchy_...` 断言
            #     `naming.state()['requests']==3`（三次 `run_name()` 各自一次），
            #     多发一次请求就变成 4 → 那条必红。
            # 判据：**只有当兜底簇真能通过相似度门槛、确实会成为一个节点时，才为它发这一次
            # 命名请求**（下面 `_fallback_parent` 用的是同一个门槛）。
            _ids_here = [x['group_id'] for x in eligible]
            _fallback_ok = False
            if not accepted and len(eligible) >= 2 and _ids_here:
                _center = unit(np.mean([vectors[g] for g in _ids_here], axis=0))
                _fallback_ok = min(float(vectors[g] @ _center) for g in _ids_here) >= .65
            naming_buckets = accepted if accepted else ({0: list(eligible)} if _fallback_ok else {})
            names=name_topics(store,package_id,naming_buckets,vectors,round_state,cancelled,model_client) if model_client else {}
            name_report=dict(_topics.LAST_REPORT.get(package_id) or {})
            if name_report.get('round')!=round_state.get('id'):
                name_report={}
            if name_report.get('refused') or name_report.get('stopped'):
                all_reports.append(name_report)
            if not accepted:
                # 中间层不许被"每簇点太少"吃掉（见批2 第 1 条的 k 下限）：本层输入 4 个点、
                # k 被 clamp 到 ≥6 时，sklearn 会把每个点单独分一簇，全部被拒 →
                # `accepted` 为空 → 中间层整层消失。
                #
                # **但"不许消失"不等于"无条件硬造一个上级"**（批3 修正）：
                # 契约 `test_optional_hierarchy_preserves_unrelated_members_without_forced_parent`
                # 明确要求"彼此不相关的成员不要硬塞上级"。所以这里与 `attach` 那段用**同一个
                # 相似度门槛**：先把本层当成一个簇算 support，只有全都 ≥0.65 才收成一个上级
                # （它们确实同类）；否则如实留作平级，不制造层级。
                # **不许凭空造层级，但"该有中间层时也不能消失"。**
                #
                # 契约 `test_optional_hierarchy_preserves_unrelated_members_without_forced_parent`
                # 要求"彼此不相关的成员不要硬塞上级"；而 `test_three_levels_below_old_twelve_branch_cutoff`
                # 要求"叶组很多、主题层很少时仍要有中间层"。两者用**同一个相似度门槛**区分：
                # 先把本层当成一个簇算 support，只有全都 ≥0.65 才收成一个上级（它们确实同类）；
                # 否则如实留作平级。
                # （实测记录：我一度在这里加了 `len(eligible) >= LP_TOPIC_MIN` 的闸，
                #  结果把三级架构那条测试打红 —— 叶组数够多时中间层必须存在，这道闸是错的。）
                if len(eligible) >= 2:
                    ids_here = [x['group_id'] for x in eligible]
                    center = unit(np.mean([vectors[g] for g in ids_here], axis=0))
                    support = {g: float(vectors[g] @ center) for g in ids_here}
                    if min(support.values()) >= .65:
                        eligible_ids = sorted(ids_here)
                        gid = 'topic:' + hashlib.sha256('|'.join(eligible_ids).encode()).hexdigest()[:24]
                        prev = prev_nodes.get(gid)
                        # 名字来源优先级：上一版同 id 节点的已验证名字 → **本次模型给的名字**
                        # （`naming_buckets` 里那个兜底簇，label=0）→ 占位名。
                        model_name = str(names.get(0) or '').strip()
                        name = ''
                        pending_name = True
                        if model_name:
                            name = model_name; pending_name = False
                        elif prev and not prev.get('name_pending') and str(prev.get('name') or '').strip():
                            name = str(prev['name']); pending_name = False; carried_over += 1
                        else:
                            name = '内容分组（%d个子目录）' % len(eligible)
                        leaf_ids = sorted(p for x in eligible for p in x['leaf_ids'])
                        node = dict(group_id=gid, node_id=gid, name=name,
                                    description='；'.join(x['name'] for x in eligible)[:900],
                                    files=sum(x['files'] for x in eligible), leaf_ids=leaf_ids,
                                    children=leaf_ids, child_nodes=eligible_ids, parent_id=None, depth=0,
                                    content_revision=signature, version=1, name_pending=pending_name,
                                    # 批3 修正：占位名节点如实标成 **'pending'**，而不是
                                    # 'needs_review'。'needs_review' 在 `visible()` 里排在前面的
                                    # 候选位置，会让它顶掉 `branch:unassigned` 成为 `branches[0]`
                                    # —— 实测打红 `test_optional_hierarchy_preserves_unrelated_members_without_forced_parent`
                                    # （它断言 branches[0] 是未归类分支、status='pending'）。
                                    # 语义上也更准：这个名字**还没拿到**，是"待命名的占位"，
                                    # 而不是"有名字待复核"。
                                    status='pending' if pending_name else 'current')
                        nodes[gid] = node
                        vectors[gid] = unit(np.mean([vectors[g] for g in eligible_ids], axis=0))
                        for item in eligible:
                            if item['group_id'] in nodes:
                                nodes[item['group_id']]['parent_id'] = gid
                        coverage_rounds.append({'level': level, 'k': int(k), 'eligible': len(eligible),
                                                'accepted': len(eligible), 'clusters': 1,
                                                'collapsed_to_parent': True,
                                                'min_support': round(min(support.values()), 4)})
                    else:
                        # 不满意相似度门槛：不造层级，如实记账（便于事后查"为什么没有中间层"）
                        coverage_rounds.append({'level': level, 'k': int(k), 'eligible': len(eligible),
                                                'accepted': 0, 'clusters': 0,
                                                'collapsed_to_parent': False,
                                                'min_support': round(min(support.values()), 4)})
                break
            coverage_rounds.append({'level': level, 'k': int(k), 'eligible': len(eligible),
                                    'accepted': sum(len(v) for v in accepted.values()),
                                    'clusters': len(accepted)})
        # 批3 记录：我一度在这里加过"本层只出一个簇就不再往上收一层"的闸门，
        # 实测**打红三条**（`test_three_levels_below_old_twelve_branch_cutoff`、
        # `test_attached_leaf_is_visible_selectable_and_counted`、
        # `test_recursive_tree_selection_export_and_stale_visibility`），已删除。
        # 方案第 1 条第一步的原话是"**不动层数**"，所以这里不再新增任何
        # "要不要多一层"的判断 —— 中间层的形状问题整套留给第 2 条 / 批4。
        # （命名调用已经提前到 `if not accepted` 之前执行，这里不再重复调用。）
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
            # 批3 第 3 条 A：**id 锚点化。**
            # 改动前 id 是"簇内**全部**成员 id 的哈希"，任何一个成员换簇 gid 就变，
            # `prev_nodes[gid]` 未命中 → 回落到占位名"内容分组（N个子目录）"。
            # 现在只取**字典序最小的 3 个成员**做锚点：少量成员进出不再改 id。
            gid='topic:'+hashlib.sha256('|'.join(children[:3]).encode()).hexdigest()[:24]
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
                # 批3 第 3 条 B：先按（锚点化的）id 命中；不中再按**成员 Jaccard** 继承。
                prev=prev_nodes.get(gid)
                if prev is None:
                    prev=_best_prev_by_jaccard(previous.get('nodes') or [], children)
                if prev and not prev.get('name_pending') and str(prev.get('name') or '').strip():
                    name=str(prev['name']);pending_name=False;review=False;carried_over+=1
                else:
                    name='内容分组（%d个子目录）'%len(items)
            scope='；'.join(x['name'] for x in items)[:900]
            # 批3 修正：**没拿到名字的节点 status 是 'pending'，不是 'needs_review'。**
            # `needs_review` 表示"有名字、但需要人复核"；占位名节点是"这个名字还没拿到"。
            # 而且 `needs_review` 在 `visible()` 里会顶掉未归类分支成为 `branches[0]`，
            # 实测打红 `test_optional_hierarchy_preserves_unrelated_members_without_forced_parent`。
            node=dict(group_id=gid,node_id=gid,name=name,description=scope,files=sum(x['files'] for x in items),
                      leaf_ids=leaf_ids,children=leaf_ids,child_nodes=children,parent_id=None,depth=0,
                      content_revision=signature,version=1,name_pending=pending_name,
                      status='pending' if pending_name else ('needs_review' if review else 'current'))
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
             'pending_names':any(n.get('name_pending') for n in nodes.values()),
             # 批2 第 6 条：把"这次聚类到底覆盖了多少"如实存下来（可见 + 可断言）。
             'coverage':coverage_rounds,
             'rejected_outliers':[{'group_id':g,'support':s} for g,s in rejected[:200]]}
    # 批2 第 6 条：覆盖率下限。接受率 <0.75 说明这一轮的 k 可能选错了 ——
    # 记一条工作流事件（reason 带接受率与 k），让"目录为什么这么碎"有据可查。
    if coverage_rounds and len(leaves)>=4:
        best=max(coverage_rounds,key=lambda r:r['accepted']/max(1,r['eligible']))
        rate=best['accepted']/float(max(1,best['eligible']))
        if rate<0.75:
            try:
                with store._lock,closing(store._connect()) as c:
                    c.execute('INSERT INTO lp_workflow_events(package_id,object,old_state,new_state,'
                              'reason,source,created) VALUES(?,?,?,?,?,?,?)',
                              (package_id,'catalog.theme','','low_coverage',
                               json.dumps({'acceptance':round(rate,4),'k':best['k'],
                                           'eligible':best['eligible'],'accepted':best['accepted'],
                                           'rounds':coverage_rounds},ensure_ascii=False),
                               'catalog_tree',time.time()))
                    c.commit()
            except Exception:
                LOGGER.exception('coverage event failed: %s', package_id)
            LOGGER.warning('theme coverage low package=%s acceptance=%.2f k=%d '
                           '(接受率 <0.75，k 可能选错)', package_id, rate, best['k'])
    with store._lock,closing(store._connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        p=c.execute('SELECT status,control_state FROM packages WHERE id=?',(package_id,)).fetchone()
        if not p or any(v in {'paused','cancelled'} for v in p) or cancelled():c.rollback();return False
        # **不许用占位名覆盖已经正确的主题名。**
        # 触发条件正是实测到的那一种：本次重建**既没命中缓存、也没成功命名过任何一簇**，
        # 而预算被拒或被 stop 掐断过 —— 即"根本没机会拿到名字"，不是"模型给的名字不合格"。
        # 后者（部分失败）仍照常写：那才是如实的"这几个还没起好名"。
        #
        # 批3 第 3 条 C：**保护与"同轮重试"必须共存。**
        #
        # 改动前是 `got==0 and not carried_over` —— 只要有一个主题继承了名字就整体放行，
        # 于是"15 个主题里 1 个继承、14 个退成占位名"这种**更糟**的情况被静默通过。
        # 但单纯收紧成"占位名过半就整批回滚"又会掐死同一轮的重试：
        # 批次3 的契约 `test_worker_stops_same_round_parent_retries_at_persisted_limit`
        # 要求"第一轮用占位名落库 → 第二轮继续重试 → 到上限才停"，走的正是
        # "占位名落库"这条路（`name_topics` 打桩返回空，调用方本来就没有名字）。
        #
        # 两者共存的判据（两轮实测收敛出来的最终口径）：
        #   · **只要这一轮还有重试额度 → 允许占位名落库**（下一轮会补上名字）；
        #     这条让"同轮重试"的既有契约继续成立；
        #   · **重试额度用尽、且上一版是有名字的 → 拒绝覆盖**
        #     （避免"重试彻底失败"把一批已经验证过的真名字换成占位名）；
        #   · **重试额度用尽、但上一版本来就是占位名 → 照常落库**
        #     （没有任何东西可保护，写下去只是把"尝试次数"如实记下来，
        #      否则 `attempted_round`/`topic_regeneration_attempts` 永远不落库，
        #      调度侧会一直认为"该重试"，反而变成无限重试）。
        placeholder_nodes=sum(1 for n in nodes.values() if n.get('name_pending'))
        named_nodes=len(nodes)-placeholder_nodes
        attempts_now=(attempted+1 if previous.get('attempted_round')==round_state['id'] else 1)
        retries_left=attempts_now<TOPIC_REGENERATION_ATTEMPTS
        prev_was_pending=bool(previous.get('pending_names'))
        if nodes and not retries_left and placeholder_nodes>named_nodes and not prev_was_pending:
            c.rollback()
            LOGGER.warning('theme layer refused to overwrite validated names with placeholders: '
                           'placeholders %d > named %d, retries exhausted (attempt=%d/%d) '
                           'package=%s nodes=%d',
                           placeholder_nodes, named_nodes, attempts_now,
                           TOPIC_REGENERATION_ATTEMPTS, package_id, len(nodes))
            return False
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
          AND (l.revision=g.content_revision OR l.locked=1) AND '''+naming.system_group_sql('g.id')+' ''',(package_id,))]
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
    if missing:
        # 批1 第 28 条：name / node_id 取自 hierarchy 的**唯一常量**。
        from services.large_package_catalog_hierarchy import (UNASSIGNED_BRANCH_NAME,
                                                              UNASSIGNED_NODE_ID)
        result.append(dict(node_id=UNASSIGNED_NODE_ID,name=UNASSIGNED_BRANCH_NAME,children=missing,child_nodes=missing,
                    parent_id=None,depth=0,files=sum(groups[p]['files'] for p in missing),status='pending'))
    return result


def retry_due(payload, latest_round=None):
    """A saved pending name may retry, but never beyond its per-round cap."""
    return bool(payload.get('pending_names') and (
        int(payload.get('topic_regeneration_attempts') or 0)<TOPIC_REGENERATION_ATTEMPTS
        or (latest_round and latest_round!=payload.get('attempted_round'))))


def stale(c,package_id):
    from services import large_package_catalog_naming as naming
    record=c.execute('SELECT fingerprint,payload FROM lp_catalog_tree WHERE package_id=?',(package_id,)).fetchone()
    if not record:return None
    leaves=[dict(r) for r in c.execute('''SELECT g.id group_id,g.content_revision,l.version,l.name FROM large_catalog_groups g
      JOIN lp_catalog_labels l ON l.package_id=g.package_id AND l.group_id=g.id WHERE g.package_id=? AND g.member_count>0
      AND (l.revision=g.content_revision OR l.locked=1) AND '''+naming.system_group_sql('g.id')+' ''',(package_id,))]
    if record['fingerprint']!=fingerprint(leaves):return True
    payload=json.loads(record['payload'])
    latest=c.execute('SELECT id FROM lp_catalog_rounds WHERE package_id=? ORDER BY started DESC LIMIT 1',(package_id,)).fetchone()
    return retry_due(payload,latest[0] if latest else None)
