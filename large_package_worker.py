"""Dedicated worker for the isolated streaming large-package workflow."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from config import Config
from services.large_package_isolated import LargePackageStore, iter_inventory, quick_preview

LOGGER = logging.getLogger("sjfx.large_package_worker")


def _counts(store, package_id):
    return store.counts(package_id)


class LargePackageWorker:
    def __init__(self, store=None, poll_seconds=2.0):
        self.store = store or LargePackageStore()
        self.poll_seconds = max(0.2, float(poll_seconds))
        self.stop_requested = False

    def _inventory_one(self, package):
        package_id = package["id"]
        root = package["root_path"]
        self.store.update_package(package_id, status="running", phase="inventory", progress=1, message="正在流式盘点目录")
        discovered = 0
        total_bytes = 0
        type_counts = {}
        try:
            for item in iter_inventory(root):
                if self.stop_requested:
                    return False
                if discovered % 25 == 0:
                    current = self.store.get(package_id)
                    if current and str(current.get("status") or "").lower() in {"paused", "cancelled"}:
                        LOGGER.info("large package %s inventory stopped at checkpoint: %s", package_id, current.get("status"))
                        return True
                self.store.upsert_file(package_id, item)
                discovered += 1
                total_bytes += int(item.get("size") or 0)
                extension = item.get("extension") or "[无扩展名]"
                type_counts[extension] = type_counts.get(extension, 0) + 1
                if discovered % 100 == 0:
                    self.store.update_package(
                        package_id, progress=min(20, max(1, discovered // 50)),
                        message="已盘点 {} 个文件".format(discovered),
                        counts={"discovered_files": discovered, "total_bytes": total_bytes,
                                "type_counts": type_counts},
                    )
            current = self.store.get(package_id)
            if current and str(current.get("status") or "").lower() in {"paused", "cancelled"}:
                return True
            self.store.update_package(
                package_id, status="running", phase="quick_parse", progress=20,
                message="目录盘点完成，开始逐文件生成本地摘要",
                counts={"discovered_files": discovered, "total_bytes": total_bytes,
                        "total_size_human": _human(total_bytes), "type_counts": type_counts},
            )
            return True
        except Exception as exc:
            LOGGER.exception("large inventory failed: %s", package_id)
            self.store.update_package(package_id, status="failed", phase="inventory", error=str(exc), message="目录盘点失败")
            return False

    def _quick_one(self, package):
        package_id = package["id"]
        root = package["root_path"]
        while True:
            current = self.store.get(package_id)
            if current and str(current.get("status") or "").lower() in {"paused", "cancelled"}:
                LOGGER.info("large package %s quick stage stopped at checkpoint: %s", package_id, current.get("status"))
                return True
            claimed = self.store.claim("quick", 1, package_id=package_id)
            if not claimed:
                counts = _counts(self.store, package_id)
                current = self.store.get(package_id)
                if current and str(current.get("status") or "").lower() in {"paused", "cancelled"}:
                    return True
                quick = counts.get("quick", {})
                pending = int(quick.get("queued", 0)) + int(quick.get("running", 0))
                if pending:
                    time.sleep(0.2)
                    continue
                self.store.update_package(package_id, status="waiting_for_selection", phase="catalog_ready", progress=70,
                                          message="智能目录和本地摘要已生成，等待选择深度解析范围", counts=counts)
                return True
            _, path = claimed[0]
            try:
                # Resolve the claimed queue item by its exact primary-key path.
                # A LIKE search can return e.g. ``13.pdf`` for ``3.pdf`` when
                # the result is limited to one row, causing false failures.
                item = self.store.get_file(package_id, path)
                if not item:
                    raise FileNotFoundError(path)
                summary, sha256 = quick_preview(root, item)
                if summary.get("status") == "failed":
                    self.store.update_file(package_id, path, quick_status="failed", error=summary.get("error"), local_summary=summary)
                    self.store.finish_queue(package_id, path, "quick", False, summary.get("error"))
                else:
                    self.store.update_file(package_id, path, quick_status="completed", sha256=sha256,
                                           category=summary.get("category") or "未分类",
                                           confidence=0.65 if summary.get("category") != "未分类" else 0.2,
                                           local_summary=summary, error=None)
                    self.store.finish_queue(package_id, path, "quick", True)
            except Exception as exc:
                self.store.update_file(package_id, path, quick_status="failed", error=str(exc)[:500])
                self.store.finish_queue(package_id, path, "quick", False, str(exc)[:500])
            counts = _counts(self.store, package_id)
            done = sum(int(counts.get("quick", {}).get(key, 0)) for key in ("completed", "failed"))
            total = max(1, int(counts.get("total_files") or 0))
            self.store.update_package(package_id, progress=min(70, 20 + int(50 * done / total)),
                                      message="已生成本地摘要 {}/{}".format(done, total), counts=counts)

    def _deep_one(self, package):
        package_id = package["id"]
        root = package["root_path"]
        while True:
            current = self.store.get(package_id)
            if current and str(current.get("status") or "").lower() in {"paused", "cancelled"}:
                LOGGER.info("large package %s deep stage stopped at checkpoint: %s", package_id, current.get("status"))
                return True
            claimed = self.store.claim("deep", 1, package_id=package_id)
            if not claimed:
                counts = _counts(self.store, package_id)
                current = self.store.get(package_id)
                if current and str(current.get("status") or "").lower() in {"paused", "cancelled"}:
                    return True
                pending = int(counts.get("deep", {}).get("queued", 0)) + int(counts.get("deep", {}).get("running", 0))
                if pending:
                    time.sleep(0.2)
                    continue
                self.store.update_package(package_id, status="completed", phase="report_ready", progress=100,
                                          message="选定范围已完成深度解析和摘要", counts=counts)
                return True
            _, path = claimed[0]
            try:
                row = self.store.get_file(package_id, path)
                if not row:
                    raise FileNotFoundError(path)
                result = self._deep_parse(root, row)
                self.store.update_file(package_id, path, deep_status="completed", deep_summary=result, error=None)
                self.store.finish_queue(package_id, path, "deep", True)
            except Exception as exc:
                self.store.update_file(package_id, path, deep_status="failed", error=str(exc)[:500])
                self.store.finish_queue(package_id, path, "deep", False, str(exc)[:500])
            counts = _counts(self.store, package_id)
            deep = counts.get("deep", {})
            done = sum(int(deep.get(key, 0)) for key in ("completed", "failed"))
            total = max(1, sum(int(deep.get(key, 0)) for key in ("queued", "running", "completed", "failed")))
            self.store.update_package(package_id, progress=70 + int(30 * done / total), message="已完成深度解析 {}/{}".format(done, total), counts=counts)

    def _deep_parse(self, root, row):
        path = Path(root) / row["path"]
        try:
            from services.unified_parser import UnifiedDocumentParser
            parser = getattr(self, "_parser", None)
            if parser is None:
                self._parser = UnifiedDocumentParser()
                parser = self._parser
            document = parser.parse(path, relative_path=row["path"], mode="accurate")
            text = str(document.get("text") or "")
            summary = {
                "summary_type": "model_pending", "source_path": row["path"],
                "source_sha256": (document.get("source") or {}).get("sha256") or row.get("sha256"),
                "coverage": document.get("coverage") or {}, "text_preview": text[:12000],
                "evidence": document.get("evidence") or [], "parser": document.get("parser") or {},
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            # Model generation remains opt-in for the isolated worker.  A local
            # parser result is still durable and clearly labelled if the model
            # is busy or unavailable.
            if str(os.getenv("LARGE_PACKAGE_MODEL_SUMMARY", "1")).lower() not in {"0", "false", "no", "off"} and text.strip():
                summary.update(self._model_summary(row["path"], text[:24000]))
            else:
                summary["summary_type"] = "local_deep"
            return summary
        except Exception as exc:
            return {"summary_type": "local_deep_failed", "source_path": row["path"], "error": str(exc)[:500]}

    def _model_summary(self, path, text):
        try:
            if Config.LLM_BACKEND == "vllm" and Config.ENABLE_VLLM:
                from services.vllm import VLLMClient
                client = VLLMClient(Config.VLLM_BASE_URL, Config.VLLM_MODEL, timeout=Config.VLLM_STRUCTURED_TIMEOUT_SECONDS,
                                    max_concurrency=1, api_key=Config.VLLM_API_KEY)
            elif Config.ENABLE_SHARED_OLLAMA:
                from services.ollama import OllamaClient
                client = OllamaClient(Config.OLLAMA_BASE_URL, Config.OLLAMA_MODEL, timeout=180, max_concurrency=1)
            else:
                return {"summary_type": "local_deep", "model_status": "disabled"}
            result = client.chat("你是资料摘要助手，只输出简洁中文摘要。", "请为文件 {} 生成轻度摘要，包含主题、关键事实、风险和后续建议：\n{}".format(path, text), temperature=0.1, max_tokens=500)
            return {"summary_type": "model", "model": result.get("model"), "summary": result.get("content", ""), "usage": result.get("usage", {})}
        except Exception as exc:
            return {"summary_type": "local_deep", "model_status": "unavailable", "model_error": str(exc)[:300]}

    def run_once(self):
        self.store.stale_running_to_retry()
        package = None
        for candidate in self._packages():
            package = candidate
            break
        if not package:
            return False
        if package["phase"] == "inventory":
            return self._inventory_one(package)
        if package["phase"] == "quick_parse":
            return self._quick_one(package)
        if package["phase"] == "deep_parse":
            return self._deep_one(package)
        return False

    def _packages(self):
        with self.store._connect() as conn:
            rows = conn.execute("SELECT * FROM packages WHERE status IN ('queued','running','waiting_for_selection') AND phase IN ('inventory','quick_parse','deep_parse') ORDER BY updated_at").fetchall()
        return [self.store._row(row) for row in rows]

    def run_forever(self):
        while not self.stop_requested:
            if not self.run_once():
                time.sleep(self.poll_seconds)


def _human(value):
    try:
        from services.scanner import human_size
        return human_size(value)
    except Exception:
        return str(value)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    LargePackageWorker().run_forever()

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    LargePackageWorker().run_forever()
