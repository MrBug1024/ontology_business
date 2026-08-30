from __future__ import annotations

import asyncio
import unittest

from app.request_body_limit import RequestBodyLimitMiddleware


class RequestBodyLimitMiddlewareTests(unittest.TestCase):
    @staticmethod
    def _run(*, headers, messages, limit=4):
        called = False
        received = bytearray()
        sent = []

        async def app(_scope, receive, send):
            nonlocal called
            called = True
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    break
                received.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        queue = list(messages)

        async def receive():
            if queue:
                return queue.pop(0)
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/upload",
            "raw_path": b"/upload",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        }
        middleware = RequestBodyLimitMiddleware(
            app,
            max_body_bytes=limit,
            paths={"/upload"},
        )
        asyncio.run(middleware(scope, receive, send))
        status = next(
            item["status"] for item in sent if item["type"] == "http.response.start"
        )
        return called, bytes(received), status

    def test_rejects_known_oversized_body_before_application(self) -> None:
        called, received, status = self._run(
            headers=[(b"content-length", b"5")],
            messages=[],
        )
        self.assertFalse(called)
        self.assertEqual(received, b"")
        self.assertEqual(status, 413)

    def test_rejects_chunked_body_before_multipart_application(self) -> None:
        called, received, status = self._run(
            headers=[],
            messages=[
                {"type": "http.request", "body": b"abc", "more_body": True},
                {"type": "http.request", "body": b"de", "more_body": False},
            ],
        )
        self.assertFalse(called)
        self.assertEqual(received, b"")
        self.assertEqual(status, 413)

    def test_replays_bounded_chunked_body_without_mutation(self) -> None:
        called, received, status = self._run(
            headers=[],
            messages=[
                {"type": "http.request", "body": b"ab", "more_body": True},
                {"type": "http.request", "body": b"cd", "more_body": False},
            ],
        )
        self.assertTrue(called)
        self.assertEqual(received, b"abcd")
        self.assertEqual(status, 204)

    def test_rejects_ambiguous_content_length(self) -> None:
        called, _received, status = self._run(
            headers=[(b"content-length", b"3"), (b"content-length", b"4")],
            messages=[],
        )
        self.assertFalse(called)
        self.assertEqual(status, 400)
