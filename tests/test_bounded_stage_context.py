"""Behavioral regression of the actual bounded-stage functions from source.

Extracting these AST nodes avoids importing the product's heavyweight model,
parser and storage dependencies. The functions are not reimplemented. The actual product module supplies the context import and the bounded-stage
globals. These tests do not execute E5 or installed acceptance.
"""
import ast
import contextvars
import math
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
CANDIDATE = HERE.parent / 'services/package_analysis.py'


def actual_bounded_functions(path):
    parsed = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    selected = [node for node in parsed.body
                if (isinstance(node, ast.ImportFrom) and node.module == 'contextvars')
                or (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in {'_bounded_timeout_seconds', '_call_with_timeout'})]
    module = ast.Module(body=selected, type_ignores=[])
    namespace = {'math': math, 'threading': threading}
    exec(compile(module, str(path), 'exec'), namespace)
    return namespace['_call_with_timeout']


def test_candidate_success_propagates_and_isolates_business_context():
    bounded_call = actual_bounded_functions(CANDIDATE)
    owner = contextvars.ContextVar('regression_owner', default='outside')
    operation = contextvars.ContextVar('regression_operation', default='outside')
    owner_token = owner.set('synthetic-owner')
    operation_token = operation.set('ordinary-selected-source')
    caller_ident = threading.get_ident()
    try:
        def stage(value, *, scale):
            observed = (owner.get(), operation.get(), threading.get_ident() != caller_ident)
            owner.set('worker-only')
            return observed, value * scale

        result = bounded_call(stage, 1, 7, scale=3)
        assert result == (('synthetic-owner', 'ordinary-selected-source', True), 21)
        assert owner.get() == 'synthetic-owner'
        assert operation.get() == 'ordinary-selected-source'
    finally:
        owner.reset(owner_token)
        operation.reset(operation_token)


def test_candidate_preserves_original_stage_failure():
    bounded_call = actual_bounded_functions(CANDIDATE)
    failure = ValueError('synthetic transport failure')
    def stage():
        raise failure
    with pytest.raises(ValueError) as observed:
        bounded_call(stage, 1)
    assert observed.value is failure


def test_candidate_timeout_remains_bounded_and_worker_can_finish():
    bounded_call = actual_bounded_functions(CANDIDATE)
    release, started, finished = threading.Event(), threading.Event(), threading.Event()
    def stage():
        started.set()
        try:
            assert release.wait(2)
            return 'late result'
        finally:
            finished.set()
    began = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match='0.1'):
            bounded_call(stage, 0.1)
        elapsed = time.monotonic() - began
        assert started.is_set() and not finished.is_set()
        assert 0.09 <= elapsed < 1.0
    finally:
        release.set()
        assert finished.wait(1)


def test_candidate_repeated_calls_copy_fresh_context():
    bounded_call = actual_bounded_functions(CANDIDATE)
    scope = contextvars.ContextVar('regression_selection', default='none')
    first = scope.set('selection-one')
    try:
        assert bounded_call(scope.get, 1) == 'selection-one'
        scope.set('selection-two')
        assert bounded_call(scope.get, 1) == 'selection-two'
    finally:
        scope.reset(first)
