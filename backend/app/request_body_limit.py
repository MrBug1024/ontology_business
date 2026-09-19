"""ASGI request-size enforcement for multipart upload routes.

FastAPI resolves ``UploadFile`` parameters after Starlette has parsed the
multipart body. Route-level ``file.read(max + 1)`` checks therefore protect
storage but do not bound parser work. This middleware rejects declared oversized
bodies immediately. Every matched request is also bounded by its actual ASGI
body bytes, including requests with Content-Length, before reaching downstream
parsers. Accepted bodies are replayed once, preserving subsequent disconnects.
"""
from __future__ import annotations

from collections.abc import Iterable
import re

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        paths: Iterable[str],
        path_patterns: Iterable[str] = (),
    ) -> None:
        if int(max_body_bytes) <= 0:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.max_body_bytes = int(max_body_bytes)
        self.paths = frozenset(str(path) for path in paths)
        self.path_patterns = tuple(re.compile(pattern) for pattern in path_patterns)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": "请求体超过上传大小限制"},
        )
        await response(scope, receive, send)

    @staticmethod
    def _content_lengths(scope: Scope) -> list[int] | None:
        values: list[int] = []
        for raw_name, raw_value in scope.get("headers", []):
            if raw_name.lower() != b"content-length":
                continue
            try:
                for part in raw_value.decode("ascii").split(","):
                    value = part.strip(" \t")
                    if not value.isdecimal():
                        return None
                    values.append(int(value))
            except (UnicodeDecodeError, ValueError):
                return None
        if any(value < 0 for value in values) or len(set(values)) > 1:
            return None
        return values

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method", "").upper() not in {"POST", "PUT", "PATCH"}
            or not (
                scope.get("path") in self.paths
                or any(pattern.fullmatch(scope.get("path", "")) for pattern in self.path_patterns)
            )
        ):
            await self.app(scope, receive, send)
            return

        lengths = self._content_lengths(scope)
        if lengths is None:
            response = JSONResponse(
                status_code=400,
                content={"detail": "Content-Length 请求头无效"},
            )
            await response(scope, receive, send)
            return
        if lengths and lengths[0] > self.max_body_bytes:
            await self._reject(scope, receive, send)
            return

        # Bound actual bytes as well as the declaration before multipart work.
        # A bytearray avoids an unbounded list of empty or one-byte ASGI frames.
        body = bytearray()
        disconnected = False
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
                break
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_body_bytes:
                await self._reject(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        payload = bytes(body)
        del body
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if disconnected:
                return {"type": "http.disconnect"}
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": payload, "more_body": False}

        await self.app(scope, replay, send)
