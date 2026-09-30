# -*- coding: utf-8 -*-
"""目录发布校验口径修正（SJFX_LP_CATALOG_VALIDATE_FIX）的回归测试。

生产 2026-09-30：6 个包的全包综合研究方向已算好，但被 validate_catalog 阻断发布：
  A. INV-2：单文件条目（km-solo-*）/系统桶成员没有向量，被算成"无归属"；
  B. INV-3：同一文件的不同副本各自成了同名单文件条目，被当成"同一主题下重名"；
  C. INV-5：已命名的 km-pending-* 组被主题层按设计排除，却被要求"必须在主题下"。
开关关闭时必须保持原口径；真正的问题（同主题同名、无名占位、树外普通叶组）照旧阻断。
"""
import json
import time
from contextlib import closing
from unittest.mock import patch

import numpy as np

import test_catalog_next_stage as hierarchy_fixture
from config import Config
from services import large_package_catalog_naming as naming


def _case():
    case = hierarchy_fixture.NextStageTests(); case.setUp()
    return case


def _branch(case, name, children):
    with closing(case.store._connect()) as c:
        c.execute("INSERT INTO lp_catalog_branches(package_id,id,name,children,revisions,round_id,updated) "
                  "VALUES(?,?,?,?,?,?,?)", (case.pid, 'topic:' + name, name,
                                           json.dumps(children), '{}', '', time.time()))
        c.commit()


def _solo(case, gid, path, name):
    """仿 assign_solo_groups：空质心、空向量成员、有当前名字。"""
    case.store.upsert_file(case.pid, dict(path=path, name=path, size=1, modified_at_ns=1))
    with closing(case.store._connect()) as c:
        c.execute("INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,updated_at,member_count) "
                  "VALUES(?,?,?,?,?,?,1)", (case.pid, gid, 'test', b'', path, time.time()))
        c.execute("INSERT INTO large_catalog_members VALUES(?,?,?,?,?)", (case.pid, path, gid, 'h-' + path, b''))
        rev = c.execute("SELECT content_revision FROM large_catalog_groups WHERE package_id=? AND id=?",
                        (case.pid, gid)).fetchone()[0]
        c.execute('INSERT INTO lp_catalog_labels VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                  (case.pid, gid, rev, 1, name, '', '[]', 'test', 'd', 0, 0, time.time()))
        c.commit()


def _base(case):
    v = np.zeros(8, dtype=np.float32); v[0] = 1
    w = np.zeros(8, dtype=np.float32); w[3] = 1
    case.add('g1', v, n=2, name='设备运行监测技术')
    case.add('g2', w, n=2, name='仓储库存盘点记录')
    _branch(case, '主题甲', ['g1', 'g2'])


def _run(case, on):
    with patch.object(Config, 'LP_CATALOG_VALIDATE_FIX', on, create=True):
        return naming.validate_catalog(case.store, case.pid)


def _has(verdict, tag):
    return any(tag in x for x in verdict['violations'])


def test_single_file_entries_without_vector_are_covered():
    case = _case()
    try:
        _base(case)
        _solo(case, 'km-solo-a-00001', 's1.txt', '世界杯购票防骗指南')
        _solo(case, 'km-solo-a-00002', 's2.txt', '赛事酒店预订须知')
        off = _run(case, False)
        assert _has(off, 'INV-2'), off                       # 原口径：误报
        on = _run(case, True)
        assert not _has(on, 'INV-2'), on
        assert on['checks']['INV2_covered']['uncovered'] == 0
        assert on['checks']['INV2_covered']['single_or_system_without_vector'] == 2
    finally:
        case.doCleanups()


def test_file_without_any_membership_still_blocks_inv2():
    case = _case()
    try:
        _base(case)
        case.store.upsert_file(case.pid, dict(path='lost.txt', name='lost.txt', size=1, modified_at_ns=1))
        on = _run(case, True)
        assert _has(on, 'INV-2'), on
        assert on['checks']['INV2_covered']['uncovered'] == 1
    finally:
        case.doCleanups()


def test_same_name_single_file_copies_warn_not_block():
    case = _case()
    try:
        _base(case)
        _solo(case, 'km-solo-b-00001', 'dirA/copy.txt', '世界杯购票防骗指南')
        _solo(case, 'km-solo-b-00002', 'dirB/copy.txt', '世界杯购票防骗指南')
        off = _run(case, False)
        assert _has(off, 'INV-3'), off                       # 原口径：误报
        on = _run(case, True)
        assert not _has(on, 'INV-3'), on
        assert not any('不同主题' in w for w in on['warnings']), on
        assert on['checks']['INV3_duplicate_names']['single_file_entry_keys'] == 1
        assert any('单文件条目' in w for w in on['warnings']), on
    finally:
        case.doCleanups()


def test_real_same_topic_duplicates_still_block():
    case = _case()
    try:
        v = np.zeros(8, dtype=np.float32); v[0] = 1
        w = np.zeros(8, dtype=np.float32); w[5] = 1
        case.add('big', v, n=3, name='生物医学基因表达研究')
        case.add('small', w, n=2, name='生物医学基因表达研究')
        _branch(case, '主题甲', ['big', 'small'])
        on = _run(case, True)
        assert _has(on, 'INV-3'), on
        # 既有的消歧改名之后放行
        with patch('services.large_package_catalog_quality.compatible_groups', return_value=False):
            naming.normalize_leaves(case.store, case.pid)
        assert not _has(_run(case, True), 'INV-3')
    finally:
        case.doCleanups()


def test_named_pending_group_warns_not_blocks_but_unnamed_still_blocks():
    case = _case()
    try:
        _base(case)
        v = np.zeros(8, dtype=np.float32); v[6] = 1
        case.add('km-pending-7889bd6b', v, n=3, name='教育人物、机构与地理条目')
        off = _run(case, False)
        assert _has(off, 'INV-5'), off                       # 原口径：误报
        on = _run(case, True)
        assert not _has(on, 'INV-5'), on
        assert on['checks']['INV5_placeholders']['files_in_named_staging_groups'] == 3
        assert any('暂存组' in w for w in on['warnings']), on
        # 名字过期（revision 不匹配、未锁定）＝仍是占位 → 阻断
        with closing(case.store._connect()) as c:
            c.execute("UPDATE lp_catalog_labels SET revision=revision+99 WHERE package_id=? AND group_id=?",
                      (case.pid, 'km-pending-7889bd6b'))
            c.commit()
        assert _has(_run(case, True), 'INV-5')
    finally:
        case.doCleanups()


def test_ordinary_leaf_outside_topics_still_blocks_inv5():
    case = _case()
    try:
        _base(case)
        v = np.zeros(8, dtype=np.float32); v[7] = 1
        case.add('g3', v, n=1, name='孤立普通叶组')
        assert _has(_run(case, True), 'INV-5')
    finally:
        case.doCleanups()


def test_switch_off_is_identical_to_old_behavior():
    case = _case()
    try:
        _base(case)
        _solo(case, 'km-solo-c-00001', 'a/x.txt', '同名副本条目')
        _solo(case, 'km-solo-c-00002', 'b/x.txt', '同名副本条目')
        off = _run(case, False)
        assert 'single_or_system_without_vector' not in off['checks']['INV2_covered']
        assert 'single_file_entry_keys' not in off['checks']['INV3_duplicate_names']
        assert 'files_in_named_staging_groups' not in off['checks']['INV5_placeholders']
        assert _has(off, 'INV-2') and _has(off, 'INV-3')
    finally:
        case.doCleanups()
