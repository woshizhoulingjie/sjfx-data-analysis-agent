import asyncio
import json
import threading
import unittest

from web_compat import InternalExecutionCapability, SJFXFastAPI, request


def _async_test(function):
    def run(self):
        return asyncio.run(function(self))

    return run


async def _request(app, path="/api/test", method="GET", headers=None, chunks=None, fail_on_receive=False):
    raw_headers = [(str(name).lower().encode("latin-1"), str(value).encode("latin-1")) for name, value in (headers or [])]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "state": {},
    }
    body_chunks = list(chunks or [b""])
    receive_count = 0
    sent = []

    async def receive():
        nonlocal receive_count
        receive_count += 1
        if fail_on_receive:
            raise AssertionError("request body was read")
        if body_chunks:
            chunk = body_chunks.pop(0)
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": bool(body_chunks),
            }
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    response_headers = {
        name.decode("latin-1").lower(): value.decode("latin-1")
        for name, value in start.get("headers", [])
    }
    return start["status"], response_headers, body, receive_count


class WebRuntimeSafetyTests(unittest.TestCase):
    @_async_test
    async def test_internal_execution_capability_cannot_be_asserted_by_json(self):
        app = SJFXFastAPI(max_content_length=256)
        capability = InternalExecutionCapability("test_internal_execution")

        @app.route("/api/test", methods=["POST"])
        def view():
            return {
                "internal": capability.get(),
                "client_claim": bool((request.get_json() or {}).get("_worker_execution")),
            }

        raw_body = json.dumps({"_worker_execution": True}).encode("utf-8")
        external = await _request(
            app,
            method="POST",
            headers=[("content-length", str(len(raw_body))), ("content-type", "application/json")],
            chunks=[raw_body],
        )
        self.assertTrue(json.loads(external[2])["client_claim"])
        self.assertFalse(json.loads(external[2])["internal"])

        with capability.activate():
            internal = await _request(
                app,
                method="POST",
                headers=[("content-length", str(len(raw_body))), ("content-type", "application/json")],
                chunks=[raw_body],
            )
        self.assertTrue(json.loads(internal[2])["internal"])
        self.assertFalse(capability.get())

    def test_internal_request_context_preserves_query_parameters(self):
        app = SJFXFastAPI()
        with app.test_request_context("/api/test?compact=1&limit=25"):
            self.assertEqual(request.path, "/api/test")
            self.assertEqual(request.args["compact"], "1")
            self.assertEqual(request.args["limit"], "25")

    @_async_test
    async def test_guard_rejects_before_any_body_read(self):
        app = SJFXFastAPI(max_content_length=4, security_headers=True)
        view_called = False

        @app.before_request
        def guard():
            if request.headers.get("x-token") != "secret":
                return {"ok": False}, 401

        @app.route("/api/test", methods=["POST"])
        def view():
            nonlocal view_called
            view_called = True
            return {"ok": True}

        status, _headers, _body, receive_count = await _request(
            app,
            method="POST",
            headers=[("content-length", "999999")],
            chunks=[b"never read"],
            fail_on_receive=True,
        )
        self.assertEqual(status, 401)
        self.assertEqual(receive_count, 0)
        self.assertFalse(view_called)

    @_async_test
    async def test_content_length_is_rejected_without_reading_body(self):
        app = SJFXFastAPI(max_content_length=4)

        @app.route("/api/test", methods=["POST"])
        def view():
            self.fail("oversized request reached the view")

        status, _headers, body, receive_count = await _request(
            app,
            method="POST",
            headers=[("content-length", "5")],
            chunks=[b"12345"],
            fail_on_receive=True,
        )
        self.assertEqual(status, 413)
        self.assertEqual(receive_count, 0)
        self.assertFalse(json.loads(body)["ok"])

    @_async_test
    async def test_streaming_limit_stops_chunked_body_before_view(self):
        app = SJFXFastAPI(max_content_length=4)
        view_called = False

        @app.route("/api/test", methods=["POST"])
        def view():
            nonlocal view_called
            view_called = True
            return {"ok": True}

        status, _headers, _body, receive_count = await _request(
            app,
            method="POST",
            chunks=[b"123", b"45", b"unread"],
        )
        self.assertEqual(status, 413)
        self.assertEqual(receive_count, 2)
        self.assertFalse(view_called)

    @_async_test
    async def test_valid_json_retains_legacy_request_and_response_semantics(self):
        app = SJFXFastAPI(max_content_length=128)
        raw_body = json.dumps({"name": "sjfx"}).encode("utf-8")

        @app.route("/api/test", methods=["POST"])
        def view():
            return {"name": request.get_json()["name"], "method": request.method}, 202

        status, _headers, body, _receive_count = await _request(
            app,
            method="POST",
            headers=[("content-length", str(len(raw_body))), ("content-type", "application/json")],
            chunks=[raw_body],
        )
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body), {"name": "sjfx", "method": "POST"})

    @_async_test
    async def test_synchronous_views_overlap_and_keep_request_context_isolated(self):
        app = SJFXFastAPI(max_content_length=64)
        lock = threading.Lock()
        both_started = threading.Event()
        active = 0

        @app.route("/api/test")
        def view():
            nonlocal active
            marker = request.headers.get("x-marker")
            with lock:
                active += 1
                if active == 2:
                    both_started.set()
            overlapped = both_started.wait(timeout=0.75)
            marker_after_wait = request.headers.get("x-marker")
            with lock:
                active -= 1
            return {
                "marker": marker,
                "marker_after_wait": marker_after_wait,
                "overlapped": overlapped,
            }

        first, second = await asyncio.gather(
            _request(app, headers=[("x-marker", "first")]),
            _request(app, headers=[("x-marker", "second")]),
        )
        first_payload = json.loads(first[2])
        second_payload = json.loads(second[2])
        self.assertTrue(first_payload["overlapped"])
        self.assertTrue(second_payload["overlapped"])
        self.assertEqual(first_payload["marker"], first_payload["marker_after_wait"])
        self.assertEqual(second_payload["marker"], second_payload["marker_after_wait"])
        self.assertEqual({first_payload["marker"], second_payload["marker"]}, {"first", "second"})

    @_async_test
    async def test_security_headers_cover_api_responses(self):
        app = SJFXFastAPI(max_content_length=64, security_headers=True)

        @app.route("/api/test")
        def view():
            return {"ok": True}

        status, headers, _body, _receive_count = await _request(app)
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["referrer-policy"], "no-referrer")
        self.assertEqual(headers["permissions-policy"], "camera=(), microphone=(), geolocation=()")
        self.assertEqual(headers["cache-control"], "no-store")


if __name__ == "__main__":
    unittest.main()
