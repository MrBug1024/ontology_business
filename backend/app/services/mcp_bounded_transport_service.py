"""Pinned remote MCP sessions with limits before protocol deserialization."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from ..config import get_settings
from ..models import MCPConfig
from . import mcp_service


MAX_RESPONSE_BYTES = 1_048_576
MAX_SSE_FRAME_BYTES = 262_144
MAX_SESSION_SECONDS = 60.0


class MCPResponseLimitError(httpx.TransportError):
    """Never include remote bytes or connection details in boundary errors."""


@dataclass
class _Budget:
    limit: int
    consumed: int = 0

    def accept(self, size: int) -> None:
        self.consumed += size
        if self.consumed > self.limit:
            raise MCPResponseLimitError("MCP 响应超过允许的读取大小")


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream, budget: _Budget, frame_limit: int | None):
        self.inner, self.budget, self.frame_limit = inner, budget, frame_limit
        self._frame = bytearray()

    async def __aiter__(self):
        try:
            async for chunk in self.inner:
                self.budget.accept(len(chunk))
                if self.frame_limit is not None:
                    # A single network chunk is bounded by the session budget.
                    # Keep delimiters spanning reads and reject before the SDK
                    # can accumulate an oversized event or parse its JSON.
                    self._frame.extend(chunk)
                    while self._frame:
                        markers = [(self._frame.find(mark), len(mark)) for mark in (b"\n\n", b"\r\n\r\n")]
                        found = [(index, length) for index, length in markers if index >= 0]
                        if not found:
                            if len(self._frame) > self.frame_limit:
                                raise MCPResponseLimitError("MCP 事件超过允许的读取大小")
                            break
                        index, length = min(found)
                        if index + length > self.frame_limit:
                            raise MCPResponseLimitError("MCP 事件超过允许的读取大小")
                        del self._frame[:index + length]
                yield chunk
        finally:
            await self.inner.aclose()

    async def aclose(self) -> None:
        await self.inner.aclose()


class BoundedTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, budget: _Budget, frame_limit: int):
        self.inner, self.budget, self.frame_limit = inner, budget, frame_limit

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.headers["Accept-Encoding"] = "identity"
        response = await self.inner.handle_async_request(request)
        try:
            if response.headers.get("Content-Encoding", "identity").strip().lower() not in {"", "identity"}:
                raise MCPResponseLimitError("MCP 资料响应不支持压缩传输")
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    declared = int(length)
                except ValueError as exc:
                    raise MCPResponseLimitError("MCP 响应长度无效") from exc
                if declared < 0 or declared > self.budget.limit - self.budget.consumed:
                    raise MCPResponseLimitError("MCP 响应超过允许的读取大小")
            is_sse = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() == "text/event-stream"
            response.stream = _BoundedStream(response.stream, self.budget, self.frame_limit if is_sse else None)
            return response
        except BaseException:
            await response.aclose()
            raise

    async def aclose(self) -> None:
        await self.inner.aclose()


@asynccontextmanager
async def bounded_session(cfg: MCPConfig, *, timeout_seconds: float | None = None,
                          max_response_bytes: int = MAX_RESPONSE_BYTES,
                          max_sse_frame_bytes: int = MAX_SSE_FRAME_BYTES):
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamable_http_client

    if cfg.transport not in {"sse", "streamable_http", "http"}:
        raise ValueError("此读取仅支持远程 MCP 连接")
    if not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES or not 1 <= max_sse_frame_bytes <= MAX_SSE_FRAME_BYTES:
        raise ValueError("MCP 响应上限无效")
    seconds = min(MAX_SESSION_SECONDS, max(1.0, float(timeout_seconds if timeout_seconds is not None else get_settings().mcp_operation_timeout_seconds)))
    budget = _Budget(max_response_bytes)
    async with asyncio.timeout(seconds):
        target = await asyncio.to_thread(mcp_service._assert_safe_remote_target, cfg.url)

        def client_factory(headers=None, timeout=None, auth=None):
            return httpx.AsyncClient(headers=headers, auth=auth,
                timeout=timeout or httpx.Timeout(min(10.0, seconds), read=seconds),
                follow_redirects=False, trust_env=False,
                transport=BoundedTransport(mcp_service._PinnedAsyncHTTPTransport(target), budget, max_sse_frame_bytes))

        headers = mcp_service._request_headers(cfg)
        if cfg.transport == "sse":
            async with sse_client(cfg.url, headers=headers or None, sse_read_timeout=seconds,
                                  httpx_client_factory=client_factory) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        else:
            async with client_factory(headers=headers) as client:
                async with streamable_http_client(cfg.url, http_client=client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
