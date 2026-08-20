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

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


_request_context: contextvars.ContextVar["RequestData | None"] = contextvars.ContextVar(
    "sjfx_request", default=None
)


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


class SJFXFastAPI(FastAPI):
    """FastAPI application with a narrow bridge for synchronous legacy views."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config: dict[str, Any] = {}
        self._before_request: list[Callable] = []
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
                body = await request_obj.body()
                context = RequestData(
                    path=request_obj.url.path,
                    method=request_obj.method,
                    headers=request_obj.headers,
                    query=request_obj.query_params,
                    body=body,
                )

                def dispatch():
                    token = _request_context.set(context)
                    try:
                        for guard in self._before_request:
                            blocked = guard()
                            if blocked is not None:
                                return _normalise_response(blocked)
                        # FastAPI keeps matched values on Request.path_params.
                        # Do not use **kwargs in this endpoint signature: FastAPI
                        # otherwise treats it as a required query parameter.
                        return _normalise_response(view(**dict(request_obj.path_params)))
                    finally:
                        _request_context.reset(token)

                # Keep the request context scoped to this FastAPI request.
                return dispatch()

            self.add_api_route(_fastapi_path(path), endpoint, methods=methods, **kwargs)
            return view

        return register

    @contextmanager
    def test_request_context(self, path: str, method: str = "GET", json=None, headers=None, **_kwargs):
        body = json if json is not None else None
        token = _request_context.set(RequestData(path=path, method=method, headers=headers, body=body))
        try:
            yield
        finally:
            _request_context.reset(token)
