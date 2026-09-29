#!/usr/bin/env python3
"""提速方案 §4 / §6.1 的只读统计：批次 0 采集 24～48 小时之后跑一次，决定后续批次的阈值。

只读打开生产库（mode=ro），不写任何东西。

用法：
    .venv/bin/python scripts/catalog_speed_stats.py                 # 默认最近 2 天
    .venv/bin/python scripts/catalog_speed_stats.py --days 1
    .venv/bin/python scripts/catalog_speed_stats.py --package lp-xxx --jaccard

输出四段：
  ① 各路径（single/batch/leaf-merge…）× route 的调用数、token、输出字符数、截断率
  ② 缓存未命中原因分布，以及 revision_changed 的占比 —— 决定批次 3a 值不值得做
     （方案 §4：≥15% 值得做，<5% 只做缓存键统一）
  ③ 重建触发源（lp_catalog_rebuild_events）分布
  ④ --jaccard：revision_changed 的组里，当前成员与"命名时指纹"的估算 Jaccard 分布
     —— 决定批次 3a 的沿用阈值（方案 §6.1 第 3 条）
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_DB = os.path.expanduser('~/.local/state/sjfx-data-analysis-agent/large_package.db')


def connect(path):
    c = sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def has_table(c, name):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                          (name,)).fetchone())


def calls_section(c, since, package):
    sql = 'SELECT package_id,status,elapsed,detail FROM lp_catalog_calls WHERE started>=?'
    args = [since]
    if package:
        sql += ' AND package_id=?'
        args.append(package)
    rows = list(c.execute(sql, args))
    stats = defaultdict(lambda: {'n': 0, 'trunc': 0, 'p_in': [], 'p_out': [], 'chars': [],
                                 'elapsed': [], 'groups': 0})
    misses = Counter()
    miss_total = 0
    measured = 0
    for r in rows:
        try:
            d = json.loads(r['detail'] or '{}')
        except (TypeError, ValueError):
            d = {}
        if 'output_chars' in d or 'cache_miss' in d or 'finish_reason' in d:
            measured += 1
        key = (str(d.get('mode') or '?'), str(d.get('route') or '-'))
        s = stats[key]
        s['n'] += 1
        s['groups'] += len(d.get('published') or []) + len(d.get('failed') or [])
        if d.get('finish_reason') == 'length':
            s['trunc'] += 1
        for field, bucket in (('prompt_tokens', 'p_in'), ('completion_tokens', 'p_out'),
                              ('output_chars', 'chars')):
            if isinstance(d.get(field), (int, float)):
                s[bucket].append(d[field])
        if isinstance(r['elapsed'], (int, float)):
            s['elapsed'].append(r['elapsed'])
        for info in (d.get('cache_miss') or {}).values():
            reason = info.get('reason') if isinstance(info, dict) else str(info)
            misses[reason or 'unknown'] += 1
            miss_total += 1

    def avg(v):
        return ('%.0f' % (sum(v) / len(v))) if v else '-'

    print('== ① 调用（%d 条，其中带第 1 批测量字段 %d 条）' % (len(rows), measured))
    if measured == 0 and rows:
        print('   ！这段时间的调用都没有测量字段：第 1 批还没生效（worker 未重启）或命名一直空闲。')
    print('   %-12s %-10s %6s %7s %7s %7s %8s %7s' % ('mode', 'route', 'calls', 'p_in', 'p_out',
                                                   'chars', 'elapsed', 'trunc'))
    for (mode, route), s in sorted(stats.items()):
        print('   %-12s %-10s %6d %7s %7s %7s %8s %6.1f%%' % (
            mode, route, s['n'], avg(s['p_in']), avg(s['p_out']), avg(s['chars']),
            ('%.1fs' % (sum(s['elapsed']) / len(s['elapsed']))) if s['elapsed'] else '-',
            100.0 * s['trunc'] / max(1, s['n'])))
    over = [(k, s) for k, s in stats.items() if s['n'] >= 20 and s['trunc'] / s['n'] > 0.02]
    if over:
        print('   → 有 route 截断率 >2%%：%s —— 方案 §7 第 5 条的按 route 分配预算值得做。'
              % ', '.join('%s/%s' % k for k, _ in over))

    print('\n== ② 缓存未命中原因（%d 个组次）' % miss_total)
    for reason, n in misses.most_common():
        print('   %-20s %6d  %5.1f%%' % (reason, n, 100.0 * n / max(1, miss_total)))
    if miss_total:
        share = misses.get('revision_changed', 0) / miss_total
        verdict = ('≥15%，批次 3a 名字继承值得打开' if share >= 0.15 else
                   '<5%，批次 3a 只做缓存键统一即可' if share < 0.05 else
                   '5%～15%，收益有限，看 ④ 的 Jaccard 分布再定')
        print('   revision_changed 占比 %.1f%% → %s' % (100 * share, verdict))
    return rows


def events_section(c, since, package):
    print('\n== ③ 重建触发源')
    if not has_table(c, 'lp_catalog_rebuild_events'):
        print('   (表还不存在)')
        return
    sql = ('SELECT trigger,COUNT(*) n,SUM(groups_touched) g FROM lp_catalog_rebuild_events '
           'WHERE at>=?')
    args = [since]
    if package:
        sql += ' AND package_id=?'
        args.append(package)
    rows = list(c.execute(sql + ' GROUP BY trigger ORDER BY n DESC', args))
    if not rows:
        print('   (这段时间没有成员重排)')
    for r in rows:
        print('   %-12s 次数=%-5d 涉及组=%d' % (r['trigger'], r['n'], r['g'] or 0))


def jaccard_section(c, package):
    from services.large_package_catalog_naming import estimate_jaccard, minhash_bytes
    print('\n== ④ revision_changed 组的成员相似度（当前成员 vs 命名时指纹）')
    if not has_table(c, 'lp_catalog_label_members'):
        print('   (指纹表还不存在)')
        return
    sql = ('SELECT l.package_id,l.group_id,m.member_count,m.minhash FROM lp_catalog_labels l '
           'JOIN large_catalog_groups g ON g.package_id=l.package_id AND g.id=l.group_id '
           'JOIN lp_catalog_label_members m ON m.package_id=l.package_id AND m.group_id=l.group_id '
           'WHERE l.revision!=g.content_revision AND l.locked=0')
    args = []
    if package:
        sql += ' AND l.package_id=?'
        args.append(package)
    rows = list(c.execute(sql, args))
    if not rows:
        print('   (没有"有指纹且修订号已变"的组：指纹只在批次 0 重启后才开始写，需要先等命名跑过)')
        return
    buckets = Counter()
    drift_ok = 0
    for r in rows:
        paths = [str(x[0]) for x in c.execute(
            'SELECT path FROM large_catalog_members WHERE package_id=? AND group_id=?',
            (r['package_id'], r['group_id']))]
        sim = estimate_jaccard(r['minhash'], minhash_bytes(paths)) or 0.0
        old = int(r['member_count'] or 0)
        if old and abs(len(paths) - old) <= max(1, int(old * 0.10)):
            drift_ok += 1
        edge = next(b for b in (0.95, 0.9, 0.8, 0.6, 0.3, 0.0) if sim >= b)
        buckets[edge] += 1
    total = len(rows)
    for edge in (0.95, 0.9, 0.8, 0.6, 0.3, 0.0):
        print('   Jaccard ≥ %.2f  %5d  %5.1f%%' % (edge, buckets[edge], 100.0 * buckets[edge] / total))
    print('   成员数变化 ≤10%%：%d / %d' % (drift_ok, total))
    hi = buckets[0.95] + buckets[0.9]
    print('   → 阈值 0.9 下可沿用约 %.1f%%（还要同时满足成员数漂移 ≤10%%）' % (100.0 * hi / total))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--days', type=float, default=2.0)
    ap.add_argument('--package', default=None)
    ap.add_argument('--jaccard', action='store_true', help='计算 ④（逐组读成员，大包较慢）')
    a = ap.parse_args()
    since = time.time() - a.days * 86400
    c = connect(a.db)
    print('统计窗口：%s 起（%.1f 天）%s' % (time.strftime('%m-%d %H:%M', time.localtime(since)),
                                      a.days, ('，包 ' + a.package) if a.package else ''))
    calls_section(c, since, a.package)
    events_section(c, since, a.package)
    if a.jaccard:
        jaccard_section(c, a.package)


if __name__ == '__main__':
    main()
