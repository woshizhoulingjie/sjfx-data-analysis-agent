"""Per-turn budgets and in-memory synthesis reuse, isolated from other turns."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import time


@dataclass
class ExecutionControl:
    deadline: float
    checkpoint: object = None
    progress: object = None
    prepared: object = None
    prepared_query: object = None

    def remaining(self, reserve=0):
        return max(0., self.deadline - time.monotonic() - reserve)

    def publish(self, **event):
        if self.checkpoint:
            self.checkpoint()
        if self.progress:
            self.progress(event)


_CURRENT = ContextVar('large_qa_execution', default=None)


def current_execution():
    return _CURRENT.get()


@contextmanager
def bind_execution(control):
    token = _CURRENT.set(control)
    try:
        yield control
    finally:
        _CURRENT.reset(token)
