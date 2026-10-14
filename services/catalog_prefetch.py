"""Overlap bounded evidence-cache warming with an existing model request.

No model calls, job claims, label application or extra GPU concurrency. Current
revision/owner keys and final application guards remain in the naming module.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import logging
import threading

LOGGER = logging.getLogger(__name__)
_lock = threading.Lock()
_executor = None
_future = None
_enabled = ContextVar('sjfx_evidence_prefetch', default=False)
_owned = ContextVar('sjfx_evidence_prefetch_futures', default=None)


@contextmanager
def scope():
    token = _enabled.set(True)
    futures = []
    owned_token = _owned.set(futures)
    try:
        yield
    finally:
        _enabled.reset(token)
        _owned.reset(owned_token)
        # Usually already finished during chat. Never leave work using a store
        # after its worker scope has returned (important for cancellation/tests).
        for future in futures:
            try:
                future.result()
            except Exception:
                LOGGER.debug('Evidence prefetch ended without a result', exc_info=True)


def enabled():
    return _enabled.get()


def start(store, package_id, group_ids, cancelled=lambda: False, limit=4):
    global _executor, _future
    ids = list(dict.fromkeys(group_ids))[:max(0, min(8, int(limit)))]
    if not ids or cancelled():
        return False
    with _lock:
        # One warm-up for the entire worker: no unbounded queue across packages.
        if _future is not None and not _future.done():
            return False
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='sjfx-evidence-prefetch')
        def warm():
            from services import large_package_catalog_naming as naming
            for group_id in ids:
                if cancelled():
                    break
                try:
                    naming.bundle(store, package_id, group_id)
                except Exception:
                    # Foreground preparation owns retry/status; warming is optional.
                    LOGGER.debug('Evidence prefetch skipped for %s/%s', package_id, group_id, exc_info=True)
        _future = _executor.submit(copy_context().run, warm)
        owned = _owned.get()
        if owned is not None:
            owned.append(_future)
        return True


def shutdown():
    global _executor, _future
    with _lock:
        executor, _executor = _executor, None
        _future = None
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)
