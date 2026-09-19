"""Bounded anonymous GET adapter over the existing pinned-origin transport."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from threading import BoundedSemaphore

import httpx

from .mcp_service import _assert_safe_remote_target, _pinned_http_client


MAX_DOCUMENT_BYTES = 262_144
DNS_TIMEOUT_SECONDS = 5
HTTP_TIMEOUT_SECONDS = 20
_RESOLVER = ThreadPoolExecutor(max_workers=2, thread_name_prefix="discovery-dns")
_RESOLUTION_SLOTS = BoundedSemaphore(2)
_TEXT_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain", "application/json"})


class ReadonlyHttpError(ValueError):
    """Safe adapter rejection; never expose endpoint response headers or errors."""


@dataclass(frozen=True)
class ReadonlyDocument:
    status_code: int
    content_type: str
    body: bytes


def _resolve_target(url: str):
    # DNS is OS-owned and cannot reliably be cancelled. Timed-out resolutions
    # retain one of two slots until they really exit, preventing unbounded threads.
    if not _RESOLUTION_SLOTS.acquire(blocking=False):
        raise ReadonlyHttpError("目标解析繁忙，请稍后重试")
    try:
        future = _RESOLVER.submit(_assert_safe_remote_target, url)
    except RuntimeError as exc:
        _RESOLUTION_SLOTS.release()
        raise ReadonlyHttpError("目标解析暂不可用") from exc
    future.add_done_callback(lambda _future: _RESOLUTION_SLOTS.release())
    try:
        return future.result(timeout=DNS_TIMEOUT_SECONDS)
    except FutureTimeout as exc:
        raise ReadonlyHttpError("目标地址解析超时，请稍后重试") from exc
    except (ValueError, OSError) as exc:
        raise ReadonlyHttpError("目标地址未通过 HTTPS、私网访问或安全解析检查") from exc


async def _fetch_document(url: str, target, authorization: str | None = None) -> ReadonlyDocument:
    headers = {"Accept-Encoding": "identity"}
    if authorization is not None:
        headers["Authorization"] = authorization
    async with _pinned_http_client(target, headers=headers,
                                  timeout=httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=5.0)) as client:
        async with client.stream("GET", url) as response:
            status = response.status_code
            if status in {401, 403} or 300 <= status < 400:
                return ReadonlyDocument(status, "", b"")
            if status != 200:
                raise ReadonlyHttpError("目标页面暂不可读取，请核对允许路径或稍后重试")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
            if content_type not in _TEXT_TYPES:
                raise ReadonlyHttpError("目标页面不是可读取的 HTML、文本或 JSON，请改用受管资料上传")
            if response.headers.get("content-encoding", "identity").strip().casefold() not in {"", "identity"}:
                raise ReadonlyHttpError("目标返回了压缩内容，请提供未压缩页面或导出资料")
            declared_length = response.headers.get("content-length")
            if declared_length is not None and (not declared_length.isdecimal() or int(declared_length) > MAX_DOCUMENT_BYTES):
                raise ReadonlyHttpError("目标页面超过 256 KB 调查上限，请选择更小页面或上传导出资料")
            body = bytearray()
            async for chunk in response.aiter_raw(chunk_size=8192):
                if len(body) + len(chunk) > MAX_DOCUMENT_BYTES:
                    raise ReadonlyHttpError("目标页面超过 256 KB 调查上限，请选择更小页面或上传导出资料")
                body.extend(chunk)
            return ReadonlyDocument(status, content_type, bytes(body))


def read_document(url: str, *, authorization: str | None = None) -> ReadonlyDocument:
    if not url.startswith("https://"):
        raise ReadonlyHttpError("目标调查仅支持 HTTPS")
    target = _resolve_target(url)

    async def bounded_read() -> ReadonlyDocument:
        return await asyncio.wait_for(_fetch_document(url, target, authorization), HTTP_TIMEOUT_SECONDS)

    try:
        return asyncio.run(bounded_read())
    except ReadonlyHttpError:
        raise
    except (httpx.HTTPError, TimeoutError, UnicodeError, ValueError) as exc:
        raise ReadonlyHttpError("目标页面读取失败或超时，未执行任何登录或表单提交") from exc
