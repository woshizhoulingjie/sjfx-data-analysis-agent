#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""分组边界审计：把"分错了的组"**找出来给人看**，不自动修。

为什么要它：命名质量的残余问题，主因不是"模型编名字"，而是**分组边界错**。
我们做过一次定向审计（`scripts/catalog_name_support_audit.py`），39% 的组被模型
判为"名称与留出成员不是同一主题"；逐条看理由，绝大多数是**混合组**
（典型理由："仅含村镇，无河流及铁路站点"），而不是名字编造。再好的命名也救不了
一个把两件事装在一起的组——所以这一层要先被看见。

为什么在这里只检测、不修：修分组要靠模型猜，而"搬动前核对"的几何判据在本语料上
判别力**全为零**（四种方案实测见 `naming.split_off_outliers` 的注释：任意 2–12 份
子集的内聚度都在 0.94 上下，只取决于规模）。没有验证手段时自动重分组只是把
"不知道"变成另一个看起来确定的错误答案。所以这个脚本只输出**可疑清单**。

三条判据（都不含"从某份语料统计出来的常量"）：

  1. `misplaced-members` —— 某个成员离**别的**组的质心比离自己组的还近。
     这是唯一**不需要阈值**的判据：它直接说明"按最近质心它不该在这里"。
     它不常出现也有原因：k-means 若用同一批质心做分配，本判据恒为空。所以一旦
     非空，就说明质心在分配之后被改过（叶子拆分重算几何、R10 回填、增量嵌入、
     新文件并入）——**那些组的边界已经陈旧**，这正是要找的东西。
  2. `twin-centroid` —— 两个组的质心几乎重合（互为最近邻，且相似度位于本包
     最高一档）。同一个主题被切成了两个组，合起来才是一个目录项。
  3. `low-coherence` / `outlier-heavy` —— 组内平均内聚度过低，或"离质心最远的
     那一撮成员"明显脱节。这条**是相对判据**（包内排名），只用来排序、不是判定：
     它按构造总会挑出固定比例的组，报告里必须和绝对数值一起看。

用法（在项目根目录；只读数据库、**零模型调用**、不写任何数据）：
    .venv/bin/python -m scripts.catalog_boundary_audit                 # 全部包
    .venv/bin/python -m scripts.catalog_boundary_audit --package lp-xxx --top 20
    .venv/bin/python -m scripts.catalog_boundary_audit --json /tmp/boundary.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

# 系统桶按设计没有质心、也不参与语义目录，不进入候选。
SYSTEM_PREFIX = ("km-other-", "km-pending-")
# 旧命名路径把文件名/摘要的十六进制 id 直接当名字留下的痕迹。
HEX_NAME = re.compile(r'^[0-9a-f]{16,}$')
BATCH = 4096


def load_groups(conn, package_id):
    """取本包所有**有质心**的组（组 id、成员数、单位质心、当前名称）。"""
    rows = conn.execute(
        "SELECT g.id, g.member_count, g.anchor, g.content_revision,"
        " l.name, l.description, l.needs_review "
        "FROM large_catalog_groups g LEFT JOIN lp_catalog_labels l "
        "  ON l.package_id=g.package_id AND l.group_id=g.id "
        " AND (l.revision=g.content_revision OR l.locked=1) "
        "WHERE g.package_id=? AND length(g.anchor)>0 ORDER BY g.id", (package_id,)).fetchall()
    groups, matrix = [], []
    for row in rows:
        if str(row[0]).startswith(SYSTEM_PREFIX):
            continue
        vector = np.frombuffer(row[2], dtype=np.float32)
        if vector.size == 0 or not np.isfinite(vector).all():
            continue
        norm = float(np.linalg.norm(vector))
        if norm <= 1e-8:
            continue
        groups.append({'id': row[0], 'members': int(row[1] or 0), 'name': row[4] or '',
                       'description': row[5] or '', 'needs_review': int(row[6] or 0)})
        matrix.append(vector / norm)
    if not groups:
        return [], np.zeros((0, 0), dtype=np.float32)
    return groups, np.vstack(matrix).astype(np.float32)


def centroid_distances(matrix):
    """返回 (最近邻下标, 最近邻相似度, 次近邻相似度, 是否互为最近邻)。

    **次近邻**是"最近/次近间隔"判据的一半：最近邻与次近邻的差距大，说明这一组的
    邻居**只有一个**（很可能就是被切开的同一个主题）；差距小说明它处在一片密集的
    邻域里，合并也解决不了问题。两者都不是判定，只是排序线索。
    """
    sim = matrix @ matrix.T
    np.fill_diagonal(sim, -np.inf)
    if len(matrix) >= 2:
        # 取前两大：argpartition 只需 O(n)，全库几千个组也不会成为瓶颈。
        top2 = np.argpartition(-sim, 1, axis=1)[:, :2]
        rows = np.arange(len(sim))
        left = sim[rows, top2[:, 0]]
        right = sim[rows, top2[:, 1]]
        pick_left = left >= right
        nearest = np.where(pick_left, top2[:, 0], top2[:, 1])
        best = np.where(pick_left, left, right)
        second = np.where(pick_left, right, left)
        mutual = nearest[nearest] == rows
    else:
        nearest = np.zeros(len(sim), dtype=np.int64)
        best = np.full(len(sim), -np.inf)
        second = np.full(len(sim), -np.inf)
        mutual = np.zeros(len(sim), dtype=bool)
    return nearest, best, second, mutual


def _phrase_terms(name):
    """从名称里取 4–8 字的汉字串作为"区分性短语"候选。

    为什么用名称而不是画像：画像要给整库 7 千个组各读一次成员正文（本脚本的定位是
    **便宜**的那一半诊断）。名称是现成的，而且"两个组的名字里有同一个少见的 4–8 字串"
    本身就是一个很强的边界信号——同一个主题被切成两个条目时，名字几乎必然撞词。
    """
    terms = []
    for run in re.findall(r'[\u4e00-\u9fff]+', str(name or '')):
        for size in range(8, 3, -1):
            for i in range(max(0, len(run) - size + 1)):
                terms.append(run[i:i + size])
    return set(terms)


def phrase_conflicts(groups, naming):
    """同包内**名称共享少见短语**的组对 → 区分性短语冲突（零模型调用、只读名称）。

    判据：某个 4–8 字短语在**同一个包**里恰好出现在 2–3 个组的名称中。共享的组越多
    越可能是通用词（"研究""资料"），所以只报"少数组共享"的情况；状态名（"内容主题
    不统一（待细分）"这类诊断名）与含泛化词的名字直接排除，它们本来就不承载主题。
    """
    index = {}
    for group in groups:
        name = str(group.get('name') or '')
        if not name or naming.is_status_name(name) or naming._GENERIC_WORD.search(name):
            continue
        for term in _phrase_terms(name):
            index.setdefault(term, set()).add(group['id'])
    conflicts = {}
    for term, ids in index.items():
        if 2 <= len(ids) <= 3:
            for group_id in sorted(ids):
                conflicts.setdefault(group_id, []).append((term, sorted(ids)))
    return conflicts


def scan_members(conn, package_id, groups, matrix):
    """逐成员算"离自己组质心多近、离最好的别的组质心多近"。**这是唯一无阈值判据。**

    分批做矩阵乘（一批 4096 个成员 × 全部组质心），内存只与批大小有关，
    所以十万级文件的包也能跑。p10 用"整包收集后一次分组"算，避免按组反复掩码。
    """
    position = {g['id']: i for i, g in enumerate(groups)}
    total = np.zeros(len(groups), dtype=np.int64)
    misplaced = np.zeros(len(groups), dtype=np.int64)
    gain = np.zeros(len(groups), dtype=np.float64)
    gain_max = np.zeros(len(groups), dtype=np.float64)
    own_sum = np.zeros(len(groups), dtype=np.float64)
    # 落差分布：让"哪些是决定性的、哪些只是差一点点"看得见。这不是判定阈值，
    # 只是把 5,746 个错位成员按"离得更近多少"分档，供人工判断优先级。
    histogram = [0, 0, 0]
    owner_chunks, cos_chunks = [], []
    cursor = conn.execute('SELECT group_id, vector FROM large_catalog_members '
                          'WHERE package_id=? AND length(vector)>0', (package_id,))
    while True:
        rows = cursor.fetchmany(BATCH)
        if not rows:
            break
        vectors, owners = [], []
        for group_id, blob in rows:
            index = position.get(group_id)
            if index is None:
                continue
            raw = np.frombuffer(blob, dtype=np.float32)
            if raw.size != matrix.shape[1]:
                continue
            norm = float(np.linalg.norm(raw))
            if norm <= 1e-8:
                continue
            vectors.append(raw / norm)
            owners.append(index)
        if not vectors:
            continue
        owns = np.asarray(owners)
        cos = np.vstack(vectors).astype(np.float32) @ matrix.T
        own_cos = cos[np.arange(len(owns)), owns]
        best_other = cos.copy()
        best_other[np.arange(len(owns)), owns] = -np.inf
        best_other = best_other.max(axis=1)
        # 严格大于才算"按最近质心放错了"。留 1e-6 容差只是浮点噪声。
        wrong = best_other > own_cos + 1e-6
        np.add.at(total, owns, 1)
        np.add.at(misplaced, owns, wrong.astype(np.int64))
        margin = np.where(wrong, best_other - own_cos, 0.0)
        np.add.at(gain, owns, margin)
        if wrong.any():
            gains = margin[wrong]
            np.maximum.at(gain_max, owns[wrong], gains)      # 该组里最决定性的那一个
            histogram[0] += int((gains < 0.005).sum())
            histogram[1] += int(((gains >= 0.005) & (gains < 0.02)).sum())
            histogram[2] += int((gains >= 0.02).sum())
        np.add.at(own_sum, owns, own_cos)
        owner_chunks.append(owns)
        cos_chunks.append(own_cos)
    p10 = np.full(len(groups), np.nan)
    if owner_chunks:
        owners_all = np.concatenate(owner_chunks)
        cos_all = np.concatenate(cos_chunks)
        order = np.argsort(owners_all, kind='stable')
        owners_sorted = owners_all[order]
        cos_sorted = cos_all[order]
        left = np.searchsorted(owners_sorted, np.arange(len(groups)), side='left')
        right = np.searchsorted(owners_sorted, np.arange(len(groups)), side='right')
        for i in range(len(groups)):
            if right[i] > left[i]:
                p10[i] = float(np.percentile(cos_sorted[left[i]:right[i]], 10))
    stats = []
    for i in range(len(groups)):
        n = int(total[i])
        stats.append({
            'scanned': n,
            'coherence': float(own_sum[i] / n) if n else None,
            'p10': None if np.isnan(p10[i]) else float(p10[i]),
            'misplaced': int(misplaced[i]),
            'gain': float(gain[i] / max(1, int(misplaced[i]))) if misplaced[i] else 0.0,
            'gain_max': float(gain_max[i]),
        })
    return stats, histogram


def percentile_rank(values):
    """每个值在全体里的百分位（0–100，越小越靠前）。用于**相对**判据排序。"""
    array = np.asarray([v if v is not None else np.nan for v in values], dtype=np.float64)
    order = np.argsort(np.where(np.isnan(array), np.inf, array), kind='stable')
    rank = np.empty(len(array), dtype=np.float64)
    rank[order] = np.arange(len(array), dtype=np.float64)
    return 100.0 * rank / max(1, len(array) - 1)


def _weighted(aggregates, key):
    """按组数加权的整体值：小包和大包直接平均会让 2 个组的包和 600 个组的包等权。"""
    total = sum(a['groups'] for a in aggregates if a.get(key) is not None)
    if not total:
        return None
    value = sum(a[key] * a['groups'] for a in aggregates if a.get(key) is not None)
    return round(value / total, 4)


def audit_package(conn, package_id, top, min_members, limit, twin_min=3):
    groups, matrix = load_groups(conn, package_id)
    if not groups:
        return None
    from services import large_package_catalog_naming as naming
    nearest, best, second, mutual = centroid_distances(matrix)
    stats, histogram = scan_members(conn, package_id, groups, matrix)
    eligible = np.asarray([g['members'] >= min_members for g in groups])
    # 旧命名路径留下的"名字就是十六进制 id"的组：不是分组问题，但同样要人看。
    placeholders = sum(1 for g in groups if HEX_NAME.match(g['name'] or ''))
    # 区分性短语冲突（名称层面，零调用）：同包内共享少见 4–8 字短语的组。
    conflicts = phrase_conflicts(groups, naming)
    # 相对判据一律**只在同一包内**排名：不同包的语料、维度、规模都不同，
    # 跨包比较等于把一个包的阈值搬到另一个包（这正是我们反复踩过的坑）。
    rank_sim = percentile_rank([best[i] if eligible[i] else None for i in range(len(groups))])
    # 次近间隔：**越大**越可疑（邻居只有一个），所以比较时用 `100 - top`。
    margins = [float(best[i] - second[i])
               if (eligible[i] and np.isfinite(best[i]) and np.isfinite(second[i])) else None
               for i in range(len(groups))]
    rank_margin = percentile_rank(margins)
    rank_coh = percentile_rank([stats[i]['coherence'] if eligible[i] else None
                                for i in range(len(groups))])
    rank_low = percentile_rank([stats[i]['p10'] if eligible[i] else None
                                for i in range(len(groups))])
    # 疑似"一个主题被切成两组"：互为最近邻、且相似度位于本包最高一档。
    # 单独列出来，因为它是最容易人工确认、也最容易合并的一类。
    #
    # 两边的规模下限不是"质量阈值"而是**可操作性**判断：一两份文件的组对合并了
    # 也没有目录价值，而这类小组成对出现的相似度天然就高（本语料任意两篇的余弦
    # 基线 0.84、小样本子集内聚度 0.94），混在一起只会把真正重复的组对淹掉。
    twins = []
    if len(groups) >= 2:
        for i, group in enumerate(groups):
            partner = int(nearest[i])
            if not (eligible[i] and eligible[partner]) or not mutual[i] or rank_sim[i] > top:
                continue
            if group['members'] < twin_min or groups[partner]['members'] < twin_min:
                continue
            if partner < i:                 # 一对只报一次
                continue
            twins.append({'a': group['id'], 'b': groups[partner]['id'],
                          'package': package_id, 'similarity': round(float(best[i]), 4),
                          'package_p50': round(float(np.median(best[np.isfinite(best)])), 4)
                          if np.isfinite(best).any() else None,
                          'a_name': group['name'], 'b_name': groups[partner]['name'],
                          'a_members': group['members'], 'b_members': groups[partner]['members']})
    twins.sort(key=lambda t: -t['similarity'])
    rows = []
    for i, group in enumerate(groups):
        if not eligible[i]:
            continue
        share = stats[i]['misplaced'] / max(1, stats[i]['scanned'])
        reasons = []
        # ① 无阈值判据：成员更靠近别的组的质心
        if stats[i]['misplaced']:
            reasons.append('misplaced-members(%d/%d, 平均+%.3f 最大+%.3f)'
                           % (stats[i]['misplaced'], stats[i]['scanned'],
                              stats[i]['gain'], stats[i]['gain_max']))
        # ② 互为最近邻 + 相似度在本包最高一档 → 一个主题被切成两组。
        # 单组包（或最近邻算成自己）必须排除：那时 best 是 -inf、mutual 恒为真，
        # 会伪造出"与自己做孪生"的理由（第一版就报了 3 条这样的假阳性）。
        partner = int(nearest[i])
        twin = (len(groups) >= 2 and partner != i and np.isfinite(best[i])
                and mutual[i] and rank_sim[i] <= top)
        if twin:
            reasons.append('twin-centroid(%.4f, 与 %s)' % (best[i], groups[partner]['id']))
        # ③ 相对判据：只用于排序
        if rank_coh[i] <= top:
            reasons.append('low-coherence(%.4f, 包内 p%.0f)' % (stats[i]['coherence'], rank_coh[i]))
        if rank_low[i] <= top:
            reasons.append('outlier-heavy(p10=%.4f, 包内 p%.0f)' % (stats[i]['p10'], rank_low[i]))
        # ③ 最近/次近间隔：邻居**只有一个**、而且那个邻居不弱 → 很可能就是被切开的同一主题。
        # 用包内排名而不是绝对阈值：间隔多大算大取决于这个包有多密。"最近邻也高于本包中位"
        # 这一条是必要的补充——否则"一个弱邻居 + 没有别的邻居"也会被算进来，那不是重复组。
        margin = float(best[i] - second[i]) if np.isfinite(best[i]) and np.isfinite(second[i]) else None
        isolated = (margin is not None and rank_margin[i] >= 100 - top and rank_sim[i] <= 50)
        if isolated:
            reasons.append('single-neighbour(最近 %.4f 比次近 %.4f 高 %.4f)'
                           % (best[i], second[i], margin))
        # ④ 区分性短语冲突：名称层面撞了同包内少见的 4–8 字短语（零调用、只读名称）
        hit = conflicts.get(group['id'])
        if hit:
            term, ids = hit[0]
            reasons.append('phrase-conflict(与 %s 共享「%s」)'
                           % (','.join(x for x in ids if x != group['id'])[:24], term))
        if not reasons:
            continue
        # 排序分数：misplaced 那一项要**同时**看比例与幅度——只看比例时，"20 个成员里
        # 有 1 个偏离 0.0001"会和"3 个成员里 1 个偏离 0.05"同分（第一版把它们排在一起，
        # 前 15 名里混进好几条浮点噪声级的记录）。幅度按报告里同一个"显著"档（0.02）
        # 归一，纯粹为了排序，不构成判定阈值。
        magnitude = min(1.0, max(0.0, stats[i]['gain_max']) / 0.02)
        score = 3.0 * share * magnitude + (2.0 if twin else 0.0) \
            + (1.0 if rank_coh[i] <= top else 0.0) + (1.0 if rank_low[i] <= top else 0.0) \
            + (1.0 if isolated else 0.0) + (1.5 if hit else 0.0)
        rows.append({'package': package_id, 'group': group['id'], 'members': group['members'],
                     'name': group['name'], 'needs_review': group['needs_review'],
                     'scanned': stats[i]['scanned'], 'coherence': stats[i]['coherence'],
                     'nn': float(best[i]) if np.isfinite(best[i]) else None,
                     'nn_second': float(second[i]) if np.isfinite(second[i]) else None,
                     'nn_margin': None if margin is None else round(margin, 4),
                     'rank_nn': float(rank_sim[i]),
                     'rank_coherence': float(rank_coh[i]), 'rank_p10': float(rank_low[i]),
                     'misplaced': stats[i]['misplaced'], 'gain': stats[i]['gain'],
                     'gain_max': stats[i]['gain_max'],
                     'phrase_conflict': (hit[0][0] if hit else None),
                     'score_parts': {'share': round(share, 4), 'magnitude': round(magnitude, 4),
                                     'twin': bool(twin), 'single_neighbour': bool(isolated),
                                     'phrase_conflict': bool(hit)},
                     'reasons': reasons, 'score': round(score, 4)})
    rows.sort(key=lambda r: -r['score'])
    # 每包的**整体**几何：组内一致（内聚度分布）与相邻区分（最近邻相似度分布）。
    # 这两项是验收指标里"组内一致/相邻标签区分"的直接来源——它们本来就要算，
    # 只是以前只用来挑可疑组、没作为分布报出来。
    finite_nn = best[np.isfinite(best)]
    coherence = [x['coherence'] for x in stats if x['coherence'] is not None]
    aggregate = {
        'groups': len(groups),
        'coherence_p10': round(float(np.percentile(coherence, 10)), 4) if coherence else None,
        'coherence_p50': round(float(np.percentile(coherence, 50)), 4) if coherence else None,
        'nn_p50': round(float(np.percentile(finite_nn, 50)), 4) if finite_nn.size else None,
        'nn_p90': round(float(np.percentile(finite_nn, 90)), 4) if finite_nn.size else None,
        'twins': len(twins),
    }
    return {'package': package_id, 'groups': len(groups), 'suspects': rows,
            'shown': rows[:limit], 'twins': twins, 'histogram': histogram,
            'placeholders': placeholders, 'aggregate': aggregate}


def main(argv=None):
    parser = argparse.ArgumentParser(description="分组边界审计（零模型调用、只读）")
    parser.add_argument("--package", action="append", default=None,
                        help="只审计指定包（可重复）；默认全部包")
    parser.add_argument("--top", type=float, default=5.0,
                        help="相对判据的百分位阈值（默认 5 = 只报每包最靠前的 5%%）")
    parser.add_argument("--min-members", type=int, default=2)
    parser.add_argument("--twin-min-members", type=int, default=3,
                        help="报告重复组对时两侧各自的成员数下限（默认 3：一两份文件的组对合并了也没有目录价值）")
    parser.add_argument("--limit", type=int, default=30, help="每个包最多打印多少行")
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)

    from services.large_package_isolated import LargePackageStore
    store = LargePackageStore()
    conn = sqlite3.connect("file:%s?mode=ro" % store.db_path, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=30000')
    packages = args.package or [r[0] for r in conn.execute(
        'SELECT DISTINCT package_id FROM large_catalog_groups ORDER BY package_id')]

    started = time.monotonic()
    report, twins, histogram, placeholders, aggregates = [], [], [0, 0, 0], 0, []
    for package_id in packages:
        result = audit_package(conn, package_id, args.top, args.min_members, args.limit,
                               args.twin_min_members)
        if not result:
            continue
        report.append(result['suspects'])
        twins += result['twins']
        placeholders += result['placeholders']
        aggregates.append(result['aggregate'])
        for i in range(3):
            histogram[i] += result['histogram'][i]
        print("\n=== 包 %s：%d 个有名有质的组 ===" % (package_id, result['groups']))
        misplaced_members = sum(r['misplaced'] for r in result['suspects'])
        print("  可疑 %d 个（占 %.1f%%）；其中按最近质心「放错组」的成员 %d 个" % (
            len(result['suspects']),
            100.0 * len(result['suspects']) / max(1, result['groups']), misplaced_members))
        if result['placeholders']:
            print("  另有 %d 个组的名称就是十六进制 id（旧命名路径遗留，不是分组问题，"
                  "但同样该重命名）" % result['placeholders'])
        if not result['suspects']:
            print("  没有可疑组（三条判据都没命中）")
            continue
        for row in result['shown']:
            print("  %-22s 成员=%-5d %s" % (row['group'], row['members'],
                                            row['name'][:40] or '（无当前名称）'))
            print("      理由：%s" % '；'.join(row['reasons']))
        if len(result['suspects']) > len(result['shown']):
            print("  …… 另有 %d 个可疑组未打印（--limit 可调）"
                  % (len(result['suspects']) - len(result['shown'])))
    conn.close()

    # **全局**按分数重排。每包内部已经排过，但直接把各包的清单首尾相接，会让
    # "第一个包里的弱证据"排在"最后一个包里的强证据"前面（第一版就是这样：
    # 报告的前 15 名里混进了 0.001 分的记录，而库里存在 5.5 分的组）。
    flat = sorted((row for rows in report for row in rows), key=lambda r: -r['score'])
    multi = [r for r in flat if len(r['reasons']) >= 2]
    counts = {}
    for row in flat:
        for reason in row['reasons']:
            key = reason.split('(')[0]
            counts[key] = counts.get(key, 0) + 1
    print("\n=== 汇总 ===")
    print("  可疑组 %d 个（其中**同时命中 ≥2 条判据**的 %d 个，最该先看）" % (len(flat), len(multi)))
    print("  各判据命中次数（按全部可疑组统计）：%s" % (counts or '无'))
    print("\n  === 全库最该先看的 10 个组（按分数=比例×幅度+判据权重）===")
    for row in flat[:10]:
        print("    %-22s 成员=%-5d 分数=%-6s %s" % (row['group'], row['members'], row['score'],
                                                   (row['name'] or '（无名称）')[:30]))
        print("        %s" % '；'.join(row['reasons']))
    if flat:
        print("  命中 misplaced-members 的组 %d 个，涉及成员 %d 个；这些成员"
              "「离别的组更近」的程度分布：" % (
                  sum(1 for r in flat if r['misplaced']), sum(r['misplaced'] for r in flat)))
        print("     <0.005（边际，可能是浮点/质心微调）%d 个；0.005–0.02（明显）%d 个；"
              "≥0.02（显著）%d 个" % tuple(histogram))
    if twins:
        print("\n  === 疑似重复组对（互为最近邻且相似度在本包最高一档、两侧各 ≥%d 个成员）"
              "%d 对 ===" % (args.twin_min_members, len(twins)))
        for twin in twins[:20]:
            print("    %.4f（本包最近邻相似度中位 %.4f）" % (twin['similarity'],
                                                          twin['package_p50'] or 0.0))
            print("        A %s（%d）%s" % (twin['a'], twin['a_members'],
                                           twin['a_name'][:34] or '（无名称）'))
            print("        B %s（%d）%s" % (twin['b'], twin['b_members'],
                                           twin['b_name'][:34] or '（无名称）'))
    if placeholders:
        print("  名称是十六进制 id 的组共 %d 个（旧命名路径遗留）" % placeholders)
    graded = [a for a in aggregates if a['coherence_p50'] is not None]
    if graded:
        print("\n  === 每包整体几何（验收指标「组内一致 / 相邻区分」的来源）===")
        print("    按组数加权的组内内聚度中位 %.4f；最近邻相似度中位 %.4f、p90 %.4f"
              % (_weighted(graded, 'coherence_p50'), _weighted(graded, 'nn_p50'),
                 _weighted(graded, 'nn_p90')))
        print("    参考：内聚度越接近 1 说明组内越同质；最近邻相似度越接近 1 说明**相邻两组越难区分**。")
    print("  注意：twin-centroid/low-coherence/outlier-heavy 是**包内相对排名**，按构造"
          "总会挑出固定比例的组；只有 misplaced-members 是无阈值判据。本清单用于**人工**"
          "决定是否重聚类，脚本不做任何自动修复。")
    print("  耗时 %.1fs" % (time.monotonic() - started))
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as handle:
            json.dump({'suspects': flat, 'twins': twins,
                       'aggregate': {'packages': aggregates,
                                     'coherence_p50_weighted': _weighted(graded, 'coherence_p50'),
                                     'nn_p50_weighted': _weighted(graded, 'nn_p50'),
                                     'nn_p90_weighted': _weighted(graded, 'nn_p90')},
                       'misplaced_gain_histogram': {'lt_0.005': histogram[0],
                                                    '0.005_to_0.02': histogram[1],
                                                    'ge_0.02': histogram[2]}},
                      handle, ensure_ascii=False, indent=2)
        print("  结果已写入 %s" % args.json)


if __name__ == '__main__':
    main()
