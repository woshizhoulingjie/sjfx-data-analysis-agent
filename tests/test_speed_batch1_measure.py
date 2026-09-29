# -*- coding: utf-8 -*-
"""提速方案 v2 第 1 批（测量补点 + P5 退避）的回归测试。

测量补点：lp_catalog_calls.detail 里多记 prompt/completion token、finish_reason、
缓存未命中原因。只增加 JSON 键，命名结果、结算口径不变。
P5：情报概览发布被驳回后，同一份目录按指数退避重试；目录一变立即重试。
"""
import json
import time
from contextlib import closing
from unittest.mock import patch

from config import Config
from services import large_package_catalog_naming as naming

import test_catalog_naming_budget as budget


def _calls(case):
    with closing(case.store._connect()) as c:
        return [json.loads(r[0] or '{}') for r in
                c.execute("SELECT detail FROM lp_catalog_calls WHERE package_id=? ORDER BY started", (case.pid,))]


class _UsageModel(budget.Model):
    """在原夹具上补 usage/finish_reason，模拟 vLLM 的真实返回。"""
    def chat(self, system, user, **kwargs):
        out = super().chat(system, user, **kwargs)
        out['usage'] = {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150}
        out['finish_reason'] = 'stop'
        return out


class _Case(budget.NamingTests):
    def runTest(self):  # pragma: no cover - 只借用 setUp/add
        pass


def _case():
    case = _Case(); case.setUp(); case.model = _UsageModel()
    return case


def test_single_call_records_token_split_finish_reason_and_first_naming_reason():
    case = _case()
    try:
        gid = case.add('a.pdf', 'Remote attestation verifies device evidence and freshness. ' * 4)
        assert case.run_name()
        details = _calls(case)
        assert len(details) == 1, details
        d = details[0]
        # 旧键保持不变
        assert d['mode'] == 'single' and d['published'] == [gid] and d['usage_state'] == 'measured'
        # 新键
        assert d['prompt_tokens'] == 100 and d['completion_tokens'] == 50
        assert d['finish_reason'] == 'stop'
        assert d['cache_miss'][gid]['reason'] in ('first_naming', 'replacing_fallback'), d
        # 命名结果本身不受影响
        assert case.store.get_file(case.pid, 'a.pdf')['topic'] == '远程证明中的设备证据验证'
    finally:
        case.tearDown()


def test_cache_miss_reason_classification():
    case = _case()
    try:
        gid = case.add('a.pdf', 'Remote attestation verifies device evidence and freshness. ' * 4)
        assert case.run_name()
        with closing(case.store._connect()) as c:
            rev, digest, model = c.execute("SELECT revision,digest,model FROM lp_catalog_labels "
                                           "WHERE package_id=? AND group_id=?", (case.pid, gid)).fetchone()
        r = naming.cache_miss_reason
        assert r(case.store, case.pid, gid, digest, model)['reason'] == 'cache_key_absent'
        assert r(case.store, case.pid, gid, 'other-digest', model)['reason'] == 'content_changed'
        assert r(case.store, case.pid, gid, digest, 'other-model')['reason'] == 'model_changed'
        with closing(case.store._connect()) as c:
            c.execute("UPDATE large_catalog_groups SET content_revision=content_revision+3 "
                      "WHERE package_id=? AND id=?", (case.pid, gid))
            c.commit()
        out = r(case.store, case.pid, gid, digest, model)
        assert out['reason'] == 'revision_changed' and out['to_rev'] == out['from_rev'] + 3, out
        with closing(case.store._connect()) as c:
            c.execute("UPDATE lp_catalog_labels SET model='merge-identical' WHERE package_id=? AND group_id=?",
                      (case.pid, gid))
            c.commit()
        assert r(case.store, case.pid, gid, digest, model)['reason'] == 'inherited_label'
        with closing(case.store._connect()) as c:
            c.execute("UPDATE lp_catalog_labels SET model='fallback-naming' WHERE package_id=? AND group_id=?",
                      (case.pid, gid))
            c.commit()
        assert r(case.store, case.pid, gid, digest, model)['reason'] == 'replacing_fallback'
        with closing(case.store._connect()) as c:
            c.execute("DELETE FROM lp_catalog_labels WHERE package_id=? AND group_id=?", (case.pid, gid))
            c.commit()
        assert r(case.store, case.pid, gid, digest, model)['reason'] == 'first_naming'
        assert r(case.store, case.pid, 'no-such-group', digest, model) == {'reason': 'unknown'}
    finally:
        case.tearDown()


def test_finish_call_without_detail_or_usage_is_unchanged():
    case = _case()
    try:
        rnd = naming.ensure_round(case.store, case.pid)
        cid = naming.reserve(case.store, case.pid, rnd['id'], 'g', 100)
        naming.finish_call(case.store, cid, None, 0.1, 'completed')
        d = _calls(case)[0]
        assert d.get('usage_state') == 'estimated'
        assert 'prompt_tokens' not in d and 'completion_tokens' not in d
    finally:
        case.tearDown()


def _worker(case):
    from large_package_worker import LargePackageWorker
    return LargePackageWorker(store=case.store, poll_seconds=0.2)


def _expire(worker, pid):
    """把退避窗口“快进”到已过期，而不去改全局时钟。"""
    key, _next, delay = worker._overview_blocked[pid]
    worker._overview_blocked[pid] = (key, time.time() - 1, delay)


def test_blocked_overview_backs_off_and_retries_when_catalog_changes():
    case = _case()
    try:
        case.add('a.pdf', 'Remote attestation verifies device evidence and freshness. ' * 4)
        worker = _worker(case)
        calls = []
        fake = lambda store, item, milestone='catalog', note='': calls.append(1) or {'published': False}
        with patch('services.large_package_reporting.build_overview', fake), \
                patch.object(Config, 'LP_OVERVIEW_BLOCKED_BACKOFF_MAX', 300):
            # deep 里程碑不走稳定性闸门，便于直接考退避
            assert worker._build_overview(case.pid, 'deep') is False
            assert len(calls) == 1 and worker._overview_blocked[case.pid][2] == 2.0
            # 退避窗口内：不重做 build_overview
            for _ in range(5):
                assert worker._build_overview(case.pid, 'deep') is False
            assert len(calls) == 1
            # 窗口过了：再试一次，退避翻倍
            _expire(worker, case.pid)
            worker._build_overview(case.pid, 'deep')
            assert len(calls) == 2 and worker._overview_blocked[case.pid][2] == 4.0
            for _ in range(20):          # 一直被驳回：退避封顶
                _expire(worker, case.pid)
                worker._build_overview(case.pid, 'deep')
            assert worker._overview_blocked[case.pid][2] == 300
            # 目录内容变了：窗口内也立即重试，退避从 2 s 重新开始
            n = len(calls)
            with closing(case.store._connect()) as c:
                cur = c.execute("UPDATE large_catalog_groups SET content_revision=content_revision+1 "
                                "WHERE package_id=?", (case.pid,))
                assert cur.rowcount >= 1
                c.commit()
            worker._build_overview(case.pid, 'deep')
            assert len(calls) == n + 1
            assert worker._overview_blocked[case.pid][2] == 2.0
        # 发布成功：清掉退避
        with patch('services.large_package_reporting.build_overview', lambda *a, **k: {'published': True}):
            _expire(worker, case.pid)
            assert worker._build_overview(case.pid, 'deep') is True
            assert case.pid not in worker._overview_blocked
    finally:
        case.tearDown()


def test_backoff_disabled_restores_old_behaviour():
    case = _case()
    try:
        case.add('a.pdf', 'Remote attestation verifies device evidence and freshness. ' * 4)
        worker = _worker(case)
        calls = []
        with patch('services.large_package_reporting.build_overview',
                   lambda *a, **k: calls.append(1) or {'published': False}), \
                patch.object(Config, 'LP_OVERVIEW_BLOCKED_BACKOFF_MAX', 0):
            for _ in range(4):
                worker._build_overview(case.pid, 'deep')
        assert len(calls) == 4 and not worker._overview_blocked
    finally:
        case.tearDown()
