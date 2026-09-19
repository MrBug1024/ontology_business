"""Exercise upload byte limits with raw ASGI frames before downstream parsing."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field

import pytest
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.types import Message, Scope

from app.request_body_limit import RequestBodyLimitMiddleware


LIMIT = 16
UPLOAD_PATH = "/api/business-distillation/project_a/conversation/attachments"
UPLOAD_PATTERN = r"/api/business-distillation/[^/]+/conversation/attachments"


@dataclass
class Observation:
    received: int = 0
    calls: int = 0
    body: bytes | None = None
    disconnected: bool = False
    next_message: Message | None = None
    sent: list[Message] = field(default_factory=list)

    @property
    def status(self) -> int | None:
        return next((message["status"] for message in self.sent if message["type"] == "http.response.start"), None)


def request_frame(body: bytes, more: bool = False) -> Message:
    return {"type": "http.request", "body": body, "more_body": more}


def exercise(
    frames: list[Message], *, headers: list[tuple[bytes, bytes]] | None = None,
    path: str = UPLOAD_PATH, method: str = "POST", read_after_body: bool = False,
    scope_type: str = "http",
) -> Observation:
    observation = Observation()
    queue = deque(frames)
    scope: Scope = {
        "type": scope_type, "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "headers": headers or [],
        "server": ("testserver", 80), "client": ("testclient", 10000),
    }

    async def receive() -> Message:
        observation.received += 1
        if not queue:
            raise AssertionError("Downstream read past the supplied ASGI messages")
        return queue.popleft()

    async def send(message: Message) -> None:
        observation.sent.append(message)

    async def downstream(app_scope, app_receive, app_send) -> None:
        observation.calls += 1
        if app_scope["type"] != "http":
            observation.next_message = await app_receive()
            return
        try:
            observation.body = await Request(app_scope, app_receive).body()
        except ClientDisconnect:
            observation.disconnected = True
            return
        if read_after_body:
            observation.next_message = await app_receive()
        await Response(observation.body, media_type="application/octet-stream")(app_scope, app_receive, app_send)

    middleware = RequestBodyLimitMiddleware(
        downstream, max_body_bytes=LIMIT, paths={"/api/fixed-upload"}, path_patterns=(UPLOAD_PATTERN,),
    )
    asyncio.run(middleware(scope, receive, send))
    return observation


@pytest.mark.parametrize("path", [UPLOAD_PATH, "/api/business-distillation/another_project/conversation/attachments", "/api/fixed-upload"])
def test_declared_oversized_upload_rejects_before_receiving_or_parsing(path):
    result = exercise([request_frame(b"not consumed")], path=path, headers=[(b"content-length", b"17")])
    assert result.status == 413
    assert result.received == 0
    assert result.calls == 0


@pytest.mark.parametrize("headers", [[], [(b"content-length", b"1")], [(b"content-length", b"0")]])
def test_actual_oversized_body_rejects_even_with_missing_or_falsely_small_length(headers):
    result = exercise([request_frame(b"abcdefgh", True), request_frame(b"ijklmnopq")], headers=headers)
    assert result.status == 413
    assert result.received == 2
    assert result.calls == 0


@pytest.mark.parametrize("headers", [[], [(b"content-length", b"16")], [(b"Content-Length", b"16"), (b"content-length", b"16")], [(b"content-length", b"16, 16")]])
def test_boundary_body_reaches_downstream_whole_and_unchanged(headers):
    payload = b"\x00\xffmultipart-body"
    assert len(payload) == LIMIT
    result = exercise([request_frame(b"", True), request_frame(payload[:7], True), request_frame(payload[7:])], headers=headers)
    assert result.status == 200
    assert result.body == payload
    assert b"".join(message.get("body", b"") for message in result.sent) == payload
    assert result.calls == 1
    assert result.received == 3


def test_small_declared_length_does_not_truncate_valid_actual_body():
    result = exercise([request_frame(b"actual", True), request_frame(b" bytes")], headers=[(b"content-length", b"1")])
    assert result.status == 200
    assert result.body == b"actual bytes"


@pytest.mark.parametrize("headers", [[(b"content-length", b"1"), (b"content-length", b"2")], [(b"content-length", b"1,2")]])
def test_conflicting_content_lengths_reject_before_reading(headers):
    result = exercise([request_frame(b"body")], headers=headers)
    assert result.status == 400
    assert result.received == 0
    assert result.calls == 0


@pytest.mark.parametrize("value", [b"", b"-1", b"+1", b"1_0", b"1.0", b"1e1", b"1,", b"\xff"])
def test_invalid_content_length_syntax_rejects_before_reading(value):
    result = exercise([request_frame(b"body")], headers=[(b"content-length", value)])
    assert result.status == 400
    assert result.received == 0
    assert result.calls == 0


@pytest.mark.parametrize("path,method", [
    ("/api/ordinary-endpoint", "POST"),
    ("/api/business-distillation/p/conversation/attachments/extra", "POST"),
    ("/unrelated/api/business-distillation/p/conversation/attachments", "POST"),
    (UPLOAD_PATH, "GET"),
])
def test_unrelated_routes_and_methods_pass_through_without_this_upload_limit(path, method):
    payload = b"unrestricted by this middleware"
    result = exercise([request_frame(payload)], path=path, method=method, headers=[(b"content-length", str(len(payload)).encode())])
    assert result.status == 200
    assert result.body == payload
    assert result.calls == 1


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
def test_mutating_upload_methods_are_limited(method):
    result = exercise([request_frame(b"x" * (LIMIT + 1))], method=method)
    assert result.status == 413
    assert result.calls == 0


@pytest.mark.parametrize("frames", [[{"type": "http.disconnect"}], [request_frame(b"partial", True), {"type": "http.disconnect"}]])
def test_disconnect_during_upload_never_becomes_a_successful_partial_body(frames):
    result = exercise(frames)
    assert result.disconnected
    assert result.body is None
    assert result.status is None
    assert result.received == len(frames)


def test_disconnect_after_complete_body_remains_visible_to_downstream():
    result = exercise([request_frame(b"complete"), {"type": "http.disconnect"}], read_after_body=True)
    assert result.body == b"complete"
    assert result.next_message == {"type": "http.disconnect"}
    assert result.received == 2


def test_non_http_scope_is_forwarded_without_consuming_protocol_messages():
    result = exercise([{"type": "websocket.connect"}], scope_type="websocket")
    assert result.next_message == {"type": "websocket.connect"}
    assert result.calls == 1
    assert result.received == 1
