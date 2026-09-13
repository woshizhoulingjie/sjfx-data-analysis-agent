"""Dedicated worker for the isolated streaming large-package workflow."""
from __future__ import annotations

import logging
import os
import time
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
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
        from services.large_package_preview_runner import PreviewRunner
        self.preview_runner = PreviewRunner()
        self._naming_executor = None
        self._naming_future = None

    def _interrupted(self, package_id):
        current = self.store.get(package_id)
        return self.stop_requested or not current or current["status"] in {"paused", "cancelled"}

    def _inventory_one(self, package):
        package_id = package["id"]
        self.store.update_package(package_id, status="running", phase="inventory", progress=1, message="正在流式盘点并建立内容画像")
        discovered, total_bytes, type_counts = 0, 0, {}
        batch = []
        inventory_errors=[]
        inventory_error_count=0
        def inventory_error(path,error):
            nonlocal inventory_error_count
            inventory_error_count+=1
            if len(inventory_errors)<20:inventory_errors.append({'path':path,'error':error[:200]})
        try:
            for item in iter_inventory(package["root_path"],on_error=inventory_error):
                if discovered % 25 == 0 and self._interrupted(package_id):
                    return True
                batch.append(item)
                discovered += 1
                total_bytes += int(item.get("size") or 0)
                extension = item.get("extension") or "[无扩展名]"
                type_counts[extension] = type_counts.get(extension, 0) + 1
                if len(batch) >= 100:
                    self.store.upsert_files(package_id, batch)
                    batch = []
                    self.store.update_package(package_id, progress=min(19, 1 + discovered // 1000), message="已盘点 {} 个文件；目录持续更新".format(discovered), counts={"discovered_files": discovered, "total_bytes": total_bytes, "type_counts": type_counts})
            if batch:
                self.store.upsert_files(package_id, batch)
            if not self._interrupted(package_id):
                self.store.update_package(package_id, status="running", phase="quick_parse", progress=20, message="盘点有 {} 处无法读取，继续识别已发现文件".format(inventory_error_count) if inventory_error_count else "盘点完成，按格式识别文件内容", counts={"discovered_files": discovered, "total_bytes": total_bytes, "total_size_human": _human(total_bytes), "type_counts": type_counts, "inventory_complete": inventory_error_count == 0, "inventory_error_count": inventory_error_count, "inventory_errors": inventory_errors})
            return True
        except Exception as exc:
            LOGGER.exception("large inventory failed: %s", package_id)
            self.store.update_package(package_id, status="failed", phase="inventory", error=str(exc), message="目录盘点失败")
            return False

    def _save_preview(self, package_id, path, summary, refine=False):
        if summary.get("status") == "failed":
            raise RuntimeError(summary.get("error") or "轻量解析失败")
        info = summary.get("portrait") or {}
        prior = self.store.get_file(package_id, path)
        # An OCR/timeout failure must not erase a useful first-pass portrait.
        if refine and prior and not summary.get("sample") and (prior.get("local_summary") or {}).get("sample"):
            warning = summary.get("notice") or "补充预算内没有新增内容"
            summary = dict(prior["local_summary"])
            info = dict(summary.get("portrait") or {})
            info.update(recognition_status="needs_review", needs_refine=False)
            summary.update(portrait=info, notice=warning)
        self.store.update_file(package_id, path, quick_status="completed", local_summary=summary,
                               category=summary.get("category") or "未分类", confidence=info.get("confidence", 0),
                               topic=info.get("topic") or "待识别内容", recognition_status=info.get("recognition_status", "metadata_only"),
                               document_role=info.get("document_role", ""), error=None)
        # Full source hashes belong to deep parsing, never to sampled previews.
        if not refine and info.get("needs_refine"):
            self.store.enqueue_recognition(package_id, path)

    def _catalog_incremental(self, package_id):
        from services.large_package_semantic import embed_batch, name_batch
        try:
            cancelled = lambda: self._interrupted(package_id)
            embed_batch(self.store, package_id, cancelled=cancelled)
            name_batch(self.store, package_id, cancelled=cancelled)
        except Exception:
            LOGGER.exception("large semantic catalog deferred: %s", package_id)

    def _quick_one(self, package):
        from services.large_package_preview_runner import PreviewCancelled, PreviewRunner
        package_id = package["id"]
        counts = self.store.counts(package_id)
        total = max(1, counts["total_files"])
        done = sum(counts.get("quick", {}).get(key, 0) for key in ("completed", "failed"))
        updated = 0.0
        runners = [self.preview_runner] + [PreviewRunner() for _ in range(Config.LP_PREVIEW_CONCURRENCY-1)]
        try:
            with ThreadPoolExecutor(max_workers=len(runners)) as pool:
                while not self._interrupted(package_id):
                    claimed = self.store.claim("quick", len(runners), package_id=package_id)
                    if not claimed:
                        stage = self.store.stage_counts(package_id, "quick")
                        if any(stage.get(k) for k in ('running','queued','retry')):
                            return True
                        self._catalog_incremental(package_id)
                        if self._interrupted(package_id):return True
                        catalog_queue = self.store.stage_counts(package_id, "catalog")
                        if catalog_queue.get("queued") or catalog_queue.get("retry"):continue
                        self.store.update_package(package_id, phase="recognize", status="running", progress=65,
                            message="基础画像已生成，正在有预算地补充难分类文件")
                        return True
                    futures=[]
                    for runner,(_,path) in zip(runners,claimed):
                        item=self.store.get_file(package_id,path)
                        future=pool.submit(runner.run,package['root_path'],item,cancel_check=lambda:self._interrupted(package_id))
                        futures.append((path,future))
                    for path,future in futures:
                        try:
                            summary,_=future.result()
                            self._save_preview(package_id,path,summary)
                            self.store.finish_queue(package_id,path,'quick',True)
                        except PreviewCancelled:
                            self.store.requeue(package_id,path,'quick')
                        except Exception as exc:
                            self.store.update_file(package_id,path,quick_status='failed',error=str(exc)[:500])
                            self.store.finish_queue(package_id,path,'quick',False,str(exc)[:500])
                        done+=1
                        if done%32==0:self._catalog_incremental(package_id)
                    if time.monotonic()-updated>=1:
                        updated=time.monotonic()
                        self.store.update_package(package_id,progress=min(65,20+int(45*done/total)),
                            message="已尝试内容预览 {}/{}；目录持续更新".format(done,total))
        finally:
            for runner in runners[1:]:runner.close()
        return True

    def _recognize_one(self, package):
        from services.large_package_preview_runner import PreviewCancelled
        package_id = package["id"]
        # Bound aggregate extra work as well as each file. Deferred files remain
        # visible and can be continued explicitly without redoing completed work.
        try:
            slice_seconds = max(5, min(300, int(os.getenv("SJFX_LP_RECOGNITION_SLICE_SECONDS", "60"))))
            max_files = max(1, min(200, int(os.getenv("SJFX_LP_RECOGNITION_SLICE_FILES", "30"))))
        except ValueError:
            slice_seconds, max_files = 60, 30
        end = time.monotonic() + slice_seconds
        processed = 0
        while processed < max_files and time.monotonic() < end and not self._interrupted(package_id):
            claimed = self.store.claim("recognize", 1, package_id=package_id)
            if not claimed:
                break
            _, path = claimed[0]
            try:
                item = self.store.get_file(package_id, path)
                summary, _ = self.preview_runner.run(package["root_path"], item, refine=True, cancel_check=lambda: self._interrupted(package_id) or time.monotonic() >= end)
                self._save_preview(package_id, path, summary, refine=True)
                self.store.finish_queue(package_id, path, "recognize", True)
            except PreviewCancelled:
                self.store.requeue(package_id, path, "recognize")
                break
            except Exception as exc:
                self.store.update_file(package_id, path, recognition_status="needs_review", error=str(exc)[:300])
                self.store.finish_queue(package_id, path, "recognize", False, str(exc)[:300])
            processed += 1
            self.store.update_package(package_id, message="已补充识别 {} 个文件；可查看内容主题与来源依据".format(processed))
        if self._interrupted(package_id):
            return True
        self._catalog_incremental(package_id)
        if self._interrupted(package_id):
            return True
        stage = self.store.stage_counts(package_id, "recognize")
        pending = sum(stage.get(k, 0) for k in ("queued", "retry", "running"))
        completed = package["phase"] == "recognize_completed"
        if Config.LP_CATALOG_MODEL_NAMING and not completed:
            from services.large_package_catalog_naming import queue_groups, ensure_round
            queue_groups(self.store, package_id)
            ensure_round(self.store, package_id)
            self.store.update_package(package_id, status="waiting_for_selection", phase="catalog_ready", progress=70,
                message="内容目录已就绪，可选择深度摘要范围；主题名称正在后台按证据完善", counts=self.store.counts(package_id))
            return True
        self.store.update_package(package_id, status="completed" if completed else "waiting_for_selection", phase="report_ready" if completed else "catalog_ready", progress=100 if completed else 70,
                                  message="内容目录已就绪；{} 个文件待补充识别，可先选择深度解析范围".format(pending), counts=self.store.counts(package_id))
        return True

    def _naming_one(self, package):
        # Resume older packages without making optional wording block selection.
        from services.large_package_catalog_naming import queue_groups, ensure_round
        package_id = package['id']
        if self._interrupted(package_id):
            return False
        queue_groups(self.store, package_id)
        ensure_round(self.store, package_id)
        self.store.update_package(package_id, status='waiting_for_selection', phase='catalog_ready', progress=70,
            message='内容目录已就绪，可选择深度摘要范围；主题名称正在后台按证据完善')
        return True

    def _submit_naming(self, package_id):
        from services.large_package_catalog_batch import process_batch
        if self._naming_future is not None and not self._naming_future.done():
            return False
        if self._naming_future is not None:
            try:
                self._naming_future.result()
            except Exception:
                LOGGER.exception('catalog naming task failed')
        if self._naming_executor is None:
            self._naming_executor = ThreadPoolExecutor(max_workers=1)
        def interrupted():
            item = self.store.get(package_id)
            return self.stop_requested or not item or item['status'] not in {'waiting_for_selection', 'completed'}
        def run_slice():
            # Continue ready batches without a poll sleep after every group;
            # bound package occupancy so other packages can get a turn.
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not interrupted():
                if not process_batch(self.store, package_id, interrupted):
                    break
        self._naming_future = self._naming_executor.submit(run_slice)
        return False

    def _deep_one(self, package):
        package_id = package["id"]
        root = package["root_path"]
        while True:
            if self.stop_requested:return True
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
                if Config.LP_CATALOG_MODEL_NAMING:
                    from services.large_package_catalog_naming import queue_groups, ensure_round
                    queue_groups(self.store,package_id)
                    with closing(self.store._connect()) as conn:
                        selection=conn.execute('SELECT id FROM selections WHERE package_id=? ORDER BY created_at DESC LIMIT 1',(package_id,)).fetchone()
                    if selection:ensure_round(self.store,package_id,'deep-'+selection[0])
                self.store.update_package(package_id, status="completed", phase="report_ready", progress=100,
                                          message="选定范围已完成深度解析和摘要", counts=counts)
                return True
            _, path = claimed[0]
            try:
                row = self.store.get_file(package_id, path)
                if not row:
                    raise FileNotFoundError(path)
                result = self._deep_parse(root, row)
                if result.get("summary_type") == "local_deep_failed":
                    raise RuntimeError(result.get("error") or "深度解析未获得正文")
                self.store.update_file(package_id, path, deep_status="completed", deep_summary=result, sha256=result.get("source_sha256") or row.get("sha256"), error=None)
                self.store.finish_queue(package_id, path, "deep", True)
                self._catalog_incremental(package_id)
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
            if not text.strip() and not document.get("evidence"):
                return {"summary_type": "local_deep_failed", "source_path": row["path"], "error": "；".join(document.get("warnings") or ["未获得可读正文"])}
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
            return self._catalog_background()
        if package["phase"] == "inventory":
            return self._inventory_one(package)
        if package["phase"] == "quick_parse":
            return self._quick_one(package)
        if package["phase"] in {"recognize", "recognize_completed"}:
            return self._recognize_one(package)
        if package["phase"] == "catalog_naming":
            return self._naming_one(package)
        if package["phase"] == "deep_parse":
            return self._deep_one(package)
        return False

    def _catalog_background(self):
        from services.large_package_semantic import embed_batch, name_batch
        with closing(self.store._connect()) as conn:
            pending = conn.execute("""SELECT q.package_id FROM queue q JOIN packages p ON p.id=q.package_id
              JOIN files f ON f.package_id=q.package_id AND f.path=q.path
              WHERE q.stage='catalog' AND q.status IN ('queued','retry')
                AND p.status IN ('waiting_for_selection','completed')
              ORDER BY (f.deep_status='completed') DESC,q.updated_at LIMIT 1""").fetchone()
            naming = conn.execute("""SELECT g.package_id FROM large_catalog_groups g JOIN packages p ON p.id=g.package_id
              WHERE g.status IN ('queued','retry') AND g.updated_at<=? AND p.status IN ('waiting_for_selection','completed')
              ORDER BY g.updated_at LIMIT 1""", (time.time(),)).fetchone()
        if self.stop_requested:return False
        if not pending and not naming:
            with closing(self.store._connect()) as conn:
                task=conn.execute("SELECT r.package_id FROM lp_catalog_rounds r JOIN packages p ON p.id=r.package_id WHERE r.status='active' AND p.status IN ('waiting_for_selection','completed') ORDER BY r.started LIMIT 1").fetchone()
            if task:
                return self._submit_naming(task['package_id'])
            return False
        name_first = naming and (not pending or getattr(self, '_catalog_named_last', False) is False)
        package_id = (naming if name_first else pending)['package_id']
        def interrupted():
            current = self.store.get(package_id)
            return self.stop_requested or not current or current['status'] not in {'waiting_for_selection', 'completed'}
        if name_first:
            self._catalog_named_last = True
            return bool(name_batch(self.store, package_id, cancelled=interrupted))
        self._catalog_named_last = False
        return bool(embed_batch(self.store, package_id, cancelled=interrupted))

    def _packages(self):
        with self.store._connect() as conn:
            rows = conn.execute("SELECT * FROM packages WHERE status IN ('queued','running','waiting_for_selection') AND phase IN ('inventory','quick_parse','recognize','recognize_completed','catalog_naming','deep_parse') ORDER BY updated_at").fetchall()
        return [self.store._row(row) for row in rows]

    def run_forever(self):
        try:
            while not self.stop_requested:
                if not self.run_once():
                    time.sleep(self.poll_seconds)
        finally:
            self.preview_runner.close()
            if self._naming_executor:
                self._naming_executor.shutdown(wait=True,cancel_futures=True)


def _human(value):
    try:
        from services.scanner import human_size
        return human_size(value)
    except Exception:
        return str(value)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    import signal
    worker = LargePackageWorker()
    signal.signal(signal.SIGTERM,lambda *_:setattr(worker,'stop_requested',True))
    signal.signal(signal.SIGINT,lambda *_:setattr(worker,'stop_requested',True))
    worker.run_forever()
