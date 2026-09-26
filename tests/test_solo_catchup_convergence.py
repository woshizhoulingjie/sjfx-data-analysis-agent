# -*- coding: utf-8 -*-
"""`_solo_catchup` 欠账判据的收敛性回归测试。

背景（2026-09-24 生产实测）：判据原先只问"有没有 queued/running/deferred 任务"，
而命名成功的 solo 组其任务状态是 `completed` —— 不在该集合里，于是包被**永久**
判为欠账，每 120 秒重排一次；而 queue_groups 因标签已齐实际建 0 条任务，纯空转。
三个包从 18:48 刷到 19:51。

本测试直接复用生产常量 SOLO_CATCHUP_STALE_SQL，因此不会与生产实现漂移。
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from large_package_worker import SOLO_CATCHUP_STALE_SQL  # noqa: E402

DDL = """
CREATE TABLE packages(id TEXT PRIMARY KEY, status TEXT, updated_at INTEGER DEFAULT 0);
CREATE TABLE large_catalog_groups(package_id TEXT, id TEXT, content_revision INTEGER DEFAULT 1,
                                  member_count INTEGER DEFAULT 1,
                                  PRIMARY KEY(package_id,id));
CREATE TABLE lp_catalog_labels(package_id TEXT, group_id TEXT, revision INTEGER DEFAULT 0,
                               locked INTEGER DEFAULT 0,
                               PRIMARY KEY(package_id,group_id));
CREATE TABLE lp_catalog_jobs(package_id TEXT, group_id TEXT, status TEXT, revision INTEGER DEFAULT 1,
                             PRIMARY KEY(package_id,group_id));
"""


@pytest.fixture()
def db():
    conn = sqlite3.connect(':memory:')
    conn.executescript(DDL)
    conn.execute("INSERT INTO packages(id,status) VALUES('p','completed')")
    conn.commit()
    yield conn
    conn.close()


def _stale(conn):
    return [r[0] for r in conn.execute(SOLO_CATCHUP_STALE_SQL)]


def _solo(conn, group_id, content_revision=1):
    conn.execute("INSERT INTO large_catalog_groups(package_id,id,content_revision) VALUES(?,?,?)",
                 ('p', group_id, content_revision))
    conn.commit()


def test_named_solo_group_is_not_stale_forever(db):
    """**核心回归**：命名成功后必须退出欠账集合。

    这正是原缺陷：任务 completed + 标签现行，旧判据仍判欠账 → 无限重排。
    """
    _solo(db, 'km-solo-aaa-00000')
    db.execute("INSERT INTO lp_catalog_labels(package_id,group_id,revision,locked) VALUES('p','km-solo-aaa-00000',1,0)")
    db.execute("INSERT INTO lp_catalog_jobs(package_id,group_id,status) VALUES('p','km-solo-aaa-00000','completed')")
    db.commit()
    assert _stale(db) == [], '已命名完成的 solo 组不得再被判为欠账'


def test_brand_new_solo_group_is_stale(db):
    """真欠账仍要被抓到：有 solo 组、无标签、无任务行。"""
    _solo(db, 'km-solo-bbb-00000')
    assert _stale(db) == ['p']


def test_stale_revision_label_is_stale(db):
    """标签版本落后且未锁定 → 需要重命名。"""
    _solo(db, 'km-solo-ccc-00000', content_revision=7)
    db.execute("INSERT INTO lp_catalog_labels(package_id,group_id,revision,locked) VALUES('p','km-solo-ccc-00000',3,0)")
    db.commit()
    assert _stale(db) == ['p']


def test_locked_stale_revision_label_is_not_stale(db):
    """锁定标签不因版本落后而重排（人工锁定的名字不该被覆盖）。"""
    _solo(db, 'km-solo-ddd-00000', content_revision=7)
    db.execute("INSERT INTO lp_catalog_labels(package_id,group_id,revision,locked) VALUES('p','km-solo-ddd-00000',3,1)")
    db.commit()
    assert _stale(db) == []


def test_inflight_job_is_not_stale(db):
    """已在飞行中的任务不重复排队（避免重复命名与 token 浪费）。"""
    _solo(db, 'km-solo-eee-00000')
    db.execute("INSERT INTO lp_catalog_jobs(package_id,group_id,status) VALUES('p','km-solo-eee-00000','queued')")
    db.commit()
    assert _stale(db) == []


def test_current_label_without_job_row_is_not_stale(db):
    """标签现行、只是任务行被清掉：queue_groups 不会重排，故不得判欠账。

    这条锁住"必须收紧到无 job 行"的边界：若判据只看标签，这里会返回 p，
    而 queue_groups 的 upsert 因 revision 相同不会重置状态 → 又是无限空转。
    """
    _solo(db, 'km-solo-fff-00000')
    db.execute("INSERT INTO lp_catalog_labels(package_id,group_id,revision,locked) VALUES('p','km-solo-fff-00000',1,0)")
    db.commit()
    assert _stale(db) == []


def test_non_solo_group_is_ignored(db):
    """只补 solo 组；普通 km 组由正常轮次调度，不由这里兜底。"""
    _solo(db, 'km-1234-00000')
    assert _stale(db) == []


def test_paused_package_is_ignored(db):
    """暂停中的包不补做（用户主动暂停的语义必须被尊重）。"""
    db.execute("UPDATE packages SET status='paused' WHERE id='p'")
    _solo(db, 'km-solo-ggg-00000')
    db.commit()
    assert _stale(db) == []
