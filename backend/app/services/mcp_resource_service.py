"""Read MCP resources using the platform's pinned, remote-only transport.

Resources are read through the protocol's read operation. Tool annotations do
not authorize tools/call, and stdio never becomes executable through this path.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from ..config import get_settings
from ..models import MCPConfig
from . import mcp_service
from .mcp_bounded_transport_service import bounded_session


MAX_RESOURCE_ITEMS = 40
MAX_RESOURCE_TEXT = 24_000
MAX_RESOURCE_PARTS = 20
REMOTE_TRANSPORTS = frozenset({"sse", "streamable_http", "http"})


class MCPResourceError(ValueError):
    """Stable resource-protocol failure safe to show in a conversation."""


@asynccontextmanager
async def _session(cfg: MCPConfig):
    if cfg.transport not in REMOTE_TRANSPORTS:
        raise ValueError("业务调查只支持远程 MCP 只读资料连接")
    async with bounded_session(cfg) as session:
        yield session


async def _list(cfg: MCPConfig) -> dict:
    async with _session(cfg) as session:
        result = await session.list_resources()
        items = result.resources
        return {
            "resources": [
                {"uri": str(item.uri), "name": str(item.name)[:200],
                 "description": str(item.description or "")[:2000]}
                for item in items[:MAX_RESOURCE_ITEMS]
            ],
            "has_more": bool(result.nextCursor) or len(items) > MAX_RESOURCE_ITEMS,
        }


async def _read(cfg: MCPConfig, uri: str) -> dict:
    from pydantic import AnyUrl

    async with _session(cfg) as session:
        result = await session.read_resource(AnyUrl(uri))
        if len(result.contents) > MAX_RESOURCE_PARTS:
            raise ValueError("MCP 资料片段过多，请在来源系统缩小资料范围")
        parts: list[str] = []
        size = 0
        for content in result.contents:
            text = getattr(content, "text", None)
            if not isinstance(text, str):
                raise ValueError("该 MCP 资料不是可读取文本，请提供文本资料或受管附件")
            parts.append(text)
            size += len(text) + (1 if len(parts) > 1 else 0)
            if size > MAX_RESOURCE_TEXT:
                raise ValueError("MCP 资料超过调查文本上限，请在来源系统提供更小的资料")
        if not parts:
            raise ValueError("MCP 未返回可读取资料")
        return {"text": "\n".join(parts), "read_only": True}


def _run(coro):
    timeout = max(5.0, float(get_settings().mcp_operation_timeout_seconds))
    try:
        return mcp_service._run(asyncio.wait_for(coro, timeout=timeout))
    except Exception as exc:
        # Provider errors may echo credentials or resource content. The worker
        # records an error category; only this stable instruction is public.
        raise MCPResourceError("MCP 资料读取失败；请确认服务支持只读资源协议并检查连接配置") from exc


def list_resources(cfg: MCPConfig) -> dict:
    return _run(_list(cfg))


def read_resource(cfg: MCPConfig, uri: str) -> dict:
    return _run(_read(cfg, uri))
