# -*- coding: utf-8 -*-
"""A2 回归：repair_changed 不得被"永远没有向量"的脏行卡成无限 retry（生产 lp-085e413e，2026-09-29）。

`assign_solo_groups` 有意给正文太短的文件建 km-solo 单文件组，成员行 vector=b''；
INSERT 触发 lp_catalog_changed_insert 把 path 写进 lp_catalog_changes。改动前
repair_changed 在这类行上 return retry、不删 changes、不存指纹，每轮完全重演。
"""
import time
from contextlib import closing

import numpy as np

import test_catalog_next_stage as hierarchy_fixture
from services import large_package_catalog_quality as quality
from services import large_package_semantic as semantic


def _case():
    case = hierarchy_fixture.NextStageTests(); case.setUp()
    return case


def _solo(case, path):
    """仿 assign_solo_groups：空质心 km-solo 组 + vector=b'' 的成员行。"""
    gid = 'km-solo-test-' + path
    case.store.upsert_file(case.pid, dict(path=path, name=path, size=10, modified_at_ns=1))
    with closing(case.store._connect()) as c:
        c.execute('INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,updated_at) '
                  'VALUES(?,?,?,?,?,?)', (case.pid, gid, 'test', b'', path, time.time()))
        c.execute('INSERT INTO large_catalog_members VALUES(?,?,?,?,?)',
                  (case.pid, path, gid, 'hash-' + path, b''))
        c.commit()
    return gid


def _dirty(case):
    with closing(case.store._connect()) as c:
        return {r[0] for r in c.execute('SELECT path FROM lp_catalog_changes WHERE package_id=?', (case.pid,))}


def _member_group(case, path):
    with closing(case.store._connect()) as c:
        row = c.execute('SELECT group_id FROM large_catalog_members WHERE package_id=? AND path=?',
                        (case.pid, path)).fetchone()
    return row[0] if row else None


def _vec(dim, hot):
    v = np.zeros(dim, dtype=np.float32); v[hot] = 1
    return v


def test_unembeddable_dirty_rows_do_not_spin():
    case = _case()
    try:
        case.add('a', _vec(8, 0), n=3)
        gids = [_solo(case, 'short-%d.txt' % i) for i in range(3)]
        assert {'short-0.txt', 'short-1.txt', 'short-2.txt'} <= _dirty(case), '前置：触发器应把 solo 成员写进 changes'

        result = quality.repair_changed(case.store, case.pid)
        assert result['status'] == 'done', result
        assert result['unembeddable'] == 3, result
        assert _dirty(case) == set(), '无向量脏行必须被清掉，否则下一轮完全重演'
        # 成员行与 km-solo 组保持不动（assign_solo_groups 的设计意图）。
        for i, gid in enumerate(gids):
            assert _member_group(case, 'short-%d.txt' % i) == gid
        # 指纹已保存：worker 的冻结闸门下一轮会直接放行（不再进 repair_changed）。
        assert semantic._km_state_get(case.store, case.pid) == semantic._km_signature(case.store, case.pid)
        assert quality.repair_changed(case.store, case.pid)['status'] == 'nothing'
    finally:
        case.doCleanups()


def test_dimension_mismatch_still_retries_without_touching_data():
    """维度不符（换过嵌入模型）仍 retry：跳过它会让 _group_geometry 在同组混合维度。
    本修复只处理"无向量"行，这条原有行为保持不变。"""
    case = _case()
    try:
        case.add('a', _vec(8, 0), n=2)
        case.store.upsert_file(case.pid, dict(path='odd.txt', name='odd.txt', size=10, modified_at_ns=1))
        with closing(case.store._connect()) as c:
            c.execute('INSERT INTO large_catalog_members VALUES(?,?,?,?,?)',
                      (case.pid, 'odd.txt', 'a', 'hash-odd', _vec(4, 0).tobytes()))
            c.commit()
        before = _dirty(case)
        assert 'odd.txt' in before

        result = quality.repair_changed(case.store, case.pid)
        assert result['status'] == 'retry', result
        assert _dirty(case) == before, 'retry 不得删除任何 changes'
        assert _member_group(case, 'odd.txt') == 'a'
    finally:
        case.doCleanups()


def test_normal_repair_unchanged():
    """没有跳过行时，返回值与改动前一致（changed == 参与判定的行数）。"""
    case = _case()
    try:
        case.add('a', _vec(8, 0), n=2)
        result = quality.repair_changed(case.store, case.pid)
        assert result['status'] == 'done' and result['changed'] == 2, result
        assert result['unembeddable'] == 0
        assert _dirty(case) == set()
    finally:
        case.doCleanups()
