"""Waiting callers must reuse either successfully initialized backend."""
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import patch

import pytest

from services.offline_translation import OfflineNLLBProvider


@pytest.mark.parametrize('loaded_field', ['_translator', '_model'])
def test_waiting_loader_rechecks_initialized_backend(loaded_field):
    provider = OfflineNLLBProvider('unused-local-model')
    entered = threading.Event()

    class ObservedLock:
        def __enter__(self):
            entered.set()
            lock.acquire()

        def __exit__(self, *_):
            lock.release()

    lock = threading.Lock()
    provider._load_lock = ObservedLock()
    lock.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            # If the backend is not rechecked inside the lock, the call would
            # attempt an unnecessary native load after the wait is released.
            with patch.dict('sys.modules', {'transformers': None}):
                future = executor.submit(provider._ensure_loaded)
                assert entered.wait(5), 'loader never reached the contested lock'
                setattr(provider, loaded_field, object())
                lock.release()
                future.result(timeout=5)
    finally:
        if lock.locked():
            lock.release()
