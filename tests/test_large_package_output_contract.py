"""第 2 批回归测试：输出契约与形状兜底。

覆盖四项：
  2.2 病态单元素组的兜底吸收（absorb_undersized 在 floor<=1 时是死代码）
  2.3 LP_CATALOG_KMODE 未开启时的降级标注
  2.4 深度摘要输出上限可配（默认 500 → 400）
  2.5 finish_reason 截断检查（改动前完全不看）

2.4 / 2.5 的依据：深度阶段是 GPU decode 瓶颈——实测单文件 p50 18.51 s，
而 500 output tokens ÷ 27.8 tok/s ≈ 18.0 s（约 97% 的时间花在生成上）。
10 GiB（同密度约 3,514 文件）在并发 2 下需 8.78 h；降到 400 tokens 约 7.02 h，
是 8 小时目标零硬件成本的达标路径。
"""
import os
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import numpy as np

from config import Config
from services.large_package_isolated import LargePackageStore
from services import large_package_semantic as semantic
from large_package_worker import LargePackageWorker


class _FakeVLLMClient:
    """记录调用参数的假 vLLM 客户端。"""

    last_kwargs = None
    reply = {"content": "摘要", "usage": {"total_tokens": 10}, "finish_reason": "stop",
             "model": "fixture-model"}

    def __init__(self, *args, **kwargs):
        pass

    def chat(self, system_prompt, user_prompt, **kwargs):
        type(self).last_kwargs = kwargs
        return dict(type(self).reply)


class DeepSummaryContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(Config, "SCAN_ALLOWED_ROOTS", ()),
            patch.object(Config, "LLM_BACKEND", "vllm"),
            patch.object(Config, "ENABLE_VLLM", True),
            patch.object(Config, "ENABLE_SHARED_OLLAMA", False),
        ]
        for item in self.patches:
            item.start()
        self.store = LargePackageStore(self.root / "state.db")
        self.pid = self.store.create(self.root)["id"]
        self.worker = LargePackageWorker(store=self.store, poll_seconds=0.2)
        _FakeVLLMClient.last_kwargs = None
        _FakeVLLMClient.reply = {"content": "摘要", "usage": {"total_tokens": 10},
                                 "finish_reason": "stop", "model": "fixture-model"}

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    # 2.4 -----------------------------------------------------------------
    def test_default_output_limit_is_400(self):
        with patch("services.vllm.VLLMClient", _FakeVLLMClient), \
                patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SJFX_LP_DEEP_SUMMARY_MAX_TOKENS", None)
            payload = self.worker._model_summary("a.txt", "正文")
        self.assertEqual(_FakeVLLMClient.last_kwargs.get("max_tokens"), 400,
                         "深度摘要默认输出上限应为 400（8 小时目标的达标路径）")
        self.assertEqual(payload.get("max_tokens"), 400)
        self.assertEqual(payload.get("summary_type"), "model")

    def test_output_limit_is_configurable(self):
        with patch("services.vllm.VLLMClient", _FakeVLLMClient), \
                patch.dict(os.environ, {"SJFX_LP_DEEP_SUMMARY_MAX_TOKENS": "500"}):
            self.worker._model_summary("a.txt", "正文")
        self.assertEqual(_FakeVLLMClient.last_kwargs.get("max_tokens"), 500,
                         "回滚开关 SJFX_LP_DEEP_SUMMARY_MAX_TOKENS 必须生效")

    # 2.5 -----------------------------------------------------------------
    def test_finish_reason_length_is_flagged_as_truncated(self):
        _FakeVLLMClient.reply = {"content": "被截断的摘要", "usage": {}, "finish_reason": "length",
                                 "model": "fixture-model"}
        with patch("services.vllm.VLLMClient", _FakeVLLMClient):
            payload = self.worker._model_summary("a.txt", "正文")
        self.assertTrue(payload.get("truncated"), "finish_reason=length 必须被标记为截断")
        self.assertEqual(payload.get("truncation_reason"), "length")
        self.assertEqual(payload.get("finish_reason"), "length")

    def test_normal_finish_reason_is_not_flagged(self):
        _FakeVLLMClient.reply = {"content": "完整摘要", "usage": {}, "finish_reason": "stop",
                                 "model": "fixture-model"}
        with patch("services.vllm.VLLMClient", _FakeVLLMClient):
            payload = self.worker._model_summary("a.txt", "正文")
        self.assertNotIn("truncated", payload)
        self.assertEqual(payload.get("finish_reason"), "stop")

    # 2.3 -----------------------------------------------------------------
    def test_catalog_mode_is_disclosed(self):
        with patch.object(Config, "LP_CATALOG_KMODE", False):
            legacy = LargePackageWorker(store=self.store, poll_seconds=0.2)
        with patch.object(Config, "LP_CATALOG_KMODE", True):
            modern = LargePackageWorker(store=self.store, poll_seconds=0.2)
        self.assertEqual(legacy.catalog_mode, "legacy")
        self.assertEqual(modern.catalog_mode, "kmeans")
        self.assertEqual(legacy._catalog_counts(self.pid).get("catalog_mode"), "legacy",
                         "降级目录必须在 counts 里显式标注，供界面区分")
        self.assertEqual(modern._catalog_counts(self.pid).get("catalog_mode"), "kmeans")


class SingletonRescueTests(unittest.TestCase):
    """2.2：单元素组占比病态时才补一次带下限的吸收。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(Config, "SCAN_ALLOWED_ROOTS", ()),
            patch.object(Config, "LP_CATALOG_KMODE", True),
            patch.object(Config, "LP_KM_MIN_GROUP", 1),          # 出厂默认：不设常规下限
            patch.object(Config, "LP_KM_MIN_GROUP_AUTO", True),
            patch.object(Config, "LP_KM_SINGLETON_ALARM", 0.25),
        ]
        for item in self.patches:
            item.start()
        self.store = LargePackageStore(self.root / "state.db")
        self.pid = self.store.create(self.root)["id"]
        with closing(self.store._connect()) as conn:
            semantic.init(conn)
        rng = np.random.default_rng(11)
        now = time.time()
        with closing(self.store._connect()) as conn:
            for index in range(8):
                path = "f%d.txt" % index
                conn.execute(
                    "INSERT OR REPLACE INTO files(package_id,path,name,extension,size,"
                    "modified_at_ns,sha256,sensitive,file_type,quick_status,category,"
                    "confidence,local_summary,deep_summary,updated_at) "
                    "VALUES(?,?,?,'.txt',10,0,NULL,0,'.txt','completed','x',0,'{}','{}',?)",
                    (self.pid, path, path, now))
                vector = rng.standard_normal(8).astype(np.float32)
                vector /= np.linalg.norm(vector)
                conn.execute(
                    "INSERT OR REPLACE INTO large_catalog_members"
                    "(package_id,path,group_id,input_hash,vector) VALUES(?,?,?,?,?)",
                    (self.pid, path, "km-pending-fixture", "h%d" % index, vector.tobytes()))
            conn.commit()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_pathological_singletons_trigger_a_rescue_absorb(self):
        # 让聚类直接返回"每个文件一个组"的病态结果（8/8 单元素）
        pathological = list(range(8))
        with patch.object(semantic, "_balanced_labels", return_value=pathological), \
                patch.object(semantic, "absorb_undersized",
                             wraps=semantic.absorb_undersized) as absorb:
            semantic.kmeans_recluster(self.store, self.pid)
        self.assertGreaterEqual(
            absorb.call_count, 2,
            "单元素组占比 100% 时必须补一次带下限的兜底吸收"
            "（改动前只有 floor=1 的那一次调用，等于不做任何事）")

    def test_healthy_partition_does_not_trigger_rescue(self):
        healthy = [0, 0, 0, 0, 1, 1, 1, 1]          # 0% 单元素组
        with patch.object(semantic, "_balanced_labels", return_value=healthy), \
                patch.object(semantic, "absorb_undersized",
                             wraps=semantic.absorb_undersized) as absorb:
            semantic.kmeans_recluster(self.store, self.pid)
        self.assertEqual(absorb.call_count, 1,
                         "健康分组不应触发兜底吸收（正常语料零影响）")


if __name__ == "__main__":
    unittest.main()
