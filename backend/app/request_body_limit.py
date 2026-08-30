"""ASGI request-size enforcement for multipart upload routes.

FastAPI resolves ``UploadFile`` parameters after Starlette has parsed the
multipart body. Route-level ``file.read(max + 1)`` checks therefore protect
storage but do not bound parser work. This middleware rejects known oversized
bodies immediately and buffers unknown-length bodies only up to the same hard
limit before handing them to the multipart parser.
"""
from __future__ import annotations

from collections.abc import Iterable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        paths: Iterable[str],
    ) -> None:
        if int(max_body_bytes) <= 0:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.max_body_bytes = int(max_body_bytes)
        self.paths = frozenset(str(path) for path in paths)

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
                values.extend(
                    int(part.strip())
                    for part in raw_value.decode("ascii").split(",")
                )
            except (UnicodeDecodeError, ValueError):
                return None
        if any(value < 0 for value in values) or len(set(values)) > 1:
            return None
        return values

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method", "").upper() not in {"POST", "PUT", "PATCH"}
            or scope.get("path") not in self.paths
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

        # A missing Content-Length generally means chunked transfer. Read only
        # the bounded body here so multipart parsing never sees excess bytes.
        if not lengths:
            messages: list[Message] = []
            total = 0
            while True:
                message = await receive()
                messages.append(message)
                if message["type"] == "http.disconnect":
                    break
                if message["type"] != "http.request":
                    continue
                total += len(message.get("body", b""))
                if total > self.max_body_bytes:
                    await self._reject(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break

            index = 0

            async def replay() -> Message:
                nonlocal index
                if index < len(messages):
                    message = messages[index]
                    index += 1
                    return message
                return {"type": "http.request", "body": b"", "more_body": False}

            await self.app(scope, replay, send)
            return

        await self.app(scope, receive, send)
