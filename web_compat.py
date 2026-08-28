"""Small FastAPI compatibility boundary for the legacy synchronous handlers.

The analysis services are synchronous and are intentionally run by a separate
Worker.  This module keeps HTTP concerns in FastAPI while allowing the existing
handlers to be migrated incrementally without retaining Flask as a runtime
dependency.  It is not a Flask emulation layer for new code.
"""

from __future__ import annotations

import contextvars
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders


_request_context: contextvars.ContextVar["RequestData | None"] = contextvars.ContextVar(
    "sjfx_request", default=None
)


class InternalExecutionCapability:
    """A process-local capability that cannot be asserted by request data."""

    def __init__(self, name: str):
        self._active = contextvars.ContextVar(str(name), default=False)

    def get(self) -> bool:
        return bool(self._active.get())

    @contextmanager
    def activate(self):
        token = self._active.set(True)
        try:
            yield
        finally:
            self._active.reset(token)


class _Headers(dict):
    def get(self, key, default=None):
        return super().get(str(key).lower(), default)


class CompatJSONResponse(JSONResponse):
    def get_json(self, silent: bool = False):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            if silent:
                return None
            raise


class RequestData:
    """Only the request surface used by the migrated synchronous handlers."""

    def __init__(self, *, path: str, method: str = "GET", headers=None, query=None, body=None):
        self.path = path
        self.method = method
        self.headers = _Headers({str(key).lower(): value for key, value in (headers or {}).items()})
        self.args = dict(query or {})
        self._body = body

    def get_json(self, silent: bool = False):
        if self._body is None or self._body == b"":
            return None
        if isinstance(self._body, (dict, list)):
            return self._body
        try:
            raw = self._body.decode("utf-8") if isinstance(self._body, bytes) else str(self._body)
            return json.loads(raw)
        except (UnicodeDecodeError, TypeError, ValueError):
            if silent:
                return None
            raise


class _RequestBodyTooLarge(Exception):
    """Internal signal used to stop buffering an oversized request body."""


class _RequestProxy:
    def _current(self) -> RequestData:
        current = _request_context.get()
        if current is None:
            raise RuntimeError("request context is unavailable")
        return current

    def __getattr__(self, name):
        return getattr(self._current(), name)


request = _RequestProxy()


def has_request_context() -> bool:
    return _request_context.get() is not None


def jsonify(payload: Any) -> CompatJSONResponse:
    return CompatJSONResponse(content=payload)


def render_template(name: str, **context) -> HTMLResponse:
    templates = Jinja2Templates(directory="templates")
    html = templates.env.get_template(name).render(**context)
    return HTMLResponse(html)


def send_from_directory(directory: str, filename: str, as_attachment: bool = False):
    path = Path(directory) / filename
    return FileResponse(
        path,
        filename=filename if as_attachment else None,
        content_disposition_type="attachment" if as_attachment else "inline",
    )


def _normalise_response(value: Any) -> Response:
    if isinstance(value, Response):
        return value
    if isinstance(value, tuple):
        body, status = value[0], value[1]
        response = _normalise_response(body)
        response.status_code = int(status)
        return response
    if isinstance(value, (dict, list)):
        return CompatJSONResponse(content=value)
    if value is None:
        return Response(status_code=204)
    return HTMLResponse(str(value))


def _fastapi_path(path: str) -> str:
    import re

    return re.sub(r"<(?:(path):)?([A-Za-z_][A-Za-z0-9_]*)>", lambda m: "{%s%s}" % (m.group(2), ":path" if m.group(1) else ""), path)


def _declared_content_length(request_obj: Request) -> int | None:
    """Parse Content-Length defensively, including duplicate/comma forms.

    Multiple identical values are valid in practice after some proxy hops.  A
    disagreement is rejected because accepting it can create request-smuggling
    ambiguity between the proxy and the application.
    """

    values: list[int] = []
    for raw_value in request_obj.headers.getlist("content-length"):
        for item in raw_value.split(","):
            item = item.strip()
            if not item or any(character not in "0123456789" for character in item):
                raise ValueError("invalid Content-Length")
            values.append(int(item))
    if not values:
        return None
    if any(value != values[0] for value in values[1:]):
        raise ValueError("conflicting Content-Length values")
    return values[0]


async def _read_request_body(request_obj: Request, limit: int | None) -> bytes:
    """Read the ASGI body incrementally without ever buffering beyond *limit*."""

    body = bytearray()
    async for chunk in request_obj.stream():
        if not chunk:
            continue
        if limit is not None and len(body) + len(chunk) > limit:
            raise _RequestBodyTooLarge
        body.extend(chunk)
    return bytes(body)


class _SecurityHeadersMiddleware:
    """Attach response headers without consuming or buffering the ASGI body."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message):
            if message.get("type") == "http.response.start":
                headers = MutableHeaders(scope=message)
                defaults = {
                    "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY",
                    "Referrer-Policy": "no-referrer",
                    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                }
                for name, value in defaults.items():
                    if name not in headers:
                        headers[name] = value
                path = str(scope.get("path") or "")
                if (
                    path.startswith("/api/") or path.startswith("/outputs/")
                ) and "Cache-Control" not in headers:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


class SJFXFastAPI(FastAPI):
    """FastAPI application with a narrow bridge for synchronous legacy views."""

    def __init__(
        self,
        *args,
        max_content_length: int | None = None,
        security_headers: bool = False,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if max_content_length is not None:
            max_content_length = int(max_content_length)
            if max_content_length <= 0:
                raise ValueError("max_content_length must be positive")
        self.max_content_length = max_content_length
        self.config: dict[str, Any] = {}
        self._before_request: list[Callable] = []
        if security_headers:
            self.add_middleware(_SecurityHeadersMiddleware)
        # Flask previously served this project-local directory automatically.
        # Keep that URL contract while FastAPI owns the HTTP runtime.
        static_dir = Path("static")
        if static_dir.is_dir():
            self.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def before_request(self, func: Callable):
        self._before_request.append(func)
        return func

    def route(self, path: str, *, methods=None, **kwargs):
        methods = methods or ["GET"]

        def register(view: Callable):
            async def endpoint(request_obj: Request):
                # Authentication/authorization guards only need request
                # metadata.  Run them before asking the ASGI server for a body
                # byte so an unauthorized client cannot make us buffer a large
                # payload.
                guard_context = RequestData(
                    path=request_obj.url.path,
                    method=request_obj.method,
                    headers=request_obj.headers,
                    query=request_obj.query_params,
                    body=None,
                )

                def dispatch_guards():
                    token = _request_context.set(guard_context)
                    try:
                        for guard in self._before_request:
                            blocked = guard()
                            if blocked is not None:
                                return _normalise_response(blocked)
                        return None
                    finally:
                        _request_context.reset(token)

                blocked = await run_in_threadpool(dispatch_guards)
                if blocked is not None:
                    return blocked

                try:
                    declared_length = _declared_content_length(request_obj)
                except ValueError:
                    return CompatJSONResponse(
                        content={"ok": False, "error": "请求 Content-Length 无效"},
                        status_code=400,
                    )
                if (
                    self.max_content_length is not None
                    and declared_length is not None
                    and declared_length > self.max_content_length
                ):
                    return CompatJSONResponse(
                        content={"ok": False, "error": "请求体超过允许大小"},
                        status_code=413,
                    )

                try:
                    body = await _read_request_body(request_obj, self.max_content_length)
                except _RequestBodyTooLarge:
                    return CompatJSONResponse(
                        content={"ok": False, "error": "请求体超过允许大小"},
                        status_code=413,
                    )

                context = RequestData(
                    path=request_obj.url.path,
                    method=request_obj.method,
                    headers=request_obj.headers,
                    query=request_obj.query_params,
                    body=body,
                )
                path_params = dict(request_obj.path_params)

                def dispatch_view():
                    token = _request_context.set(context)
                    try:
                        # FastAPI keeps matched values on Request.path_params.
                        # Do not use **kwargs in this endpoint signature: FastAPI
                        # otherwise treats it as a required query parameter.
                        return _normalise_response(view(**path_params))
                    finally:
                        _request_context.reset(token)

                # Legacy views perform synchronous file/SQLite work.  Running
                # them in Starlette's worker pool keeps the event loop available
                # for progress, status and cancellation requests. Context vars
                # are explicitly scoped inside the worker for request isolation.
                return await run_in_threadpool(dispatch_view)

            self.add_api_route(_fastapi_path(path), endpoint, methods=methods, **kwargs)
            return view

        return register

    @contextmanager
    def test_request_context(self, path: str, method: str = "GET", json=None, headers=None, **_kwargs):
        body = json if json is not None else None
        split = urlsplit(path)
        token = _request_context.set(RequestData(
            path=split.path, method=method, headers=headers,
            query=dict(parse_qsl(split.query, keep_blank_values=True)), body=body,
        ))
        try:
            yield
        finally:
            _request_context.reset(token)
