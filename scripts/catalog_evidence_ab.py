#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""取文改造的验收工具：同一批真实组，"改动前取文" vs "改动后取文"各命名一次。

为什么需要它：改造的质量收益不能靠感觉判断。本脚本用**改动前的模块**（从
.codex-backups 的备份文件动态加载）与当前模块分别构建证据包、各调一次模型，
并排输出名称与四项可自动统计的指标，供人工盲评。

用法（在项目根目录，服务不需要重启；只读数据库、不写任何生产数据）：
    .venv/bin/python -m scripts.catalog_evidence_ab --limit 8
    .venv/bin/python -m scripts.catalog_evidence_ab --package-likes %a4ac25b3,%69068249 --limit 10
    .venv/bin/python -m scripts.catalog_evidence_ab --legacy <备份的 naming.py 路径>

输出：并排名称 + 汇总（泛化词比例、占位名比例、平均 token、平均耗时）。
写库？不写。只调用本机模型并读数据库。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
import time
from contextlib import closing

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_BACKUP_GLOB = ".codex-backups"
GENERIC_WORDS = ('汇编', '资料', '条目', '概况', '简介', '概览', '杂项', '综合')
PLACEHOLDER_WORDS = ('无共同主题', '缺乏共同主题', '需审查', '需要审查', '杂乱')


def find_legacy(root):
    """找到最早的那份改动前备份（`.codex-backups/l1-l3-evidence-*/`）。"""
    base = os.path.join(root, DEFAULT_BACKUP_GLOB)
    if not os.path.isdir(base):
        return None
    candidates = []
    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name, "services", "large_package_catalog_naming.py")
        if os.path.isfile(path):
            candidates.append((name, path))
    return candidates[0][1] if candidates else None


def load_module(path, name="legacy_naming"):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pick_groups(db_path, package_likes, limit, low, high):
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    conn.row_factory = sqlite3.Row
    groups = []
    per_package = max(1, limit // max(1, len(package_likes)))
    for like in package_likes:
        for row in conn.execute(
                "SELECT package_id,id,member_count,content_revision,"
                " (SELECT name FROM lp_catalog_labels l WHERE l.package_id=g.package_id "
                "  AND l.group_id=g.id) name "
                "FROM large_catalog_groups g WHERE g.package_id LIKE ? "
                "AND member_count BETWEEN ? AND ? "
                "ORDER BY member_count DESC LIMIT ?", (like, low, high, per_package)):
            groups.append(dict(row))
    conn.close()
    return groups


def name_once(client, material, max_tokens):
    from services.model_output import extract_json_value
    started = time.monotonic()
    response = client.chat(material['system'], material['user'], temperature=0.1,
                           max_tokens=max_tokens, retries=0, timeout=120)
    elapsed = time.monotonic() - started
    try:
        value = extract_json_value(response.get('content', ''))
        return str(value.get('name') or ''), str(value.get('description') or ''), elapsed, True
    except Exception:
        return '', '', elapsed, False


def main(argv=None):
    parser = argparse.ArgumentParser(description="取文改造的改动前/后 A/B 对照")
    parser.add_argument("--legacy", default=None, help="改动前的 naming.py 路径（默认自动找备份）")
    parser.add_argument("--package-likes", default="%a4ac25b3,%69068249",
                        help="要抽样的包（SQL LIKE，逗号分隔）")
    parser.add_argument("--limit", type=int, default=8, help="对照的组数")
    parser.add_argument("--min-members", type=int, default=20)
    parser.add_argument("--max-members", type=int, default=120)
    parser.add_argument("--max-tokens", type=int, default=260)
    parser.add_argument("--json", default=None, help="把结果写到该 JSON 文件")
    args = parser.parse_args(argv)

    from config import Config
    from services.large_package_isolated import LargePackageStore
    from services import large_package_catalog_naming as naming

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(root)
    legacy_path = args.legacy or find_legacy(root)
    if not legacy_path or not os.path.isfile(legacy_path):
        raise SystemExit("找不到改动前的模块，请用 --legacy 指定备份里的 naming.py")
    legacy = load_module(legacy_path)
    print("改动前模块 %s (VERSION=%s)" % (legacy_path, getattr(legacy, 'VERSION', '?')))
    print("改动后模块 VERSION=%s" % naming.VERSION)

    store = LargePackageStore()
    groups = pick_groups(store.db_path, [x for x in args.package_likes.split(',') if x],
                         args.limit, args.min_members, args.max_members)
    if not groups:
        raise SystemExit("没有抽到任何组，请调整 --package-likes / --min-members")
    client = naming.client()
    print("模型=%s 并发=%d 待测组数=%d\n" % (client.model, naming.naming_concurrency(), len(groups)))

    rows = []
    for group in groups:
        pid, gid = group['package_id'], group['id']
        print("=" * 100)
        print("组 %s 成员=%d 当前线上名称=%r" % (gid, group['member_count'], group['name']))
        try:
            new_material = naming.bundle(store, pid, gid)
        except Exception as exc:
            print("  新证据包构建失败：%s" % exc)
            continue
        try:
            old_material = legacy.bundle(store, pid, gid)
        except Exception as exc:
            print("  旧证据包构建失败：%s" % exc)
            old_material = None
        profile = (json.loads(new_material['user']).get('group_profile') or {})
        terms = [term for term, _ in (profile.get('distinctive') or [])[:10]]
        print("  组画像(改后才有)：%s" % ('、'.join(terms) or '（无）'))

        entry = {'group_id': gid, 'members': group['member_count'], 'profile_terms': terms}
        for tag, material in (('legacy', old_material), ('current', new_material)):
            if material is None:
                continue
            name, desc, elapsed, ok = name_once(client, material, args.max_tokens)
            entry[tag] = {
                'name': name, 'description': desc, 'ok': ok, 'seconds': round(elapsed, 2),
                'tokens': material['tokens'],
                'generic': any(word in name for word in GENERIC_WORDS),
                'placeholder': any(word in name for word in PLACEHOLDER_WORDS),
            }
            print("  【%s】%s" % ('改动前' if tag == 'legacy' else '改动后', name or '(解析失败)'))
            print("       %s" % (desc or '—')[:90])
            print("       token=%d 耗时=%.1fs 泛化词=%s 占位=%s" % (
                material['tokens'], elapsed, entry[tag]['generic'], entry[tag]['placeholder']))
        rows.append(entry)

    print("\n" + "=" * 100)
    print("汇总")
    for tag in ('legacy', 'current'):
        values = [row[tag] for row in rows if tag in row]
        if not values:
            continue
        label = '改动前' if tag == 'legacy' else '改动后'
        print("  %s：泛化词 %d/%d，占位名 %d/%d，平均 token %.0f，平均耗时 %.1fs" % (
            label, sum(1 for v in values if v['generic']), len(values),
            sum(1 for v in values if v['placeholder']), len(values),
            sum(v['tokens'] for v in values) / len(values),
            sum(v['seconds'] for v in values) / len(values)))
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as handle:
            json.dump(rows, handle, ensure_ascii=False, indent=2)
        print("  结果已写入 %s" % args.json)


if __name__ == '__main__':
    main()
