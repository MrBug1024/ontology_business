"""MCP 服务：连接 MCP Server（stdio / sse / streamable_http），列出并调用工具。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ipaddress
import re
import socket
from typing import Any, Callable
from urllib.parse import urlsplit

import httpcore
import httpx

from ..config import get_settings
from ..models import MCPConfig


_BLOCKED_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


@dataclass(frozen=True)
class _PinnedTarget:
    """One validated network destination for an MCP origin."""

    scheme: str
    hostname: str
    port: int
    authority: str
    address: str


def _version_pair(value: str) -> tuple[int, int] | None:
    try:
        major, minor, *_rest = str(value).split(".")
        return int(major), int(minor)
    except (TypeError, ValueError):
        return None


def _assert_pinning_runtime_compatibility() -> None:
    """Fail closed if the private httpcore SNI extension contract drifts."""
    httpx_version = _version_pair(getattr(httpx, "__version__", ""))
    httpcore_version = _version_pair(getattr(httpcore, "__version__", ""))
    if httpx_version not in {(0, 27), (0, 28)} or httpcore_version != (1, 0):
        raise RuntimeError(
            "MCP 安全固定 IP 需要 httpx 0.27/0.28 与 httpcore 1.0；"
            "当前依赖版本未经验证，已拒绝建立远程连接"
        )


class _PinnedAsyncHTTPTransport(httpx.AsyncBaseTransport):
    """Connect to one validated IP while retaining the logical Host and TLS SNI.

    ``sni_hostname`` is an httpcore 1.0 request extension rather than a stable
    public HTTPX API. Construction therefore asserts the supported dependency
    window above and fails closed after dependency drift.
    """

    def __init__(
        self,
        target: _PinnedTarget,
        *,
        inner: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        _assert_pinning_runtime_compatibility()
        self._target = target
        self._inner = inner or httpx.AsyncHTTPTransport(trust_env=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request_host = request.url.raw_host.decode("ascii").casefold()
        request_port = request.url.port or (443 if request.url.scheme == "https" else 80)
        if (
            request.url.scheme != self._target.scheme
            or request_host != self._target.hostname.casefold()
            or request_port != self._target.port
        ):
            raise httpx.TransportError("MCP transport attempted to leave its pinned origin")

        extensions = dict(request.extensions)
        if self._target.scheme == "https":
            extensions["sni_hostname"] = self._target.hostname
        headers = request.headers.copy()
        headers["Host"] = self._target.authority
        pinned_request = httpx.Request(
            request.method,
            request.url.copy_with(host=self._target.address),
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )
        return await self._inner.handle_async_request(pinned_request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _request_headers(cfg: MCPConfig) -> dict[str, str]:
    headers: dict[str, str] = {}
    seen: set[str] = set()
    for raw_key, raw_value in (cfg.headers or {}).items():
        key = str(raw_key).strip()
        value = str(raw_value)
        if not key or key.lower() in _BLOCKED_HEADERS:
            raise ValueError(f"MCP 请求头不受支持: {key or '空名称'}")
        identity = key.casefold()
        if identity in seen:
            raise ValueError(f"MCP 请求头存在重复名称: {key}")
        seen.add(identity)
        if any(mark in key or mark in value for mark in ("\r", "\n")):
            raise ValueError(f"MCP 请求头不能包含换行符: {key}")
        headers[key] = value
    return headers


def _allowed_private_host(hostname: str) -> bool:
    allowed = {
        value.strip().rstrip(".").casefold()
        for value in get_settings().mcp_private_host_allowlist.split(",")
        if value.strip()
    }
    return hostname.rstrip(".").casefold() in allowed


def _assert_safe_remote_target(url: str) -> _PinnedTarget:
    """Resolve once, validate every answer and return one immutable target."""
    parsed = urlsplit(str(url or "").strip())
    settings = get_settings()
    allowed_schemes = {"https", "http"} if settings.allow_insecure_mcp_http else {"https"}
    if parsed.scheme not in allowed_schemes or not parsed.hostname:
        raise ValueError("远程 MCP 仅允许公网 HTTPS 目标")
    if parsed.username or parsed.password:
        raise ValueError("MCP URL 不能包含用户凭据")
    hostname = parsed.hostname.rstrip(".").casefold()
    allow_private = _allowed_private_host(hostname)
    if hostname == "localhost" or hostname.endswith(".localhost"):
        if not allow_private:
            raise ValueError("远程 MCP 不允许访问本机或内网主机")
    try:
        normalized_url = httpx.URL(str(url or "").strip())
        ascii_hostname = normalized_url.raw_host.decode("ascii")
        port = normalized_url.port or (443 if parsed.scheme == "https" else 80)
        authority = normalized_url.netloc.decode("ascii")
    except (UnicodeError, ValueError) as exc:
        raise ValueError("远程 MCP 目标地址无效") from exc
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if not allow_private and not literal.is_global:
            raise ValueError("远程 MCP 不允许访问本机、私网、链路本地或保留地址")
        return _PinnedTarget(
            scheme=parsed.scheme,
            hostname=ascii_hostname,
            port=port,
            authority=authority,
            address=str(literal),
        )
    try:
        resolved: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        seen: set[str] = set()
        for item in socket.getaddrinfo(ascii_hostname, port, type=socket.SOCK_STREAM):
            address = ipaddress.ip_address(str(item[4][0]).split("%", 1)[0])
            if str(address) not in seen:
                seen.add(str(address))
                resolved.append(address)
    except (OSError, ValueError) as exc:
        raise ValueError("远程 MCP 目标主机无法安全解析") from exc
    if not resolved or (not allow_private and any(not address.is_global for address in resolved)):
        raise ValueError("远程 MCP 目标解析到本机、私网、链路本地或保留地址")
    return _PinnedTarget(
        scheme=parsed.scheme,
        hostname=ascii_hostname,
        port=port,
        authority=authority,
        address=str(resolved[0]),
    )


def _pinned_http_client(
    target: _PinnedTarget,
    *,
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout or httpx.Timeout(30.0, read=get_settings().mcp_operation_timeout_seconds),
        auth=auth,
        follow_redirects=False,
        trust_env=False,
        transport=_PinnedAsyncHTTPTransport(target),
    )


def _sse_http_client_factory(
    target: _PinnedTarget,
) -> Callable[..., httpx.AsyncClient]:
    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        return _pinned_http_client(
            target,
            headers=headers,
            timeout=timeout,
            auth=auth,
        )

    return factory


def public_error(error: BaseException | str, cfg: MCPConfig | None = None) -> str:
    """Return a bounded transport error without echoing common credentials."""
    message = str(error or "MCP 连接失败")
    message = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1***", message)
    message = re.sub(r"(?i)(basic\s+)[^\s,;]+", r"\1***", message)
    message = re.sub(
        r"(?i)((?:api[_-]?key|access[_-]?token|authorization|cookie|password|secret)=)[^&\s]+",
        r"\1***",
        message,
    )
    message = re.sub(r"([?&][^=&#\s]+)=([^&#\s]+)", r"\1=***", message)
    if cfg is not None:
        for value in [
            *(str(item) for item in (cfg.headers or {}).values()),
            *(str(item) for item in (cfg.env or {}).values()),
        ]:
            if len(value) >= 3:
                message = message.replace(value, "***")
    return message[:1_000]


def _run(coro):
    """在同步上下文中运行协程（避免与已有事件循环冲突）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


async def _list_tools_async(cfg: MCPConfig) -> list[dict[str, Any]]:
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.streamable_http import streamable_http_client

    if cfg.transport == "stdio":
        if not get_settings().allow_mcp_stdio:
            raise ValueError("当前部署未开启服务端 stdio MCP")
        params = StdioServerParameters(command=cfg.command, args=cfg.args or [], env=cfg.env or None)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.list_tools()
                return _tools_to_dict(res.tools)
    if cfg.transport == "sse":
        target = await asyncio.to_thread(_assert_safe_remote_target, cfg.url)
        async with sse_client(
            cfg.url,
            headers=_request_headers(cfg) or None,
            sse_read_timeout=get_settings().mcp_operation_timeout_seconds,
            httpx_client_factory=_sse_http_client_factory(target),
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.list_tools()
                return _tools_to_dict(res.tools)
    if cfg.transport in ("streamable_http", "http"):
        target = await asyncio.to_thread(_assert_safe_remote_target, cfg.url)
        async with _pinned_http_client(
            target,
            headers=_request_headers(cfg),
            timeout=httpx.Timeout(30.0, read=get_settings().mcp_operation_timeout_seconds),
        ) as http_client:
            async with streamable_http_client(cfg.url, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    res = await session.list_tools()
                    return _tools_to_dict(res.tools)
    raise ValueError(f"未知 MCP 传输类型: {cfg.transport}")


async def _call_tool_async(
    cfg: MCPConfig,
    name: str,
    arguments: dict[str, Any],
    *,
    execution_key: str | None = None,
) -> dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.streamable_http import streamable_http_client

    tool_metadata = (
        {"com.ontology-platform/capability-execution-key": execution_key}
        if execution_key
        else None
    )

    if cfg.transport == "stdio":
        if not get_settings().allow_mcp_stdio:
            raise ValueError("当前部署未开启服务端 stdio MCP")
        params = StdioServerParameters(command=cfg.command, args=cfg.args or [], env=cfg.env or None)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool(name, arguments, meta=tool_metadata)
                return _content_to_text(res)
    if cfg.transport == "sse":
        target = await asyncio.to_thread(_assert_safe_remote_target, cfg.url)
        async with sse_client(
            cfg.url,
            headers=_request_headers(cfg) or None,
            sse_read_timeout=get_settings().mcp_operation_timeout_seconds,
            httpx_client_factory=_sse_http_client_factory(target),
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool(name, arguments, meta=tool_metadata)
                return _content_to_text(res)
    if cfg.transport in ("streamable_http", "http"):
        target = await asyncio.to_thread(_assert_safe_remote_target, cfg.url)
        async with _pinned_http_client(
            target,
            headers=_request_headers(cfg),
            timeout=httpx.Timeout(30.0, read=get_settings().mcp_operation_timeout_seconds),
        ) as http_client:
            async with streamable_http_client(cfg.url, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    res = await session.call_tool(name, arguments, meta=tool_metadata)
                    return _content_to_text(res)
    raise ValueError(f"未知 MCP 传输类型: {cfg.transport}")


def _tools_to_dict(tools) -> list[dict[str, Any]]:
    out = []
    for t in tools:
        out.append(
            {
                "name": t.name,
                "description": getattr(t, "description", "") or "",
                "input_schema": getattr(t, "inputSchema", {}) or {},
            }
        )
    return out


def _content_to_text(res) -> dict[str, Any]:
    parts: list[str] = []
    for c in getattr(res, "content", []) or []:
        if hasattr(c, "text"):
            parts.append(c.text)
        else:
            parts.append(str(c))
    return {"status": "success" if not getattr(res, "isError", False) else "error", "text": "\n".join(parts)}


def list_tools(cfg: MCPConfig) -> list[dict[str, Any]]:
    timeout = max(5.0, float(get_settings().mcp_operation_timeout_seconds))
    return _run(asyncio.wait_for(_list_tools_async(cfg), timeout=timeout))


def call_tool(
    cfg: MCPConfig,
    name: str,
    arguments: dict[str, Any],
    *,
    execution_key: str | None = None,
) -> dict[str, Any]:
    timeout = max(5.0, float(get_settings().mcp_operation_timeout_seconds))
    try:
        return _run(
            asyncio.wait_for(
                _call_tool_async(
                    cfg,
                    name,
                    arguments,
                    execution_key=execution_key,
                ),
                timeout=timeout,
            )
        )
    except Exception as exc:  # noqa: BLE001
        raise ValueError(public_error(exc, cfg)) from None


def test_connection(cfg: MCPConfig) -> tuple[bool, str]:
    try:
        tools = list_tools(cfg)
        return True, f"连接成功，发现 {len(tools)} 个工具"
    except Exception as exc:  # noqa: BLE001
        return False, public_error(exc, cfg)
