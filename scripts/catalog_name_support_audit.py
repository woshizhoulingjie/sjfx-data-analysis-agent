#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""命名支撑审计：用**留出成员**定向验证名称是否有支撑（专家报告 §4.4 / §14-D）。

为什么要它：我们实测过三条"零调用"的支撑检查在本语料上**都没有判别力**——
  · 名称向量 vs 组质心：最佳阈值准确率 71%，42% 真名被误判；
  · 名称词面 vs 正文证据：真名命中率中位 0.143，50% 真名被误判；
  · 名称词面 vs 结构化档案词表：真名命中率中位 0.000，53% 真名被误判。
原因是模型本来就在改写与概括，而这类语料的余弦基线高达 0.84。
剩下唯一可行的是**定向模型判断**——但它是生成调用，所以必须先量化"多久出一次问题"，
再决定要不要把它接进流水线。本脚本就是那个量化工具。

做法：抽样若干组，把**没有参与起草该名称的成员**（按路径散列取，排除被引用证据）
的短片段给模型，只问一句："这些材料支持这个名称吗？" 只输出布尔与一句理由。

用法（在项目根目录；只读数据库、只调本机模型，不写任何数据）：
    .venv/bin/python -m scripts.catalog_name_support_audit --limit 20
    .venv/bin/python -m scripts.catalog_name_support_audit --limit 40 --json /tmp/support.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from contextlib import closing

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

AUDIT_SYSTEM = (
    '你是资料目录名称的验收员。输入是若干材料片段与一个已生成的目录名称。'
    '只回答一件事：这些材料与这个名称**是不是同一个主题**。'
    '名称是对整组的概括：允许改写，允许只覆盖整组的一部分，'
    '片段少、片段短、片段看起来像表头或目录，都**不构成**"不一致"。'
    '只有当材料讲的是**另一件事**、或者它们之间根本看不出共同主题时，才判不一致。'
    '只输出JSON：{"consistent":true,"reason":"20字以内说明"}')


def sample_groups(db_path, limit, low, high, per_package):
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT g.package_id, g.id, g.member_count, g.content_revision, g.anchor,"
        " l.name, l.description, l.needs_review "
        "FROM large_catalog_groups g JOIN lp_catalog_labels l "
        "  ON l.package_id=g.package_id AND l.group_id=g.id "
        "WHERE (l.revision=g.content_revision OR l.locked=1) "
        "  AND g.member_count BETWEEN ? AND ? AND length(g.anchor)>0 "
        "  AND l.name NOT LIKE '%待细分%' AND l.name NOT LIKE '%待补充%' "
        "ORDER BY g.member_count DESC LIMIT ?", (low, high, limit * 4)).fetchall()
    conn.close()
    buckets = {}
    for row in rows:
        buckets.setdefault(row['package_id'], []).append(dict(row))
    picked = []
    for package_id, items in buckets.items():
        picked += items[:per_package]
    return picked[:limit]


def heldout_texts(store, group, exclude, count):
    """取**没有参与起草名称**的成员片段（按路径散列稳定抽样）。

    每个成员取最多两段、丢掉过短的片段并优先较长的：只取"第一段"时经常拿到
    表头，会让审计误判成"材料无内容"（首轮实测就踩过这个偏差）。
    """
    with closing(store._connect()) as conn:
        paths = [str(r[0]) for r in conn.execute(
            'SELECT path FROM large_catalog_members WHERE package_id=? AND group_id=?',
            (group['package_id'], group['id']))]
        paths = [p for p in paths if p not in exclude]
        if not paths:
            return []
        paths.sort(key=lambda p: hashlib.sha256(p.encode('utf-8')).hexdigest())
        chosen = paths[:count * 3]
        marks = ','.join('?' for _ in chosen)
        rows = conn.execute('SELECT path, local_summary FROM files WHERE package_id=? AND path IN (%s)'
                            % marks, [group['package_id']] + chosen).fetchall()
    candidates = []
    for raw in rows:
        row = store._row(raw)
        info = (row.get('local_summary') or {}).get('portrait') or {}
        parts = [p for p in (info.get('evidence') or []) if isinstance(p, dict) and p.get('text')]
        text = ' '.join(str(p.get('text') or '') for p in parts[:2]).strip()
        if len(text) >= 80:
            candidates.append(text[:260])
    candidates.sort(key=len, reverse=True)
    return candidates[:count]


def main(argv=None):
    parser = argparse.ArgumentParser(description="命名支撑审计（留出成员定向验证）")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--min-members", type=int, default=5)
    parser.add_argument("--max-members", type=int, default=150)
    parser.add_argument("--heldout", type=int, default=4)
    parser.add_argument("--per-package", type=int, default=6)
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)

    from services.large_package_isolated import LargePackageStore
    from services import large_package_catalog_naming as naming
    from services.model_output import extract_json_value

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(root)
    store = LargePackageStore()
    groups = sample_groups(store.db_path, args.limit, args.min_members, args.max_members,
                           args.per_package)
    if not groups:
        raise SystemExit("没有抽到组")
    client = naming.client()
    print("模型=%s 待审计组数=%d 每组留出片段=%d\n" % (client.model, len(groups), args.heldout))

    results = []
    for group in groups:
        material = naming.bundle(store, group['package_id'], group['id'])
        cited = {e['source_path'] for e in material['evidence']}
        留出 = heldout_texts(store, group, cited, args.heldout)
        if not 留出:
            print("跳过（没有留出成员素材）:", group['id'])
            continue
        payload = json.dumps({'name': group['name'], 'description': group['description'] or '',
                              'materials': 留出}, ensure_ascii=False)
        started = time.monotonic()
        try:
            response = client.chat(AUDIT_SYSTEM, payload, temperature=0.0, max_tokens=80,
                                   retries=0, timeout=60)
            value = extract_json_value(response.get('content', ''))
            # 判据是"同不同一个主题"，字段名用 consistent；兼容首轮的 supported。
            supported = bool(value.get('consistent', value.get('supported')))
            reason = str(value.get('reason') or '')[:40]
        except Exception as exc:
            supported, reason = None, '审计失败：%s' % str(exc)[:40]
        elapsed = time.monotonic() - started
        results.append({'group': group['id'], 'package': group['package_id'][-12:],
                        'members': group['member_count'], 'name': group['name'],
                        'supported': supported, 'reason': reason, 'needs_review': group['needs_review'],
                        'seconds': round(elapsed, 2)})
        flag = {True: '一致', False: '**不一致**', None: '失败'}[supported]
        print("  [%s] 成员=%-4d 复核=%d %s" % (flag, group['member_count'],
                                             group['needs_review'], group['name'][:44]))
        print("        %s  (%.1fs)" % (reason, elapsed))

    ok = [r for r in results if r['supported'] is not None]
    bad = [r for r in ok if not r['supported']]
    print("\n=== 汇总 ===")
    print("  审计成功 %d 组，其中**名称与留出成员不同主题** %d 组（%.0f%%）" % (
        len(ok), len(bad), 100.0 * len(bad) / max(1, len(ok))))
    print("  这些组的 needs_review 标记：%s" % (
        {True: sum(1 for r in bad if r['needs_review']), False: sum(1 for r in bad if not r['needs_review'])}))
    print("  平均耗时 %.2fs/组" % (sum(r['seconds'] for r in ok) / max(1, len(ok))))
    if bad:
        print("\n  不支持清单：")
        for r in bad:
            print("    - %s | %s | %s" % (r['group'], r['name'][:38], r['reason']))
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as handle:
            json.dump(results, handle, ensure_ascii=False, indent=2)
        print("\n  结果已写入 %s" % args.json)


if __name__ == '__main__':
    main()
