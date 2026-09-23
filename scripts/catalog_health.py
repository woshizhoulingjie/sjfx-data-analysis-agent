#!/usr/bin/env python3
"""智能目录体检：一次性看清**所有**包的目录是否健康。

为什么要它：2026-09-19 的教训 —— 命名任务失败两次会被写成 `deferred`，而拾取
只认 `queued`，于是模型短暂不可用时任务永久搁置，叶组拿不到名字，主题层只能在
残缺的叶组集合上生成，其余全部落进「待归并主题」。当时**没有任何一个固定指标
能看出这件事**，是靠人打开页面肉眼发现的。17 个包里坏了 5 个。

用法：
    .venv/bin/python scripts/catalog_health.py            # 全部包
    .venv/bin/python scripts/catalog_health.py --bad-only # 只看有问题的

判据（任一命中即标记）：
    待归并叶组 > 20%      主题层没覆盖大部分叶组
    叶组无名字 > 20%      命名没跑完 / 拿不到名字
    deferred 任务 > 0     有任务被永久搁置（除非 attempts 已达上限）
"""
from __future__ import annotations
import argparse, json, os, sys, time
from contextlib import closing

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.large_package_isolated import LargePackageStore  # noqa: E402


def audit(store, package_id):
    with closing(store._connect()) as c:
        groups = [dict(r) for r in c.execute(
            "SELECT g.id, g.content_revision, g.member_count, l.revision "
            "FROM large_catalog_groups g "
            "LEFT JOIN lp_catalog_labels l ON l.package_id=g.package_id AND l.group_id=g.id "
            "WHERE g.package_id=?", (package_id,))]
        real = [g for g in groups
                if not str(g['id']).startswith(('km-other-', 'km-pending-'))]
        named = [g for g in real
                 if g['revision'] is not None and g['revision'] == g['content_revision']]
        branches = [dict(r) for r in c.execute(
            "SELECT id, name, children FROM lp_catalog_branches WHERE package_id=?",
            (package_id,))]
        known = {str(g['id']) for g in real}
        covered = set()
        for b in branches:
            try:
                children = json.loads(b['children'] or '[]')
            except (TypeError, ValueError):
                children = []
            covered.update(str(x) for x in children if str(x) in known)
        jobs = dict(c.execute(
            "SELECT status, COUNT(*) FROM lp_catalog_jobs WHERE package_id=? GROUP BY status",
            (package_id,)).fetchall())
        # 只算**真正卡住**的：还有重试额度（attempts < 上限）且已经到期。
        # 已经用尽额度的 deferred 是"重试过、确实做不了"，不是 bug；
        # 未到期的只是在退避窗口里，也不是 bug。
        from config import Config as _Cfg
        _cap = max(1, int(getattr(_Cfg, 'LP_NAME_MAX_ATTEMPTS', 5) or 5))
        stuck = c.execute(
            "SELECT COUNT(*) FROM lp_catalog_jobs WHERE package_id=? AND status='deferred' "
            "AND attempts<? AND next_at<=?", (package_id, _cap, time.time())).fetchone()[0]
        # 轮次是否还活着 —— 决定"卡住的 deferred"是不是真问题：
        #   轮次 active  → 任务本该被重试却没动，**是 bug**
        #   轮次已耗尽   → 重试无从谈起，**是预算/需要重建**，不是同一个问题
        round_row = c.execute(
            "SELECT status, deadline, requests, max_requests, tokens, max_tokens "
            "FROM lp_catalog_rounds WHERE package_id=? ORDER BY started DESC LIMIT 1",
            (package_id,)).fetchone()
        rnd = dict(round_row) if round_row else {}
        exhausted = bool(rnd) and (
            rnd.get('status') != 'active'
            or (rnd.get('deadline') or 0) <= time.time()
            or (rnd.get('requests') or 0) >= (rnd.get('max_requests') or 0)
            or (rnd.get('tokens') or 0) >= (rnd.get('max_tokens') or 0))
        files = c.execute("SELECT COUNT(*) FROM files WHERE package_id=?",
                          (package_id,)).fetchone()[0]
    leaves = len(real)
    pending = leaves - len(covered)
    unnamed = leaves - len(named)
    return {
        'files': files, 'leaves': leaves, 'named': len(named),
        'topics': len(branches), 'covered': len(covered),
        'pending': pending, 'pending_pct': (pending / leaves * 100) if leaves else 0.0,
        'unnamed_pct': (unnamed / leaves * 100) if leaves else 0.0,
        'deferred': jobs.get('deferred', 0), 'stuck': stuck, 'queued': jobs.get('queued', 0),
        'round_status': rnd.get('status') or '(无轮次)', 'round_exhausted': exhausted,
        'completed_jobs': jobs.get('completed', 0),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bad-only', action='store_true', help='只列出有问题的包')
    # 叶组数达不到 LP_TOPIC_MERGE_MIN_GROUPS 的包**本来就形不成主题层**，
    # 拿"待归并 100%"去指责它是误报。
    try:
        from config import Config as _Cfg
        _default_min = max(1, int(getattr(_Cfg, 'LP_TOPIC_MERGE_MIN_GROUPS', 12) or 12))
    except Exception:
        _default_min = 12
    ap.add_argument('--min-leaves', type=int, default=_default_min,
                    help='叶组少于该数的包不参与判定（默认取 LP_TOPIC_MERGE_MIN_GROUPS）')
    args = ap.parse_args()

    store = LargePackageStore()
    with closing(store._connect()) as c:
        packages = [dict(r) for r in c.execute(
            "SELECT id, root_path, status, phase FROM packages ORDER BY updated_at DESC")]

    rows, bad = [], 0
    for p in packages:
        try:
            m = audit(store, p['id'])
        except Exception as exc:                     # 单包坏掉不该让整次体检失败
            print("  ! %s 体检失败: %s" % (p['id'][:20], str(exc)[:80]))
            continue
        m.update(id=p['id'], name=os.path.basename(str(p['root_path']).rstrip('/')),
                 status=p['status'], phase=p['phase'])
        problems = []
        if m['leaves'] >= args.min_leaves:
            if m['pending_pct'] > 20:
                problems.append('待归并 %.0f%%' % m['pending_pct'])
            if m['unnamed_pct'] > 20:
                problems.append('无名字 %.0f%%' % m['unnamed_pct'])
            # **deferred 本身不再是"缺陷"** —— 修复之后它们会被正常重试
            # （生产实测失败原因多为名称校验：`目录名称不能使用状态或字段名`、
            # `名称含未见子证据的编号或术语`，属模型产出的质量问题，重试到上限是
            # 预期行为）。只有"叶组没进主题""叶组没名字"才是目录真的坏了。
            # 这里只把 deferred 当作**提示信息**，不据此判定包有问题。
            if m['stuck'] > 0 and m['round_exhausted']:
                problems.append('轮次已耗尽，%d 个任务未完成（需重建）' % m['stuck'])
        m['problems'] = problems
        bad += 1 if problems else 0
        rows.append(m)

    if not args.bad_only:
        print("%-21s %-10s %7s %6s %7s %8s %8s %9s" % (
            "包", "目录", "文件", "叶组", "进主题", "待归并%", "无名字%", "卡住"))
        print("-" * 90)
        for m in rows:
            print("%-21s %-10s %7d %6d %7d %7.1f%% %7.1f%% %9d%s" % (
                m['id'][:20], m['name'][:10], m['files'], m['leaves'], m['covered'],
                m['pending_pct'], m['unnamed_pct'], m['stuck'],
                '   <<< ' + ', '.join(m['problems']) if m['problems'] else ''))
    else:
        for m in rows:
            if m['problems']:
                print("%-21s %-10s 叶组=%-5d 进主题=%-5d 待归并=%.0f%% 无名字=%.0f%% deferred=%d   <<< %s" % (
                    m['id'][:20], m['name'][:10], m['leaves'], m['covered'],
                    m['pending_pct'], m['unnamed_pct'], m['stuck'], ', '.join(m['problems'])))

    print()
    print("结论: %d / %d 个包有目录问题" % (bad, len(rows)))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
